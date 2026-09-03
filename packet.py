"""
Showing packet builder.
------------------------
Takes a set of already-parsed MLS Listings plus a showing order and times,
and produces one merged, branded PDF: a cover page (ordered schedule +
route map) followed by each listing's full branded flyer, in showing order.

Geocoding tries two free services, in order, neither requiring an API key
or billing account (matches the rest of this project's "no external
service signup required" philosophy): the US Census Bureau's geocoder
first (authoritative TIGER/Line address ranges -- the more reliable of the
two for rural numbered-grid roads), then OpenStreetMap's Nominatim as a
fallback for addresses Census doesn't have (rate-limited to 1 request/
second per Nominatim's usage policy). Every result from either service is
checked against the listing's own ZIP before being accepted -- both
services' free-text matching can otherwise return a confidently-wrong
result in a neighboring county for these addresses (see `_zip_ok`).
Building the map adds a small delay per stop (worse case if a stop falls
through to Nominatim), and the whole thing fails soft -- if geocoding is
unavailable (no internet, a stop's address doesn't resolve anywhere, etc.)
the packet is still built, just without a map or without that one pin.

Both the geocoding pass and the map-tile render are outside our control --
a Nominatim rate limit (HTTP 429) or a slow/unresponsive OSM tile server
can otherwise stall a request indefinitely. `build_packet` bounds the whole
geocode+map step to a hard wall-clock budget (`_MAP_STEP_BUDGET_SECONDS`)
so a bad day from either free service degrades to "packet without a map"
instead of the request running past gunicorn's worker timeout and getting
killed outright (which happened in production once: repeated Nominatim 429s
plus tile fetching pushed a 5-stop packet past the timeout and the whole
packet failed instead of just losing its map).
"""
import io
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

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
JLG_BLOCK = os.path.join(STATIC_DIR, "logo", "JLG-COMBO-BLUE.png")
BROKERAGE_LOCKUP = os.path.join(STATIC_DIR, "logo", "at-properties-christies-color.png")
BROKERAGE_LOCKUP_BW = os.path.join(STATIC_DIR, "logo", "at-properties-christies-blackonly.png")

# Hard wall-clock ceiling for "geocode all stops + render the route map" in
# `build_packet`, in seconds. Well under gunicorn's 120s worker timeout
# (see Dockerfile) so a slow/rate-limited external service always leaves
# enough room to still build and return the rest of the packet without a
# map, rather than the whole request getting killed. See the module
# docstring and the comment at its call site for the incident that prompted
# this.
_MAP_STEP_BUDGET_SECONDS = 45
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


def _city_level(address):
    """Drop the street line, keeping just "City, ST ZIP" -- a true last-
    resort fallback for addresses on rural/newly-built roads that
    Nominatim's free OSM data doesn't have street-level coverage for at
    all under any query we can construct. A city-center pin is still far
    more useful on a multi-stop showing route map than a stop silently
    vanishing from it, and the map's own caption already caveats the
    route as approximate -- but see `_county_level` below, which is tried
    first and is usually able to avoid needing this at all."""
    parts = (address or "").split(",")
    if len(parts) < 2:
        return None
    return ",".join(p.strip() for p in parts[1:]).strip() or None


_ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?\s*$")


def _expected_zip(address):
    """Pull the 5-digit ZIP off the end of an "..., ST ZIP" address string,
    so a geocode result can be sanity-checked against it (see
    `_zip_matches`). Every candidate query built below (full address,
    county-swap, city-level) keeps the original ZIP in the tail, so this is
    computed once per stop and reused across all of that stop's candidates.
    Anchored to the end of the last comma-separated segment rather than a
    bare "first 5-digit number found" search -- a 5-digit house number
    (rare, but not impossible) would otherwise be misread as the ZIP."""
    if not address:
        return None
    last_part = address.split(",")[-1]
    m = _ZIP_RE.search(last_part.strip())
    return m.group(1) if m else None


