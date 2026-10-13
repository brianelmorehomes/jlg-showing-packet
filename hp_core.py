"""hp_core -- the one Home Platform listing-sheet parser shared by every JLG app.

Home Platform (Compass) prints a listing as a multi-page PDF in two flavors:

  * Client  - banner "Client - <address> ... Page n/N". Buyer-safe.
  * Agent   - banner "Agent - <address> ...". Adds Listing Contacts, Agent
              Remarks, Showing Instructions, owner info, lockbox, etc.

Both flavors use the same layout, and both come in two label vocabularies
(Chicago/MRED and Michigan/RESO). This module reads either and returns ONE
normalized structure, so a field is found, formatted and named the same way
no matter which app asks for it. The apps differ only in what they show:

  * Client-facing apps (Showing Packet) read `buckets` where agent_only is
    False. `parse_file(..., client_safe=True)` strips agent data entirely.
  * Agent-facing apps (Tour Packet Builder) read everything.

KEEP THIS FILE IDENTICAL IN EVERY APP. Edit it in one place (the shared/
folder of the app-dev project), then run sync_hp_core.sh. Do not edit a
copy inside one app.

Public API
----------
    parse_file(source, prefer="agent", client_safe=False, with_photo=False)
        -> list[dict]   one normalized dict per listing in the PDF
    HP_CORE_VERSION

Normalized dict (keys always present, "" / [] when the sheet has no data):
    flavor, mls_number, status, status_raw, address{line1,city,state,zip,full},
    list_price, orig_list_price, beds, baths_full, baths_half, sqft,
    year_built, property_type, county, lat, lng, description,
    rooms[{name,level,dims}], history[{date,event,source,price}],
    schools[{name,kind,grades,note,rating}], transit[str],
    buckets[{key,title,icon,agent_only,rows[[label,value]]}],
    agent{...} (agent flavor only), facts{canonical_key: value}, photo_bytes.
"""
from __future__ import annotations

import io
import re

import pdfplumber

HP_CORE_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Low-level reading
# ---------------------------------------------------------------------------

_BANNER_PAGE_RE = re.compile(r"\bPage\s+(\d+)\s*/\s*(\d+)\b")
_BANNER_RE = re.compile(
    r"(?P<flavor>Client|Agent)\s*-\s*(?P<addrcity>.+?)\s+(?P<state>[A-Z]{2})\s+"
    r"(?:(?P<zip>\d{5})\s+)?"
    r"(?P<status>Active\s*\(\s*Private\s*\)|Active|Pending|Contingent|Sold|Closed|"
    r"Coming Soon|Withdrawn|Expired|New|Canceled|Cancelled|Temporarily Off Market)\s+"
    r"\$(?P<price>[\d,]+)\s+"
    r"(?P<beds>\d+)\s*BD\s*[•*]\s*(?P<bfull>\d+)\s*BA"
    r"(?:\s*[•*]\s*(?P<bhalf>\d+)\s*1/2\s*BA)?\s*[•*]\s*"
    r"(?P<sqft>[\d,]+|-)\s*SF",
    re.S,
)
_WRAPPED_ZIP_RE = re.compile(r"(?m)^(\d{5})$")

_STREET_SUFFIXES = {
    "st", "street", "ave", "avenue", "rd", "road", "dr", "drive", "ln", "lane", "ct", "court",
    "blvd", "boulevard", "pl", "place", "way", "ter", "terrace", "pkwy", "parkway", "cir",
    "circle", "trl", "trail", "hwy", "highway", "sq", "square", "aly", "alley", "pt", "point",
    "run", "row", "path", "pike", "loop", "walk",
}
_UNIT_WORDS = {"unit", "apt", "apartment", "ste", "suite", "#", "no", "lot", "fl", "floor"}

_STATUS_MAP = {
    "active": "Active", "active ( private )": "Active (Private)", "pending": "Pending",
    "contingent": "Contingent", "sold": "Sold", "closed": "Closed", "coming soon": "Coming Soon",
    "new": "New", "withdrawn": "Withdrawn", "expired": "Expired", "canceled": "Canceled",
    "cancelled": "Canceled", "temporarily off market": "Off Market",
}


def _words(page):
    return [w for w in page.extract_words(extra_attrs=["fontname", "size"]) if w.get("size", 0) >= 6.0]


def _rows(words, tol=3):
    ws = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows, cur, top = [], [], None
    for w in ws:
        if top is None or abs(w["top"] - top) <= tol:
            cur.append(w)
            top = w["top"] if top is None else top
        else:
            rows.append(cur)
            cur, top = [w], w["top"]
    if cur:
        rows.append(cur)
    for r in rows:
        r.sort(key=lambda w: w["x0"])
    return rows


def _is_bold(w):
    return "Bold" in (w.get("fontname") or "")


def _headers(words):
    """[(text, top)] of >=7.5pt bold section headings, top to bottom."""
    hw = [w for w in words if w.get("size", 0) >= 7.5 and _is_bold(w)]
    return [(" ".join(w["text"] for w in r), r[0]["top"]) for r in _rows(hw)]


def _pairs_by_row(words, tol=3):
    """Ordered [(label, value)], whole rows left to right (rooms)."""
    pairs = []
    for row in _rows(words, tol):
        label, value, mode = [], [], None
        for w in row:
            if _is_bold(w):
                if mode == "value":
                    pairs.append((" ".join(label), " ".join(value)))
                    label, value = [], []
                mode = "label"
                label.append(w["text"])
            else:
                mode = "value"
                value.append(w["text"])
        if label or value:
            pairs.append((" ".join(label), " ".join(value)))
    return pairs


def _pairs_by_column(words, page_width, tol=3):
    """Ordered [(label, value)] for a two-column grid: left column top to
    bottom, then right. A row holding only regular-weight words is the
    wrapped continuation of the value above it in that column (a long value
    otherwise gets glued onto whatever sits at the same height in the other
    column). The sheet hard-wraps long unbroken lists mid-word
    ("...Oven,E" / "lectric Water Heater"); a lowercase start right after a
    letter is the same word."""
    mid = page_width * 0.49
    pairs = []
    for col in (0, 1):
        cw = [w for w in words if (w["x0"] >= mid) == bool(col)]
        for row in _rows(cw, tol):
            if not any(_is_bold(w) for w in row):
                if pairs and pairs[-1][1]:
                    cont = " ".join(w["text"] for w in row)
                    prev = pairs[-1][1]
                    glue = "" if (prev[-1].isalpha() and cont[0].islower()) else " "
                    pairs[-1] = (pairs[-1][0], prev + glue + cont)
                continue
            label, value, mode = [], [], None
            for w in row:
                if _is_bold(w):
                    if mode == "value":
                        pairs.append((" ".join(label), " ".join(value)))
                        label, value = [], []
                    mode = "label"
                    label.append(w["text"])
                else:
                    mode = "value"
                    value.append(w["text"])
            pairs.append((" ".join(label), " ".join(value)))
    return pairs


