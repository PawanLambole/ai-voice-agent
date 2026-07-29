"""
VoiceLink REST API Client & Telephone Helper for InboundAIVoice.

Provides number normalization and API integration for VoiceLink (https://app.voicelink.co.in/api).
Supports both direct Bearer token auth and automatic login via /v1/auth/login.
"""

import os
import re
import json
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger("voicelink-client")

DEFAULT_API_BASE = "https://app.voicelink.co.in/api"


def normalize_indian_number(phone: str, default_country_code: str = "91") -> Dict[str, str]:
    """
    Normalize Indian phone numbers for VoiceLink.
    
    VoiceLink's carrier rejects full 12-digit E.164 numbers (e.g. 919307512816)
    with cause "38 - Network out of order". It expects the 10-digit national number
    (e.g. 9307512816) plus a separate country_code ("91").
    """
    digits = re.sub(r"[^\d]", "", phone.strip())
    
    # 10 digits (e.g. 9766573966)
    if len(digits) == 10:
        customer_number = digits
        country_code = default_country_code
    # 12 digits starting with 91 (e.g. 919766573966)
    elif len(digits) == 12 and digits.startswith("91"):
        customer_number = digits[2:]
        country_code = "91"
    # 11 digits starting with 0 (e.g. 09766573966)
    elif len(digits) == 11 and digits.startswith("0"):
        customer_number = digits[1:]
        country_code = default_country_code
    else:
        customer_number = digits
        country_code = default_country_code

    return {
        "customer_number": customer_number,
        "country_code": country_code,
        "full_e164": f"+{country_code}{customer_number}",
        "national_10": customer_number,
    }


class VoiceLinkClient:
    def __init__(
        self,
        api_base: Optional[str] = None,
        bearer_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout_sec: float = 15.0,
    ):
        base = api_base or os.getenv("VOICELINK_API_BASE") or DEFAULT_API_BASE
        self.api_base = base.rstrip("/")
        self.bearer_token = bearer_token or os.getenv("VOICELINK_RESELLER_TOKEN") or ""
        self.username = username or os.getenv("VOICELINK_USERNAME") or os.getenv("VOICELINK_RESELLER_USERNAME")
        self.password = password or os.getenv("VOICELINK_PASSWORD") or os.getenv("VOICELINK_RESELLER_PASSWORD")
        self.timeout_sec = timeout_sec

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def refresh_bearer(self) -> str:
        """Log in via POST /v1/auth/login to mint a new bearer token."""
        if not self.username or not self.password:
            raise ValueError("Cannot refresh bearer token: VOICELINK_USERNAME/PASSWORD not set")

        url = f"{self.api_base}/v1/auth/login"
        logger.info(f"[VOICELINK] Logging in to {url} as user {self.username}...")
        
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(
                url,
                json={"username": self.username, "password": self.password},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"VoiceLink auth login failed ({resp.status_code}): {resp.text}")

            data = resp.json()
            token = data.get("data", {}).get("access_token") or data.get("access_token")
            if not token:
                raise RuntimeError(f"VoiceLink auth login response missing access_token: {data}")

            self.bearer_token = token
            logger.info("[VOICELINK] Bearer token successfully refreshed.")
            return token

    def originate_call(
        self,
        did_number: str,
        phone_number: str,
        custom_parameters: Optional[str] = None,
        websocket_url: Optional[str] = None,
        webhook_url: Optional[str] = None,
        call_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Originate outbound call via POST /v1/add_lead.
        """
        norm = normalize_indian_number(phone_number)
        clean_did = re.sub(r"[^\d]", "", did_number)

        payload: Dict[str, Any] = {
            "did_number": clean_did,
            "customer_number": norm["customer_number"],
            "country_code": norm["country_code"],
        }
        if custom_parameters is not None:
            payload["custom_parameters"] = custom_parameters
        if websocket_url is not None:
            payload["websocket_url"] = websocket_url
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
        if call_limit is not None:
            payload["call_limit"] = call_limit

        url = f"{self.api_base}/v1/add_lead"
        logger.info(
            f"[VOICELINK] Calling add_lead: did={clean_did}, customer={norm['customer_number']}, cc={norm['country_code']}"
        )

        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, json=payload, headers=self._get_headers())

            # Auto-retry once on 401 if credentials exist
            if resp.status_code == 401 and self.username and self.password:
                logger.warning("[VOICELINK] Received 401 Unauthorized — attempting to refresh Bearer token...")
                self.refresh_bearer()
                resp = client.post(url, json=payload, headers=self._get_headers())

            if resp.status_code not in (200, 201):
                raise RuntimeError(f"VoiceLink add_lead failed ({resp.status_code}): {resp.text}")

            result = resp.json()
            logger.info(f"[VOICELINK] add_lead response: {result}")
            return result
