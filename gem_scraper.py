"""gem_scraper.py — GeM (Government e-Marketplace) public bid list scraper.

GeM's bid list at bidplus.gem.gov.in is publicly readable HTML — no login
required. Google does not index it. This is the single biggest source of
government tender leads in India (~800–1200 active bids at any time).
"""
import re
import urllib.parse
import urllib.request

from common import log

# GeM changes this URL occasionally — try in order until one works
GEM_URLS = [
    "https://bidplus.gem.gov.in/all-bids-data",
    "https://bidplus.gem.gov.in/all-bids",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(3_000_000).decode("utf-8", errors="replace")


def fetch_gem(keywords, max_results=80):
    """Fetches GeM's public bid list and filters by keyword.

    Returns a list of dicts with keys: source, url, title, snippet.
    Returns [] on error (never raises).
    """
    out = []
    html = ""
    for url in GEM_URLS:
        try:
            html = _fetch_html(url)
            if html and len(html) > 1000:
                break
        except Exception as e:
            log.warning(f"GeM fetch failed for {url}: {e}")
            continue
    if not html:
        return out

    kw_lower = [str(k).lower() for k in keywords if k]
    seen_urls = set()

    # GeM bid cards contain <a href="/showbidDocument/12345">...</a>
    for m in re.finditer(r'href="(/showbidDocument/\d+)"[^>]*>\s*([^<]{10,300})', html, re.I):
        path, title = m.group(1), m.group(2).strip()
        full_url = "https://bidplus.gem.gov.in" + path
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        hay = title.lower()
        if kw_lower and not any(k in hay for k in kw_lower):
            continue
        out.append({
            "source": "GeM",
            "url": full_url,
            "title": title,
            "snippet": title[:500],
        })
        if len(out) >= max_results:
            break

    if not out:
        # Fallback — sometimes GeM wraps bid links in a different anchor
        for m in re.finditer(r'href="(/showbidDocument/\d+)"[^>]*>\s*([^<]+)', html, re.I):
            path, title = m.group(1), m.group(2).strip()
            full_url = "https://bidplus.gem.gov.in" + path
            if full_url in seen_urls or not title:
                continue
            seen_urls.add(full_url)
            if kw_lower and not any(k in title.lower() for k in kw_lower):
                continue
            out.append({"source": "GeM", "url": full_url, "title": title[:300], "snippet": title[:500]})
            if len(out) >= max_results:
                break
    return out
