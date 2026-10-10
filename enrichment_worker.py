#!/usr/bin/env python3
"""
enrichment_worker.py - finds the decision maker + contact details for new leads, writes a dossier
back to the Google Sheet and notifies you on Telegram (with action buttons).
Contact details are only accepted when they actually appear in the research text (no hallucinated emails/phones).
"""
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

from common import (LLMRouter, SearchEngine, SheetAPI, SheetError, Telegram, env_int, extract_emails, extract_phones,
                    fetch_page, log, tg_escape)


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def sanitize_contacts(intel, osint_text):
    """Keep only contact data that is grounded in the research text."""
    low = osint_text.lower()
    low_digits = digits(osint_text)

    website = str(intel.get("website") or "").strip()
    if website and not website.lower().startswith("http"):
        website = "https://" + website
    if website and not re.match(r"^https?://[^\s]+\.[^\s]+$", website):
        website = ""

    email = str(intel.get("email") or "").strip().lower()
    candidates = extract_emails(osint_text)
    if email and email not in low:
        email = ""
    if not email and candidates:
        host = re.sub(r"^https?://(www\.)?", "", website).split("/")[0].lower() if website else ""
        pick = [c for c in candidates if host and c.endswith("@" + host)] or candidates
        email = pick[0]

    phone = str(intel.get("phone") or "").strip()
    pd = digits(phone)
    if pd.startswith("91") and len(pd) == 12:
        pd = pd[2:]
    if not pd or pd not in low_digits:
        phone = ""
    else:
        phone = pd if len(pd) == 10 else phone
    if not phone:
        mobiles = extract_phones(osint_text, 1)
        phone = mobiles[0] if mobiles else ""

    linkedin = str(intel.get("linkedin_url") or "").strip()
    if linkedin and ("linkedin.com" not in linkedin.lower() or linkedin.lower() not in low):
        linkedin = ""

    return {"email": email, "phone": phone, "website": website, "linkedin_url": linkedin}


def analyze(llm, company, ref_url, osint_text):
    prompt = f"""You are an elite executive headhunter and senior B2B account strategist.
TARGET ORGANIZATION: {company}
REFERENCE URL: {ref_url}
TODAY: {datetime.now().strftime('%Y-%m-%d')}

OSINT RESEARCH DATA (the ONLY source of truth):
{osint_text[:11000]}

TASKS
1. Identify the single highest-value decision maker (Director, CEO, MD, VP Procurement, Chief Project Engineer,
   Head of Engineering, BIM lead...). If no person is named in the data use "Procurement Head" as dm_name.
2. Write an executive-ready dossier for pitching commercial solutions.

STRICT RULES
- NEVER invent names, emails, phone numbers or URLs. Use ONLY values that literally appear in the research data;
  otherwise return an empty string.
- Return ONLY JSON with this schema:
{{
  "dm_name": "string",
  "dm_title": "string",
  "email": "string (empty if not in data)",
  "phone": "string (empty if not in data)",
  "website": "official company website (empty if unknown)",
  "linkedin_url": "profile/company page URL (empty if not in data)",
  "dossier": "Markdown string with exactly these sections:\\n### 🏢 Executive Profile & Operations\\n### ⚙️ Current Capex, Project Signals & Expansion\\n### 🎯 Key Buying Triggers & Operational Bottlenecks\\n### 🚀 Ready-to-Send Cold Outreach Pitch"
}}"""
    return llm.generate_json(prompt, validator=lambda d: isinstance(d, dict) and ("dossier" in d or "dm_name" in d))


