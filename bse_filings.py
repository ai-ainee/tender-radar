"""bse_filings.py — BSE corporate announcements (SEBI LODR Reg 30 material events).

NO hardcoded category keywords. Filings are filtered only by Target Products
from Settings; the AI evaluator classifies each filing (order win / capex /
partnership / etc.) via the event_fingerprint extraction.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from common import log

UA = "Mozilla/5.0 (compatible; RadarScout/2.0; business-research)"


def fetch_bse_filings(target_keywords, max_days=3, max_results=60):
    """Fetches recent BSE corporate announcements filtered by target keywords.

    Returns a list of dicts: source, company, subject, details, date, url.
    No signal categorisation — that's the AI evaluator's job.
    Returns [] on error.
    """
    out = []
    try:
        to_date = datetime.now().strftime("%Y%m%d")
        from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y%m%d")
        api_url = (
            "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
            f"?strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P&strToDate={to_date}"
            "&strType=C&subcategory=-1"
        )
        req = urllib.request.Request(api_url, headers={
            "User-Agent": UA,
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read(2_000_000).decode("utf-8", errors="replace"))

        rows = data.get("Table") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            log.warning("BSE: unexpected response shape (no 'Table' array)")
            return []

        kw_lower = [str(k).lower() for k in target_keywords if k]

        for item in rows[:max_results]:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()
            details = str(item.get("MORE") or "").strip()
            scrip = str(item.get("SLONGNAME") or item.get("Scripname") or "").strip()
            if not subject:
                continue
            # Filter ONLY by target keywords — no hardcoded category keywords
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
    except Exception as e:
        log.warning(f"BSE filings fetch failed: {e}")
    return out
