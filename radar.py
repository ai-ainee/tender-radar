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

def log(msg):
    print(msg, flush=True)

log(">>> ENTERPRISE RADAR MASTER ACTIVE (STRICT B2B BUYER FILTER + AI ENRICHMENT ENABLED)")

GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
PRODUCTS_FILE = "products.txt"
STATES_FILE = "states.txt"
NEGATIVE_FILE = "negative_keywords.txt"
SEEN_FILE = "seen_links.txt"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# Positive signals
COMMERCIAL_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|"
    r"corrigendum|addendum|extension|license|licenses|subscription|renewal|contract|hiring|vacancy|"
    r"drafter|modeler|architect|engineer|job|jobs|capex|expansion|project win|awarded|contractor|"
    r"consultancy|freelance|subcontract|rera|ireps|environmental clearance|seiaa|"
    r"allotted land|dpr|feasibility|empanelment|appointed as|joins as|head of bim|chief architect|"
    r"series a|series b|raises funding|acquired|acquisition|merger|ipo|drhp)\b",
    re.IGNORECASE,
)

LOCATION_PATTERNS = re.compile(
    r"\b(New Delhi|Delhi|NCR|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|"
    r"Noida|Gurgaon|Gurugram|Jaipur|Lucknow|Chandigarh|Kochi|Bhopal|Indore|Patna|Coimbatore|Vadodara|"
    r"Surat|Nagpur|Thane|Pimpri|Nashik|Visakhapatnam|Madurai|Salem|Ghaziabad|Kanpur|Agra|Varanasi|"
    r"Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat|Telangana|Haryana|Kerala|Rajasthan|"
    r"Madhya Pradesh|Bihar|West Bengal|Andhra Pradesh|Punjab|Odisha|Jharkhand|Chhattisgarh|Assam|Uttarakhand)\b",
    re.IGNORECASE,
)

TECH_STACK_PATTERNS = re.compile(
    r"\b(AutoCAD|Revit|Civil 3D|Navisworks|Fusion 360|Advance Steel|Inventor|3ds Max|Maya|"
    r"Tekla|STAAD\.?Pro|ETABS|SolidWorks|CATIA|Rhino|SketchUp|MicroStation|BIM 360|ACC)\b",
    re.IGNORECASE
)

TENDER_ID_PATTERNS = re.compile(
    r"(?:Tender\s*(?:ID|Ref|No|Notice|Number)?[:\s#-]+|NIT\s*(?:No|Number)?[:\s#-]+|GEM/\d{4}/[A-Z]/\d+)([A-Za-z0-9\/\-_]{5,32})",
    re.IGNORECASE
)

STATE_MAP = {
    'new delhi': 'Delhi', 'delhi': 'Delhi', 'ncr': 'Delhi/NCR',
    'noida': 'Uttar Pradesh', 'greater noida': 'Uttar Pradesh',
    'gurgaon': 'Haryana', 'gurugram': 'Haryana', 'faridabad': 'Haryana',
    'ghaziabad': 'Uttar Pradesh', 'meerut': 'Uttar Pradesh',
    'mumbai': 'Maharashtra', 'pune': 'Maharashtra', 'nagpur': 'Maharashtra',
    'thane': 'Maharashtra', 'pimpri': 'Maharashtra', 'nashik': 'Maharashtra', 'maharashtra': 'Maharashtra',
    'bengaluru': 'Karnataka', 'bangalore': 'Karnataka', 'karnataka': 'Karnataka',
    'chennai': 'Tamil Nadu', 'coimbatore': 'Tamil Nadu', 'tamil nadu': 'Tamil Nadu',
    'ahmedabad': 'Gujarat', 'vadodara': 'Gujarat', 'surat': 'Gujarat', 'gujarat': 'Gujarat',
    'lucknow': 'Uttar Pradesh', 'kanpur': 'Uttar Pradesh', 'uttar pradesh': 'Uttar Pradesh',
    'hyderabad': 'Telangana', 'telangana': 'Telangana',
    'visakhapatnam': 'Andhra Pradesh', 'andhra pradesh': 'Andhra Pradesh',
    'kolkata': 'West Bengal', 'west bengal': 'West Bengal',
    'patna': 'Bihar', 'bihar': 'Bihar',
    'kochi': 'Kerala', 'kerala': 'Kerala',
    'jaipur': 'Rajasthan', 'rajasthan': 'Rajasthan',
    'bhopal': 'Madhya Pradesh', 'indore': 'Madhya Pradesh', 'madhya pradesh': 'Madhya Pradesh',
    'chandigarh': 'Chandigarh', 'ludhiana': 'Punjab', 'punjab': 'Punjab',
    'bhubaneswar': 'Odisha', 'odisha': 'Odisha', 'ranchi': 'Jharkhand', 'jharkhand': 'Jharkhand',
    'raipur': 'Chhattisgarh', 'chhattisgarh': 'Chhattisgarh', 'guwahati': 'Assam', 'assam': 'Assam',
    'dehradun': 'Uttarakhand', 'uttarakhand': 'Uttarakhand'
}

