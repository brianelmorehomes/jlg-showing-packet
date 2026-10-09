"""Google Maps Platform helpers: geocoding, driving time, and static map images.

Everything here is behind one switch: google_enabled() is True only when
USE_GOOGLE_MAPS is "true" AND GOOGLE_MAPS_KEY is set. With the switch off,
the app behaves exactly as it did before this module existed (Mapbox or
Nominatim geocoding, Mapbox drive times, OpenStreetMap route map).

Why the map moves to Google too: Google's Maps Service Terms don't allow
Geocoding API results to be shown on a non-Google map, and they limit how
long latitude/longitude may be cached (30 days). So when Google geocodes a
stop, the packet map for that tour is also a Google Static Map, and each
Google-geocoded stop records when it was geocoded (Stop.geocoded_at) so
app.py can refresh it before it ages out.

Every function fails soft (returns None) so a Google outage, a missing API
or a denied key degrades to the old behaviour instead of breaking a packet.
Error handling never prints the request URL, because it contains the key.
"""
from __future__ import annotations

import math
import os

import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Google allows caching lat/lng for 30 consecutive days. Refresh well before.
COORD_MAX_AGE_DAYS = 25

_TIMEOUT = 8


def _key() -> str:
    return os.environ.get("GOOGLE_MAPS_KEY", "").strip()


def google_enabled() -> bool:
    return os.environ.get("USE_GOOGLE_MAPS", "").strip().lower() == "true" and bool(_key())


def _log(what: str, exc: Exception | None = None, status: int | None = None):
    detail = f" status={status}" if status is not None else ""
    if exc is not None:
        detail += f" error={type(exc).__name__}"
    print(f"google_maps: {what} failed{detail}")


def geocode(address: str) -> tuple[float, float] | None:
    """Address -> (lat, lng) via the Geocoding API, or None."""
    if not address or not google_enabled():
        return None
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"address": address, "components": "country:US", "key": _key()},
            timeout=_TIMEOUT,
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("status") != "OK":
            _log(f"geocode (api status {data.get('status')})", status=resp.status_code)
            return None
        results = data.get("results") or []
        if not results:
            return None
        loc = results[0]["geometry"]["location"]
        return (float(loc["lat"]), float(loc["lng"]))
    except Exception as exc:
        _log("geocode", exc)
        return None


def drive_time(a: tuple[float, float], b: tuple[float, float]) -> dict | None:
    """Driving time/distance between two points via the Routes API, or None
    (including when the Routes API isn't enabled for the key)."""
    if not a or not b or not google_enabled():
        return None
    body = {
        "origin": {"location": {"latLng": {"latitude": a[0], "longitude": a[1]}}},
        "destination": {"location": {"latLng": {"latitude": b[0], "longitude": b[1]}}},
        "travelMode": "DRIVE",
    }
    try:
        resp = requests.post(
            ROUTES_URL,
            json=body,
            headers={
                "X-Goog-Api-Key": _key(),
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            _log("drive_time", status=resp.status_code)
            return None
        routes = resp.json().get("routes") or []
        if not routes:
            return None
        route = routes[0]
        seconds = float(str(route["duration"]).rstrip("s"))
        return {
            "minutes": round(seconds / 60, 1),
            "miles": round(route.get("distanceMeters", 0) / 1609.34, 1),
            "method": "google",
        }
    except Exception as exc:
        _log("drive_time", exc)
        return None


# ---------------------------------------------------------------- static map

TILE = 256
MAX_ZOOM = 16
MIN_ZOOM = 3


def world_px(lat: float, lng: float, zoom: float) -> tuple[float, float]:
    """Web Mercator world pixel (256px tiles) for lat/lng at a zoom level."""
    scale = TILE * (2 ** zoom)
    x = (lng + 180.0) / 360.0 * scale
    s = math.sin(math.radians(max(min(lat, 85.0511), -85.0511)))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def world_px_to_latlng(x: float, y: float, zoom: float) -> tuple[float, float]:
    scale = TILE * (2 ** zoom)
    lng = x / scale * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lng


def fit_view(points, width, height, pad_x=44, pad_top=34, pad_bottom=40):
    """Choose (center_lat, center_lng, zoom) so every (lat, lng) in `points`
    fits inside a width x height (logical px) map with room for pins and
    for Google's logo/attribution along the bottom edge."""
    avail_w = max(width - 2 * pad_x, 30)
    avail_h = max(height - pad_top - pad_bottom, 20)
    xs, ys = zip(*(world_px(lat, lng, 0) for lat, lng in points))
    span_x = max(max(xs) - min(xs), 1e-6)
    span_y = max(max(ys) - min(ys), 1e-6)
    zoom = MAX_ZOOM - 1 if len(points) == 1 else MAX_ZOOM
    if len(points) > 1:
        zoom = min(math.log2(avail_w / span_x), math.log2(avail_h / span_y))
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, math.floor(zoom)))
    cx = (max(xs) + min(xs)) / 2
    # pins sit above their point and the bottom edge carries Google's logo,
    # so shift the view slightly so the content centres in the free area
    cy = (max(ys) + min(ys)) / 2 + (pad_bottom - pad_top) / 2 / (2 ** zoom)
    lat, lng = world_px_to_latlng(cx, cy, 0)
    return lat, lng, int(zoom)


def fetch_static_map(center, zoom, width, height, scale=2) -> bytes | None:
    """PNG bytes of a Google Static Map (width/height are logical px;
    the returned image is width*scale x height*scale). None on failure."""
    if not google_enabled():
        return None
    width = min(int(width), 640)
    height = min(int(height), 640)
    try:
        resp = requests.get(
            STATIC_MAP_URL,
            params={
                "center": f"{center[0]:.6f},{center[1]:.6f}",
                "zoom": int(zoom),
                "size": f"{width}x{height}",
                "scale": scale,
                "maptype": "roadmap",
                "format": "png",
                "key": _key(),
            },
            timeout=_TIMEOUT + 4,
        )
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code != 200 or not ctype.startswith("image/"):
            _log("static map", status=resp.status_code)
            return None
        return resp.content
    except Exception as exc:
        _log("static map", exc)
        return None
