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

def log(msg):
    print(msg, flush=True)

log(">>> ENTERPRISE RADAR 9.2 ACTIVE (SYNTAX ERROR FIXED)")

# ---------------------------------------------------------------------------
# 1. Credentials & Session Config
# ---------------------------------------------------------------------------
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

COMMERCIAL_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|"
    r"corrigendum|addendum|extension|license|licenses|subscription|renewal|contract|hiring|vacancy|"
    r"drafter|modeler|architect|engineer|job|jobs|capex|expansion|project win|awarded|contractor|"
    r"consultancy|freelance|subcontract|indiamart|rera|ireps|environmental clearance|seiaa|"
    r"allotted land|dpr|feasibility|empanelment|appointed as|joins as|head of bim|chief architect|"
    r"series a|series b|raises funding|acquired|acquisition|merger|ipo|drhp)\b",
    re.IGNORECASE,
)

LOCATION_PATTERNS = re.compile(
    r"\b(New Delhi|Delhi|NCR|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|"
    r"Noida|Gurgaon|Gurugram|Jaipur|Lucknow|Chandigarh|Kochi|Bhopal|Indore|Patna|Coimbatore|Vadodara|"
    r"Surat|Nagpur|Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat|Telangana|Haryana|Kerala|Rajasthan|Madhya Pradesh|Bihar|West Bengal)\b",
    re.IGNORECASE,
)

STATE_MAP = {
    'new delhi': 'Delhi', 'delhi': 'Delhi', 'ncr': 'Delhi/NCR',
    'mumbai': 'Maharashtra', 'pune': 'Maharashtra', 'nagpur': 'Maharashtra', 'maharashtra': 'Maharashtra',
    'bengaluru': 'Karnataka', 'bangalore': 'Karnataka', 'karnataka': 'Karnataka',
    'chennai': 'Tamil Nadu', 'coimbatore': 'Tamil Nadu', 'tamil nadu': 'Tamil Nadu',
    'kolkata': 'West Bengal', 'west bengal': 'West Bengal',
    'hyderabad': 'Telangana', 'telangana': 'Telangana',
    'ahmedabad': 'Gujarat', 'vadodara': 'Gujarat', 'surat': 'Gujarat', 'gujarat': 'Gujarat',
    'noida': 'Uttar Pradesh', 'lucknow': 'Uttar Pradesh', 'uttar pradesh': 'Uttar Pradesh',
    'gurgaon': 'Haryana', 'gurugram': 'Haryana', 'haryana': 'Haryana',
    'jaipur': 'Rajasthan', 'rajasthan': 'Rajasthan',
    'chandigarh': 'Chandigarh', 'kochi': 'Kerala', 'kerala': 'Kerala',
    'bhopal': 'Madhya Pradesh', 'indore': 'Madhya Pradesh', 'madhya pradesh': 'Madhya Pradesh',
    'patna': 'Bihar', 'bihar': 'Bihar'
}

