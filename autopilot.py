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
        
    print(f"\n🏁 CYCLE COMPLETE. Waiting for next window...", flush=True)

if __name__ == "__main__":
    print("🤖 CRM AUTOPILOT ENGAGED")
    print("⏰ Operating Hours: 08:00 to 20:00 (Running every 2 hours)")
    
    while True:
        current_hour = datetime.now().hour
        
        # Check if the time is between 8:00 AM (8) and 7:59 PM (19)
        if 8 <= current_hour < 20:
            run_crm_cycle()
            
            # Sleep for exactly 2 hours (7200 seconds) before running the next sweep
            print("\n💤 Sleeping for 120 minutes...", flush=True)
            time.sleep(7200) 
        else:
            print(f"🌙 Current Time: {datetime.now().strftime('%H:%M')}. Outside operating hours. Resting...", flush=True)
            # Sleep for 30 minutes, then wake up and check the clock again
            time.sleep(1800)
