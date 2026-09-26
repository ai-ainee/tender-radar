import os
import sys
import json
import re
import time
import uuid
import urllib.parse
import io
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import List

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    from googlesearch import search as google_organic_search
except ImportError:
    google_organic_search = None

def log(msg):
    print(msg, flush=True)

log(">>> ENTERPRISE RADAR ACTIVE: 4-PIPE ENGINE + WIDE DATA NET")

GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
PRODUCTS_FILE = "products.txt"
STATES_FILE = "states.txt"
NEGATIVE_FILE = "negative_keywords.txt"
SEEN_FILE = "seen_links.txt"
BLOCKED_SOURCES_FILE = "blocked_sources.txt"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"'
})

COMMERCIAL_PATTERNS = re.compile(
    r"\b(tender|rfp|bid|gem|eprocure|procurement|supply|quotation|eoi|nit|hiring|vacancy|"
    r"drafter|modeler|architect|engineer|capex|expansion|project win|contractor|consultancy|"
    r"rera|dpr|empanelment|ipo|merger|series a|series b|raises funding|acquired|software license|"
    r"tech upgrade|dealer|reseller|distributor|indiamart|training|partner|request for quotation|"
    r"vendor registration|supplier empanelment|gcc|capability center)\b",
    re.IGNORECASE
)

LOCATION_PATTERNS = re.compile(
    r"\b(Delhi|NCR|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|Noida|"
    r"Gurgaon|Gurugram|Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat|Telangana|Haryana|"
    r"Kerala|Rajasthan|Madhya Pradesh|Bihar|West Bengal|Andhra Pradesh|Punjab|Odisha|Assam)\b",
    re.IGNORECASE
)

TECH_STACK_PATTERNS = re.compile(
    r"\b(AutoCAD|Revit|Civil 3D|Navisworks|Fusion 360|Advance Steel|Inventor|3ds Max|Tekla|"
    r"STAAD|ETABS|SolidWorks|CATIA|Rhino|SketchUp|MicroStation|BIM 360|ACC)\b",
    re.IGNORECASE
)

SELLER_MARKERS = [
    "indiamart", "tradeindia", "dealer", "reseller", "distributor", "authorized partner",
    "training center", "course", "tutorial", "wholesale", "gold partner"
]

JUNK_MARKERS = [
    "housekeeping", "security guard", "catering", "canteen", "stationery", "taxi",
    "scrap", "medicines", "medical equipment", "sweeping", "printer cartridge", "photocopier",
    "coupon", "promo code", "shein", "porn", "casino", "betting", "retailmenot"
]

PORTAL_DOMAINS = [
    "google.com", "news.google.com", "linkedin.com", "naukri.com", "indeed.com",
    "foundit.in", "shine.com", "monsterindia.com", "economictimes.indiatimes.com",
    "moneycontrol.com", "business-standard.com", "livemint.com", "eprocure.gov.in",
    "gem.gov.in", "ireps.gov.in", "facebook.com", "twitter.com", "x.com",
    "adecco.com", "glassdoor.co.in", "ambitionbox.com", "justdial.com", "sulekha.com",
    "mycorporateinfo.com", "zaubacorp.com"
]

PORTAL_SUFFIX_REGEX = re.compile(
    r"\s*-\s*(Naukri\.com|LinkedIn|Indeed|Foundit|TimesJobs|Shine\.com|The Economic Times|Moneycontrol|Google News|Business Standard|Livemint)\s*$",
    re.IGNORECASE
)

STATE_MAP = {
    'ncr': 'Delhi/NCR', 'delhi': 'Delhi', 'new delhi': 'Delhi',
    'noida': 'Uttar Pradesh', 'greater noida': 'Uttar Pradesh', 'ghaziabad': 'Uttar Pradesh',
    'gurgaon': 'Haryana', 'gurugram': 'Haryana', 'faridabad': 'Haryana',
    'mumbai': 'Maharashtra', 'pune': 'Maharashtra', 'nagpur': 'Maharashtra', 'thane': 'Maharashtra',
    'bengaluru': 'Karnataka', 'bangalore': 'Karnataka',
    'chennai': 'Tamil Nadu', 'coimbatore': 'Tamil Nadu',
    'ahmedabad': 'Gujarat', 'vadodara': 'Gujarat', 'surat': 'Gujarat',
    'hyderabad': 'Telangana', 'kolkata': 'West Bengal',
    'jaipur': 'Rajasthan', 'lucknow': 'Uttar Pradesh', 'kochi': 'Kerala'
}