def _zip_ok(postcode, expected_zip):
    """Nominatim's free-text search is not a hard database lookup -- it's a
    fuzzy match over whatever OSM data exists, and for rural Michigan
    numbered-grid roads ("68th Street", "116th Avenue", etc.) that same
    road name and house number routinely exists in more than one county.
    Observed directly on a real showing packet: "2313 68th Street,
    Allegan County, MI 49408" -- county named explicitly in the query --
    still matched a completely different "2313 68th Street" 30+ miles
    south in Van Buren County (ZIP 49090), because Nominatim treats the
    county/ZIP text as ranking hints, not a strict filter, and there was
    apparently no better-covered OSM data for the correct Allegan County
    address. A pin that's precise-looking but in the wrong county is worse
    than an honest, coarser city-center pin, so every match (from either
    geocoding provider -- see `_census_geocode_one` below) is checked
    against the ZIP we already know is correct (MLS-supplied) before it's
    accepted -- a mismatch is treated the same as no match at all, which
    lets the candidate loop fall through to the next (coarser) query
    instead of confidently plotting the wrong location."""
    if not expected_zip:
        return True
    if not postcode:
        # No postcode on the result to check -- don't reject a match we
        # can't actually verify, that would throw away otherwise-good hits.
        return True
    return postcode[:5] == expected_zip


def _nominatim_postcode(loc):
    return (loc.raw or {}).get("address", {}).get("postcode") if getattr(loc, "raw", None) else None


def _census_geocode_one(address, timeout=6):
    """Look up a single address against the US Census Bureau's free
    geocoder (TIGER/Line-based, no API key or billing account required --
    same "no signup" bar as Nominatim). Tried *before* Nominatim for every
    stop: it resolved every rural numbered-grid address in the real-world
    case that exposed this whole geocoding problem (Nominatim returned zero
    results at all for 3 of 5 stops on that showing route), because it's
    matching against the Census's own authoritative address-range dataset
    rather than fuzzy-searching community-contributed OSM text. It isn't a
    strict superset of Nominatim's coverage, though -- one stop on that same
    real route (a Douglas, MI address) had no Census match at all but
    geocoded correctly on Nominatim's very first try -- so this is a first
    attempt, not a replacement; `geocode_addresses` still falls through to
    the full Nominatim candidate chain if this returns nothing (or fails
    zip validation). Returns (lat, lon, postcode) or None; every failure
    mode (network, timeout, malformed response, no match) is swallowed so a
    single stop's lookup can't abort the whole route's geocoding."""
    import json
    import urllib.parse
    import urllib.request

    try:
        params = {"address": address, "benchmark": "Public_AR_Current", "format": "json"}
        url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "jlg-showing-packet-app"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        matches = data.get("result", {}).get("addressMatches") or []
        if not matches:
            return None
        m = matches[0]
        coords = m.get("coordinates") or {}
        lat, lon = coords.get("y"), coords.get("x")
        if lat is None or lon is None:
            return None
        postcode = (m.get("addressComponents") or {}).get("zip")
        return (float(lat), float(lon), postcode)
    except Exception:
        return None


def _county_level(address, county):
    """Swap the mailing city for the county, keeping the full street
    address -- e.g. "6456 104th Avenue, South Haven, MI 49090" with
    county="Allegan" becomes "6456 104th Avenue, Allegan County, MI
    49090". Rural Michigan/Illinois addresses are routinely mailed under
    the nearest small town's name for postal purposes even though the
    parcel itself sits in a different township outside that town's
    limits (observed directly on a real listing: "South Haven" is the
    mailing city, but the property is actually in Casco Township,
    Allegan County -- a separate place, miles from South Haven's own
    town center). Nominatim's free-text search fails outright on the
    mailing-city version of addresses like this because it's genuinely
    looking in the wrong place, but the *county* is accurate (it's an
    MLS-supplied field, not a mailing convenience) and resolving the
    same house number against the county instead routinely finds an
    exact street-level match. Tried before `_city_level` because it's
    still a real address match, not an area-centroid guess."""
    if not county or not (county or "").strip():
        return None
    parts = (address or "").split(",")
    if len(parts) < 3:
        return None
    street = parts[0].strip()
    state_zip = parts[-1].strip()
    if not street or not state_zip:
        return None
    county_str = county.strip()
    if not county_str.lower().endswith("county"):
        county_str += " County"
    return f"{street}, {county_str}, {state_zip}"