EXPIRED_YEARS_PATTERN = re.compile(r"\b(2018|2019|2020|2021|2022|2023)\b")
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_REGEX = re.compile(r"(?:\+91[- ]?)?[6789]\d{9}\b")
VALUE_PATTERNS = re.compile(r"(?:₹|Rs\.?|INR|\$)\s*[\d,]+(?:\.\d+)?\s*(?:Cr(?:ore)?|Lakh|L|K|Million|M|Billion|B)?\b", re.IGNORECASE)
DEADLINE_PATTERNS = re.compile(r"(?:due|closing|last|end)\s*(?:date|time)?[:\s\-]+(\d{1,2}[-\/.]\d{1,2}[-\/.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.IGNORECASE)
QUANTITY_PATTERNS = re.compile(r"(\d+)\s*(?:nos|qty|licenses|users|seats|posts|openings|positions|units)\b", re.IGNORECASE)
EMD_PATTERNS = re.compile(r"(?:emd|earnest money|bid security)[:\s\-]+(?:₹|Rs\.?|INR)?\s*[\d,]+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# 2. Multi-Key API Pool Manager
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 3. Pydantic Structured Outputs
# ---------------------------------------------------------------------------
class LeadData(BaseModel):
    item_index: int = Field(description="The index number of the evaluated item.")
    is_lead: bool = Field(description="True if this is a commercial lead, hiring mandate, or corporate signal.")
    lead_type: str = Field(description="Strictly one of: 'Corporate Signal (M&A / Funding)', 'Government / GeM Tender', 'Hiring Mandate', 'Private Capex / Expansion Win', 'Upstream Project Clearance', 'Leadership Move', 'Architect / Consultant Empanelment', 'B2B Sub-Consultancy', or 'Non-Lead'.")
    org: str = Field(description="Target enterprise, PSU, builder, or hiring entity.")
    address: str = Field(description="City or district location in India.")
    state: str = Field(description="Specific Indian State. 'Pan-India' if not specific.")
    contact_person: str = Field(description="Identified decision maker or HR contact. 'Not Listed' if absent.")
    email: str = Field(description="Email address. 'Not Listed' if absent.")
    phone: str = Field(description="Phone number. 'Not Listed' if absent.")
    estimated_value: str = Field(description="Contract budget, capex value, or 'Not Disclosed'.")
    quantity: str = Field(description="Required seats, units, or scope volume.")
    deadline: str = Field(description="Closing deadline, or 'Immediate / Open'.")
    emd_fee: str = Field(description="EMD/Tender fee. STRICTLY 'N/A' if private.")
    priority: str = Field(description="Strictly one of: '🔥 High Urgency', '⚡ Warm', or '🌱 Strategic Nurture'.")
    eligibility: str = Field(description="Required vendor criteria, technical certifications, or strategic note.")
    summary: str = Field(description="A clean, one-sentence executive summary.")

class LeadBatchResponse(BaseModel):
    leads: List[LeadData]

# ---------------------------------------------------------------------------
# 4. Utilities, Sheet Push, and Telegram Routing
# ---------------------------------------------------------------------------
def load_products():
    if not os.path.exists(PRODUCTS_FILE): return ["AutoCAD", "Revit", "Civil 3D"]
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

def unwrap_destination_url(initial_url):
    if "news.google.com" not in initial_url: return initial_url
    try:
        resp = SESSION.get(initial_url, allow_redirects=True, timeout=10)
        final_url = resp.url
        if "google.com" in final_url:
            match = re.search(r'data-n-url="([^"]+)"', resp.text)
            if not match: match = re.search(r'<meta[^>]+http-equiv="refresh"[^>]+content="[^"]*url=([^"]+)"', resp.text, re.IGNORECASE)
            if match: return match.group(1).replace("&amp;", "&")
        return final_url
    except Exception:
        return initial_url

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

def push_to_google_sheet(payload):
    if not GOOGLE_SHEET_WEBHOOK: return True
    try:
        res = SESSION.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=10)
        try:
            if res.json().get("result") == "duplicate_ignored":
                log("  -> [Double-Lock] Sheet identified an existing link. Suppressing alert.")
                return False
        except Exception:
            pass
        return True
    except Exception as e:
        log(f"  -> Sheet Push Error: {e}")
        return True

def send_telegram(text, target_state="Pan-India"):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    default_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token: return
    
    generic_fallbacks = ["pan-india", "india", "not listed", "", "unknown", "pan india", "rest of india"]
    clean_state = (target_state or "").strip().lower()
    
    active_chat_id = None
    if clean_state not in generic_fallbacks:
        safe_state = target_state.upper().replace(" ", "_").replace("-", "_")
        active_chat_id = os.environ.get(f"TELEGRAM_CHAT_ID_{safe_state}")
        
    if not active_chat_id:
        active_chat_id = default_chat_id
        
    if not active_chat_id: return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": active_chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False}
    try:
        res = SESSION.post(url, json=payload, timeout=10)
        log(f"  -> Telegram dispatched for [{target_state}]. Status: {res.status_code}")
    except Exception as e:
        log(f"  -> Telegram Dispatch Error: {e}")

# ---------------------------------------------------------------------------
# 5. In-Memory PDF Reader & Deep Web Scraper
# ---------------------------------------------------------------------------
def deep_scrape_content(url):
    try:
        response = SESSION.get(url, timeout=10)
        if response.status_code != 200: return ""

        if "application/pdf" in response.headers.get("Content-Type", "") or url.lower().endswith(".pdf"):
            if not PdfReader: return "[PDF Detected - In-memory parsing active]"
            pdf = PdfReader(io.BytesIO(response.content))
            text = "".join([(page.extract_text() or "") + " " for page in pdf.pages[:5]])
            return re.sub(r'\s+', ' ', text)[:15000]

        soup = BeautifulSoup(response.content, 'html.parser')
        for script in soup(["script", "style", "noscript", "header", "footer"]): script.extract()
        return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:15000]
    except Exception:
        pass
    return ""

