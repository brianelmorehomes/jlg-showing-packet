"""
New Home Platform listing sheet parser ("single_listing_print" export).
--------------------------------------------------------------------------
Brian's brokerage is transitioning to a new listing platform (its
disclaimer footer names "Compass International Holdings" -- see
is_new_platform() below) whose print sheet is a completely different
physical document from both MRED's classic "Full Report" (parser.py) and
MichRIC's "New Full Detail Report" (parser_michric.py). It's also MLS-
agnostic: the same sheet layout is produced whether the underlying
listing lives in MRED (Illinois) or MichRIC (Michigan) -- confirmed
directly against real samples of both -- so this one module handles
both, rather than needing its own MRED/MichRIC split the way the classic
sheets do.

Two real differences from the classic sheets that matter for callers:

1. Each PDF export comes in a "Client" flavor and/or an "Agent" flavor
   (sometimes both concatenated into one multi-page file, sometimes just
   one). The Agent flavor adds a few extra Key Details rows (PRKG, FEES,
   bare Beds/Baths) and an internal-only Listing Contacts/Agent Remarks/
   Showing Instructions block -- none of which this flyer needs (the
   internal fields are already stripped from every other source's flyer
   too), and the extra Key Details rows just duplicate data the Client
   flavor already has elsewhere on the sheet (Property History/Details,
   the header banner). So Client-only input works fine and is preferred
   when present; Agent-only input parses just as well since the grid
   extraction below is driven by font weight, not a fixed field list --
   an unrecognized bold label is just a dict key nothing ever looks up.

2. This sheet has meaningfully LESS data than a MichRIC full-detail
   report: no room dimensions table, no categorized interior/exterior/
   construction feature grid, no water source/sewer, no heating type
   breakdown, no basement finish detail, no fireplace detail, and no
   County field at all (MRED/MichRIC's classic sheets both carry County
   directly). Flyers built from this source will legitimately have more
   blank/placeholder cards than one built from a classic MRED or MichRIC
   sheet -- that's an honest reflection of what this source actually
   contains, not a parsing bug. The missing County field specifically
   means jlg-showing-packet's route-map geocoding (packet.py's
   _county_level() rural-address fallback) has nothing to fall back on
   for listings parsed from this source -- worth knowing if a showing
   packet stop sourced this way ever lands a mislocated pin the way
   6456 104th Avenue did before that fix.

Detection and extraction approach
----------------------------------
Every Key Details / Property History / Property Details row on this
sheet renders as BOLD label word(s) immediately followed by REGULAR
value word(s) -- confirmed directly via pdfplumber's per-word font name
(bold rows use an "...-Bold" font, values use a "...-Regular" font) --
with two side-by-side (Key Details/Property Details) or four side-by-
side (Property History's first row) label:value pairs sharing one
visual text row. There's no colon delimiter, and the exact set of
fields present varies a lot by property/MLS (an MRED listing's Key
Details has Township/Ownership/Heat-Fuel rows a MichRIC listing's
doesn't, and vice versa for Architectural Style/Waterfront/Zoning), so
rather than hand-maintain an exhaustive label list, `_extract_kv_grid()`
below reads the bold/regular run pattern directly off each row's words
and builds a {label: value} dict from whatever's actually there. Callers
just look up the handful of labels they care about by name; anything
else present on the sheet (Ownership, Zoning, Subdivision Name, ...)
is harmlessly left in the dict, unused, same "generic label:value
scraping so it degrades gracefully" philosophy as parser.py.
"""
import io
import re

import fitz  # PyMuPDF
import pdfplumber

from parser import Listing, money, _is_nullish


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def is_new_platform(text: str) -> bool:
    """Signature check mirroring mls_router.py's is_michric()/MRED default
    pattern -- this platform's own compliance footer names its parent
    company, present on every real export seen from it."""
    return "Compass International Holdings" in (text or "")


# ---------------------------------------------------------------------------
# Font-weight-driven grid extraction
# ---------------------------------------------------------------------------

