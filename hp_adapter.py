"""hp_adapter -- one flyer layout for every MLS source.

The Home Platform parser (hp_core) hands render.py a normalized record on
`listing.hp`, and render.py draws the card-style flyer (templates/flyer_hp.html)
from it. MichRIC and MRED sheets are parsed into the older flat `Listing`
fields instead, which used to mean they rendered through the legacy template
(templates/flyer.html) and looked nothing like a Home Platform flyer.

This module converts a parsed MichRIC / MRED `Listing` into the same record
shape hp_core produces, so render.py takes the card-flyer path for all three
sources. The output layout is identical no matter which MLS the sheet came
from; only the amount of detail differs (MRED private-network sheets, for
example, simply have fewer cards to fill).

Contract with the rest of the app:
  attach_hp(listing)  sets listing.hp (dict) or leaves it None on any failure,
                      in which case render.py falls back to the legacy flyer
                      so a conversion never fails outright.
"""
import logging
import re

log = logging.getLogger(__name__)

_LEVEL_MAP = {
    "main level": "Main", "main": "Main", "1st level": "Main", "first level": "Main",
    "2nd level": "Second", "second level": "Second", "upper": "Upper", "upper level": "Upper",
    "3rd level": "Third", "third level": "Third", "lower level": "Lower", "lower": "Lower",
    "basement": "Basement", "lower level / basement": "Basement",
}
_FREQ_MAP = {"yr": "annually", "year": "annually", "yearly": "annually", "annual": "annually", "mo": "monthly",
             "month": "monthly", "qtr": "quarterly", "quarter": "quarterly", "semi-annual": "semi-annually"}
_PTYPE_MAP = {"single family residence": "Single Family", "single family home": "Single Family"}


# ---------------------------------------------------------------- helpers
def _clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip(" ;,")


def _nullish(v):
    return _clean(v).lower() in ("", "-", "--", "—", "n/a", "na", "none", "unknown", "unk", "tbd", "null", "no data")


def _clist(v):
    """'A; B;C' -> 'A, B, C' (hp_core's list style)."""
    v = _clean(v)
    if not v:
        return ""
    parts = [p.strip() for p in re.split(r"[;,]\s*", v) if p.strip() and not _nullish(p)]
    return ", ".join(parts)


def _num(v):
    try:
        return float(re.sub(r"[^\d.]", "", str(v or "")) or "nan")
    except ValueError:
        return float("nan")


def _money(v):
    n = _num(v)
    if n != n:
        return ""
    return "${:,.0f}".format(n)


def _int_str(v):
    n = _num(v)
    return "{:,.0f}".format(n) if n == n else ""


def _norm_level(v):
    k = _clean(v).lower()
    return _LEVEL_MAP.get(k, _clean(v).title() if k else "")


def _norm_dims(v):
    v = _clean(v)
    if not v or v.upper() in ("COMBO",):
        return v if v.upper() == "COMBO" else ""
    return re.sub(r"\s*[xX]\s*", " x ", v)


def _school_name(v):
    v = _clean(v)
    v = re.sub(r"\s*\d+(?:st|nd|rd|th)?/Alternate:.*$", "", v)   # "(299) 2nd/Alternate:299"
    v = re.sub(r"\s*\(\d+\)\s*$", "", v)                          # trailing district number
    return v.strip()