def main():
    started = time.time()
    tg = Telegram()
    log.info("=== Enrichment worker starting ===")
    try:
        sheet = SheetAPI()
        data = sheet.get("get_pending_leads", limit=env_int("MAX_ENRICH_PER_RUN", 15))
    except SheetError as e:
        log.error(f"Cannot fetch pending leads: {e}")
        tg.send(f"🚨 <b>Enrichment halted</b>\n<code>{tg_escape(e)}</code>")
        return 1

    leads = data.get("leads") or []
    if not leads:
        log.info("No leads waiting for enrichment.")
        return 0

    llm = LLMRouter()
    if not llm.available():
        tg.send("🚨 <b>Enrichment halted</b>: no LLM API key configured.")
        return 1
    search = SearchEngine()
    max_runtime = env_int("MAX_RUNTIME_MIN", 25) * 60
    done = failed = 0
    log.info(f"{len(leads)} leads awaiting enrichment")

    for lead in leads:
        if time.time() - started > max_runtime:
            log.warning("Time budget reached - remaining leads will be picked up next run.")
            break
        lead_id = str(lead.get("lead_id") or "").strip()
        org = str(lead.get("organization") or "").strip()
        ref_url = str(lead.get("url") or "").strip()
        if not lead_id or not org:
            continue
        log.info(f"Enriching {org} ({lead_id})")

        snippets = []
        for q in (f'{org} Director OR CEO OR "Managing Director" OR Procurement OR "Head of Projects" linkedin',
                  f"{org} corporate office contact email phone website"):
            for item in search.search(q, country="India", pages=1)[:6]:
                snippets.append(f"{item.get('title', '')}\n{item.get('snippet', '')}\nLink: {item.get('link', '')}")
        scraped = fetch_page(ref_url) if ref_url else ""
        osint = "\n\n".join(snippets) + ("\n\nREFERENCE PAGE TEXT:\n" + scraped if scraped else "")
        if len(osint.strip()) < 40:
            log.warning(f"No research data found for {org}; skipping this run.")
            failed += 1
            continue

        intel = analyze(llm, org, ref_url, osint)
        if not intel:
            log.warning(f"AI could not synthesise intel for {org} - will retry next run.")
            failed += 1
            continue

        c = sanitize_contacts(intel, osint)
        dm_name = str(intel.get("dm_name") or "Procurement Head").strip() or "Procurement Head"
        dm_title = str(intel.get("dm_title") or "").strip()
        dossier = intel.get("dossier") or ""
        if not isinstance(dossier, str):
            dossier = str(dossier)
        if not dossier.strip():
            dossier = f"### 🏢 Executive Profile & Operations\n{org}\n\n_No further intelligence could be verified._"

        try:
            res = sheet.post("update_lead_dossier", lead_id=lead_id, dossier=dossier,
                             contacts={"dm_name": dm_name, "dm_title": dm_title, "email": c["email"],
                                       "phone": c["phone"], "website": c["website"], "linkedin_url": c["linkedin_url"]})
            log.info(f"   saved: {res.get('message', 'ok')}")
        except SheetError as e:
            log.error(f"   could not save dossier for {org}: {e}")
            failed += 1
            continue
        try:
            sheet.post("upsert_contact", contact_data={
                "company_name": org, "linkedin_url": c["linkedin_url"],
                "row_array": [lead_id, dm_name, org, dm_title, c["phone"], c["email"], c["linkedin_url"], ""]})
        except SheetError as e:
            log.warning(f"   contact upsert failed: {e}")
        done += 1

        buttons = []
        mobile = extract_phones(c["phone"], 1)
        if mobile:
            wa = quote(f"Hi {dm_name if dm_name != 'Procurement Head' else 'Team'}, I came across your recent "
                       f"project activity at {org}. Would love to share how we can help.")
            buttons.append([{"text": "💬 WhatsApp", "url": f"https://wa.me/91{mobile[0]}?text={wa}"}])
        buttons.append([{"text": "✅ Contacted", "callback_data": f"contacted|{lead_id}"},
                        {"text": "🔁 Re-enrich", "callback_data": f"reenrich|{lead_id}"},
                        {"text": "🗑 Discard", "callback_data": f"discard|{lead_id}"}])
        msg = [f"🎯 <b>LEAD ENRICHED: {tg_escape(org)}</b>", "",
               f"👤 <b>Contact:</b> {tg_escape(dm_name)}" + (f" (<i>{tg_escape(dm_title)}</i>)" if dm_title else "")]
        if c["phone"]:
            msg.append(f"📞 <b>Phone:</b> {tg_escape(c['phone'])}")
        if c["email"]:
            msg.append(f"✉️ <b>Email:</b> {tg_escape(c['email'])}")
        if c["website"]:
            msg.append(f"🌐 <b>Website:</b> {tg_escape(c['website'])}")
        if c["linkedin_url"]:
            msg.append(f"🔗 <b>LinkedIn:</b> {tg_escape(c['linkedin_url'])}")
        msg.append("")
        msg.append(f"📋 Dossier saved to CRM. Send <code>/lead {tg_escape(org)}</code> to read it here.")
        tg.send("\n".join(msg), reply_markup={"inline_keyboard": buttons})
        time.sleep(1.5)

    duration = f"{int(time.time() - started)}s"
    try:
        sheet.post("report_run", job="enrich", stats={"enriched": done, "failed": failed, "duration": duration,
                                                      "llm_calls": llm.calls, "search_calls": search.calls})
    except SheetError:
        pass
    tg.send(f"🏁 <b>Enrichment complete</b>\n• Enriched: <code>{done}</code>\n• Failed / retry next run: <code>{failed}</code>"
            f"\n• Duration: <code>{duration}</code>")
    log.info(f"Enrichment done: {done} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