def geocode_addresses(addresses, counties=None, user_agent="jlg-showing-packet-app"):
    """Best-effort geocode a list of full address strings (street + city/
    state/zip) to (lat, lon). `counties`, if given, is a parallel list of
    each listing's MLS-supplied county name (or "" / None), used to build
    a more accurate fallback query than the bare city-centroid one when
    the mailing city and the county aren't the same place (see
    `_county_level`). Returns a list the same length as `addresses`, with
    None in place of any address that failed to resolve or if geocoding
    is unavailable at all (e.g. no internet) -- callers should treat
    every entry as optional."""
    results = [None] * len(addresses)
    counties = counties or [None] * len(addresses)
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
        # max_retries/error_wait are intentionally light: a 429 (rate limited)
        # response from Nominatim almost never clears within the same
        # request's lifetime, so retrying it hard just burns wall-clock time
        # that counts against the outer deadline below without improving the
        # odds of success. One quick retry is enough to absorb a genuine
        # transient blip (a dropped connection, a slow DNS lookup) without
        # turning a real rate-limit into a multi-second stall per candidate.
        geolocator = Nominatim(user_agent=user_agent, timeout=8)
        geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1, max_retries=1, error_wait_seconds=1.0, swallow_exceptions=True)

        # Hard deadline for the whole batch (all addresses, both providers,
        # every fallback candidate). Once we're past it we stop attempting
        # further Nominatim fallbacks and just return whatever resolved so
        # far -- a partial map (or none) beats stalling the request. This is
        # deliberately generous relative to `_MAP_STEP_BUDGET_SECONDS` in
        # `build_packet`, which is the real backstop; this one just avoids
        # spending the *entire* budget on geocoding and leaving nothing for
        # the actual tile render.
        deadline = time.monotonic() + 25

        for i, addr in enumerate(addresses):
            if not addr:
                continue
            if time.monotonic() > deadline:
                break
            expected_zip = _expected_zip(addr)

            # Try the US Census geocoder first (see _census_geocode_one) --
            # it's the more reliable of the two for these addresses, but
            # doesn't cover everything Nominatim does, so this is a first
            # attempt rather than a replacement for the chain below.
            stripped = _strip_unit(addr)
            census_hit = False
            for census_candidate in ([addr, stripped] if stripped != addr else [addr]):
                census = _census_geocode_one(census_candidate)
                if census:
                    lat, lon, postcode = census
                    if _zip_ok(postcode, expected_zip):
                        results[i] = (lat, lon)
                        census_hit = True
                        break
            if census_hit:
                continue

            if time.monotonic() > deadline:
                break

            candidates = [addr]
            if stripped != addr:
                candidates.append(stripped)
            county_level = _county_level(stripped, counties[i] if i < len(counties) else None)
            if county_level and county_level not in candidates:
                candidates.append(county_level)
            city_level = _city_level(addr)
            if city_level:
                candidates.append(city_level)
            for candidate in candidates:
                if time.monotonic() > deadline:
                    break
                try:
                    loc = geocode(candidate, addressdetails=True)
                except Exception:
                    loc = None
                if loc and _zip_ok(_nominatim_postcode(loc), expected_zip):
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

def cover_density(n_rows):
    """A 2-3 stop showing day and a 12-stop broker open-house tour need
    very different type scales to both fit the schedule *and* the route
    map on one page -- rather than let the map spill onto its own mostly-
    empty second page as the stop count grows, the schedule rows (and,
    via `map_height_for` below, the map image itself) shrink a notch at a
    time to make room. Empirically tuned against real multi-stop packets
    rather than computed from exact CSS box math."""
    if n_rows <= 6:
        return ""
    if n_rows <= 9:
        return "compact"
    if n_rows <= 13:
        return "tight"
    return "very-tight"