def _band_words(words, top, nxt):
    return [w for w in words if top + 2 < w["top"] < nxt]


def _band_lines(words):
    return [" ".join(w["text"] for w in r) for r in _rows(words)]


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

_NULLISH = {"", "-", "--", "none", "n/a", "na", "null", "unknown", "false", "no", "0", "0.0", "n"}


def _clean(v):
    return re.sub(r"\s+", " ", (v or "").strip())


def install_pdf_cache():
    """Speed-up for the web apps: several parsers (sniffing, the classic
    parser, enrichment, this module) each open the SAME uploaded bytes with
    pdfplumber, and pdfminer's layout pass is the slowest step by far. This
    swaps pdfplumber.open for a wrapper that, for io.BytesIO / bytes input,
    keeps the most recently parsed PDF and hands it back to later callers
    (their `with` blocks no longer close it). One entry only, so memory
    stays flat. Safe to call more than once."""
    import hashlib
    import pdfplumber
    if getattr(pdfplumber, "_jlg_pdf_cache", False):
        return
    orig_open = pdfplumber.open
    state = {"key": None, "pdf": None}

    class _Shared:
        def __init__(self, pdf):
            self._pdf = pdf

        def __getattr__(self, name):
            return getattr(self._pdf, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

    def cached_open(fp, *args, **kwargs):
        if not args and not kwargs and isinstance(fp, (bytes, io.BytesIO)):
            data = fp if isinstance(fp, bytes) else fp.getvalue()
            key = hashlib.md5(data).hexdigest()
            if state["key"] != key:
                old = state["pdf"]
                state["pdf"], state["key"] = None, None
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                state["pdf"] = orig_open(io.BytesIO(data))
                state["key"] = key
            return _Shared(state["pdf"])
        return orig_open(fp, *args, **kwargs)

    pdfplumber.open = cached_open
    pdfplumber._jlg_pdf_cache = True


def tidy_mls_text(v):
    """Repair run-together MLS pick-list values, e.g. the sheet prints
    "Close Plus30Days,See Remarks" and "Cash,Conventional,Va Loan". Adds the
    missing spaces (after commas, around embedded numbers) and fixes a few
    well-known acronyms. Leaves numbers like "$1,224" and emails untouched."""
    v = _clean(v)
    if not v:
        return v
    v = re.sub(r"([a-z])(\d+)([A-Z])", r"\1 \2 \3", v)   # Plus30Days -> Plus 30 Days
    v = re.sub(r",(?=[A-Za-z])", ", ", v)                    # a,b -> a, b
    v = re.sub(r"\bVa Loan\b", "VA Loan", v)
    v = re.sub(r"\bFha\b", "FHA", v)
    return v


def nullish(v):
    return _clean(v).lower() in _NULLISH


def clist(v):
    # split on commas, but never inside a thousands separator ("8,712 SF")
    parts = [p.strip() for p in re.split(r"(?<!\d),(?![^(]*\))|,(?!\d{3}(?!\d))(?![^(]*\))", v or "") if p.strip()]
    return ", ".join(parts)


def money_s(v):
    s = (v or "").replace(",", "").replace("$", "").strip()
    try:
        return "${:,.0f}".format(float(s))
    except ValueError:
        return _clean(v)


def num_s(v):
    s = (v or "").replace(",", "").strip()
    try:
        return "{:,.0f}".format(float(s))
    except ValueError:
        return _clean(v)


def int_s(v):
    try:
        return str(int(float((v or "").replace(",", ""))))
    except ValueError:
        return _clean(v)


def dims_s(v):
    return re.sub(r"\s*[xX]\s*", " x ", _clean(v)) if re.search(r"\d\s*[xX]\s*\d", v or "") else _clean(v)


def sqft_s(v):
    s = _clean(v)
    m = re.match(r"^([\d,\.]+)\s*(?:sqft|sq ft|sf)?$", s, re.I)
    return num_s(m.group(1)) if m else s


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


def parse_banner(text):
    m = _BANNER_RE.search(text or "")
    if not m:
        return None
    street, city = _split_street_city(m.group("addrcity"))
    zip_code = m.group("zip") or ""
    if not zip_code:
        zm = _WRAPPED_ZIP_RE.search((text or "")[:800])
        zip_code = zm.group(1) if zm else ""
    return {
        "flavor": m.group("flavor").lower(),
        "line1": street, "city": city, "state": m.group("state"), "zip": zip_code,
        "status_raw": re.sub(r"\s+", " ", m.group("status").strip()),
        "status": _STATUS_MAP.get(re.sub(r"\s+", " ", m.group("status").strip().lower()), m.group("status").strip()),
        "price": "$" + m.group("price"), "beds": m.group("beds"), "bfull": m.group("bfull"),
        "bhalf": m.group("bhalf") or "0", "sqft": "" if m.group("sqft") == "-" else m.group("sqft"),
    }


# ---------------------------------------------------------------------------
# Reading a sheet into sections
# ---------------------------------------------------------------------------

TEXT_SECTIONS = {
    "Description", "Amenities", "Showing Instructions", "Agent Remarks", "Schools", "Transit",
    "Transaction History", "Open Houses",
}
SKIP_SECTIONS = {"My Agent"}


class Section:
    __slots__ = ("name", "pairs", "lines", "room_pairs")

    def __init__(self, name):
        self.name = name
        self.pairs = []
        self.lines = []
        self.room_pairs = []


def read_sections(pdf, page_idxs):
    """Walk the pages of one sheet, return [Section]. A band is the area
    between one >=7.5pt bold heading and the next; words above the first
    heading of a page continue the previous page's last section."""
    sections = []
    prev_name = "Property Information"
    for pi in page_idxs:
        page = pdf.pages[pi]
        words = _words(page)
        heads = [(t, top) for t, top in _headers(words) if not _BANNER_PAGE_RE.search(t)]
        bounds = []
        first_top = heads[0][1] if heads else page.height
        banner_bottom = 0
        # skip the banner row itself (everything within the first ~22pt)
        bw = [w for w in words if w["top"] < 40 and _BANNER_PAGE_RE.search(" ".join(x["text"] for x in words if abs(x["top"] - w["top"]) <= 3))]
        banner_bottom = max([w["top"] for w in bw], default=0)
        bounds.append((prev_name, banner_bottom, first_top - 0.1, False))
        for i, (t, top) in enumerate(heads):
            nxt = heads[i + 1][1] if i + 1 < len(heads) else page.height + 5
            bounds.append((t, top, nxt, True))
        for name, top, nxt, is_head in bounds:
            if name in SKIP_SECTIONS:
                prev_name = name if is_head else prev_name
                continue
            bw = [w for w in words if (top + 2 < w["top"] < nxt) if is_head] if is_head else [w for w in words if banner_bottom + 1 < w["top"] < nxt]
            if not bw:
                if is_head:
                    prev_name = name
                continue
            sec = Section(name)
            if name in TEXT_SECTIONS:
                sec.lines = _band_lines(bw)
            else:
                sec.pairs = _pairs_by_column(bw, page.width)
                sec.room_pairs = _pairs_by_row(bw)
            sections.append(sec)
            if is_head:
                prev_name = name
    return sections


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def _room_name(n):
    n = re.sub(r"Bedroom #(\d)", r"Bedroom \1", n)
    n = n.replace("Master Bedroom", "Primary Bedroom")
    n = re.sub(r"\s+Separate Shwr$", "", n)
    return n.strip()


def parse_rooms(room_pairs):
    """Rooms from the ordered row pairs of every band, starting at the first
    'Room Information' label. Handles both vocabularies:
      Michigan:  Room 1 / Type <name> / Level <lvl> [/ Description <text>]
      Chicago:   <Name> Information / <Name> Level / <Name> Dimensions
    """
    start = next((i for i, (k, _) in enumerate(room_pairs) if k == "Room Information"), None)
    if start is None:
        return []
    rooms, name, cur = [], None, {}

    def flush():
        if name and (cur.get("dims") or (cur.get("level") and cur["level"].upper() not in ("N/A", "NA", "-"))):
            rooms.append({"name": _room_name(name), "level": cur.get("level", ""), "dims": cur.get("dims", ""),
                          "note": cur.get("note", "")})

    for k, v in room_pairs[start + 1:]:
        if _BANNER_PAGE_RE.search(k):
            continue
        if re.fullmatch(r"Room \d+", k):
            flush(); name = None; cur = {}
            continue
        m = re.fullmatch(r"(.+?) Information", k)
        if m and k not in ("Agent Information", "Listing Information", "Market Information", "Showing Information"):
            flush(); name = m.group(1); cur = {}
            continue
        if k in ("Agent", "Agent Information", "Sale and Listing", "Transaction History"):
            break
        if re.search(r"Level$", k):
            cur["level"] = _clean(v)
        elif re.search(r"Dimensions$", k) or k in ("Size",):
            cur["dims"] = dims_s(v)
        elif k in ("Room Type", "Type") and name is None:
            name = _clean(v)
        elif k == "Description":
            cur["note"] = _clean(v)
    flush()
    out, seen = [], set()
    for r in rooms:
        key = re.sub(r"[^a-z0-9]", "", r["name"].lower())
        if key in seen and "bath" not in r["name"].lower():
            continue  # the feed sometimes lists a room twice ("Bedroom 4" / "Bedroom4")
        seen.add(key)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Text sections: history, schools, transit
# ---------------------------------------------------------------------------

_HIST_RE = re.compile(r"^(\d\d/\d\d/\d{4})\s+(.+)$")


def parse_history(lines):
    out = []
    for ln in lines:
        m = _HIST_RE.match(ln.strip())
        if not m:
            continue
        rest = m.group(2)
        price = re.findall(r"\$[\d,]+", rest)
        ev = re.match(r"(Listed \(Active\)|Listed|Sold \(Public Record\)|Sold|Pending|Cancelled|Canceled|Price Change|"
                      r"Expired|Withdrawn|Contingent|Back on Market|Relisted)", rest)
        src = re.search(r"(MRED #\d+|Public Record #?\d*|MichRIC #?\d+|[A-Z][A-Za-z]+ #\d+)", rest)
        out.append({"date": m.group(1), "event": ev.group(1) if ev else rest.split(" MRED")[0].strip(),
                    "source": src.group(1) if src else "", "price": price[-1] if price else ""})
    return out


_SCHOOL_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<type>Public|Charter|Private|Magnet)\s*[•·]\s*(?P<grades>[A-Za-z0-9\-]+)\s*[•·]\s*"
    r"(?P<note>.+?)\s*(?:Rating:\s*(?P<rating>\d+/10))?$", re.I)


