import time
import asyncio
from datetime import datetime

# Import your three CRM engines
import radar
import deep_hunter
import deep_intel

def run_crm_cycle():
    print(f"\n=======================================================", flush=True)
    print(f"🚀 STARTING FULL CRM CYCLE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"=======================================================\n", flush=True)
    
    # 1. Run The Scout
    try:
        radar.run()
    except Exception as e:
        print(f"⚠️ Radar Error: {e}")
        
    time.sleep(5) # Brief pause to let Google Sheets catch up
    
    # 2. Run The Hunter
    try:
        asyncio.run(deep_hunter.hunt_async())
    except Exception as e:
        print(f"⚠️ Hunter Error: {e}")
        
    time.sleep(5)
    
    # 3. Run The Analyst
    try:
        asyncio.run(deep_intel.run_intel())
    except Exception as e:
        print(f"⚠️ Analyst Error: {e}")
        
    print(f"\n🏁 ALL 3 CYCLES COMPLETE.", flush=True)

if __name__ == "__main__":
    print("🤖 CRM AUTOPILOT ENGAGED (Cloud Mode)")
    
    # Run exactly ONCE and then exit. 
    # GitHub Actions will handle scheduling the next run.
    run_crm_cycle()
    
    print("✅ Shutting down script to save GitHub Actions minutes.")
