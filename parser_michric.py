"""
MichRIC listing sheet parser (Michigan MLS).

MichRIC's "Residential" sheet is a completely different physical layout
from MRED's: everything lives on ONE dense page of wrapped label:value
pairs (no fixed multi-page sections), followed by a second page that's
just the listing agent's compliance footer. There's no per-listing
pagination to split (each upload is already one listing), and the field
set itself varies by property type (waterfront listings carry frontage/
body-of-water fields, association listings carry HOA fields, a condo/
townhome unit can carry a full Elementary/Middle/High School breakdown
that a fee-simple single-family sheet doesn't, etc.).

This targets the same shared `Listing` schema as the MRED parser
(imported from parser.py, not duplicated) so every downstream piece --
the flyer template, the cover page, geocoding, the route map, the PDF
merge -- works identically regardless of which MLS a sheet came from.
Fields MichRIC doesn't provide for a given listing are left blank; the
flyer template already omits any section with no data rather than
rendering it empty.

Column layout note: several sections (Property Features, Additional
Details, the room Dimensions grid) are laid out as a fixed 3-up grid
that is NOT safe to read as plain top-to-bottom text -- a taller left
column's wrapped continuation line can butt directly against the next
column's text with zero whitespace between them (verified against real
exports, e.g. "...1 Mile or LessMain Level Primary:Yes..." is two
different columns' values glued together with no separator at all).
This is the same failure mode the MRED parser hit with its Assessment/
Tax/Schools columns. Unlike MRED's variable "Print to PDF" exports
though, MichRIC's own generated PDF uses one fixed template: every
sheet checked places these 3-up grids at the SAME two column offsets
relative to the page's own left margin (confirmed across all sample
property types/pages), so the boundaries are derived from that margin
once rather than re-detected per section.
"""
import io
import re

import fitz  # PyMuPDF
import pdfplumber

from parser import Listing, money, _grab, _column_text

# Fixed 3-up grid geometry, relative to the page's own left margin --
# confirmed identical across every sample sheet checked (Property
# Features, Additional Details, and the Dimensions room grid all share
# it: column values consistently start ~186pt and ~372pt right of the
# left margin). Kept relative rather than absolute so a slightly
# different left margin (e.g. a different print/export path) still
# lines up. The boundary itself is placed short of that start position
# rather than exactly at it -- floating-point x0 values from pdfplumber
# can land a hair below the nominal column start (observed: a word's
# real x0 landing ~0.000008pt under a boundary computed as exactly
# left_margin+186), which is enough to misclassify it into the previous
# column at a razor-thin boundary.
#
# That said, the gap has to be wide enough in the *other* direction too:
# an unusually long column-1 value (e.g. MichRIC's standardized "Public
# Access 1 Mile or Less" water-access phrase) can wrap such that its
# trailing word's real x0 lands ~13pt short of the true column-2 start
# (observed: x0=173pt-from-margin, vs. the true ~186pt column-2 start) --
# with the boundary any tighter than that, this word gets misclassified
# into column 2 instead, both truncating the column-1 value AND leaving
# a stray orphan token at the top of column 2's text (seen concretely:
# an errant "1" landing in front of "Microwave" in a Kitchen Appliances
# list, from "...Public Access 1" losing its "1"). 180 sits comfortably
# between that ~173pt worst case and the true ~186pt column-2 start,
# confirmed against every sample sheet on hand.
_COL2_OFFSET = 180
_COL3_OFFSET = 355

_LEVEL_WORDS = ("Main", "Upper", "Lower", "Basement")


def is_michric(text: str) -> bool:
    """Cheap signature check so the app can auto-route an upload to this
    parser vs. the MRED one, without asking the user which MLS it's from."""
    return "MichRIC" in (text or "")


def _dollars(s):
    """MichRIC prints dollar figures as bare numbers, sometimes with cents
    (e.g. "22,122.6") -- format to a whole-dollar "$" string matching how
    the flyer displays every other price field."""
    if not s:
        return ""
    s = s.replace(",", "").replace("$", "").strip()
    try:
        val = float(s)
    except ValueError:
        return ""
    return f"${val:,.0f}"


