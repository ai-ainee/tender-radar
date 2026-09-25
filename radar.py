import os
import sys
import json
import re
import time
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
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

def log(msg):
    print(msg, flush=True)

log(">>> ENTERPRISE RADAR ACTIVE: HYBRID B2B + DECODER + ANTI-DUPLICATE SPAM SHIELD")

GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
PRODUCTS_FILE = "products.txt"
STATES_FILE = "states.txt"
NEGATIVE_FILE = "negative_keywords.txt"
SEEN_FILE = "seen_links.txt"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
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
    "scrap", "medicines", "medical equipment", "sweeping", "printer cartridge", "photocopier"
]

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
    org: str
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

def extract_base_website(url):
    try:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return "Web Portal"

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
    """Cracks Google News encryption to get the true destination URL."""
    if "news.google.com" in url:
        try:
            if gnewsdecoder:
                dec = gnewsdecoder(url)
                if dec and dec.get("status"):
                    return dec.get("decoded_url")
        except Exception as e:
            log(f"    ⚠️ [URL Decoder Error]: {e}")
    return url

def free_b2b_enrichment(company_name, lead_type):
    data = {"name": "", "title": "", "url": "", "web": ""}
    if not DDGS or not company_name or len(company_name) < 4:
        return data
    if company_name.lower() in ["commercial buyer", "commercial enterprise", "target enterprise", "buyer entity"]:
        return data
        
    try:
        time.sleep(2) # Prevent GitHub Actions Rate Limit Ban
        ddgs = DDGS()
        web_res = list(ddgs.text(f"{company_name} official website india", max_results=1))
        if web_res:
            data["web"] = web_res[0].get("href", "")
            
        time.sleep(2) # Prevent GitHub Actions Rate Limit Ban
        role_clause = '"Head of BIM" OR "Design Head" OR "Chief Architect" OR HR' if "hiring" in lead_type.lower() else 'Procurement OR "Purchase Manager" OR Director'
        li_res = list(ddgs.text(f'"{company_name}" ({role_clause}) site:linkedin.com/in/', max_results=1))
        if li_res:
            data["url"] = li_res[0].get("href", "")
            clean_title = li_res[0].get("title", "").replace(" | LinkedIn", "").replace(" - LinkedIn", "")
            data["name"] = clean_title.split(" - ")[0].split(" | ")[0]
            data["title"] = clean_title
            
    except Exception as e:
        log(f"    ⚠️ [Enrichment Warning (DuckDuckGo)]: {e}")
    return data

def deep_scrape_content(url):
    try:
        r = SESSION.get(url, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            if "application/pdf" in r.headers.get("Content-Type", "") or url.lower().endswith(".pdf"):
                if not PdfReader:
                    return "[PDF Document]"
                pdf = PdfReader(io.BytesIO(r.content))
                return "".join([(p.extract_text() or "") + " " for p in pdf.pages[:3]])[:15000]
            soup = BeautifulSoup(r.content, 'html.parser')
            for s in soup(["script", "style", "noscript", "header", "footer"]):
                s.extract()
            return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:15000]
    except Exception:
        pass
    return ""

def push_to_sheet(payload):
    """Pushes to Google Sheets and reads the response to prevent duplicates."""
    if not GOOGLE_SHEET_WEBHOOK:
        return "success"
    try:
        # allow_redirects=True is required to read the JSON response from Apps Script
        res = SESSION.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=15, allow_redirects=True)
        try:
            resp_data = res.json()
            if resp_data.get("result") == "duplicate_ignored":
                return "duplicate"
        except Exception:
            pass
        return "success"
    except Exception as e:
        log(f"    ❌ [Sheet Webhook Error]: {e}")
        return "error"

