import asyncio
import os
from dotenv import load_dotenv
from livekit import api

# Load environment variables
load_dotenv(".env")

async def main():
    # Initialize LiveKit API
    # Credentials (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) are auto-loaded from .env
    lkapi = api.LiveKitAPI()
    sip = lkapi.sip

    provider = os.getenv("TELEPHONY_PROVIDER", "MOBILE_SIM").upper()

    if provider == "MOBILE_SIM":
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID") or os.getenv("MOBILE_SIP_TRUNK_ID")
        address  = os.getenv("MOBILE_SIP_DOMAIN")
        username = os.getenv("MOBILE_SIP_USERNAME")
        password = os.getenv("MOBILE_SIP_PASSWORD")
        number   = os.getenv("MOBILE_OUTBOUND_NUMBER")
    elif provider == "VOBIZ":
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID") or os.getenv("VOBIZ_SIP_TRUNK_ID")
        address  = os.getenv("VOBIZ_SIP_DOMAIN")
        username = os.getenv("VOBIZ_USERNAME")
        password = os.getenv("VOBIZ_PASSWORD")
        number   = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    else:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID") or os.getenv("VOICELINK_SIP_TRUNK_ID")
        address  = os.getenv("VOICELINK_SIP_DOMAIN")
        username = os.getenv("VOICELINK_USERNAME")
        password = os.getenv("VOICELINK_PASSWORD")
        number   = os.getenv("VOICELINK_OUTBOUND_NUMBER")

    if not trunk_id:
        print(f"Error: OUTBOUND_TRUNK_ID / {provider}_SIP_TRUNK_ID not found in .env")
        return

    print(f"Updating SIP Trunk: {trunk_id}")
    print(f"  Provider: {provider}")
    print(f"  Address:  {address}")
    print(f"  Username: {username}")
    print(f"  Numbers:  [{number}]")

    try:
        # Update the trunk credentials
        await sip.update_outbound_trunk_fields(
            trunk_id,
            address=address,
            auth_username=username,
            auth_password=password,
            numbers=[number] if number else [],
        )
        print(f"\n[SUCCESS] SIP Trunk updated successfully with {provider} credentials!")

    except Exception as e:
        print(f"\n[ERROR] Failed to update trunk: {e}")
    finally:
        await lkapi.aclose()

if __name__ == "__main__":
    asyncio.run(main())

