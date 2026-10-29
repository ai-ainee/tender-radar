"""test_bse.py — GitHub Actions diagnostic for BSE endpoint.

Writes results to bse_diagnose_output.txt so you can download them
from the Actions artifacts even if the step fails.
"""
import json
import sys
import traceback

def write(text):
    """Print AND append to file."""
    print(text)
    with open("bse_diagnose_output.txt", "a", encoding="utf-8") as f:
        f.write(text + "\n")

# Start fresh
open("bse_diagnose_output.txt", "w").close()

write("=" * 60)
write("BSE ENDPOINT DIAGNOSTIC")
write("=" * 60)

# ── Test imports ──────────────────────────────────────────────
try:
    from sources.bse import _build_url_primary, _build_url_alt, _fetch_via_cffi, _fetch_via_urllib
    write("[OK] imports from sources.bse")
except Exception as e:
    write(f"[FAIL] could not import sources.bse: {e}")
    traceback.print_exc()
    sys.exit(0)

# ── Test each endpoint ────────────────────────────────────────
for name, builder in [("PRIMARY", _build_url_primary), ("ALTERNATE", _build_url_alt)]:
    write(f"\n--- {name} endpoint (7-day window) ---")
    try:
        url = builder(7)
    except Exception as e:
        write(f"[FAIL] could not build URL: {e}")
        continue

    write(f"URL: {url[:140]}...")

    # Try curl_cffi (production path)
    try:
        cffi_body = _fetch_via_cffi(url)
    except Exception as e:
        write(f"curl_cffi EXCEPTION: {e}")
        cffi_body = None

    if cffi_body is None:
        write("curl_cffi:  FAILED (returned None)")
    else:
        write(f"curl_cffi:  {len(cffi_body)} bytes")
        preview = cffi_body[:300].decode("utf-8", errors="replace")
        write(f"  preview: {preview!r}")

        # Try to parse
        try:
            data = json.loads(cffi_body.decode("utf-8", errors="replace"))
            if isinstance(data, str):
                write(f"  PARSED: string -> {data!r}")
            elif isinstance(data, list):
                write(f"  PARSED: list with {len(data)} item(s)")
                if data and isinstance(data[0], dict):
                    write(f"  first item keys: {list(data[0].keys())[:10]}")
            elif isinstance(data, dict):
                write(f"  PARSED: dict with keys {list(data.keys())[:10]}")
                for k, v in data.items():
                    if isinstance(v, list):
                        write(f"    {k}: list[{len(v)}]")
                        if v and isinstance(v[0], dict):
                            write(f"      first row keys: {list(v[0].keys())[:10]}")
                    else:
                        write(f"    {k}: {type(v).__name__} = {str(v)[:80]!r}")
        except Exception as e:
            write(f"  PARSE FAILED: {e}")

    # Try urllib (fallback)
    try:
        urllib_body = _fetch_via_urllib(url)
    except Exception as e:
        write(f"urllib EXCEPTION: {e}")
        urllib_body = None

    if urllib_body is None:
        write("urllib:     FAILED (returned None)")
    else:
        write(f"urllib:     {len(urllib_body)} bytes")
        preview = urllib_body[:300].decode("utf-8", errors="replace")
        write(f"  preview: {preview!r}")

# ── Test the full fetch_bse call ─────────────────────────────
write("\n--- full fetch_bse() call with empty keywords ---")
try:
    from sources.bse import fetch_bse
    result = fetch_bse([], max_days=7, max_results=10)
    write(f"fetch_bse([], 7d): {len(result)} row(s)")
    if result:
        for i, r in enumerate(result[:3]):
            write(f"  [{i}] {str(r.get('title', ''))[:80]!r} | {r.get('date')}")
except Exception as e:
    write(f"fetch_bse EXCEPTION: {e}")
    traceback.print_exc()

write("\n--- full fetch_bse() call with intent keywords ---")
try:
    kw = ["capex", "expansion", "order", "contract", "acquisition",
          "investment", "facility", "plant", "construction", "tender"]
    result = fetch_bse(kw, max_days=7, max_results=10)
    write(f"fetch_bse({kw}, 7d): {len(result)} row(s)")
except Exception as e:
    write(f"fetch_bse EXCEPTION: {e}")

write("\n" + "=" * 60)
write("DIAGNOSTIC COMPLETE")
write("=" * 60)
