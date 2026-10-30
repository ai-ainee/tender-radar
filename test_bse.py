"""test_bse.py — defensive BSE diagnostic.

Writes results to bse_diagnose_output.txt so you can download the artifact.
"""
import json
import sys
import traceback

def write(text):
    print(text, flush=True)
    with open("bse_diagnose_output.txt", "a", encoding="utf-8") as f:
        f.write(text + "\n")

open("bse_diagnose_output.txt", "w").close()

write("=" * 60)
write("BSE DIAGNOSTIC v2")
write("=" * 60)

# ── 1. Inspect bse package ────────────────────────────────────
write("\n--- bse package ---")
try:
    import bse as bse_pkg
    write(f"[OK] import bse -> {getattr(bse_pkg, '__file__', '?')}")
    write(f"  version: {getattr(bse_pkg, '__version__', 'unknown')}")
    members = [m for m in dir(bse_pkg) if not m.startswith('_')]
    write(f"  public members: {members}")
    BSE_cls = getattr(bse_pkg, 'BSE', None)
    if BSE_cls:
        write(f"  BSE class methods: {[m for m in dir(BSE_cls) if not m.startswith('_')]}")
except ImportError as e:
    write(f"[FAIL] cannot import bse: {e}")
    write("  → requires 'bse' in requirements.txt AND workflow install step")
    bse_pkg = None
except Exception as e:
    write(f"[FAIL] bse import raised: {e}")
    bse_pkg = None

# ── 2. Inspect sources.bse (only what exists) ─────────────────
write("\n--- sources.bse ---")
try:
    import sources.bse as sbs
    write("[OK] imported sources.bse")
    funcs = [n for n in dir(sbs) if not n.startswith('_')]
    write(f"  functions: {funcs}")
    # Only test what's actually present
    for name in ("fetch_bse", "_fetch_via_cffi", "_try_endpoint", "_build_url_primary", "_build_url_alt"):
        write(f"  has {name}: {hasattr(sbs, name)}")
except Exception as e:
    write(f"[FAIL] sources.bse import raised: {e}")
    traceback.print_exc()
    sbs = None

# ── 3. Call fetch_bse ─────────────────────────────────────────
if sbs and hasattr(sbs, "fetch_bse"):
    write("\n--- fetch_bse() call ---")
    for days, kw in [(15, []), (15, ["capex","expansion","order","contract","investment","plant"])]:
        try:
            result = sbs.fetch_bse(kw, max_days=days, max_results=10)
            write(f"fetch_bse(kw={len(kw)}, {days}d): {len(result)} row(s)")
            if result:
                for i, r in enumerate(result[:3]):
                    write(f"  [{i}] {str(r.get('title',''))[:80]!r}")
                    write(f"      company={r.get('company')!r} date={r.get('date')!r}")
        except Exception as e:
            write(f"[FAIL] fetch_bse(kw={len(kw)}, {days}d) raised: {e}")
            traceback.print_exc()

# ── 4. Raw requests session with warmup ──────────────────────
write("\n--- raw session test ---")
try:
    import requests
    from datetime import datetime, timedelta

    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")

    s = requests.Session()
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    s.headers.update({"User-Agent": ua, "Accept-Language": "en-IN,en;q=0.9"})

    write("  warming up homepage...")
    s.get("https://www.bseindia.com/", timeout=15)
    write(f"    cookies: {len(s.cookies)}")
    s.get("https://www.bseindia.com/corporates/ann.html", timeout=15)
    write(f"    cookies after ann.html: {len(s.cookies)}")

    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip="
        f"&strSearch=P&strToDate={to_date}&strType=C&subcategory="
    )
    write(f"  GET {url[:120]}...")
    r = s.get(url, headers={
        "Referer": "https://www.bseindia.com/corporates/ann.html",
        "Origin": "https://www.bseindia.com",
        "Accept": "application/json, text/plain, */*",
    }, timeout=20)
    write(f"  status: {r.status_code}")
    write(f"  body[:400]: {r.text[:400]!r}")
    try:
        data = r.json()
        write(f"  parsed type: {type(data).__name__}")
        if isinstance(data, dict):
            write(f"  keys: {list(data.keys())}")
            for k, v in data.items():
                if isinstance(v, list):
                    write(f"    {k}: list[{len(v)}]")
    except Exception as e:
        write(f"  parse failed: {e}")
except Exception as e:
    write(f"[FAIL] raw session test raised: {e}")
    traceback.print_exc()

write("\n" + "=" * 60)
write("DIAGNOSTIC COMPLETE")
write("=" * 60)