def send_telegram(text, lead_type=""):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return

    ltype = (lead_type or "").lower()
    chat_id = None

    # Dynamic Telegram Topic / Channel Router
    if "buyer" in ltype or "rfq" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_BUYERS")
    elif "tender" in ltype or "govt" in ltype or "gem" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_TENDERS")
    elif "hiring" in ltype or "vacancy" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_HIRING")
    elif "capex" in ltype or "expansion" in ltype or "project" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_CAPEX")
    elif "supplier" in ltype or "reseller" in ltype or "training" in ltype or "partner" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_SUPPLIERS")
    elif "media" in ltype or "news" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_INDUSTRY_MEDIA")
    elif "corporate" in ltype or "enterprise" in ltype:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID_CORP")

    # Safe Fallback to master chat if channel secret isn't provided
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not chat_id:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        SESSION.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"    ⚠️ [Telegram Send Error]: {e}")

def fetch_all_opportunities(product):
    all_items = []
    seen = set()
    queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:ireps.gov.in) India when:7d',
        f'"{product}" ("request for quotation" OR RFQ OR "vendor registration" OR "supplier empanelment" OR "IT procurement") India when:7d',
        f'"{product}" (site:linkedin.com/posts OR site:linkedin.com/pulse) ("looking for vendors" OR "need quotes" OR "software procurement" OR "authorized partner") India when:7d',
        f'"{product}" (capex OR awarded OR expansion OR "Global Capability Center" OR GCC OR "Design Center" OR IPO OR reseller OR partner) India when:7d',
        f'"{product}" (hiring OR vacancy) (site:linkedin.com/jobs OR site:naukri.com) India when:7d'
    ]
    for q in queries:
        try:
            r = SESSION.get(f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en", timeout=8)
            if r.status_code == 200 and r.content:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    l, t, d, pub = [item.findtext(k, "") for k in ["link", "title", "description", "pubDate"]]
                    if l and l not in seen:
                        seen.add(l)
                        all_items.append({"title": t, "link": l, "summary": d, "product": product, "pubDate": pub})
        except Exception:
            pass
    return all_items

def try_gemini_analysis(batch):
    if not batch:
        return None
    items_block = ""
    for i, x in enumerate(batch):
        # Decode the URL to bypass Google News Redirect Shield
        real_url = get_real_url(x["link"])
        x["real_link"] = real_url
        
        deep = deep_scrape_content(real_url)
        body = deep if len(deep) > 250 else x["summary"]
        
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {real_url}\nData: {body[:4000]}\n"

    prompt = (
        "You are an elite B2B Sales AI analyzing CAD/BIM/AEC market signals in India.\n"
        "REJECT (is_lead=False): ONLY non-software Junk (housekeeping, security, catering, stationery, scrap, vehicles).\n"
        "ACCEPT (is_lead=True): Genuine CAD/BIM buyers, active RFQs, hiring roles, capex projects, AND resellers/dealers/training partners.\n\n"
        "CLASSIFICATION MATRIX for 'lead_type':\n"
        "- If asking for quotes, RFQ, or vendor registration -> 'Active Private Buyer (RFQ)'\n"
        "- If a dealer, reseller, channel partner, or CAD institute -> 'Suppliers'\n"
        "- If govt portal, GeM, or tender -> 'Govt Tender'\n"
        "- If recruiting/job opening -> 'Hiring Mandate'\n"
        "- If factory, capex, EPC project, or construction -> 'Private Capex'\n"
        "- Else -> 'Corporate Lead'\n\n"
        "Extract all 38 firmographic, project, tender, tech stack, and contact fields. Use 'N/A' or 'Not Listed' if missing.\n"
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

def extract_lead_locally(item, real_url):
    text = f"{item['title']} {item['summary']}".lower()
    if any(jm in text for jm in JUNK_MARKERS):
        return {"is_lead": False}

    is_buyer = any(k in text for k in ["rfq", "request for quotation", "vendor registration", "supplier empanelment", "looking for vendors", "need quotes", "it procurement"])
    is_seller = any(sm in text for sm in SELLER_MARKERS)
    is_govt = any(k in text for k in ["gem.gov", "eprocure", "ireps", "tender", "nit", "bid", "corrigendum"])
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
    
    raw_text = f"{item['title']} {item['summary']}"
    found_tools = list(set(TECH_STACK_PATTERNS.findall(raw_text)))
    if item['product'] not in found_tools:
        found_tools.insert(0, item['product'])

    loc_match = LOCATION_PATTERNS.search(raw_text)
    address = loc_match.group(0).title() if loc_match else "India"
    state = STATE_MAP.get(address.lower(), "Pan-India")

    emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", raw_text)
    phones = re.findall(r"(?:\+91[- ]?)?[6789]\d{9}\b", raw_text)
    intent = "High" if (is_govt or is_hiring or is_buyer) else "Medium"

    return {
        "item_index": 0, "is_lead": True, "lead_type": ltype,
        "org": item["title"].split("-")[-1].strip() if "-" in item["title"] else "Target Enterprise",
        "entity_type": "Partner/Supplier" if is_seller else "Commercial", "industry": "AEC / Infrastructure",
        "hq": address, "est_size": "Unknown", "dm_name": "Not Listed", "dm_title": "Not Listed",
        "email": emails[0] if emails else "N/A", "phone": phones[0] if phones else "N/A",
        "secondary_contact": "N/A", "boardline": "N/A", "project_name": "N/A", "project_stage": p_stage,
        "project_scale": "N/A", "total_investment": "N/A", "epc": "N/A", "pmc": "N/A", "completion_date": "N/A",
        "tender_id": "N/A", "tender_value": "N/A", "emd": "N/A", "tender_fee": "N/A", "pre_bid": "N/A",
        "deadline": "N/A", "bid_opening": "N/A", "contract_duration": "N/A", "eligibility": "N/A",
        "job_title": "N/A", "vacancies": "N/A", "exp_level": "N/A", "salary": "N/A",
        "tech_stack": ", ".join(found_tools), "competitor": "None", "buying_intent": intent, "urgency": "Warm",
        "sales_action": "Reach out with pricing immediately" if is_buyer else "Standard Sales Follow-Up",
        "pitch_angle": "N/A", "summary": re.sub(r"<[^>]+>", " ", item['summary']).strip()[:180],
        "state": state
    }

def dispatch_lead(item, d):
    prod = item["product"]
    org = d.get("org", "Buyer Entity")
    ltype = d.get("lead_type", "Corporate Lead")
    
    # Ensure decoded URL is used
    real_link = item.get("real_link", item["link"])
    
    enrich = free_b2b_enrichment(org, ltype)
    web = enrich["web"] or extract_base_website(real_link)
    dm_name = enrich["name"] or d.get("dm_name", "Not Listed")
    dm_li = enrich["url"] or d.get("dm_linkedin", "N/A")
    dm_title = d.get("dm_title", "Not Listed")
    
    # --- EMAIL GUESSER MODULE ---
    email = d.get("email", "N/A")
    if email in ["N/A", "Not Listed", "", "None", None]:
        valid_name = dm_name and dm_name.lower() not in ["not listed", "not found", "found via linkedin search", "key stakeholder"]
        valid_web = web and not any(x in web for x in ["google", "news", "eprocure", "Web Portal", "linkedin.com"])
        if valid_name and valid_web:
            clean_name = re.sub(r'[^a-zA-Z\s]', '', dm_name.split('-')[0]).strip()
            parts = clean_name.split()
            try:
                domain = urllib.parse.urlparse(web).netloc.replace("www.", "")
                if len(parts) >= 1 and "." in domain:
                    f_name = parts[0].lower()
                    l_name = parts[-1].lower() if len(parts) > 1 else ""
                    if l_name:
                        email = f"⚠️ GUESSED: {f_name}.{l_name}@{domain} OR {f_name}@{domain}"
                    else:
                        email = f"⚠️ GUESSED: {f_name}@{domain}"
            except Exception:
                pass
    # -----------------------------

    pub_date = format_pubdate(item.get("pubDate", ""))
    app_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    state = d.get("state", "Pan-India")
    hq = d.get("hq", "India")
    if state in ["Pan-India", "India", ""] and hq.lower() in STATE_MAP:
        state = STATE_MAP[hq.lower()]

    payload = {
        "appearance_date": app_date, "published_date": pub_date,
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
    
    # Check if Google Sheets accepted the lead, or blocked it as a duplicate
    sheet_status = push_to_sheet(payload)
    
    if sheet_status == "duplicate":
        log(f"    ⏭️ [DUPLICATE IGNORED]: {org} is already in the Sheet. Skipping Telegram alert.")
        return  # Abort here so you don't get spammed on Telegram!

    # Dynamic Telegram UI Builder (hides empty fields)
    def f(label, val):
        if val and str(val).lower() not in ["n/a", "not listed", "none", "unknown", "none detected", ""]:
            return f"{label} {val}\n"
        return ""

    msg = f"🚨 *Intelligence Signal Alert!*\n\n"
    msg += f"🎯 *Intent:* {payload['buying_intent']} | {payload['urgency']}\n"
    msg += f"💡 *AI Advice:* _{payload['sales_action']}_\n"
    msg += f"🗣️ *Pitch:* _{payload['pitch_angle']}_\n\n"
    
    msg += f"🏢 *Entity:* {org} ({payload['entity_type']})\n"
    msg += f"🏷 *Category:* {ltype}\n"
    msg += f(f"🏭 *Industry:*", payload['industry'])
    
    msg += f(f"🏗 *Project:*", payload['project_name'])
    msg += f(f"📈 *Stage:*", payload['project_stage'])
    msg += f(f"📐 *Scale:*", payload['project_scale'])
    msg += f(f"💰 *Investment:*", payload['total_investment'])
    msg += f(f"👷 *EPC/Builder:*", payload['epc'])
    msg += f(f"🤝 *PMC:*", payload['pmc'])
    
    if payload['tender_id'] != 'N/A':
        msg += f"🆔 *Tender ID:* `{payload['tender_id']}`\n"
    msg += f(f"📅 *Pre-Bid Meeting:* 🚨", payload['pre_bid'])
    msg += f(f"⏳ *Deadline:*", payload['deadline'])
    msg += f(f"💵 *Value:*", payload['tender_value'])
    
    msg += f(f"💼 *Hiring:*", payload['job_title'])
    msg += f(f"👥 *Vacancies:*", payload['vacancies'])
    msg += f(f"🎓 *Experience:*", payload['exp_level'])
    
    msg += f"\n🛠 *Tech Stack:* `{payload['tech_stack']}`\n"
    msg += f(f"⚔️ *Competitors:*", payload['competitor'])
    
    msg += f"\n👤 *DM:* {dm_name}"
    if dm_title not in ["Not Listed", ""]:
        msg += f" - {dm_title}"
    msg += "\n"
    if dm_li != "N/A":
        msg += f"🔗 *LinkedIn:* [View Profile]({dm_li})\n"
    
    msg += f(f"📧 *Email:*", payload['email'])
    msg += f(f"📞 *Phone:*", payload['phone'])
    msg += f(f"🏢 *Boardline:*", payload['boardline'])
    msg += f"📍 *Location:* {hq}, {state}\n"
    msg += f"🌐 *Portal:* {web}\n\n"
    msg += f"🔗 [Open Original Document]({real_link})"

    # Final Dispatch to the correct Telegram channel
    send_telegram(msg, lead_type=ltype)
    log(f"    >>> [RECORDED]: {org} | Type: {ltype} | Intent: {payload['buying_intent']}")

def main():
    products = load_products()
    seen = load_seen()
    
    log(f">>> Scanning targets for products: {', '.join(products)}")
    for p in products:
        raw_items = fetch_all_opportunities(p)
        candidates = []
        for item in raw_items:
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            save_seen(item["link"])
            
            combined = f"{item['title']} {item['summary']}".lower()
            if not any(jm in combined for jm in JUNK_MARKERS):
                candidates.append(item)
                
        if not candidates:
            continue
            
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i+10]
            evals = try_gemini_analysis(batch)
            if evals:
                for res in evals:
                    d = res if isinstance(res, dict) else res.model_dump()
                    if d.get("is_lead"):
                        idx = d.get("item_index", 0) % len(batch)
                        dispatch_lead(batch[idx], d)
            else:
                for item in batch:
                    # LOCAL FALLBACK ALSO NEEDS THE DECODED LINK!
                    real = get_real_url(item["link"])
                    item["real_link"] = real
                    d = extract_lead_locally(item, real)
                    if d.get("is_lead"):
                        dispatch_lead(item, d)

if __name__ == "__main__":
    main()