def parse_schools(lines):
    out = []
    for ln in lines:
        m = _SCHOOL_RE.match(ln.strip())
        if not m:
            continue
        name = m.group("name")
        g = m.group("grades").upper()
        low = name.lower()
        kind = ("Elementary" if re.search(r"elementary|primary|\bk-?8\b", low) or g.startswith(("PK", "K"))
                else "Middle" if re.search(r"middle|junior|\bjr\b", low)
                else "High" if re.search(r"high|\bhs\b|academy|preparatory|college prep", low) or g.startswith(("9", "10"))
                else "School")
        if re.search(r"high|preparatory", low) and not g.startswith(("PK", "K", "6")):
            kind = "High"
        out.append({"name": name, "kind": kind, "grades": m.group("grades"), "note": m.group("note").strip(),
                    "rating": m.group("rating") or "", "serves": "serves this home" in m.group("note").lower()})
    return out


_TRANSIT_RE = re.compile(r"^(?P<name>.+?)\s+(?P<min>\d+)\s*min\s*[•·]\s*(?P<mi>[\d\.]+)\s*mi", re.I)


def parse_transit(lines):
    out = []
    for ln in lines:
        m = _TRANSIT_RE.match(ln.strip())
        if m:
            out.append({"name": m.group("name"), "minutes": m.group("min"), "miles": m.group("mi")})
    return out


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

# (key, title, icon, agent_only) in display order.
BUCKETS = [
    ("showing", "Showing Instructions", "door", True),
    ("contacts", "Listing Contacts", "phone", True),
    ("agent_notes", "Agent-Only Remarks", "doc", True),
    ("price", "Price & Costs", "dollar", False),
    ("location", "Location", "pin", False),
    ("know", "Good to Know", "clock", False),
    ("size", "Size & Layout", "ruler", False),
    ("interior", "Interior", "sofa", False),
    ("kitchen", "Kitchen & Appliances", "chef", False),
    ("systems", "Systems & Utilities", "bolt", False),
    ("outdoors", "Outdoors & Lot", "tree", False),
    ("building", "The Building", "building", False),
    ("parking", "Parking", "car", False),
    ("water", "Water", "wave", False),
    ("public", "Public Record", "doc", False),
    ("listing", "Listing Details", "doc", True),
    ("owner", "Owner of Record", "doc", True),
    ("other", "Other Sheet Details", "doc", True),
]

_NOISE_LABEL = re.compile(
    r"(Level$|Dimensions$|^\$-?[\d,.]*\s*/\s*SF|^DOM$|^Owner Name$|Association Fee Frequency|Unit Floor Level|"
    r"Building/Complex|Parcel Identification|^Total # Units|^Age$|^Air Conditioning$|^Construction$|Disability Access|"
    r"^Description$)", re.I)

