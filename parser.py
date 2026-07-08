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
    if not s.startswith("$"):
        return s
    return s


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    mls_number: str = ""
    property_type: str = ""
    status: str = ""
    list_date: str = ""
    dom_list_side: str = ""
    dom_total: str = ""
    list_price: str = ""
    address_line1: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    directions: str = ""

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
    parking_incl_in_price: str = ""
    lot_size: str = ""

    total_units: str = ""
    total_stories: str = ""
    unit_floor_level: str = ""

    basement: str = ""
    basement_bath: str = ""
    fireplaces: str = ""
    stories: str = ""

    assessment_amount: str = ""
    assessment_frequency: str = ""
    assessment_includes: str = ""
    special_assessments: str = ""

    tax_amount: str = ""
    tax_year: str = ""
    tax_exemptions: str = ""
    mult_pins: str = ""

    elementary: str = ""
    junior_high: str = ""
    high_school: str = ""

    pets_allowed: str = ""
    max_pet_weight: str = ""

    remarks: str = ""

    interior_features: str = ""
    exterior_features: str = ""
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
    listing.ownership = _grab(page1_text, "Ownership", ["Subdivision"])
    listing.rooms_total = _grab(page1_text, "Rooms", ["Bathrooms"])
    listing.bedrooms = _grab(page1_text, "Bedrooms", ["Master Bath"])

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

    m = re.search(r"Bathrooms(?:\s*\(Full/Half\))?:?\s*(\d+)\s*/\s*(\d+)", page1_text)
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

    m = re.search(r"Appx SF:\s*([\d,]+)", page1_text)
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
    page_width = 612
    left_margin = 14
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages_words = [p.extract_words() for p in pdf.pages]
            if pdf.pages:
                page_width = pdf.pages[0].width
    except Exception:
        pass
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
        listing.special_assessments = _grab(full_text, "Special Assessments", ["Tax Year", "\n"])
        listing.tax_year = _grab(full_text, "Tax Year", ["Flood Zone", "\n"])
        listing.tax_exemptions = _grab(full_text, "Tax Exmps", ["Appx SF", "Main +", "\n"])
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
        listing.special_assessments = _grab(assess_col, "Special Assessments", ["\n"])

        listing.tax_amount = money("$" + _first_num(_grab(tax_col, "Amount", ["\n"])))
        listing.tax_year = _grab(tax_col, "Tax Year", ["\n"])

        # "Mult PINs" and "Tax Exmps" values can wrap onto a second line
        # within this column (e.g. "Mult PINs: (See Agent\nRemarks)"), so
        # flatten newlines to spaces first rather than grabbing from the raw
        # column text, which would truncate at the wrap.
        tax_col_flat = re.sub(r"\s*\n\s*", " ", tax_col)
        listing.mult_pins = _grab(tax_col_flat, "Mult PINs", ["Tax Year", "$"])
        listing.tax_exemptions = _grab(tax_col_flat, "Tax Exmps", ["Coop Tax Deduction", "$"])

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

    m = re.search(r"Max Pet Weight:(\d+)", page1_text)
    if m:
        # MRED zero-fills this field ("000") when no specific limit was
        # entered -- that's a null placeholder, not an actual 0 lb limit.
        weight = int(m.group(1))
        if weight > 0:
            listing.max_pet_weight = str(weight)

    listing.assessment_includes = _grab(full_text, "Asmt Incl", ["HERS Index Score", "\n"])

    # --- Remarks (long free text) -------------------------------------------------
    m = re.search(r"Remarks:\s*(.*?)\s*(?:School Data|Broker Private Remarks)", full_text, re.S)
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
            # omit broker remarks entirely. Both are looked up on the same
            # page as the grid itself, not just the first page they appear
            # on anywhere in the document (the copyright line repeats on
            # every page).
            bottom_top = _find_on(words, "Broker", margin_cutoff) or _find_on(words, "Copyright", margin_cutoff)
            if bottom_top is not None:
                top, bottom = grid_top - 2, bottom_top - 2
                # Flatten to single-line-per-column text: every field we pull
                # out of this grid is a short label:value pair, and MRED
                # wraps long values (e.g. "Garage Door Opener(s), Heated,
                # Tandem") onto a second line within the same cell, so
                # newlines here are just wrapping, not meaningful row breaks.
                feat_col1 = _degarble(re.sub(r"\s*\n\s*", " ", _column_text(words, 0, 195, top, bottom)))
                feat_col2 = _degarble(re.sub(r"\s*\n\s*", " ", _column_text(words, 195, 395, top, bottom)))
                feat_col3 = _degarble(re.sub(r"\s*\n\s*", " ", _column_text(words, 395, page_width, top, bottom)))

        room_hdr_hit = _find("Room", margin_cutoff)
        if room_hdr_hit:
            ri, words, room_hdr_top, _ = room_hdr_hit
            interior_top = _find_on(words, "Interior", top_min=room_hdr_top)
            if interior_top is not None:
                top, bottom = room_hdr_top - 2, interior_top - 2
                rooms_left = _column_text(words, 0, 306, top, bottom)
                rooms_right = _column_text(words, 306, page_width, top, bottom)
    except Exception:
        pass

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
        "Age:", "Type:", "Style:", "Exterior:",
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
    listing.exterior_features = _grab(full_text, "Exterior Property Features", ["Age:"])
    listing.heating = _grab_feat(feat_col1, "Heating")
    listing.cooling = _grab_feat(feat_col1, "Air Cond")
    listing.kitchen_features = _grab_feat(feat_col1, "Kitchen")
    listing.appliances = _grab_feat(feat_col1, "Appliances")
    listing.bath_amenities = _grab_feat(feat_col1, "Bath Amn")
    listing.amenities = _grab_feat(feat_col3, "Amenities")
    listing.laundry = _grab_feat(feat_col2, "Laundry Features")
    listing.age = _grab_feat(feat_col1, "Age") or listing.age
    m = re.search(r"\bParking:(Garage|None|Space/s|Assigned Spaces|Off Street|Driveway|N/A)", page1_text)
    listing.parking_type = m.group(1) if m else ""
    listing.garage_details = _grab_feat(feat_col2, "Garage Details")

    # Number of stories in the home itself (distinct from a condo building's
    # "# Stories:", which is about the building, not the unit) -- shown as
    # its own quick fact for detached/townhome listings.
    type_val = _grab(feat_col1, "Type", ["Style:"])
    m = re.search(r"(\d+)\+?\s*Stor(?:y|ies)", type_val)
    if m:
        listing.stories = m.group(1)

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
