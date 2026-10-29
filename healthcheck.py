"""
Radar Scout health check - run weekly by the workflow, on demand (job = health) or locally: python healthcheck.py

Deploy 2.2 fixes:
  • sources/ module check — verifies all 8 new source modules import cleanly
    and that Playwright + its browser binary are installed. Runs before the
    live API calls so a broken sources/gem.py fails fast with a clear message
    instead of producing "tenders: 0 lead(s)" on the next harvest run.
  • Adapter health — uses load_settings() to get normalised shape (was showing
    all adapters disabled because it passed raw API response)
  • CompanyDedup — now loads CRM snapshot too (was only showing 22 local cache rows)
  • Contact Master — distinguishes empty tab vs missing tab vs filtered rows

Verifies: libraries, sources modules, OCR tools, API keys, Apps Script webhook,
live LLM, live search, Telegram, adapters, phone coverage, CompanyDedup cache.
Exits 1 when CRITICAL.
"""
import importlib
import os
import shutil
import sys
import time

from common import (LLMRouter, SearchEngine, SheetAPI, SheetError, Telegram,
                    log, run_job, tg_escape)

RUN_INFO = {}
OK, WARN, BAD = "✅", "⚠️", "❌"


def check_libs(add):
    for mod, why, critical in (("requests", "HTTP", True), ("bs4", "HTML parsing", True), ("pypdf", "PDF text", False),
                               ("dns", "e-mail MX checks", False), ("ddgs", "free search fallback", False),
                               ("pytesseract", "OCR for scanned PDFs", False), ("pdf2image", "OCR for scanned PDFs", False),
                               ("playwright", "JS/gated portal crawling", False)):
        try:
            importlib.import_module(mod)
        except Exception:
            add(BAD if critical else WARN, f"Python library '{mod}' missing ({why})", critical)
    for tool, why in (("tesseract", "OCR"), ("pdftoppm", "PDF to image for OCR")):
        if not shutil.which(tool):
            add(WARN, f"'{tool}' not installed ({why}); scanned PDFs will be skipped", False)

    for mod, why, critical in (("standard_lead", "StandardLead schema", True),
                               ("phone_extractor", "Phone extraction waterfall", True),
                               ("adapters", "Source adapter framework", True)):
        try:
            importlib.import_module(mod)
        except Exception as e:
            add(BAD, f"New module '{mod}' missing/broken ({why}): {str(e)[:80]}", critical)


def check_sources(add):
    """Verify the new sources/ modules are importable and their deps exist.

    Each source module is a self-contained external data source (GeM via
    Playwright, CPPP via proxy, BSE filings, company watchlist, news feeds,
    MoEF, trade publications). If any of them has a syntax error, missing
    import, or missing dependency, the harvest pipeline silently returns
    zero leads from that source with no obvious signal beyond a single log
    line. Running this check weekly catches regressions before harvests.
    """
    # (module, label, critical)
    # critical = True means the source should always work; False means
    # best-effort source that may legitimately be missing if not deployed.
    modules = [
        ("sources",                    "sources package itself",       True),
        ("sources.gem",                "GeM via Playwright",           True),
        ("sources.cppp",               "CPPP with proxy fallback",     True),
        ("sources.bse",                "BSE filings (7-day window)",   True),
        ("sources.company_watchlist",  "Watchlist crawler",            True),
        ("sources.news_feeds",         "News RSS feeds",               True),
        ("sources.moef",               "MoEF public hearings",         False),
        ("sources.trade_publications", "Trade publication RSS",        False),
    ]
    for mod, label, critical in modules:
        try:
            importlib.import_module(mod)
        except Exception as e:
            add(BAD if critical else WARN,
                f"Source module '{mod}' broken ({label}): {str(e)[:80]}",
                critical)

    # Verify each module exposes its expected fetch_* function
    expected_callables = [
        ("sources.gem",                "fetch_gem",                 True),
        ("sources.cppp",               "fetch_cppp",                True),
        ("sources.bse",                "fetch_bse",                 True),
        ("sources.company_watchlist",  "fetch_company_watchlist",   True),
        ("sources.news_feeds",         "fetch_all_news",            True),
        ("sources.moef",               "fetch_moef",                False),
        ("sources.trade_publications", "fetch_trade_publications",  False),
    ]
    for mod_name, fn_name, critical in expected_callables:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue  # already reported above
        if not callable(getattr(mod, fn_name, None)):
            add(BAD if critical else WARN,
                f"Source module '{mod_name}' missing callable '{fn_name}()'",
                critical)

    # Playwright needs both the Python package AND the Chromium browser binary.
    # `pip install playwright` alone does NOT install the browser — the workflow
    # must also run `python -m playwright install chromium --with-deps`.
    try:
        import playwright  # noqa: F401
    except ImportError:
        add(WARN, "playwright package not installed — GeM will return 0 leads", False)
    else:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                browser.close()
            add(OK, "Playwright Chromium launches cleanly (GeM will work)", False)
        except Exception as e:
            msg = str(e)[:120]
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                add(BAD,
                    "Playwright Chromium browser binary NOT installed — "
                    "run `python -m playwright install chromium --with-deps` "
                    "in the workflow after `pip install`",
                    False)
            else:
                add(WARN, f"Playwright Chromium launch failed: {msg}", False)


