import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from google import genai
from google.genai.errors import APIError
import requests

print(">>> ENGINE STARTING...")
print(f"Current Directory: {os.getcwd()}")
print(f"Directory Contents: {os.listdir('.')}")

# ---------------------------------------------------------------------------
# 1. Environment Secrets
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

print(f"GEMINI_API_KEY Present: {bool(GEMINI_API_KEY)}")
print(f"TELEGRAM_BOT_TOKEN Present: {bool(TELEGRAM_BOT_TOKEN)}")
print(f"TELEGRAM_CHAT_ID Present: {bool(TELEGRAM_CHAT_ID)}")
print(f"GOOGLE_SHEET_WEBHOOK Present: {bool(GOOGLE_SHEET_WEBHOOK)}")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PROCUREMENT_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|license|licenses|subscription|renewal|contract)\b",
    re.IGNORECASE,
)


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        print(f"CRITICAL: {PRODUCTS_FILE} does not exist in repository!")
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        prods = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]
    print(f"Loaded {len(prods)} products from {PRODUCTS_FILE}: {prods}")
    return prods


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")


def push_to_google_sheet(product, ltype, org, contact, email, phone, summary, link):
    if not GOOGLE_SHEET_WEBHOOK:
        print("  -> Google Sheet webhook not set. Skipping.")
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "product": product,
        "type": ltype,
        "org": org,
        "contact_person": contact,
        "email": email,
        "phone": phone,
        "summary": summary,
        "link": link,
    }
    try:
        res = requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=15)
        print(f"  -> Sheet updated! Status: {res.status_code}")
    except Exception as e:
        print(f"  -> Sheet push error: {e}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  -> Telegram secrets not set. Skipping.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        res = requests.post(url, json=payload, timeout=12)
        print(f"  -> Telegram sent! Status: {res.status_code}")
    except Exception as e:
        print(f"  -> Telegram error: {e}")


def fetch_opportunities(product):
    queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:tenderdetail.com OR site:tender247.com)',
        f'"{product}" (tender OR "RFP" OR "NIT" OR "bid document") (portal OR department OR corporation OR university) India',
        f'{product} ("procurement of software licenses" OR "annual subscription" OR "rate contract") India',
    ]

    all_items = []
    seen_urls = set()

    for q in queries:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    link = item.findtext("link", "")
                    title = item.findtext("title", "")
                    desc = item.findtext("description", "")
                    if link and title and link not in seen_urls:
                        seen_urls.add(link)
                        all_items.append({"title": title, "link": link, "summary": desc})
        except Exception as e:
            print(f"Fetch error on query: {e}")

    return all_items[:15]