class APIKeyPool:
    def __init__(self):
        raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
        self.keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        self.current_index = 0

    def get_client(self):
        if not self.keys:
            return None
        from google import genai
        return genai.Client(api_key=self.keys[self.current_index])

    def rotate(self):
        if len(self.keys) > 1:
            old_idx = self.current_index
            self.current_index = (self.current_index + 1) % len(self.keys)
            log(f"    ⚠️ [Quota Switched]: API Key {old_idx + 1} -> Key {self.current_index + 1}")
            return True
        return False

KEY_POOL = APIKeyPool()

class LeadData(BaseModel):
    item_index: int
    is_lead: bool
    lead_type: str = Field(description="Govt Tender, Private Capex, Hiring Mandate, Corporate Lead, Suppliers, Active Private Buyer (RFQ), or Media News")
    org: str = Field(description="The actual hiring or buying corporate entity name. NOT a portal name.")
    org_website: str = Field(description="Official corporate website homepage of the company. NEVER a job portal or news URL.")
    entity_type: str = Field(description="Govt, PSU, Private, MNC, Startup, Training Institute, Channel Partner")
    industry: str
    hq: str
    est_size: str = Field(description="Estimated company or project size")
    dm_name: str = Field(description="Decision maker name if identified")
    dm_title: str = Field(description="Decision maker job title")
    email: str
    phone: str
    secondary_contact: str = Field(description="HR or secondary procurement contact")
    boardline: str
    project_name: str
    project_stage: str
    project_scale: str
    total_investment: str
    epc: str = Field(description="EPC Contractor / Builder")
    pmc: str = Field(description="Project Management Consultant")
    completion_date: str
    tender_id: str
    tender_value: str
    emd: str
    tender_fee: str
    pre_bid: str = Field(description="Pre-Bid Meeting Date")
    deadline: str = Field(description="Submission deadline")
    bid_opening: str
    contract_duration: str
    eligibility: str
    job_title: str = Field(description="Hiring job title if applicable")
    vacancies: str
    exp_level: str
    salary: str
    tech_stack: str = Field(description="CAD/BIM software stack")
    competitor: str = Field(description="Competitor software mentioned")
    buying_intent: str = Field(description="High, Medium, or Low")
    urgency: str = Field(description="Immediate, Warm, or Strategic Nurture")
    sales_action: str = Field(description="1-sentence actionable AI sales advice")
    pitch_angle: str = Field(description="1-sentence cold email opening line tailored to this signal")
    summary: str

class LeadBatchResponse(BaseModel):
    leads: List[LeadData]

def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return ["AutoCAD", "Revit", "Civil 3D"]
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")
        
def load_negative_keywords():
    if not os.path.exists(NEGATIVE_FILE):
        return []
    with open(NEGATIVE_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]

def load_blocked_sources():
    if not os.path.exists(BLOCKED_SOURCES_FILE):
        return []
    with open(BLOCKED_SOURCES_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]

def is_portal_url(url):
    if not url:
        return True
    url_lower = url.lower()
    return any(p in url_lower for p in PORTAL_DOMAINS) or "news.google" in url_lower

def clean_org_name(title):
    cleaned = PORTAL_SUFFIX_REGEX.sub("", title).strip()
    if "-" in cleaned:
        parts = [p.strip() for p in cleaned.split("-") if p.strip()]
        for p in reversed(parts):
            if not is_portal_url(p) and len(p) > 2 and not any(kw in p.lower() for kw in ["hiring", "urgent", "opening", "job", "vacancy"]):
                return p
        return parts[-1]
    return cleaned

def format_pubdate(pubdate_str):
    if not pubdate_str:
        return "Not Listed"
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return pubdate_str

def get_real_url(url):
    if "news.google.com" in url:
        try:
            if gnewsdecoder:
                dec = gnewsdecoder(url)
                if dec and dec.get("status"):
                    return dec.get("decoded_url")
        except Exception:
            pass
    return url