# Labels that carry no buyer or agent value (system flags, counters, unit
# echoes, placeholder text). Never shown, not even under "Other".
JUNK = {
    "Document Date", "Photos Count", "IDX Status", "Remarks On Internet", "StandardStatus", "Market Code",
    "Months Back Date", "Omt", "Board Number", "Managing Broker", "Public Document Count", "Rural",
    "OpenHouse Count", "Total Open Houses Count", "Virtual Tour URL Branded", "Virtual Tour URL Unbranded",
    "Virtual Tour URLUnbranded", "Virtual Tour URLBranded", "Video Groups", "Total Floor Plans Count",
    "Total Virtual Tours Count", "North Coordinate", "South Coordinate", "East Coordinate", "West Coordinate",
    "Total Sq Ft", "Aprox Total Finished Sq Ft", "Total Finished and Unfin Sq Ft", "Num Of Days For Bid Approval",
    "Agent Owned/Interest", "Property Currently Being", "Offered for Sale or Rent", "Percent Common Area Coop",
    "Special Service Area", "Habitable Residence", "List Office URL", "Co List Office URL", "Co List Office Fax",
    "Units Property Sub Type", "Units Property Type", "Units Living Area Units", "Living Area Units",
    "Building Area Units", "Lot Size Units", "Lot Measurement", "Property Sub-Type", "Property Sub Type",
    "Above Grade Finished Area Units", "Virtual Tour", "Virtual Tour Date", "Virtual Tour/Additional Media",
    "Leased", "Frequency", "Mobile Mobile", "RoomType_DR", "RoomType_FR", "RoomType_KT", "Room Type", "Type",
    "Level", "Room Level", "Dimensions", "Landscape : Shrubs/Hedges", "Notes:", "Summary", "Beds", "Baths",
    "FEES", "PRKG", "Bedrooms Total", "Total Beds Above Grade", "Bedrooms", "Full Baths", "Half Baths",
    "Bathrooms Total (Integer)", "Bathrooms Total (Decimal)", "Bathrooms Total (Notational)", "Total Baths",
    "Num of Baths", "Total Bathrooms", "Full Bathrooms", "Main Full Baths", "Main Half Baths", "Main Bedrooms",
    "Basement Bedrooms", "Basement Full Baths", "Basement Half Baths", "Basement Bathrooms",
    "Bedrooms (Below Grade)", "Total Bedrooms", "MLS Prop Type 1", "MLS Prop Type 2", "MLS Prop Type 3",
    "Zoning Description", "ZoningDescription", "Type Attached", "Type Detached", "Living Area Source",
    "SqFt Source", "Waterfront Options", "Additional Parcels", "Mineral Rights", "Property Type",
    "Property Sub-Type", "New Construction YN", "Rebuilt", "Equipped", "Has Heating", "Has Cooling",
    "Has Fireplace", "Has Basement", "Heating", "Cooling", "Garage Y/N", "Garage", "Attached Garage",
    "Fireplace", "Bedrooms Possible", "Income Property", "Accessibility Features YN", "Basement",
    "Virtual Tour YN", "Electric YN", "Rental", "Rental Information", "Pets Allowed", "Pet Policy",
    "Total Units", "Unit Floor", "Open House", "City", "Corporate Limits", "Street #", "Street Name",
    "Suffix", "State", "Zip Code", "Parcel Number", "Total Stories", "Stories", "Year Built", "Ownership",
    "Taxes", "HOA Fees", "Ownership Type",
}


# ---------------------------------------------------------------------------
# Building the normalized record
# ---------------------------------------------------------------------------

def _first_wins(pairs_iter):
    d = {}
    for k, v in pairs_iter:
        k = _clean(k)
        v = _clean(v)
        if k and v and v not in ("-", "--") and k not in d:
            d[k] = v
    return d


def _contacts(pairs):
    """[{name, dre, brokerage, email, mobile, office}] from the Listing
    Contacts block (Agent Name starts each contact)."""
    out, cur = [], None
    keymap = {"Agent Name": "name", "DRE": "license", "Brokerage": "brokerage", "Email": "email",
              "Mobile": "mobile", "Office": "office"}
    for k, v in pairs:
        k = _clean(k)
        if k == "Agent Name":
            cur = {}
            out.append(cur)
        if cur is not None and k in keymap and v and _clean(v) not in ("-", "--"):
            cur[keymap[k]] = _clean(v)
    return out


def _unit_free(line1):
    return re.sub(r"\s+(?:Unit|Apt|#|Ste|Suite)\s*\S+$", "", line1 or "", flags=re.I)