# Table-level fragments ("Upper Main Lower Basement Total", "Bedrooms 3 1 0
# 2 6", ...) sit in a column immediately beside the remarks paragraph and
# get interleaved row-by-row into the plain-text extraction of Public
# Remarks. This is a fixed, small MLS-standard vocabulary, so it can be
# stripped back out the same way MRED's own text-layer defects are handled
# (see _degarble in parser.py) rather than showing up as garbage mid-sentence.
_GRID_BLEED_PATTERNS = [
    r"Upper Main Lower Basement Total",
    r"\bBedrooms(?:\s+[\d,]+){1,5}",
    r"\bFull Baths(?:\s+[\d,]+){1,5}",
    r"\bHalf Baths(?:\s+[\d,]+){1,5}",
    r"\bTotal Sqft(?:\s+[\d,]+){1,5}",
    r"\bFin Sqft Lvl(?:\s+[\d,]+){1,5}",
    r"\bSqft Abv Gr(?:\s+[\d,]+)?",
    r"\bFin Blw Gr(?:\s+[\d,]+)?",
    # "Unfin Blw Gr" (Unfinished [SF] Below Grade) is itself a 2-line
    # label -- the two halves don't land next to each other in the
    # remarks text, they land wherever the remarks paragraph's own line
    # count happens to reach that row, which can be many words apart
    # (e.g. "...Unfin Blw 0 enjoy easy access..." and, much later in the
    # same paragraph, "...stretches of Lake Gr Michigan shoreline."), so
    # each half has to be stripped independently rather than as one
    # contiguous phrase.
    r"\bUnfin Blw(?:\s+[\d,]+)?\b",
    r"\bGr\b",
]

_NULLISH = {"none", "0", "n/a", "na", "nkatol", "nkatl", "-"}


def _is_nullish(val):
    return (val or "").strip().lower() in _NULLISH


_ASSOC_FREQ = {
    "monthly": "mo",
    "quarterly": "qtr",
    "annually": "yr",
    "annual": "yr",
    "semi-annually": "semi-annual",
    "biannually": "biannual",
}


def _find_row_words(words, text1, text2, top_tol=3, max_gap=140):
    """First row (top-to-bottom) where `text1` is immediately followed by
    `text2` a bit further right on the same visual line -- used to anchor
    on a two-word section header ("Property Features", "Additional
    Details") without depending on a hardcoded pixel position."""
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    for i, w in enumerate(ordered):
        if w["text"] != text1:
            continue
        for w2 in ordered[i + 1:i + 6]:
            if (
                abs(w2["top"] - w["top"]) <= top_tol
                and w["x0"] < w2["x0"] <= w["x0"] + max_gap
                and w2["text"] == text2
            ):
                return w["top"], w["x0"]
    return None


def _find_word_top(words, text, after_top=None):
    for w in sorted(words, key=lambda w: w["top"]):
        if w["text"] == text and (after_top is None or w["top"] > after_top):
            return w["top"]
    return None


def _split_boundary_merged_words(words, boundaries):
    """Some source PDFs (seen on a "flexmls Web" MichRIC export, a
    different export flavor than the ones this parser was originally
    tuned against) have a text-layer defect where two ADJACENT COLUMNS'
    words merge into one literal pdfplumber word token whenever there's
    zero horizontal gap between them right at a column boundary -- e.g.
    one column's wrapped "Low-" running directly into the next column's
    "Main Level Primary:Yes" as one glued token "Low-Main". Bucketing a
    whole word into a column by its x0 (the normal, deliberately-word-
    safe approach -- see _column_text's docstring) then puts the ENTIRE
    merged token into whichever column its x0 falls in, corrupting that
    column's value AND silently starving the other column of its first
    word -- which cascades into a second failure: e.g. Appliances
    swallowing "Level Primary:Yes" wholesale because the FEAT_STOPS
    boundary "Main Level Primary:" can no longer match text that's
    missing its leading "Main".

    Splits a word only when BOTH of these hold, to keep this from ever
    firing on ordinary same-column text:
      1. Its bounding box actually straddles one of the known column-cut
         x-coordinates (the geometric signature of this exact defect --
         "MichRIC" in the compliance footer, for instance, is nowhere
         near these boundaries and is untouched).
      2. Its text has a lowercase/hyphen-to-uppercase transition, AND
         the tail after that transition isn't itself all-uppercase (so
         "MichRIC" -> "Mich"/"RIC" is still never split even if it
         somehow were near a boundary, since "RIC" is all-caps -- this
         is what tells a real merged-value boundary like "GarageLot" ->
         "Garage"/"Lot" apart from an acronym-suffixed proper noun)."""
    out = []
    for w in words:
        text = w["text"]
        split_at = None
        for m in re.finditer(r"[a-z-](?=[A-Z])", text):
            idx = m.start() + 1
            first, second = text[:idx], text[idx:]
            if len(first) < 2 or len(second) < 2 or second.isupper():
                continue
            if any(w["x0"] < b < w["x1"] for b in boundaries):
                split_at = idx
                break
        if split_at is None:
            out.append(w)
            continue
        first_text, second_text = text[:split_at], text[split_at:]
        split_x = w["x0"] + (w["x1"] - w["x0"]) * (len(first_text) / len(text))
        out.append(dict(w, text=first_text, x1=split_x))
        out.append(dict(w, text=second_text, x0=split_x))
    return out