EXPIRED_YEARS_PATTERN = re.compile(r"\b(2018|2019|2020|2021|2022|2023)\b")
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_REGEX = re.compile(r"(?:\+91[- ]?)?[6789]\d{9}\b")
VALUE_PATTERNS = re.compile(r"(?:₹|Rs\.?|INR|\$)\s*[\d,]+(?:\.\d+)?\s*(?:Cr(?:ore)?|Lakh|L|K|Million|M|Billion|B)?\b", re.IGNORECASE)
DEADLINE_PATTERNS = re.compile(r"(?:due|closing|last|end)\s*(?:date|time)?[:\s\-]+(\d{1,2}[-\/.]\d{1,2}[-\/.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.IGNORECASE)

# Strict Reseller & Junk Filters
SELLER_MARKERS = [
    "indiamart", "tradeindia", "justdial", "dealer", "dealers", "reseller", "resellers",
    "distributor", "distributors", "authorized partner", "channel partner", "training center",
    "course", "syllabus", "learn autocad", "tutorial", "wholesale", "we sell", "buy from us",
    "gold partner", "platinum partner"
]

JUNK_MARKERS = [
    "housekeeping", "security guard", "security services", "catering", "canteen", 
    "stationery", "taxi", "vehicle hiring", "scrap", "auction", "medicines", 
    "medical equipment", "hospital supply", "sweeping", "cleaning", "manpower outsourcing",
    "printer cartridge", "photocopier"
]

class APIKeyPool:
    def __init__(self):
        raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
        self.keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        self.current_index = 0

    def get_current_key(self):
        return self.keys[self.current_index] if self.keys else None

    def rotate_key(self):
        if not self.keys or len(self.keys) <= 1: return False
        old_idx = self.current_index
        self.current_index = (self.current_index + 1) % len(self.keys)
        if self.current_index == 0: return False
        log(f"    ⚠️ [Quota Limit] Switching API Key {old_idx + 1} -> Key {self.current_index + 1}")
        return True

    def get_client(self):
        key = self.get_current_key()
        if not key: return None
        try:
            from google import genai
            return genai.Client(api_key=key)
        except Exception as e:
            log(f"Client init error on key {self.current_index + 1}: {e}")
            return None

KEY_POOL = APIKeyPool()

class LeadData(BaseModel):
    item_index: int = Field(description="The index number of the evaluated item.")
    is_lead: bool = Field(description="True ONLY if genuine B2B software buyer, engineering/CAD hiring role, or capex project. FALSE if seller or irrelevant.")
    lead_type: str = Field(description="Category of signal.")
    org: str = Field(description="Target enterprise, PSU, builder, or hiring entity.")
    address: str = Field(description="City or district location in India.")
    state: str = Field(description="Specific Indian State. 'Pan-India' if not specific.")
    contact_person: str = Field(description="Identified contact or 'Not Listed'.")
    email: str = Field(description="Email address or 'Not Listed'.")
    phone: str = Field(description="Phone number or 'Not Listed'.")
    estimated_value: str = Field(description="Contract budget or 'Not Disclosed'.")
    quantity: str = Field(description="Required seats/units.")
    deadline: str = Field(description="Closing deadline.")
    emd_fee: str = Field(description="EMD fee or 'N/A'.")
    priority: str = Field(description="'🔥 High Urgency', '⚡ Warm', or '🌱 Strategic Nurture'.")
    eligibility: str = Field(description="Vendor criteria or notes.")
    summary: str = Field(description="One-sentence executive summary.")
    tender_id: str = Field(description="Tender/Ref Number, GeM Bid ID, or 'N/A'.")
    project_stage: str = Field(description="Planning & Feasibility, Design & Engineering, Tender & Bidding, Under Construction, or Team Expansion.")
    tech_stack: str = Field(description="Comma-separated CAD/BIM/AEC software stack mentioned or inferred.")

class LeadBatchResponse(BaseModel):
    leads: List[LeadData]

def load_products():
    if not os.path.exists(PRODUCTS_FILE): return ["AutoCAD", "Revit"]
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

def load_states():
    if not os.path.exists(STATES_FILE): return ["Maharashtra", "Karnataka", "Tamil Nadu", "Gujarat", "Delhi", "Uttar Pradesh"]
    with open(STATES_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

def load_negatives():
    if not os.path.exists(NEGATIVE_FILE): return []
    with open(NEGATIVE_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")

def extract_base_website(url):
    try: return f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
    except Exception: return "Web Portal"

def format_pubdate(pubdate_str):
    if not pubdate_str: return "Not Listed"
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return pubdate_str

def is_item_recent(pubdate_str, max_days):
    if not pubdate_str: return True
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(days=max_days)
    except Exception:
        return True

def free_b2b_enrichment(company_name, lead_type):
    """Automatically searches the web for the Official Website and LinkedIn Decision Makers."""
    enriched_data = {"dm_name": "Not Found", "linkedin_url": "", "website": ""}
    
    if not DDGS: return enriched_data
    if not company_name or len(company_name) < 4 or company_name.lower() in ["commercial enterprise", "commercial buyer"]:
        return enriched_data
        
    try:
        ddgs = DDGS()
        # 1. Search for official website
        web_results = ddgs.text(f"{company_name} official website india", max_results=1)
        if web_results:
            enriched_data["website"] = web_results[0].get("href", "")
            
        # 2. Search for exact Decision Maker on LinkedIn
        if "hiring" in lead_type.lower() or "design" in lead_type.lower():
            role_keywords = '"Head of BIM" OR "Chief Architect" OR "Design Head" OR HR'
        else:
            role_keywords = 'Procurement OR "Purchase Manager" OR Director OR "General Manager"'
            
        dork_query = f'"{company_name}" ({role_keywords}) site:linkedin.com/in/'
        li_results = ddgs.text(dork_query, max_results=1)
        
        if li_results:
            title_text = li_results[0].get("title", "")
            enriched_data["linkedin_url"] = li_results[0].get("href", "")
            enriched_data["dm_name"] = title_text.replace(" | LinkedIn", "").replace(" - LinkedIn", "")
            
        time.sleep(1) # Prevent rate limits
    except Exception as e:
        log(f"    ⚠️ [Enrichment Warning]: {e}")
        
    return enriched_data

def push_to_google_sheet(payload):
    if not GOOGLE_SHEET_WEBHOOK: return False
    try:
        res = SESSION.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=15, allow_redirects=False)
        log(f"    📊 [Sheet Response]: Status {res.status_code}")
        return True
    except Exception as e:
        log(f"    ❌ [Sheet Exception]: {e}")
        return False

def send_telegram(text, is_media_source=False, target_state="Pan-India"):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token: return
    
    active_chat_id = None
    if is_media_source:
        active_chat_id = os.environ.get("TELEGRAM_CHAT_ID_INDUSTRY_MEDIA")
    
    if not active_chat_id:
        generic_fallbacks = ["pan-india", "india", "not listed", "", "unknown", "pan india", "rest of india"]
        clean_state = (target_state or "").strip().lower()
        if clean_state not in generic_fallbacks:
            safe_state_key = target_state.upper().replace(" ", "_").replace("-", "_")
            active_chat_id = os.environ.get(f"TELEGRAM_CHAT_ID_{safe_state_key}")
            
    if not active_chat_id:
        active_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        
    if not active_chat_id: return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": active_chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False}
    try:
        SESSION.post(url, json=payload, timeout=10)
    except Exception:
        pass

def deep_scrape_content(url):
    try:
        response = SESSION.get(url, timeout=8, allow_redirects=True)
        if response.status_code != 200: return ""
        if "application/pdf" in response.headers.get("Content-Type", "") or url.lower().endswith(".pdf"):
            if not PdfReader: return "[PDF Detected]"
            pdf = PdfReader(io.BytesIO(response.content))
            return "".join([(page.extract_text() or "") + " " for page in pdf.pages[:3]])[:10000]
        soup = BeautifulSoup(response.content, 'html.parser')
        for script in soup(["script", "style", "noscript", "header", "footer"]): script.extract()
        return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:10000]
    except Exception:
        pass
    return ""

def fetch_direct_cppp_tenders(product):
    url = "https://eprocure.gov.in/cppp/latestactivetenders/1/xml"
    items = []
    try:
        resp = SESSION.get(url, timeout=10)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for tender in root.findall(".//Tender")[:40]:
                title = tender.findtext("Title", "")
                link = tender.findtext("TenderURL", "")
                desc = tender.findtext("Description", "")
                ref_no = tender.findtext("TenderReferenceNumber", "N/A")
                if product.lower() in title.lower() or product.lower() in desc.lower():
                    items.append({
                        "title": f"[DIRECT CPPP TENDER] {title}", "link": link, "summary": f"{desc} | Ref: {ref_no}",
                        "product": product, "raw_pubdate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
                    })
    except Exception:
        pass
    return items

def fetch_all_opportunities(product, time_window_query, max_age_days):
    all_items = fetch_direct_cppp_tenders(product)
    seen_in_scan = set([i["link"] for i in all_items])

    stream_queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:ireps.gov.in OR site:etenders.gov.in) India {time_window_query}',
        f'"{product}" ("tender notice" OR "request for proposal" OR "corrigendum" OR "bid invitation" OR "nit") India {time_window_query}',
        f'"{product}" (site:constructionbusinesstoday.com OR site:constructionweekonline.in OR site:moneycontrol.com OR site:economictimes.indiatimes.com OR site:themachinist.in) {time_window_query}',
        f'"{product}" (capex OR "project win" OR "awarded contract" OR "EPC contract" OR "new manufacturing plant") India {time_window_query}',
        f'"{product}" ("Environmental Clearance" OR "DPR approved" OR RERA OR "Detailed Project Report" OR "allotted land" OR MIDC OR GIDC) India {time_window_query}',
        f'"{product}" ("Empanelment of Architects" OR "EOI for Architectural" OR "design consultancy") India {time_window_query}',
        f'"{product}" (hiring OR vacancy OR "job opening") (site:linkedin.com/jobs OR site:naukri.com) India {time_window_query}'
    ]

    for q in stream_queries:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = SESSION.get(url, timeout=10)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    link, title, desc, raw_pubdate = [item.findtext(k, "").strip() for k in ["link", "title", "description", "pubDate"]]
                    if not is_item_recent(raw_pubdate, max_age_days) or EXPIRED_YEARS_PATTERN.search(title + " " + desc): continue
                    if link and title and link not in seen_in_scan:
                        seen_in_scan.add(link)
                        all_items.append({"title": title, "link": link, "summary": desc, "product": product, "raw_pubdate": raw_pubdate})
        except Exception:
            pass
    
    log(f"Total unique candidates gathered for {product}: {len(all_items)}")
    return all_items

