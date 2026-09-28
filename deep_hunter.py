import os
import re
import json
import asyncio
import aiohttp
import requests
import dns.resolver
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

# --- UPGRADED DDGS IMPORT ---
try:
    from ddgs import AsyncDDGS
except ImportError:
    try:
        from duckduckgo_search import AsyncDDGS
    except ImportError:
        AsyncDDGS = None

WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
SECRET = os.environ.get("WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SERPER_KEY = os.environ.get("SERPER_API_KEY")

raw_keys = os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
current_key_index = 0

def get_next_gemini_client():
    global current_key_index
    if not GEMINI_KEYS: return None
    key = GEMINI_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
    return genai.Client(api_key=key)

# --- UPGRADED MODEL SELECTOR (Strict Version Parsing) ---
BEST_MODEL_CACHE = None

def get_best_gemini_model(client):
    global BEST_MODEL_CACHE
    if BEST_MODEL_CACHE: return BEST_MODEL_CACHE
    try:
        valid_models = []
        for m in client.models.list():
            name = m.name.lower()
            if re.match(r'^models/gemini-\d+\.\d+-flash$', name):
                valid_models.append(name)
                
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_CACHE = valid_models[0]
            return BEST_MODEL_CACHE
    except Exception:
        pass
        
    BEST_MODEL_CACHE = "gemini-2.5-flash"
    return BEST_MODEL_CACHE

async def is_b2b_email(email):
    if not email: return False
    personal_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "rediffmail.com"}
    try:
        domain = email.split('@')[-1].lower()
        if domain in personal_domains: return False
        def check_mx():
            dns.resolver.resolve(domain, 'MX')
            return True
        await asyncio.to_thread(check_mx)
        return True
    except Exception:
        return False