def parse_sheet(pdf, page_idxs, banner, client_safe=False):
    sections = read_sections(pdf, page_idxs)
    agent_flavor = banner["flavor"] == "agent" and not client_safe

    pairs_all, pub_pairs, contact_pairs = [], [], []
    room_pairs, text = [], {}
    for s in sections:
        if s.name in TEXT_SECTIONS:
            text.setdefault(s.name, []).extend(s.lines)
        elif s.name == "Public Records":
            pub_pairs.extend(s.pairs)
        elif s.name == "Listing Contacts":
            contact_pairs.extend(s.pairs)
        else:
            pairs_all.extend(s.pairs)
        if s.name not in TEXT_SECTIONS:
            room_pairs.extend(s.room_pairs)
    P = _first_wins(pairs_all)
    PUB = _first_wins(pub_pairs)
    used = set()

    def g(*labels, src=None):
        d = src if src is not None else P
        for lab in labels:
            v = d.get(lab, "")
            if v and not nullish(v):
                used.add(lab)
                return v
        for lab in labels:
            if lab in d:
                used.add(lab)
        return ""

    def gyes(*labels):
        for lab in labels:
            if P.get(lab, "").strip().lower() in ("yes", "true", "y"):
                used.add(lab)
                return True
        return False

    B = {key: [] for key, *_ in BUCKETS}

    def row(bucket, label, value):
        value = tidy_mls_text(value) if isinstance(value, str) else value
        if value and not nullish(value):
            B[bucket].append([label, value])

    facts = {}

    # ----- headline ---------------------------------------------------------
    desc = " ".join(text.get("Description", [])).strip()
    amenities = " ".join(text.get("Amenities", [])).strip()
    addr = {"line1": banner["line1"], "city": banner["city"], "state": banner["state"], "zip": banner["zip"]}
    addr["full"] = f"{addr['line1']}, {addr['city']}, {addr['state']} {addr['zip']}".strip(", ")
    out = {
        "flavor": "agent" if agent_flavor else "client",
        "mls_number": g("MLS ID"), "status": banner["status"], "status_raw": banner["status_raw"],
        "address": addr, "list_price": banner["price"], "beds": banner["beds"],
        "baths_full": banner["bfull"], "baths_half": banner["bhalf"], "sqft": banner["sqft"],
        "property_type": g("Property Type") or "", "description": desc, "amenities": amenities,
    }
    out["year_built"] = g("Year Built")
    out["county"] = (g("County") or _clean(PUB.get("County", ""))).title()

    # coordinates (Michigan sheets print them)
    try:
        lat, lng = float(P.get("Latitude", "")), float(P.get("Longitude", ""))
        if 24 <= lat <= 50 and -125 <= lng <= -66:
            out["lat"], out["lng"] = lat, lng
            used.update({"Latitude", "Longitude"})
    except ValueError:
        pass
    out.setdefault("lat", None)
    out.setdefault("lng", None)

    # ----- history / prices -------------------------------------------------
    cur_price = g("Current price")
    list_price = g("List price")
    out["orig_list_price"] = list_price if list_price else banner["price"]
    if cur_price:
        out["list_price"] = cur_price
    hist = parse_history(text.get("Transaction History", []))
    out["history"] = hist
    list_date = g("List date")
    dom = g("DOM / CDOM")
    price_note = ""
    try:
        cp = int(out["list_price"].replace("$", "").replace(",", ""))
        op = int(out["orig_list_price"].replace("$", "").replace(",", ""))
        if op and cp < op:
            price_note = f"Reduced from {out['orig_list_price']} ({round((op - cp) / op * 100)}% off)"
        elif op and cp > op:
            price_note = f"Raised from {out['orig_list_price']}"
    except ValueError:
        pass
    # a relist at a lower price shows up in the transaction history
    if not price_note and len(hist) >= 2:
        listed = [h for h in hist if h["event"].startswith("Listed") and h["price"]]
        if len(listed) >= 2:
            try:
                a = int(listed[0]["price"].replace("$", "").replace(",", ""))
                b = int(listed[1]["price"].replace("$", "").replace(",", ""))
                if a < b and (listed[0]["date"][-4:] == listed[1]["date"][-4:]):
                    price_note = f"Previously listed at {listed[1]['price']} on {listed[1]['date']}"
            except ValueError:
                pass
    sf_for_ppsf = out["sqft"].replace(",", "")
    ppsf = ""
    if sf_for_ppsf.isdigit() and int(sf_for_ppsf) > 0:
        try:
            ppsf = "${:,.0f}/sq ft".format(int(out["list_price"].replace("$", "").replace(",", "")) / int(sf_for_ppsf))
        except ValueError:
            pass
    out["price_note"] = price_note
    out["price_per_sqft"] = ppsf
    out["list_date"] = list_date
    dom_list = dom_total = ""
    if dom and "/" in dom:
        dom_list, dom_total = [p.strip() for p in dom.split("/", 1)]
    else:
        dom_list = g("Days on Market", "DOM") or dom
    out["dom"] = dom_list
    out["dom_total"] = dom_total
    facts.update(list_date=list_date, dom=dom_list, dom_total=dom_total)

    row("price", "List price", out["list_price"] + (f"  ·  {ppsf}" if ppsf else ""))
    row("price", "Price history", price_note)
    if list_date:
        row("price", "Listed", f"{list_date}  ·  {dom_list} days on market" + (f" ({dom_total} cumulative)" if dom_total and dom_total != dom_list else ""))

    # ----- fees, taxes -------------------------------------------------------
    hoa_raw = g("HOA Fees") or g("Approx. Assoc Fee", "Association Fee")
    hoa_amt = hoa_freq = ""
    if hoa_raw:
        m = re.match(r"^\$?([\d,\.]+)\s*(?:/\s*(\w+))?", hoa_raw)
        if m:
            try:
                if float(m.group(1).replace(",", "")) > 0:
                    hoa_amt = money_s(m.group(1))
                    hoa_freq = (m.group(2) or g("Association Fee Frequency") or "monthly").lower().replace("mo", "monthly") if (m.group(2) or "").lower() == "mo" else (m.group(2) or g("Association Fee Frequency") or "monthly").lower()
            except ValueError:
                pass
    hoa_incl = clist(g("Association Fee Includes"))
    facts.update(hoa_amount=hoa_amt, hoa_freq=hoa_freq, hoa_includes=hoa_incl)
    if hoa_amt:
        row("price", "HOA", f"{hoa_amt} {hoa_freq}")
    row("price", "HOA includes", hoa_incl)
    mf = g("Master Association Fee")
    if mf and mf.lower() not in ("not required",):
        row("price", "Master association fee", mf)
    sa = P.get("Special Assessments", "")
    if sa and sa.strip().lower() not in ("n", "no", "none", "-", "$0"):
        row("price", "Special assessments", sa)
    used.add("Special Assessments")

    tax_raw = g("Taxes") or g("Tax Annual Amount")
    tax_year = g("Tax Year")
    tax_amt = ""
    if tax_raw:
        m = re.match(r"^\$?([\d,\.]+)", tax_raw)
        tax_amt = money_s(m.group(1)) if m else tax_raw
    taxable = g("Taxable Value", "Tax Assessed Value")
    sev = g("SEV")
    homestead = P.get("Homestead", "")
    used.add("Homestead")
    facts.update(tax_amount=tax_amt, tax_year=tax_year, taxable_value=num_s(taxable) if taxable else "",
                 sev=num_s(sev) if sev else "", homestead_pct=homestead.replace("%", "").strip())
    row("price", "Property tax", f"{tax_amt} ({tax_year})" if tax_amt and tax_year else tax_amt)
    if taxable:
        row("price", "Taxable value", money_s(taxable))
    if sev:
        row("price", "State equalized value", money_s(sev))
    if homestead != "" and (taxable or sev):
        row("price", "Homestead", homestead.replace("%", "") + "%")
    row("price", "Tax exemptions", clist(g("Tax Exemptions") or PUB.get("Tax Exemptions", "")))
    ps, pdt = g("Previous sold"), g("Previous sold date")
    if ps and pdt:
        row("price", "Last sold", f"{ps}  ·  {pdt}")
    row("price", "Buyer financing", clist(g("Buyer Financing")))
    row("price", "Terms accepted", clist(g("Terms Available")))

    # ----- location ----------------------------------------------------------
    facts["directions"] = g("Directions")
    facts["cross_streets"] = g("Cross Streets", "Cross Street")
    row("location", "Directions", facts["directions"])
    row("location", "Cross streets", facts["cross_streets"])
    row("location", "Area", g("Area", "MLS Area Major", "Area - Major"))
    row("location", "Township", g("Township", "Municipality"))
    row("location", "County", out["county"])
    row("location", "Subdivision", g("Subdivision Name", "Subdivision"))
    row("location", "Exposure", clist(g("Front Exposure", "Exposure")))
    row("location", "School district", " / ".join(dict.fromkeys(
        v for v in (g("Elementary School District"), g("Middle Or Junior School District"), g("High School District")) if v)))
    facts["school_district"] = g("High School District", "School District")

    # ----- good to know ------------------------------------------------------
    row("know", "Open house", g("Open House"))
    row("know", "Possession", g("Possession"))
    row("know", "Occupancy", g("Occupant Type"))
    pet_policy = g("Pet Policy")
    pets = ""
    if pet_policy.lower() == "allowed" or gyes("Pets Allowed") or g("Pet Information"):
        pets = "Allowed"
        pi = clist(g("Pet Information"))
        if pi:
            pets += f" · {pi}"
        try:
            w = float(g("Max Pet Weight") or 0)
            if 0 < w < 900:
                pets += f" · up to {int(w)} lb"
        except ValueError:
            pass
    elif pet_policy:
        pets = pet_policy
    facts["pets"] = pets
    row("know", "Pets", pets)
    if gyes("Can Owner Rent Unit?"):
        row("know", "Rental", "Owner may rent the unit")
    row("know", "Virtual tour", "Available" if (gyes("Virtual Tour YN") or (g("Total Virtual Tours Count") not in ("", "0"))) else "")
    if gyes("Built Before 1978"):
        row("know", "Built before 1978", "Yes (lead-paint disclosure applies)")
    if gyes("New Construction") :
        row("know", "New construction", "Yes")
    if gyes("Rehabilitated"):
        ry = g("Rehab Year")
        row("know", "Rehabilitated", ry or "Yes")
    row("know", "Accessibility", clist(g("Accessibility Features")))

    # ----- size & layout -----------------------------------------------------
    def numeric(*labels):
        for lab in labels:
            v = P.get(lab, "")
            if re.fullmatch(r"[\d,]+(?:\.\d+)?", v or "") and float(v.replace(",", "")) > 0:
                used.add(lab)
                return num_s(v) + " sq ft"
        return ""
    row("size", "Living area", numeric("Living Area", "SqFt Above Grade", "Above Grade Finished Area", "Building Area Total") or (out["sqft"] and out["sqft"] + " sq ft"))
    row("size", "Total finished", numeric("Total Fin SqFt All Levels", "Total Finished SqFt"))
    row("size", "Below grade finished", numeric("Below Grade Finished Area", "Basement Finished SQFT"))
    row("size", "Basement total", numeric("Total Basement SQFT"))
    row("size", "Main level", numeric("Main Level SQFT"))
    stories = g("Stories Total") or g("Stories", "Total Stories")
    row("size", "Stories", int_s(stories) if stories else "")
    row("size", "Levels", clist(g("Levels")))
    row("size", "Bedrooms", out["beds"])
    row("size", "Bathrooms", out["baths_full"] + (f" full, {out['baths_half']} half" if out["baths_half"] not in ("", "0") else " full"))
    if gyes("Main Level Primary"):
        row("size", "Primary suite", "Main level")

    # ----- interior ----------------------------------------------------------
    row("interior", "Features", clist(g("Interior Property Features", "Interior Features")))
    row("interior", "Flooring", clist(g("Flooring")))
    row("interior", "Windows", clist(g("Window Features")))
    row("interior", "Dining", g("Dining Room"))
    pb = re.sub(r"[,\s&]+$", "", clist(g("Master Bedroom Bath")))
    row("interior", "Primary bath", pb)
    row("interior", "Bath amenities", clist(g("Bath Amenities")))
    fp_n = g("Num of Interior Fireplaces", "# Fireplaces", "Total Fireplaces", "Fireplaces Total")
    fp_det = clist(g("Fireplace Features"))
    fp_loc = g("Fireplace Location")
    if fp_n or fp_det:
        row("interior", "Fireplace", " · ".join(x for x in (int_s(fp_n) if fp_n else "", fp_det, fp_loc) if x))
    facts.update(fireplaces=int_s(fp_n) if fp_n else "", fireplace_details=fp_det)
    sub = clist(g("Substructure"))
    bt = g("Basement Type")
    bsmt = ""
    if sub or bt or gyes("Has Basement") or P.get("Basement", "").lower() in ("yes", "true"):
        bsmt = sub or bt or "Yes"
        used.update({"Basement", "Has Basement"})
    elif bt == "" and P.get("Basement Type", "").lower() == "none":
        bsmt = ""
    out["basement"] = bsmt
    row("interior", "Basement", bsmt)
    row("interior", "Laundry", clist(g("Laundry Features")))
    row("interior", "Other equipment", clist(g("Other Equipment")))
    facts["laundry"] = clist(P.get("Laundry Features", ""))
    rt = g("Num Of Rooms", "Total Rooms", "Room Total")
    facts["rooms_total"] = int_s(rt) if rt else ""
    row("interior", "Total rooms", facts["rooms_total"])

    # ----- kitchen -----------------------------------------------------------
    kf = clist(g("Kitchen Features"))
    ap = clist(g("Appliances"))
    row("kitchen", "Kitchen", kf)
    row("kitchen", "Appliances", ap)
    facts.update(kitchen_features=kf, appliances=ap)

    # ----- systems -----------------------------------------------------------
    heat_parts = [clist(g("Heat Source")), clist(g("Heat Type"))]
    heat = "; ".join(x for x in heat_parts if x)
    if not heat:
        heat = clist(g("Heat/Fuel"))
    if not heat:
        hv = P.get("Heating", "")
        if hv and hv.lower() not in ("yes", "true", "no", "false"):
            heat = clist(hv)
            used.add("Heating")
    cool = clist(g("Air Conditioning Type", "Air Conditioning", "Cooling"))
    row("systems", "Heating", heat)
    row("systems", "Cooling", cool)
    ev = g("Electric")
    row("systems", "Electric", "" if ev.lower() == "true" else clist(ev))
    wsrc = clist(g("Water Source", "Water"))
    sewer = clist(g("Sewer"))
    row("systems", "Water", wsrc)
    row("systems", "Sewer", sewer)
    row("systems", "Utilities", clist(g("Utilities")))
    row("systems", "At the street", clist(g("Util Avail at Street")))
    row("systems", "Water heater", clist(g("Water Heater")))
    facts.update(heating=heat, cooling=cool, water_source=wsrc, sewer=sewer)

    # ----- outdoors / lot ----------------------------------------------------
    acres = g("Lot Acres", "Lot Size Acres")
    lot_sf = g("Lot Sq. Ft", "Lot Square Footage", "Lot Size Area")
    lot = ""
    if acres:
        try:
            lot = f"{float(acres):.2f} acres".replace(".00 acres", " acres")
        except ValueError:
            lot = acres
    elif lot_sf and re.search(r"\d", lot_sf):
        lot = sqft_s(lot_sf.replace("sf", "").strip()) + " sq ft"
    condo = bool(g("Total Units", "Total # Units in Building") or g("Unit Floor", "Unit Floor Level"))
    out["lot"] = lot
    out["is_condo"] = condo
    facts["lot_size"] = lot
    lotdim = g("Lot Dimensions", "Lot Size Dimensions")
    if lotdim.upper() == "COMMON":
        lotdim = ""
    if not condo:
        row("outdoors", "Lot", " · ".join(x for x in (lot, lotdim.replace("'", " ft").replace(" ft x", " x") if lotdim else "") if x))
    row("outdoors", "Lot features", clist(g("Lot Features", "Lot Description")))
    fl = g("Frontage Length")
    try:
        row("outdoors", "Frontage", f"{int(float(fl))} ft" if fl else "")
    except ValueError:
        pass
    road = " · ".join(x for x in (clist(g("Road Frontage Type")), clist(g("Road Surface Type")), clist(g("Road Responsibility"))) if x)
    row("outdoors", "Road", road)
    row("outdoors", "Landscape", clist(g("Landscape", "Vegetation")))
    row("outdoors", "Exterior", clist(g("Exterior Features", "Exterior Property Features")))
    row("outdoors", "Patio / porch", clist(g("Patio And Porch Features", "Patio and Porch Features")))
    row("outdoors", "Fencing", clist(g("Fencing")))
    pool = "Pool" if gyes("Private Pool") else ""
    if gyes("Has Spa"):
        pool = (pool + " & spa") if pool else "Spa"
    row("outdoors", "Pool / spa", pool)
    row("outdoors", "Other structures", clist(g("Other Structures", "Outbuilding 1 Description")))

    # ----- building / construction -------------------------------------------
    units = g("Total Units", "Total # Units in Building")
    try:
        units = int_s(units) if units else ""
    except ValueError:
        pass
    ufloor = g("Unit Floor", "Unit Floor Level")
    total_stories = g("Stories Total") or g("Total Stories", "Stories")
    facts.update(total_units=units, unit_floor=int_s(ufloor) if ufloor else "",
                 total_stories=int_s(total_stories) if total_stories else "")
    if condo:
        row("building", "Building", " · ".join(x for x in (units and f"{units} units", total_stories and f"{int_s(total_stories)} stories") if x))
        row("building", "Your unit", f"Floor {int_s(ufloor)}" if ufloor else "")
    row("building", "Year built", g("Year Built"))
    yr = g("Year Renovated", src=PUB)
    row("building", "Style", g("Architectural Style") or ("" if g("Style", src=PUB).lower() in ("condominium", "single family residence") else g("Style", src=PUB)))
    row("building", "Construction", clist(g("Exterior Building Type", "Exterior Material", "Construction", "Construction Materials")))
    row("building", "Roof", clist(g("Roof Type", "Roof", "Roofing")))
    row("building", "Foundation", clist(g("Foundation Details")))
    row("building", "Management", " · ".join(x for x in (g("Management Company"), g("Management Contact Name"), g("Management Phone")) if x))
    row("building", "Management hours", g("Management"))
    row("building", "Amenities", clist(g("Common Area Amenities")))
    if amenities and not condo:
        pass
    out["architectural_style"] = g("Architectural Style")

    # ----- parking -----------------------------------------------------------
    pk_feat = clist(g("Parking Features"))
    pk_det = clist(g("Parking Details"))
    gs = g("Num Of Garage Spaces", "Garage Spaces")
    ps_ = g("Num Of Parking Spaces")
    low = (pk_feat + " " + pk_det).lower()
    pk_type = ""
    if gs or "garage" in low or gyes("Garage", "Garage Y/N", "Attached Garage"):
        pk_type = "Attached garage" if ("attached" in low or gyes("Attached Garage")) and "detached" not in low else \
                  "Detached garage" if "detached" in low else "Garage"
    elif ps_ or pk_feat or pk_det:
        pk_type = "Parking"
    n = int_s(gs or ps_) if (gs or ps_) else ""
    cost = g("Deeded Garage Cost")
    incl = P.get("Parking Included in Price", "")
    used.add("Parking Included in Price")
    # A bare "Parking" label adds nothing; the Details row says what it is.
    row("parking", "Parking", f"{n} · {pk_type}" if n and pk_type else ("" if pk_type == "Parking" else pk_type))
    _det = ", ".join(x.strip() for x in (pk_det or pk_feat).split(",") if x.strip() and x.strip().lower() not in ("yes", "no"))
    row("parking", "Details", _det)
    drive = clist(g("Driveway"))
    row("parking", "Driveway", drive)
    row("parking", "Deeded cost", money_s(cost) if cost else "")
    if incl.strip().lower() in ("yes", "no"):
        row("parking", "Included in price", incl.strip().title())
    facts.update(parking_type=pk_type, parking_spaces=n, parking_details=pk_det or pk_feat, parking_cost=money_s(cost) if cost else "",
                 parking_included=incl.strip().title() if incl.strip().lower() in ("yes", "no") else "", driveway=drive)

    # ----- water -------------------------------------------------------------
    bow = g("Body of Water", "Water Body Name")
    wf = gyes("Waterfront", "Has Waterfront")
    wfeat = clist(g("Water Fea Amenities", "Water Fea. Amenities", "Waterfront Features", "WaterFrontage"))
    row("water", "Waterfront", "Yes" if wf else "")
    row("water", "Body of water", bow)
    row("water", "Features", wfeat)
    facts.update(body_of_water=bow, waterfront="Yes" if wf else "", water_features=wfeat)

    # ----- public record -----------------------------------------------------
    row("public", "Parcel (APN/PIN)", g("APN", src=PUB) or g("Parcel Identification Number", "Parcel Number", "Tax ID #"))
    row("public", "Zoning", g("Zoning", src=PUB) or g("Zoning"))
    land, adds, tot = g("Land", src=PUB), g("Additions", src=PUB), g("Total", src=PUB)
    if land:
        row("public", "Assessed value", f"{land} land + {adds} improvements = {tot}" if adds and tot else land)
    row("public", "Public-record size", g("Total Finished SqFt", src=PUB))
    row("public", "Public-record lot", g("Lot Size", src=PUB))
    row("public", "Legal description", g("Legal"))
    op = g("Percent Owner Occupied")
    if op:
        row("public", "Owner-occupied", f"{int(float(op))}% of units")
    row("public", "Additional parcels", g("Additional Parcels Description"))
    facts["parcel"] = g("APN", src=PUB) or g("Parcel Identification Number", "Parcel Number", "Tax ID #")
    used.update({"Additional Parcels", "Mineral Rights"})

    # ----- rooms / schools / transit ------------------------------------------
    out["rooms"] = parse_rooms(room_pairs)
    out["schools"] = parse_schools(text.get("Schools", []))
    out["transit"] = parse_transit(text.get("Transit", []))
    out["general_transit_note"] = clist(g("General Information"))

    # ----- agent-only --------------------------------------------------------
    agent = {}
    if agent_flavor:
        si = " ".join(text.get("Showing Instructions", [])).strip()
        extra = [g("Secure ShowingAssist"), g("Instructions")]
        extra = [e for e in extra if e]
        agent["showing_instructions"] = si
        agent["showing_extra"] = " ".join(extra)
        agent["remarks"] = " ".join(text.get("Agent Remarks", [])).strip()
        agent["lockbox"] = g("Lock Box Type")
        contacts = _contacts(contact_pairs)
        agent["contacts"] = contacts
        la = {"name": g("List Agent Full Name"), "email": g("Listing Agent Email Address"),
              "mobile": g("List Agent Mobile Phone"), "office_direct": g("List Agent Office Phone")}
        agent["list_agent_detail"] = la
        agent["list_office"] = {"name": g("List Office Name"), "phone": g("List Office Phone"), "fax": g("List Office Fax")}
        agent["list_team"] = g("List Team Name")
        agent["colist"] = {"name": g("Co List Agent Full Name"), "email": g("Co List Agent Email"),
                           "mobile": g("Co List Agent Mobile Phone"), "phone": g("Co List Agent Office Phone"),
                           "office": g("Co List Office Name"), "office_phone": g("Co List Office Phone"),
                           "license": g("Co List Agent State License")}
        agent["listing_type"] = " · ".join(x for x in (g("Listing Type"), g("Additional Sales Information")) if x)
        agent["earnest_money"] = g("Holds Earnest Money")
        agent["expires"] = g("Expiration Date")
        agent["transaction_coordinator"] = g("Listing Transaction Coordinator")
        agent["owner_name"] = g("Name", src=PUB) if g("Name", src=PUB) else ""
        agent["owner_mailing"] = g("Mailing Address", src=PUB)
        agent["owner_title"] = g("Title", src=PUB)
        agent["years_owned"] = g("Years Owned", src=PUB)
        agent["owner_occupied"] = g("Owner Occupied", src=PUB)
        # buckets
        if agent["showing_instructions"] or agent["showing_extra"] or agent["lockbox"]:
            row("showing", "Instructions", agent["showing_instructions"])
            row("showing", "More", agent["showing_extra"])
            row("showing", "Lockbox", agent["lockbox"])
        for i, c in enumerate(contacts):
            who = "Listing agent" if i == 0 else "Co-listing agent" if i == 1 else f"Contact {i + 1}"
            B["contacts"].append([who, " · ".join(x for x in (c.get("name"), c.get("mobile") or c.get("office"), c.get("email"), c.get("brokerage")) if x)])
        row("contacts", "Office", " · ".join(x for x in (agent["list_office"]["name"], agent["list_office"]["phone"]) if x))
        row("contacts", "Team", agent["list_team"])
        row("agent_notes", "Remarks", agent["remarks"])
        row("listing", "Listing type", agent["listing_type"])
        row("listing", "Earnest money held by listing broker", agent["earnest_money"])
        row("listing", "Listing expires", agent["expires"])
        row("listing", "Transaction coordinator", agent["transaction_coordinator"])
        row("owner", "Owner", " · ".join(x for x in (agent["owner_name"], agent["owner_title"]) if x))
        row("owner", "Mailing address", agent["owner_mailing"])
        row("owner", "Years owned", agent["years_owned"])
        row("owner", "Owner occupied", agent["owner_occupied"])
    out["agent"] = agent

    # ----- leftovers ----------------------------------------------------------
    if agent_flavor:
        for k, v in P.items():
            if k in used or k in JUNK or nullish(v) or len(v) > 160 or _NOISE_LABEL.search(k):
                continue
            if k.startswith(("Co List", "List ")) or k in ("Agent Name", "Email", "Mobile", "Office", "DRE", "Brokerage"):
                continue
            if _clean(v).lower() in ("square feet", "sq ft", "sqft", "acres"):
                continue  # a bare unit with no number is a mis-paired cell
            row("other", k, clist(v) if "," in v and len(v) < 120 else v)

    out["buckets"] = [
        {"key": key, "title": title, "icon": icon, "agent_only": ao, "rows": B[key]}
        for key, title, icon, ao in BUCKETS if B[key] and (agent_flavor or not ao)
    ]
    out["facts"] = facts
    out["transit_note"] = out["general_transit_note"]
    return out


