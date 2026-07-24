"""
MRED / MLS listing sheet parser.

Extracts structured, buyer-relevant fields plus the embedded property photo
from a standard MRED-style MLS listing sheet PDF (the kind produced by
MRED Connect / MLS "Full Report" export), so it can be rendered into a
branded, client-facing flyer.

This is built and tuned against the "Attached Single" (condo) template but
uses generic label:value scraping so it degrades gracefully on other
property types (detached single, multi-unit) rather than crashing.
"""
import io
import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import pdfplumber


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grab(text, label, stop_labels):
    """Grab the value following `label:` up until the next of `stop_labels`,
    a newline, or end of string. Labels/stops are plain strings (already
    escaped) matched literally."""
    pattern = re.escape(label) + r":\s*(.*?)(?=" + "|".join(re.escape(s) for s in stop_labels) + r"|\n|$)"
    m = re.search(pattern, text)
    if not m:
        return ""
    return m.group(1).strip(" ,")


def _grab_no_prefix(text, label, bad_prefixes, stop_labels):
    """Like _grab(), but skips any match for `label:` that's immediately
    preceded by one of `bad_prefixes` -- e.g. label="Ownership",
    bad_prefixes=["Garage ", "Parking "] matches a standalone "Ownership:"
    but not "Garage Ownership:"/"Parking Ownership:". _grab()'s plain
    re.search always returns the FIRST occurrence of "label:" anywhere in
    the text, which is a real problem when a genuinely different field
    happens to end in the same word (see the MRED "Ownership:" callers).
    Each prefix gets its own (?<!...) lookbehind rather than one
    alternation -- Python's re requires a fixed-width lookbehind, and
    "Garage " / "Parking " are different lengths, so `(?<!Garage |Parking
    )` fails to compile at all while chained individual lookbehinds work
    fine."""
    prefix_lookbehinds = "".join(f"(?<!{re.escape(p)})" for p in bad_prefixes)
    pattern = (
        prefix_lookbehinds + re.escape(label) + r":\s*(.*?)(?="
        + "|".join(re.escape(s) for s in stop_labels) + r"|\n|$)"
    )
    m = re.search(pattern, text)
    if not m:
        return ""
    return m.group(1).strip(" ,")


def _first_num(s):
    m = re.search(r"[\d,]+", s or "")
    return m.group(0) if m else ""


def _column_text(words, x_min, x_max, top_min, top_max, row_tol=3):
    """Reconstruct text for one column of a multi-column layout by selecting
    whole words whose *start* x-position falls inside [x_min, x_max) and whose
    top falls inside [top_min, top_max), grouping into rows by proximity.

    This is safer than pdfplumber's bbox-cropping for narrow columns because
    it assigns each *whole word* to a column (by where the word begins)
    instead of clipping glyphs at a hard pixel boundary, which can chop a
    word in half when it happens to straddle the column edge.
    """
    picked = [
        w for w in words
        if top_min <= w["top"] < top_max and x_min <= w["x0"] < x_max
    ]
    picked.sort(key=lambda w: (w["top"], w["x0"]))
    rows = []
    current_top = None
    current_words = []
    for w in picked:
        if current_top is None or abs(w["top"] - current_top) <= row_tol:
            current_words.append(w)
            current_top = w["top"] if current_top is None else current_top
        else:
            rows.append(current_words)
            current_words = [w]
            current_top = w["top"]
    if current_words:
        rows.append(current_words)
    lines = [" ".join(w["text"] for w in row) for row in rows]
    return "\n".join(lines)


def _degarble(text):
    """Some source PDFs have a text-layer defect where two overlapping text
    runs get interleaved character-by-character (e.g. "Appliances:" comes
    out as "A p p li a n c e s :"), apparently from an MLS export quirk on
    the original agent's end -- not something recoverable byte-for-byte on
    our side. Left in place, it prevents every stop-label match after it
    from ever firing, silently swallowing every real field that follows in
    that column. This strips runs of 5+ consecutive 1-2 character "words"
    (a strong signature of that corruption, essentially never occurring in
    genuine field values) so extraction can pick back up cleanly at the
    next real label."""
    tokens = text.split(" ")
    out = []
    i = 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and len(tokens[j].strip(",:;-")) <= 2:
            j += 1
        if j - i >= 5:
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(t for t in out if t)


def money(s):
    if not s:
        return ""
    s = s.strip()
    # A bare "$" with no digits happens when the caller builds this from a
    # grab that came back empty (e.g. "$" + _first_num("")) -- most often
    # because the Assessments/Tax grid this value would normally come from
    # doesn't exist at all on a compact/private-network export. That's
    # meaningfully different from a real $0, so it's treated the same as
    # "no value" rather than rendered as a bare, confusing "$".
    if not re.search(r"\d", s):
        return ""
    if not s.startswith("$"):
        return s
    return s


# MRED fills in several fields with a literal "No"/"None" rather than
# leaving them blank when there's genuinely nothing to report (Special
# Assessments, Tax Exemptions), so a plain truthiness check isn't enough to
# keep MLS boilerplate off the buyer-facing flyer -- confirmed shipping on
# real output as "Special: No" / "Exemptions: None" before this was added.
# Mirrors parser_michric.py's _is_nullish(), with "no" added since MRED
# specifically uses it as a null placeholder on these boolean-style fields
# (unlike MichRIC's set, which is used for free-text label:value fields
# where "No" can be a legitimate answer worth keeping).
_NULLISH = {"none", "no", "0", "n/a", "na", "-"}


