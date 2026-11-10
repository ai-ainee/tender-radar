"""sources/gem.py — Public GeM bid collector with search-index fallback.

Step 25: GeM rejects connections from some hosted CI runners even if the site
opens in a user's browser. Try GeM's public HTML pages first. When unavailable,
use configured SERPER_API_KEYS to discover *individually indexed* GeM bid
references on public web pages. Fallback records are provisional, NOT verified
GeM bid status. No guessed GeM document URLs, logins, CAPTCHAs or proxy bypass.

Contract: fetch_gem(keywords, max_results=80) -> [{url,title,snippet}, ...].
"""

import os
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from common import log

GEM_URLS = (
    "https://bidplus.gem.gov.in/all-bids",
    "https://bidplus.gem.gov.in/bidlists",
)
GEM_HOST = "bidplus.gem.gov.in"
BID_LINK = re.compile(r"/(?:showbidDocument/\d+|bidding/bid/[^?#]+)", re.I)
BID_NUMBER = re.compile(r"\bGEM\s*/\s*(20\d{2})\s*/\s*B\s*/\s*(\d{5,9})\b", re.I)
BLOCK_INDICATORS = (
    "access denied", "captcha", "verify you are human", "request blocked",
    "attention required", "unusual traffic",
)
LANDING_SEGMENTS = frozenset((
    "search", "search-results", "category", "categories", "topic", "topics",
    "latest-tenders", "all-tenders", "product", "products", "tag", "tags",
    "jobs", "tenders", "tender", "home",
))


def _normalise_bid_link(href):
    if not href:
        return None
    url = urljoin("https://bidplus.gem.gov.in/", str(href).strip())
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname != GEM_HOST:
        return None
    if not BID_LINK.search(parts.path):
        return None
    return url.split("#", 1)[0]


def _extract_items(page, keywords, max_results):
    raw = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]'))
        .filter(a => /showbidDocument\\/\\d+|\\/bidding\\/bid\\//i.test(a.href))
        .map(a => {
            let node = a.closest('tr, .bid_card, .card, .bid-list, li') || a.parentElement || a;
            return {href: a.href, text: (node.innerText || a.innerText || '').trim().replace(/\\s+/g, ' ')};
        })""")
    terms = [str(k).casefold().strip() for k in keywords if str(k).strip()]
    output, seen = [], set()
    for item in raw or []:
        url = _normalise_bid_link(item.get("href", ""))
        title = str(item.get("text", "")).strip()
        if not url or url in seen or not title:
            continue
        seen.add(url)
        if terms and not any(term in title.casefold() for term in terms):
            continue
        output.append({"url": url, "title": title[:300], "snippet": title[:500]})
        if len(output) >= max_results:
            break
    return output


def _official_listing(keywords, max_results):
    """Return official HTML matches; failures are logged and never disguised."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("[gem] playwright missing; cannot check official listing")
        return []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage",
            ])
            try:
                page = browser.new_context(viewport={"width": 1365, "height": 900}).new_page()
                output, seen = [], set()
                for address in GEM_URLS:
                    response = None
                    try:
                        response = page.goto(address, wait_until="domcontentloaded", timeout=25000)
                    except PWTimeout:
                        log.warning("[gem] navigation timed out: %s", address)
                    except Exception as exc:
                        log.warning("[gem] navigation failed %s: %s", address, str(exc)[:160])
                        continue
