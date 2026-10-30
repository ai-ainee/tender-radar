"""sources/bse.py — BSE announcements via ScraperAPI residential proxy.

Akamai blocks all direct requests (curl_cffi, requests, bse package)
from datacenter IPs like GitHub Actions. ScraperAPI routes through
residential IPs, bypassing the block.

Cost: 1 credit per request (standard proxy mode). Requires
SCRAPERAPI_API_KEYS env var.
"""
import os
import urllib.parse
from datetime import datetime, timedelta

import requests

from common import log

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BSE_ENDPOINT = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"


def _get_scraperapi_key():
    raw = os.getenv("SCRAPERAPI_API_KEYS", "")
    keys = [k.strip() for k in raw.replace('"', "").replace("'", "").split(",") if k.strip()]
    return keys[0] if keys else None


def fetch_bse(keywords, max_days=15, max_results=60):
    """Fetch BSE announcements via ScraperAPI. Returns [] on failure."""
    key = _get_scraperapi_key()
    if not key:
        log.warning("[bse] SCRAPERAPI_API_KEYS not set — skipping")
        return []

    max_days = max(int(max_days), 15)  # BSE's 7-day window is often empty

    kw_lower = [str(k).lower().strip() for k in (keywords or []) if k]
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y%m%d")

    url = (
        f"{BSE_ENDPOINT}?pageno=1&strCat=-1&strPrevDate={from_date}"
        f"&strScrip=&strSearch=P&strToDate={to_date}&strType=C&subcategory="
    )

    proxies = {
        "http":  f"http://scraperapi:{key}@proxy-server.scraperapi.com:8001",
        "https": f"http://scraperapi:{key}@proxy-server.scraperapi.com:8001",
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/corporates/ann.html",
        "Origin": "https://www.bseindia.com",
    }

    try:
        log.info(f"[bse] fetching via ScraperAPI proxy (last {max_days}d)")
        r = requests.get(url, proxies=proxies, headers=headers, timeout=90, verify=False)
    except Exception as e:
        log.warning(f"[bse] ScraperAPI request failed: {str(e)[:200]}")
        return []

    if r.status_code != 200:
        log.warning(f"[bse] ScraperAPI HTTP {r.status_code}")
        return []

    try:
        data = r.json()
    except Exception:
        log.warning(f"[bse] non-JSON response: {r.text[:150]!r}")
        return []

    if not isinstance(data, dict) or not isinstance(data.get("Table"), list):
        log.info(f"[bse] response has no 'Table' key: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        return []

    rows = data["Table"]
    if not rows:
        log.info(f"[bse] no announcements in last {max_days} day(s)")
        return []

    log.info(f"[bse] {len(rows)} raw announcement(s) via ScraperAPI")

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

    log.info(f"[bse] {len(out)} matching row(s) after keyword filter")
    return out