def _extract_kv_grid(words, top_min, top_max, row_tol=3):
    """Extract {label: value} pairs from a bold-label/regular-value grid
    section (Key Details, Property History, Property Details on this
    platform's sheet) bounded vertically by [top_min, top_max). Handles
    any number of side-by-side columns per row -- driven purely by each
    word's font weight and left-to-right order, not a fixed set of x
    positions, since Property History uses 4 columns on its first row
    and 2 on its second while Key Details/Property Details use 2
    throughout. See module docstring for why this beats a hardcoded
    label list."""
    picked = [w for w in words if top_min <= w["top"] < top_max]
    picked.sort(key=lambda w: (w["top"], w["x0"]))

    rows = []
    cur_top, cur = None, []
    for w in picked:
        if cur_top is None or abs(w["top"] - cur_top) <= row_tol:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else cur_top
        else:
            rows.append(cur)
            cur, cur_top = [w], w["top"]
    if cur:
        rows.append(cur)

    kv = {}
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        pairs = []
        label_words, value_words = [], []
        mode = None
        for w in row:
            is_bold = "Bold" in (w.get("fontname") or "")
            if is_bold:
                if mode == "value" and (label_words or value_words):
                    pairs.append((label_words, value_words))
                    label_words, value_words = [], []
                mode = "label"
                label_words.append(w["text"])
            else:
                mode = "value"
                value_words.append(w["text"])
        if label_words or value_words:
            pairs.append((label_words, value_words))
        for lw, vw in pairs:
            label = " ".join(lw).strip()
            value = " ".join(vw).strip()
            if label:
                kv[label] = value
    return kv


def _section_headers(words):
    """Return [(header_text, top), ...] for this page's bold, >=7.5pt
    section/subsection headings (Key Details, Description, Property
    History, Property Details, Amenities, Schools, Transit, My Agent,
    Listing Contacts, ...), in top-to-bottom order. Body/value text on
    this sheet all sits at 7.0pt, so the size threshold alone reliably
    separates headings from content without needing to match specific
    heading strings."""
    hdr_words = [
        w for w in words
        if w.get("size", 0) >= 7.5 and "Bold" in (w.get("fontname") or "")
    ]
    hdr_words.sort(key=lambda w: (w["top"], w["x0"]))
    rows = []
    cur_top, cur = None, []
    for w in hdr_words:
        if cur_top is None or abs(w["top"] - cur_top) <= 3:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else cur_top
        else:
            rows.append(cur)
            cur, cur_top = [w], w["top"]
    if cur:
        rows.append(cur)
    out = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        out.append((" ".join(w["text"] for w in row), row[0]["top"]))
    return out


def _section_bounds(headers, name, page_bottom):
    """Find `name` in the (text, top) header list and return (top, next_top)
    -- the vertical band belonging to that section, up to whichever
    header comes next (any heading, not just ones this parser knows
    about) or the bottom of the page."""
    for i, (text, top) in enumerate(headers):
        if text == name:
            nxt = headers[i + 1][1] if i + 1 < len(headers) else page_bottom
            return top, nxt
    return None


def _grid_section(words, headers, name, page_bottom):
    bounds = _section_bounds(headers, name, page_bottom)
    if not bounds:
        return {}
    top, nxt = bounds
    # Skip the header's own row itself (its words are also >=7.5pt bold,
    # which would otherwise be picked up as a spurious label/value pair).
    body_words = [w for w in words if w["top"] > top + 2]
    return _extract_kv_grid(body_words, top, nxt)


def _section_rows(words, headers, name, page_bottom):
    """One reading-order line of text per visual row within a section --
    the building block both _text_section (Description/Amenities, which
    want one joined paragraph) and _parse_schools (which needs each
    school kept on its own line) are built from."""
    bounds = _section_bounds(headers, name, page_bottom)
    if not bounds:
        return []
    top, nxt = bounds
    body = [w for w in words if top + 2 < w["top"] < nxt]
    body.sort(key=lambda w: (w["top"], w["x0"]))
    rows = []
    cur_top, cur = None, []
    for w in body:
        if cur_top is None or abs(w["top"] - cur_top) <= 3:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else cur_top
        else:
            rows.append(cur)
            cur, cur_top = [w], w["top"]
    if cur:
        rows.append(cur)
    return [" ".join(w["text"] for w in sorted(r, key=lambda w: w["x0"])) for r in rows]


def _text_section(words, headers, name, page_bottom):
    """Plain prose/list body text of a section (Description, Amenities),
    reconstructed in reading order rather than as a label:value grid."""
    return " ".join(_section_rows(words, headers, name, page_bottom)).strip()


