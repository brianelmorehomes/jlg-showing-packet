"""
Showing packet builder.
------------------------
Takes a set of already-parsed MLS Listings plus a showing order and times,
and produces one merged, branded PDF: a cover page (ordered schedule +
route map) followed by each listing's full branded flyer, in showing order.

Geocoding uses OpenStreetMap's free Nominatim service (no API key, no
billing account -- matches the rest of this project's "no external service
signup required" philosophy). It's rate-limited to 1 request/second per
Nominatim's usage policy, so building the map adds roughly 1 second per
stop; for a typical showing day (a handful of stops) that's a few seconds,
and it fails soft -- if geocoding is unavailable (no internet, a stop's
address doesn't resolve, etc.) the packet is still built, just without a
map or without that one pin.
"""
import io
import os
import re
import tempfile

import pdfplumber
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageDraw, ImageFont

from render import render_flyer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
FONT_DIR = os.path.join(STATIC_DIR, "fonts")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
LOGO_LOCKUP = os.path.join(STATIC_DIR, "logo", "jlg_atproperties_christies_lockup.png")
LOGO_LOCKUP_BW = os.path.join(STATIC_DIR, "logo", "jlg_atproperties_christies_lockup_blackonly.png")
PIN_FONT = os.path.join(FONT_DIR, "WorkSans-Bold-Final.ttf")

NAVY = (3, 43, 66, 255)
WHITE = (255, 255, 255, 255)


# ---------------------------------------------------------------------------
# Multi-listing batch export splitting
# ---------------------------------------------------------------------------

def split_into_listing_pdfs(file_bytes):
    """A single uploaded PDF is usually one listing's sheet (1-3 pages), but
    agents commonly batch-export several listings into ONE PDF at once from
    MRED (e.g. "print" a whole search result set) -- that comes back as one
    file with each listing's 2ish pages concatenated back-to-back. Uploading
    a file like that should surface every address inside it, not just the
    first one.

    Detection: MRED repeats "MLS #:<number>" as a running header on every
    page belonging to one listing; the number changes exactly where the
    next listing's pages start. Grouping consecutive pages by that number
    is a more reliable boundary signal than a fixed page count, since one
    listing can run 2 or 3 pages depending on how much content it has. A
    page where the number can't be read (e.g. a stray disclaimer-only page)
    is treated as a continuation of whatever listing precedes it, rather
    than starting a new group.

    Returns a list of standalone single-listing PDFs (as bytes). For an
    ordinary single-listing upload this returns `[file_bytes]` unchanged --
    the common case is a no-op, and downstream parsing doesn't need to know
    whether a split happened."""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            page_mls = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                m = re.search(r"MLS #:\s*(\d+)", text)
                page_mls.append(m.group(1) if m else None)
    except Exception:
        return [file_bytes]

    groups = []
    current_mls = None
    current_pages = []
    for i, mls in enumerate(page_mls):
        if mls is not None and current_pages and mls != current_mls:
            groups.append(current_pages)
            current_pages = []
        current_pages.append(i)
        if mls is not None:
            current_mls = mls
    if current_pages:
        groups.append(current_pages)

    if len(groups) <= 1:
        return [file_bytes]

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        blobs = []
        for pages in groups:
            writer = PdfWriter()
            for p in pages:
                writer.add_page(reader.pages[p])
            buf = io.BytesIO()
            writer.write(buf)
            blobs.append(buf.getvalue())
        return blobs
    except Exception:
        return [file_bytes]


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

# Nominatim's free-text search reliably fails on addresses that include a
# unit/apt/suite designator (e.g. "875 N Michigan Ave Unit 3105") -- it's
# not a recognized token in its address grammar, so the whole query comes
# back with no match. The unit is irrelevant for a map pin anyway (we only
# need the building), so it's stripped before geocoding rather than passed
# through verbatim.
_UNIT_RE = re.compile(
    r"\s+(?:unit|apt|apartment|ste|suite|#|no\.?|floor|fl\.?)\s*\.?\s*[\w-]+\s*(?=,|$)",
    re.IGNORECASE,
)


def _strip_unit(address):
    return _UNIT_RE.sub("", address or "").strip()