# ---------------------------------------------------------------------------
# 6. Direct CPPP Native XML Harvester
# ---------------------------------------------------------------------------
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
                if product.lower() in title.lower() or product.lower() in desc.lower():
                    items.append({
                        "title": f"[DIRECT CPPP TENDER] {title}", "link": link, "summary": desc,
                        "product": product, "raw_pubdate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
                    })
    except Exception:
        pass
    return items

# ---------------------------------------------------------------------------
# 7. Multi-Stream Harvester (Optimized for Government Tenders & Enterprise Deals)
# ---------------------------------------------------------------------------
def fetch_all_opportunities(product, time_window_query, max_age_days):
    all_items = fetch_direct_cppp_tenders(product)
    seen_in_scan = set([i["link"] for i in all_items])

    stream_queries = [
        # --- PRIORITY 1: OFFICIAL GOVERNMENT PROCUREMENT PORTALS ---
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:ireps.gov.in OR site:etenders.gov.in) India {time_window_query}',
        f'"{product}" ("tender notice" OR "request for proposal" OR "corrigendum" OR "bid invitation" OR "nit") India {time_window_query}',
        
        # --- PRIORITY 2: PRIVATE CAPEX & INFRASTRUCTURE WINS ---
        f'"{product}" (capex OR "project win" OR "awarded contract" OR "EPC contract" OR "new manufacturing plant" OR "groundbreaking") India {time_window_query}',
        f'"{product}" ("Environmental Clearance" OR "DPR approved" OR RERA OR "Detailed Project Report" OR "allotted land" OR MIDC OR GIDC) India {time_window_query}',
        
        # --- PRIORITY 3: CONSULTANCY & ARCHITECT EMPANELMENT ---
        f'"{product}" ("Empanelment of Architects" OR "EOI for Architectural" OR "design consultancy" OR "subcontract") India {time_window_query}',
        
        # --- PRIORITY 4: CORPORATE SIGNALS & FUNDING ---
        f'"{product}" ("raises funding" OR "Series A" OR "Series B" OR "acquired by" OR "merger" OR "IPO" OR "DRHP") India {time_window_query}',
        
        # --- PRIORITY 5: TARGETED HIRING MANDATES ---
        f'"{product}" (hiring OR vacancy OR "job opening" OR drafter OR modeler) (site:linkedin.com/jobs OR site:naukri.com) India {time_window_query}'
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
    return all_items

# ---------------------------------------------------------------------------
# 8. 3-Tier AI Cascade
# ---------------------------------------------------------------------------
def invoke_model_with_key_rotation(prompt, target_model):
    attempts_left = (len(KEY_POOL.keys) * 2) if KEY_POOL.keys else 2
    while attempts_left > 0:
        client = KEY_POOL.get_client()
        if not client: return None
        try:
            from google.genai import types
            response = client.models.generate_content(
                model=target_model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LeadBatchResponse, temperature=0.1)
            )
            return json.loads(response.text)
        except Exception as err:
            err_msg = str(err).lower()
            if "429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg:
                if not KEY_POOL.rotate_key(): break
                attempts_left -= 1
            elif "503" in err_msg or "timeout" in err_msg:
                time.sleep(3)
                attempts_left -= 1
            else:
                break
    return None