def free_b2b_enrichment(company_name, lead_type):
    data = {"name": "", "title": "", "url": "", "web": ""}
    if not DDGS or not company_name or len(company_name) < 3:
        return data
    if is_portal_url(company_name) or company_name.lower() in ["commercial buyer", "commercial enterprise", "target enterprise", "buyer entity"]:
        return data
        
    try:
        time.sleep(2)
        ddgs = DDGS()
        web_res = list(ddgs.text(f'"{company_name}" official company website india', max_results=3))
        if web_res:
            for res in web_res:
                candidate_url = res.get("href", "")
                if not is_portal_url(candidate_url):
                    parsed = urllib.parse.urlparse(candidate_url)
                    data["web"] = f"{parsed.scheme}://{parsed.netloc}"
                    break
            
        time.sleep(2)
        role_clause = '"Head of BIM" OR "Design Head" OR "Chief Architect" OR HR' if "hiring" in lead_type.lower() else 'Procurement OR "Purchase Manager" OR Director'
        li_res = list(ddgs.text(f'"{company_name}" ({role_clause}) site:linkedin.com/in/', max_results=1))
        if li_res:
            data["url"] = li_res[0].get("href", "")
            clean_title = li_res[0].get("title", "").replace(" | LinkedIn", "").replace(" - LinkedIn", "")
            data["name"] = clean_title.split(" - ")[0].split(" | ")[0]
            data["title"] = clean_title
            
    except Exception:
        pass
    return data

def extract_metadata_fast(url):
    """Fast extraction of Title and Description for Google Organic Links"""
    try:
        r = SESSION.get(url, timeout=5, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, 'html.parser')
            title = soup.title.string if soup.title else url
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            desc = meta_desc['content'] if meta_desc else ""
            return title.strip(), desc.strip()
    except Exception:
        pass
    return "Target Website", ""

def deep_scrape_content(url):
    if url.lower().endswith(".pdf"):
        try:
            r = SESSION.get(url, timeout=15, allow_redirects=True)
            if r.status_code == 200 and PdfReader:
                pdf = PdfReader(io.BytesIO(r.content))
                return "".join([(p.extract_text() or "") + " " for p in pdf.pages[:3]])[:15000]
        except Exception:
            return "[PDF Document]"
            
    if sync_playwright:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
                
                # FIXED: wait_until="domcontentloaded" prevents timeouts on ad-heavy sites
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000) 
                
                try:
                    page.evaluate("""
                        document.querySelectorAll('button').forEach(b => {
                            let txt = b.innerText.toLowerCase();
                            if(txt.includes('see more') || txt.includes('show more')) {
                                b.click();
                            }
                        });
                    """)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                
                content = page.content()
                browser.close()
                soup = BeautifulSoup(content, 'html.parser')
                for s in soup(["script", "style", "noscript", "header", "footer"]):
                    s.extract()
                return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:15000]
        except Exception as e:
            log(f"    ⚠️ [Playwright Fallback]: {e}")

    try:
        r = SESSION.get(url, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, 'html.parser')
            for s in soup(["script", "style", "noscript", "header", "footer"]):
                s.extract()
            return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:15000]
    except Exception:
        pass
        
    return ""

def push_to_sheet(payload):
    if not GOOGLE_SHEET_WEBHOOK:
        return "success"
    try:
        res = SESSION.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=15, allow_redirects=True)
        try:
            resp_data = res.json()
            if resp_data.get("result") == "duplicate_ignored":
                return "duplicate"
        except Exception:
            pass
        return "success"
    except Exception:
        return "error"

def send_telegram(text, lead_type="", lead_id=""):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return None, None

    ltype = (lead_type or "").lower()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if "buyer" in ltype or "rfq" in ltype: chat_id = os.environ.get("TELEGRAM_CHAT_ID_BUYERS") or chat_id
    elif "tender" in ltype or "govt" in ltype or "gem" in ltype: chat_id = os.environ.get("TELEGRAM_CHAT_ID_TENDERS") or chat_id
    elif "hiring" in ltype or "vacancy" in ltype: chat_id = os.environ.get("TELEGRAM_CHAT_ID_HIRING") or chat_id
    elif "capex" in ltype or "expansion" in ltype or "project" in ltype: chat_id = os.environ.get("TELEGRAM_CHAT_ID_CAPEX") or chat_id
    elif "supplier" in ltype or "reseller" in ltype or "partner" in ltype: chat_id = os.environ.get("TELEGRAM_CHAT_ID_SUPPLIERS") or chat_id
    elif "media" in ltype or "news" in ltype: chat_id = os.environ.get("TELEGRAM_CHAT_ID_INDUSTRY_MEDIA") or chat_id

    if not chat_id:
        return None, None

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Qualify", "callback_data": f"Q|{lead_id}"},
                {"text": "❌ Reject", "callback_data": f"R|{lead_id}"}
            ]
        ]
    }

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
        "reply_markup": reply_markup
    }
    try:
        r = SESSION.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return chat_id, data.get("result", {}).get("message_id")
    except Exception as e:
        log(f"    ⚠️ [Telegram Send Error]: {e}")
    
    return None, None

