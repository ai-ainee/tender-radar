import os
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
        valid_models = [m.name.lower() for m in client.models.list() if "flash" in m.name.lower()]
        if valid_models:
            valid_models.sort(reverse=True)
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return BEST_MODEL_STACK

def generate_email_permutations(name, domain):
    if not name or name == "N/A" or not domain or domain == "N/A": return []
    parts = name.lower().replace(".", "").split()
    if not parts: return []
    f = parts[0]
    l = parts[-1] if len(parts) > 1 else ""
    domain = domain.replace("www.", "").replace("http://", "").replace("https://", "").split("/")[0]
    perms = [f"{f}@{domain}"]
    if l: perms.extend([f"{f}.{l}@{domain}", f"{f[0]}{l}@{domain}", f"{f}{l[0]}@{domain}"])
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
            async with session.post(url, headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}, data=json.dumps({"q": query, "gl": "in", "num": num}), timeout=30) as response:
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
def ai_verify_entity_sync(org_name, web_results, li_results, legal_results, b2b_results, gov_results):
    client = get_next_gemini_client()
    if not client: return None
    model_stack = get_flash_model_stack(client)
    
    prompt = f"""
You are an elite B2B Data Analyst triangulating sources for: "{org_name}"
Identify OFFICIAL website domain. Extract best DECISION MAKER (Priority: Procurement > CEO > Director). Extract explicit B2B mobile numbers/emails.
Web: {json.dumps(web_results)}
LinkedIn: {json.dumps(li_results)}
Legal: {json.dumps(legal_results)}
B2B: {json.dumps(b2b_results)}
Gov: {json.dumps(gov_results)}
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
            res = client.models.generate_content(model=model_name, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0))
            raw_text = res.text.strip()
            # SAFE JSON STRIPPER
            if raw_text.startswith("```"):
                raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
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

async def process_lead(session, lead, sem):
    async with sem:
        print(f"[*] Triangulating: {lead['org']}", flush=True)
        
        web_res, li_res, legal_res, b2b_res, gov_res = await asyncio.gather(
            async_get_search_results(session, f'"{lead["org"]}" official website india'),
            async_get_search_results(session, f'site:linkedin.com/in/ ("Procurement" OR "Purchase" OR "CEO") "{lead["org"]}"'),
            async_get_search_results(session, f'(site:zaubacorp.com OR site:thecompanycheck.com) "{lead["org"]}" directors'),
            async_get_search_results(session, f'(site:indiamart.com OR site:justdial.com) "{lead["org"]}" contact'),
            async_get_search_results(session, f'(site:eprocure.gov.in OR site:gem.gov.in) "{lead["org"]}"')
        )
        
        ai_data = await asyncio.to_thread(ai_verify_entity_sync, lead['org'], web_res, li_res, legal_res, b2b_res, gov_res)
        
        if ai_data:
            if ai_data.get("verified_website"): lead["website"] = ai_data["verified_website"]
            if ai_data.get("dm_name"): lead["dm_name"], lead["dm_title"] = ai_data["dm_name"], ai_data.get("dm_title", "Decision Maker")
            if ai_data.get("directory_email") and not await is_b2b_email(lead.get("email", "")): lead["email"] = ai_data["directory_email"]
            if ai_data.get("directory_phone") and lead.get("phone", "N/A") == "N/A": lead["phone"] = ai_data["directory_phone"]

        if lead["website"] and lead["website"] != "N/A":
            emails, phones = await async_crawl_contacts(session, lead["website"])
            if not emails and lead["dm_name"] != "N/A":
                perms = generate_email_permutations(lead["dm_name"], lead["website"])
                if perms: emails = perms
            for e in emails:
                if await is_b2b_email(e):
                    lead["email"] = e
                    break
            if phones and lead.get("phone", "N/A") == "N/A": lead["phone"] = phones[0]

        try:
            async with session.post(WEBHOOK, json={"secret": SECRET, "action": "update_lead", "lead_id": lead['lead_id'], **lead}, timeout=60) as response: 
                await response.read()
                print(f"    ✅ Enriched: {lead['org']}", flush=True)
        except Exception: pass
        
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            msg = f"🌟 <b>ENRICHED QUALIFIED LEAD</b>\n\n🏢 <b>Company:</b> {lead.get('org', 'Unknown')}\n👤 <b>DM:</b> {lead.get('dm_name', 'N/A')} ({lead.get('dm_title', 'N/A')})\n✉️ <b>Email:</b> {lead.get('email', 'N/A')}\n📞 <b>Phone:</b> {lead.get('phone', 'N/A')}\n🌐 <b>Web:</b> {lead.get('website', 'N/A')}"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": [[{"text": "🚀 Move to Pipeline", "callback_data": f"pipeline_{lead['lead_id']}"}], [{"text": "🗑️ Drop", "callback_data": f"dropqual_{lead['lead_id']}"}]]}}
            try:
                async with session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30) as response: await response.read()
            except Exception: pass

async def hunt_async():
    print(">>> 🕵️‍♂️ DEEP HUNTER ACTIVE (Safe Regex & Retry Version)", flush=True)
    if not WEBHOOK or not SECRET: return
    
    pending = []
    for attempt in range(3):
        try:
            pending = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_pending"}, timeout=60).json().get("pending_leads", [])
            break
        except Exception as e:
            print(f"    ⚠️ Sheets API timeout. Retrying {attempt+1}/3...", flush=True)
            time.sleep(5)
            if attempt == 2: return print("❌ Failed to fetch pending leads.", flush=True)
            
    if not pending: return print("    -> No pending leads found.", flush=True)
        
    client = get_next_gemini_client()
    if client: get_flash_model_stack(client)

    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5)) as session:
        await asyncio.gather(*[process_lead(session, lead, sem) for lead in pending])

if __name__ == "__main__":
    asyncio.run(hunt_async())
