import os
import re
import json
import logging
import warnings
import asyncio
import aiohttp
import requests
import dns.resolver
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

warnings.filterwarnings("ignore")
logging.getLogger("google.genai.models").setLevel(logging.ERROR)

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

BEST_MODEL_STACK = []
def get_flash_model_stack(client):
    global BEST_MODEL_STACK
    if BEST_MODEL_STACK: return BEST_MODEL_STACK
    try:
        valid_models = [m.name.lower() for m in client.models.list() if re.match(r'^models/gemini-\d+\.\d+-flash$', m.name.lower())]
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return BEST_MODEL_STACK

async def is_b2b_email(email):
    if not email: return False
    try:
        domain = email.split('@')[-1].lower()
        if domain in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}: return False
        await asyncio.to_thread(dns.resolver.resolve, domain, 'MX')
        return True
    except Exception: return False

async def async_get_search_results(session, query, num=5):
    results = []
    if SERPER_KEY:
        try:
            url = "[https://google.serper.dev/search](https://google.serper.dev/search)"
            async with session.post(url, headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}, data=json.dumps({"q": query, "gl": "in", "num": num}), timeout=20) as response:
                if response.status == 200:
                    for r in (await response.json()).get("organic", []):
                        results.append({"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")})
                    if results: return results
        except Exception: pass
    if AsyncDDGS:
        try:
            async def fetch_ddgs(): return await AsyncDDGS().text(query, max_results=num, backend="lite")
            res = await asyncio.wait_for(fetch_ddgs(), timeout=15.0)
            for r in res: results.append({"title": r.get("title", ""), "link": r.get("href", ""), "snippet": r.get("body", "")})
        except Exception: pass
    return results

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_verify_entity_sync(org_name, web_results, li_results):
    client = get_next_gemini_client()
    if not client: return None
    model_stack = get_flash_model_stack(client)
    prompt = f"Target: '{org_name}'. Task 1: Identify OFFICIAL website from Web Results. Reject directories. Task 2: Identify DECISION MAKER (Procurement, CEO, Founder) from LinkedIn Results. Web: {json.dumps(web_results)}. LinkedIn: {json.dumps(li_results)}"
    schema = {"type": "OBJECT", "properties": {"verified_website": {"type": "STRING", "nullable": True}, "dm_name": {"type": "STRING", "nullable": True}, "dm_title": {"type": "STRING", "nullable": True}}}
    
    for model_name in model_stack:
        try:
            res = client.models.generate_content(model=model_name, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0))
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
            return json.loads(raw_text)
        except Exception as e:
            if "503" in str(e) or "500" in str(e) or "limit: 0" in str(e): continue
            raise e
    raise Exception("Models unavailable.")

async def async_crawl_contacts(session, url):
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=10)) as response:
            if response.status == 200:
                text = (await response.read()).decode('utf-8', errors='ignore')
                emails = [e for e in set(re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)) if not e.lower().endswith((".png", ".jpg", ".css", ".js"))]
                phones = [p for p in set(re.findall(r"(?:\+91[- ]?|0)?[6-9]\d{9}\b", text)) if len(p.replace("+91", "").replace("-", "").replace(" ", "").strip()) >= 10]
                return emails, phones
    except Exception: pass
    return [], []

async def async_send_telegram(session, lead):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    msg = f"🌟 <b>ENRICHED QUALIFIED LEAD</b>\n\n🏢 <b>Company:</b> {lead.get('org', 'Unknown')}\n👤 <b>DM:</b> {lead.get('dm_name', 'N/A')} ({lead.get('dm_title', 'N/A')})\n✉️ <b>Email:</b> {lead.get('email', 'N/A')}\n📞 <b>Phone:</b> {lead.get('phone', 'N/A')}\n🌐 <b>Web:</b> {lead.get('website', 'N/A')}"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": [[{"text": "🚀 Move to Pipeline", "callback_data": f"pipeline_{lead['lead_id']}"}], [{"text": "🗑️ Drop", "callback_data": f"dropqual_{lead['lead_id']}"}]]}}
    try:
        async with session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30) as response: await response.read()
    except Exception: pass

async def process_lead(session, lead, sem):
    async with sem:
        print(f"[*] Enriching Target: {lead['org']}", flush=True)
        web_res, li_res = await asyncio.gather(async_get_search_results(session, f'"{lead["org"]}" official website india'), async_get_search_results(session, f'"{lead["org"]}" (Procurement OR Purchase OR Sourcing OR CEO) site:linkedin.com/in/'))
        if web_res or li_res:
            ai_data = await asyncio.to_thread(ai_verify_entity_sync, lead['org'], web_res, li_res)
            if ai_data:
                if ai_data.get("verified_website"): lead["website"] = ai_data["verified_website"]
                if ai_data.get("dm_name"): lead["dm_name"], lead["dm_title"] = ai_data["dm_name"], ai_data.get("dm_title", "Decision Maker")
                    
        if lead["website"] and lead["website"] != "N/A":
            emails, phones = await async_crawl_contacts(session, lead["website"])
            for e in emails:
                if await is_b2b_email(e):
                    lead["email"] = e
                    break
            if phones and lead.get("phone", "N/A") == "N/A": lead["phone"] = phones[0]

        try:
            async with session.post(WEBHOOK, json={"secret": SECRET, "action": "update_lead", "lead_id": lead['lead_id'], **lead}, timeout=30) as response: 
                await response.read()
                print(f"    ✅ Updated: {lead['org']}", flush=True)
        except Exception: pass
        await async_send_telegram(session, lead)

async def hunt_async():
    print(">>> 🕵️‍♂️ DEEP HUNTER ACTIVE (Production)", flush=True)
    if not WEBHOOK or not SECRET: return
    try: pending = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_pending"}, timeout=30).json().get("pending_leads", [])
    except Exception as e: return print(f"❌ Failed to fetch pending leads: {e}", flush=True)
    if not pending: return print("    -> No pending leads found.", flush=True)
        
    client = get_next_gemini_client()
    if client: get_flash_model_stack(client)

    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5)) as session:
        await asyncio.gather(*[process_lead(session, lead, sem) for lead in pending])

if __name__ == "__main__":
    asyncio.run(hunt_async())
