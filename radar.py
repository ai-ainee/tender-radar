#!/usr/bin/env python3
"""
radar.py - Radar Scout harvester.
Reads settings from your Google Sheet, searches the web, scores pages with a free LLM,
and pushes qualified leads back into the Sheet. Sends a Telegram summary at the end.
"""
import hashlib
import os
import random
import sys
import time
import uuid
from datetime import datetime

from common import (LLMRouter, SearchEngine, SheetAPI, SheetError, Telegram, clean_url, env_int, extract_phone,
                    fetch_page, log, normalize_company, tg_escape)

CACHE_FILE = "seen_links.txt"
SHEET_INBOX = "📥 Inbox"
SHEET_REVIEW = "⚠️ Needs Review"
SHEET_PARTNERS = "🤝 Partners & Suppliers"
SHEET_TRASH = "🗑 AI_Trash"

TRACK_KEYS = ["TRACK_1_PUBLIC_TENDERS", "TRACK_2_CORPORATE_PROCUREMENT",
              "TRACK_3_BUSINESS_EXPANSION", "TRACK_4_COMMERCIAL_HIRING"]


class StopRun(Exception):
    """Raised to leave the campaign loops early (time budget, link budget, AI outage)."""


# --------------------------------------------------------------------------- cache
def load_cache(sheet):
    seen = set()
    if not os.path.exists(CACHE_FILE):
        open(CACHE_FILE, "a", encoding="utf-8").close()
    try:
        with open(CACHE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            seen.update(clean_url(line) for line in f if line.strip())
    except Exception as e:
        log.warning(f"Local cache unreadable: {e}")
    try:
        data = sheet.get("get_all_urls")
        seen.update(clean_url(u) for u in data.get("urls", []) if isinstance(u, str) and u.strip())
        log.info(f"Cache synchronised: {len(seen)} known links")
    except SheetError as e:
        log.warning(f"Cloud cache unavailable, continuing with local cache only: {e}")
    seen.discard("")
    return seen


def mark_seen(seen, url):
    c = clean_url(url)
    if not c or c in seen:
        return
    seen.add(c)
    try:
        with open(CACHE_FILE, "a", encoding="utf-8") as f:
            f.write(c + "\n")
    except Exception as e:
        log.warning(f"Could not write cache: {e}")


# --------------------------------------------------------------------------- settings
def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [x for x in value.replace("\n", ",").split(",")]
    return [str(x).strip() for x in value if str(x).strip()]


def load_settings(sheet):
    s = sheet.get("get_settings")
    return {
        "special_prompt": str(s.get("special_prompt") or "").strip(),
        "targets": _as_list(s.get("target_products")),
        "industries": _as_list(s.get("industry_keywords")),
        "countries": _as_list(s.get("target_countries")),
        "states": _as_list(s.get("target_states")),
        "banned_kw": [k.lower() for k in _as_list(s.get("banned_keywords"))],
        "banned_sites": [k.lower() for k in _as_list(s.get("banned_websites"))],
        "protected": [k.lower() for k in _as_list(s.get("protected_domains"))],
    }


# --------------------------------------------------------------------------- query generation
class QueryGenerator:
    def __init__(self, target, industry, country, states, llm, use_ai=True):
        self.target = target.strip()
        self.industry = industry.strip() if industry and industry != "ALL_SECTORS" else ""
        self.country = (country or "").strip()
        self.states = [s.strip() for s in states if s.strip()]
        self.year = datetime.now().year
        self.llm = llm
        self.use_ai = use_ai

    @staticmethod
    def _normalize(data):
        if not isinstance(data, dict):
            return {}
        for parent in ("tracks", "queries", "data", "result"):
            if isinstance(data.get(parent), dict):
                data = data[parent]
                break
        out = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not v:
                continue
            ck = "".join(ch for ch in str(k).upper() if ch.isalnum())
            queries = [str(x).strip() for x in v if str(x).strip()][:2]
            if not queries:
                continue
            for n, name in enumerate(TRACK_KEYS, start=1):
                label = name.split("_", 2)[2].replace("_", "")
                if f"TRACK{n}" in ck or label in ck:
                    out[name] = queries
                    break
        return out

    def _ai_tracks(self, prompt):
        if not self.use_ai or not self.llm.available():
            return {}
        data = self.llm.generate_json(prompt, validator=lambda d: isinstance(d, dict))
        return self._normalize(data)

    def _merge(self, ai, fallback):
        merged = dict(fallback)
        merged.update({k: v for k, v in ai.items() if v})
        return merged

    def build_special_tracks(self, special_prompt):
        log.info(f"Generating search queries for special directive: '{special_prompt}'")
        sp = special_prompt
        fb = {
            "TRACK_1_PUBLIC_TENDERS": [f"{sp} tender RFP {self.year}", f"{sp} procurement bid notice"],
            "TRACK_2_CORPORATE_PROCUREMENT": [f"{sp} vendor empanelment RFQ", f"{sp} supplier contract requirement"],
            "TRACK_3_BUSINESS_EXPANSION": [f"{sp} project expansion capex", f"{sp} new facility investment"],
            "TRACK_4_COMMERCIAL_HIRING": [f"{sp} company hiring team", f"{sp} careers specialist lead"],
        }
        prompt = f"""You are a B2B sales intelligence strategist.
MANDATORY CAMPAIGN DIRECTIVE: "{special_prompt}"
Generate web-search queries (2 per track, max 10 words each, plain text, no quotes/operators):
TRACK_1_PUBLIC_TENDERS: active public tenders / bids / RFPs matching the directive.
TRACK_2_CORPORATE_PROCUREMENT: private RFQs, vendor empanelment, supply contracts.
TRACK_3_BUSINESS_EXPANSION: capex, new plants, expansion signals.
TRACK_4_COMMERCIAL_HIRING: hiring/team-scaling signals.
Return ONLY JSON with exactly those 4 keys, each an array of 2 strings."""
        return self._merge(self._ai_tracks(prompt), fb)

    def build_tracks(self):
        state = random.choice(self.states) if self.states else ""
        t, ind, yr = self.target, self.industry, self.year
        loc = state or self.country
        loc_s = f" {loc}" if loc else ""
        ind_s = f"{ind} " if ind else ""
        fb = {
            "TRACK_1_PUBLIC_TENDERS": [f"{t} tender RFP {yr}{loc_s}", f"{t} procurement bid notice{loc_s}"],
            "TRACK_2_CORPORATE_PROCUREMENT": [f"{ind_s}{t} vendor empanelment supplier{loc_s}",
                                              f"{t} corporate RFQ requirement {yr}"],
            "TRACK_3_BUSINESS_EXPANSION": [f"{ind_s}{t} project expansion capex {yr}{loc_s}",
                                           f"{t} new plant facility construction{loc_s}"],
            "TRACK_4_COMMERCIAL_HIRING": [f"{t} hiring lead specialist{loc_s}", f"{ind_s}{t} team expansion careers {yr}"],
        }
        industry_line = f"INDUSTRY FOCUS: {ind}" if ind else "INDUSTRY: all commercial/industrial sectors"
        prompt = f"""You are a B2B sales intelligence strategist.
Generate diverse web-search queries to find enterprise buyers, tenders and organisations purchasing or expanding in '{t}'.
{industry_line}
{f"Geography: {self.country}" if self.country else ""}
{f"Region: {state}" if state else ""}
Rules: 2 queries per track, max 12 words each, plain text only (no site:, quotes, minus).
TRACK_1_PUBLIC_TENDERS: government tenders, bids, RFPs for '{t}'.
TRACK_2_CORPORATE_PROCUREMENT: private RFQs, vendor empanelment, supply contracts for '{t}'.
TRACK_3_BUSINESS_EXPANSION: capex, new plants, major project launches needing '{t}'.
TRACK_4_COMMERCIAL_HIRING: companies hiring specialists/heads/teams for '{t}'.
Return ONLY JSON with exactly those 4 keys, each an array of 2 strings."""
        return self._merge(self._ai_tracks(prompt), fb)


# --------------------------------------------------------------------------- AI evaluation
def _valid_verdicts(d):
    if isinstance(d, list):
        return all(isinstance(x, dict) for x in d)
    if isinstance(d, dict):
        return isinstance(d.get("leads"), list) or "item_index" in d
    return False


class Evaluator:
    def __init__(self, llm):
        self.llm = llm

    @staticmethod
    def _slice(text, max_chars=3000):
        text = (text or "").strip()
        if len(text) <= max_chars:
            return text
        return f"{text[:2200]}\n\n[...truncated...]\n\n{text[-800:]}"

    def evaluate(self, batch, target, industry, country, states, geo_rule, ban_rule, special=""):
        """Returns list of verdict dicts, or None if every AI route failed."""
        items = "\n".join(
            f"--- ITEM {i} ---\nTRACK: {x['track']}\nURL: {x['url']}\n<scraped_data>\n{self._slice(x.get('raw_text'))}\n</scraped_data>\n"
            for i, x in enumerate(batch))
        directive = (f"MANDATORY SPECIAL FOCUS DIRECTIVE: {special}\nOnly qualify prospects matching this directive.\n"
                     if special else "")
        ind_line = (f"INDUSTRY: {industry}" if industry and industry != "ALL_SECTORS"
                    else "INDUSTRY: any commercial / industrial sector (infer it)")
        prompt = f"""You are an elite B2B sales intelligence analyst judging genuine commercial buying intent.
TODAY'S DATE: {datetime.now().strftime('%Y-%m-%d')}
TARGET PRODUCT / SOLUTION: {target}
{directive}{ind_line}
{f"TARGET GEOGRAPHY: {country}" if country else ""}
{f"REGIONAL FOCUS: {', '.join(states)}" if states else ""}
{geo_rule}
{ban_rule}

OBJECTIVE: find real, identifiable companies / public bodies with an ACTIVE requirement, project, tender or
expansion matching: {special if special else target}.

VALID: government/PSU/municipal tenders, bids, RFPs; companies launching projects, expanding, procuring,
issuing RFQs / vendor empanelment, or hiring dedicated specialists for this function.
EXCLUDE (is_valid=false): schools/training/tutorials, tenders whose deadline is before today's date,
freelancers, blogs, generic articles without an identifiable buyer, and vendors merely advertising themselves
(use entity_role SELLER for those).

Return ONLY JSON: {{"leads": [ one object per ITEM ]}} where each object has:
"item_index": integer matching the ITEM number,
"is_valid": boolean,
"confidence": "HIGH" | "MEDIUM" | "LOW",
"entity_role": "BUYER" | "PROJECT_BUYER" | "SERVICE_USER" | "SELLER" | "IRRELEVANT",
"organization": official name of the company / procuring body,
"industry": string, "city": string, "state": string,
"target_solution": the specific requirement,
"why_engage_now": 2-3 sentences (signal, urgency, opportunity),
"product_usage": "Confirmed User" | "Prospective Buyer" | "Competitor" | "Unknown" plus short evidence,
"upcoming_events": tender closing date / milestone, or "Unknown",
"dm_name": contact person if stated in the text else "",
"phone": phone if stated in the text else ""
Never invent facts that are not in the scraped text.

BATCH:
{items}"""
        data = self.llm.generate_json(prompt, validator=_valid_verdicts)
        if data is None:
            return None
        if isinstance(data, dict):
            data = data.get("leads") if isinstance(data.get("leads"), list) else [data]
        return data


def map_verdicts(verdicts, n):
    out = {}
    for pos, v in enumerate(verdicts or []):
        if not isinstance(v, dict):
            continue
        try:
            idx = int(v.get("item_index"))
        except (TypeError, ValueError):
            idx = pos if len(verdicts) == n else None
        if idx is None or not 0 <= idx < n or idx in out:
            continue
        out[idx] = v
    return out


# --------------------------------------------------------------------------- routing into the sheet
class Router:
    def __init__(self, sheet):
        self.sheet = sheet

    def push(self, doc, ai, default_target, default_industry):
        """Returns (outcome, company). outcome in INBOX/REVIEW/PARTNER/TRASH/DUPLICATE/SKIPPED/FAILED."""
        company = normalize_company(str(ai.get("organization") or ""))
        if company.lower() in ("unknown", "unknown firm", ""):
            return "SKIPPED", company

        is_valid = ai.get("is_valid") in (True, "true", "True", "yes")
        confidence = str(ai.get("confidence") or "LOW").upper()
        role = str(ai.get("entity_role") or "IRRELEVANT").upper()
        if role == "IRRELEVANT":
            is_valid = False

        if is_valid:
            try:
                chk = self.sheet.post("pre_flight_check", company_name=company)
                if chk.get("exists") is True:
                    log.info(f"[-] Duplicate in CRM: {company}")
                    return "DUPLICATE", company
            except SheetError as e:
                log.warning(f"pre_flight_check failed (continuing): {e}")

        capture = datetime.now().strftime("%Y-%m-%d %H:%M")
        why = str(ai.get("why_engage_now") or "")
        solution = str(ai.get("target_solution") or default_target)
        phone = str(ai.get("phone") or "")
        if len(extract_phone(phone)) != 10:
            phone = extract_phone(doc.get("raw_text", ""))
        else:
            phone = extract_phone(phone)
        dm = str(ai.get("dm_name") or "")
        if default_industry and default_industry != "ALL_SECTORS":
            industry = default_industry
        else:
            industry = str(ai.get("industry") or "Commercial Enterprise")
        lead_id = f"{str(uuid.uuid4())[:8].upper()}::{hashlib.md5(doc['url'].encode()).hexdigest()[:10]}"

        if not is_valid:
            sheet_name, outcome = SHEET_TRASH, "TRASH"
            row = [capture, company, doc["url"], why, ""]
        elif role == "SELLER":
            sheet_name, outcome = SHEET_PARTNERS, "PARTNER"
            row = [lead_id, capture, company, str(ai.get("city") or ""), str(ai.get("state") or ""), solution,
                   doc["url"], "", "", "", ""]
        else:
            sheet_name = SHEET_REVIEW if confidence == "LOW" else SHEET_INBOX
            outcome = "REVIEW" if confidence == "LOW" else "INBOX"
            row = [capture, str(ai.get("upcoming_events") or "Unknown"), role, industry,
                   str(ai.get("state") or "N/A"), str(ai.get("city") or "N/A"), company, solution, why,
                   doc["url"], str(ai.get("product_usage") or "Unknown"), "", lead_id]

        log.info(f"[*] {company} [{role}/{confidence}] -> {sheet_name}")
        try:
            res = self.sheet.post("insert_lead", target_sheet=sheet_name, company_name=company, signal_brief=why,
                                  row_data=row, contact_phone=phone, dm_name=dm)
        except SheetError as e:
            log.error(f"insert_lead failed for {company}: {e}")
            return "FAILED", company
        if res.get("duplicate"):
            return "DUPLICATE", company
        return outcome, company


# --------------------------------------------------------------------------- main
def main():
    started = time.time()
    t0 = datetime.now()
    tg = Telegram()
    log.info("=== Radar Scout harvester starting ===")

    try:
        sheet = SheetAPI()
        cfg = load_settings(sheet)
    except SheetError as e:
        log.error(f"Cannot read settings: {e}")
        tg.send(f"🚨 <b>Radar Scout halted</b>\nCannot read Google Sheet settings:\n<code>{tg_escape(e)}</code>")
        return 1

    special = cfg["special_prompt"]
    if not cfg["targets"] and not special:
        msg = "No Target Products (and no Special Prompt) found in the Settings sheet."
        log.error(msg)
        tg.send(f"⚠️ <b>Radar Scout</b>: {tg_escape(msg)}")
        return 1

    llm = LLMRouter()
    if not llm.available():
        tg.send("🚨 <b>Radar Scout halted</b>: no LLM API key configured (add GEMINI_API_KEYS in GitHub secrets).")
        return 1
    search = SearchEngine()
    evaluator = Evaluator(llm)
    router = Router(sheet)
    seen = load_cache(sheet)

    country = cfg["countries"][0] if cfg["countries"] else ""
    states = cfg["states"]
    geo_rule = (f"GEOGRAPHY RULE: must have verifiable commercial operations or active requirements in {country}."
                if country else "")
    ban_rule = f"Ignore these domains: {', '.join(cfg['banned_sites'])}" if cfg["banned_sites"] else ""

    max_runtime = env_int("MAX_RUNTIME_MIN", 40) * 60
    max_links = env_int("MAX_LINKS_PER_RUN", 80)
    max_combos = env_int("MAX_COMBOS_PER_RUN", 12)
    pages = env_int("SEARCH_PAGES", 2)
    use_ai_queries = os.getenv("AI_QUERIES", "1").strip() != "0"

    # build campaigns
    campaigns = []
    if special:
        log.info(f"SPECIAL DIRECTIVE ACTIVE: {special}")
        gen = QueryGenerator(special, "Special Campaign", country, states, llm, use_ai_queries)
        campaigns.append((special, "Special Campaign", gen))
    else:
        industries = cfg["industries"] or ["ALL_SECTORS"]
        combos = [(t, i) for t in cfg["targets"] for i in industries]
        if len(combos) > max_combos:
            offset = (datetime.now().toordinal() * max_combos) % len(combos)
            combos = (combos[offset:] + combos[:offset])[:max_combos]
            log.info(f"Running {max_combos} of the product/industry combinations this time (rotating daily).")
        for t, i in combos:
            campaigns.append((t, i, QueryGenerator(t, i, country, states, llm, use_ai_queries)))

    stats = {"scanned": 0, "inbox": 0, "review": 0, "partners": 0, "trash": 0, "dupes": 0, "failed": 0}
    session_companies = set()
    pending = set()  # links already queued this run
    ai_failures = 0
    stop_reason = ""

    def check_budget():
        if time.time() - started > max_runtime:
            raise StopRun(f"time budget of {max_runtime // 60} min reached")
        if stats["scanned"] >= max_links:
            raise StopRun(f"link budget of {max_links} reached")

    try:
        for target, industry, gen in campaigns:
            check_budget()
            tracks = gen.build_special_tracks(special) if special else gen.build_tracks()
            for track_name, queries in tracks.items():
                docs = []
                for query in queries:
                    check_budget()
                    recent = any(w in query.lower() for w in ("tender", "rfp", "bid", "procurement"))
                    log.info(f"Searching: {query}")
                    results = search.search(query, country, pages=pages, recent=recent)
                    fresh = 0
                    for res in results:
                        raw = (res.get("link") or "").strip()
                        c = clean_url(raw)
                        if not raw or not c or c in seen or c in pending:
                            continue
                        pending.add(c)
                        check_budget()
                        stats["scanned"] += 1
                        fresh += 1
                        low = raw.lower()
                        protected = any(p in low for p in cfg["protected"] if p)
                        snippet = (res.get("snippet") or "").lower()
                        if not protected and (any(b in low for b in cfg["banned_sites"] if b) or
                                              any(k in snippet for k in cfg["banned_kw"] if k)):
                            mark_seen(seen, raw)
                            continue
                        content = fetch_page(raw, protected_domains=cfg["protected"])
                        if content and len(content) > 120:
                            docs.append({"track": track_name, "url": raw, "raw_text": content})
                        else:
                            mark_seen(seen, raw)  # unreadable page: do not retry forever
                    log.info(f"   {len(results)} results, {fresh} new")

                for i in range(0, len(docs), 4):
                    batch = docs[i:i + 4]
                    check_budget()
                    verdicts = evaluator.evaluate(batch, target, industry, country, states, geo_rule, ban_rule, special)
                    if verdicts is None:
                        ai_failures += 1
                        log.warning("AI evaluation failed for a batch - links left un-cached so they are retried next run.")
                        if ai_failures >= 2:
                            raise StopRun("all AI providers are exhausted / unreachable")
                        continue
                    ai_failures = 0
                    vmap = map_verdicts(verdicts, len(batch))
                    for idx, doc in enumerate(batch):
                        v = vmap.get(idx)
                        if v is None:
                            mark_seen(seen, doc["url"])
                            continue
                        comp = normalize_company(str(v.get("organization") or ""))
                        if comp in session_companies:
                            mark_seen(seen, doc["url"])
                            continue
                        outcome, company = router.push(doc, v, target, industry)
                        if outcome == "FAILED":
                            stats["failed"] += 1
                            continue  # not cached -> retried next run
                        mark_seen(seen, doc["url"])
                        if outcome in ("INBOX", "REVIEW", "PARTNER"):
                            session_companies.add(company)
                        key = {"INBOX": "inbox", "REVIEW": "review", "PARTNER": "partners", "TRASH": "trash",
                               "DUPLICATE": "dupes"}.get(outcome)
                        if key:
                            stats[key] += 1
    except StopRun as e:
        stop_reason = str(e)
        log.warning(f"Stopping early: {e}")
    except Exception as e:  # never die silently
        stop_reason = f"unexpected error: {e}"
        log.exception("Unexpected error")

    duration = str(datetime.now() - t0).split(".")[0]
    pushed = stats["inbox"] + stats["review"]
    log.info(f"Done: {stats} in {duration}")
    try:
        sheet.post("report_run", job="harvest", stats=dict(stats, duration=duration, stop_reason=stop_reason,
                                                           llm_calls=llm.calls, search_calls=search.calls))
    except SheetError:
        pass
    lines = ["🏁 <b>Radar Scout complete</b>",
             f"• Links scanned: <code>{stats['scanned']}</code>",
             f"• New leads → CRM: <code>{pushed}</code> (Inbox {stats['inbox']}, Review {stats['review']})",
             f"• Partners/suppliers: <code>{stats['partners']}</code> • Discarded: <code>{stats['trash']}</code>"
             f" • Duplicates: <code>{stats['dupes']}</code>",
             f"• Duration: <code>{duration}</code>"]
    if stats["failed"]:
        lines.append(f"• ⚠️ Sheet write failures: <code>{stats['failed']}</code> (will retry next run)")
    if stop_reason:
        lines.append(f"• ⛔ Stopped early: {tg_escape(stop_reason)}")
    tg.send("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