# ---------------------------------------------------------------------------
# File level
# ---------------------------------------------------------------------------

def _photo(file_bytes, page_idx):
    try:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[page_idx]
        cands = []
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                if r.y0 < 150:
                    cands.append((r.x0, img[0]))
        if cands:
            cands.sort()
            base = doc.extract_image(cands[0][1])
            doc.close()
            return base["image"], base.get("ext", "jpg")
        doc.close()
    except Exception:
        pass
    return None, ""


def _blocks(pdf):
    """[(page_idxs, banner)] - a new sheet starts wherever the banner's page
    counter resets to 1."""
    blocks = []
    cur = None
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        head = text[:700]
        pm = _BANNER_PAGE_RE.search(head)
        if pm and pm.group(1) == "1":
            banner = parse_banner(head)
            cur = {"pages": [i], "banner": banner}
            blocks.append(cur)
        elif cur is not None:
            cur["pages"].append(i)
        else:
            cur = {"pages": [i], "banner": parse_banner(head)}
            blocks.append(cur)
    return [b for b in blocks if b["banner"]]


def is_home_platform(pdf_or_bytes) -> bool:
    try:
        pdf = pdf_or_bytes if hasattr(pdf_or_bytes, "pages") else pdfplumber.open(io.BytesIO(pdf_or_bytes))
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
        return bool(re.search(r"(?:Agent|Client) - .+Page \d+\s*/\s*\d+", text)) and "Key Details" in text
    except Exception:
        return False


