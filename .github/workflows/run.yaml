import os, sys, json, re, time, urllib.parse, io
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import List

try: from pypdf import PdfReader
except ImportError: PdfReader = None

try: from duckduckgo_search import DDGS
except ImportError: DDGS = None

def log(msg): print(msg, flush=True)

log(">>> ENTERPRISE RADAR ACTIVE: GOD-MODE B2B + BUYER INTENT + EMAIL GUESSER")

GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
PRODUCTS_FILE, STATES_FILE, NEGATIVE_FILE, SEEN_FILE = "products.txt", "states.txt", "negative_keywords.txt", "seen_links.txt"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36"})

COMMERCIAL_PATTERNS = re.compile(r"\b(tender|rfp|bid|gem|eprocure|procurement|supply|quotation|eoi|nit|hiring|vacancy|drafter|modeler|architect|engineer|capex|expansion|project win|contractor|consultancy|rera|dpr|empanelment|ipo|merger|series a|series b|raises funding|acquired|software license|tech upgrade|dealer|reseller|distributor|indiamart|training|partner|request for quotation|vendor registration|gcc|capability center)\b", re.IGNORECASE)
LOCATION_PATTERNS = re.compile(r"\b(Delhi|NCR|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|Noida|Gurgaon|Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat|Telangana|Haryana|Kerala|Rajasthan|Madhya Pradesh|Bihar|West Bengal|Andhra Pradesh|Punjab|Odisha)\b", re.IGNORECASE)
TECH_STACK_PATTERNS = re.compile(r"\b(AutoCAD|Revit|Civil 3D|Navisworks|Fusion 360|Advance Steel|Inventor|3ds Max|Tekla|STAAD|ETABS|SolidWorks|CATIA|Rhino|SketchUp|MicroStation|BIM 360)\b", re.IGNORECASE)

SELLER_MARKERS = ["indiamart", "tradeindia", "dealer", "reseller", "distributor", "authorized partner", "training center", "course", "tutorial", "wholesale", "gold partner"]
JUNK_MARKERS = ["housekeeping", "security guard", "catering", "canteen", "stationery", "taxi", "scrap", "medicines", "medical equipment", "sweeping", "printer cartridge", "photocopier"]

STATE_MAP = {'ncr': 'Delhi/NCR', 'noida': 'Uttar Pradesh', 'gurgaon': 'Haryana', 'mumbai': 'Maharashtra', 'pune': 'Maharashtra', 'bengaluru': 'Karnataka', 'chennai': 'Tamil Nadu', 'ahmedabad': 'Gujarat', 'hyderabad': 'Telangana', 'kolkata': 'West Bengal'}

class APIKeyPool:
    def __init__(self):
        self.keys = [k.strip() for k in (os.environ.get("GEMINI_API_KEYS") or "").split(",") if k.strip()]
        self.current_index = 0
    def get_client(self):
        if not self.keys: return None
        from google import genai
        return genai.Client(api_key=self.keys[self.current_index])
    def rotate(self):
        if len(self.keys) > 1:
            self.current_index = (self.current_index + 1) % len(self.keys)
            return True
        return False

KEY_POOL = APIKeyPool()

class LeadData(BaseModel):
    item_index: int
    is_lead: bool
    lead_type: str = Field(description="Govt Tender, Private Capex, Hiring Mandate, Corporate Lead, Suppliers, or Active Private Buyer (RFQ)")
    org: str
    entity_type: str = Field(description="Govt, PSU, Private, MNC, Startup, Training Institute, Channel Partner")
    industry: str
    hq: str
    est_size: str = Field(description="Estimated company/project size")
    dm_name: str = Field(description="Decision maker name if mentioned")
    dm_title: str = Field(description="Decision maker job title")
    email: str
    phone: str
    secondary_contact: str = Field(description="HR or secondary contact")
    boardline: str
    project_name: str
    project_stage: str
    project_scale: str
    total_investment: str
    epc: str = Field(description="EPC Contractor Builder")
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
    job_title: str = Field(description="Hiring: Job Title")
    vacancies: str
    exp_level: str
    salary: str
    tech_stack: str = Field(description="CAD/BIM software stack")
    competitor: str = Field(description="Competitor software mentioned")
    buying_intent: str = Field(description="High, Medium, or Low")
    urgency: str = Field(description="Immediate, Warm, or Strategic Nurture")
    sales_action: str = Field(description="1-sentence AI sales advice")
    pitch_angle: str = Field(description="1-sentence cold email opening line")
    summary: str

class LeadBatchResponse(BaseModel):
    leads: List[LeadData]

