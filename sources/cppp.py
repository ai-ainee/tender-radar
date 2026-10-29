"""sources/cppp.py — CPPP (Central Public Procurement Portal) via proxy.

Direct access from datacenter IPs is blocked by Cloudflare (HTTP 403).
Chain of fallbacks:
  1. Direct HTTP  (works from residential IPs, occasionally from CI)
  2. Jina Reader  (free 1M chars/month, reliable from CI)
  3. ScraperAPI   (paid, if keys configured)

The RSS feed at eprocure.gov.in/cppp/rssdldtlist returns RSS 2.0 XML
which we parse directly. Handles Atom as a safety net.
"""
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

from common import log

CPPP_RSS = "https://eprocure.gov.in/cppp/rssdldtlist"
CPPP_ALT = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http(url, timeout=25, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {
            "User-Agent": UA,
            "Accept": "application/xml,application/atom+xml,text/xml,text/html;q=0.9,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[cppp] http {url[:60]}: {str(e)[:80]}")
        return ""


def _try_direct():
    for url in (CPPP_RSS, CPPP_ALT):
        body = _http(url)
        if body and ("<item>" in body or "<entry>" in body):
            return body, "direct"
    return "", ""


def _try_jina():
    jina_key = os.getenv("JINA_API_KEY", "").strip()
    headers = {"User-Agent": UA}
    if jina_key:
        headers["Authorization"] = f"Bearer {jina_key}"
    for url in (CPPP_RSS, CPPP_ALT):
        body = _http(f"https://r.jina.ai/{url}", headers=headers, timeout=40)
        if body and ("<item>" in body or "<entry>" in body):
            return body, "jina"
    return "", ""


def _try_scraperapi():
    keys = [k.strip() for k in os.getenv("SCRAPERAPI_API_KEYS", "").split(",")
            if k.strip()]
    if not keys:
        return "", ""
    for key in keys:
        for url in (CPPP_RSS, CPPP_ALT):
            scraper_url = f"http://api.scraperapi.com?api_key={key}&url={url}&render=false"
            body = _http(scraper_url, timeout=60)
            if body and ("<item>" in body or "<entry>" in body):
                return body, "scraperapi"
    return "", ""


def _parse_feed(body, keywords, max_results):
    kw_lower = [str(k).lower().strip() for k in keywords if k]
    out = []
    try:
        root = ET.fromstring(body)
    except Exception as e:
        log.warning(f"[cppp] XML parse failed: {str(e)[:100]}")
        return []

    is_atom = root.tag.endswith("feed")
    item_tag = "entry" if is_atom else "item"

    for item in root.iter(item_tag):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link and is_atom:
            for ln in item.findall("link"):
                if ln.get("rel") in (None, "alternate"):
                    link = ln.get("href") or ""
                    break
        desc = (item.findtext("description")
                or item.findtext("summary") or "").strip()
        date = (item.findtext("pubDate")
                or item.findtext("published")
                or item.findtext("updated") or "").strip()
        if not title or not link:
            continue
        if kw_lower:
            hay = (title + " " + desc).lower()
            if not any(k in hay for k in kw_lower):
                continue
        out.append({
            "url": link,
            "title": title[:300],
            "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
            "date": date[:30],
        })
        if len(out) >= max_results:
            break

    return out


def fetch_cppp(keywords, max_results=100):
    """Return list of {url, title, snippet, date} from CPPP RSS."""
    if not keywords:
        return []

    body, method = _try_direct()
    if not body:
        body, method = _try_jina()
    if not body:
        body, method = _try_scraperapi()
    if not body:
        log.warning("[cppp] all methods failed (direct, jina, scraperapi)")
        return []

    out = _parse_feed(body, keywords, max_results)
    log.info(f"[cppp] {len(out)} tender(s) via {method}")
    return out