def fetch_all_opportunities(product):
    all_items = []
    seen = set()
    
    # --- PIPE 1: CUSTOM RSS FEEDS (Google Alerts) ---
    CUSTOM_RSS_FEEDS = [
        # https://www.google.com/alerts/feeds/17849060262234467766/15851068127971070186
    ]
    for rss_url in CUSTOM_RSS_FEEDS:
        try:
            r = SESSION.get(rss_url, timeout=8)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall(".//entry") or root.findall(".//item"):
                    l = item.findtext("{http://www.w3.org/2005/Atom}link", "") or item.findtext("link", "")
                    t = item.findtext("{http://www.w3.org/2005/Atom}title", "") or item.findtext("title", "")
                    if l and l not in seen:
                        seen.add(l)
                        all_items.append({"title": re.sub(r"<[^>]+>", "", t), "link": l, "summary": "Via Custom RSS", "product": product, "pubDate": "Recent"})
        except Exception:
            pass

    # --- PIPE 2: GOOGLE NEWS RSS ---
    news_queries = [
        f'"{product}" (capex OR expansion OR project OR "new facility" OR GCC) India',
        f'"{product}" (hiring OR vacancy OR "job opening") India'
    ]
    for q in news_queries:
        try:
            r = SESSION.get(f"https://news.google.com/rss/search?q={urllib.parse.quote(q + ' when:14d')}&hl=en-IN&gl=IN&ceid=IN:en", timeout=8)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    l, t, d, pub = [item.findtext(k, "") for k in ["link", "title", "description", "pubDate"]]
                    if l and l not in seen:
                        seen.add(l)
                        all_items.append({"title": t, "link": l, "summary": d, "product": product, "pubDate": pub})
        except Exception:
            pass

    # --- PIPE 3: THE KEYWORD MULTIPLIER ---
    search_keywords = [
        product,
        f"{product} drafting services",
        "BIM implementation tender",
        "MEP design consultancy",
        "structural detailing RFQ"
    ]
    
    web_queries = []
    for kw in search_keywords:
        web_queries.extend([
            f'"{kw}" (tender OR NIT OR RFP) site:gov.in',
            f'"{kw}" ("request for quotation" OR "supplier empanelment") India',
            f'"{kw}" ("authorized partner" OR dealer OR reseller) India'
        ])

    # --- PIPE 4: DDG WITH GOOGLE ORGANIC FAILOVER ---
    for wq in web_queries:
        success = False
        
        # ATTEMPT A & B: DuckDuckGo Main API -> Lite API
        if DDGS:
            time.sleep(4) 
            try:
                res = list(DDGS().text(wq, max_results=5)) 
                for item in res:
                    l, t, d = item.get("href", ""), item.get("title", ""), item.get("body", "")
                    if l and l not in seen:
                        seen.add(l)
                        all_items.append({"title": t, "link": l, "summary": d, "product": product, "pubDate": "Recent"})
                success = True
            except Exception:
                try:
                    time.sleep(3)
                    res = list(DDGS().text(wq, max_results=5, backend="lite"))
                    for item in res:
                        l, t, d = item.get("href", ""), item.get("title", ""), item.get("body", "")
                        if l and l not in seen:
                            seen.add(l)
                            all_items.append({"title": t, "link": l, "summary": d, "product": product, "pubDate": "Recent"})
                    success = True
                except Exception:
                    log(f"    ⚠️ [DDG Blocked] for query: {wq[:30]}...")

        # ATTEMPT C: Google Organic HTML Scraper (Failover)
        if not success and google_organic_search:
            log(f"    🔄 [Failover Active]: Routing through Google Organic Search...")
            try:
                time.sleep(5) 
                urls = list(google_organic_search(wq, num=5, stop=5, pause=3))
                for l in urls:
                    if l and l not in seen:
                        seen.add(l)
                        t, d = extract_metadata_fast(l)
                        all_items.append({"title": t, "link": l, "summary": d, "product": product, "pubDate": "Recent"})
            except Exception as e:
                pass
            
    return all_items