def try_gemini_analysis(batch):
    if not batch: return None
    items_block = ""
    for idx, it in enumerate(batch):
        deep_text = deep_scrape_content(it['real_link'])
        context_payload = deep_text if len(deep_text) > 500 else it['summary']
        clean_title = it['title'].replace('"', "'")
        real_link = it['real_link']
        items_block += f"\n--- ITEM {idx} ---\nTitle: {clean_title}\nLink: {real_link}\nData: {context_payload}\n"

    prompt = f"""
    You are an elite enterprise software sales strategist and Indian commercial intelligence director.
    Analyze the following scraped webpage and PDF data.
    Extract direct deals, procurement tenders, hiring mandates, and macro corporate signals.
    Data to process: {items_block}
    """

    log("    [Invoking Tier 1: Gemini 2.5 Pro...]")
    pro_result = invoke_model_with_key_rotation(prompt, 'gemini-2.5-pro')
    if pro_result and "leads" in pro_result: return pro_result["leads"]

    KEY_POOL.current_index = 0
    log("    [Tier 1 Exhausted. Invoking Tier 2: Gemini 3.6 Flash...]")
    flash_result = invoke_model_with_key_rotation(prompt, 'gemini-3.6-flash')
    if flash_result and "leads" in flash_result: return flash_result["leads"]
    
    return None

# ---------------------------------------------------------------------------
# 9. Local Deterministic Failsafe (Tier 3) with Anti-Junk Filters
# ---------------------------------------------------------------------------
def extract_lead_locally(item, real_url):
    text = f"{item['title']} {item['summary']}"
    lower_text = text.lower()
    
    # IMMEDIATE JUNK / FOREIGN FILTER: Drop international or spammy listings
    foreign_markers = ["singapore", "united states", "usa", " uk ", "canada", "dubai", "uae", "australia", "germany", " 幸运飞车", "등기부등본"]
    if any(m in lower_text for m in foreign_markers):
        return {"is_lead": False}

    is_govt = False
    if any(k in lower_text for k in ["gem.gov", "eprocure", "ireps", "tender", "nit", "bid", "corrigendum", "cpwd"]):
        ltype = "🏛 Government / GeM Tender"; is_govt = True
    elif any(k in lower_text for k in ["appointed as", "joins as", "head of bim", "chief architect"]): ltype = "👤 Leadership Move"
    elif any(k in lower_text for k in ["environmental clearance", "seiaa", "dpr", "allotted land"]): ltype = "🌱 Upstream Project Clearance"
    elif any(k in lower_text for k in ["empanelment", "eoi for architectural"]): ltype = "🤝 Architect Empanelment"
    elif any(k in lower_text for k in ["naukri", "linkedin", "indeed", "hiring", "drafter", "vacancy"]): ltype = "💼 Hiring Mandate"
    elif any(k in lower_text for k in ["capex", "project win", "awarded", "epc", "groundbreaking"]): ltype = "🏗 Private Capex / Expansion Win"
    elif any(k in lower_text for k in ["raises funding", "series a", "acquired by", "merger"]): ltype = "Corporate Signal (M&A / Funding)"
    else: ltype = "🤝 B2B Sub-Consultancy"

    loc_match = LOCATION_PATTERNS.search(text)
    address = loc_match.group(0) if loc_match else "India"
    state = STATE_MAP.get(address.lower(), "Pan-India")

    emails = EMAIL_REGEX.findall(text)
    phones = PHONE_REGEX.findall(text)
    val_match = VALUE_PATTERNS.search(text)
    qty_match = QUANTITY_PATTERNS.search(text)
    deadline_match = DEADLINE_PATTERNS.search(text)

    emd_fee = "N/A"
    eligibility = "Standard Commercial Terms"
    if is_govt:
        emd_match = EMD_PATTERNS.search(text)
        emd_fee = emd_match.group(0) if emd_match else "Refer Tender Doc"
        eligibility = "Authorized OEM Partner Required"

    priority = "🔥 High Urgency" if deadline_match or val_match else "⚡ Warm"
    return {
        "item_index": 0, "is_lead": True, "lead_type": ltype,
        "org": item["title"].split("-")[-1].strip() if "-" in item["title"] else "Commercial Enterprise",
        "address": address, "state": state, "contact_person": "Key Stakeholder",
        "email": emails[0] if emails else "Not Listed", "phone": phones[0] if phones else "Not Listed",
        "estimated_value": val_match.group(0) if val_match else "Not Disclosed",
        "quantity": qty_match.group(0) if qty_match else "1 Package / Position",
        "deadline": deadline_match.group(0) if deadline_match else "Open / Immediate",
        "emd_fee": emd_fee, "priority": priority, "eligibility": eligibility,
        "summary": re.sub(r"<[^>]+>", " ", item['summary']).strip()[:180], "clean_link": real_url
    }