def try_gemini_analysis(batch):
    if not batch: return None
    items_block = ""
    for idx, it in enumerate(batch):
        deep_text = deep_scrape_content(it['real_link'])
        context_payload = deep_text if len(deep_text) > 300 else it['summary']
        items_block += f"\n--- ITEM {idx} ---\nTitle: {it['title']}\nLink: {it['real_link']}\nData: {context_payload}\n"

    prompt = (
        "You are a strict B2B Sales Intelligence filter for CAD/BIM/AEC software (like AutoCAD, Revit).\n"
        "Analyze the following Indian business/government items.\n\n"
        "CRITICAL REJECTION RULES (Set 'is_lead' to FALSE if):\n"
        "1. Sellers/Dealers: The entity is selling, teaching, or reselling software.\n"
        "2. Junk/Irrelevant Industry: The text is about unrelated tenders (e.g., housekeeping, catering, security, medical equipment, vehicle hiring, scrap).\n"
        "3. Passing Mentions: The software is just mentioned in a website sidebar or unrelated context.\n\n"
        "ACCEPT ONLY (Set 'is_lead' to TRUE if):\n"
        "1. Genuine software buyers/tenders looking for CAD/BIM licenses.\n"
        "2. Companies actively hiring for CAD/BIM/Drafting roles.\n"
        "3. Large Capex/Construction project announcements (potential buyers).\n\n"
        "Extract: Tender/Ref ID, Project Stage (Planning, Design, Tender, Construction, Team Expansion), & CAD/BIM Tech Stack.\n"
        f"Data: {items_block}"
    )

    client = KEY_POOL.get_client()
    if not client: return None
    try:
        from google.genai import types
        response = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LeadBatchResponse, temperature=0.1)
        )
        res = json.loads(response.text)
        if res and "leads" in res: return res["leads"]
    except Exception as e:
        log(f"    ⚠️ [Gemini API Error]: {e}")
        KEY_POOL.rotate_key()
    return None

