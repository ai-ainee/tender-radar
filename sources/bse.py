"""sources/bse.py — BSE corporate announcements via the bse package.

The raw BSE API is protected by Akamai, which blocks plain requests and
even curl_cffi from datacenter IPs (403 Access Denied). The `bse` package
(https://github.com/BennyThadikaran/BseIndiaApi) handles session cookies,
correct headers, and the correct endpoint, making it reliable from GitHub
Actions.

Returns list of {url, title, snippet, company, date}.
"""
import urllib.parse
from datetime import datetime, timedelta

from common import log

try:
    from bse import BSE
    _HAS_BSE_PKG = True
except ImportError:
    _HAS_BSE_PKG = False


def fetch_bse(keywords, max_days=7, max_results=60):
    """Fetch BSE announcements using the bse package.

    Returns [] on any failure. Never raises.
    """
    if not _HAS_BSE_PKG:
        log.warning("[bse] bse package not installed — skipping (pip install bse)")
        return []

    kw_lower = [str(k).lower().strip() for k in (keywords or []) if k]
    from_date = datetime.now() - timedelta(days=max_days)
    to_date = datetime.now()

    rows = None
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with BSE(download_folder=tmpdir) as bse:
                # Fetch all announcements in the date range.
                # Omit category/subcategory to get everything; keyword
                # filtering happens below in Python, which is faster and
                # more predictable than BSE's server-side subject search.
                data = bse.announcements(
                    from_date=from_date,
                    to_date=to_date,
                )
                if isinstance(data, dict) and isinstance(data.get("Table"), list):
                    rows = data["Table"]
                else:
                    log.info("[bse] bse.announcements returned no Table")
                    return []
    except Exception as e:
        log.warning(f"[bse] bse package error: {str(e)[:200]}")
        return []

    if not rows:
        log.info(f"[bse] no announcements in last {max_days} day(s)")
        return []

    log.info(f"[bse] {len(rows)} raw announcement(s) from bse package")

    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue

        subject = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()
        details = str(item.get("MORE") or "").strip()
        scrip = str(item.get("SLONGNAME") or item.get("Scripname") or "").strip()

        if not subject:
            continue

        if kw_lower:
            hay = (subject + " " + details + " " + scrip).lower()
            if not any(k in hay for k in kw_lower):
                continue

        attachment = str(item.get("ATTACHMENTNAME") or "").strip()
        if attachment:
            pdf_url = (
                "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
                + urllib.parse.quote(attachment)
            )
        else:
            pdf_url = (
                "https://www.bseindia.com/corporates/ann.html?scrip="
                + urllib.parse.quote(scrip)
            )

        out.append({
            "url": pdf_url,
            "title": subject[:300],
            "snippet": details[:800],
            "company": scrip[:200],
            "date": str(item.get("NEWS_DT") or "")[:10],
        })

        if len(out) >= max_results:
            break

    log.info(f"[bse] {len(out)} matching row(s) after keyword filter")
    return out
