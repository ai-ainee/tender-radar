"""bse_filings.py — BSE corporate announcements (SEBI LODR Reg 30 material events).

Every listed Indian company MUST file material events (order wins, capex,
partnerships, M&A) with BSE the same day. This is the canonical source
that news sites copy days later — and Google does not index it directly.
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from common import log

UA = "Mozilla/5.0 (compatible; RadarScout/2.0; business-research)"

# Signal category detection — used to tag filings and drop noise.
SIGNAL_KEYWORDS = {
    "order_win":       ["order", "award", "loi", "loa", "contract", "winning", "bagging"],
    "capex_expansion": ["capex", "expansion", "capacity", "plant", "facility", "greenfield", "brownfield"],
    "fundraising":     ["fund raising", "qip", "rights issue", "preferential", "fundraise"],
    "partnership":     ["agreement", "mou", "partnership", "joint venture", "tie-up"],
    "acquisition":     ["acquisition", "takeover", "merger"],
    "new_product":     ["launch", "new product", "unveiling"],
}


def _classify(subject, details):
    """Returns the category key ('order_win', 'capex_expansion', ...) or None."""
    text = (str(subject or "") + " " + str(details or "")).lower()
    for category, keywords in SIGNAL_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None


def _is_relevant(subject, details, company, target_keywords):
    """True if the filing mentions any of the user's target keywords."""
    if not target_keywords:
        return True
    hay = (str(subject or "") + " " + str(details or "") + " " + str(company or "")).lower()
    return any(str(k).lower() in hay for k in target_keywords if k)


def fetch_bse_filings(target_keywords, max_days=3, max_results=60):
    """Fetches recent BSE corporate announcements filtered by keyword + category.

    Returns a list of dicts with keys:
      source, company, subject, details, category, date, url
    Returns [] on error (never raises).
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
            log.warning(f"BSE: unexpected response shape (no 'Table' array)")
            return []

        for item in rows[:max_results]:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()
            details = str(item.get("MORE") or "").strip()
            scrip   = str(item.get("SLONGNAME") or item.get("Scripname") or "").strip()
            if not subject:
                continue
            category = _classify(subject, details)
            if not category:
                continue
            if not _is_relevant(subject, details, scrip, target_keywords):
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
                "category": category,
                "date": str(item.get("NEWS_DT") or "")[:10],
                "url": pdf_url,
            })
            if len(out) >= max_results:
                break
        # Small polite pause — BSE rate-limits if hammered
        time.sleep(0.5)
    except Exception as e:
        log.warning(f"BSE filings fetch failed: {e}")
    return out
