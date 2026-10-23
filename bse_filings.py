"""bse_filings.py — BSE corporate announcements (SEBI LODR Reg 30 material events).

Uses curl_cffi (Chrome TLS impersonation) to bypass the WAF that blocks urllib
from datacenter IPs. Falls back to urllib if curl_cffi is unavailable.

NO hardcoded category keywords. Filings are filtered ONLY by target keywords
from Settings; the AI evaluator classifies each filing via event_fingerprint.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from common import log

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _build_url(max_days):
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y%m%d")
    return (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P&strToDate={to_date}"
        "&strType=C&subcategory=-1"
    )


def _fetch_via_cffi(api_url):
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
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        # Warm-up to establish session
        session.get("https://www.bseindia.com/corporates/ann.html", headers=hdrs_html, timeout=15)
        time.sleep(1)
        r = session.get(api_url, headers=hdrs_json, timeout=20)
        if r.status_code == 200 and r.content:
            return r.content
        log.info(f"BSE cffi returned HTTP {r.status_code}")
        return None
    except Exception as e:
        log.warning(f"BSE cffi error: {str(e)[:150]}")
        return None


def _fetch_via_urllib(api_url):
    try:
        req = urllib.request.Request(api_url, headers={
            "User-Agent": UA,
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(2_000_000)
    except Exception as e:
        log.warning(f"BSE urllib fallback error: {str(e)[:150]}")
        return None


def fetch_bse_filings(target_keywords, max_days=3, max_results=60):
    """Fetches recent BSE corporate announcements filtered by target keywords.

    Returns a list of dicts: source, company, subject, details, date, url.
    Returns [] on error (never raises).
    """
    out = []
    api_url = _build_url(max_days)

    body_bytes = _fetch_via_cffi(api_url)
    if body_bytes is None:
        body_bytes = _fetch_via_urllib(api_url)
    if body_bytes is None:
        return out

    try:
        data = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning(f"BSE JSON parse failed: {str(e)[:100]}")
        return out

    rows = data.get("Table") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        log.warning("BSE: unexpected response shape (no 'Table' array)")
        return out

    kw_lower = [str(k).lower() for k in (target_keywords or []) if k]

    for item in rows[:max_results]:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()
        details = str(item.get("MORE") or "").strip()
        scrip = str(item.get("SLONGNAME") or item.get("Scripname") or "").strip()
        if not subject:
            continue
        # Filter ONLY by target keywords
        if kw_lower:
            hay = (subject + " " + details + " " + scrip).lower()
            if not any(k in hay for k in kw_lower):
                continue
        attachment = str(item.get("ATTACHMENTNAME") or "").strip()
        if attachment:
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{urllib.parse.quote(attachment)}"
        else:
            pdf_url = f"https://www.bseindia.com/corporates/ann.html?scrip={urllib.parse.quote(scrip)}"
        out.append({
            "source": "BSE",
            "company": scrip,
            "subject": subject,
            "details": details[:800],
            "date": str(item.get("NEWS_DT") or "")[:10],
            "url": pdf_url,
        })
        if len(out) >= max_results:
            break

    time.sleep(0.5)
    return out
