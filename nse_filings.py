"""nse_filings.py — NSE corporate announcements.

Same as bse_filings: no hardcoded category keywords, filtered only by Target
Products. AI evaluator classifies each filing via event_fingerprint.
"""
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

from common import log

UA = "Mozilla/5.0 (compatible; RadarScout/2.0; business-research)"


def fetch_nse_filings(target_keywords, max_results=60):
    """Fetches recent NSE corporate announcements filtered by target keywords.

    Returns a list of dicts: source, company, subject, details, date, url.
    Returns [] on error.
    """
   out = []
    try:
        # curl_cffi with cookie jar to bypass TLS fingerprint + maintain session
        try:
            from curl_cffi import requests as cffi_requests
            _use_cffi = True
        except ImportError:
            _use_cffi = False

        body_bytes = None
        if _use_cffi:
            try:
                session = cffi_requests.Session(impersonate="chrome120")
                hdrs = {
                    "User-Agent": UA,
                    "Accept-Language": "en-IN,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
                session.get("https://www.nseindia.com/", headers=hdrs, timeout=15)
                time.sleep(1)
                session.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements", headers=hdrs, timeout=15)
                time.sleep(1)
                r = session.get(
                    "https://www.nseindia.com/api/corporate-announcements?index=equities",
                    headers={**hdrs, "Accept": "application/json"},
                    timeout=20,
                )
                if r.status_code == 200:
                    body_bytes = r.content
                else:
                    log.warning(f"NSE cffi HTTP {r.status_code}")
            except Exception as e:
                log.warning(f"NSE cffi error: {e}")

        if body_bytes is None:
            # Fallback to urllib
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            opener.addheaders = [
                ("User-Agent", UA),
                ("Accept-Language", "en-IN,en;q=0.9"),
                ("Accept", "*/*"),
            ]
            opener.open("https://www.nseindia.com/", timeout=15)
            time.sleep(1)
            opener.open("https://www.nseindia.com/companies-listing/corporate-filings-announcements", timeout=15)
            time.sleep(1)
            resp = opener.open(
                "https://www.nseindia.com/api/corporate-announcements?index=equities",
                timeout=20,
            )
            body_bytes = resp.read()

        data = json.loads(body_bytes.decode("utf-8", errors="replace"))
        if not isinstance(data, list):
            log.warning("NSE: unexpected response shape (expected list)")
            return []

        kw_lower = [str(k).lower() for k in target_keywords if k]

        for item in data[:max_results]:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or item.get("desc") or "").strip()
            symbol = str(item.get("symbol") or "").strip()
            details = str(item.get("attchmntText") or "").strip()
            if not subject:
                continue
            # Filter ONLY by target keywords
            if kw_lower:
                hay = (subject + " " + details).lower()
                if not any(k in hay for k in kw_lower):
                    continue
            attach = str(item.get("attchmntFile") or "").strip()
            url = attach if (attach and attach.startswith("http")) else \
                  f"https://www.nseindia.com/get-quotes/equity?symbol={urllib.parse.quote(symbol)}"
            out.append({
                "source": "NSE",
                "company": symbol,
                "subject": subject,
                "details": details[:800],
                "date": str(item.get("an_dt") or item.get("exchdisstime") or "")[:10],
                "url": url,
            })
            if len(out) >= max_results:
                break
        time.sleep(0.3)
    except Exception as e:
        log.warning(f"NSE filings fetch failed: {e}")
    return out