def parse_file(source, prefer="agent", client_safe=False, with_photo=False):
    """Parse a Home Platform PDF (path or bytes). Returns one dict per
    listing. A file holding both flavors of the same listing yields one
    record, from the `prefer` flavor when present. prefer='client' or
    client_safe=True never returns agent-only data."""
    data = open(source, "rb").read() if isinstance(source, str) else source
    pdf = pdfplumber.open(io.BytesIO(data))
    blocks = _blocks(pdf)
    groups = []  # consecutive blocks for the same listing
    for b in blocks:
        key = (b["banner"]["line1"].lower(), b["banner"]["zip"])
        if groups and groups[-1]["key"] == key and b["banner"]["flavor"] != groups[-1]["blocks"][-1]["banner"]["flavor"]:
            groups[-1]["blocks"].append(b)
        else:
            groups.append({"key": key, "blocks": [b]})
    results = []
    for gr in groups:
        by_flavor = {b["banner"]["flavor"]: b for b in gr["blocks"]}
        chosen = by_flavor.get(prefer) or next(iter(by_flavor.values()))
        safe = bool(client_safe or prefer == "client")
        rec = parse_sheet(pdf, chosen["pages"], chosen["banner"], client_safe=safe)
        rec["source_pages"] = chosen["pages"]
        rec["flavors_present"] = sorted(by_flavor)
        if with_photo:
            img, ext = _photo(data, chosen["pages"][0])
            rec["photo_bytes"], rec["photo_ext"] = img, ext
        results.append(rec)
    return results
