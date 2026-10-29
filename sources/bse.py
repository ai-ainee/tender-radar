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
        f"&strSearch=P&strToDate={to_date}&strType=C&subcategory=-1"
    )


def _fetch_via_cffi(api_url):
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    try:
        session = cffi_requests.Session(impersonate="chrome120")
        hdrs_html = {"User-Agent": UA,
                     "Accept": "text/html,application/xhtml+xml"}
        hdrs_json = {"User-Agent": UA,
                     "Referer": "https://www.bseindia.com/",
                     "Origin": "https://www.bseindia.com",
                     "Accept": "application/json, text/plain, */*"}
        session.get("https://www.bseindia.com/corporates/ann.html",
                    headers=hdrs_html, timeout=15)
        time.sleep(1)
        r = session.get(api_url, headers=hdrs_json, timeout=20)
        if r.status_code == 200 and r.content:
            return r.content
        log.debug(f"[bse] cffi HTTP {r.status_code}")
        return None
    except Exception as e:
        log.debug(f"[bse] cffi error: {str(e)[:120]}")
        return None


def _fetch_via_urllib(api_url):
    try:
        req = urllib.request.Request(api_url, headers={
            "User-Agent": UA,
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
            "Accept": "application/json, text/plain, */*",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(2_000_000)
    except Exception as e:
        log.debug(f"[bse] urllib error: {str(e)[:120]}")
        return None


def _try_endpoint(api_url):
    """Return parsed rows list, or None if the fetch itself failed."""
    body = _fetch_via_cffi(api_url)
    if body is None:
        body = _fetch_via_urllib(api_url)
    if body is None:
        return None

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        log.debug(f"[bse] JSON parse failed: {str(e)[:80]}")
        return None

    # BSE returns the literal JSON string "No Record Found!" when empty.
    if isinstance(data, str):
        log.debug(f"[bse] empty-response marker: {data[:60]!r}")
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
    """Return list of BSE announcements matching keywords.

    Returns [] on any failure. Never raises.
    """
    kw_lower = [str(k).lower().strip() for k in (keywords or []) if k]

    for url_builder, name in ((_build_url_primary, "primary"),
                              (_build_url_alt, "alternate")):
        api_url = url_builder(max_days)
        rows = _try_endpoint(api_url)
        if rows is None:
            continue
        if not rows:
            log.debug(f"[bse] {name}: 0 rows in {max_days}-day window")
            continue

        log.debug(f"[bse] {name}: {len(rows)} rows")
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

        if out:
            log.info(f"[bse] {len(out)} matching row(s) from {name} endpoint")
            return out

    log.info(f"[bse] no matching rows in last {max_days} day(s)")
    return []