async def async_get_search_results(session, query, num=5):
    results = []
    if SERPER_KEY:
        try:
            url = "https://google.serper.dev/search"
            payload = json.dumps({"q": query, "gl": "in", "num": num})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            async with session.post(url, headers=headers, data=payload, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    for r in data.get("organic", []):
                        results.append({"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")})
                    if results: return results
        except Exception:
            pass

    if AsyncDDGS:
        try:
            ddgs = AsyncDDGS()
            res = await ddgs.text(query, max_results=num, backend="lite")
            for r in res:
                results.append({"title": r.get("title", ""), "link": r.get("href", ""), "snippet": r.get("body", "")})
        except Exception:
            pass
    return results

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_verify_entity_sync(org_name, web_results, li_results):
    client = get_next_gemini_client()
    if not client: return None
    best_model = get_best_gemini_model(client)
    
    prompt = f"""
You are an elite B2B Data Analyst. Target Organization: "{org_name}"

Task 1: Identify the OFFICIAL corporate website from 'Web Results'.
- REJECT directories (IndiaMart, JustDial, ZaubaCorp, Tofler).
- REJECT forums/social (GameFAQs, Reddit, Facebook, YouTube).
- If none exist, return null.

Task 2: Identify the DECISION MAKER from 'LinkedIn Results'.
- MUST be a human profile (linkedin.com/in/), NOT a company page.
- Look for: Director, Founder, CEO, Head of BIM, Procurement.
- Extract Name and Title. If uncertain, return null.

Web Results: {json.dumps(web_results)}
LinkedIn Results: {json.dumps(li_results)}
"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "verified_website": {"type": "STRING", "nullable": True},
            "dm_name": {"type": "STRING", "nullable": True},
            "dm_title": {"type": "STRING", "nullable": True}
        }
    }
    try:
        res = client.models.generate_content(
            model=best_model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0)
        )
        raw_text = res.text.strip()
        if raw_text.startswith("```"):
            raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
        return json.loads(raw_text)
    except Exception as e:
        raise e

async def async_crawl_contacts(session, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 200:
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' not in content_type: return [], []
                html = await response.read()
                text = html.decode('utf-8', errors='ignore')
                emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
                phones = re.findall(r"(?:\+91[- ]?|0)?[6-9]\d{9}\b", text)
                clean_emails = [e for e in set(emails) if not e.lower().endswith((".png", ".jpg", ".css", ".js", ".svg"))]
                clean_phones = [p for p in set(phones) if len(p.replace("+91", "").replace("-", "").replace(" ", "").strip()) >= 10]
                return clean_emails, clean_phones
    except Exception:
        pass
    return [], []

async def async_send_telegram(session, lead):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    
    # HTML Parsing is 100x safer against random character crashes like underscores in emails
    msg = f"🚀 <b>NEW QUALIFIED LEAD</b>\n\n" \
          f"🏢 <b>Company:</b> {lead.get('org', 'Unknown')}\n" \
          f"🎯 <b>Intent:</b> {lead.get('intent', 'Corporate Lead')}\n" \
          f"👤 <b>DM:</b> {lead.get('dm_name', 'N/A')} ({lead.get('dm_title', 'N/A')})\n" \
          f"✉️ <b>Email:</b> {lead.get('email', 'N/A')}\n" \
          f"📞 <b>Phone:</b> {lead.get('phone', 'N/A')}\n" \
          f"🌐 <b>Web:</b> {lead.get('website', 'N/A')}\n" \
          f"🔗 <b>Source:</b> <a href='{lead.get('link', '')}'>View Link</a>"
          
    reply_markup = {"inline_keyboard": [[{"text": "✅ Qualify", "callback_data": f"qualify_{lead['lead_id']}"}, {"text": "❌ Reject", "callback_data": f"reject_{lead['lead_id']}"}]]}
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": reply_markup, "disable_web_page_preview": True}
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            await response.read()
    except Exception:
        pass

async def process_lead(session, lead):
    print(f"\n[*] Enriching Target: {lead['org']}")
    web_task = async_get_search_results(session, f'"{lead["org"]}" official website india')
    li_task = async_get_search_results(session, f'"{lead["org"]}" (Director OR CEO OR Procurement) site:[linkedin.com/in/](https://linkedin.com/in/)')
    web_res, li_res = await asyncio.gather(web_task, li_task)
    
    if web_res or li_res:
        ai_data = await asyncio.to_thread(ai_verify_entity_sync, lead['org'], web_res, li_res)
        if ai_data:
            if ai_data.get("verified_website"): lead["website"] = ai_data["verified_website"]
            if ai_data.get("dm_name"):
                lead["dm_name"] = ai_data["dm_name"]
                lead["dm_title"] = ai_data.get("dm_title", "Decision Maker")
                
    if lead["website"] and lead["website"] != "N/A":
        emails, phones = await async_crawl_contacts(session, lead["website"])
        for e in emails:
            if await is_b2b_email(e):
                lead["email"] = e
                break
        if phones and lead["phone"] == "N/A": lead["phone"] = phones[0]

    payload = {"secret": SECRET, "action": "update_lead", "row_index": lead['row_index'], "sheet_name": lead.get('sheet_name', '📥 Inbox'), **lead}
    try:
        async with session.post(WEBHOOK, json=payload, timeout=10) as response:
            await response.read()
    except Exception:
        pass
        
    await async_send_telegram(session, lead)

async def hunt_async():
    print(">>> 🕵️‍♂️ DEEP HUNTER V2 ACTIVE (Full Async Engine)")
    if not WEBHOOK or not SECRET: return
    
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_pending"}, timeout=15)
        pending = res.json().get("pending_leads", [])
    except Exception:
        return
        
    if not pending:
        print("    -> No pending leads found. Sleeping.")
        return
        
    print(f"    -> Found {len(pending)} leads needing enrichment.")
    
    # THREAD SAFETY FIX: Pre-fetch the best AI model synchronously BEFORE launching threads
    client = get_next_gemini_client()
    if client: get_best_gemini_model(client)

    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_lead(session, lead) for lead in pending]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(hunt_async())