def _three_cols(words, left_margin, top_min, top_max, page_width):
    b1 = left_margin + _COL2_OFFSET
    b2 = left_margin + _COL3_OFFSET
    words = _split_boundary_merged_words(words, [b1, b2])
    col1 = _column_text(words, 0, b1, top_min, top_max)
    col2 = _column_text(words, b1, b2, top_min, top_max)
    col3 = _column_text(words, b2, page_width, top_min, top_max)
    return col1, col2, col3


def _grab_feat(coltext, label, stops):
    return _grab(coltext, label, [s for s in stops if s.rstrip(":") != label] + ["$", "\n"])


# Every entry needs its trailing colon (matching a real "Label:" boundary)
# -- a bare word with no colon (e.g. "View" instead of "View:") will also
# match that word wherever it legitimately appears *inside* another
# field's own value, silently truncating it right before that word. This
# bit a real listing: Water Fea. Amenities values ending "...Private
# Frontage; View" were getting cut to "...Private Frontage;", losing
# "View" itself, because a bare "View" stop (presumably added to guard
# some other field ahead of an actual "View:" label seen on a different
# sheet) matched the word "View" mid-phrase here instead.
FEAT_STOPS = [
    "Exterior Material:", "Roofing:", "Windows:", "Water Fea. Amenities:",
    "Fencing:", "Landscape:", "Pool:", "Parking Features:", "Patio and Porch Features:",
    "Garage Spaces:", "Exterior Features:", "View:",
    "Laundry Features:", "Appliances:", "Main Level Primary:", "Total Fireplaces:",
    "Air Conditioning:", "Kitchen Features:", "Flooring:",
    "Heat Source:", "Heat Type:", "Substructure:", "Water Heater:", "Water:",
    "Water Type:", "Sewer:", "Util Avail at Street:", "Utilities Attached:",
]

ROOM_NAMES = (
    r"Kitchen|Dining Room|Dining Area|Living Room|Family Room|Great Room|"
    r"Primary Bedroom|Primary Bathroom|Bedroom \d|Bathroom \d|"
    r"Laundry|Mud Room|Den|Office|Study|Loft|Bonus Room|Recreation|Rec Room|"
    r"Foyer|Sunroom|Sun Room|Four Season Room|Sauna|Other|Utility Room|"
    r"Breakfast Room|Eating Area|Pantry|Workshop|Storage|Walk In Closet"
)


def _parse_room_column(col_text):
    rooms = []
    for line in col_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r"(" + ROOM_NAMES + r")\s+([\d.]+)\s+([\d.]+)\s+(" + "|".join(_LEVEL_WORDS) + r")$",
            line,
        )
        if m:
            rooms.append({"name": m.group(1), "size": f"{m.group(2)} x {m.group(3)}", "level": m.group(4), "flooring": ""})
            continue
        m = re.match(r"(" + ROOM_NAMES + r")\s+(" + "|".join(_LEVEL_WORDS) + r")$", line)
        if m:
            rooms.append({"name": m.group(1), "size": "", "level": m.group(2), "flooring": ""})
            continue
        m = re.match(r"(" + ROOM_NAMES + r")$", line)
        if m:
            rooms.append({"name": m.group(1), "size": "", "level": "", "flooring": ""})
    return rooms