def try_gemini_analysis(batch):
    if not batch:
        return None
    items_block = ""
    for i, x in enumerate(batch):
        body = x.get("deep_text", x["summary"])
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {x['real_link']}\nData: {body[:15000]}\n"

    prompt = (
        "You are an elite B2B Sales AI analyzing a BATCH of multiple CAD/BIM/AEC market signals in India.\n"
        "CRITICAL: You MUST evaluate EVERY SINGLE ITEM in the batch. Do not stop at the first one.\n"
        "REJECT (is_lead=False) IMMEDIATELY IF THE TEXT CONTAINS:\n"
        "1. Non-software Junk (housekeeping, scrap, catering).\n"
        "2. SEO Spam, Coupon Codes, Affiliate links, or Adult/Casino content.\n"
        "3. Market Research Reports (CAGR, global forecast, industry report).\n"
        "4. Stock Market/Financial News (Q3 earnings, share price, dividend, Nifty/Sensex).\n"
        "5. Projects or jobs located OUTSIDE of India (e.g., Dubai, USA, Saudi, UK).\n"
        "6. Anti-bot/Captcha messages (e.g., 'verify you are human', 'access denied', 'cloudflare').\n"
        "7. Freelance gigs (Upwork, Fiverr), Student/Academic projects, or intern roles with no software buying power.\n"
        "8. STRICT DATE CHECK: If the article, tender, or job posting explicitly shows a year from 2025 or older, or a deadline that has already passed, REJECT IT IMMEDIATELY.\n\n"
        "ACCEPT (is_lead=True): Genuine CAD/BIM buyers, active RFQs, corporate hiring roles, capex projects, AND resellers/dealers/training partners IN INDIA.\n\n"
        "CLASSIFICATION MATRIX for 'lead_type':\n"
        "- If asking for quotes, RFQ, or vendor registration -> 'Active Private Buyer (RFQ)'\n"
        "- If a dealer, reseller, channel partner, or CAD institute -> 'Suppliers'\n"
        "- If published on a Government portal (GeM, eProcure) OR explicitly a State/Central Govt Tender -> 'Govt Tender'\n"
        "- If the source is LinkedIn, it is NEVER a Govt Tender (classify as Hiring Mandate, Private Capex, or Corporate Lead instead).\n"
        "- If recruiting/job opening -> 'Hiring Mandate'\n"
        "- If factory, capex, EPC project, or construction -> 'Private Capex'\n"
        "- Else -> 'Corporate Lead'\n\n"
        "CRITICAL FIRMOGRAPHIC RULES:\n"
        "1. 'org' MUST be the actual client or hiring company name. NEVER output job portals like 'Naukri', 'LinkedIn', 'Indeed', or news media names.\n"
        "2. 'org_website' MUST be the primary corporate domain of that company (e.g. 'https://www.company.com'). NEVER output portal links (linkedin.com, naukri.com). If unknown, use 'Not Listed'.\n"
        "3. If Lead is 'Hiring Mandate' and DM Name is missing, set DM Title to 'Talent Acquisition / HR Head'.\n"
        "4. If Salary or Experience is missing, set to 'Undisclosed' instead of 'N/A'.\n"
        "Extract all 38 fields for EACH valid item. You MUST return a JSON list containing an object for EVERY valid lead, and strictly ensure the 'item_index' matches the item number from the text below.\n"
        f"{items_block}"
    )

    client = KEY_POOL.get_client()
    if not client:
        return None
    try:
        from google.genai import types
        res = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=LeadBatchResponse,
                temperature=0.1
            )
        )
        parsed = json.loads(res.text)
        return parsed.get("leads", [])
    except Exception as e:
        log(f"    ⚠️ [Gemini Execution Error]: {e}")
        KEY_POOL.rotate()
    return None

