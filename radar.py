import json
import os
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from google import genai
import requests

# 1. Load Secrets
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

# Modern User-Agent header so Google doesn't block GitHub Actions runners
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


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
        print(f"Sheet Response: Status {res.status_code}")
    except Exception as e:
        print(f"Sheet push error: {e}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
        print(f"Telegram Response: Status {res.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")


def fetch_opportunities(product):
    """Fetches real listings with browser headers to prevent blocks."""
    # Build a query specifically tailored for software licenses/subscriptions in India
    query = f'"{product}" (tender OR procurement OR "gem.gov.in" OR RFP OR licenses OR "subscription renewal")'
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"

    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200 and resp.content:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:8]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "")
                if link and title:
                    items.append({"title": title, "link": link, "summary": desc})
    except Exception as e:
        print(f"Fetch error for {product}: {e}")

    return items


def analyze_with_ai(client, product, title, summary):
    prompt = f"""
    You are an enterprise software sales and tender evaluation agent in India.
    Analyze this item for Autodesk/CAD software requirement: "{product}".

    Title: {title}
    Snippet: {summary}

    Identify if this indicates a commercial buying requirement, tender, license procurement, contract award, or RFP in India.
    Extract contact person, email, or phone if available (otherwise "Not Listed").

    Reply ONLY with a raw JSON object (no markdown backticks):
    {{
      "is_lead": true,
      "lead_type": "Software Tender / GeM Bid / Corporate License RFP",
      "org": "Name of Authority, PSU, University, or Enterprise",
      "contact_person": "Officer Name or Not Listed",
      "email": "Email address or Not Listed",
      "phone": "Phone number or Not Listed",
      "summary": "1 concise sentence describing the software requirements or seats needed"
    }}
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        clean = (
            response.text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(clean)
    except Exception as e:
        print(f"AI Parse error: {e}")
        return {"is_lead": False}


def main():
    if not GEMINI_API_KEY:
        print("Missing GEMINI_API_KEY.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    print(f"Scanning for software products: {products}")
    total_leads = 0

    for prod in products:
        print(f"\n--- Scanning: {prod} ---")
        entries = fetch_opportunities(prod)
        print(f"Items found: {len(entries)}")

        for entry in entries:
            link = entry["link"]
            title = entry["title"]
            summary = entry["summary"]

            if link in seen:
                continue

            analysis = analyze_with_ai(client, prod, title, summary)

            if analysis.get("is_lead") is True:
                total_leads += 1
                org = analysis.get("org", "Govt / Corporate Buyer")
                ltype = analysis.get("lead_type", "Software Procurement")
                contact = analysis.get("contact_person", "Not Listed")
                email = analysis.get("email", "Not Listed")
                phone = analysis.get("phone", "Not Listed")
                lead_summary = analysis.get("summary", "N/A")

                print(f"[QUALIFIED LEAD]: {title[:60]}")
                push_to_google_sheet(
                    prod, ltype, org, contact, email, phone, lead_summary, link
                )

                msg = (
                    f"🚨 *New Software Requirement Lead!*\n\n"
                    f"📦 *Product:* {prod}\n"
                    f"🏛 *Buyer / Org:* {org}\n"
                    f"📋 *Type:* {ltype}\n"
                    f"👤 *Contact Person:* {contact}\n"
                    f"📧 *Email:* {email}\n"
                    f"📞 *Phone:* {phone}\n"
                    f"📝 *Summary:* {lead_summary}\n\n"
                    f"🔗 [Open Procurement Link]({link})"
                )
                send_telegram(msg)

            seen.add(link)
            save_seen(link)

    print(f"\nTotal qualified leads logged this run: {total_leads}")


if __name__ == "__main__":
    main()