def free_b2b_enrichment(company_name, lead_type):
    data = {"name": "", "title": "", "url": "", "web": ""}
    if not DDGS or len(company_name) < 4: return data
    try:
        ddgs = DDGS()
        web_res = ddgs.text(f"{company_name} official website india", max_results=1)
        if web_res: data["web"] = web_res[0].get("href", "")
        role = '"Head of BIM" OR Architect OR HR' if "hiring" in lead_type.lower() else 'Procurement OR Director'
        li_res = ddgs.text(f'"{company_name}" ({role}) site:linkedin.com/in/', max_results=1)
        if li_res:
            data["url"] = li_res[0].get("href", "")
            data["name"] = li_res[0].get("title", "").split(" - ")[0]
            data["title"] = "Found via LinkedIn Search"
        time.sleep(1)
    except: pass
    return data

def deep_scrape_content(url):
    try:
        r = SESSION.get(url, timeout=8, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, 'html.parser')
            for s in soup(["script", "style"]): s.extract()
            return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:10000]
    except: pass
    return ""

def push_to_sheet(payload):
    if GOOGLE_SHEET_WEBHOOK:
        try: SESSION.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=10, allow_redirects=False)
        except: pass

def fetch_all_opportunities(product):
    all_items = []
    seen = set()
    
    # UPGRADED B2B SEARCH QUERIES
    queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in) India when:7d',
        f'"{product}" ("request for quotation" OR RFQ OR "vendor registration" OR "supplier empanelment" OR "IT procurement") India when:7d',
        f'"{product}" (site:linkedin.com/posts OR site:linkedin.com/pulse) ("looking for vendors" OR "need quotes" OR "software procurement" OR "authorized partner") India when:7d',
        f'"{product}" (capex OR awarded OR expansion OR "Global Capability Center" OR GCC OR IPO OR reseller OR partner) India when:7d',
        f'"{product}" (hiring OR vacancy) (site:linkedin.com/jobs OR site:naukri.com) India when:7d'
    ]
    for q in queries:
        try:
            r = SESSION.get(f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en", timeout=8)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    l, t, d = [item.findtext(k, "") for k in ["link", "title", "description"]]
                    if l not in seen:
                        seen.add(l)
                        all_items.append({"title": t, "link": l, "summary": d, "product": product})
        except: pass
    return all_items

def try_gemini_analysis(batch):
    if not batch: return None
    items_block = "".join([f"\n--- ITEM {i} ---\nTitle: {x['title']}\nData: {x['summary'][:500]}\n" for i, x in enumerate(batch)])
    prompt = (
        "You are an elite B2B Sales AI for CAD/BIM software.\n"
        "REJECT (is_lead=False): ONLY non-software Junk Tenders (housekeeping, catering, medical, security, scrap, vehicles).\n"
        "ACCEPT (is_lead=True): Genuine CAD/BIM buyers, corporate/enterprise prospects, hiring roles, capex projects, AND Suppliers/Resellers/Training Institutes.\n"
        "CRITICAL CLASSIFICATION RULE 1: If the entity is a dealer, reseller, IT partner, or training institute, classify `lead_type` as '🤝 Suppliers'.\n"
        "CRITICAL CLASSIFICATION RULE 2: If the text is an RFQ, asking for quotes, or vendor registration for software, classify `lead_type` as '🛒 Active Private Buyer (RFQ)'.\n"
        "Extract all 38 firmographic, project, tender, and contact details. Use 'N/A' or 'Not Listed' if missing.\n"
        f"{items_block}"
    )
    client = KEY_POOL.get_client()
    if not client: return None
    try:
        from google.genai import types
        res = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LeadBatchResponse, temperature=0.1)
        )
        return json.loads(res.text).get("leads", [])
    except Exception as e:
        log(f"Gemini API Error: {e}")
        KEY_POOL.rotate()
    return None