def geocode_addresses(addresses, user_agent="jlg-showing-packet-app"):
    """Best-effort geocode a list of full address strings (street + city/
    state/zip) to (lat, lon). Returns a list the same length as `addresses`,
    with None in place of any address that failed to resolve or if
    geocoding is unavailable at all (e.g. no internet) -- callers should
    treat every entry as optional."""
    results = [None] * len(addresses)
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter

        # A cold connection's first request or two (DNS + TLS handshake) can
        # occasionally take longer than a warm one -- observed in practice
        # to sometimes exceed a 10s timeout on the very first lookup of a
        # batch, which would otherwise drop that one stop's pin for a
        # reason that has nothing to do with the address itself. A longer
        # timeout plus a couple of retries (geopy retries with backoff)
        # absorbs that without giving up on a stop over a slow first
        # connection.
        geolocator = Nominatim(user_agent=user_agent, timeout=15)
        geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1, max_retries=2, error_wait_seconds=2.0, swallow_exceptions=True)
        for i, addr in enumerate(addresses):
            if not addr:
                continue
            candidates = [addr]
            stripped = _strip_unit(addr)
            if stripped != addr:
                candidates.append(stripped)
            for candidate in candidates:
                try:
                    loc = geocode(candidate)
                except Exception:
                    loc = None
                if loc:
                    results[i] = (loc.latitude, loc.longitude)
                    break
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Numbered route map
# ---------------------------------------------------------------------------

def _make_pin(number, size=44):
    img = Image.new("RGBA", (size, size + 14), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 2, size - 2], fill=NAVY, outline=WHITE, width=2)
    d.polygon(
        [(size / 2 - 8, size - 6), (size / 2 + 8, size - 6), (size / 2, size + 12)],
        fill=NAVY,
    )
    try:
        font = ImageFont.truetype(PIN_FONT, int(size * 0.46))
    except Exception:
        font = ImageFont.load_default()
    text = str(number)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 2), text, font=font, fill=WHITE)
    fd, path = tempfile.mkstemp(suffix=f"_pin{number}.png")
    os.close(fd)
    img.save(path)
    return path, size


def _spread_coincident_points(valid):
    """Multiple showing stops are frequently different units in the *same*
    building (a common way to tour a high-rise) -- geocoding strips the
    unit number, so those stops resolve to the exact same rooftop
    coordinate. Left alone, their pins would land on the identical pixel
    and only the last one drawn would be visible, silently "losing" every
    earlier stop at that address. This nudges duplicates apart in a small
    circle around their shared point (sized relative to how spread out the
    whole route is, so it stays visible whether the stops span one block
    or across the city) so every stop keeps a distinguishable pin without
    materially misstating its location."""
    from collections import defaultdict
    import math

    groups = defaultdict(list)
    for n, (lat, lon) in valid:
        groups[(round(lat, 5), round(lon, 5))].append(n)

    if all(len(ns) == 1 for ns in groups.values()):
        return valid

    lats = [p[0] for _, p in valid]
    lons = [p[1] for _, p in valid]
    span = max(max(lats) - min(lats), max(lons) - min(lons))
    radius = max(span * 0.035, 0.0006)

    offset_by_n = {}
    for ns in groups.values():
        if len(ns) == 1:
            continue
        for idx, n in enumerate(ns):
            angle = 2 * math.pi * idx / len(ns)
            offset_by_n[n] = (radius * math.sin(angle), radius * math.cos(angle))

    return [
        (n, (lat + offset_by_n.get(n, (0, 0))[0], lon + offset_by_n.get(n, (0, 0))[1]))
        for n, (lat, lon) in valid
    ]