def extract_lead_locally(item, real_url, deep_text=""):
    text = f"{item['title']} {item['summary']} {deep_text}".lower()
    
    is_buyer = any(k in text for k in ["rfq", "request for quotation", "vendor registration", "supplier empanelment", "looking for vendors", "need quotes", "it procurement"])
    is_seller = any(sm in text for sm in SELLER_MARKERS)
    is_govt = any(k in text for k in ["gem.gov", "eprocure", "ireps"]) or (any(k in text for k in ["tender", "nit", "corrigendum"]) and "linkedin.com" not in item["link"])
    is_capex = any(k in text for k in ["capex", "project win", "awarded", "expansion", "empanelment"])
    is_hiring = any(k in text for k in ["hiring", "vacancy", "jobs", "opening"])
    is_corp = any(k in text for k in ["series a", "series b", "raises funding", "acquired", "acquisition", "merger", "ipo", "software", "license", "subscription", "gcc", "capability center"])
    
    if not (is_govt or is_capex or is_hiring or is_corp or is_seller or is_buyer):
        return {"is_lead": False}

    if is_buyer: ltype = "🛒 Active Private Buyer (RFQ)"
    elif is_seller: ltype = "🤝 Suppliers"
    elif is_govt: ltype = "🏛 Govt Tender"
    elif is_hiring: ltype = "💼 Hiring Mandate"
    elif is_capex: ltype = "🏗 Capex & Projects"
    else: ltype = "🏢 Corporate Lead"

    p_stage = "Active Procurement" if is_buyer else ("Tender & Bidding" if is_govt else ("Team Expansion" if is_hiring else "Planning / Execution"))
    
    raw_text = f"{item['title']} {item['summary']} {deep_text}"
    found_tools = list(set(TECH_STACK_PATTERNS.findall(raw_text)))
    if item['product'] not in found_tools:
        found_tools.insert(0, item['product'])

    loc_match = LOCATION_PATTERNS.search(raw_text)
    address = loc_match.group(0).title() if loc_match else "India"
    state = STATE_MAP.get(address.lower(), "Pan-India")

    emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", raw_text)
    phones = re.findall(r"(?:\+91[- ]?)?[6789]\d{9}\b", raw_text)
    intent = "High" if (is_govt or is_hiring or is_buyer) else "Medium"

    detected_org = clean_org_name(item["title"])

    return {
        "item_index": 0, "is_lead": True, "lead_type": ltype,
        "org": detected_org, "org_website": "Not Listed",
        "entity_type": "Partner/Supplier" if is_seller else "Commercial", "industry": "AEC / Infrastructure",
        "hq": address, "est_size": "Unknown", "dm_name": "Not Listed", "dm_title": "Talent Acquisition / HR Head" if is_hiring else "Not Listed",
        "email": emails[0] if emails else "N/A", "phone": phones[0] if phones else "N/A",
        "secondary_contact": "N/A", "boardline": "N/A", "project_name": "N/A", "project_stage": p_stage,
        "project_scale": "N/A", "total_investment": "N/A", "epc": "N/A", "pmc": "N/A", "completion_date": "N/A",
        "tender_id": "N/A", "tender_value": "N/A", "emd": "N/A", "tender_fee": "N/A", "pre_bid": "N/A",
        "deadline": "N/A", "bid_opening": "N/A", "contract_duration": "N/A", "eligibility": "N/A",
        "job_title": "N/A", "vacancies": "N/A", "exp_level": "Undisclosed", "salary": "Undisclosed",
        "tech_stack": ", ".join(found_tools), "competitor": "None", "buying_intent": intent, "urgency": "Warm",
        "sales_action": "Reach out with pricing immediately" if is_buyer else "Standard Sales Follow-Up",
        "pitch_angle": "N/A", "summary": re.sub(r"<[^>]+>", " ", item['summary']).strip()[:180],
        "state": state
    }