def extract_lead_locally(item, real_url):
    text = f"{item['title']} {item['summary']}".lower()
    
    foreign_markers = ["singapore", "united states", "usa", " uk ", "canada", "dubai", "uae", "australia", "germany"]
    if any(m in text for m in foreign_markers): return {"is_lead": False}
    if any(jm in text for jm in JUNK_MARKERS): return {"is_lead": False}

    is_buyer = any(k in text for k in ["rfq", "request for quotation", "vendor registration", "supplier empanelment", "looking for vendors", "need quotes", "it procurement"])
    is_seller = any(sm in text for sm in SELLER_MARKERS)
    is_govt = any(k in text for k in ["gem.gov", "eprocure", "ireps", "tender", "nit", "bid", "corrigendum"])
    is_capex = any(k in text for k in ["capex", "project win", "awarded", "expansion", "empanelment"])
    is_hiring = any(k in text for k in ["hiring", "vacancy", "jobs", "opening"])
    is_corp = any(k in text for k in ["series a", "series b", "raises funding", "acquired", "acquisition", "merger", "ipo", "software", "license", "subscription", "gcc", "capability center"])
    
    if not (is_govt or is_capex or is_hiring or is_corp or is_seller or is_buyer): return {"is_lead": False}

    if is_buyer: ltype = "🛒 Active Private Buyer (RFQ)"
    elif is_seller: ltype = "🤝 Suppliers"
    elif is_govt: ltype = "🏛 Government Tender"
    elif is_hiring: ltype = "💼 Hiring Mandate"
    elif is_capex: ltype = "🏗 Capex & Projects"
    else: ltype = "🏢 Corporate Lead" 

    if is_seller: p_stage = "Channel/Partner Network"
    elif is_buyer: p_stage = "Active Procurement"
    elif is_govt: p_stage = "Tender & Bidding"
    elif is_hiring: p_stage = "Team Expansion"
    elif any(k in text for k in ["dpr", "clearance", "rera", "feasibility", "planned"]): p_stage = "Planning & Feasibility"
    elif any(k in text for k in ["epc", "civil work", "construction", "plant"]): p_stage = "Under Construction"
    else: p_stage = "Corporate Operations"

    raw_text = f"{item['title']} {item['summary']}"
    found_tools = list(set(TECH_STACK_PATTERNS.findall(raw_text)))
    if item['product'] not in found_tools: found_tools.insert(0, item['product'])
    tech_stack_str = ", ".join(found_tools)

    loc_match = LOCATION_PATTERNS.search(raw_text)
    address = loc_match.group(0).title() if loc_match else "India"
    state = STATE_MAP.get(address.lower(), "Pan-India")

    emails = EMAIL_REGEX.findall(raw_text)
    phones = PHONE_REGEX.findall(raw_text)
    
    intent = "High" if (is_govt or is_hiring or is_buyer) else "Medium"

    return {
        "item_index": 0, "is_lead": True, "lead_type": ltype,
        "org": item["title"].split("-")[-1].strip() if "-" in item["title"] else "Commercial Enterprise",
        "entity_type": "Partner/Supplier" if is_seller else "Commercial", "industry": "AEC / General", "hq": address, "est_size": "Unknown",
        "dm_name": "Not Listed", "dm_title": "Not Listed", "email": emails[0] if emails else "N/A", "phone": phones[0] if phones else "N/A",
        "secondary_contact": "N/A", "boardline": "N/A", "project_name": "N/A", "project_stage": p_stage, "project_scale": "N/A",
        "total_investment": "N/A", "epc": "N/A", "pmc": "N/A", "completion_date": "N/A",
        "tender_id": "N/A", "tender_value": "N/A", "emd": "N/A", "tender_fee": "N/A", "pre_bid": "N/A",
        "deadline": "N/A", "bid_opening": "N/A", "contract_duration": "N/A", "eligibility": "N/A",
        "job_title": "N/A", "vacancies": "N/A", "exp_level": "N/A", "salary": "N/A",
        "tech_stack": tech_stack_str, "competitor": "None", "buying_intent": intent, "urgency": "Warm",
        "sales_action": "Reach out with pricing immediately" if is_buyer else "Standard Follow-Up", "pitch_angle": "N/A", "summary": re.sub(r"<[^>]+>", " ", item['summary']).strip()[:180],
        "clean_link": real_url
    }

