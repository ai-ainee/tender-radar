import os
import json
import re
import asyncio
import aiohttp
import requests
import dns.resolver
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

# Fallback imports
try:
    from duckduckgo_search import AsyncDDGS
except ImportError:
    AsyncDDGS = None

# --- ENVIRONMENT VARIABLES ---
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

# --- 1. DPDP COMPLIANCE & EMAIL VALIDATION ---
async def is_b2b_email(email):
    """Rejects personal emails for legal compliance and validates MX records to prevent bounces."""
    personal_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}
    try:
        domain = email.split('@')[-1].lower()
        if domain in personal_domains:
            return False
        
        # Async DNS MX Check
        def check_mx():
            dns.resolver.resolve(domain, 'MX')
            return True
            
        await asyncio.to_thread(check_mx)
        return True
    except Exception:
        return False

# --- 2. ASYNC SEARCH ENGINE (Serper + DDG) ---
async def async_get_search_results(session, query, num=5):
    """Fetches search snippets asynchronously for the AI to read."""
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

# --- 3. AI ENTITY RESOLUTION ---
@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_verify_entity(org_name, web_results, li_results):
    """Forces Gemini to deduce the true website and DM from search context."""
    client = get_next_gemini_client()
    if not client: return None
    
    prompt = f"""
You are an elite B2B Data Analyst. 
Target Organization: "{org_name}"

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
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema, temperature=0.0
            )
        )
        
        raw_text = res.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            
        return json.loads(raw_text)
    except Exception as e:
        print(f"    ⚠️ AI Resolution Error: {e}")
        raise e

# --- 4. ASYNC WEBSITE CRAWLER ---
async def async_crawl_contacts(session, url):
    """Scrapes homepage safely to find emails and phones."""
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

# --- 5. TELEGRAM DELIVERY ---
def send_telegram(lead):
    """Sends the finalized lead to your phone with interactive buttons."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    
    msg = f"🚀 **NEW QUALIFIED LEAD**\n\n" \
          f"🏢 **Company:** {lead.get('org', 'Unknown')}\n" \
          f"🎯 **Intent:** {lead.get('intent', 'Corporate Lead')}\n" \
          f"👤 **DM:** {lead.get('dm_name', 'N/A')} ({lead.get('dm_title', 'N/A')})\n" \
          f"✉️ **Email:** {lead.get('email', 'N/A')}\n" \
          f"📞 **Phone:** {lead.get('phone', 'N/A')}\n" \
          f"🌐 **Web:** {lead.get('website', 'N/A')}\n" \
          f"🔗 **Source:** {lead.get('link', 'N/A')}"
          
    # Connects to the Apps Script handleTelegramClick function
    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Qualify", "callback_data": f"qualify_{lead['lead_id']}"},
            {"text": "❌ Reject", "callback_data": f"reject_{lead['lead_id']}"}
        ]]
    }
    
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "reply_markup": reply_markup,
        "disable_web_page_preview": True
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"    ⚠️ Telegram delivery failed: {e}")

# --- 6. ASYNC ORCHESTRATOR ---
async def process_lead(session, lead):
    print(f"\n[*] Enriching Target: {lead['org']}")
    
    # Run contextual searches simultaneously
    web_task = async_get_search_results(session, f'"{lead["org"]}" official website india')
    li_task = async_get_search_results(session, f'"{lead["org"]}" (Director OR CEO OR "Head of BIM" OR Procurement) site:[linkedin.com/in/](https://linkedin.com/in/)')
    web_res, li_res = await asyncio.gather(web_task, li_task)
    
    # AI Verify
    if web_res or li_res:
        ai_data = ai_verify_entity(lead['org'], web_res, li_res)
        if ai_data:
            if ai_data.get("verified_website"): lead["website"] = ai_data["verified_website"]
            if ai_data.get("dm_name"):
                lead["dm_name"] = ai_data["dm_name"]
                lead["dm_title"] = ai_data.get("dm_title", "Decision Maker")
                print(f"    ✅ AI Found DM: {lead['dm_name']}")
                
    # Direct Crawl
    if lead["website"] and lead["website"] != "N/A":
        print(f"    -> Crawling domain: {lead['website']}")
        emails, phones = await async_crawl_contacts(session, lead["website"])
        
        # Verify emails strictly
        for e in emails:
            if await is_b2b_email(e):
                lead["email"] = e
                print(f"    ✅ B2B Email Verified: {e}")
                break
                
        if phones and lead["phone"] == "N/A":
            lead["phone"] = phones[0]

    # Save to Google Sheets
    payload = {"secret": SECRET, "action": "update_lead", "row_index": lead['row_index'], **lead}
    try:
        requests.post(WEBHOOK, json=payload, timeout=10)
        print(f"    ✅ CRM Updated Successfully.")
    except Exception as e:
        print(f"    ❌ CRM Update Failed: {e}")
        
    # Send Telegram (Final Step)
    send_telegram(lead)


async def hunt_async():
    print(">>> 🕵️‍♂️ DEEP HUNTER V2 ACTIVE (Async + AI Validation)")
    
    if not WEBHOOK or not SECRET:
        print("❌ WEBHOOK or SECRET missing.")
        return

    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_pending"}, timeout=15)
        pending = res.json().get("pending_leads", [])
    except Exception as e:
        print(f"❌ Failed to fetch pending leads: {e}")
        return
        
    if not pending:
        print("    -> No pending leads found. Sleeping.")
        return
        
    print(f"    -> Found {len(pending)} leads needing enrichment.")

    # Limit to 5 concurrent connections so we don't trip anti-bot systems
    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_lead(session, lead) for lead in pending]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(hunt_async())
