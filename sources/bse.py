"""sources/bse.py — BSE corporate announcements via curl_cffi.

Fixes two problems from the legacy bse_filings.py:
  1. Widened window from 3 → 7 days (3 days catches 0 rows across sectors
     more often than not).
  2. Correctly handles the literal string "No Record Found!" that BSE
     returns as a valid JSON string (json.loads accepts it as a Python str,
     which then gets iterated character-by-character by the old code).
  3. Falls back to the AnnSubCategoryGetData endpoint if AnnGetData
     returns empty.

Returns list of {url, title, snippet, company, date}.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from common import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _build_url_primary(max_days):
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y%m%d")
    return (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P"
        f"&strToDate={to_date}&strType=C&subcategory=-1"
    )


def _build_url_alt(max_days):
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y%m%d")
    return (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip="
        f"&strSearch=P&strToDate={to_date}&strType=C&subcategory="
    )


def _fetch_via_cffi(api_url):
    """Fetch BSE announcements using the bse package, which handles
    session cookies and headers correctly."""
    try:
        from bse import BSE
        from datetime import datetime, timedelta
    except ImportError:
        return None

    try:
        # The bse package needs a download folder for cookie storage
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with BSE(download_folder=tmpdir) as bse:
                # Reconstruct the date range from the api_url
                # (or better, accept from_date/to_date as arguments)
                to_date = datetime.now()
                from_date = to_date - timedelta(days=7)
                # The bse package returns a dict with a 'Table' key
                data = bse.announcements(
                    from_date=from_date,
                    to_date=to_date,
                    category="-1",
                    subcategory="",
                    scripcode="",
                )
                if data and isinstance(data, dict) and "Table" in data:
                    import json
                    return json.dumps(data).encode("utf-8")
                return None
    except Exception as e:
        log.warning(f"[bse] bse package error: {str(e)[:150]}")
        return None

def _try_endpoint(api_url):
    """Fetch and parse BSE announcements using the bse package."""
    body = _fetch_via_cffi(api_url)
    if body is None:
        return None

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        log.debug(f"[bse] JSON parse failed: {str(e)[:80]}")
        return None

    # The bse package returns a dict with a 'Table' key
    if isinstance(data, dict) and isinstance(data.get("Table"), list):
        return data["Table"]
    return []
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Table", "Data", "data", "Announcements",
                    "announcements", "records", "result", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    log.debug(f"[bse] unexpected type {type(data).__name__}")
    return None

def fetch_bse(keywords, max_days=7, max_results=60):
    """Fetch BSE announcements using the bse package."""
    kw_lower = [str(k).lower().strip() for k in (keywords or []) if k]

    rows = _try_endpoint(None)  # api_url no longer needed
    if rows is None:
        return []

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
            pdf_url = ("https://www.bseindia.com/xml-data/corpfiling/"
                       "AttachLive/" + urllib.parse.quote(attachment))
        else:
            pdf_url = ("https://www.bseindia.com/corporates/ann.html"
                       "?scrip=" + urllib.parse.quote(scrip))
        out.append({
            "url": pdf_url,
            "title": subject[:300],
            "snippet": details[:800],
            "company": scrip[:200],
            "date": str(item.get("NEWS_DT") or "")[:10],
        })
        if len(out) >= max_results:
            break

    log.info(f"[bse] {len(out)} matching row(s) from bse package")
    return out

    log.info(f"[bse] no matching rows in last {max_days} day(s)")
    return []