# ---------------------------------------------------------------------------
# Header banner ("Client - 123 Main St Chicago IL 60640 Active $500,000
# 3 BD * 2 BA * 1 1/2 BA * 1,500 SF * $333/SF Page 1/2")
# ---------------------------------------------------------------------------

_BANNER_RE = re.compile(
    r"(?:Client|Agent)\s*-\s*(?P<addrcity>.+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})\s+"
    r"(?P<status>Active\s*\(\s*Private\s*\)|Active|Pending|Contingent|Sold|Closed|"
    r"Coming Soon|Withdrawn|Expired|New)\s+"
    r"\$(?P<price>[\d,]+)\s+"
    r"(?P<beds>\d+)\s*BD\s*[•*]\s*(?P<bfull>\d+)\s*BA"
    r"(?:\s*[•*]\s*(?P<bhalf>\d+)\s*1/2\s*BA)?\s*[•*]\s*"
    r"(?P<sqft>[\d,]+)\s*SF"
)

# Common street-type suffixes, used to split the banner's glued-together
# "<street address><city>" text (there's no delimiter between them --
# confirmed on every real sample: "1619 West Summerdale Avenue Chicago",
# "3126 Red Oak Drive Saugatuck"). Not exhaustive -- a street name with no
# recognized suffix (e.g. a bare "Broadway") falls back to treating just
# the last word as the city, which is a reasonable but imperfect guess.
_STREET_SUFFIXES = {
    "avenue", "ave", "street", "st", "drive", "dr", "road", "rd", "lane", "ln",
    "boulevard", "blvd", "way", "court", "ct", "place", "pl", "circle", "cir",
    "terrace", "ter", "parkway", "pkwy", "trail", "trl", "highway", "hwy",
    "square", "sq", "loop", "path", "row", "crossing", "xing", "pass", "walk",
    "point", "pt", "crescent", "cres", "close", "commons",
}
_UNIT_WORDS = {"unit", "apt", "apartment", "ste", "suite", "#"}


def _split_street_city(blob):
    words = (blob or "").split()
    if not words:
        return "", ""
    idx = None
    for i, w in enumerate(words):
        if w.strip(".,").lower() in _STREET_SUFFIXES:
            idx = i
    if idx is None:
        if len(words) < 2:
            return blob.strip(), ""
        return " ".join(words[:-1]).strip(), words[-1].strip()
    j = idx + 1
    if j < len(words) and words[j].strip(".,#").lower() in _UNIT_WORDS:
        j = min(j + 2, len(words))
    return " ".join(words[:j]).strip(), " ".join(words[j:]).strip()


_STATUS_MAP = {
    "active": "ACTV",
    "active ( private )": "PRIV-ACTV",
    "pending": "PEND",
    "contingent": "CTG",
    "sold": "SOLD",
    "closed": "CLSD",
    "coming soon": "NEW",
    "new": "NEW",
    "withdrawn": "EXP",
    "expired": "EXP",
}


def _parse_banner(page1_text):
    m = _BANNER_RE.search(page1_text)
    out = {}
    if not m:
        return out
    street, city = _split_street_city(m.group("addrcity"))
    out["address_line1"] = street
    out["city"] = city
    out["state"] = m.group("state")
    out["zip_code"] = m.group("zip")
    out["status"] = _STATUS_MAP.get(re.sub(r"\s+", " ", m.group("status").strip().lower()), "")
    out["list_price"] = f"${m.group('price')}"
    out["bedrooms"] = m.group("beds")
    out["bathrooms_full"] = m.group("bfull")
    out["bathrooms_half"] = m.group("bhalf") or "0"
    out["approx_sf"] = m.group("sqft")
    return out


# ---------------------------------------------------------------------------
# Schools (name + grade range + "Serves this home"/"Nearby school"/
# "Choice school" + rating -- richer than MRED/MichRIC's bare district
# code, but needs bucketing into the shared elementary/junior_high/
# high_school fields the flyer template already expects).
# ---------------------------------------------------------------------------

_SCHOOL_LINE_RE = re.compile(
    r"^(?P<name>.+?)\s+(?:Public|Charter|Private)\s*[••]\s*"
    r"(?P<grades>[A-Za-z0-9\-]+)\s*[••]\s*(?P<note>.+?)\s*"
    r"(?:Rating:\s*(?P<rating>\d+/10))?$",
    re.IGNORECASE,
)