def extract_lead_locally(item, real_url):
    text = f"{item['title']} {item['summary']}".lower()
    
    foreign_markers = ["singapore", "united states", "usa", " uk ", "canada", "dubai", "uae", "australia", "germany"]
    if any(m in text for m in foreign_markers): return {"is_lead": False}
    if any(sm in text for sm in SELLER_MARKERS): return {"is_lead": False}
    if any(jm in text for jm in JUNK_MARKERS): return {"is_lead": False}

    is_govt = any(k in text for k in ["gem.gov", "eprocure", "ireps", "tender", "nit", "bid", "corrigendum"])
    is_capex = any(k in text for k in ["capex", "project win", "awarded", "expansion", "empanelment"])
    is_hiring = any(k in text for k in ["hiring", "vacancy", "jobs", "opening"])
    
    if not (is_govt or is_capex or is_hiring): return {"is_lead": False}

    ltype = "🏛 Government / GeM Tender" if is_govt else ("💼 Hiring Mandate" if is_hiring else "🏗 Private Capex / Expansion Win")

    if is_govt: p_stage = "Tender & Bidding"
    elif is_hiring: p_stage = "Team Expansion"
    elif any(k in text for k in ["dpr", "clearance", "rera", "feasibility", "planned"]): p_stage = "Planning & Feasibility"
    elif any(k in text for k in ["epc", "civil work", "construction", "plant"]): p_stage = "Under Construction"
    else: p_stage = "Design & Engineering"

    raw_text = f"{item['title']} {item['summary']}"
    found_tools = list(set(TECH_STACK_PATTERNS.findall(raw_text)))
    if item['product'] not in found_tools: found_tools.insert(0, item['product'])
    tech_stack_str = ", ".join(found_tools)

    tender_id_match = TENDER_ID_PATTERNS.search(raw_text)
    tender_id = tender_id_match.group(1).strip() if tender_id_match else "N/A"

    loc_match = LOCATION_PATTERNS.search(raw_text)
    address = loc_match.group(0).title() if loc_match else "India"
    state = STATE_MAP.get(address.lower(), "Pan-India")

    emails = EMAIL_REGEX.findall(raw_text)
    phones = PHONE_REGEX.findall(raw_text)
    val_match = VALUE_PATTERNS.search(raw_text)
    deadline_match = DEADLINE_PATTERNS.search(raw_text)

    return {
        "item_index": 0, "is_lead": True, "lead_type": ltype,
        "org": item["title"].split("-")[-1].strip() if "-" in item["title"] else "Commercial Enterprise",
        "address": address, "state": state, "contact_person": "Key Stakeholder",
        "email": emails[0] if emails else "Not Listed", "phone": phones[0] if phones else "Not Listed",
        "estimated_value": val_match.group(0) if val_match else "Not Disclosed",
        "quantity": "1 Requirement", "deadline": deadline_match.group(0) if deadline_match else "Open / Immediate",
        "emd_fee": "Refer Tender Doc" if is_govt else "N/A", "priority": "🔥 High Urgency" if deadline_match or val_match else "⚡ Warm", 
        "eligibility": "Standard Commercial Terms", "summary": re.sub(r"<[^>]+>", " ", item['summary']).strip()[:180],
        "tender_id": tender_id, "project_stage": p_stage, "tech_stack": tech_stack_str, "clean_link": real_url
    }