def dispatch_lead(item, d):
    prod = item["product"]
    raw_org = d.get("org", "")
    
    org = clean_org_name(raw_org) if raw_org else clean_org_name(item["title"])
    if is_portal_url(org):
        org = clean_org_name(item["title"])
        
    ltype = d.get("lead_type", "Corporate Lead")
    real_link = item.get("real_link", item["link"])
    
    gemini_web = d.get("org_website", "")
    enrich = free_b2b_enrichment(org, ltype)
    
    web = "Not Listed"
    if gemini_web and not is_portal_url(gemini_web) and gemini_web.lower() not in ["not listed", "n/a", "none"]:
        web = gemini_web
    elif enrich.get("web") and not is_portal_url(enrich["web"]):
        web = enrich["web"]
    elif not is_portal_url(real_link):
        parsed = urllib.parse.urlparse(real_link)
        web = f"{parsed.scheme}://{parsed.netloc}"

    dm_name = enrich["name"] or d.get("dm_name", "Not Listed")
    dm_li = enrich["url"] or d.get("dm_linkedin", "N/A")
    dm_title = d.get("dm_title", "Not Listed")
    
    email = d.get("email", "N/A")
    if email in ["N/A", "Not Listed", "", "None", None]:
        valid_name = dm_name and dm_name.lower() not in ["not listed", "not found", "found via linkedin search", "key stakeholder"]
        valid_web = web and not is_portal_url(web) and web not in ["Not Listed", "N/A"]
        
        try:
            domain = urllib.parse.urlparse(web).netloc.replace("www.", "")
            if valid_name and valid_web and "." in domain:
                clean_name = re.sub(r'[^a-zA-Z\s]', '', dm_name.split('-')[0]).strip()
                parts = clean_name.split()
                if len(parts) >= 1:
                    f_name = parts[0].lower()
                    l_name = parts[-1].lower() if len(parts) > 1 else ""
                    if l_name:
                        email = f"⚠️ GUESSED: {f_name}.{l_name}@{domain} OR {f_name}@{domain}"
                    else:
                        email = f"⚠️ GUESSED: {f_name}@{domain}"
            elif "hiring" in ltype.lower() and valid_web and "." in domain:
                email = f"⚠️ GUESSED: hr@{domain} OR careers@{domain}"
        except Exception:
            pass

    pub_date = format_pubdate(item.get("pubDate", ""))
    app_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    state = d.get("state", "Pan-India")
    hq = d.get("hq", "India")
    if state in ["Pan-India", "India", ""] and hq.lower() in STATE_MAP:
        state = STATE_MAP[hq.lower()]

    lead_id = uuid.uuid4().hex[:8]
    
    def f(label, val):
        if val and str(val).lower() not in ["n/a", "not listed", "none", "unknown", "none detected", ""]:
            return f"{label} {val}\n"
        return ""

    msg = f"🚨 *Intelligence Signal Alert!*\n\n"
    msg += f"🎯 *Intent:* {d.get('buying_intent', 'Medium')} | {d.get('urgency', 'Warm')}\n"
    msg += f"💡 *AI Advice:* _{d.get('sales_action', 'Outreach')}_\n"
    msg += f"🗣️ *Pitch:* _{d.get('pitch_angle', 'N/A')}_\n\n"
    
    msg += f"🏢 *Entity:* {org} ({d.get('entity_type', 'Commercial')})\n"
    msg += f"🏷 *Category:* {ltype}\n"
    msg += f(f"🏭 *Industry:*", d.get('industry', 'AEC'))
    
    msg += f(f"🏗 *Project:*", d.get('project_name', 'N/A'))
    msg += f(f"📈 *Stage:*", d.get('project_stage', 'N/A'))
    msg += f(f"📐 *Scale:*", d.get('project_scale', 'N/A'))
    msg += f(f"💰 *Investment:*", d.get('total_investment', 'N/A'))
    msg += f(f"👷 *EPC/Builder:*", d.get('epc', 'N/A'))
    msg += f(f"🤝 *PMC:*", d.get('pmc', 'N/A'))
    
    tender_id = d.get('tender_id', 'N/A')
    if tender_id != 'N/A':
        msg += f"🆔 *Tender ID:* `{tender_id}`\n"
    msg += f(f"📅 *Pre-Bid Meeting:* 🚨", d.get('pre_bid', 'N/A'))
    msg += f(f"⏳ *Deadline:*", d.get('deadline', 'N/A'))
    msg += f(f"💵 *Value:*", d.get('tender_value', 'N/A'))
    
    msg += f(f"💼 *Hiring:*", d.get('job_title', 'N/A'))
    msg += f(f"👥 *Vacancies:*", d.get('vacancies', 'N/A'))
    msg += f(f"🎓 *Experience:*", d.get('exp_level', 'N/A'))
    msg += f(f"💸 *Salary:*", d.get('salary', 'N/A'))
    
    msg += f"\n🛠 *Tech Stack:* `{d.get('tech_stack', prod)}`\n"
    msg += f(f"⚔️ *Competitors:*", d.get('competitor', 'N/A'))
    
    msg += f"\n👤 *DM:* {dm_name}"
    if dm_title not in ["Not Listed", ""]:
        msg += f" - {dm_title}"
    msg += "\n"
    if dm_li != "N/A":
        msg += f"🔗 *LinkedIn:* [View Profile]({dm_li})\n"
    
    msg += f(f"📧 *Email:*", email)
    msg += f(f"📞 *Phone:*", d.get('phone', 'N/A'))
    msg += f(f"🏢 *Boardline:*", d.get('boardline', 'N/A'))
    msg += f"📍 *Location:* {hq}, {state}\n"
    msg += f"🌐 *Corporate Website:* {web}\n\n"
    msg += f"🔗 [Open Original Document]({real_link})"

    tg_chat_id, tg_msg_id = send_telegram(msg, lead_type=ltype, lead_id=lead_id)

    payload = {
        "lead_id": lead_id,
        "tg_chat_id": tg_chat_id or "",
        "tg_msg_id": tg_msg_id or "",
        "appearance_date": app_date, 
        "published_date": pub_date,
        "buying_intent": d.get("buying_intent", "Medium"), "urgency": d.get("urgency", "Warm"),
        "sales_action": d.get("sales_action", "Outreach"), "pitch_angle": d.get("pitch_angle", ""),
        "org": org, "entity_type": d.get("entity_type", "Commercial"), "industry": d.get("industry", "AEC"),
        "hq": hq, "state": state, "website": web, "est_size": d.get("est_size", "N/A"),
        "dm_name": dm_name, "dm_title": dm_title, "dm_linkedin": dm_li, "email": email,
        "phone": d.get("phone", "N/A"), "secondary_contact": d.get("secondary_contact", "N/A"), "boardline": d.get("boardline", "N/A"),
        "project_name": d.get("project_name", "N/A"), "project_stage": d.get("project_stage", "Planning"),
        "project_scale": d.get("project_scale", "N/A"), "total_investment": d.get("total_investment", "N/A"),
        "epc": d.get("epc", "N/A"), "pmc": d.get("pmc", "N/A"), "completion_date": d.get("completion_date", "N/A"),
        "tender_id": d.get("tender_id", "N/A"), "tender_value": d.get("tender_value", "N/A"),
        "emd": d.get("emd", "N/A"), "tender_fee": d.get("tender_fee", "N/A"), "pre_bid": d.get("pre_bid", "N/A"),
        "deadline": d.get("deadline", "N/A"), "bid_opening": d.get("bid_opening", "N/A"), "contract_duration": d.get("contract_duration", "N/A"),
        "eligibility": d.get("eligibility", "N/A"), "job_title": d.get("job_title", "N/A"),
        "vacancies": d.get("vacancies", "N/A"), "exp_level": d.get("exp_level", "N/A"), "salary": d.get("salary", "N/A"),
        "tech_stack": d.get("tech_stack", prod), "competitor": d.get("competitor", "None"),
        "summary": d.get("summary", item["title"]), "link": real_link, "type": ltype
    }
    
    sheet_status = push_to_sheet(payload)
    if sheet_status == "duplicate":
        log(f"    ⏭️ [DUPLICATE IGNORED]: {org} is already in the Sheet.")
        return  

    log(f"    >>> [RECORDED]: {org} | Web: {web} | Intent: {payload['buying_intent']}")

