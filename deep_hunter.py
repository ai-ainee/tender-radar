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
def ai_verify_entity_sync(org_name, web_results, li_results, legal_results, b2b_results, gov_results):
    client = get_next_gemini_client()
    if not client: return None
    model_stack = get_flash_model_stack(client)
    
    prompt = f"""
You are an elite B2B Data Analyst triangulating 5 directory sources for: "{org_name}"

Task 1: Identify the OFFICIAL corporate website domain from 'Web Results'.
Task 2: Identify the best DECISION MAKER. Priority order:
  A. Procurement/Purchasing Lead (from LinkedIn or Gov Directories)
  B. Founder/CEO (from LinkedIn)
  C. Official Director (from ZaubaCorp / TheCompanyCheck Legal Directories)
  D. Tender Inviting Authority (from Gov Directories)
Task 3: Extract explicitly listed B2B mobile numbers or emails from IndiaMART/TradeIndia/JustDial/Sulekha snippets.

Data Sources:
Web Results: {json.dumps(web_results)}
LinkedIn: {json.dumps(li_results)}
Legal Directories (ZaubaCorp): {json.dumps(legal_results)}
B2B Directories (IndiaMART/JustDial): {json.dumps(b2b_results)}
Gov Directories (GeM/CPPP): {json.dumps(gov_results)}
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
            if raw_text.startswith("```"): raw_text = re.sub(r'^