def map_height_for(n_rows, base=760):
    """Matching pixel height for the route map PNG at each density tier --
    the map is a fixed-aspect-ratio image, so CSS alone can shrink its
    width but not its height without cropping it; the actual render has
    to ask for a shorter image up front to reclaim vertical space."""
    density = cover_density(n_rows)
    return {"": base, "compact": 560, "tight": 420, "very-tight": 320}[density]


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
        logo_jlg=JLG_BLOCK,
        logo_brokerage=BROKERAGE_LOCKUP_BW if print_safe_logo else BROKERAGE_LOCKUP,
        map_image=map_image,
        prepared_date=prepared_date,
        footer_label=client_name or "Showing Schedule",
        density=cover_density(len(rows)),
    )
    HTML(string=html_str, base_url=BASE_DIR).write_pdf(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Notes page (end of packet)
# ---------------------------------------------------------------------------

def render_notes(
    rows,
    output_path,
    showing_date="",
    client_name="",
    agent_name="Brian Elmore",
    print_safe_logo=False,
    prepared_date="",
):
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    template = env.get_template("notes.html")
    html_str = template.render(
        rows=rows,
        showing_date=showing_date,
        client_name=client_name,
        agent_name=agent_name,
        font_dir=FONT_DIR,
        logo_jlg=JLG_BLOCK,
        logo_brokerage=BROKERAGE_LOCKUP_BW if print_safe_logo else BROKERAGE_LOCKUP,
        prepared_date=prepared_date,
        footer_label=client_name or "Showing Notes",
    )
    HTML(string=html_str, base_url=BASE_DIR).write_pdf(output_path)
    return output_path


# ---------------------------------------------------------------------------
# PDF merge
# ---------------------------------------------------------------------------

def _blank_page_pdf(output_path, width=612, height=792):
    """A single blank Letter-size page -- inserted right after the cover so
    that when the packet is printed double-sided, every listing's flyer
    still starts on a fresh sheet (recto) instead of drifting onto the back
    of whatever page happened to precede it. Built directly with pypdf
    rather than rendered through WeasyPrint since there's no content on it
    at all -- just an empty page of the same size as the rest of the
    packet."""
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    with open(output_path, "wb") as f:
        writer.write(f)
    return output_path


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

        # 2. Best-effort geocode + route map. Shorter for a longer stop
        # list, so schedule + map keep fitting on one cover page together
        # (see cover_density/map_height_for).
        #
        # Run geocoding + tile rendering on a hard wall-clock budget. Both
        # steps depend on free third-party services (Nominatim, the Census
        # geocoder, OSM tile servers) that are outside our control -- a
        # rate limit or a slow tile server can otherwise stall this step
        # for minutes, well past gunicorn's worker timeout, which kills the
        # whole request (and the whole packet, map or no map) rather than
        # just losing the map. `geocode_addresses` has its own internal
        # 25s deadline, but the tile render (`build_route_map`) doesn't --
        # this outer timeout is the actual backstop. If it trips, we
        # proceed without a map exactly as if geocoding had failed outright.
        map_image = None
        if include_map:
            addresses = [item["listing"].full_address for item in ordered_items]
            counties = [item["listing"].county for item in ordered_items]
            fd, map_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            tmp_paths.append(map_path)

            def _geocode_and_map():
                points = geocode_addresses(addresses, counties=counties, user_agent=geocode_user_agent)
                if not any(points):
                    return None
                return build_route_map(
                    points, map_path, height=map_height_for(len(ordered_items))
                )

            # Deliberately NOT a `with ThreadPoolExecutor(...) as pool:` --
            # the context manager's __exit__ calls shutdown(wait=True),
            # which blocks until the submitted task finishes even if we've
            # already given up on it via future.result(timeout=...). That
            # would silently defeat this entire timeout (verified directly:
            # a stuck tile fetch made this hang indefinitely with the
            # `with` form). Calling shutdown(wait=False) below lets us
            # return immediately; the stray thread is abandoned to finish
            # or die on its own, which is an acceptable trade for a
            # low-traffic internal tool where this should be a rare event.
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(_geocode_and_map)
            try:
                map_image = future.result(timeout=_MAP_STEP_BUDGET_SECONDS)
            except FutureTimeoutError:
                map_image = None
            except Exception:
                map_image = None
            finally:
                pool.shutdown(wait=False)

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

        # 4. Blank page right after the cover, for double-sided printing
        # alignment (see _blank_page_pdf).
        fd, blank_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        tmp_paths.append(blank_path)
        _blank_page_pdf(blank_path)

        # 5. Notes page at the very end -- one small section per stop.
        fd, notes_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        tmp_paths.append(notes_path)
        render_notes(
            rows,
            notes_path,
            showing_date=showing_date,
            client_name=client_name,
            agent_name=agent_name,
            print_safe_logo=print_safe_logo,
            prepared_date=prepared_date,
        )

        # 6. Merge cover + blank + flyers + notes, in order.
        merge_pdfs([cover_path, blank_path] + flyer_paths + [notes_path], output_path)
        return output_path
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