# ---------------------------------------------------------------------------
# 10. Pipeline Orchestrator & Dispatcher
# ---------------------------------------------------------------------------
def main():
    products = load_products()
    seen = load_seen()

    time_query = "when:14d" if len(seen) < 10 else "when:3d"
    max_age = 14 if len(seen) < 10 else 3

    candidates = []
    for prod in products:
        items = fetch_all_opportunities(prod, time_query, max_age)
        for item in items:
            if item["link"] in seen: continue
            seen.add(item["link"])
            save_seen(item["link"])

            if COMMERCIAL_PATTERNS.search(f"{item['title']} {item['summary']}"):
                item["real_link"] = unwrap_destination_url(item["link"])
                candidates.append(item)

    if not candidates: return
    leads_recorded = 0
    batch_size = 10

    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        evaluations = try_gemini_analysis(batch)

        if evaluations:
            for res_dict in evaluations:
                idx = res_dict.get("item_index")
                if idx is not None and idx < len(batch) and res_dict.get("is_lead") is True:
                    dispatch_lead(batch[idx], res_dict)
                    leads_recorded += 1
        else:
            for item in batch:
                res_dict = extract_lead_locally(item, item["real_link"])
                if res_dict.get("is_lead") is True:
                    dispatch_lead(item, res_dict)
                    leads_recorded += 1

def dispatch_lead(item, data):
    prod = item["product"]
    org = data.get("org", "Commercial Buyer")
    address = data.get("address", "India")
    state = data.get("state", "Pan-India")
    real_link = item.get("real_link", item.get("clean_link", item["link"]))
    
    website = extract_base_website(real_link)
    if "google.com" in website: website = "Domain Hidden by Google"

    ltype, contact, email, phone = data.get("lead_type", "Commercial Lead"), data.get("contact_person", "Not Listed"), data.get("email", "Not Listed"), data.get("phone", "Not Listed")
    summary, estimated_value, quantity, deadline, emd_fee = data.get("summary", item['title']), data.get("estimated_value", "Not Disclosed"), data.get("quantity", "1 Requirement"), data.get("deadline", "Check Notice"), data.get("emd_fee", "N/A")
    priority, eligibility = data.get("priority", "⚡ Warm"), data.get("eligibility", "N/A")
    
    pub_date, app_date = format_pubdate(item.get("raw_pubdate", "")), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    log(f"\n>>> [CONFIRMED {priority}]: {item['title'][:70]}\n    Category: {ltype} | Org: {org} | State: {state}")

    is_new = push_to_google_sheet({
        "appearance_date": app_date, "published_date": pub_date, "deadline": deadline, "priority": priority, "product": prod, "type": ltype,
        "estimated_value": estimated_value, "quantity": quantity, "emd_fee": emd_fee, "org": org, "address": address, "state": state,
        "website": website, "contact_person": contact, "email": email, "phone": phone, "eligibility": eligibility, "summary": summary, "link": real_link
    })

    if is_new:
        emd_str = f"💳 *EMD / Tender Fee:* {emd_fee}\n" if emd_fee != 'N/A' else ""
        msg = (
            f"🚨 *Intelligence Signal Alert!*\n\n"
            f"🎯 *Priority Level:* {priority}\n"
            f"🏷 *Category:* {ltype}\n"
            f"🏢 *Entity / Buyer:* {org}\n"
            f"📦 *Product / Subject:* {prod} ({quantity})\n"
            f"💰 *Budget / Value:* {estimated_value}\n"
            f"⏳ *Key Deadline / Date:* `{deadline}`\n"
            f"{emd_str}"
            f"📍 *Location:* {address}, {state}\n"
            f"📅 *Published:* {pub_date}\n"
            f"👤 *Stakeholder / Contact:* {contact}\n"
            f"📧 *Email:* {email}\n"
            f"📞 *Phone:* {phone}\n"
            f"📝 *Sales Summary:* {summary}\n"
            f"📋 *Context:* {eligibility}\n"
            f"🌐 *Portal:* {website}\n\n"
            f"🔗 [Open Original Document Link]({real_link})"
        )
        send_telegram(msg, state)

if __name__ == "__main__":
    main()