def dispatch_lead(item, d):
    prod = item["product"]
    org = d.get("org", "Buyer")
    
    enrich = free_b2b_enrichment(org, d.get("lead_type", ""))
    web = enrich["web"] or extract_base_website(item["link"])
    dm_name = enrich["name"] or d.get("dm_name", "Not Listed")
    dm_li = enrich["url"] or d.get("dm_linkedin", "N/A")
    dm_title = d.get("dm_title", "Not Listed")
    
    # ---------------------------------------------------------
    # NEW B2B EMAIL GUESSER LOGIC
    # ---------------------------------------------------------
    email = d.get("email", "N/A")
    if email == "N/A" or email == "Not Listed":
        valid_name = dm_name and dm_name.lower() not in ["not listed", "not found", "found via linkedin search"]
        valid_web = web and "google" not in web and "news" not in web and "eprocure" not in web and web != "Web Portal"
        
        if valid_name and valid_web:
            # Clean name (e.g., removes titles like "- Head of BIM")
            clean_name = re.sub(r'[^a-zA-Z\s]', '', dm_name.split('-')[0]).strip()
            parts = clean_name.split()
            domain = web.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
            
            if len(parts) >= 1 and "." in domain:
                f_name = parts[0].lower()
                l_name = parts[-1].lower() if len(parts) > 1 else ""
                
                if l_name:
                    email = f"⚠️ GUESSED: {f_name}.{l_name}@{domain} OR {f_name}@{domain}"
                else:
                    email = f"⚠️ GUESSED: {f_name}@{domain}"
    # ---------------------------------------------------------

    app_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    payload = {
        "appearance_date": app_date, "published_date": app_date,
        "buying_intent": d.get("buying_intent", "Medium"), "urgency": d.get("urgency", "Warm"),
        "sales_action": d.get("sales_action", "Outreach"), "pitch_angle": d.get("pitch_angle", ""),
        "org": org, "entity_type": d.get("entity_type", "Commercial"), "industry": d.get("industry", "AEC"),
        "hq": d.get("hq", "India"), "website": web, "est_size": d.get("est_size", "N/A"),
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
        "summary": d.get("summary", item["title"]), "link": item["link"],
        "type": d.get("lead_type", "Lead"), "state": d.get("hq", "India")
    }
    
    push_to_sheet(payload)

    def f(label, val):
        if val and str(val).lower() not in ["n/a", "not listed", "none", "unknown", "none detected", ""]:
            return f"{label} {val}\n"
        return ""

    msg = f"🚨 *Intelligence Signal Alert!*\n\n"
    msg += f"🎯 *Intent:* {payload['buying_intent']} | {payload['urgency']}\n"
    msg += f"💡 *AI Advice:* _{payload['sales_action']}_\n"
    msg += f"🗣️ *Pitch:* _{payload['pitch_angle']}_\n\n"
    
    msg += f"🏢 *Entity:* {org} ({payload['entity_type']})\n"
    msg += f(f"🏷 *Type:*", payload['type'])
    msg += f(f"🏭 *Industry:*", payload['industry'])
    
    msg += f(f"🏗 *Project:*", payload['project_name'])
    msg += f(f"📈 *Stage:*", payload['project_stage'])
    msg += f(f"📐 *Scale:*", payload['project_scale'])
    msg += f(f"💰 *Investment:*", payload['total_investment'])
    msg += f(f"👷 *EPC/Builder:*", payload['epc'])
    msg += f(f"🤝 *PMC:*", payload['pmc'])
    
    msg += f(f"🆔 *Tender ID:* `{payload['tender_id']}`", "") if payload['tender_id'] != 'N/A' else ""
    msg += f(f"📅 *Pre-Bid Meeting:* 🚨", payload['pre_bid'])
    msg += f(f"⏳ *Deadline:*", payload['deadline'])
    msg += f(f"💵 *Value:*", payload['tender_value'])
    
    msg += f(f"💼 *Hiring:*", payload['job_title'])
    msg += f(f"👥 *Vacancies:*", payload['vacancies'])
    msg += f(f"🎓 *Experience:*", payload['exp_level'])
    
    msg += f"\n🛠 *Tech Stack:* `{payload['tech_stack']}`\n"
    msg += f(f"⚔️ *Competitors:*", payload['competitor'])
    
    msg += f"\n👤 *DM:* {dm_name}"
    msg += f" - {dm_title}" if dm_title != "Not Listed" else ""
    msg += "\n"
    if dm_li != "N/A": msg += f"🔗 *LinkedIn:* [View Profile]({dm_li})\n"
    
    msg += f(f"📧 *Email:*", payload['email'])
    msg += f(f"📞 *Phone:*", payload['phone'])
    msg += f(f"🏢 *Boardline:*", payload['boardline'])
    msg += f"📍 *Location:* {payload['hq']}\n"
    msg += f"🌐 *Portal:* {web}\n\n"
    msg += f"🔗 [Open Original Document]({item['link']})"

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    
    log(f"    >>> [RECORDED]: {org} | Type: {payload['type']} | Intent: {payload['buying_intent']}")

def main():
    if not os.path.exists(PRODUCTS_FILE): return
    prods = [l.strip() for l in open(PRODUCTS_FILE) if l.strip()]
    for p in prods:
        items = fetch_all_opportunities(p)
        for i in range(0, len(items), 10):
            batch = items[i:i+10]
            
            clean_batch = []
            for item in batch:
                combined = f"{item['title']} {item['summary']}".lower()
                if not any(jm in combined for jm in JUNK_MARKERS):
                    clean_batch.append(item)
                    
            if not clean_batch: continue

            evals = try_gemini_analysis(clean_batch)
            if evals:
                for res in evals:
                    d = res if isinstance(res, dict) else res.model_dump()
                    if d.get("is_lead"): dispatch_lead(clean_batch[d.get("item_index", 0) % len(clean_batch)], d)
            else:
                for item in clean_batch:
                    d = extract_lead_locally(item, item["link"])
                    if d.get("is_lead"): dispatch_lead(item, d)

if __name__ == "__main__": main()
