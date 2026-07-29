import asyncio
import os
from dotenv import load_dotenv
from livekit import api

# Load environment variables
load_dotenv(".env")

async def setup_inbound():
    lk = api.LiveKitAPI()
    try:
        # 1. List existing trunks to find inbound trunk ID
        inbound_trunks = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        trunk_ids = [t.sip_trunk_id for t in inbound_trunks.items]
        
        # Update inbound trunk to include user DID numbers only
        did_number = os.getenv("VOICELINK_DID_NUMBER", "919429391395")
        numbers_to_set = list(set([did_number, f"+{did_number}"]))
        
        for t_id in trunk_ids:
            await lk.sip.update_inbound_trunk_fields(t_id, numbers=numbers_to_set)
            print(f"[SUCCESS] Updated Inbound Trunk {t_id} numbers: {numbers_to_set}")
        
        # 2. Check if dispatch rule already exists
        rules = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        for r in rules.items:
            print(f"Existing Dispatch Rule: ID={r.sip_dispatch_rule_id}, Name={r.name}")
            
        # 3. Create or confirm Inbound Dispatch Rule
        if not rules.items:
            rule = api.SIPDispatchRule(
                dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                    room_prefix="inbound_"
                )
            )
            req = api.CreateSIPDispatchRuleRequest(
                name="Inbound Call Rule",
                trunk_ids=trunk_ids,
                rule=rule,
            )
            res = await lk.sip.create_dispatch_rule(req)
            print(f"[SUCCESS] Created Inbound Dispatch Rule: {res.sip_dispatch_rule_id}")
        else:
            print("[INFO] Inbound Dispatch Rule is already active.")
            
    except Exception as e:
        print(f"[ERROR] Failed to configure inbound dispatch rule: {e}")
    finally:
        await lk.aclose()

if __name__ == "__main__":
    asyncio.run(setup_inbound())