def build_route_map(points, out_path, width=1300, height=760):
    """points: ordered list of (lat, lon) or None. Draws numbered pins in
    showing order with a connecting line. Skips any stop that failed to
    geocode. Returns out_path, or None if fewer than 1 point resolved."""
    valid = [(i + 1, p) for i, p in enumerate(points) if p]
    if not valid:
        return None
    valid = _spread_coincident_points(valid)

    from staticmap import StaticMap, IconMarker, Line

    m = StaticMap(width, height, url_template="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")

    if len(valid) > 1:
        line_coords = [(lon, lat) for _, (lat, lon) in valid]
        m.add_line(Line(line_coords, (3, 43, 66, 170), 4))

    pin_paths = []
    try:
        for n, (lat, lon) in valid:
            pin_path, size = _make_pin(n)
            pin_paths.append(pin_path)
            m.add_marker(IconMarker((lon, lat), pin_path, size // 2, size + 10))
        img = m.render()
        img.save(out_path)
    except Exception:
        return None
    finally:
        for p in pin_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
    return out_path


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------

def render_cover(
    rows,
    output_path,
    showing_date="",
    client_name="",
    agent_name="Brian Elmore",
    agent_phone="",
    agent_email="brian@justinlucasgroup.com",
    print_safe_logo=False,
    map_image=None,
    prepared_date="",
):
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    template = env.get_template("cover.html")
    html_str = template.render(
        rows=rows,
        showing_date=showing_date,
        client_name=client_name,
        agent_name=agent_name,
        agent_phone=agent_phone,
        agent_email=agent_email,
        font_dir=FONT_DIR,
        logo_lockup=LOGO_LOCKUP_BW if print_safe_logo else LOGO_LOCKUP,
        map_image=map_image,
        prepared_date=prepared_date,
        footer_label=client_name or "Showing Schedule",
    )
    HTML(string=html_str, base_url=BASE_DIR).write_pdf(output_path)
    return output_path


# ---------------------------------------------------------------------------
# PDF merge
# ---------------------------------------------------------------------------

def merge_pdfs(pdf_paths, output_path):
    writer = PdfWriter()
    for p in pdf_paths:
        writer.append(p)
    with open(output_path, "wb") as f:
        writer.write(f)
    return output_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_packet(
    ordered_items,
    output_path,
    showing_date="",
    client_name="",
    agent_name="Brian Elmore",
    agent_phone="",
    agent_email="brian@justinlucasgroup.com",
    print_safe_logo=False,
    include_map=True,
    geocode_user_agent="jlg-showing-packet-app",
    prepared_date="",
):
    """ordered_items: list of {"listing": Listing, "time": str} in showing
    order. Builds the full packet PDF at output_path."""
    tmp_paths = []
    try:
        # 1. Per-listing flyer PDFs, in showing order.
        flyer_paths = []
        for item in ordered_items:
            fd, fp = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            tmp_paths.append(fp)
            render_flyer(
                item["listing"],
                fp,
                agent_phone=agent_phone,
                agent_email=agent_email,
                agent_name=agent_name,
                print_safe_logo=print_safe_logo,
            )
            flyer_paths.append(fp)

        # 2. Best-effort geocode + route map.
        map_image = None
        if include_map:
            addresses = [item["listing"].full_address for item in ordered_items]
            points = geocode_addresses(addresses, user_agent=geocode_user_agent)
            if any(points):
                fd, map_path = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                tmp_paths.append(map_path)
                map_image = build_route_map(points, map_path)

        # 3. Cover page.
        rows = []
        for i, item in enumerate(ordered_items, start=1):
            l = item["listing"]
            rows.append({
                "n": i,
                "time": (item.get("time") or "").strip(),
                "address_line1": l.address_line1 or l.full_address or "(address not found)",
                "city_state_zip": l.city_state_zip,
                "price": l.list_price,
                "beds": f"{l.bedrooms} bd" if l.bedrooms else "",
                "baths": f"{l.bathrooms_display} ba" if l.bathrooms_full else "",
                "sqft": f"{l.approx_sf} sf" if l.approx_sf else "",
            })

        fd, cover_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        tmp_paths.append(cover_path)
        render_cover(
            rows,
            cover_path,
            showing_date=showing_date,
            client_name=client_name,
            agent_name=agent_name,
            agent_phone=agent_phone,
            agent_email=agent_email,
            print_safe_logo=print_safe_logo,
            map_image=map_image,
            prepared_date=prepared_date,
        )

        # 4. Merge cover + flyers in order.
        merge_pdfs([cover_path] + flyer_paths, output_path)
        return output_path
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