def batch_analyze_with_ai(client, product, batch):
    items_text = ""
    for idx, item in enumerate(batch):
        items_text += f"\n--- ITEM {idx} ---\nTitle: {item['title']}\nSnippet: {item['summary']}\nLink: {item['link']}\n"

    prompt = f"""
    You are an Indian commercial procurement specialist and tender analyst.
    Evaluate the following search items for commercial opportunities related to: "{product}".

    {items_text}

    Mark "is_lead": true if the item indicates ANY commercial requirement in India:
    - Government, PSU, defense, rail, or municipal corporation tenders mentioning CAD/software requirements
    - State/Central university, IIT, NIT, or polytechnic software lab procurement
    - GeM bids, RFPs, Expressions of Interest (EOI), or Notice Inviting Tenders (NIT)
    - Architecture, infrastructure, or construction tenders specifying Autodesk/AutoCAD/BIM software execution
    - Software reseller/distributor empanelment or enterprise license renewal notices

    Mark "is_lead": false ONLY for pure software tutorials, crack/piracy downloads, or generic corporate quarterly financial reports.

    Extract the organization name, contact officer, official email, and phone number whenever present (use "Not Listed" if missing).

    Reply ONLY with a raw JSON list matching this format:
    [
      {{
        "item_index": 0,
        "is_lead": true or false,
        "lead_type": "Govt Tender / GeM Bid / University Lab RFP / Corporate RFP",
        "org": "Organization, Department, or Authority Name",
        "contact_person": "Officer Name or Not Listed",
        "email": "Email or Not Listed",
        "phone": "Phone or Not Listed",
        "summary": "1 concise sentence stating the scope of software or work required",
        "rejection_reason": "Reason if false, otherwise empty"
      }}
    ]
    """

    delays = [5, 15, 30]
    for attempt, wait_time in enumerate(delays):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
            )
            raw = (
                response.text.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            return json.loads(raw)
        except APIError as e:
            if e.code in (429, 503):
                print(f"  [AI Throttled ({e.code})] Waiting {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"  [AI API Error]: {e}")
                return []
        except Exception as e:
            print(f"  [AI Parse Error]: {e}")
            return []

    return []


def main():
    if not GEMINI_API_KEY:
        print("FATAL ERROR: GEMINI_API_KEY secret is completely missing from GitHub Secrets!")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    if not products:
        print("FATAL ERROR: No products to scan. Check products.txt file!")
        sys.exit(1)

    print(f"Monitoring Products: {products}")
    total_leads = 0

    for prod in products:
        print(f"\n==========================================")
        print(f"Scanning for: {prod}")
        entries = fetch_opportunities(prod)
        print(f"Found {len(entries)} candidate items on web.")

        to_evaluate = []
        for entry in entries:
            link = entry["link"]
            text_blob = f"{entry['title']} {entry['summary']}"

            if link in seen:
                continue

            seen.add(link)
            save_seen(link)

            if PROCUREMENT_PATTERNS.search(text_blob):
                to_evaluate.append(entry)
            else:
                print(f"  [Skipped Local Filter - No Tender Terms]: {entry['title'][:55]}...")

        if not to_evaluate:
            print(f"No potential tender candidates for {prod}.")
            continue

        print(f"Evaluating {len(to_evaluate)} pre-qualified items with Gemini...")

        chunk_size = 5
        for i in range(0, len(to_evaluate), chunk_size):
            chunk = to_evaluate[i : i + chunk_size]
            results = batch_analyze_with_ai(client, prod, chunk)

            for res in results:
                idx = res.get("item_index", 0)
                if idx < len(chunk):
                    item = chunk[idx]
                    if res.get("is_lead") is True:
                        total_leads += 1
                        org = res.get("org", "Govt / Corporate Buyer")
                        ltype = res.get("lead_type", "Software Procurement")
                        contact = res.get("contact_person", "Not Listed")
                        email = res.get("email", "Not Listed")
                        phone = res.get("phone", "Not Listed")
                        lead_summary = res.get("summary", "Procurement requirement identified.")

                        print(f"\n>>> [LEAD APPROVED]: {item['title'][:70]}")
                        print(f"    Buyer: {org} | Type: {ltype}")

                        push_to_google_sheet(
                            prod, ltype, org, contact, email, phone, lead_summary, item["link"]
                        )

                        msg = (
                            f"🚨 *New Indian Commercial Lead!*\n\n"
                            f"📦 *Product:* {prod}\n"
                            f"🏛 *Buyer / Org:* {org}\n"
                            f"📋 *Type:* {ltype}\n"
                            f"👤 *Contact Person:* {contact}\n"
                            f"📧 *Email:* {email}\n"
                            f"📞 *Phone:* {phone}\n"
                            f"📝 *Summary:* {lead_summary}\n\n"
                            f"🔗 [Open Procurement Link]({item['link']})"
                        )
                        send_telegram(msg)
                    else:
                        reason = res.get("rejection_reason", "Not a real procurement lead")
                        print(f"  [AI Rejected]: {item['title'][:50]}... (Reason: {reason})")

    print(f"\n==========================================")
    print(f"Total qualified leads logged: {total_leads}")


if __name__ == "__main__":
    main()