def main():
    products = load_products()
    negatives = load_negatives()
    seen = load_seen()
    time_query, max_age = "when:7d", 7
    candidates = []
    
    log(">>> Phase 1: Gathering Intelligence Signals...")
    for prod in products:
        items = fetch_all_opportunities(prod, time_query, max_age)
        for item in items:
            if item["link"] in seen: continue
            combined_text = f"{item['title']} {item['summary']}".lower()
            
            # MASSIVE JUNK FILTER BEFORE AI
            if any(neg in combined_text for neg in negatives) or any(sm in combined_text for sm in SELLER_MARKERS) or any(jm in combined_text for jm in JUNK_MARKERS):
                continue
            
            seen.add(item["link"])
            save_seen(item["link"])
            if COMMERCIAL_PATTERNS.search(combined_text):
                item["real_link"] = item["link"]
                candidates.append(item)

    log(f"\n>>> Phase 2: Total filtered commercial candidates to evaluate: {len(candidates)}")
    if not candidates: return

    for i in range(0, len(candidates), 10):
        batch = candidates[i:i + 10]
        log(f"\n>>> Processing Batch {i//10 + 1}/{(len(candidates)+9)//10} ({len(batch)} items)...")
        evaluations = try_gemini_analysis(batch)
        
        if evaluations:
            for res_dict in evaluations:
                idx = res_dict.get("item_index") if isinstance(res_dict, dict) else getattr(res_dict, "item_index", None)
                is_lead = res_dict.get("is_lead") if isinstance(res_dict, dict) else getattr(res_dict, "is_lead", False)
                if idx is not None and idx < len(batch) and is_lead:
                    data = res_dict if isinstance(res_dict, dict) else res_dict.model_dump()
                    dispatch_lead(batch[idx], data)
        else:
            log("    [Using Reliable Local Extraction Engine...]")
            for item in batch:
                res_dict = extract_lead_locally(item, item["real_link"])
                if res_dict.get("is_lead") is True:
                    dispatch_lead(item, res_dict)

