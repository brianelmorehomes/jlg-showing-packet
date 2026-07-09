"""
Renders a parsed Listing into the branded, print-ready 2-page PDF flyer.
"""
import base64
import dataclasses
import datetime
import os
import re
import tempfile

import fitz  # PyMuPDF -- used only to count pages of our own rendered
             # output for the 2-page-cap retry loop in render_flyer()
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

# Page-2 density tiers, from loosest to tightest -- each name is a CSS
# class applied to the page-2 wrapper, the room-dimensions table, and
# every feature-group card (see flyer.html / the .page2.dense and
# .page2.very-dense rules). render_flyer() below renders at increasing
# tiers and actually measures the resulting PDF's page count each time,
# rather than trying to predict ahead of time whether a given listing's
# content will fit -- text-wrapping/height is fundamentally hard to
# predict from field lengths alone (this is exactly how a previous
# heuristic, "dense if room count > 12," broke: a listing added enough
# extra feature-card content and a longer tax-disclosure note to spill a
# 3rd page even with a totally ordinary 6-room table). See DEV_NOTES.md.
PAGE2_TIERS = ["", "dense", "very-dense"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
FONT_DIR = os.path.join(STATIC_DIR, "fonts")
LOGO_LOCKUP = os.path.join(STATIC_DIR, "logo", "jlg_atproperties_christies_lockup.png")
# Some printers/drivers render the brand's saturated red (@properties' "@"
# symbol) as near-black instead of red, no matter the print quality setting
# -- that's a printer color-management issue, not something fixable from the
# PDF side. Per @properties/Christie's own marketing guidelines: "If a piece
# is in black and white, the logo must either be all white or all black --
# no greyscale is permitted." This all-black lockup (red channel desaturated
# to match the surrounding black/white luminance, so anti-aliased edges stay
# smooth) is the compliant fallback for exactly that situation.
LOGO_LOCKUP_BW = os.path.join(STATIC_DIR, "logo", "jlg_atproperties_christies_lockup_blackonly.png")

STATUS_LABELS = {
    "NEW": "New Listing",
    "ACTV": "For Sale",
    "PCH": "Price Change",
    "BOM": "Back on Market",
    "CTG": "Contingent",
    "PCNG": "Pending",
    "PEND": "Pending",
    "ATTY": "Attorney Review",
    "SOLD": "Sold",
    "CLSD": "Sold",
}


def split_remarks(remarks: str):
    """Pull the first sentence out as an italic pull-quote lead-in, keep the
    rest as body copy. Falls back gracefully on odd punctuation."""
    if not remarks:
        return "", ""
    m = re.match(r"(.+?[.!?])\s+(.*)", remarks, re.S)
    if not m:
        return "", remarks
    lead, rest = m.group(1).strip(), m.group(2).strip()
    # Keep the lead-in short; if the first "sentence" is huge (e.g. no early
    # period), just skip the pull-quote treatment.
    if len(lead) > 160:
        return "", remarks
    return lead, rest


def friendly_property_type(listing):
    """MRED's raw property_type strings ('Attached Single', 'Detached Single')
    are MLS jargon; prefer a buyer-friendly label when we can infer one.
    Also folds in architectural_style (e.g. MichRIC "Contemporary"/"Ranch")
    and a New Construction callout when present -- both are distinguishing,
    marketable facts that deserve the same high-visibility spot right next
    to the price, not a separate card a buyer might skim past."""
    ownership = (listing.ownership or "").strip().lower()
    ptype = (listing.property_type or "").strip()
    if ownership == "condo":
        base = "Condominium"
    elif ownership == "co-op":
        base = "Co-op"
    elif "attached" in ptype.lower():
        base = "Attached Home"
    elif "detached" in ptype.lower():
        base = "Single Family Home"
    else:
        base = ptype or "Residential"

    style = (listing.architectural_style or "").strip()
    if style and style.lower() not in ("other", "n/a", "none"):
        base = f"{base} · {style}"
    if (listing.new_construction or "").strip().lower() == "yes":
        base = f"New Construction · {base}"
    return base


def lot_size_display(listing):
    """Format the MRED 'Dimensions' (lot size) field for buyer-facing display.

    MRED often stores this as shouted all-caps free text ("COMMON",
    "PER SURVEY") rather than a plain dimension string ("50 X 125"); those
    read as SHOUTING next to normally-cased labels, so sentence-case any
    value that's pure letters/spaces (leaving actual dimension strings,
    which mix in digits, untouched)."""
    val = (listing.lot_size or "").strip()
    if not val:
        return "TBD"
    if re.fullmatch(r"[A-Za-z ]+", val) and val.upper() == val:
        return val.title()
    return val


def parking_note(listing):
    """A short, explicit callout for whether parking is included in the list
    price -- this is a frequent point of buyer confusion, so it's worth
    surfacing plainly rather than leaving it buried in the raw MLS fields.
    Also surfaces the actual cost when MRED's Garage Ownership field calls
    out a separate purchase price (e.g. "Deeded Sold Separately
    ($25,000)") -- otherwise a buyer only sees "Not included in price"
    with no idea what that add-on would actually cost them."""
    incl = (listing.parking_incl_in_price or "").strip().lower()
    parts = []
    if incl == "no":
        parts.append("Not included in price")
    elif incl == "yes":
        parts.append("Included in price")
    ownership = (listing.garage_ownership or "").strip()
    if "$" in ownership:
        parts.append(ownership)
    return " — ".join(parts)


def market_time_display(listing):
    """Combine list date and days-on-market into one compact header line --
    both signal how fresh a listing is, so they belong together rather than
    as two separate stray facts."""
    parts = []
    if listing.list_date:
        try:
            d = datetime.datetime.strptime(listing.list_date, "%m/%d/%Y")
            parts.append(f"Listed {d.strftime('%b')} {d.day}, {d.year}")
        except ValueError:
            parts.append(f"Listed {listing.list_date}")
    if listing.dom_total:
        if listing.dom_list_side and listing.dom_list_side != listing.dom_total:
            parts.append(f"{listing.dom_list_side} Days on Market ({listing.dom_total} Total)")
        else:
            unit = "Day" if listing.dom_total == "1" else "Days"
            parts.append(f"{listing.dom_total} {unit} on Market")
    # Tenant-occupied is a real move-in-timeline fact a buyer needs to know
    # up front (MRED "Curr. Leased:") -- worth a spot in this same header
    # line rather than a separate card, since it's a single short flag.
    if (listing.curr_leased or "").strip().lower() == "yes":
        parts.append("Tenant-Occupied")
    return " · ".join(parts)


def mult_pins_display(listing):
    """Whether this listing's tax bill spans multiple PINs/parcels. MRED
    doesn't list the extra parcels here, just a pointer to the agent
    remarks, so translate that into a plain yes/no + pointer instead of
    showing the raw MLS phrasing verbatim."""
    val = (listing.mult_pins or "").strip()
    if not val:
        return ""
    if val.lower().startswith("no"):
        return "No"
    return "Yes (see agent remarks for parcel detail)"


def pets_display(listing):
    """Pet policy plus, when the MLS sheet specifies one, the max pet
    weight -- an easy detail to bury but one pet-owning buyers care about."""
    base = (listing.pets_allowed or "").strip()
    if not base:
        return ""
    if listing.max_pet_weight:
        return f"{base} (max {listing.max_pet_weight} lbs)"
    return base


def basement_display(listing):
    """Basement type plus whether it has its own bathroom -- a detached/
    townhome-relevant field that doesn't apply to condos."""
    base = (listing.basement or "").strip()
    if not base:
        return ""
    if (listing.basement_bath or "").strip().lower() == "yes":
        return f"{base} (bath included)"
    return base


def assessment_line_display(listing):
    """The Assessment card's headline figure. Fee-simple/detached homes
    with no HOA still carry an "Amount:$0" field -- showing that literally
    ("$0 / mo") reads like a data error, so call out plainly that there's
    no HOA/association fee instead."""
    amt = (listing.assessment_amount or "").strip()
    if not amt:
        return ""
    if amt in ("$0", "$0.00"):
        return "No HOA / Association Fee"
    return f"{amt} / {listing.assessment_frequency or 'mo'}"


def water_features_display(listing):
    """Combine the recreational/access water facts into one card, in the
    order a buyer would actually want to read them: the one unambiguous
    Yes/No (Waterfront -- a property can have water_features/deeded
    access without being waterfront itself, seen concretely on 2 of 3
    real samples this was tested against), then which body of water and
    how much frontage, then the descriptive recreational amenities text,
    then whether there's a pool. MichRIC listings carry all of these as
    separate fields; called out together here rather than scattered
    across a generic exterior-features blob.

    Water SOURCE and SEWER TYPE (well/septic/public) are deliberately
    NOT included here -- they used to be, but Brian flagged that
    distinction as critical enough for MI buyers (a real cost/
    maintenance consideration, not just a nice-to-know) that folding it
    into this broader recreational-water card wasn't prominent enough.
    They now get their own card -- see water_utilities_display() below.

    Now also populated for MRED, where "Waterfront:No" is the overwhelming
    majority case (ordinary city listings) -- showing a bare "Not
    Waterfront" card on every single one of those would be noise, not
    signal, so the negative flag only surfaces here when there's other
    water-related content on the card to give it context; a bare "Yes"
    always surfaces on its own since that's genuinely notable either way."""
    parts = []
    other_water_info = bool(listing.body_of_water or listing.water_features or listing.pool)
    if listing.waterfront and (listing.waterfront.strip().lower() == "yes" or other_water_info):
        parts.append("Waterfront" if listing.waterfront.strip().lower() == "yes" else "Not Waterfront")
    if listing.body_of_water:
        bow = listing.body_of_water
        parts.append(f"{bow} — {listing.water_frontage_ft} ft frontage" if listing.water_frontage_ft else bow)
    if listing.water_features:
        parts.append(listing.water_features)
    if listing.pool:
        parts.append(f"Pool: {listing.pool}")
    return "; ".join(parts)


def water_utilities_display(listing):
    """Water source (public/well) and sewer type (public/septic) as their
    own explicit card, split out from water_features_display() above.
    Brian's guidance: for Michigan buyers specifically, well-vs-municipal
    water and septic-vs-public sewer are real cost/maintenance facts, not
    a minor detail to bury inside a broader recreational-water card --
    "it's THAT important." Only populated for MichRIC currently (MRED's
    equivalent "Water:"/"Sewer:" fields are recognized in FEAT_LABELS but
    not yet captured -- see DEV_NOTES.md, judged lower-priority since
    Chicago-area listings are virtually always municipal on both)."""
    parts = []
    if listing.water_source:
        parts.append(f"Water: {listing.water_source}")
    if listing.sewer_type:
        parts.append(f"Sewer: {listing.sewer_type}")
    return "; ".join(parts)


def tax_uncap_note(listing):
    """Michigan-specific disclosure: a property's 'taxable value' (what
    the shown property tax is actually based on) is capped year-over-year
    for the *current* owner, but uncaps to match the State Equalized
    Value (SEV) the year after a sale/transfer of ownership. The *current*
    SEV shown on the MLS sheet reflects the current owner's situation and
    can be stale -- what a buyer actually wants to know is what SEV, and
    therefore taxable value, would likely become for THIS sale. Michigan
    assessors set SEV to roughly half of a property's true cash (market)
    value, with the sale price serving as the primary evidence used for
    that determination the year after a transfer -- so this estimates
    that directly: list price / 2, framed explicitly as an "assuming a
    sale at list price" estimate rather than a promise.

    Deliberately stops there rather than projecting an actual new-tax-
    dollar figure. Doing that would also require assuming the buyer's
    homestead status matches the current owner's, which frequently won't
    hold -- Michigan's Principal Residence Exemption swings the effective
    millage rate substantially, and all 3 real MichRIC samples this was
    tested against show 0% homestead (Southwest Michigan vacation/
    lakefront properties, not primary residences), so a same-homestead
    assumption would be wrong more often than not for Brian's actual
    listings. That final dollar estimate is left to the buyer's agent or
    the local assessor, same as before -- just anchored to a concrete
    projected SEV now instead of a vague "may uncap" with no number.

    Gated to MI listings only (checked via listing.state, not merely
    "does tax_sev/tax_taxable_value exist" -- MRED never populates those
    fields, but an early version of this gate relied on that alone, which
    would have let this MI-specific note fire on any MRED listing with a
    list price).

    Originally stopped at the projected SEV and punted the actual dollar
    figure to "ask your agent" -- deliberately avoided computing a tax
    estimate because that requires a millage rate, and this app has no
    way to look one up. But the millage rate doesn't need to be looked
    up: every MichRIC sheet already shows this exact parcel's own current
    tax_amount and tax_taxable_value, and MI tax bills are computed
    directly off taxable value, so tax_amount / tax_taxable_value * 1000
    *is* this property's real, current, all-in mill rate (county +
    township + school + library + everything else already blended in) --
    no external millage table needed. Applying that rate to the
    projected post-sale taxable value turns the old vague callout into
    an actual $/yr estimate. Still hedged into a low-high range rather
    than one number, because the one thing that materially moves this
    property's own rate is homestead status: MI's Principal Residence
    Exemption exempts roughly 18 mills of local school operating tax, so
    a buyer who will homestead vs. won't can land ~18 mills apart on the
    same taxable value. When the current owner's homestead_pct is 0 or
    100 that swing is unambiguous and becomes the low/high ends of the
    range; a partial/unknown split can't be cleanly separated, so that
    case falls back to a single point estimate at the current blended
    rate. If tax_amount itself isn't available (older/partial export),
    falls back to the original SEV-only estimate with no dollar figure."""
    if (listing.state or "").strip().upper() != "MI":
        return ""
    list_price_raw = (listing.list_price or "").replace(",", "").replace("$", "")
    tv_raw = (listing.tax_taxable_value or "").replace(",", "").replace("$", "")
    tax_raw = (listing.tax_amount or "").replace(",", "").replace("$", "")
    try:
        list_price = float(list_price_raw)
    except ValueError:
        return ""
    if not list_price:
        return ""
    est_new_sev = list_price * 0.5
    try:
        tv = float(tv_raw)
    except ValueError:
        tv = 0
    if tv and est_new_sev <= tv * 1.02:
        return ""
    homestead = (listing.homestead_pct or "").strip()

    try:
        tax_amt = float(tax_raw)
    except ValueError:
        tax_amt = 0

    if tax_amt and tv:
        current_mills = tax_amt / tv * 1000
        if homestead == "0":
            low_mills, high_mills = max(current_mills - 18, 0), current_mills
        elif homestead == "100":
            low_mills, high_mills = current_mills, current_mills + 18
        else:
            low_mills = high_mills = current_mills
        low_est = est_new_sev * low_mills / 1000
        high_est = est_new_sev * high_mills / 1000
        if low_mills == high_mills:
            range_str = f"~${low_est:,.0f}/yr"
            homestead_clause = ""
        else:
            range_str = f"~${low_est:,.0f}–${high_est:,.0f}/yr"
            homestead_clause = "; low end assumes a homestead exemption"
        return (
            f"Assuming a sale at list price, est. post-sale tax: {range_str} (taxable value resets to "
            f"~${est_new_sev:,.0f} at this property's current effective rate{homestead_clause}). "
            f"Confirm with your local assessor."
        )

    homestead_note = ""
    if homestead == "0":
        homestead_note = " Currently non-homestead — your rate may differ further if you'll occupy as your primary residence."
    return (
        f"Assuming a sale at list price, taxable value would likely reset to ~${est_new_sev:,.0f} "
        f"the year after closing (MI taxable value uncaps to match SEV, and SEV is set to roughly half "
        f"of sale price)." + homestead_note + " Ask your agent for an estimated post-sale tax figure."
    )


def tax_exemption_note(listing):
    """Illinois/Cook County-specific disclosure, parallel in spirit to
    tax_uncap_note() above: a shown property tax figure (tax_amount) is
    computed *after* whatever exemptions the current owner has on file
    (Homeowner, Senior, Senior Freeze, etc. -- MRED's "Tax Exmps:" field),
    and those don't automatically carry over to a new owner. An owner-
    occupant buyer can typically apply for their own Homeowner Exemption,
    but it doesn't show up immediately, and a Senior/Senior Freeze
    exemption won't transfer at all unless the buyer separately qualifies
    -- so the real first-year bill can run higher than what's shown here.
    Only fires when tax_exemptions actually has a value (nullish
    placeholders like "None" are already filtered out during parsing)."""
    exemptions = (listing.tax_exemptions or "").strip()
    if not exemptions:
        return ""
    return (
        f"Shown tax reflects the current owner's {exemptions} exemption(s) — "
        "these don't automatically transfer to a new owner, so your first-year "
        "bill may be higher until you establish your own exemptions."
    )


def price_change_note(listing):
    """Surface a price change (almost always a reduction) as an explicit
    negotiation signal -- MRED carries both the original and current list
    price, but only the current one shows up anywhere else on the flyer,
    so a buyer has no way to know a price drop even happened unless this
    is called out. Only fires when the two prices actually differ."""
    orig_raw = (listing.orig_list_price or "").replace("$", "").replace(",", "").strip()
    cur_raw = (listing.list_price or "").replace("$", "").replace(",", "").strip()
    try:
        orig_v, cur_v = float(orig_raw), float(cur_raw)
    except ValueError:
        return ""
    if not orig_v or not cur_v or orig_v == cur_v:
        return ""
    if cur_v < orig_v:
        pct = (orig_v - cur_v) / orig_v * 100
        return f"Reduced from {listing.orig_list_price} ({pct:.0f}% off original)"
    return f"Increased from {listing.orig_list_price}"


def remarks_size_class(remarks: str):
    """Longer agent remarks (common on content-rich detached single-family
    listings) need a tighter type scale to still fit page 1 of the fixed
    2-page layout, rather than spilling remarks onto their own extra page."""
    words = len((remarks or "").split())
    if words > 420:
        return "very-tight"
    if words > 320:
        return "tight"
    if words > 240:
        return "condensed"
    return ""


def rooms_use_compact(listing):
    """True when no room in this listing has any size or flooring data --
    the case where the standard 4-column table (Room/Size/Level/Flooring)
    wastes roughly half its width on "--" placeholders on every row.
    This is the overwhelming common case for MichRIC listings, whose
    source sheets seem to typically carry only room name + level (unlike
    MRED's reliably-populated per-room square footage and flooring) --
    confirmed on a real 20-room MichRIC sample that couldn't be made to
    fit in 2 pages even at the tightest density tier until this was
    added. Checked per-listing (not per-MLS) so a MichRIC sheet that does
    carry real dimensions, or a rare MRED sheet that doesn't, both still
    get whichever layout actually fits their real data."""
    rooms = listing.rooms or []
    if not rooms:
        return False
    return not any((r.get("size") or "").strip() or (r.get("flooring") or "").strip() for r in rooms)


def rooms_columns(listing, n_cols=3):
    """Split listing.rooms into n_cols roughly-equal columns for the
    compact room list layout. Splitting into columns (rather than one
    long single-column list) is what actually saves vertical space for
    room-heavy listings; 3 columns loosely mirrors the MichRIC source
    sheet's own 3-up room-list presentation."""
    rooms = listing.rooms or []
    if not rooms:
        return []
    per_col = -(-len(rooms) // n_cols)  # ceil division
    return [rooms[i:i + per_col] for i in range(0, len(rooms), per_col) if rooms[i:i + per_col]]


def feature_groups(listing, water_features_display_val, water_utilities_display_val):
    """Build the full list of populated Property Features groups as
    (title, body) pairs -- every group that flyer.html's Property
    Features section can show, in the order they'd naturally read.
    Consumed by feature_columns() below rather than rendered directly;
    kept as its own function so the group list (what counts as a
    "group," how each one's body text is assembled) lives in exactly one
    place instead of being duplicated between a Python balancer and a
    Jinja template."""
    groups = []
    if listing.interior_features:
        groups.append(("Interior", listing.interior_features))
    if listing.kitchen_features or listing.appliances:
        parts = []
        if listing.kitchen_features:
            parts.append(listing.kitchen_features + ("." if listing.appliances else ""))
        if listing.appliances:
            parts.append(f"Appliances: {listing.appliances}")
        groups.append(("Kitchen", " ".join(parts)))
    if listing.bath_amenities:
        groups.append(("Bath Amenities", listing.bath_amenities))
    if listing.exterior_features:
        groups.append(("Exterior", listing.exterior_features))
    if water_features_display_val:
        groups.append(("Water Access & Features", water_features_display_val))
    if water_utilities_display_val:
        groups.append(("Water Source & Sewer", water_utilities_display_val))
    if listing.heating or listing.cooling:
        parts = []
        if listing.heating:
            parts.append(f"Heat: {listing.heating}" + ("." if listing.cooling else ""))
        if listing.cooling:
            parts.append(f"AC: {listing.cooling}")
        groups.append(("Heating & Cooling", " ".join(parts)))
    if listing.laundry:
        groups.append(("Laundry", listing.laundry))
    if listing.parking_type:
        body = listing.parking_type
        if listing.parking_spaces:
            body += f" · {listing.parking_spaces} space(s)"
        if listing.garage_details:
            body += f" · {listing.garage_details}"
        groups.append(("Parking & Garage", body))
    if listing.amenities:
        groups.append(("Building Amenities", listing.amenities))
    return groups


def feature_columns(listing, water_features_display_val, water_utilities_display_val):
    """Split feature_groups() into two columns, greedily adding each
    group to whichever column currently has less total content
    (character count as a proxy for rendered height at a roughly-fixed
    font size). Previously the template hardcoded a fixed left/right
    split (Interior/Kitchen/Bath/Exterior/Water always left, Heating/
    Laundry/Parking/Amenities always right) -- reliably left-heavy for
    any listing that populates most of the left-hand fields, which is
    the common case, and got worse once Water Source & Sewer became its
    own group. Balancing by actual content adapts to whatever mix of
    fields a given listing populates instead of favoring one side by
    construction."""
    groups = feature_groups(listing, water_features_display_val, water_utilities_display_val)
    col1, col2, weight1, weight2 = [], [], 0, 0
    for title, body in groups:
        w = len(title) + len(body)
        if weight1 <= weight2:
            col1.append({"title": title, "body": body})
            weight1 += w
        else:
            col2.append({"title": title, "body": body})
            weight2 += w
    return col1, col2


def render_flyer(
    listing,
    output_path,
    agent_phone="",
    agent_email="brian@justinlucasgroup.com",
    agent_name="Brian Elmore",
    print_safe_logo=False,
):
    env = Environment(loader=FileSystemLoader(os.path.join(BASE_DIR, "templates")))
    template = env.get_template("flyer.html")

    photo_path = None
    tmp_photo = None
    if listing.photo_bytes:
        tmp_photo = tempfile.NamedTemporaryFile(
            suffix=f".{listing.photo_ext}", delete=False
        )
        tmp_photo.write(listing.photo_bytes)
        tmp_photo.close()
        photo_path = tmp_photo.name

        # Some MRED-exported listing photos are pillarboxed/letterboxed --
        # a portrait (or otherwise off-aspect) source photo gets padded with
        # solid black bars to fill a fixed thumbnail frame, and that black
        # padding is baked into the actual pixel data (not a metadata or
        # rendering issue -- it reproduces identically across every PDF
        # renderer because it's simply what the image contains). Detect and
        # crop out uniform black bars from the outer edges before use so
        # the hero photo box shows just the real photo, scaled/cropped
        # sensibly by `background-size: cover` instead of a tiny image
        # framed in black.
        try:
            from PIL import Image

            def _autocrop_black_bars(im, thresh=12, max_std=6):
                im = im.convert("RGB")
                w, h = im.size
                px = im.load()

                def col_is_bar(x):
                    vals = [px[x, y] for y in range(0, h, max(1, h // 50))]
                    means = [sum(v) / 3 for v in vals]
                    avg = sum(means) / len(means)
                    if avg > thresh:
                        return False
                    var = sum((m - avg) ** 2 for m in means) / len(means)
                    return var ** 0.5 <= max_std

                def row_is_bar(y):
                    vals = [px[x, y] for x in range(0, w, max(1, w // 50))]
                    means = [sum(v) / 3 for v in vals]
                    avg = sum(means) / len(means)
                    if avg > thresh:
                        return False
                    var = sum((m - avg) ** 2 for m in means) / len(means)
                    return var ** 0.5 <= max_std

                left = 0
                while left < w // 2 and col_is_bar(left):
                    left += 1
                right = w - 1
                while right > w // 2 and col_is_bar(right):
                    right -= 1
                top = 0
                while top < h // 2 and row_is_bar(top):
                    top += 1
                bottom = h - 1
                while bottom > h // 2 and row_is_bar(bottom):
                    bottom -= 1

                if left == 0 and right == w - 1 and top == 0 and bottom == h - 1:
                    return im
                # Guard against over-cropping a genuinely dark photo (e.g. a
                # dusk/night exterior shot): only accept the crop if it still
                # leaves a reasonably sized image.
                cropped = im.crop((left, top, right + 1, bottom + 1))
                if cropped.width < w * 0.3 or cropped.height < h * 0.3:
                    return im
                return cropped

            with Image.open(photo_path) as im:
                im = _autocrop_black_bars(im)
                im.save(photo_path, format="JPEG", quality=90, dpi=(96, 96))
        except Exception:
            pass

    lead, rest = split_remarks(listing.remarks)
    size_class = remarks_size_class(listing.remarks)
    # For very long remarks, skip the larger italic pull-quote treatment
    # entirely (it costs extra vertical space for no informational gain --
    # the quote is just the opening sentence, already in the body) and run
    # the whole passage at the tight, uniform body size instead.
    if size_class == "very-tight":
        lead, rest = "", (listing.remarks or "").strip()

    # Fields that don't depend on the page-2 density tier are computed once;
    # only page2_class changes between retry attempts below.
    wfd = water_features_display(listing)
    wud = water_utilities_display(listing)
    feature_col1, feature_col2 = feature_columns(listing, wfd, wud)
    render_kwargs = dict(
        l=listing,
        font_dir=FONT_DIR,
        logo_lockup=LOGO_LOCKUP_BW if print_safe_logo else LOGO_LOCKUP,
        photo_path=photo_path,
        status_label=STATUS_LABELS.get(listing.status, listing.status or "For Sale"),
        remarks_lead=lead,
        remarks_rest=rest,
        remarks_size_class=size_class,
        rooms_use_compact=rooms_use_compact(listing),
        rooms_columns=rooms_columns(listing),
        friendly_type=friendly_property_type(listing),
        agent_phone=agent_phone,
        agent_email=agent_email,
        agent_name=agent_name or "Brian Elmore",
        sqft_display=listing.approx_sf or "TBD",
        lot_size_display=lot_size_display(listing),
        parking_note=parking_note(listing),
        market_time_display=market_time_display(listing),
        mult_pins_display=mult_pins_display(listing),
        pets_display=pets_display(listing),
        basement_display=basement_display(listing),
        assessment_line_display=assessment_line_display(listing),
        water_features_display=wfd,
        water_utilities_display=wud,
        feature_col1=feature_col1,
        feature_col2=feature_col2,
        tax_uncap_note=tax_uncap_note(listing),
        tax_exemption_note=tax_exemption_note(listing),
        price_change_note=price_change_note(listing),
        is_condo_like=(listing.ownership or "").strip().lower() in ("condo", "co-op"),
        prepared_date=datetime.date.today().strftime("%B %-d, %Y"),
    )

    # Start at the tier the old room-count heuristic would have picked --
    # skips a wasted extra render/measure round-trip in the common case
    # where that guess is already right, without relying on it being
    # right: the loop below re-renders at a tighter tier and re-measures
    # actual page count whenever it isn't, which is what actually
    # guarantees the 2-page cap regardless of how much content a future
    # field addition piles onto page 2.
    start_tier = "dense" if len(listing.rooms or []) > 12 else ""
    start_idx = PAGE2_TIERS.index(start_tier)

    for tier_idx in range(start_idx, len(PAGE2_TIERS)):
        html_str = template.render(page2_class=PAGE2_TIERS[tier_idx], **render_kwargs)
        HTML(string=html_str, base_url=BASE_DIR).write_pdf(output_path)

        is_last_tier = tier_idx == len(PAGE2_TIERS) - 1
        if is_last_tier:
            break  # tightest tier available -- ship whatever we got
        try:
            doc = fitz.open(output_path)
            page_count = doc.page_count
            doc.close()
        except Exception:
            break  # couldn't verify -- ship what we have rather than loop
        if page_count <= 2:
            break

    if tmp_photo:
        os.unlink(tmp_photo.name)

    return output_path
