"""
MLS source router.

The app now understands listing sheets from three different sources --
MRED (Illinois), MichRIC (Michigan), and the newer cross-MLS "Home
Platform" print sheet Brian's brokerage is transitioning to -- which are
three completely different physical documents, so each gets its own
parser module (parser.py / parser_michric.py / parser_platform.py)
rather than one parser trying to branch on all three layouts internally.
This module is the single place that decides which one a given upload
actually is and dispatches to it, so app.py doesn't need to know or ask
-- same `parse_listing_pdf(file_bytes, source_filename)` call site
either way, same shared `Listing` result.

Detection is a cheap text signature check (each source's own copyright/
disclaimer line names itself, e.g. "...Copyright 2026 MichRIC(R), LLC..."
or "...Compass International Holdings..." for the new platform) rather
than anything more elaborate -- all three are consistently present on
every real export seen from their respective source. Defaults to the
MRED parser when none of the signatures are found, since that's the
original/primary format this app was built for.
"""
import hashlib
import io
import os
import pickle
import tempfile
import time

import pdfplumber

try:  # parse each upload's PDF once, not once per parser
    import hp_core as _hp_core
    _hp_core.install_pdf_cache()
except Exception:  # pragma: no cover - optimization only
    pass

from parser import parse_listing_pdf as _parse_mred
from parser_michric import parse_listing_pdf as _parse_michric, is_michric
from parser_platform import parse_listing_pdf as _parse_platform, is_new_platform
from hp_adapter import attach_hp


def _sniff_text(file_bytes: bytes) -> str:
    # MichRIC's own "Copyright ... MichRIC(R), LLC" signature sits in the
    # compliance footer on page 2 (the listing data itself is entirely on
    # page 1), so this has to check more than just the first page or every
    # MichRIC upload would silently fall through to the MRED parser. The
    # new Home Platform sheet's "Compass International Holdings" signature
    # is on every page (it's in each page's own disclaimer block), so it
    # doesn't strictly need this, but scanning the same 2 pages costs
    # nothing extra and keeps this one shared sniff for all three checks.
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages = pdf.pages[:2]
            return "\n".join((p.extract_text() or "") for p in pages)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Parsed-listing cache
# ---------------------------------------------------------------------------
# The upload page calls /preview (parses every listing to show the list) and
# then /generate (parses them all again to build the packet). Parsing a
# Chicago Home Platform listing takes seconds even on a laptop and far longer
# on Render's free tier, so the second pass is skipped by keeping each parsed
# listing on disk, keyed by the listing PDF's content. A disk cache (not an
# in-memory one) because gunicorn runs two workers and the two requests can
# land on different ones. The key includes a fingerprint of the code files, so
# a redeploy or local edit never serves a listing parsed by older code.

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "jlg_listing_cache")
_CACHE_MAX_FILES = 60
_CACHE_MAX_AGE = 3 * 3600


def _code_fingerprint():
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.md5()
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            try:
                st = os.stat(os.path.join(here, name))
                h.update(f"{name}:{st.st_size}:{int(st.st_mtime)}".encode())
            except OSError:
                pass
    return h.hexdigest()[:10]


_CODE_FP = _code_fingerprint()


def _cache_path(file_bytes):
    return os.path.join(_CACHE_DIR, hashlib.md5(file_bytes).hexdigest() + "-" + _CODE_FP + ".pkl")


def _cache_get(file_bytes):
    try:
        with open(_cache_path(file_bytes), "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def _cache_put(file_bytes, listing):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _cache_path(file_bytes)
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(listing, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
        entries = []
        for name in os.listdir(_CACHE_DIR):
            full = os.path.join(_CACHE_DIR, name)
            try:
                entries.append((os.path.getmtime(full), full))
            except OSError:
                pass
        entries.sort(reverse=True)
        now = time.time()
        for i, (mtime, full) in enumerate(entries):
            if i >= _CACHE_MAX_FILES or now - mtime > _CACHE_MAX_AGE:
                try:
                    os.unlink(full)
                except OSError:
                    pass
    except Exception:
        pass  # caching is only an optimization


def _parse_uncached(file_bytes: bytes, source_filename: str = ""):
    text = _sniff_text(file_bytes)
    if is_new_platform(text):
        return _parse_platform(file_bytes, source_filename)
    # MichRIC and MRED sheets are converted to the same record Home Platform
    # sheets produce (hp_adapter), so every source renders through the one
    # card-style flyer.
    if is_michric(text):
        listing = _parse_michric(file_bytes, source_filename)
    else:
        listing = _parse_mred(file_bytes, source_filename)
    return attach_hp(listing)


def parse_listing_pdf(file_bytes: bytes, source_filename: str = ""):
    cached = _cache_get(file_bytes)
    if cached is not None:
        if hasattr(cached, "source_filename"):
            cached.source_filename = source_filename
        return cached
    listing = _parse_uncached(file_bytes, source_filename)
    _cache_put(file_bytes, listing)
    return listing