def dispatch_lead(item, data):
    prod = item["product"]
    org = data.get("org", "Commercial Buyer")
    address = data.get("address", "India")
    state = data.get("state", "Pan-India")
    
    if state.lower() in ["pan-india", "india", "not listed", ""] and address.lower() in STATE_MAP:
        state = STATE_MAP[address.lower()]

    real_link = item.get("real_link", item["link"])
    website = extract_base_website(real_link)
    
    is_media = any(domain in website for domain in ["constructionbusinesstoday.com", "constructionweekonline.in", "moneycontrol.com", "economictimes.indiatimes.com", "themachinist.in"])
    if is_media: data["lead_type"] = "📰 Industry Media News"
    ltype = data.get("lead_type", "Commercial Lead")

    # --- FREE AI ENRICHMENT ---
    log(f"    🔍 [Enriching]: Hunting for Decision Makers at {org}...")
    enrichment = free_b2b_enrichment(org, ltype)
    
    if enrichment["website"] and ("google" in website or "news" in website or "eprocure" in website):
        website = enrichment["website"]
        
    contact = data.get("contact_person", "Not Listed")
    li_str = ""
    if enrichment["linkedin_url"]:
        contact = f"{enrichment['dm_name']} (Via LinkedIn)"
        li_str = f"🔗 *DM LinkedIn:* [View Profile]({enrichment['linkedin_url']})\n"
        data["contact_person"] = f"{enrichment['dm_name']} | {enrichment['linkedin_url']}"
    else:
        data["contact_person"] = contact
    # --------------------------

    email, phone = data.get("email", "Not Listed"), data.get("phone", "Not Listed")
    summary, estimated_value, quantity, deadline, emd_fee = data.get("summary", item['title']), data.get("estimated_value", "Not Disclosed"), data.get("quantity", "1 Requirement"), data.get("deadline", "Check Notice"), data.get("emd_fee", "N/A")
    priority, project_stage, tech_stack, tender_id = data.get("priority", "⚡ Warm"), data.get("project_stage", "Design & Engineering"), data.get("tech_stack", prod), data.get("tender_id", "N/A")

    if any(k in summary.lower() for k in ["urgent", "immediate", "flash", "closing soon"]): priority = "🔥 High Urgency (ACTION REQUIRED)"

    pub_date, app_date = format_pubdate(item.get("raw_pubdate", "")), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    log(f"    >>> [RECORDED]: {org} | {project_stage} | Stack: {tech_stack}")
    push_to_google_sheet({
        "appearance_date": app_date, "published_date": pub_date, "deadline": deadline, "priority": priority,
        "product": prod, "type": ltype, "project_stage": project_stage, "tech_stack": tech_stack,
        "tender_id": tender_id, "estimated_value": estimated_value, "quantity": quantity, "emd_fee": emd_fee,
        "org": org, "address": address, "state": state, "website": website, "contact_person": data["contact_person"],
        "email": email, "phone": phone, "eligibility": data.get("eligibility", "N/A"), "summary": summary, "link": real_link
    })

    emd_str = f"💳 *EMD / Tender Fee:* {emd_fee}\n" if emd_fee != 'N/A' else ""
    ref_str = f"🆔 *Tender / Ref ID:* `{tender_id}`\n" if tender_id != 'N/A' else ""
    
    msg = (
        f"🚨 *Intelligence Signal Alert!*\n\n🎯 *Priority:* {priority}\n🏷 *Category:* {ltype}\n🏗 *Project Stage:* {project_stage}\n"
        f"🛠 *Tech Stack:* `{tech_stack}`\n🏢 *Entity / Buyer:* {org}\n{ref_str}📦 *Product / Scope:* {prod} ({quantity})\n"
        f"💰 *Budget / Value:* {estimated_value}\n⏳ *Key Deadline:* `{deadline}`\n{emd_str}📍 *Location:* {address}, {state}\n"
        f"📅 *Published:* {pub_date}\n👤 *Stakeholder:* {contact}\n{li_str}📧 *Email:* {email}\n📞 *Phone:* {phone}\n"
        f"📝 *Sales Summary:* {summary}\n🌐 *Portal:* {website}\n\n🔗 [Open Original Document Link]({real_link})"
    )
    send_telegram(msg, is_media_source=is_media, target_state=state)
    return True

if __name__ == "__main__":
    main()
