"""test_bse_pkg.py — inspect the bse package's announcements response."""
import traceback
from datetime import datetime, timedelta

def write(text):
    print(text, flush=True)
    with open("bse_diagnose_output.txt", "a", encoding="utf-8") as f:
        f.write(text + "\n")

open("bse_diagnose_output.txt", "w").close()

write("=" * 60)
write("BSE PACKAGE ANNOUNCEMENTS DIAGNOSTIC")
write("=" * 60)

try:
    from bse import BSE
except Exception as e:
    write(f"[FAIL] cannot import bse: {e}")
    raise SystemExit(0)

try:
    with BSE(download_folder="./tmp_bse") as bse:
        write(f"[OK] BSE context opened")
        try:
            write(f"  package version: {bse.version()}")
        except Exception as e:
            write(f"  version() failed: {e}")

        to_date = datetime.now()
        from_date = to_date - timedelta(days=15)
        write(f"  from_date={from_date!r}  to_date={to_date!r}")

        try:
            res = bse.announcements(
                from_date=from_date,
                to_date=to_date,
            )
            write(f"[OK] bse.announcements() returned")
            write(f"  type: {type(res).__name__}")
            if isinstance(res, dict):
                write(f"  keys: {list(res.keys())}")
                for k, v in res.items():
                    if isinstance(v, list):
                        write(f"    {k}: list[{len(v)}]")
                        if v and isinstance(v[0], dict):
                            write(f"      first item keys: {list(v[0].keys())[:10]}")
                            write(f"      first item sample: {str(v[0])[:300]}")
                    else:
                        write(f"    {k}: {type(v).__name__} = {str(v)[:200]}")
            else:
                write(f"  raw: {str(res)[:400]}")
        except Exception as e:
            write(f"[FAIL] bse.announcements() raised: {e}")
            traceback.print_exc()

        # Try with explicit category/subcategory defaults
        try:
            write("\n--- retry with explicit defaults ---")
            res2 = bse.announcements(
                from_date=from_date,
                to_date=to_date,
                category="-1",
                subcategory="-1",
            )
            write(f"  type: {type(res2).__name__}")
            if isinstance(res2, dict):
                write(f"  keys: {list(res2.keys())}")
                for k, v in res2.items():
                    if isinstance(v, list):
                        write(f"    {k}: list[{len(v)}]")
        except Exception as e:
            write(f"[FAIL] retry raised: {e}")

except Exception as e:
    write(f"[FAIL] BSE context failed: {e}")
    traceback.print_exc()

write("\n" + "=" * 60)
write("DIAGNOSTIC COMPLETE")
write("=" * 60)