def main():
    products = load_products()
    seen = load_seen()
    negative_kw = load_negative_keywords()
    blocked_domains = load_blocked_sources()
    
    log(f">>> Scanning targets for products: {', '.join(products)}")
    for p in products:
        raw_items = fetch_all_opportunities(p)
        candidates = []
        for item in raw_items:
            
            # --- THE NEW DOMAIN BLOCKER SHIELD ---
            if any(domain in item["link"].lower() for domain in blocked_domains):
                continue
                
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            save_seen(item["link"])
            
            combined = f"{item['title']} {item['summary']}".lower()
            if not any(jm in combined for jm in JUNK_MARKERS) and not any(nk in combined for nk in negative_kw):
                candidates.append(item)
                
        if not candidates:
            continue
            
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i+10]
            
            for x in batch:
                real = get_real_url(x["link"])
                x["real_link"] = real
                deep = deep_scrape_content(real)
                x["deep_text"] = deep if len(deep) > 250 else x["summary"]

            evals = try_gemini_analysis(batch)
            if evals:
                for res in evals:
                    d = res if isinstance(res, dict) else res.model_dump()
                    if d.get("is_lead"):
                        idx = d.get("item_index", 0) % len(batch)
                        dispatch_lead(batch[idx], d)
            else:
                for item in batch:
                    d = extract_lead_locally(item, item["real_link"], item.get("deep_text", ""))
                    if d.get("is_lead"):
                        dispatch_lead(item, d)

if __name__ == "__main__":
    main()
