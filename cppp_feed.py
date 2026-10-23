"""cppp_feed.py — CPPP (Central Public Procurement Portal) RSS ingestion.

eprocure.gov.in publishes an RSS feed of latest tenders that Google does
not index. This is one of the two biggest sources of government tender
leads after GeM.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from common import log

CPPP_RSS = "https://eprocure.gov.in/cppp/rssdldtlist"
UA = "Mozilla/5.0 (compatible; RadarScout/2.0; business-research)"


def fetch_cppp(keywords, max_results=100):
    """Fetches CPPP RSS and filters titles/descriptions by keywords.

    Returns a list of dicts with keys: source, url, title, snippet, date.
    Returns [] on any error (never raises).
    """
    out = []
    try:
        req = urllib.request.Request(CPPP_RSS, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(1_000_000).decode("utf-8", errors="replace")
        root = ET.fromstring(body)
        kw_lower = [str(k).lower() for k in keywords if k]
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            date  = (item.findtext("pubDate") or "").strip()
            if not title or not link:
                continue
            hay = (title + " " + desc).lower()
            if kw_lower and not any(k in hay for k in kw_lower):
                continue
            out.append({
                "source": "CPPP",
                "url": link,
                "title": title,
                "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
                "date": date,
            })
            if len(out) >= max_results:
                break
    except Exception as e:
        log.warning(f"CPPP fetch failed: {e}")
    return out
