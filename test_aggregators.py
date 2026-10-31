"""test_aggregators.py — pass 5: gem feed, get_tender, search schema."""
import json
import requests

URL = "https://gateway.pipeworx.io/india-tenders/mcp"
HEADERS = {"Content-Type": "application/json"}

def call(name, args):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": args}}
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=60)
        data = r.json()
        content = data.get("result", {}).get("content", [])
        if content and content[0].get("text"):
            return json.loads(content[0]["text"])
        return {"_error": str(data)[:300]}
    except Exception as e:
        return {"_error": str(e)}

print("="*60)
print("TEST 1: india_cppp_latest_tenders feed=gem")
print("="*60)
result = call("india_cppp_latest_tenders", {"feed": "gem"})
if "_error" in result:
    print(f"ERROR: {result['_error']}")
else:
    print(f"Keys: {list(result.keys())}")
    tenders = result.get("tenders", [])
    print(f"Returned: {len(tenders)} tenders")
    if tenders:
        print("First tender (full JSON):")
        print(json.dumps(tenders[0], indent=2)[:1000])

print("\n" + "="*60)
print("TEST 2: india_get_tender for DRDO tender_id")
print("="*60)
result = call("india_get_tender", {"tender_id": "2026_DRDO_796637_1"})
print(f"Full response (truncated):")
print(json.dumps(result, indent=2)[:2000])

print("\n" + "="*60)
print("TEST 3: india_search_tenders 'software' — full schema of one tender")
print("="*60)
result = call("india_search_tenders", {"query": "software", "limit": 5})
if "_error" in result:
    print(f"ERROR: {result['_error']}")
else:
    tenders = result.get("tenders", [])
    print(f"Returned: {len(tenders)} tenders")
    if tenders:
        print("First tender (full JSON):")
        print(json.dumps(tenders[0], indent=2)[:1000])

print("\n" + "="*60)
print("DONE")