def check_keys(add):
    llm = LLMRouter()
    pools = {"Gemini": llm.gemini, "Groq": llm.groq, "OpenRouter": llm.openrouter, "Extra LLM": llm.extra}
    have = {n: len(p) for n, p in pools.items() if p}
    if not have:
        add(BAD, "No LLM API keys configured (GEMINI_API_KEYS / GROQ_API_KEYS / OPENROUTER_API_KEYS)", True)
    else:
        add(OK, "LLM keys: " + ", ".join(f"{n} x{c}" for n, c in have.items()), False)
        if len(have) == 1 and sum(have.values()) == 1:
            add(WARN, "Only ONE LLM key configured - add a second provider as a backup", False)
    if llm.openai:
        add(WARN, "OPENAI_API_KEY is set - this provider is PAID. Remove the secret to stay 100% free", False)
    s = SearchEngine()
    sk = len(getattr(s, "serper", []) or []) if hasattr(s, "serper") else 0
    bk = len(getattr(s, "brave", []) or []) if hasattr(s, "brave") else 0
    add(OK if (sk or bk) else WARN, f"Search keys: Serper x{sk}, Brave x{bk}" + ("" if (sk or bk) else " (only the free DuckDuckGo fallback; it is often blocked on GitHub IPs)"), False)
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        add(WARN, "Telegram is not configured - you will get no alerts from Python", False)
    return llm


def check_adapters(add, sheet):
    """FIX: use load_settings() so is_enabled() sees 'countries'/'targets' keys."""
    try:
        from adapters import get_all, get_enabled
        from radar import load_settings
    except Exception as e:
        add(BAD, f"Adapter framework not importable: {str(e)[:80]}", True)
        return

    all_adapters = get_all()
    if not all_adapters:
        add(BAD, "No adapters registered — adapter auto-register failed", True)
        return

    try:
        cfg = load_settings(sheet)          # ← FIX: normalised shape
    except Exception as e:
        add(WARN, f"Cannot check adapter enablement (settings load failed): {str(e)[:80]}", False)
        return

    enabled_names = {a.name for a in get_enabled(cfg)}
    all_names = sorted(a.name for a in all_adapters)
    disabled = [n for n in all_names if n not in enabled_names]

    if not enabled_names:
        add(BAD, f"All {len(all_adapters)} adapters are DISABLED for current Settings", True)
        return

    if disabled:
        add(WARN, f"Adapters: {len(enabled_names)}/{len(all_adapters)} enabled — "
                  f"enabled={sorted(enabled_names)} disabled={disabled}", False)
    else:
        add(OK, f"All {len(all_adapters)} adapters enabled: {all_names}", False)


def check_phone_coverage(add, sheet):
    """FIX: distinguish empty vs missing vs filtered."""
    try:
        data = sheet.get("get_contact_index")
    except SheetError as e:
        add(WARN, f"Cannot check phone coverage (contact index failed): {str(e)[:80]}", False)
        return

    if isinstance(data, dict) and data.get("status") == "error":
        add(BAD, f"Contact Master unreachable: {data.get('message', '')[:100]}", False)
        return

    rows = data.get("contacts") if isinstance(data, dict) else None
    if rows is None:
        add(WARN, "Contact Master: unexpected response shape (no 'contacts' array)", False)
        return
    if len(rows) == 0:
        add(WARN, "Contact Master is EMPTY (0 contacts) — run /enrich to populate. "
                  "No phone/email coverage to measure yet.", False)
        return

    total = len(rows)
    with_mobile = sum(1 for r in rows if r.get("mobile"))
    with_email = sum(1 for r in rows if r.get("email") and not str(r.get("email", "")).startswith("info@"))
    with_phone_only = sum(1 for r in rows if r.get("phone") and not r.get("mobile"))
    with_both = sum(1 for r in rows if r.get("mobile") and r.get("email"))

    mobile_pct = round(with_mobile / total * 100) if total else 0
    email_pct = round(with_email / total * 100) if total else 0
    both_pct = round(with_both / total * 100) if total else 0

    mobile_level = OK if mobile_pct >= 20 else WARN if mobile_pct >= 5 else BAD
    email_level = OK if email_pct >= 30 else WARN if email_pct >= 10 else BAD

    add(mobile_level,
        f"Phone coverage: {with_mobile}/{total} contacts with mobile ({mobile_pct}%) "
        f"+ {with_phone_only} landline-only", False)
    add(email_level,
        f"Email coverage: {with_email}/{total} contacts with personal email ({email_pct}%)", False)
    if mobile_pct >= 20 and email_pct >= 30:
        add(OK, f"Contact quality healthy — {both_pct}% of contacts have BOTH mobile + email", False)