def _parse_schools(lines):
    """Only schools flagged "Serves this home" are this property's actual
    assigned schools -- "Nearby school"/"Choice school" entries are other
    options in the area, not what the address is zoned for, so those are
    left out of the flyer's Elementary/Middle/High fields entirely rather
    than risk implying they're assigned."""
    elem = mid = high = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = _SCHOOL_LINE_RE.match(line)
        if not m or "serves this home" not in m.group("note").lower():
            continue
        name = m.group("name").strip()
        grades = m.group("grades").upper()
        name_low = name.lower()
        if not elem and ("elementary" in name_low or grades.startswith("PK") or grades.startswith("K-")):
            elem = name
        elif not high and ("high" in name_low or grades.endswith("-12")):
            high = name
        elif not mid and ("middle" in name_low or "junior" in name_low or "jr" in name_low):
            mid = name
    return elem, mid, high


# ---------------------------------------------------------------------------
# Photo -- the property photo is always the LEFTMOST image in the header's
# photo row (top of page); the Google Maps thumbnail sits to its right and
# the agent headshot further right still, confirmed identical positioning
# across every real sample regardless of property. Picking by x-position
# rather than image size/order avoids ever grabbing the map or headshot.
# ---------------------------------------------------------------------------

def _extract_photo(file_bytes, listing):
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[0]
        candidates = []
        for img in page.get_images(full=True):
            xref = img[0]
            rects = page.get_image_rects(xref)
            for r in rects:
                if r.y0 < 150:  # header photo row only
                    candidates.append((r.x0, xref))
        if candidates:
            candidates.sort(key=lambda c: c[0])
            xref = candidates[0][1]
            base = doc.extract_image(xref)
            listing.photo_bytes = base["image"]
            listing.photo_ext = base.get("ext", "jpg")
        doc.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def _page_kind(text):
    stripped = (text or "").lstrip()
    if stripped.startswith("Client -") or stripped.startswith("Client-"):
        return "client"
    if stripped.startswith("Agent -") or stripped.startswith("Agent-"):
        return "agent"
    return None


def _lot_size_from(details):
    for key in ("Lot Acres", "Lot Sq. Ft", "Lot Dimensions"):
        val = (details.get(key) or "").strip()
        if val and val != "-":
            return val
    return ""