def parse_listing_pdf(file_bytes: bytes, source_filename: str = "") -> Listing:
    listing = Listing(source_filename=source_filename)

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        words = page.extract_words()
        page_width = page.width
        page_height = page.height

    left_margin = min((w["x0"] for w in words), default=27)

    # --- Status / category -----------------------------------------------
    m = re.match(
        r"\s*[A-Za-z]+\s+((?:Active|Pending|Contingent|Sold|Coming Soon|Under Contract|Withdrawn|Expired)(?:\s+Backup)?)\s+\d",
        text,
    )
    if m:
        listing.status = m.group(1)

    # --- MLS number / price -----------------------------------------------
    m = re.search(r"List Number:\s*(\d+)", text)
    if m:
        listing.mls_number = m.group(1)

    m = re.search(r"List Price:\s*\$?([\d,]+)", text)
    if m:
        listing.list_price = money("$" + m.group(1))

    # --- Address ------------------------------------------------------------
    # Zip usually sits right after the state on line 1, but on longer
    # addresses MichRIC wraps it onto line 2 instead (right before "List
    # Price Sqft:") -- both are checked rather than assuming one fixed spot.
    m = re.search(r"(\d[^\n,]*?),\s*([A-Za-z .]+?),\s*([A-Z]{2})\s*(\d{5})?\s*List Price:", text)
    if m:
        listing.address_line1 = m.group(1).strip()
        listing.city = m.group(2).strip()
        listing.state = m.group(3).strip()
        listing.zip_code = m.group(4) or ""
    if not listing.zip_code:
        m2 = re.search(r"\b(\d{5})\b\s*List Price Sqft:", text)
        if m2:
            listing.zip_code = m2.group(1)

    # --- Property type (line 2, before County: / zip / List Price Sqft) ---
    m = re.search(r"\n([A-Za-z /]+?)\s*(?:County:|(?:\d{5}\s+)?List Price Sqft:)", text)
    if m:
        listing.property_type = m.group(1).strip()

    # --- Core facts (clean single-line label:value pairs) -------------------
    m = re.search(r"Year Built:\s*(\d{4})", text)
    if m:
        listing.year_built = m.group(1)

    m = re.search(r"Total Rooms AG:\s*(\d+)", text)
    if m:
        listing.rooms_total = m.group(1)

    m = re.search(r"Stories:\s*(\d+)", text)
    if m:
        listing.stories = m.group(1)

    m = re.search(r"SqFt Above Grade:\s*([\d,]+)", text)
    if m:
        listing.approx_sf = m.group(1)

    m = re.search(r"Total Fireplaces:\s*(\d+)", text)
    if m and m.group(1) != "0":
        listing.fireplaces = m.group(1)

    m = re.search(r"\bBasement:\s*(Yes|No)", text)
    if m and m.group(1) == "Yes":
        listing.basement = "Yes"

    # --- Buyer-critical facts that were previously dropped entirely --------
    # Found by re-reading real MichRIC sheets end-to-end as a buyer would,
    # independent of what the parser already captured -- these are all
    # plain same-line label:value pairs (no column-wrap risk) so a direct
    # regex against the linear text is safe, same as Year Built/Stories/etc.
    # above.
    m = re.search(r"Days on Market:\s*(\d+)", text)
    if m:
        listing.dom_total = m.group(1)

    m = re.search(r"Waterfront:\s*(Yes|No)", text)
    if m:
        listing.waterfront = m.group(1)

    m = re.search(r"Water Access Y/N:\s*(Yes|No)", text)
    if m:
        listing.water_access = m.group(1)

    m = re.search(r"Water Frontage:\s*(\d+)", text)
    if m:
        listing.water_frontage_ft = m.group(1)

    m = re.search(r"New Construction:\s*(Yes|No)", text)
    if m:
        listing.new_construction = m.group(1)

    m = re.search(r"County:\s*([A-Za-z]+)", text)
    if m:
        listing.county = m.group(1)

    m = re.search(r"Taxable Value:\s*([\d,]+)", text)
    if m:
        listing.tax_taxable_value = m.group(1)

    m = re.search(r"\bSEV:\s*([\d,]+)", text)
    if m:
        listing.tax_sev = m.group(1)

    # "Homestead %:" -- what share of the *current* tax bill is at the
    # lower homestead (Principal Residence Exemption) rate. All 3 real
    # samples this was tested against show 0% (unsurprising -- these are
    # Southwest Michigan lakefront/vacation properties, not primary
    # residences), which materially affects how any post-sale tax
    # estimate should be caveated -- see render.py's tax_uncap_note().
    m = re.search(r"Homestead %:\s*(\d+)", text)
    if m:
        listing.homestead_pct = m.group(1)

    # --- Architectural Style / Body of Water (wrap-prone header fields) ----
    # Both live in the same 3-up header facts grid as everything above, but
    # unlike those, their *values* can land on a different visual row than
    # their label when the value is long enough to wrap ("Contemporary"
    # wraps to the next row; "Ranch" doesn't) -- the exact same failure
    # mode the Property Features grid has, just one section higher up the
    # page. A plain regex on linear page text would silently interleave
    # neighboring columns' content in between label and wrapped value, so
    # this isolates each field's own column first (word x-position, not
    # reading order) the same way the Property Features grid does, then
    # runs the normal _grab() against that column's own flattened text.
    # Column boundaries (0-364 / 364-468 / 468-page_width) were measured
    # directly against real sample sheets. Column 2's real start position
    # varies more than the Property Features grid's does -- seen as low as
    # 368.8pt on one sample vs. ~371.5pt on others -- and a wrapped
    # continuation line in column 1 (e.g. a long Association Info. value)
    # can push a word out to 359.7pt, so the col1/col2 boundary has to
    # thread a narrower needle than elsewhere in this file: 364 sits with
    # ~4-5pt to spare on both sides, confirmed against every sample sheet
    # on hand. An earlier version of this boundary (369) was too close to
    # that 368.8pt real column start and silently misclassified "Total
    # Rooms AG:" into column 1, corrupting Architectural Style's value on
    # a real listing ("Other Total" instead of "Other").
    style_top = _find_word_top(words, "Architectural")
    directions_top = next((w["top"] for w in words if w["text"].startswith("Directions:")), None)
    if style_top is not None and directions_top is not None:
        header_col1 = re.sub(r"\s*\n\s*", " ", _column_text(words, 0, 364, style_top - 1, directions_top - 1))
        header_col3 = re.sub(r"\s*\n\s*", " ", _column_text(words, 468, page_width, style_top - 1, directions_top - 1))

        style = _grab(header_col1, "Architectural Style", ["Stories:", "\n"])
        if style and not _is_nullish(style):
            listing.architectural_style = style

        bow = _grab(header_col3, "Body of Water", ["Statewide Lakes", "\n"])
        if bow and not _is_nullish(bow):
            listing.body_of_water = bow

    # Bed/bath/room-count table ("Bedrooms 3 1 0 2 6") -- the level columns
    # (Upper/Main/Lower/Basement) that are all-zero get dropped from the row
    # entirely rather than printed as 0, so the column count varies listing
    # to listing; the last number is always the total regardless.
    def _row_total(label):
        m = re.search(re.escape(label) + r"((?:\s+[\d,]+){1,5})\s*\n", text)
        if not m:
            return ""
        nums = m.group(1).split()
        return nums[-1] if nums else ""

    listing.bedrooms = _row_total("Bedrooms")
    listing.bathrooms_full = _row_total("Full Baths")
    half = _row_total("Half Baths")
    listing.bathrooms_half = half if half and half != "0" else ""

    # --- Lot size -------------------------------------------------------------
    # Only accept a genuine numeric dimension string ("325 x 325",
    # "113x176x103x200") -- anything else on this line is the *next*
    # field's label bleeding in from an adjacent column, not part of the
    # dimensions themselves.
    m = re.search(r"Lot Dimensions:\s*([\d.]+(?:\s*x\s*[\d.]+)+)", text)
    if m:
        listing.lot_size = m.group(1).strip()
    else:
        m2 = re.search(r"Lot Acres:\s*([\d.]+)", text)
        if m2:
            listing.lot_size = f"{m2.group(1)} Acres"

    # --- Directions -------------------------------------------------------------
    m = re.search(r"Directions:\s*(.*?)\s*Cross Streets:", text, re.S)
    if m:
        listing.directions = re.sub(r"\s*\n\s*", " ", m.group(1).strip())

    # --- Public remarks (strip the interleaved bed/bath table fragments) ----
    m = re.search(r"Public Remarks:\s*(.*?)\s*Dimensions\b", text, re.S)
    if m:
        remarks = m.group(1)
        for pat in _GRID_BLEED_PATTERNS:
            remarks = re.sub(pat, " ", remarks)
        remarks = re.sub(r"\s*\n\s*", " ", remarks)
        remarks = re.sub(r"\s{2,}", " ", remarks).strip()
        listing.remarks = remarks

    # --- Association / HOA --------------------------------------------------
    m = re.search(r"\bAssociation:\s*(Yes|No)", text)
    if m and m.group(1) == "Yes":
        fee_m = re.search(r"Approx\.?\s*Assoc Fee:\s*\$?([\d,]+)", text)
        freq_m = re.search(r"Assoc\.?\s*Fee Payable:\s*([A-Za-z-]+)", text)
        if fee_m:
            listing.assessment_amount = money("$" + fee_m.group(1))
        freq = (freq_m.group(1) if freq_m else "").lower()
        listing.assessment_frequency = _ASSOC_FREQ.get(freq, freq_m.group(1) if freq_m else "")
    elif m and m.group(1) == "No":
        # Mirrors the MRED convention (see assessment_line_display in
        # render.py): an explicit $0 renders as "No HOA / Association Fee"
        # instead of a blank Assessment card.
        listing.assessment_amount = "$0"

    # --- Tax & Legal ----------------------------------------------------------
    tl_start = text.find("Tax and Legal")
    pf_start = text.find("Property Features")
    tax_legal = text[tl_start:pf_start] if tl_start != -1 and pf_start != -1 else text

    m = re.search(r"Seller's Annual Property Tax:\s*([\d,.]+)", tax_legal)
    if m:
        listing.tax_amount = _dollars(m.group(1))

    m = re.search(r"For Tax Year:\s*(\d{4})", tax_legal)
    if m:
        listing.tax_year = m.group(1)

    # A condo/townhome unit's sheet can carry a full Elementary/Middle/High
    # breakdown here as a 4th column beside Taxable Value / Tax Year / SEV
    # -- when present, that's strictly better than the single district name
    # every sheet has, so it's preferred and the plain district name is
    # only used as a fallback for sheets that don't break it out.
    m = re.search(r"Elementary School:\s*([^\n]*?)(?:\s*Middle School:|\s*SEV|\n|$)", tax_legal)
    if m:
        listing.elementary = m.group(1).strip()
    m = re.search(r"Middle School:\s*([^\n]*?)(?:\s*Special Assmt|\s*For Tax|\n|$)", tax_legal)
    if m:
        listing.junior_high = m.group(1).strip()
    m = re.search(r"High School:\s*([^\n]*?)(?:\s*Homestead|\n|$)", tax_legal)
    if m:
        listing.high_school = m.group(1).strip()

    if not (listing.elementary or listing.junior_high or listing.high_school):
        m = re.search(r"School District:\s*([A-Za-z .]+?)(?:\n|Legal:|Special Assmt|Homestead|$)", tax_legal)
        if m:
            listing.school_district = m.group(1).strip()

    m = re.search(r"Special Assmt/Type:\s*([^\n]*?)(?:\s*High School:|\s*Homestead|\n|$)", tax_legal)
    if m and not _is_nullish(m.group(1)):
        listing.special_assessments = m.group(1).strip()

    # --- Property Features (fixed 3-up word-position grid) ------------------
    pf_header = _find_row_words(words, "Exterior", "Features")
    ad_header = _find_row_words(words, "Additional", "Details")
    if pf_header:
        pf_top, _ = pf_header
        bottom = ad_header[0] if ad_header else page_height
        ext_col, int_col, con_col = _three_cols(words, left_margin, pf_top - 1, bottom - 2, page_width)

        ext_flat = re.sub(r"\s*\n\s*", " ", ext_col)
        int_flat = re.sub(r"\s*\n\s*", " ", int_col)
        con_flat = re.sub(r"\s*\n\s*", " ", con_col)

        # The Exterior Features column carries several independent
        # label:value fields -- Exterior Material is always present, but
        # Roofing/Windows/Fencing/Landscape/Pool/the literal "Exterior
        # Features:" note/Patio and Porch Features are each only present
        # when the listing agent filled them in (e.g. no "Pool:" line at
        # all on a listing with no pool). An earlier version of this
        # parser only ever captured Exterior Material and silently
        # dropped everything else in this column -- including real,
        # buyer-relevant details like an in-ground pool -- so each field
        # is grabbed independently here and skipped when absent/nullish
        # rather than assumed present.
        ext_material = _grab_feat(ext_flat, "Exterior Material", FEAT_STOPS)
        roofing = _grab_feat(ext_flat, "Roofing", FEAT_STOPS)
        windows = _grab_feat(ext_flat, "Windows", FEAT_STOPS)
        fencing = _grab_feat(ext_flat, "Fencing", FEAT_STOPS)
        landscape = _grab_feat(ext_flat, "Landscape", FEAT_STOPS)
        pool = _grab_feat(ext_flat, "Pool", FEAT_STOPS)
        ext_note = _grab_feat(ext_flat, "Exterior Features", FEAT_STOPS)
        patio_porch = _grab_feat(ext_flat, "Patio and Porch Features", FEAT_STOPS)

        ext_parts = [ext_material] if ext_material and not _is_nullish(ext_material) else []
        if roofing and not _is_nullish(roofing):
            ext_parts.append(f"Roof: {roofing}")
        if windows and not _is_nullish(windows):
            ext_parts.append(f"Windows: {windows}")
        if fencing and not _is_nullish(fencing):
            ext_parts.append(f"Fencing: {fencing}")
        if landscape and not _is_nullish(landscape):
            ext_parts.append(f"Landscape: {landscape}")
        # Pool is deliberately NOT added to ext_parts/exterior_features --
        # it's surfaced on the Water Access & Features card instead (see
        # listing.pool below and render.py's water_features_display()),
        # since a pool is water-related and buyers scanning for water
        # amenities on a listing shouldn't have to also check Exterior.
        if ext_note and not _is_nullish(ext_note):
            ext_parts.append(ext_note)
        if patio_porch and not _is_nullish(patio_porch):
            ext_parts.append(f"Patio/Porch: {patio_porch}")
        listing.exterior_features = "; ".join(ext_parts)

        # Water access/features are a major selling point on lakefront
        # listings specifically -- kept as their own field (water_features
        # on Listing) with a dedicated card in flyer.html rather than
        # buried inside the general exterior-features blob above.
        water_feat = _grab_feat(ext_flat, "Water Fea. Amenities", FEAT_STOPS)
        if water_feat and not _is_nullish(water_feat):
            listing.water_features = water_feat

        if pool and not _is_nullish(pool):
            listing.pool = pool

        # The flyer's quick-fact strip has room for a short category
        # ("Attached Garage"), not the full semicolon-separated features
        # list -- that fuller list (which MichRIC's "Parking Features"
        # actually is, e.g. "Attached; Garage Door Opener; Garage Faces
        # Front") belongs in garage_details instead, matching how the
        # page-2 "Parking & Garage" group already expects to combine the
        # two (short type + spaces + full detail string).
        raw_parking = _grab_feat(ext_flat, "Parking Features", FEAT_STOPS)
        if raw_parking:
            listing.garage_details = raw_parking
            low = raw_parking.lower()
            if "attached" in low and "detached" not in low:
                listing.parking_type = "Attached Garage"
            elif "detached" in low:
                listing.parking_type = "Detached Garage"
            else:
                listing.parking_type = "Garage"
        elif re.search(r"Garage Y/N:\s*Yes", text):
            listing.parking_type = "Garage"

        m = re.search(r"Garage Spaces:\s*(\d+)", ext_flat)
        if m:
            listing.parking_spaces = m.group(1)

        listing.laundry = _grab_feat(int_flat, "Laundry Features", FEAT_STOPS)
        listing.appliances = _grab_feat(int_flat, "Appliances", FEAT_STOPS)
        listing.kitchen_features = _grab_feat(int_flat, "Kitchen Features", FEAT_STOPS)
        listing.cooling = _grab_feat(int_flat, "Air Conditioning", FEAT_STOPS)

        heat_source = _grab_feat(con_flat, "Heat Source", FEAT_STOPS)
        heat_type = _grab_feat(con_flat, "Heat Type", FEAT_STOPS)
        listing.heating = "; ".join(v for v in (heat_source, heat_type) if v)

        if listing.basement == "Yes":
            sub = _grab_feat(con_flat, "Substructure", FEAT_STOPS)
            if sub and not _is_nullish(sub):
                listing.basement = sub

        # Whether a home is on municipal water/sewer or a private well and
        # septic system is a significant, cost-relevant fact for buyers on
        # rural/lakefront Michigan properties specifically (well and
        # septic systems carry their own maintenance/inspection/repair
        # considerations that municipal service doesn't) -- like Pool,
        # these get their own dedicated fields surfaced on the Water
        # Access & Features card rather than being silently dropped (both
        # labels were already in FEAT_STOPS purely as boundary markers for
        # other grabs, but neither was ever actually captured into a
        # Listing field until now).
        water_src = _grab_feat(con_flat, "Water", FEAT_STOPS)
        if water_src and not _is_nullish(water_src):
            listing.water_source = water_src
        sewer = _grab_feat(con_flat, "Sewer", FEAT_STOPS)
        if sewer and not _is_nullish(sewer):
            listing.sewer_type = sewer

        # --- Additional Details (same 3-up grid, next section down) -------
        if ad_header:
            ad_top, _ = ad_header
            other_top = _find_word_top(words, "Other", after_top=ad_top) or _find_word_top(words, "Brian", after_top=ad_top)
            ad_bottom = other_top or page_height
            add1, add2, add3 = _three_cols(words, left_margin, ad_top - 1, ad_bottom - 2, page_width)
            add1_flat = re.sub(r"\s*\n\s*", " ", add1)
            add2_flat = re.sub(r"\s*\n\s*", " ", add2)
            add3_flat = re.sub(r"\s*\n\s*", " ", add3)

            extra_items = _grab(add1_flat, "Additional Items", ["Other Equipment:", "$"])
            if extra_items:
                listing.interior_features = (
                    f"{listing.interior_features}; {extra_items}" if listing.interior_features else extra_items
                )
            lot_desc = _grab(add2_flat, "Lot Description", ["Security Features:", "Mineral Rights:", "Zoning:", "$"])
            if lot_desc:
                listing.exterior_features = (
                    f"{listing.exterior_features}; Lot: {lot_desc}" if listing.exterior_features else f"Lot: {lot_desc}"
                )
            # "Security Features:" was already a recognized stop-boundary
            # for lot_desc above (so it wouldn't get swallowed into Lot
            # Description), but -- same pattern as Pool/Water/Sewer above
            # -- was never actually captured anywhere itself.
            security = _grab(add2_flat, "Security Features", ["Mineral Rights:", "Zoning:", "$"])
            if security and not _is_nullish(security):
                listing.interior_features = (
                    f"{listing.interior_features}; Security: {security}" if listing.interior_features else f"Security: {security}"
                )

    # --- Room dimensions (same fixed 3-up grid) ------------------------------
    dim_top = _find_word_top(words, "Dimensions")
    if dim_top is not None:
        tl_top = _find_word_top(words, "Tax", after_top=dim_top)
        bottom = tl_top if tl_top else page_height
        # Skip the header row itself ("Room Name Length Width Level" x3).
        hdr_row_bottom = dim_top + 12
        c1, c2, c3 = _three_cols(words, left_margin, hdr_row_bottom, bottom - 2, page_width)
        listing.rooms = _parse_room_column(c1) + _parse_room_column(c2) + _parse_room_column(c3)

    # --- Photo (largest/topmost embedded image = the listing photo; a
    # small compliance badge/icon can also be embedded elsewhere on the
    # page, so this isn't simply "the first image") --------------------------
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pg = doc[0]
        candidates = []
        for info in pg.get_image_info(xrefs=True):
            bbox = info["bbox"]
            width = bbox[2] - bbox[0]
            if width > 100:  # filters out small icon/badge images
                candidates.append((bbox[1], info["xref"]))  # (top, xref)
        if candidates:
            candidates.sort(key=lambda c: c[0])
            xref = candidates[0][1]
            base = doc.extract_image(xref)
            listing.photo_bytes = base["image"]
            listing.photo_ext = base.get("ext", "jpg")
        doc.close()
    except Exception:
        pass

    return listing
