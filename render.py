"""
Renders a parsed Listing into the branded, print-ready 2-page PDF flyer.
"""
import base64
import dataclasses
import datetime
import os
import re
import tempfile

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

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
    """Combine every water-related fact into one card, in the order a
    buyer would actually want to read them: the one unambiguous Yes/No
    (Waterfront -- a property can have water_features/deeded access
    without being waterfront itself, seen concretely on 2 of 3 real
    samples this was tested against), then which body of water and how
    much frontage, then the descriptive recreational amenities text, then
    the practical pool/water-source/sewer facts. MichRIC listings carry
    all of these as separate fields; called out together here rather than
    scattered across a generic exterior-features or utilities blob.

    Now also populated for MRED, where "Waterfront:No" is the overwhelming
    majority case (ordinary city listings) -- showing a bare "Not
    Waterfront" card on every single one of those would be noise, not
    signal, so the negative flag only surfaces here when there's other
    water-related content on the card to give it context; a bare "Yes"
    always surfaces on its own since that's genuinely notable either way."""
    parts = []
    other_water_info = bool(
        listing.body_of_water or listing.water_features or listing.pool
        or listing.water_source or listing.sewer_type
    )
    if listing.waterfront and (listing.waterfront.strip().lower() == "yes" or other_water_info):
        parts.append("Waterfront" if listing.waterfront.strip().lower() == "yes" else "Not Waterfront")
    if listing.body_of_water:
        bow = listing.body_of_water
        parts.append(f"{bow} — {listing.water_frontage_ft} ft frontage" if listing.water_frontage_ft else bow)
    if listing.water_features:
        parts.append(listing.water_features)
    if listing.pool:
        parts.append(f"Pool: {listing.pool}")
    if listing.water_source:
        parts.append(f"Water Source: {listing.water_source}")
    if listing.sewer_type:
        parts.append(f"Sewer: {listing.sewer_type}")
    return "; ".join(parts)


def tax_uncap_note(listing):
    """Michigan-specific disclosure: a property's 'taxable value' (what
    the shown property tax is actually based on) is capped year-over-year
    for the *current* owner, but uncaps to match the State Equalized
    Value (SEV) the year after a sale/transfer of ownership -- meaning a
    buyer's real first-year tax bill can end up meaningfully higher than
    the seller's shown property tax. Seen concretely on 2 of 3 real
    MichRIC samples this was tested against: SEV 41% and 70% above the
    current taxable value, respectively. Deliberately does NOT compute or
    promise a specific new-tax-bill number -- that's a call for the
    buyer's agent or the local assessor, not this app -- and only fires
    when there's a real gap (>2%, to ignore rounding noise), not on every
    MI listing."""
    tv_raw = (listing.tax_taxable_value or "").replace(",", "").replace("$", "")
    sev_raw = (listing.tax_sev or "").replace(",", "").replace("$", "")
    try:
        tv, sev = float(tv_raw), float(sev_raw)
    except ValueError:
        return ""
    if not tv or not sev or sev <= tv * 1.02:
        return ""
    return f"MI taxable value may uncap to match SEV (${sev:,.0f}) after sale — ask your agent for an estimated post-sale tax figure."


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

    html_str = template.render(
        l=listing,
        font_dir=FONT_DIR,
        logo_lockup=LOGO_LOCKUP_BW if print_safe_logo else LOGO_LOCKUP,
        photo_path=photo_path,
        status_label=STATUS_LABELS.get(listing.status, listing.status or "For Sale"),
        remarks_lead=lead,
        remarks_rest=rest,
        remarks_size_class=size_class,
        page2_dense=len(listing.rooms or []) > 12,
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
        water_features_display=water_features_display(listing),
        tax_uncap_note=tax_uncap_note(listing),
        tax_exemption_note=tax_exemption_note(listing),
        price_change_note=price_change_note(listing),
        is_condo_like=(listing.ownership or "").strip().lower() in ("condo", "co-op"),
        prepared_date=datetime.date.today().strftime("%B %-d, %Y"),
    )

    HTML(string=html_str, base_url=BASE_DIR).write_pdf(output_path)

    if tmp_photo:
        os.unlink(tmp_photo.name)

    return output_path