def _is_nullish(val):
    return (val or "").strip().lower() in _NULLISH


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    mls_number: str = ""
    property_type: str = ""
    architectural_style: str = ""  # e.g. MichRIC "Contemporary"/"Ranch" --
                                    # distinct from and more specific than
                                    # property_type ("Single Family
                                    # Residence")
    status: str = ""
    list_date: str = ""
    dom_list_side: str = ""
    dom_total: str = ""
    new_construction: str = ""  # Yes/No -- only worth a badge when "Yes"
    list_price: str = ""
    orig_list_price: str = ""  # MRED "Orig List Price:" -- differs from
                                # list_price only after a price change, in
                                # which case it's a real negotiation signal
                                # worth surfacing (see render.py's
                                # price_change_note()).
    address_line1: str = ""
    city: str = ""
    county: str = ""
    state: str = ""
    zip_code: str = ""
    directions: str = ""
    curr_leased: str = ""  # MRED "Curr. Leased:" -- Yes means the property
                            # is currently tenant-occupied, which affects a
                            # buyer's move-in timeline. See render.py's
                            # market_time_display().
    exposure: str = ""  # MRED "Exposure:" -- unit-facing direction(s), e.g.
                         # "N (North), W (West)". Condo-relevant (affects
                         # natural light expectations); folded into
                         # interior_features rather than given its own card.

    bedrooms: str = ""
    bathrooms_full: str = ""
    bathrooms_half: str = ""
    rooms_total: str = ""
    approx_sf: str = ""
    year_built: str = ""
    age: str = ""
    ownership: str = ""

    parking_type: str = ""
    parking_spaces: str = ""
    garage_details: str = ""
    garage_type: str = ""  # MRED "Garage Type:" -- Attached / Detached.
                            # parking_type only says the broad category
                            # ("Garage" vs "Space/s" vs "Driveway" etc,
                            # from the page-1 header "Parking:" field);
                            # this is the specific attached-vs-detached
                            # fact buyers actually ask about, and lives in
                            # a completely different part of the sheet (the
                            # feature grid, not the header), so it needs
                            # its own field rather than folding into
                            # parking_type. See render.py's feature_groups().
    garage_ownership: str = ""  # MRED "Garage Ownership:" -- e.g. "Deeded
                                 # Sold Separately ($25,000)". A financially
                                 # significant detail that parking_incl_in_
                                 # price alone doesn't convey (it only says
                                 # whether parking is included, not what it
                                 # costs if it isn't). See render.py's
                                 # parking_note().
    parking_incl_in_price: str = ""
    lot_size: str = ""

    total_units: str = ""
    total_stories: str = ""
    unit_floor_level: str = ""

    basement: str = ""
    basement_bath: str = ""
    fireplaces: str = ""
    fireplace_details: str = ""  # type and/or room, e.g. "Wood Burning,
                                  # Living Room" -- fireplaces above is
                                  # just the bare count; buyers asked for
                                  # more than a number when it's available
    stories: str = ""

    possession: str = ""  # e.g. MRED "Possession:" ("Closing", "Negotiable")
                           # or MichRIC "Possession:" -- when a buyer can
                           # actually move in, a real gap the bare price/
                           # facts strip doesn't answer
    pct_owner_occupied: str = ""  # MRED "% Own. Occ.:" -- condo-specific;
                                   # affects FHA/some conventional loan
                                   # eligibility, so worth surfacing when
                                   # the sheet has it

    assessment_amount: str = ""
    assessment_frequency: str = ""
    assessment_includes: str = ""
    special_assessments: str = ""

    tax_amount: str = ""
    tax_year: str = ""
    tax_exemptions: str = ""
    mult_pins: str = ""
    tax_taxable_value: str = ""  # MichRIC "Taxable Value:" -- what the
                                  # shown tax_amount is actually based on
                                  # for the *current* owner
    tax_sev: str = ""  # MichRIC "SEV:" (State Equalized Value) -- MI
                        # taxable value uncaps to match this after a sale,
                        # so a real gap here means a buyer's actual future
                        # tax bill will likely be meaningfully higher than
                        # tax_amount above. See render.py's tax_uncap_note().
    homestead_pct: str = ""  # MichRIC "Homestead %:" -- what share of the
                              # *current* owner's tax bill is at the lower
                              # homestead (Principal Residence Exemption)
                              # rate vs. the higher non-homestead rate. A
                              # buyer whose own homestead status differs
                              # from this will see a meaningfully different
                              # effective rate than the current bill implies
                              # -- see render.py's tax_uncap_note().

    elementary: str = ""
    junior_high: str = ""
    high_school: str = ""
    school_district: str = ""  # single-name district (e.g. MichRIC sheets,
                                # which don't split out elementary/jr/high)

    pets_allowed: str = ""
    max_pet_weight: str = ""

    remarks: str = ""

    interior_features: str = ""
    exterior_features: str = ""
    water_features: str = ""  # e.g. MichRIC "Water Fea. Amenities" -- kept
                               # separate from exterior_features so it can
                               # get its own prominent card on waterfront
                               # listings instead of being buried
    pool: str = ""  # also surfaced on the Water Access & Features card,
                     # not folded into exterior_features -- see render.py's
                     # water_features_display()
    water_source: str = ""  # e.g. MichRIC "Water:" -- Public/Well. A
                             # well-vs-municipal distinction is a real,
                             # cost-relevant fact for MI buyers specifically
                             # (rural/lakefront properties), so it's kept
                             # separate rather than folded into a generic
                             # utilities blob and risking getting buried.
    sewer_type: str = ""  # e.g. MichRIC "Sewer:" -- Public Sewer/Septic
                           # Tank. Same reasoning as water_source.
    waterfront: str = ""  # MichRIC "Waterfront:" -- Yes/No. A property
                           # can have water_features (deeded/public access)
                           # without being Waterfront itself -- this is the
                           # one unambiguous flag for that distinction.
    water_access: str = ""  # MichRIC "Water Access Y/N:" -- captured for
                             # completeness but not necessarily rendered;
                             # seen disagreeing with the presence of real
                             # water_features text on at least one real
                             # sample, so treat as informational only.
    body_of_water: str = ""  # e.g. "Paw Paw Lake" -- MichRIC "Body of
                              # Water:"
    water_frontage_ft: str = ""  # MichRIC "Water Frontage:" (feet)
    heating: str = ""
    cooling: str = ""
    kitchen_features: str = ""
    appliances: str = ""
    bath_amenities: str = ""
    amenities: str = ""
    laundry: str = ""

    rooms: list = field(default_factory=list)  # list of dicts: name/size/level/flooring

    list_broker_name: str = ""
    list_brokerage: str = ""
    list_broker_phone: str = ""

    photo_bytes: bytes = None
    photo_ext: str = "jpg"

    source_filename: str = ""

    @property
    def full_address(self):
        parts = [self.address_line1]
        loc = ", ".join(p for p in [self.city, self.state] if p)
        if loc:
            parts.append(loc + (f" {self.zip_code}" if self.zip_code else ""))
        return ", ".join(parts)

    @property
    def street_address(self):
        return self.address_line1

    @property
    def city_state_zip(self):
        loc = ", ".join(p for p in [self.city, self.state] if p)
        return (loc + (f" {self.zip_code}" if self.zip_code else "")).strip()

    @property
    def bathrooms_display(self):
        full = self.bathrooms_full or "0"
        half = self.bathrooms_half or "0"
        if half and half != "0":
            return f"{full}.{1 if half else 0}"
        return full

    @property
    def file_safe_name(self):
        base = self.address_line1 or self.mls_number or "listing"
        base = re.sub(r"[^A-Za-z0-9 _-]", "", base).strip().replace(" ", "_")
        return base or "listing"


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_listing_pdf(file_bytes: bytes, source_filename: str = "") -> Listing:
    listing = Listing(source_filename=source_filename)

    # --- Text extraction (pdfplumber gives clean reading-order text) -------
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page1_text = pdf.pages[0].extract_text() or ""
        page2_text = pdf.pages[1].extract_text() if len(pdf.pages) > 1 else ""

    full_text = page1_text + "\n" + page2_text

    # --- Property type / MLS# / price -------------------------------------
    # Some exports (e.g. browser "Print to PDF") prepend a timestamp/site
    # banner line before the real listing header, so this can't be anchored
    # to the very start of the page text -- search for it instead of
    # matching only at position 0.
    m = re.search(r"([A-Za-z ]+?)\s*MLS #:\s*(\d+)", page1_text)
    if m:
        listing.property_type = m.group(1).strip()
        listing.mls_number = m.group(2).strip()

    listing.list_price = money(_grab(page1_text, "List Price", ["Orig List Price", "\n"]))
    listing.status = _grab(page1_text, "Status", ["List Date"])
    listing.list_date = _grab(page1_text, "List Date", ["Orig List Price"])
    if not listing.list_date:
        # Compact/private-network exports (Status starting "PRIV") have no
        # "List Date:" at all -- "Actv. Date:" (the date this listing went
        # active on the private network) is the closest equivalent.
        listing.list_date = _grab(page1_text, "Actv. Date", ["Max List Price", "\n"])
    listing.orig_list_price = money(_grab(page1_text, "Orig List Price", ["\n"]))

    # "Mkt. Time (Lst./Tot.)" is MRED's days-on-market field: the first
    # number is time on the *current* listing period, the second is
    # cumulative across any relists -- they differ only if this property
    # has been relisted, so both are worth keeping. Listings that are no
    # longer straightforwardly active (e.g. status CTG/contingent) instead
    # show a single "Lst. Mkt. Time:" total with no Lst./Tot. split.
    m = re.search(r"Mkt\.?\s*Time\s*\(Lst\.?/Tot\.?\):(\d+)\s*/\s*(\d+)", page1_text)
    if m:
        listing.dom_list_side, listing.dom_total = m.group(1), m.group(2)
    else:
        m = re.search(r"Lst\.?\s*Mkt\.?\s*Time:(\d+)", page1_text)
        if m:
            listing.dom_total = m.group(1)

    # --- Address ------------------------------------------------------------
    m = re.search(r"Address:(.*?),\s*([A-Za-z .]+),\s*([A-Z]{2})\s*(\d{5})", full_text)
    if m:
        listing.address_line1 = m.group(1).strip()
        listing.city = m.group(2).strip()
        listing.state = m.group(3).strip()
        listing.zip_code = m.group(4).strip()

    listing.directions = _grab(page1_text, "Directions", ["Sold by"])

    # --- Core facts ----------------------------------------------------------
    listing.year_built = _grab(page1_text, "Year Built", ["Blt Before 78"])
    # Curr. Leased "No" is real, meaningful data (not a null placeholder --
    # unlike Special Assessments/Tax Exmps above), so this is captured as-is
    # rather than run through _is_nullish(); market_time_display() in
    # render.py only surfaces it when it's actually "Yes" anyway.
    listing.curr_leased = _grab(page1_text, "Curr. Leased", ["\n"])
    # Negative lookbehind so this only matches a standalone "Ownership:"
    # label, not "Garage Ownership:"/"Parking Ownership:" -- a plain
    # re.search for "Ownership:" matches the FIRST occurrence anywhere in
    # the page, and on the standard full-listing layout that's harmlessly
    # masked because the real "Ownership:Condo" field happens to appear
    # earlier in reading order than the garage grid. Compact/private-
    # network exports (Status starting "PRIV") have no standalone
    # "Ownership:" field at all, so without the lookbehind this silently
    # grabs "Fee/Leased Parking Ownership:" off the garage block instead.
    listing.ownership = _grab_no_prefix(page1_text, "Ownership", ["Garage ", "Parking "], ["Subdivision"])
    if not listing.ownership:
        # Compact/private-network exports label this "Type Detached/
        # Attached:" instead (e.g. "Type Detached/Attached: Condo").
        listing.ownership = _grab(page1_text, "Type Detached/Attached", ["Basement:", "\n"])
    listing.rooms_total = _grab(page1_text, "Rooms", ["Bathrooms"])
    # "Area:"/"List Price:" added as stops alongside "Master Bath" --
    # compact/private-network exports pack "Bedrooms: 3 Area: 8006 List
    # Price: $525,000" onto a single line with no field-separating
    # newline, and don't have a "Master Bath:" label on this line at all
    # (they use "Master Bedroom Bath:" instead, much further down), so
    # without these extra stops the grab runs to the end of that whole
    # line instead of just the bedroom count.
    listing.bedrooms = _grab(page1_text, "Bedrooms", ["Master Bath", "Master Bedroom Bath", "Area:"])

    # County -- already have the display/template plumbing for this from the
    # MichRIC work (city-line "County" suffix), just never wired up for MRED.
    listing.county = _grab(full_text, "County", ["\n"])

    # Waterfront -- same story: field/display machinery already exists from
    # MichRIC (see water_features_display() in render.py), just never
    # captured for MRED. "No" is real data here (not a null placeholder),
    # so it's kept as-is; water_features_display() already handles not
    # showing a bare "Not Waterfront" on every ordinary city listing.
    listing.waterfront = _grab(full_text, "Waterfront", ["\n"])

    # "Dimensions" is MRED's lot-size field. For most condos it just says
    # COMMON (shared lot), but rowhome-style/low-rise condos and any
    # detached listing can carry real lot dimensions here, which buyers do
    # care about.
    m = re.search(r"Dimensions:(.*?)Ownership:", full_text, re.S)
    if m:
        listing.lot_size = m.group(1).strip()

    # Building-level facts -- especially relevant for condos/co-ops so buyers
    # know building size and where in it this unit sits.
    m = re.search(r"Total Units:(\d+)", page1_text)
    if m:
        listing.total_units = m.group(1)
    m = re.search(r"#\s*Stories:(\d+)", page1_text)
    if m:
        listing.total_stories = m.group(1)
    m = re.search(r"Unit Floor Lvl\.:(\d+)", page1_text)
    if m:
        listing.unit_floor_level = m.group(1)

    # Case-insensitive -- compact/private-network exports write this label
    # lowercase ("Bathrooms (full/half): 2 / 0") where the standard full
    # listing layout capitalizes it ("Bathrooms\n(Full/Half):\n2/0"); a
    # case-sensitive \(Full/Half\) silently misses the lowercase variant
    # and leaves both fields blank.
    m = re.search(r"Bathrooms(?:\s*\(full/half\))?:?\s*(\d+)\s*/\s*(\d+)", page1_text, re.IGNORECASE)
    if m:
        listing.bathrooms_full, listing.bathrooms_half = m.group(1), m.group(2)

    # Basement is a condo-irrelevant, detached/townhome-relevant field --
    # whether it's finished, and what kind (English, walkout, crawl, etc.)
    # is a significant selling point for single-family homes.
    basement_val = _grab(page1_text, "Basement", ["Bsmnt. Bath", "\n"])
    if basement_val and basement_val.lower() != "none":
        listing.basement = basement_val
        listing.basement_bath = _grab(page1_text, "Bsmnt. Bath", ["Parking Incl", "\n"])

    m = re.search(r"#\s*Fireplaces:(\d+)", page1_text)
    if m and m.group(1) != "0":
        listing.fireplaces = m.group(1)

    # "Appx SF:" on the standard full listing layout; compact/private-
    # network exports use "Approx Sq Ft:" instead.
    m = re.search(r"Appx SF:\s*([\d,]+)", page1_text) or re.search(r"Approx Sq Ft:\s*([\d,]+)", page1_text)
    if m:
        sf = m.group(1)
        listing.approx_sf = sf if sf not in ("0", "") else ""

    listing.age = _grab(full_text, "Age", ["Laundry Features", "Type:"])

    # --- Parking ---------------------------------------------------------------
    listing.parking_type = _grab(page1_text, "Parking", ["# Spaces"])
    m = re.search(r"#\s*Spaces:Gar:(\d+)", page1_text)
    if m:
        listing.parking_spaces = m.group(1)
    listing.garage_details = _grab(full_text, "Garage Details", ["Parking Ownership", "\n"])

    # "Parking Incl. In Price" -- distinct from (and not to be confused with)
    # "SP Incl. Parking", which is a sold-price field for closed listings.
    # Buyers regularly get tripped up by parking being available but sold
    # separately, so this needs to be called out explicitly.
    m = re.search(r"(?<!SP )Parking Incl\.(Yes|No)", page1_text)
    if m:
        listing.parking_incl_in_price = m.group(1)

    # --- Multi-page word index ----------------------------------------------------
    # Different MRED export flavors put the same sections on different pages
    # depending on how much content there is -- a long remarks paragraph or a
    # big room grid can push the feature grid or the school/tax block onto
    # page 2 instead of page 1 -- so anchor words are searched across every
    # page rather than assuming a fixed page index.
    pages_words = []
    pages_heights = []
    page_width = 612
    left_margin = 14
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages_words = [p.extract_words() for p in pdf.pages]
            pages_heights = [p.height for p in pdf.pages]
            if pdf.pages:
                page_width = pdf.pages[0].width
    except Exception:
        pass

    def _text_spanning_pages(start_pi, start_top, end_pi, end_top, x_min, x_max):
        """Like _column_text, but for a block whose bottom boundary lands on
        a later page than its top boundary -- a longer listing (bigger
        remarks/room table) can push the trailing anchor word that would
        normally close out a section onto the next page. Reconstructs the
        column text as if the pages were stitched into one tall page."""
        if start_pi == end_pi:
            return _column_text(pages_words[start_pi], x_min, x_max, start_top, end_top)
        parts = [_column_text(pages_words[start_pi], x_min, x_max, start_top, pages_heights[start_pi])]
        for pi in range(start_pi + 1, end_pi):
            parts.append(_column_text(pages_words[pi], x_min, x_max, 0, pages_heights[pi]))
        parts.append(_column_text(pages_words[end_pi], x_min, x_max, 0, end_top))
        return "\n".join(p for p in parts if p)
    all_words_flat = [w for words in pages_words for w in words]
    if all_words_flat:
        # The page's actual left margin varies by export source (native MRED
        # PDF vs. a browser "Print to PDF", different browsers, etc), so
        # anchor words are matched relative to the page's own margin rather
        # than an absolute pixel value -- a hardcoded cutoff like "x0 < 20"
        # is brittle and silently drops whole sections when a differently
        # exported sheet's margin is a few points wider.
        left_margin = min(w["x0"] for w in all_words_flat)
    margin_cutoff = left_margin + 30

    def _find(text, x0_max=None):
        """First (page_index, words, top, x0) match for an exact word,
        searched page by page in order, optionally constrained near the
        left margin."""
        for pi, words in enumerate(pages_words):
            for w in words:
                if w["text"] == text and (x0_max is None or w["x0"] < x0_max):
                    return pi, words, w["top"], w["x0"]
        return None

    def _find_on(words, text, x0_max=None, top_min=None):
        w = next(
            (
                w for w in words
                if w["text"] == text
                and (x0_max is None or w["x0"] < x0_max)
                and (top_min is None or w["top"] >= top_min)
            ),
            None,
        )
        return w["top"] if w else None

    def _find_after(start_pi, start_top, text, x0_max=None):
        """Find a closing anchor word starting on `start_pi` (below
        `start_top`) and, if not there, continuing onto later pages. A
        content-heavy listing can push a section's closing anchor (e.g. the
        'Broker'/'Copyright' line that ends the feature grid) onto the next
        page instead of leaving it on the same page as the section's
        opening anchor, which a same-page-only search would miss entirely,
        silently blanking every field that section holds. Returns
        (page_index, top) or None."""
        top = _find_on(pages_words[start_pi], text, x0_max, top_min=start_top)
        if top is not None:
            return start_pi, top
        for pi in range(start_pi + 1, len(pages_words)):
            top = _find_on(pages_words[pi], text, x0_max)
            if top is not None:
                return pi, top
        return None

    def _find_phrase(text1, text2, max_gap=45, top_tol=3, min_top=None):
        """First (page_index, words, top, x0) of `text1` immediately
        followed by `text2` on the same line (small vertical tolerance, a
        few points to the right) -- used to anchor on a two-word header
        like "School Data" or "Square Footage" without depending on where
        on the page it happens to sit. Matching the pair rather than either
        word alone avoids false hits from the same word appearing elsewhere
        as plain body text (e.g. "0-1000 Square Feet" is not "Square
        Footage")."""
        for pi, words in enumerate(pages_words):
            ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
            for i, w in enumerate(ordered):
                if w["text"] != text1:
                    continue
                if min_top is not None and w["top"] <= min_top:
                    continue
                for w2 in ordered[i + 1:i + 4]:
                    if (
                        abs(w2["top"] - w["top"]) <= top_tol
                        and w["x0"] < w2["x0"] <= w["x0"] + max_gap
                        and w2["text"] == text2
                    ):
                        return pi, words, w["top"], w["x0"]
        return None

    def _word_on_row(words, text, row_top, tol=3, x0_min=None):
        """First occurrence of an exact word on a specific line (by top),
        optionally required to sit to the right of some x-position -- used
        to read off the rest of a header row once its left-most word (e.g.
        "Assessments") has already been located."""
        for w in words:
            if (
                w["text"] == text
                and abs(w["top"] - row_top) <= tol
                and (x0_min is None or w["x0"] > x0_min)
            ):
                return w["top"], w["x0"]
        return None

    # Detached/fee-simple listings don't carry a Pet Info column (no HOA
    # pet rules), so MRED swaps it for a "Miscellaneous" column instead --
    # that word only ever appears in that layout, making it a clean
    # discriminator for which of the two block layouts this sheet uses.
    has_misc_column = any(w["text"] == "Miscellaneous" for words in pages_words for w in words)

    school_col = assess_col = tax_col = pet_col = ""

    if has_misc_column:
        # Detached-style layout: "School Data" is its own single-column
        # block, followed further down the page by a separate 3-column
        # "Assessments / Tax / Miscellaneous" row. Unlike the condo 4-column
        # block, each row here already reads correctly left-to-right in
        # plain text (nothing else sits beside "School Data" at its height),
        # so pull values directly with regex instead of reconstructing
        # columns by x-position, which isn't stable in this layout (the
        # whole block sits in a variable-width sidebar next to the remarks
        # paragraph rather than spanning the full page width).
        listing.elementary = _grab(full_text, "Elementary", ["\n"])
        listing.junior_high = _grab(full_text, "Junior High", ["\n"])
        listing.high_school = _grab(full_text, "High School", ["\n"])

        m = re.search(r"Amount:\$?([\d,]+(?:\.\d+)?)\s*Amount:\$?([\d,]+(?:\.\d+)?)", full_text)
        if m:
            listing.assessment_amount = money("$" + m.group(1))
            listing.tax_amount = money("$" + m.group(2))
        special_assessments = _grab(full_text, "Special Assessments", ["Tax Year", "\n"])
        listing.special_assessments = "" if _is_nullish(special_assessments) else special_assessments
        listing.tax_year = _grab(full_text, "Tax Year", ["Flood Zone", "\n"])
        tax_exemptions = _grab(full_text, "Tax Exmps", ["Appx SF", "Main +", "\n"])
        listing.tax_exemptions = "" if _is_nullish(tax_exemptions) else tax_exemptions
        listing.mult_pins = _grab(full_text, "Mult PINs", ["Habitable", "\n"])
    else:
        # Condo/attached layout: School Data / Assessments / Tax / Pet Info.
        # Two sub-variants of this layout both appear in real MRED exports:
        # sometimes all four sit in one shared row (a plain linear read
        # jumbles them together), and sometimes School Data is its own
        # block *above* a separate Assessments/Tax/Pet Info row (each still
        # a 3-way jumble on their own). Rather than crop by a hardcoded
        # pixel range -- which only matches whichever sample this was first
        # tuned against, and silently returns nothing for the other -- find
        # each column's actual header position on the page and derive the
        # crop boundaries from that, so both variants (and any export with
        # slightly different margins) resolve the same way.
        header_hit = _find_phrase("School", "Data")
        if header_hit:
            pi, words, top_school, x0_school = header_hit
            assess_hit = _find("Assessments")
            if assess_hit and assess_hit[0] == pi:
                _, _, top_assess, x0_assess = assess_hit
                tax_pos = _word_on_row(words, "Tax", top_assess, x0_min=x0_assess)
                pet_pos = _word_on_row(words, "Pet", top_assess, x0_min=(tax_pos[1] if tax_pos else x0_assess))
                footer_hit = _find_phrase("Square", "Footage", min_top=top_assess)
                if tax_pos and pet_pos and footer_hit and footer_hit[0] == pi:
                    _, _, top_footer, _ = footer_hit
                    x0_tax, x0_pet = tax_pos[1], pet_pos[1]
                    # A column's *values* can start to the left of its own
                    # header word (e.g. a "Tax" header can sit further
                    # right than that column's "Amount:$2,077" line below
                    # it), so cropping right at each header's x0 leaks the
                    # next column's numbers into the previous one. The
                    # midpoint between two consecutive headers is a safer
                    # boundary -- it doesn't assume either column's values
                    # line up with its own label.
                    boundary_assess_tax = (x0_assess + x0_tax) / 2
                    boundary_tax_pet = (x0_tax + x0_pet) / 2
                    # School Data sits above the Assessments row in the
                    # stacked variant (top_assess clearly greater than
                    # top_school) or shares its row in the single-row
                    # variant (top_assess == top_school) -- either way, its
                    # bottom edge is wherever the Assessments row starts,
                    # unless they're the same row, in which case it (like
                    # the other three columns) runs down to the footer. In
                    # the stacked case nothing else shares its vertical
                    # band, so it's safe (and more forgiving of an
                    # off-by-a-bit boundary) to read its full width rather
                    # than cropping by x-position at all.
                    same_row = top_assess <= top_school + 3
                    school_bottom = (top_footer - 2) if same_row else (top_assess - 2)
                    school_x_max = (x0_assess + x0_school) / 2 if same_row else page_width
                    school_col = _column_text(words, 0, school_x_max, top_school - 2, school_bottom)
                    assess_col = _column_text(words, 0, boundary_assess_tax, top_assess - 2, top_footer - 2)
                    tax_col = _column_text(words, boundary_assess_tax, boundary_tax_pet, top_assess - 2, top_footer - 2)
                    pet_col = _column_text(words, boundary_tax_pet, page_width, top_assess - 2, top_footer - 2)

        listing.assessment_amount = money("$" + _first_num(_grab(assess_col, "Amount", ["\n"])))
        listing.assessment_frequency = _grab(assess_col, "Frequency", ["\n"])
        special_assessments = _grab(assess_col, "Special Assessments", ["\n"])
        listing.special_assessments = "" if _is_nullish(special_assessments) else special_assessments

        listing.tax_amount = money("$" + _first_num(_grab(tax_col, "Amount", ["\n"])))
        listing.tax_year = _grab(tax_col, "Tax Year", ["\n"])

        # "Mult PINs" and "Tax Exmps" values can wrap onto a second line
        # within this column (e.g. "Mult PINs: (See Agent\nRemarks)"), so
        # flatten newlines to spaces first rather than grabbing from the raw
        # column text, which would truncate at the wrap.
        tax_col_flat = re.sub(r"\s*\n\s*", " ", tax_col)
        listing.mult_pins = _grab(tax_col_flat, "Mult PINs", ["Tax Year", "$"])
        tax_exemptions = _grab(tax_col_flat, "Tax Exmps", ["Coop Tax Deduction", "$"])
        listing.tax_exemptions = "" if _is_nullish(tax_exemptions) else tax_exemptions

        listing.elementary = _grab(school_col, "Elementary", ["\n"])
        listing.junior_high = _grab(school_col, "Junior High", ["\n"])
        listing.high_school = _grab(school_col, "High School", ["\n"])

        # Stop at "Pet Weight:" (the field label, with its colon) as well as
        # "Max Pet Weight" -- on some sheets the word "Max" lands a hair's-
        # width inside the neighboring tax column (its x-position is right
        # at the column boundary), leaving an orphaned "Pet Weight:000"
        # fragment in this column that "Max Pet Weight" alone wouldn't catch
        # as a stop point. The colon is required so this doesn't also match
        # the legit pet-policy phrase "Pet Weight Limitation" (no colon)
        # that can appear earlier in this same value.
        listing.pets_allowed = _grab(re.sub(r"\s*\n\s*", " ", pet_col), "Pets Allowed", ["Max Pet Weight", "Pet Weight:", "$"])

        if not header_hit:
            # Compact/private-network exports have no "School Data"
            # section at all -- there's no bottom School/Assessments/Tax/
            # Pet grid to key off here, but Elementary/Junior High/High
            # School (when the sheet actually has school names, not just
            # a bare district code) are still simple standalone full-
            # width lines, worth grabbing directly. A value that's just a
            # bare district code with no actual school name (e.g. "(299)",
            # seen on the one real private-listing sample this was built
            # against) isn't useful to show on its own, so it's filtered
            # out the same as a genuinely missing value.
            def _school_name(label):
                val = _grab(full_text, label, ["\n"])
                return "" if re.fullmatch(r"\(\d+\)", val) else val
            listing.elementary = _school_name("Elementary")
            listing.junior_high = _school_name("Junior High")
            listing.high_school = _school_name("High School")

            # Pet Information lives in this format's OWN left/right
            # 2-column header grid (Bedrooms/Bathrooms/Actv. Date/Type/
            # Pets Allowed/Pet Information on the left; Area/List Price/
            # .../Basement/Master Bedroom Bath/Approx Sq Ft on the right)
            # rather than the bottom grid this whole function otherwise
            # keys off. Its value can wrap onto a second row, so this is
            # reconstructed by word x-position (the same way every other
            # wrapped-value column in this file is) rather than a linear-
            # text regex, which would splice in whatever sits in the
            # right-hand column on the wrapped row -- seen on the one
            # real sample this was built against: "Master Bedroom Bath:
            # Full" landing mid-sentence inside the pet value ("...Dogs
            # OK, Pet Master Bedroom Bath: Full Count Limitation...").
            # The x=390 column split and "Elementary:" bottom anchor are
            # both empirically read off that one real sample -- revisit
            # if a future private-listing sample splits differently.
            info_hit = next(
                (
                    (pi, ws, w["top"])
                    for pi, ws in enumerate(pages_words)
                    for w in ws
                    if w["text"] == "Information:" and w["x0"] < 300
                ),
                None,
            )
            if info_hit:
                pi, words, info_top = info_hit
                elem_top = next(
                    (w["top"] for w in words if w["text"].startswith("Elementary:") and w["top"] > info_top),
                    None,
                )
                bottom = (elem_top - 2) if elem_top else (info_top + 40)
                pet_block = _column_text(words, 0, 390, info_top - 2, bottom)
                pet_val = _grab(re.sub(r"\s*\n\s*", " ", pet_block), "Pet Information", ["\n"])
                if pet_val and not _is_nullish(pet_val):
                    listing.pets_allowed = pet_val

    m = re.search(r"Max Pet Weight:(\d+)", page1_text)
    if m:
        # MRED zero-fills this field ("000") when no specific limit was
        # entered -- that's a null placeholder, not an actual 0 lb limit.
        weight = int(m.group(1))
        if weight > 0:
            listing.max_pet_weight = str(weight)

    listing.assessment_includes = _grab(full_text, "Asmt Incl", ["HERS Index Score", "\n"])

    # --- Remarks (long free text) -------------------------------------------------
    # "Exterior Property Features"/"Copyright" added as terminators alongside
    # the standard layout's "School Data"/"Broker Private Remarks" --
    # compact/private-network exports have neither of those two sections at
    # all (no School Data header, no broker remarks), so without a
    # terminator that actually exists in that layout the whole regex fails
    # to match and remarks comes back completely blank, even though the
    # sheet has a full remarks paragraph sitting right there.
    m = re.search(
        r"Remarks:\s*(.*?)\s*(?:School Data|Broker Private Remarks|Exterior Property Features|Copyright)",
        full_text, re.S,
    )
    if m:
        remarks = m.group(1).strip()
        remarks = re.sub(r"\s*\n\s*", " ", remarks)
        remarks = re.sub(r"\s{2,}", " ", remarks)
        listing.remarks = remarks

    # --- Features: 3-column grid (Age/Type/... | Laundry/Garage/... | Sewer/...) --
    feat_col1 = feat_col2 = feat_col3 = ""
    rooms_left = rooms_right = ""
    try:
        grid_hit = _find("Age:")
        if grid_hit:
            gi, words, grid_top, _ = grid_hit
            # Bottom boundary: prefer "Broker Private Remarks:" (present on
            # the fuller "Agent" report exports); fall back to the standard
            # MRED copyright disclaimer line, which every export flavor
            # seems to carry, for the shorter "Customer"-style exports that
            # omit broker remarks entirely. A content-heavy listing (long
            # remarks, big room table) can push this closing anchor onto
            # the page *after* the grid itself, so the search continues
            # forward across pages rather than assuming it's always on the
            # grid's own page -- a same-page-only search would come back
            # empty and silently blank every field in this grid.
            bottom_hit = _find_after(gi, grid_top, "Broker", margin_cutoff) or _find_after(gi, grid_top, "Copyright", margin_cutoff)
            if bottom_hit is not None:
                bi, bottom_top = bottom_hit
                top, bottom = grid_top - 2, bottom_top - 2
                # Flatten to single-line-per-column text: every field we pull
                # out of this grid is a short label:value pair, and MRED
                # wraps long values (e.g. "Garage Door Opener(s), Heated,
                # Tandem") onto a second line within the same cell, so
                # newlines here are just wrapping, not meaningful row breaks.
                feat_col1 = _degarble(re.sub(r"\s*\n\s*", " ", _text_spanning_pages(gi, top, bi, bottom, 0, 195)))
                feat_col2 = _degarble(re.sub(r"\s*\n\s*", " ", _text_spanning_pages(gi, top, bi, bottom, 195, 395)))
                feat_col3 = _degarble(re.sub(r"\s*\n\s*", " ", _text_spanning_pages(gi, top, bi, bottom, 395, page_width)))

        room_hdr_hit = _find("Room", margin_cutoff)
        if room_hdr_hit:
            ri, words, room_hdr_top, _ = room_hdr_hit
            interior_hit = _find_after(ri, room_hdr_top, "Interior")
            if interior_hit is not None:
                ii, interior_top = interior_hit
                top, bottom = room_hdr_top - 2, interior_top - 2
                rooms_left = _text_spanning_pages(ri, top, ii, bottom, 0, 306)
                rooms_right = _text_spanning_pages(ri, top, ii, bottom, 306, page_width)
    except Exception:
        pass

    if not feat_col1 and not feat_col2 and not feat_col3:
        # Compact/private-network exports have no "Age:" grid at all, so
        # every FEAT_LABELS-based grab below (kitchen, heating, bath
        # amenities, amenities, laundry, garage details) comes back empty
        # -- correctly, since none of that data exists on this leaner
        # sheet. But the one thing this format DOES carry is a short
        # "Exterior Property Features:" mini-block of plain Garage/Parking
        # label:value lines (not organized into the usual x-position
        # grid), e.g. "Garage Ownership: Fee/Leased Parking Ownership:" /
        # "Garage On Site: Yes Parking On Site:" / "Garage Type: Attached
        # Parking Space:" / "Garage Space: 1" -- each Garage-side label
        # paired on the same physical line with a Parking-side label
        # (usually empty), so the Parking label doubles as this line's own
        # stop boundary.
        compact_stops = [
            "Garage On Site:", "Garage Type:", "Garage Space:",
            "Parking Ownership:", "Parking On Site:", "Parking Space:",
            "Copyright",
        ]
        g_ownership = _grab(full_text, "Garage Ownership", compact_stops)
        if g_ownership and not _is_nullish(g_ownership):
            listing.garage_ownership = g_ownership
        g_type = _grab(full_text, "Garage Type", compact_stops)
        if g_type and not _is_nullish(g_type):
            listing.garage_type = g_type
        g_onsite = _grab(full_text, "Garage On Site", compact_stops)
        g_space = _grab(full_text, "Garage Space", compact_stops)
        if g_space and not listing.parking_spaces:
            listing.parking_spaces = g_space
        if (g_onsite or "").strip().lower() == "yes" and not listing.parking_type:
            listing.parking_type = "Garage"

    # The feature grid's exact set/order of fields varies by property type
    # (a detached home adds Attic/Basement Details/Additional Rooms/Gas
    # Supplier/etc that a condo sheet doesn't carry), and some source PDFs
    # have individual field labels with corrupted character spacing (a
    # defect in that PDF's own text layer -- e.g. "A p p li a n c e s :" --
    # not something fixable on our end) that breaks an exact stop-label
    # match. Rather than hardcode one stop label per field, which silently
    # swallows everything up to the next label that DOES happen to match,
    # every grab below is also bounded by this shared list of every label
    # that can plausibly appear in this grid, so a grab always stops at the
    # next real field even when its own "preferred" neighbor isn't reachable.
    FEAT_LABELS = [
        "Age:", "Type:", "Exposure:", "Style:", "Exterior:",
        "Heating:", "Air Cond:", "Kitchen:", "Appliances:", "Dining:", "Attic:",
        "Basement Details:", "Bath Amn:", "Fireplace Details:", "Fireplace Location:",
        "Electricity:", "Equipment:", "Additional Rooms:", "Other Structures:",
        "Door Features:", "Window Features:", "Gas Supplier:", "Electric Supplier:",
        "Laundry Features:", "Garage Ownership:", "Garage On Site:", "Garage Type:",
        "Garage Details:", "Parking Ownership:", "Parking On Site:", "Parking Details:",
        "Parking Fee", "Driveway:", "Foundation:", "Exst Bas/Fnd:", "Disability Access:",
        "Disability Details:", "Lot Size:", "Lot Size Source:", "Lot Desc:",
        "Zero Lot Line:", "Relist:", "Roof:", "Sewer:", "Water:", "Const Opts:",
        "General Info:", "Amenities:", "Asmt Incl:", "HERS Index Score:",
        "Green Disc", "Green Rating Source:", "Green Feats:", "Sale Terms:",
        "Possession:", "Occ Date:", "Est Occp Date:", "Management:", "Rural:",
        "Vacant:", "Addl. Sales Info.:", "Broker Owned/Interest:",
    ]

    def _grab_feat(text, label, primary_stops=()):
        return _grab(text, label, list(primary_stops) + FEAT_LABELS)

    listing.interior_features = _grab(full_text, "Interior Property Features", ["Exterior Property Features"])
    # "Garage Ownership:" added as a stop -- compact/private-network
    # exports label a short Garage/Parking-only mini-block "Exterior
    # Property Features:" with no actual exterior bullet content before
    # it, and _grab()'s "\s*" after the colon eats the newline right up
    # to that mini-block's first line, so without this stop the grab
    # swallows "Garage Ownership: Fee/Leased Parking Ownership:" as if it
    # were the exterior-features value.
    listing.exterior_features = _grab(full_text, "Exterior Property Features", ["Age:", "Garage Ownership:"])

    # Exposure (unit-facing direction, e.g. "N (North), W (West)") --
    # condo-relevant, affects natural light expectations. Not significant
    # enough to warrant its own card, so folded into Interior Features
    # (mirrors parser_michric.py's pattern of folding Security Features in
    # rather than adding a one-off card for every small extra fact).
    exposure = _grab_feat(feat_col1, "Exposure")
    if exposure and not _is_nullish(exposure):
        listing.exposure = exposure
        listing.interior_features = (
            f"{listing.interior_features}; Exposure: {exposure}" if listing.interior_features else f"Exposure: {exposure}"
        )

    listing.heating = _grab_feat(feat_col1, "Heating")
    listing.cooling = _grab_feat(feat_col1, "Air Cond")
    listing.kitchen_features = _grab_feat(feat_col1, "Kitchen")
    listing.appliances = _grab_feat(feat_col1, "Appliances")
    listing.bath_amenities = _grab_feat(feat_col1, "Bath Amn")
    listing.amenities = _grab_feat(feat_col3, "Amenities")
    listing.laundry = _grab_feat(feat_col2, "Laundry Features")
    listing.age = _grab_feat(feat_col1, "Age") or listing.age
    m = re.search(r"\bParking:(Garage|None|Space/s|Assigned Spaces|Off Street|Driveway|N/A)", page1_text)
    # Falls back to whatever parking_type may already be set (rather than
    # blanking it) when this header pattern doesn't match -- compact/
    # private-network exports don't carry a "Parking:Garage"-style header
    # token at all, so this regex never matches for them, but the
    # compact-format block above (see feat_col1/2/3-empty branch) already
    # derived "Garage" from "Garage On Site: Yes" for that format; this
    # used to unconditionally overwrite that with "", silently dropping
    # the whole Parking & Garage card since feature_groups() gates it on
    # parking_type being non-empty.
    listing.parking_type = m.group(1) if m else (listing.parking_type or "")
    listing.garage_details = _grab_feat(feat_col2, "Garage Details")
    # "Garage Ownership:" was already a recognized FEAT_LABELS stop-word
    # (protecting neighboring grabs' boundaries) but was never itself
    # grabbed into a field -- the same "recognized but dropped" bug pattern
    # found repeatedly in the MichRIC parser. This is the one case where it
    # actually matters: a value like "Deeded Sold Separately ($25,000)" is a
    # real, buyer-relevant cost that parking_incl_in_price alone doesn't
    # convey. See render.py's parking_note().
    garage_ownership = _grab_feat(feat_col2, "Garage Ownership")
    if garage_ownership and not _is_nullish(garage_ownership):
        listing.garage_ownership = garage_ownership

    # "Garage Type:" (Attached/Detached) -- same "recognized as a
    # FEAT_LABELS stop but never actually grabbed" gap as Garage
    # Ownership above. Lives in the same feature-grid column as Garage
    # Details/Garage Ownership, right next to them on the sheet.
    garage_type = _grab_feat(feat_col2, "Garage Type")
    if garage_type and not _is_nullish(garage_type):
        listing.garage_type = garage_type

    # Number of stories in the home itself (distinct from a condo building's
    # "# Stories:", which is about the building, not the unit) -- shown as
    # its own quick fact for detached/townhome listings. Bounded by the full
    # FEAT_LABELS list (not just "Style:") so it can't run on and swallow
    # "Exposure:" and everything after it when a listing has no Style field
    # at all -- it's not displayed directly (only stories is pulled from
    # it), but a follow-on regex search against an over-long string is
    # needless fragility worth closing off while touching this code.
    type_val = _grab_feat(feat_col1, "Type", ["Style:"])
    m = re.search(r"(\d+)\+?\s*Stor(?:y|ies)", type_val)
    if m:
        listing.stories = m.group(1)

    # Fireplace type/location -- the facts strip already shows a bare
    # count (# Fireplaces), this is what kind and/or which room, when the
    # sheet says more than that.
    fp_details = _grab_feat(feat_col1, "Fireplace Details")
    fp_location = _grab_feat(feat_col1, "Fireplace Location")
    fp_parts = [p for p in (fp_details, fp_location) if p and not _is_nullish(p)]
    if fp_parts:
        listing.fireplace_details = ", ".join(fp_parts)

    # Roof, Lot Description, and Other Structures fold into Exterior with
    # a "Label: value" prefix -- same convention parser_michric.py already
    # uses for Roofing/Lot Description on MI sheets, so both apps' Exterior
    # card reads the same way regardless of source MLS.
    roof = _grab_feat(feat_col2, "Roof")
    if roof and not _is_nullish(roof):
        listing.exterior_features = (
            f"{listing.exterior_features}; Roof: {roof}" if listing.exterior_features else f"Roof: {roof}"
        )
    lot_desc = _grab_feat(feat_col2, "Lot Desc")
    if lot_desc and not _is_nullish(lot_desc):
        listing.exterior_features = (
            f"{listing.exterior_features}; Lot: {lot_desc}" if listing.exterior_features else f"Lot: {lot_desc}"
        )
    other_structures = _grab_feat(feat_col1, "Other Structures")
    if other_structures and not _is_nullish(other_structures):
        listing.exterior_features = (
            f"{listing.exterior_features}; Other Structures: {other_structures}" if listing.exterior_features else f"Other Structures: {other_structures}"
        )

    # Equipment (extras included in the sale, e.g. "Intercom, Ceiling Fan")
    # and Additional Rooms fold into Interior the same way. "No additional
    # rooms" is this field's own literal null-value phrasing (not in the
    # general _NULLISH set, which only covers single words like "None"),
    # so it needs its own check to avoid printing that non-answer verbatim.
    equipment = _grab_feat(feat_col1, "Equipment")
    if equipment and not _is_nullish(equipment):
        listing.interior_features = (
            f"{listing.interior_features}; Extras: {equipment}" if listing.interior_features else f"Extras: {equipment}"
        )
    additional_rooms = _grab_feat(feat_col1, "Additional Rooms")
    if (
        additional_rooms
        and not _is_nullish(additional_rooms)
        and additional_rooms.strip().lower() != "no additional rooms"
    ):
        listing.interior_features = (
            f"{listing.interior_features}; Additional Rooms: {additional_rooms}" if listing.interior_features else f"Additional Rooms: {additional_rooms}"
        )

    # Possession terms (e.g. "Closing", "Negotiable", a specific date) --
    # lives in the same right-hand column as Sale Terms/Amenities/Asmt Incl.
    possession = _grab_feat(feat_col3, "Possession")
    if possession and not _is_nullish(possession):
        listing.possession = possession

    # % Owner-Occupied -- header-block field (not part of the feature
    # grid), condo-specific, affects FHA/some conventional loan eligibility.
    pct_own_occ = _grab(page1_text, "% Own. Occ.", ["% Cmn. Own.", "\n"])
    if pct_own_occ and not _is_nullish(pct_own_occ):
        listing.pct_owner_occupied = pct_own_occ

    # --- Room dimension tables (two side-by-side mini tables) ---------------------
    # MRED's room-name picklist is large, especially once a listing has a
    # finished basement or extra levels (dens, decks, storage, etc.) -- this
    # whitelist needs to be generous, since any room name not on it is
    # silently dropped from the table rather than just displayed oddly.
    ROOM_NAMES = (
        r"Living Room|Dining Room|Kitchen|Family Room|Great Room|"
        r"Master Bedroom|2nd Bedroom|3rd Bedroom|4th Bedroom|5th Bedroom|6th Bedroom|"
        r"Laundry Room|Laundry|Mud Room|Walk In Closet|Walk-In Closet|"
        r"Deck|Terrace|Balcony|Porch|Enclosed Porch|Screened Porch|Patio|"
        r"Storage|Recreation Room|Rec Room|Bonus Room|Loft|Office|Den|Study|"
        r"Library|Sun Room|Sunroom|Heated Sun Room|Foyer|Gallery|Atrium|"
        r"Exercise Room|Media Room|Game Room|Theatre Room|Sitting Room|"
        r"Breakfast Room|Eating Area|Dinette|Utility Room|Workshop|"
        r"Bar/Entertainment|Pantry|Play Room|Wine Cellar|Craft Room|Tandem Room"
    )

    def parse_room_column(col_text):
        rooms = []
        for line in col_text.splitlines():
            line = line.strip()
            m = re.match(
                r"(" + ROOM_NAMES + r")"
                r"\s*([\dX]+|COMBO)?\s*(Main Level|2nd Level|3rd Level|Lower Level|Basement)?\s*(Hardwood|Carpet|Ceramic Tile|Vinyl|Marble|Wood Laminate|Luxury Vinyl|Other)?",
                line,
            )
            if not m:
                continue
            name, size, level, flooring = m.groups()
            if not size and not level and not flooring:
                continue
            rooms.append({"name": name, "size": size or "", "level": level or "", "flooring": flooring or ""})
        return rooms

    listing.rooms = parse_room_column(rooms_left) + parse_room_column(rooms_right)

    # --- Listing broker (MLS compliance credit) -----------------------------------
    m = re.search(r"List Broker:\s*(.*?)\s*\(\d+\)\s*(?:on behalf of\s*(.*?)\s*\(T?\d+\))?\s*/\s*(.*?)\s*/", full_text)
    if m:
        listing.list_broker_name = m.group(1).strip()
        listing.list_brokerage = (m.group(2) or "").strip()
        listing.list_broker_phone = m.group(3).strip()

    # --- Photo -------------------------------------------------------------------
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[0]
        images = page.get_images(full=True)
        if images:
            xref = images[0][0]
            base = doc.extract_image(xref)
            listing.photo_bytes = base["image"]
            listing.photo_ext = base.get("ext", "jpg")
        doc.close()
    except Exception:
        pass

    return listing
