"""test_gepnic.py — verify GePNIC scraping works before wiring into pipeline.

Tests 4 paths:
  1. CPPP direct
  2. CPPP via Jina proxy (bypasses Cloudflare)
  3. Maharashtra NIC GePNIC direct
  4. Maharashtra NIC GePNIC via Jina proxy

Writes findings to gepnic_output.txt for artifact download.
"""
import os
import re

def w(s):
    print(s, flush=True)
    with open("gepnic_output.txt", "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

open("gepnic_output.txt", "w").close()

w("=" * 60)
w("GePNIC diagnostic — 4 paths")
w("=" * 60)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ── HTTP helpers ────────────────────────────────────────────────
try:
    import requests
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    w("[FATAL] requests not installed")
    raise SystemExit(0)

def fetch(url, use_jina=False, timeout=30):
    """Fetch url, optionally via Jina Reader proxy."""
    if use_jina:
        jina_key = os.getenv("JINA_API_KEY", "").strip()
        headers = {"User-Agent": UA, "X-Return-Format": "html"}
        if jina_key:
            headers["Authorization"] = f"Bearer {jina_key}"
        actual = "https://r.jina.ai/" + url
    else:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        actual = url
    try:
        r = requests.get(actual, headers=headers, timeout=timeout, verify=False)
        return r.status_code, r.text
    except Exception as e:
        return -1, f"EXCEPTION: {str(e)[:150]}"

def looks_like_html(body):
    """Rough check that body contains HTML anchors and tables."""
    if not body or len(body) < 500:
        return False
    low = body.lower()
    return "<a " in low and ("<tr" in low or "<table" in low)

def count_tender_rows(body):
    """Count likely tender rows (anchors with reasonable text)."""
    if not body:
        return 0
    # Simple heuristic: anchors with text length 20-300, containing tender-ish words
    matches = re.findall(r'<a[^>]+>([^<]{20,300})</a>', body, re.I)
    TENDER_WORDS = ("tender", "bid", "rfp", "rfq", "eoi", "procurement",
                    "supply", "purchase", "work", "service")
    return sum(1 for m in matches if any(tw in m.lower() for tw in TENDER_WORDS))

def preview(body, n=200):
    if not body:
        return ""
    s = str(body).replace("\n", " ").replace("\r", "")
    return s[:n]

# ── Test 1-2: CPPP ──────────────────────────────────────────────
CPPP_URLS = [
    "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata",
    "https://eprocure.gov.in/epublish/app?page=FrontEndListTendersbyDate&service=page",
]

for cppp_url in CPPP_URLS:
    w(f"\n--- CPPP: {cppp_url[:80]} ---")

    # Direct
    status, body = fetch(cppp_url, use_jina=False)
    w(f"  Direct: HTTP {status}, {len(body) if body else 0} chars")
    if status == 200 and looks_like_html(body):
        rows = count_tender_rows(body)
        w(f"    looks_like_html=YES, tender-ish anchors={rows}")
        w(f"    preview: {preview(body)!r}")
    elif status == 200:
        w(f"    looks_like_html=NO (returned non-HTML or empty)")
        w(f"    preview: {preview(body)!r}")
    else:
        w(f"    error preview: {preview(body)!r}")

    # Jina
    status2, body2 = fetch(cppp_url, use_jina=True)
    w(f"  Jina:   HTTP {status2}, {len(body2) if body2 else 0} chars")
    if status2 == 200 and looks_like_html(body2):
        rows = count_tender_rows(body2)
        w(f"    looks_like_html=YES, tender-ish anchors={rows}")
        w(f"    preview: {preview(body2, 300)!r}")
    elif status2 == 200:
        w(f"    looks_like_html=NO")
        w(f"    preview: {preview(body2, 300)!r}")
    else:
        w(f"    error preview: {preview(body2, 200)!r}")

# ── Test 3-4: Maharashtra ──────────────────────────────────────
MH_URLS = [
    "https://mahatenders.gov.in/nicgep/app?page=FrontEndLatestActiveTenders&service=page",
    "https://mahatenders.gov.in/nicgep/app?page=FrontEndListTendersbyDate&service=page",
]

for mh_url in MH_URLS:
    w(f"\n--- Maharashtra: {mh_url[:80]} ---")

    status, body = fetch(mh_url, use_jina=False)
    w(f"  Direct: HTTP {status}, {len(body) if body else 0} chars")
    if status == 200 and looks_like_html(body):
        rows = count_tender_rows(body)
        w(f"    looks_like_html=YES, tender-ish anchors={rows}")
        w(f"    preview: {preview(body)!r}")
    elif status == 200:
        w(f"    looks_like_html=NO")
        w(f"    preview: {preview(body)!r}")
    else:
        w(f"    error preview: {preview(body)!r}")

    status2, body2 = fetch(mh_url, use_jina=True)
    w(f"  Jina:   HTTP {status2}, {len(body2) if body2 else 0} chars")
    if status2 == 200 and looks_like_html(body2):
        rows = count_tender_rows(body2)
        w(f"    looks_like_html=YES, tender-ish anchors={rows}")
        w(f"    preview: {preview(body2, 300)!r}")
    elif status2 == 200:
        w(f"    looks_like_html=NO")
        w(f"    preview: {preview(body2, 300)!r}")
    else:
        w(f"    error preview: {preview(body2, 200)!r}")

w("\n" + "=" * 60)
w("DONE")