def parse_listing_pdf(file_bytes: bytes, source_filename: str = "") -> Listing:
    listing = Listing(source_filename=source_filename)

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page_texts = [p.extract_text() or "" for p in pdf.pages]
        kinds = [_page_kind(t) for t in page_texts]

        # Prefer the Client flavor when present (simpler, and everything
        # this flyer needs is already there -- see module docstring);
        # fall back to Agent-only input if that's all that was uploaded.
        if "client" in kinds:
            use_idx = [i for i, k in enumerate(kinds) if k == "client"]
        elif "agent" in kinds:
            use_idx = [i for i, k in enumerate(kinds) if k == "agent"]
        else:
            use_idx = []

        if not use_idx:
            # Not actually this platform's format (shouldn't happen --
            # mls_router.py only dispatches here on a positive signature
            # match) -- return the mostly-empty listing rather than
            # guessing at a page layout with no recognizable banner.
            return listing

        banner = _parse_banner(page_texts[use_idx[0]])
        for field_name, val in banner.items():
            setattr(listing, field_name, val)

        details = {}      # Key Details
        history = {}      # Property History
        propdetails = {}  # Property Details
        pubrecords = {}   # Public Records
        description = ""
        amenities_text = ""
        schools_lines = []

        for i in use_idx:
            page = pdf.pages[i]
            words = page.extract_words(extra_attrs=["fontname", "size"])
            # Drop fine-print words (the compliance disclaimer paragraph
            # renders at ~5.0pt, vs. ~7.0pt for every real label/value/
            # description/amenities word and ~7.5-8.0pt for headers,
            # confirmed directly on real samples -- see module docstring's
            # "7.0pt body" convention). Without this, a section with
            # nothing else below it on the page (Amenities in particular,
            # which sits last before the disclaimer block on every real
            # sample seen) has no next-header boundary to stop at and
            # _text_section() swallows the entire multi-hundred-word
            # disclaimer paragraph into that section's value.
            words = [w for w in words if w.get("size", 0) >= 6.0]
            headers = _section_headers(words)
            bottom = page.height

            d = _grid_section(words, headers, "Key Details", bottom)
            if d:
                details.update(d)
            h = _grid_section(words, headers, "Property History", bottom)
            if h:
                history.update(h)
            pd = _grid_section(words, headers, "Property Details", bottom)
            if pd:
                propdetails.update(pd)
            # Public Records -- a county-assessor data block also present
            # on this sheet (Client page 2, Agent's equivalent page), that
            # carries a County field this platform otherwise doesn't have
            # anywhere (see module docstring's "no County field at all").
            # Also carries the current owner's name and mailing address --
            # deliberately only County gets read out of this dict below;
            # never add a wholesale dump of this section to the flyer.
            pr = _grid_section(words, headers, "Public Records", bottom)
            if pr:
                pubrecords.update(pr)
            if not description:
                description = _text_section(words, headers, "Description", bottom)
            if not amenities_text:
                amenities_text = _text_section(words, headers, "Amenities", bottom)
            if not schools_lines:
                schools_lines = _section_rows(words, headers, "Schools", bottom)

    # --- MLS / property basics ---------------------------------------------
    listing.mls_number = details.get("MLS ID", "")
    ptype = details.get("Property Type", "")
    if ptype:
        listing.property_type = ptype
    style = details.get("Architectural Style", "")
    if style:
        listing.architectural_style = style
    if not listing.year_built:
        listing.year_built = details.get("Year Built", "")
    ownership = details.get("Ownership", "")
    if ownership:
        listing.ownership = ownership

    # County -- not present anywhere in Key Details/Property Details on
    # this sheet (see module docstring), but IS present in the separate
    # Public Records section, which otherwise only carries county-assessor
    # data (owner name/mailing address, assessed value breakdown) this
    # flyer has no business showing -- County is the one field from that
    # section worth reading out. Fixes the geocoding fallback gap noted in
    # jlg-showing-packet's packet.py (_county_level()) for listings from
    # this source.
    county = pubrecords.get("County", "")
    if county and not _is_nullish(county):
        listing.county = county.title()

    # Interior fireplace count -- shares the same `fireplaces` field the
    # classic MRED parser populates from "# Fireplaces:" (see parser.py),
    # already wired into the facts strip there; this sheet just labels it
    # differently ("Num of Interior Fireplaces").
    fireplaces = details.get("Num of Interior Fireplaces", "")
    if fireplaces and not _is_nullish(fireplaces):
        listing.fireplaces = fireplaces

    # --- Parking/garage ------------------------------------------------------
    garage_spaces = details.get("Num Of Garage Spaces", "")
    parking_spaces = details.get("Num Of Parking Spaces", "")
    if garage_spaces and not _is_nullish(garage_spaces):
        n = garage_spaces.split(".")[0]
        listing.parking_type = "Garage"
        listing.parking_spaces = n
    elif parking_spaces and not _is_nullish(parking_spaces):
        n = parking_spaces.split(".")[0]
        listing.parking_type = "Space/s"
        listing.parking_spaces = n
    incl = details.get("Parking Included in Price", "")
    if incl:
        listing.parking_incl_in_price = incl

    # --- Heating/cooling (MRED-sourced listings only carry these here) -----
    heat = details.get("Heat/Fuel", "")
    if heat:
        listing.heating = heat
    cooling = details.get("Air Conditioning Type", "")
    if cooling:
        listing.cooling = cooling

    # --- Waterfront (MichRIC-sourced listings only carry these here) -------
    if details.get("Has Waterfront", "").strip().lower() == "yes":
        listing.waterfront = "Yes"
    wf = details.get("Waterfront Features", "")
    if wf and not _is_nullish(wf):
        listing.water_features = wf

    # --- Taxes / HOA ---------------------------------------------------------
    taxes = details.get("Taxes", "")
    if taxes and not _is_nullish(taxes):
        listing.tax_amount = money(taxes.split("/")[0].strip())
    hoa = details.get("HOA Fees", "")
    if hoa and not _is_nullish(hoa):
        amt, _, freq = hoa.partition("/")
        listing.assessment_amount = money(amt.strip())
        listing.assessment_frequency = freq.strip().capitalize() or "mo"

    # --- Property History: dates/DOM/price history --------------------------
    list_date = history.get("List date", "")
    if list_date and not _is_nullish(list_date):
        listing.list_date = list_date
    cur_price = history.get("Current price", "")
    if cur_price and not _is_nullish(cur_price):
        listing.list_price = money(cur_price)
    orig_price = history.get("List price", "")
    if orig_price and not _is_nullish(orig_price):
        listing.orig_list_price = money(orig_price)
    dom = history.get("DOM / CDOM", "")
    if dom and not _is_nullish(dom):
        parts = [p.strip() for p in dom.split("/")]
        if len(parts) == 2 and all(p.replace(",", "").isdigit() for p in parts):
            listing.dom_list_side, listing.dom_total = parts[0], parts[1]

    # --- Property Details: lot/stories ---------------------------------------
    lot = _lot_size_from(propdetails)
    if lot:
        listing.lot_size = lot
    # `listing.stories` (stories in the home itself, paired with Basement/
    # Fireplaces on the facts strip) vs. `listing.total_stories` (a condo
    # BUILDING's floor count, paired with Total Units/Unit Floor Level) is
    # a real distinction the shared Listing model and flyer.html template
    # already draw for classic MRED (see parser.py's own "Type
    # Detached/Attached: 2 Stories" -> listing.stories vs. "# Stories:" ->
    # listing.total_stories, with the latter's own comment noting it's
    # "especially relevant for condos/co-ops"). This sheet's "Total
    # Stories" field is the former, not the latter -- confirmed by Red Oak
    # Dr, a single-family Ranch, populating it directly with no Total
    # Units/Unit Floor Level fields anywhere on the sheet. Mapping it to
    # `total_stories` was wrong: flyer.html's facts-strip-secondary picks
    # its whole second row based on whether ANY of total_units/
    # total_stories/unit_floor_level is set, so a populated total_stories
    # on a non-condo listing silently swapped Basement/Fireplaces out for
    # a Total Units/Unit Floor row that's always blank for this source.
    stories = propdetails.get("Total Stories", "")
    if stories and not _is_nullish(stories):
        listing.stories = stories
    else:
        # Property Details' own "Total Stories" comes back blank ("-") on
        # every MRED-sourced sample seen so far -- but MRED-sourced
        # listings put the story count in Key Details' "MLS Prop Type 2"
        # instead, as free text like "2 Stories" (this mirrors the
        # classic MRED sheet's own "Type Detached/Attached: 2 Stories"
        # field, which is exactly what this fallback is patterned after).
        # MichRIC-sourced listings do the reverse: Total Stories is
        # populated directly (the branch above), and MLS Prop Type 2
        # holds a property-type string instead ("Single Family
        # Residence") that this regex simply won't match -- confirmed on
        # real samples of both, so this fallback only ever fires when
        # it's actually needed.
        m = re.search(r"(\d+(?:\.\d+)?)\s*Stor", details.get("MLS Prop Type 2", ""), re.IGNORECASE)
        if m:
            listing.stories = m.group(1)

    # --- Amenities / Description / Schools -----------------------------------
    if amenities_text:
        listing.amenities = amenities_text
        # This sheet has no dedicated Basement field -- it only ever shows
        # up as a token inside the Amenities list, either bare
        # ("Basement") or with a finish/size qualifier ("Full Basement"),
        # confirmed on real samples of both -- so a substring match (not
        # exact) is needed to catch the qualified form. Some listings
        # (3541 N Paulina) carry BOTH tokens in the same list ("...
        # Fireplace, Basement, Forced Air, Range, Full Basement, Park,
        # ...") -- MRED appears to emit a bare "Basement" amenity flag
        # alongside a separately-sourced finish/size qualifier rather than
        # one or the other. Taking the first match blindly picked the bare
        # "Basement" and threw away the more useful "Full Basement" sitting
        # right next to it, so this explicitly prefers any qualified token
        # over the bare one when both are present, and only falls back to
        # bare "Basement" -> "Yes" when that's genuinely all the sheet
        # gives us.
        basement_tokens = [t.strip() for t in amenities_text.split(",") if "basement" in t.lower()]
        if basement_tokens:
            basement_item = next((t for t in basement_tokens if t.lower() != "basement"), basement_tokens[0])
            listing.basement = "Yes" if basement_item.lower() == "basement" else basement_item
    if description:
        listing.remarks = description
    if schools_lines:
        elem, mid, high = _parse_schools(schools_lines)
        listing.elementary = elem
        listing.junior_high = mid
        listing.high_school = high

    _extract_photo(file_bytes, listing)

    return listing