def check_company_dedup(add, sheet):
    """FIX: pass sheet= to load CRM snapshot (was sheet=None → only 22 local)."""
    try:
        from adapters import CompanyDedup
        dedup = CompanyDedup(sheet=sheet) if sheet else CompanyDedup(sheet=None)   # ← FIX
        n = len(dedup)
        if n == 0:
            add(WARN, "CompanyDedup empty — pre-check will not save quota until first run repopulates it", False)
        elif n < 100:
            add(WARN, f"CompanyDedup only {n} companies — pre-check coverage is thin "
                      f"(local cache only; CRM snapshot may have failed to load)", False)
        else:
            add(OK, f"CompanyDedup cache: {n} companies (pre-check armed)", False)
    except Exception as e:
        add(WARN, f"CompanyDedup cache check failed: {str(e)[:80]}", False)


def check_company_watchlist(add, sheet):
    """Verify company_watchlist is set in Settings and returned by Apps Script.

    Without this, adapters/company.py can't crawl any buyer URLs — it will
    always return 0 leads even though the code path is wired correctly.
    """
    try:
        st = sheet.get("get_settings")
    except Exception as e:
        add(WARN, f"Cannot check company_watchlist (settings unavailable): {str(e)[:80]}", False)
        return

    if not isinstance(st, dict):
        add(WARN, "Settings response shape is not a dict — company_watchlist unknown", False)
        return

    wl = st.get("company_watchlist")
    if not isinstance(wl, list):
        add(BAD,
            "Apps Script does NOT return 'company_watchlist' — adapters/company.py will produce 0 leads. "
            "Add `company_watchlist: []` and the colMap entry in doGet → get_settings.",
            False)
        return

    urls = [u for u in wl if isinstance(u, str) and u.strip()]
    if not urls:
        add(WARN,
            "company_watchlist is EMPTY in Settings — adapters/company.py will produce 0 leads. "
            "Add 20+ buyer URLs (NHAI, CPWD, MHADA, etc.) to the 'Company Watchlist' column.",
            False)
    elif len(urls) < 5:
        add(WARN, f"company_watchlist has only {len(urls)} URL(s) — add 20+ for good coverage", False)
    else:
        add(OK, f"company_watchlist: {len(urls)} URL(s) configured", False)


def main(live=True):
    started = time.time()
    lines, state = [], {"critical": 0}

    def add(level, msg, critical=False):
        lines.append(f"{level} {msg}")
        if level == BAD and critical:
            state["critical"] += 1
        log.info(f"[health] {level} {msg}")

    check_libs(add)
    check_sources(add)                      # ← NEW: sources/ modules + Playwright browser
    llm = check_keys(add)

    sheet = None
    try:
        t0 = time.time()
        sheet = SheetAPI()
        cfg = sheet.get("get_settings")
        n_targets = len(cfg.get("target_products") or [])
        add(OK if n_targets else WARN,
            f"Apps Script webhook OK in {time.time() - t0:.1f}s; "
            f"{n_targets} target product(s) in Settings", False)

        # Report Manual Extraction backlog
        try:
            manual_data = sheet.get("get_pending_leads")  # reuse existing action if available
            # Or skip this — healthcheck can be extended later
        except Exception:
            pass
    except SheetError as e:
        add(BAD, f"Apps Script webhook FAILED: {e} "
                 f"(check WEBHOOK_URL / WEBHOOK_SECRET and latest deployment)", True)

    if sheet:
        check_adapters(add, sheet)
        check_company_watchlist(add, sheet)    # ← NEW: verify watchlist is wired
        check_phone_coverage(add, sheet)

    check_company_dedup(add, sheet)

    if live:
        try:
            out = llm.generate_json('Return only this JSON: {"ok": true}', validator=lambda d: isinstance(d, dict))
            used = ", ".join(f"{k}:{v}" for k, v in llm.calls.items()) or "?"
            add(OK if out else BAD,
                f"Live LLM call {'worked' if out else 'FAILED - every provider failed (quota, bad keys or outage)'} ({used})",
                not out)
        except Exception as e:
            add(BAD, f"Live LLM call crashed: {e}", True)
        try:
            res = SearchEngine().search("public tender notice", country="india")
            add(OK if res else BAD,
                f"Live search returned {len(res or [])} result(s)" if res
                else "Live search returned NOTHING (keys exhausted or blocked)",
                not res)
        except Exception as e:
            add(BAD, f"Live search crashed: {e}", True)

    crit = state["critical"]
    head = (f"🩺 <b>Radar health check</b> - "
            f"{'ALL GOOD' if not any(l.startswith((BAD, WARN)) for l in lines) else ('PROBLEMS' if crit else 'warnings')}")
    full_msg = head + "\n" + "\n".join(tg_escape(l) for l in lines)
    Telegram().send(full_msg[:4000])
    RUN_INFO.update(stats={"critical": crit, "checks": len(lines)})
    if crit:
        RUN_INFO.update(degraded=True, degraded_reason=f"{crit} critical check(s) failed")
    print("\n".join(lines))
    return 1 if crit else 0


if __name__ == "__main__":
    sys.exit(run_job("health", main, RUN_INFO))