# ------------------------------------------------------------------ build
def build_hp(l):
    import hp_core
    import render as R

    ex = getattr(l, "extras", None) or {}
    structured = bool(ex.get("structured"))
    rental = bool(l.is_rental)
    rows = {key: [] for key, *_ in hp_core.BUCKETS}

    def row(bucket, label, value):
        value = _clean(value)
        if value and not _nullish(value):
            rows[bucket].append([label, value])

    # ----- headline numbers ------------------------------------------------
    price = _money(l.list_price)
    orig = _money(l.orig_list_price) or price
    sqft = _int_str(l.approx_sf)
    ppsf = ""
    if not rental and sqft and price:
        sf_n, pr_n = _num(sqft), _num(price)
        if sf_n and pr_n == pr_n:
            ppsf = "${:,.0f}/sq ft".format(pr_n / sf_n)
    price_note = ""
    if not rental and price and orig:
        cp, op = _num(price), _num(orig)
        if op and cp < op:
            price_note = f"Reduced from {orig} ({round((op - cp) / op * 100)}% off)"
        elif op and cp > op:
            price_note = f"Raised from {orig}"

    dom = _clean(l.dom_list_side or l.dom_total)
    listed = " · ".join(x for x in (_clean(l.list_date), f"{dom} days on market" if dom else "") if x)

    # ----- price & costs ----------------------------------------------------
    row("price", "Rent" if rental else "List price", (price + ("/mo" if rental else "")) + (f"  ·  {ppsf}" if ppsf else ""))
    row("price", "Price history", price_note)
    row("price", "Listed", listed)
    hoa_n = _num(l.assessment_amount)
    hoa_amt, hoa_freq = "", ""
    if hoa_n == hoa_n and hoa_n > 0:
        hoa_amt = _money(l.assessment_amount)
        hoa_freq = _FREQ_MAP.get(_clean(l.assessment_frequency).lower(), _clean(l.assessment_frequency).lower()) or "monthly"
        row("price", "HOA", f"{hoa_amt} {hoa_freq}")
        incl = re.split(r"\s*(?:Listing and Selling|Occupant Type|Time RP|Showing Instructions)", l.assessment_includes or "")[0]
        row("price", "HOA includes", _clist(incl))
    if not _nullish(l.special_assessments):
        row("price", "Special assessments", l.special_assessments)
    if rental:
        row("price", "Lease terms", l.lease_terms)
        row("price", "Security deposit", l.security_deposit)
        row("price", "Rent includes", _clist(l.rent_includes))
        row("price", "Available", l.available_date)
    else:
        tax_amt = _money(l.tax_amount)
        if tax_amt:
            row("price", "Property tax", f"{tax_amt} ({l.tax_year})" if l.tax_year else tax_amt)
        taxable = _money(l.tax_taxable_value)
        row("price", "Taxable value", taxable)
        sev = _money(l.tax_sev)
        if sev and sev != taxable:
            row("price", "State equalized value", sev)
        if _clean(l.homestead_pct) and taxable:
            row("price", "Homestead", _clean(l.homestead_pct).replace("%", "") + "%")
        row("price", "Tax exemptions", _clist(l.tax_exemptions))
        tax_note = R.tax_uncap_note(l) or R.tax_exemption_note(l)

    # ----- location -----------------------------------------------------------
    row("location", "Directions", l.directions)
    row("location", "Cross streets", ex.get("cross_streets"))
    row("location", "Municipality", ex.get("municipality"))
    row("location", "County", l.county)
    row("location", "Exposure", _clist(l.exposure))
    row("location", "Getting around", _clist(ex.get("general_info")))
    sd = l.school_district or ex.get("school_district") or ""
    if not (l.elementary or l.junior_high or l.high_school) and re.search(r"[A-Za-z]{3}", sd or ""):
        row("location", "School district", sd)

    # ----- good to know -------------------------------------------------------
    if not rental:
        row("know", "Taxes", tax_note)
    row("know", "Open house", l.open_house)
    row("know", "Possession", l.possession)
    row("know", "Sale terms", _clist(ex.get("sale_terms")))
    if _clean(l.curr_leased).lower().startswith("y"):
        row("know", "Occupancy", "Currently leased")
    pets = re.split(r"\s+(?:Master Bedroom|Approx)\b", R.pets_display(l) or "")[0]
    row("know", "Pets", pets)
    if _clean(l.new_construction).lower() == "yes":
        row("know", "New construction", "Yes")
    if R.is_private_listing(l):
        row("know", "Private listing", "Private MLS network only; some details may be unavailable.")

    # ----- size ---------------------------------------------------------------
    stories = _clean(l.stories) or (re.search(r"(\d+)\s*Stor", l.ownership or "") or [None, ""])[1]
    row("size", "Living area", f"{sqft} sq ft" if sqft else "")
    row("size", "Stories", stories)
    row("size", "Bedrooms", l.bedrooms)

    # ----- interior -----------------------------------------------------------
    flooring = ex.get("flooring") or ", ".join(dict.fromkeys(_clean(r.get("flooring")) for r in (l.rooms or []) if _clean(r.get("flooring"))))
    int_feats = "; ".join(s for s in re.split(r";\s*", l.interior_features or "") if s.strip() and not s.strip().startswith("Exposure:"))
    row("interior", "Features", _clist(int_feats))
    row("interior", "Flooring", _clist(flooring))
    row("interior", "Windows", _clist(ex.get("windows")))
    row("interior", "Bath amenities", _clist(l.bath_amenities))
    fp_n = _clean(ex.get("fireplaces") or l.fireplaces)
    if fp_n not in ("", "0") or _clean(l.fireplace_details):
        fp_det = re.split(r"\s+(?:School District|Possession|Association|Ownership)\b", l.fireplace_details or "")[0]
        row("interior", "Fireplace", " · ".join(x for x in (fp_n if fp_n != "0" else "", _clist(fp_det)) if x))
    row("interior", "Basement", l.basement)
    row("interior", "Basement bath", l.basement_bath)
    row("interior", "Laundry", _clist(l.laundry))
    row("interior", "Total rooms", l.rooms_total)

    # ----- kitchen ------------------------------------------------------------
    row("kitchen", "Kitchen", _clist(l.kitchen_features))
    row("kitchen", "Appliances", _clist(l.appliances))

    # ----- systems ------------------------------------------------------------
    row("systems", "Heating", _clist(l.heating))
    row("systems", "Cooling", _clist(l.cooling))
    row("systems", "Electric", _clist(ex.get("electricity")))
    row("systems", "Water", _clist(l.water_source or ex.get("water")))
    row("systems", "Sewer", _clist(l.sewer_type or ex.get("sewer")))
    row("systems", "Utilities", _clist(ex.get("utilities")))
    row("systems", "At the street", _clist(ex.get("util_street")))
    row("systems", "Water heater", _clist(ex.get("water_heater")))

    # ----- lot / outdoors -----------------------------------------------------
    acres = ex.get("lot_acres")
    lot_dims = _clean(l.lot_size)
    lot_tile = ""
    if acres and _num(acres) == _num(acres):
        lot_tile = f"{_num(acres):.2f} acres".replace(".00 acres", " acres")
    elif lot_dims and not _nullish(lot_dims):
        lot_tile = lot_dims
    is_condo = bool(
        re.search(r"condo|co-op", f"{l.ownership} {l.property_type}", re.I) or l.unit_floor_level or l.total_units
    )
    if not is_condo and lot_tile:
        dims_txt = lot_dims if (lot_dims and lot_dims != lot_tile and re.search(r"\d\s*x\s*\d", lot_dims, re.I)) else ""
        row("outdoors", "Lot", " · ".join(x for x in (lot_tile, (dims_txt + " ft") if dims_txt else "") if x))
    row("outdoors", "Lot features", _clist(ex.get("lot_description")))
    row("outdoors", "Road", _clist(ex.get("street_type")))
    row("outdoors", "Landscape", _clist(ex.get("landscape")))
    blob_roof = blob_lot = blob_other = ""
    if structured:
        row("outdoors", "Exterior", _clist(ex.get("exterior_note")))
    else:
        keep = []
        for seg in re.split(r";\s*", l.exterior_features or ""):
            s = seg.strip()
            if s.startswith("Roof:"):
                blob_roof = s[5:].strip()
            elif s.startswith("Lot:"):
                blob_lot = s[4:].strip()
            elif s.startswith("Other Structures:"):
                blob_other = s[17:].strip()
            elif s:
                keep.append(s)
        row("outdoors", "Exterior", _clist("; ".join(keep)))
        row("outdoors", "Lot features", _clist(blob_lot))
        row("outdoors", "Other structures", _clist(blob_other))
    row("outdoors", "Patio / porch", _clist(ex.get("patio_porch")))
    row("outdoors", "Fencing", _clist(ex.get("fencing")))
    row("outdoors", "Pool / spa", _clist(l.pool))
    row("outdoors", "Other structures", _clist(ex.get("other_structures")))
    row("outdoors", "Security", _clist(ex.get("security")))

    # ----- building -----------------------------------------------------------
    if is_condo:
        bits = [f"{_int_str(l.total_units)} units" if l.total_units else "", f"{_int_str(l.total_stories)} stories" if l.total_stories else ""]
        row("building", "Building", " · ".join(b for b in bits if b))
        row("building", "Your unit", f"Floor {_int_str(l.unit_floor_level)}" if l.unit_floor_level else "")
    row("building", "Year built", l.year_built if not _nullish(l.year_built) else "")
    row("building", "Style", _clist(l.architectural_style))
    row("building", "Construction", _clist(ex.get("exterior_material")))
    row("building", "Roof", _clist(ex.get("roofing") or blob_roof))
    row("building", "Foundation", _clist(ex.get("foundation")))
    row("building", "Management", _clist(ex.get("management")))
    row("building", "Amenities", _clist(l.amenities))

    # ----- parking ------------------------------------------------------------
    lead = _clean(l.parking_type)
    gtype = _clean(l.garage_type)
    if gtype and "garage" in lead.lower() and gtype.lower() not in lead.lower():
        lead = f"{gtype} {lead}"
    elif gtype and lead and gtype.lower() not in lead.lower():
        lead = f"{lead} ({gtype})"
    lead = lead.capitalize() if lead.isupper() else lead
    spaces = _clean(l.parking_spaces)
    row("parking", "Parking", f"{spaces} · {lead}" if spaces and lead else lead)
    details = _clist(l.garage_details or l.parking_details)
    if details and details.lower() not in lead.lower():
        row("parking", "Details", details)
    row("parking", "Ownership", _clean(l.garage_ownership or l.parking_ownership))
    row("parking", "Driveway", _clist(ex.get("driveway")))
    pip = _clean(l.parking_incl_in_price)
    if pip.lower() in ("yes", "no"):
        row("parking", "Included in price", pip.title())

    # ----- water --------------------------------------------------------------
    if _clean(l.waterfront).lower() == "yes":
        row("water", "Waterfront", "Yes")
    _bow = re.split(r"\s*(?:Terms Available|Accessibility Features|Sale Terms|Statewide Lakes)", _clean(l.body_of_water))[0]
    row("water", "Body of water", _bow)
    row("water", "Features", _clist(l.water_features))
    if _num(l.water_frontage_ft) > 0:
        row("water", "Frontage", f"{_clean(l.water_frontage_ft)} ft")

    # ----- public record ------------------------------------------------------
    row("public", "Parcel (APN/PIN)", ex.get("tax_id"))
    row("public", "Additional parcels", R.mult_pins_display(l))
    row("public", "Zoning", ex.get("zoning"))
    row("public", "Legal description", ex.get("legal"))
    if _clean(l.pct_owner_occupied):
        row("public", "Owner-occupied", f"{_clean(l.pct_owner_occupied).replace('%', '')}% of units")

    # ----- rooms / schools / transit -------------------------------------------
    rooms = []
    for r in l.rooms or []:
        dims, lvl = _norm_dims(r.get("size")), _norm_level(r.get("level"))
        if dims or lvl:
            rooms.append({"name": _clean(r.get("name")), "level": lvl, "dims": dims, "note": ""})
    schools = []
    codes = set(re.findall(r"\((\d{2,4})\)", f"{l.elementary} {l.junior_high} {l.high_school}"))
    if "299" in codes:
        row("location", "School district", "Chicago Public Schools (299)")
    for nm, kind in ((l.elementary, "Elementary"), (l.junior_high, "Middle"), (l.high_school, "High")):
        nm = _school_name(nm)
        if nm and re.search(r"[A-Za-z]{3}", nm):
            dup = next((s for s in schools if s["name"] == nm), None)
            if dup:
                dup["grades"] = f'{dup["grades"]} / {kind}' if dup["grades"] else kind
                continue
            grades = "" if kind.lower() in nm.lower() else kind
            schools.append({"name": nm, "kind": kind, "grades": grades, "note": "", "rating": "", "serves": True})
    transit = []
    for ln in (l.nearby_transit or "").splitlines():
        m = re.match(r"(.+?)\s*[-–]\s*(\d+)\s*min", ln.strip())
        if m:
            transit.append({"name": m.group(1).strip(), "minutes": m.group(2)})

    ptype_full = R.friendly_property_type(l) or ""
    ptype, _, style = ptype_full.partition(" · ")
    ptype = _PTYPE_MAP.get(ptype.strip().lower(), ptype.strip())
    style = _clist(style or l.architectural_style)

    facts = {
        "parking_type": lead, "parking_spaces": spaces,
        "lot_size": lot_tile, "unit_floor": _int_str(l.unit_floor_level), "total_stories": _int_str(l.total_stories),
        "hoa_amount": hoa_amt, "hoa_freq": hoa_freq,
    }
    addr = {"line1": l.address_line1, "city": l.city, "state": l.state, "zip": l.zip_code, "full": l.full_address}
    out = {
        "flavor": "client", "source": "adapter", "mls_number": l.mls_number,
        "status": R.status_label_for(l), "status_raw": l.status, "address": addr,
        "list_price": price + ("/mo" if rental and price else ""), "orig_list_price": orig,
        "beds": _clean(l.bedrooms), "baths_full": _clean(l.bathrooms_full),
        "baths_half": _clean(l.bathrooms_half), "sqft": sqft,
        "year_built": l.year_built if not _nullish(l.year_built) else "",
        "property_type": ptype, "architectural_style": style, "county": _clean(l.county),
        "lat": None, "lng": None, "description": _clean(l.remarks), "amenities": _clean(l.amenities),
        "rooms": rooms, "history": [], "schools": schools, "transit": transit,
        "price_note": price_note, "price_per_sqft": ppsf, "list_date": _clean(l.list_date),
        "dom": _clean(l.dom_list_side or l.dom_total), "dom_total": _clean(l.dom_total),
        "is_condo": is_condo, "lot": lot_tile, "basement": _clean(l.basement),
        "facts": facts, "photo_bytes": l.photo_bytes,
        "buckets": [
            {"key": key, "title": title, "icon": icon, "agent_only": ao, "rows": rows[key]}
            for key, title, icon, ao in hp_core.BUCKETS if rows[key] and not ao
        ],
    }
    return out


def attach_hp(listing):
    """Fill listing.hp so render.py uses the card flyer. Never raises."""
    try:
        if getattr(listing, "hp", None):
            return listing
        hp = build_hp(listing)
        if hp["address"]["line1"] and hp["list_price"]:
            listing.hp = hp
    except Exception:
        log.exception("hp_adapter failed; using legacy flyer")
        listing.hp = None
    return listing
