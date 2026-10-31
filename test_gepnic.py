"""test_gepnic.py — pass 2: print actual anchor texts to verify tenders vs nav."""
import os
import re

def w(s):
    print(s, flush=True)
    with open("gepnic_output.txt", "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

open("gepnic_output.txt", "w").close()

w("=" * 60)
w("GePNIC pass 2 — anchor text inspection")
w("=" * 60)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

try:
    import requests
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    w("[FATAL] requests missing")
    raise SystemExit(0)

def fetch(url):
    try:
        r = requests.get(url, timeout=30, verify=False,
                         headers={"User-Agent": UA,
                                  "Accept": "text/html",
                                  "Accept-Language": "en-IN,en;q=0.9"})
        return r.status_code, r.text
    except Exception as e:
        return -1, str(e)

def show_anchors(label, url):
    w(f"\n=== {label} ===")
    w(f"URL: {url}")
    status, body = fetch(url)
    w(f"HTTP {status}, {len(body) if body else 0} chars")

    if status != 200 or not body:
        w("  (failed — skipping anchor dump)")
        return

    # Extract ALL anchors with href and text
    anchors = re.findall(
        r'<a[^>]+href=["\']([^"\']*)["\'][^>]*>([^<]{5,300})</a>',
        body, re.I | re.S
    )

    w(f"  Total anchors found: {len(anchors)}")
    w(f"  First 15 anchors (text | href[:80]):")
    for i, (href, text) in enumerate(anchors[:15]):
        t = re.sub(r"\s+", " ", text.strip())[:120]
        h = (href or "")[:80]
        w(f"    [{i}] {t!r}")
        w(f"         href={h!r}")

    # Also dump the first table structure — tells us if it's a tender table
    w("\n  First <table> presence check:")
    tables = re.findall(r"<table[^>]*>", body, re.I)
    w(f"    <table> count: {len(tables)}")
    if tables:
        w(f"    first table tag: {tables[0][:200]!r}")

show_anchors(
    "CPPP FrontEndListTendersbyDate",
    "https://eprocure.gov.in/epublish/app?page=FrontEndListTendersbyDate&service=page"
)
show_anchors(
    "Maharashtra FrontEndLatestActiveTenders",
    "https://mahatenders.gov.in/nicgep/app?page=FrontEndLatestActiveTenders&service=page"
)
show_anchors(
    "Maharashtra FrontEndListTendersbyDate",
    "https://mahatenders.gov.in/nicgep/app?page=FrontEndListTendersbyDate&service=page"
)

w("\n" + "=" * 60)
w("DONE")
