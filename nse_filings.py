"""nse_filings.py — NSE corporate announcements.

Same purpose as BSE filings, different API. NSE requires a valid session
cookie; we warm one by hitting the homepage first. Shares the category
classifier with bse_filings to keep the pipeline consistent.
"""
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

from common import log
from bse_filings import _classify, _is_relevant  # shared classifier

UA = "Mozilla/5.0 (compatible; RadarScout/2.0; business-research)"


def fetch_nse_filings(target_keywords, max_results=60):
    """Fetches recent NSE corporate announcements filtered by keyword + category.

    Returns a list of dicts with keys:
      source, company, subject, details, category, date, url
    Returns [] on error (never raises).
    """
    out = []
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "en-IN,en;q=0.9"),
            ("Accept", "*/*"),
        ]
        # Warm the session — NSE blocks API calls without valid cookies
        opener.open("https://www.nseindia.com/", timeout=15)
        time.sleep(1)
        opener.open("https://www.nseindia.com/companies-listing/corporate-filings-announcements", timeout=15)
        time.sleep(1)
        resp = opener.open(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            timeout=20,
        )
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(data, list):
            log.warning(f"NSE: unexpected response shape (expected list)")
            return []

        for item in data[:max_results]:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or item.get("desc") or "").strip()
            symbol  = str(item.get("symbol") or "").strip()
            details = str(item.get("attchmntText") or "").strip()
            if not subject:
                continue
            category = _classify(subject, details)
            if not category:
                continue
            if not _is_relevant(subject, details, symbol, target_keywords):
                continue
            attach = str(item.get("attchmntFile") or "").strip()
            if attach and attach.startswith("http"):
                url = attach
            else:
                url = f"https://www.nseindia.com/get-quotes/equity?symbol={urllib.parse.quote(symbol)}"
            out.append({
                "source": "NSE",
                "company": symbol,
                "subject": subject,
                "details": details[:800],
                "category": category,
                "date": str(item.get("an_dt") or item.get("exchdisstime") or "")[:10],
                "url": url,
            })
            if len(out) >= max_results:
                break
        time.sleep(0.3)
    except Exception as e:
        log.warning(f"NSE filings fetch failed: {e}")
    return out
