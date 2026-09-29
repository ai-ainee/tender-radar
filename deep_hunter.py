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
        valid_models = []
        for m in client.models.list():
            name = m.name.lower()
            banned_keywords = ["audio", "tts", "image", "omni", "vision", "native", "preview", "thinking", "2.5"]
            if "flash" in name and not any(bad in name for bad in banned_keywords):
                valid_models.append(name)
        if valid_models:
            valid_models.sort(reverse=True)
            for preferred in ["models/gemini-3.5-flash-lite", "models/gemini-1.5-flash"]:
                if preferred in valid_models:
                    valid_models.insert(0, valid_models.pop(valid_models.index(preferred)))
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-3.5-flash-lite", "gemini-1.5-flash"]
    return BEST_MODEL_STACK

def generate_email_permutations(name, domain):
    if not name or name == "N/A" or not domain or domain == "N/A": return []
    parts = name.lower().replace(".", "").split()
    if not parts: return []
    f, l = parts[0], parts[-1] if len(parts) > 1 else ""
    clean_domain = domain.replace("www.", "").replace("http://", "").replace("https://", "").split("/")[0]
    perms = [f"{f}@{clean_domain}"]
    if l: perms.extend([f"{f}.{l}@{clean_domain}", f"{f[0]}{l}@{clean_domain}", f"{f}{l[0]}@{clean_domain}"])
    return perms

async def verify_domain_mx(domain):
    try:
        await asyncio.to_thread(dns.resolver.resolve, domain, 'MX')
        return True
    except Exception: return False

async def is_b2b_email(email):
    if not email: return False
    try:
        domain = email.split('@')[-1].lower()
        if domain in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}: return False
        return await verify_domain_mx(domain)
    except Exception: return False

