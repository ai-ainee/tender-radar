"""nse_filings.py — NSE corporate announcements.

Uses curl_cffi (Chrome TLS impersonation) with a session warm-up to bypass
the WAF that blocks urllib from datacenter IPs. Falls back to urllib if
curl_cffi is unavailable.

NO hardcoded category keywords. Filings are filtered ONLY by target keywords
from Settings; the AI evaluator classifies each filing via event_fingerprint.
"""
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

from common import log

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _fetch_via_cffi():
    """Fetches the NSE announcements JSON via curl_cffi with session warm-up.

    Returns decoded bytes on success, None on any failure.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None

    try:
        session = cffi_requests.Session(impersonate="chrome120")
        hdrs_html = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        hdrs_json = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        }

        # Warm-up: visit homepage + filings page to obtain session cookies
        session.get("https://www.nseindia.com/", headers=hdrs_html, timeout=15)
        time.sleep(1)
        session.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                    headers=hdrs_html, timeout=15)
        time.sleep(1)

        r = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers=hdrs_json,
            timeout=20,
        )
        if r.status_code == 200 and r.content:
            return r.content
        log.info(f"NSE cffi returned HTTP {r.status_code}")
        return None
    except Exception as e:
        log.warning(f"NSE cffi error: {str(e)[:150]}")
        return None


def _fetch_via_urllib():
    """Fallback: original urllib path with cookie jar warm-up."""
    try:
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
        return resp.read()
    except Exception as e:
        log.warning(f"NSE urllib fallback error: {str(e)[:150]}")
        return None


def fetch_nse_filings(target_keywords, max_results=60):
    """Fetches recent NSE corporate announcements filtered by target keywords.

    Returns a list of dicts: source, company, subject, details, date, url.
    Returns [] on error (never raises).
    """
    out = []

    body_bytes = _fetch_via_cffi()
    if body_bytes is None:
        body_bytes = _fetch_via_urllib()
    if body_bytes is None:
        return out

    try:
        data = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning(f"NSE JSON parse failed: {str(e)[:100]}")
        return out

    if not isinstance(data, list):
        log.warning("NSE: unexpected response shape (expected list)")
        return out

    kw_lower = [str(k).lower() for k in (target_keywords or []) if k]

    for item in data:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or item.get("desc") or "").strip()
        symbol = str(item.get("symbol") or "").strip()
        details = str(item.get("attchmntText") or "").strip()
        if not subject:
            continue
        # Filter ONLY by target keywords — no hardcoded category keywords
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
    return out
