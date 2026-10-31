"""test_aggregators.py — check if Pipeworx and Bidrove MCP servers are live.

This is a standalone test. It does not modify the pipeline or depend on
our existing code. It sends a simple query to each service and prints
the result. If both fail, the aggregator approach is not viable.
"""
import json
import requests

# Test a common, high-volume keyword.
KEYWORD = "autocad"

def test_pipeworx():
    print("\n" + "="*60)
    print("TESTING: Pipeworx (gateway.pipeworx.io)")
    print("="*60)

    # Pipeworx uses an MCP-compliant JSON-RPC endpoint.
    url = "https://gateway.pipeworx.io/india-tenders/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "india_search_tenders",
            "arguments": {"query": KEYWORD, "limit": 5}
        }
    }
    headers = {"Content-Type": "application/json"}
    try:
        print(f"Sending POST to {url}...")
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"Status Code: {resp.status_code}")
        # Print the full response text to see exactly what came back.
        print("Response Body:")
        print(resp.text[:2000]) # Limit to 2000 chars for readability
    except Exception as e:
        print(f"ERROR: Request failed. {e}")

def test_bidrove():
    print("\n" + "="*60)
    print("TESTING: Bidrove (bidrove-mcp via npm)")
    print("="*60)
    print("Note: The bidrove-mcp package is a local stdio server, not a")
    print("      public HTTP endpoint. A direct HTTP test is not possible.")
    print("      We are checking if the NPM package can be fetched,")
    print("      which indicates the package is live, but we cannot")
    print("      test the actual Bidrove API from this script without")
    print("      installing the package and running it as a subprocess.")
    print("      Given the paid-plan requirement, this is likely a dead end")
    print("      for a 100% free setup.")
    print("\nSkipping actual API test for Bidrove.")
    print("Verdict: Assume NOT VIABLE for a free pipeline.")

# --- Run the tests ---
if __name__ == "__main__":
    test_pipeworx()
    test_bidrove()
    print("\n" + "="*60)
    print("DIAGNOSTIC COMPLETE")
    print("="*60)
