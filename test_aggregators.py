"""test_aggregators.py — pass 2: discover tools + test multiple keywords."""
import json
import requests

PIPEWORX_URL = "https://gateway.pipeworx.io/india-tenders/mcp"
HEADERS = {"Content-Type": "application/json"}

def rpc(method, params=None, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        payload["params"] = params
    try:
        r = requests.post(PIPEWORX_URL, json=payload, headers=HEADERS, timeout=30)
        return r.status_code, r.json() if r.text else {}
    except Exception as e:
        return -1, {"error": str(e)}

print("=" * 60)
print("STEP 1: List available MCP tools")
print("=" * 60)
status, resp = rpc("tools/list")
print(f"HTTP {status}")
if "result" in resp and "tools" in resp["result"]:
    for tool in resp["result"]["tools"]:
        print(f"  • {tool.get('name')}")
        print(f"    {tool.get('description', '')[:200]}")
else:
    print(f"Unexpected response: {str(resp)[:500]}")

print("\n" + "=" * 60)
print("STEP 2: Test multiple keywords against india_search_tenders")
print("=" * 60)

KEYWORDS = ["cad", "software", "revit", "autodesk", "bim", "design"]

for kw in KEYWORDS:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "india_search_tenders",
            "arguments": {"query": kw, "limit": 3}
        }
    }
    try:
        r = requests.post(PIPEWORX_URL, json=payload, headers=HEADERS, timeout=30)
        data = r.json()
        content = data.get("result", {}).get("content", [])
        if content and content[0].get("text"):
            inner = json.loads(content[0]["text"])
            matched = inner.get("matched", 0)
            returned = inner.get("returned", 0)
            scanned = len(inner.get("organisations_scanned", []))
            print(f"\n[{kw.upper()}] matched={matched}, returned={returned}, orgs_scanned={scanned}")

            # Show first 3 tender titles if any
            for i, t in enumerate(inner.get("tenders", [])[:3]):
                title = (t.get("title") or "")[:100]
                closing = t.get("closing_date") or ""
                print(f"    {i+1}. {title!r}")
                if closing:
                    print(f"       closing={closing}")
        else:
            print(f"\n[{kw.upper()}] empty response")
    except Exception as e:
        print(f"\n[{kw.upper()}] ERROR: {e}")

print("\n" + "=" * 60)
print("DONE")
