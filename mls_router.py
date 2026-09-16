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
import io

import pdfplumber

from parser import parse_listing_pdf as _parse_mred
from parser_michric import parse_listing_pdf as _parse_michric, is_michric
from parser_platform import parse_listing_pdf as _parse_platform, is_new_platform


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


def parse_listing_pdf(file_bytes: bytes, source_filename: str = ""):
    text = _sniff_text(file_bytes)
    if is_new_platform(text):
        return _parse_platform(file_bytes, source_filename)
    if is_michric(text):
        return _parse_michric(file_bytes, source_filename)
    return _parse_mred(file_bytes, source_filename)