async def async_get_search_results(session, query, num=5):
    results = []
    if SERPER_KEY:
        try:
            url = "https://google.serper.dev/search"
            async with session.post(url, headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}, data=json.dumps({"q": query, "gl": "in", "num": num}), timeout=25) as response:
                if response.status == 200:
                    for r in (await response.json()).get("organic", []):
                        results.append({"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")})
                    if results: return results
        except Exception: pass
    if AsyncDDGS:
        try:
            async def fetch_ddgs(): return await AsyncDDGS().text(query, max_results=num, backend="lite")
            res = await asyncio.wait_for(fetch_ddgs(), timeout=12.0)
            for r in res: results.append({"title": r.get("title", ""), "link": r.get("href", ""), "snippet": r.get("body", "")})
        except Exception: pass
    return results

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_verify_entity_sync(org_name, web_results, li_results, legal_results, b2b_results):
    client = get_next_gemini_client()
    if not client: return None
    model_stack = get_flash_model_stack(client)
    
    prompt = f"""
You are an expert OSINT Triangulation Analyst investigating: "{org_name}"
Extract the OFFICIAL corporate domain, primary Decision Maker, and official phone/email.
Web: {json.dumps(web_results)}
LinkedIn: {json.dumps(li_results)}
Corporate Registries: {json.dumps(legal_results)}
Directories: {json.dumps(b2b_results)}
"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "verified_website": {"type": "STRING", "nullable": True},
            "dm_name": {"type": "STRING", "nullable": True},
            "dm_title": {"type": "STRING", "nullable": True},
            "directory_phone": {"type": "STRING", "nullable": True},
            "directory_email": {"type": "STRING", "nullable": True}
        }
    }
    
    for model_name in model_stack:
        try:
            chat = client.chats.create(model=model_name)
            res = chat.send_message(prompt, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0))
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            return json.loads(raw_text)
        except Exception: continue
    return None

async def async_crawl_contacts(session, url):
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, ssl=False, timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=10)) as response:
            if response.status == 200:
                text = (await response.read()).decode('utf-8', errors='ignore')
                emails = [e for e in set(re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)) if not e.lower().endswith((".png", ".jpg", ".css", ".js"))]
                phones = [p for p in set(re.findall(r"(?:\+91[- ]?|0)?[6-9]\d{9}\b", text)) if len(p.replace("+91", "").replace("-", "").replace(" ", "").strip()) >= 10]
                return emails, phones
    except Exception: pass
    return [], []

async def process_lead(session, lead, sem):
    async with sem:
        print(f"[*] Triangulating: {lead['org']}", flush=True)
        
        web_res, li_res, legal_res, b2b_res = await asyncio.gather(
            async_get_search_results(session, f'"{lead["org"]}" official website india'),
            async_get_search_results(session, f'site:linkedin.com/in/ ("Procurement" OR "Director" OR "CEO") "{lead["org"]}"'),
            async_get_search_results(session, f'(site:zaubacorp.com OR site:thecompanycheck.com) "{lead["org"]}" directors'),
            async_get_search_results(session, f'(site:indiamart.com OR site:justdial.com) "{lead["org"]}" contact')
        )
        
        ai_data = await asyncio.to_thread(ai_verify_entity_sync, lead['org'], web_res, li_res, legal_res, b2b_res)
        
        if ai_data:
            if ai_data.get("verified_website"): lead["website"] = ai_data["verified_website"]
            if ai_data.get("dm_name"): lead["dm_name"], lead["dm_title"] = ai_data["dm_name"], ai_data.get("dm_title", "Decision Maker")
            if ai_data.get("directory_email") and not await is_b2b_email(lead.get("email", "")): lead["email"] = ai_data["directory_email"]
            if ai_data.get("directory_phone") and lead.get("phone", "N/A") == "N/A": lead["phone"] = ai_data["directory_phone"]

        if lead.get("website") and lead["website"] != "N/A":
            emails, phones = await async_crawl_contacts(session, lead["website"])
            if not emails and lead.get("dm_name") != "N/A":
                perms = generate_email_permutations(lead["dm_name"], lead["website"])
                if perms: emails = perms
            for e in emails:
                if await is_b2b_email(e): lead["email"] = e; break
            if phones and lead.get("phone", "N/A") == "N/A": lead["phone"] = phones[0]

        for attempt in range(3):
            try:
                async with session.post(WEBHOOK, json={"secret": SECRET, "action": "update_lead", **lead}, timeout=30) as response: 
                    await response.read()
                    print(f"    ✅ Enriched Target: {lead['org']}", flush=True)
                    break
            except Exception: await asyncio.sleep(2)

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            msg = (
                f"🌟 <b>ENRICHED QUALIFIED TARGET</b>\n\n"
                f"🏢 <b>Company:</b> {lead.get('org', 'Unknown')}\n"
                f"📍 <b>Location:</b> {lead.get('city', 'Unknown')}, {lead.get('state', 'Pan-India')}\n"
                f"👤 <b>DM:</b> {lead.get('dm_name', 'N/A')} ({lead.get('dm_title', 'N/A')})\n"
                f"✉️ <b>Email:</b> {lead.get('email', 'N/A')}\n"
                f"📞 <b>Phone:</b> {lead.get('phone', 'N/A')}\n"
                f"🌐 <b>Web:</b> {lead.get('website', 'N/A')}"
            )
            payload = {
                "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [ [{"text": "🎯 Move to Leads", "callback_data": f"tolead_{lead['lead_id']}"}], [{"text": "❌ Reject", "callback_data": f"rejectqual_{lead['lead_id']}"}] ] }
            }
            try:
                async with session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30) as response: await response.read()
            except Exception: pass

async def hunt_async():
    print(">>> 🕵️‍♂️ DEEP HUNTER ACTIVE (17-Column Synchronized)", flush=True)
    if not WEBHOOK or not SECRET: return
    try:
        pending = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_pending"}, timeout=30).json().get("pending_leads", [])
    except Exception: return
    if not pending: return print("    -> No leads currently pending enrichment.", flush=True)

    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5)) as session:
        await asyncio.gather(*[process_lead(session, lead, sem) for lead in pending])

if __name__ == "__main__":
    asyncio.run(hunt_async())
