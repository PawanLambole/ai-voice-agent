import argparse
import asyncio
import os
import random
import json
from dotenv import load_dotenv
from livekit import api
from voicelink_client import normalize_indian_number

# Load environment variables
load_dotenv(".env")

async def main():
    parser = argparse.ArgumentParser(description="Make an outbound call via LiveKit Agent.")
    parser.add_argument("--to", required=True, help="The phone number to call (e.g., 9766573966 or +91...)")
    args = parser.parse_args()

    # 1. Validation & Normalization
    raw_input = args.to.strip()
    norm = normalize_indian_number(raw_input)
    phone_number = norm["full_e164"]

    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not (url and api_key and api_secret):
        print("Error: LiveKit credentials missing in .env.local")
        return

    # 2. Setup API Client
    lk_api = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)

    # 3. Create a unique room for this call
    # We use a random suffix to ensure room names are unique
    room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"

    print(f"Initating call to {phone_number}...")
    print(f"Session Room: {room_name}")

    try:
        # 4. Dispatch the Agent
        # We explicitly tell LiveKit to send the 'outbound-caller' agent to this room.
        # We pass the phone number in the 'metadata' field so the agent knows who to dial.
        dispatch_request = api.CreateAgentDispatchRequest(
            agent_name="outbound-caller", # Must match agent.py
            room=room_name,
            metadata=json.dumps({"phone_number": phone_number})
        )
        
        dispatch = await lk_api.agent_dispatch.create_dispatch(dispatch_request)

        print("\n[SUCCESS] Call Dispatched Successfully!")
        print(f"Dispatch ID: {dispatch.id}")
        print("-" * 40)
        print("The agent is now joining the room and will dial the number.")
        print("Check your agent terminal for logs.")
        
    except Exception as e:
        print(f"\n[ERROR] Error dispatching call: {e}")
    
    finally:
        await lk_api.aclose()

if __name__ == "__main__":
    asyncio.run(main())
