"""cppp_feed.py — CPPP (Central Public Procurement Portal) RSS ingestion.

eprocure.gov.in publishes an RSS feed of latest tenders that Google does
not index. This is one of the two biggest sources of government tender
leads after GeM.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from common import log

CPPP_URLS = [
    "https://eprocure.gov.in/cppp/rssdldtlist",                    # original feed
    "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata", # alternate JSON endpoint
    "https://eprocure.gov.in/epublish/app?page=FrontEndListTendersbyDate&service=page",  # HTML fallback
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def fetch_cppp(keywords, max_results=100):
    """Fetches CPPP RSS and filters titles/descriptions by keywords.

    Returns a list of dicts with keys: source, url, title, snippet, date.
    Returns [] on any error (never raises).
    """
    out = []
    body = ""
    for url in CPPP_URLS:
        try:
            try:
                from curl_cffi import requests as cffi_requests
                r = cffi_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome120", timeout=20)
                if r.status_code == 200 and r.content:
                    body = r.content.decode("utf-8", errors="replace")
                    break
                else:
                    log.info(f"CPPP {url} -> HTTP {r.status_code}")
            except ImportError:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read(1_000_000).decode("utf-8", errors="replace")
                if body:
                    break
        except Exception as e:
            log.info(f"CPPP {url} failed: {str(e)[:80]}")
            continue

    if not body:
        log.warning("CPPP: all URLs failed")
        return out

    try:
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
