"""sources/bse.py — BSE corporate announcements via ScraperAPI proxy.

BSE's Akamai protection blocks datacenter IPs (like GitHub Actions).
This module routes all requests through ScraperAPI's proxy network,
which rotates residential IPs and manages headers automatically,
achieving a high success rate against Akamai.

Returns list of {url, title, snippet, company, date}.
"""
import urllib.parse
from datetime import datetime, timedelta

import requests

from common import log

# ScraperAPI proxy endpoint
SCRAPERAPI_PROXY = "http://scraperapi:{}@proxy-server.scraperapi.com:8001"
# The correct BSE API endpoint for announcements
BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _get_scraperapi_key():
    """Retrieve the first ScraperAPI key from environment variables."""
    import os
    raw = os.getenv("SCRAPERAPI_API_KEYS", "")
    keys = [k.strip() for k in raw.replace('"', "").replace("'", "").split(",") if k.strip()]
    return keys[0] if keys else None


def fetch_bse(keywords, max_days=7, max_results=60):
    """Fetch BSE announcements using ScraperAPI proxy.

    Returns [] on any failure. Never raises.
    """
    api_key = _get_scraperapi_key()
    if not api_key:
        log.warning("[bse] SCRAPERAPI_API_KEYS not set — skipping BSE")
        return []

    kw_lower = [str(k).lower().strip() for k in (keywords or []) if k]

    # Build the correct BSE API URL with a wider date range
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y%m%d")
    
    api_url = (
        f"{BSE_API_URL}?pageno=1&strCat=-1&strPrevDate={from_date}"
        f"&strScrip=&strSearch=P&strToDate={to_date}&strType=C&subcategory="
    )

    # Configure the proxy for requests
    proxies = {
        "http": SCRAPERAPI_PROXY.format(api_key),
        "https": SCRAPERAPI_PROXY.format(api_key),
    }

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/corporates/ann.html",
        "Origin": "https://www.bseindia.com",
    }

    rows = []
    try:
        log.info(f"[bse] fetching via ScraperAPI proxy (last {max_days} days)")
        response = requests.get(
            api_url,
            proxies=proxies,
            headers=headers,
            timeout=60,
            verify=False,  # ScraperAPI proxy handles SSL verification
        )
        
        if response.status_code != 200:
            log.warning(f"[bse] ScraperAPI proxy returned HTTP {response.status_code}")
            return []

        data = response.json()
        
        # The API returns a dict with a 'Table' key
        if isinstance(data, dict) and isinstance(data.get("Table"), list):
            rows = data["Table"]
        else:
            log.info(f"[bse] API response has no 'Table' key: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
            return []

    except requests.RequestException as e:
        log.warning(f"[bse] ScraperAPI request failed: {str(e)[:200]}")
        return []
    except Exception as e:
        log.warning(f"[bse] Unexpected error: {str(e)[:200]}")
        return []

    if not rows:
        log.info(f"[bse] no announcements returned for last {max_days} days")
        return []

    log.info(f"[bse] {len(rows)} raw announcement(s) from BSE API")

    # Filter by keywords and format the output
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
