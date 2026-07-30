import os
import sys
import json
import logging
import certifi
import pytz
import re
import asyncio
import time
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

from typing import Annotated, Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        getattr(sys.stdout, "reconfigure")(encoding="utf-8")
        getattr(sys.stderr, "reconfigure")(encoding="utf-8")
    except Exception:
        pass

# Fix for macOS SSL certificate verification
os.environ["SSL_CERT_FILE"] = certifi.where()

# ── Sentry error tracking (#21) ───────────────────────────────────────────────
import sentry_sdk
_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    sentry_sdk.init(
        dsn=_sentry_dsn,
        traces_sample_rate=0.1,
        integrations=[AsyncioIntegration()],
        environment=os.environ.get("ENVIRONMENT", "production"),
    )

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

load_dotenv()
logger = logging.getLogger("outbound-agent")
logging.basicConfig(level=logging.INFO)

from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
    llm,
)
from livekit.plugins import openai, sarvam, silero

import db
from voicelink_client import normalize_indian_number, VoiceLinkClient

CONFIG_FILE = "config.json"

# ── Rate limiting (#37) ───────────────────────────────────────────────────────
_call_timestamps: dict = defaultdict(list)
RATE_LIMIT_CALLS  = 5
RATE_LIMIT_WINDOW = 3600  # 1 hour

def is_rate_limited(phone: str) -> bool:
    if phone in ("unknown", "demo"):
        return False
    now = time.time()
    _call_timestamps[phone] = [t for t in _call_timestamps[phone] if now - t < RATE_LIMIT_WINDOW]
    if len(_call_timestamps[phone]) >= RATE_LIMIT_CALLS:
        return True
    _call_timestamps[phone].append(now)
    return False


# ── Config loader (#17 partial — per-client path awareness) ───────────────────
def get_live_config(phone_number: str | None = None):
    """Load config — tries Supabase DB first, then per-client file, then default config.json."""
    config = {}
    try:
        import db
        db_cfg = db.load_config_from_db()
        if db_cfg:
            config = db_cfg
            logger.info(f"[CONFIG] Loaded live config from Supabase DB ({len(db_cfg)} keys).")
    except Exception as e:
        logger.warning(f"[CONFIG] Could not load from Supabase DB: {e}")

    if not config:
        paths = []
        if phone_number and phone_number != "unknown":
            clean = phone_number.replace("+", "").replace(" ", "")
            paths.append(f"configs/{clean}.json")
        paths += ["configs/default.json", CONFIG_FILE]

        for path in paths:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        config = json.load(f)
                        logger.info(f"[CONFIG] Loaded: {path}")
                        break
                except Exception as e:
                    logger.error(f"[CONFIG] Failed to read {path}: {e}")

    return {
        "agent_instructions":       config.get("agent_instructions", ""),
        "stt_min_endpointing_delay":config.get("stt_min_endpointing_delay", 0.05),
        "llm_model":                config.get("llm_model", "gpt-4o-mini"),
        "llm_provider":             config.get("llm_provider", "openai"),
        "tts_voice":                config.get("tts_voice", "kavya"),
        "tts_language":             config.get("tts_language", "hi-IN"),
        "tts_provider":             config.get("tts_provider", "sarvam"),
        "stt_provider":             config.get("stt_provider", "sarvam"),
        "stt_language":             config.get("stt_language", "unknown"),
        "lang_preset":              config.get("lang_preset", "multilingual"),
        "max_turns":                config.get("max_turns", 25),
        **config,
    }


# ── Token counter (#11) ───────────────────────────────────────────────────────
def count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        return len(enc.encode(text))
    except Exception:
        return len(text.split())


# ── IST time context ──────────────────────────────────────────────────────────
def get_ist_time_context() -> str:
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    today_str = now.strftime("%A, %B %d, %Y")
    time_str  = now.strftime("%I:%M %p")
    days_lines = []
    for i in range(7):
        day   = now + timedelta(days=i)
        label = "Today" if i == 0 else ("Tomorrow" if i == 1 else day.strftime("%A"))
        days_lines.append(f"  {label}: {day.strftime('%A %d %B %Y')} → ISO {day.strftime('%Y-%m-%d')}")
    days_block = "\n".join(days_lines)
    return (
        f"\n\n[SYSTEM CONTEXT]\n"
        f"Current date & time: {today_str} at {time_str} IST\n"
        f"Resolve ALL relative day references using this table:\n{days_block}\n"
        f"Always use ISO dates when calling save_booking_intent. Appointments in IST (+05:30).]"
    )


# ── Language presets ──────────────────────────────────────────────────────────
LANGUAGE_PRESETS = {
    "hinglish":    {"label": "Hinglish (Hindi+English)", "tts_language": "hi-IN", "tts_voice": "kavya",  "instruction": "Speak in natural Hinglish — mix Hindi and English like educated Indians do. Default to Hindi but use English words when more natural."},
    "hindi":       {"label": "Hindi",                   "tts_language": "hi-IN", "tts_voice": "ritu",   "instruction": "Speak only in pure Hindi. Avoid English words wherever a Hindi equivalent exists."},
    "english":     {"label": "English (India)",         "tts_language": "en-IN", "tts_voice": "dev",    "instruction": "Speak only in Indian English with a warm, professional tone."},
    "tamil":       {"label": "Tamil",                   "tts_language": "ta-IN", "tts_voice": "priya",  "instruction": "Speak only in Tamil. Use standard spoken Tamil for a professional context."},
    "telugu":      {"label": "Telugu",                  "tts_language": "te-IN", "tts_voice": "kavya",  "instruction": "Speak only in Telugu. Use clear, polite spoken Telugu."},
    "gujarati":    {"label": "Gujarati",                "tts_language": "gu-IN", "tts_voice": "rohan",  "instruction": "Speak only in Gujarati. Use polite, professional Gujarati."},
    "bengali":     {"label": "Bengali",                 "tts_language": "bn-IN", "tts_voice": "neha",   "instruction": "Speak only in Bengali (Bangla). Use standard, polite spoken Bengali."},
    "marathi":     {"label": "Marathi",                 "tts_language": "mr-IN", "tts_voice": "shubh",  "instruction": "Speak only in Marathi. Use polite, standard spoken Marathi."},
    "kannada":     {"label": "Kannada",                 "tts_language": "kn-IN", "tts_voice": "rahul",  "instruction": "Speak only in Kannada. Use clear, professional spoken Kannada."},
    "malayalam":   {"label": "Malayalam",               "tts_language": "ml-IN", "tts_voice": "ritu",   "instruction": "Speak only in Malayalam. Use polite, professional spoken Malayalam."},
    "multilingual":{"label": "Multilingual (Auto)",     "tts_language": "hi-IN", "tts_voice": "kavya",  "instruction": "Detect the caller's language from their first message and reply in that SAME language for the entire call. Supported: Hindi, Hinglish, English, Tamil, Telugu, Gujarati, Bengali, Marathi, Kannada, Malayalam. Switch if caller switches."},
}

def get_language_instruction(lang_preset: str) -> str:
    preset = LANGUAGE_PRESETS.get(lang_preset, LANGUAGE_PRESETS["multilingual"])
    return f"\n\n[LANGUAGE DIRECTIVE]\n{preset['instruction']}"


# ── External imports ──────────────────────────────────────────────────────────
from calendar_tools import get_available_slots, create_booking, cancel_booking
from notify import (
    notify_booking_confirmed,
    notify_booking_cancelled,
    notify_call_no_booking,
    notify_agent_error,
)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL CONTEXT — All AI-callable functions
# ══════════════════════════════════════════════════════════════════════════════

class AgentTools(llm.ToolContext):

    def __init__(self, caller_phone: str, caller_name: str = ""):
        super().__init__(tools=[])
        self.caller_phone        = caller_phone
        self.caller_name         = caller_name
        self.booking_intent: dict | None = None
        self.sip_domain          = os.getenv("VOICELINK_SIP_DOMAIN")
        self.ctx_api             = None
        self.room_name           = None
        self._sip_identity       = None

    # ── Tool: Transfer to Human ───────────────────────────────────────────
    @llm.function_tool(description="Transfer this call to a human agent. Use if: caller asks for human, is angry, or query is outside scope.")
    async def transfer_call(self, reason: Annotated[str, "Reason for transfer"] = "") -> str:
        logger.info(f"[TOOL] transfer_call triggered (reason: {reason})")
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER")
        if destination and self.sip_domain and "@" not in destination:
            clean_dest  = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean_dest}@{self.sip_domain}"
        if destination and not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        try:
            if self.ctx_api and self.room_name and destination and self._sip_identity:
                await self.ctx_api.sip.transfer_sip_participant(
                    api.TransferSIPParticipantRequest(
                        room_name=self.room_name,
                        participant_identity=self._sip_identity,
                        transfer_to=destination,
                        play_dialtone=False,
                    )
                )
                return "Transfer initiated successfully."
            return "Unable to transfer right now."
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return "Unable to transfer right now."

    # ── Tool: End Call ────────────────────────────────────────────────────
    @llm.function_tool(description="End the call. Use ONLY when caller says bye/goodbye or after booking is fully confirmed.")
    async def end_call(self, reason: Annotated[str, "Reason for ending call"] = "") -> str:
        logger.info(f"[TOOL] end_call triggered — hanging up (reason: {reason}).")
        try:
            if self.ctx_api and self.room_name and self._sip_identity:
                await self.ctx_api.sip.transfer_sip_participant(
                    api.TransferSIPParticipantRequest(
                        room_name=self.room_name,
                        participant_identity=self._sip_identity,
                        transfer_to="tel:+00000000",
                        play_dialtone=False,
                    )
                )
        except Exception as e:
            logger.warning(f"[END-CALL] SIP hangup failed: {e}")
        return "Call ended."

    # ── Tool: Save Booking Intent ─────────────────────────────────────────
    @llm.function_tool(description="Save booking intent after caller confirms appointment. Call this ONCE after you have name, phone, email, date, time.")
    async def save_booking_intent(
        self,
        start_time:  Annotated[str,  "ISO 8601 datetime e.g. '2026-03-01T10:00:00+05:30'"],
        caller_name: Annotated[str,  "Full name of the caller"],
        caller_phone:Annotated[str,  "Phone number of the caller"],
        notes:       Annotated[str,  "Any notes, email, or special requests"] = "",
    ) -> str:
        logger.info(f"[TOOL] save_booking_intent: {caller_name} at {start_time}")
        try:
            self.booking_intent = {
                "start_time":   start_time,
                "caller_name":  caller_name,
                "caller_phone": caller_phone,
                "notes":        notes,
            }
            self.caller_name = caller_name
            return f"Booking intent saved for {caller_name} at {start_time}. I'll confirm after the call."
        except Exception as e:
            logger.error(f"[TOOL] save_booking_intent failed: {e}")
            return "I had trouble saving the booking. Please try again."

    # ── Tool: Check Availability (#13) ────────────────────────────────────
    @llm.function_tool(description="Check available appointment slots for a given date. Call this when user asks about availability.")
    async def check_availability(
        self,
        date: Annotated[str, "Date to check in YYYY-MM-DD format e.g. '2026-03-01'"],
    ) -> str:
        logger.info(f"[TOOL] check_availability: date={date}")
        try:
            slots = get_available_slots(date)
            if not slots:
                return f"No available slots on {date}. Would you like to check another date?"
            slot_strings = [s.get("start_time", str(s))[-8:][:5] for s in slots[:6]]
            return f"Available slots on {date}: {', '.join(slot_strings)} IST."
        except Exception as e:
            logger.error(f"[TOOL] check_availability failed: {e}")
            return "I'm having trouble checking the calendar right now."

    # ── Tool: Business Hours (#31) ────────────────────────────────────────
    @llm.function_tool(description="Check if the business is currently open and what the operating hours are.")
    async def get_business_hours(self, query: Annotated[str, "General query about business hours"] = "") -> str:
        ist  = pytz.timezone("Asia/Kolkata")
        now  = datetime.now(ist)
        hours = {
            0: ("Monday",    "10:00", "19:00"),
            1: ("Tuesday",   "10:00", "19:00"),
            2: ("Wednesday", "10:00", "19:00"),
            3: ("Thursday",  "10:00", "19:00"),
            4: ("Friday",    "10:00", "19:00"),
            5: ("Saturday",  "10:00", "17:00"),
            6: ("Sunday",    None,    None),
        }
        day_name, open_t, close_t = hours[now.weekday()]
        current_time = now.strftime("%H:%M")
        if open_t is None or close_t is None:
            return "We are closed on Sundays. Next opening: Monday 10:00 AM IST."
        if open_t <= current_time <= close_t:
            return f"We are OPEN. Today ({day_name}): {open_t}–{close_t} IST."
        return f"We are CLOSED. Today ({day_name}): {open_t}–{close_t} IST."


# ══════════════════════════════════════════════════════════════════════════════
# AGENT CLASS
# ══════════════════════════════════════════════════════════════════════════════

VOICE_PERSONA_MAP = {
    # Sarvam / ElevenLabs Female Voices
    "kavya":  {"name": "Kavya",  "gender": "female", "verb": "bol rahi hoon", "ability": "kar sakti hoon"},
    "ritu":   {"name": "Ritu",   "gender": "female", "verb": "bol rahi hoon", "ability": "kar sakti hoon"},
    "priya":  {"name": "Priya",  "gender": "female", "verb": "bol rahi hoon", "ability": "kar sakti hoon"},
    "neha":   {"name": "Neha",   "gender": "female", "verb": "bol rahi hoon", "ability": "kar sakti hoon"},
    "shreya": {"name": "Shreya", "gender": "female", "verb": "bol rahi hoon", "ability": "kar sakti hoon"},
    "stella": {"name": "Stella", "gender": "female", "verb": "speaking",     "ability": "can help"},

    # Sarvam / ElevenLabs Male Voices
    "rahul":  {"name": "Rahul",  "gender": "male",   "verb": "bol raha hoon", "ability": "kar sakta hoon"},
    "rohan":  {"name": "Rohan",  "gender": "male",   "verb": "bol raha hoon", "ability": "kar sakta hoon"},
    "dev":    {"name": "Dev",    "gender": "male",   "verb": "bol raha hoon", "ability": "kar sakta hoon"},
    "shubh":  {"name": "Shubh",  "gender": "male",   "verb": "bol raha hoon", "ability": "kar sakta hoon"},
    "amit":   {"name": "Amit",   "gender": "male",   "verb": "bol raha hoon", "ability": "kar sakta hoon"},
}

def get_persona_for_voice(tts_voice: str):
    v = (tts_voice or "kavya").lower()
    if v in VOICE_PERSONA_MAP:
        return VOICE_PERSONA_MAP[v]
    return {"name": tts_voice.capitalize(), "gender": "female", "verb": "bol rahi hoon", "ability": "kar sakti hoon"}

class OutboundAssistant(Agent):

    def __init__(self, agent_tools: AgentTools, first_line: str = "", live_config: dict | None = None, is_outbound: bool = False):
        all_tools: Any = llm.find_function_tools(agent_tools)
        # Exclude calendar, availability, and booking intent tools as requested
        tools = [
            t for t in all_tools
            if getattr(t, "name", getattr(t, "__name__", "")) not in (
                "book_appointment", "check_availability", "save_booking_intent"
            )
        ]
        self._first_line   = first_line
        self._live_config  = live_config or {}
        self._is_outbound  = is_outbound
        live_config_loaded = self._live_config

        base_instructions = live_config_loaded.get("agent_instructions") or ""
        if len(base_instructions.strip()) < 20:
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    _cfg_bak = json.load(f)
                    base_instructions = _cfg_bak.get("agent_instructions", "")
            except Exception:
                pass

        ist_context       = get_ist_time_context()
        lang_preset       = live_config_loaded.get("lang_preset", "multilingual")
        lang_instruction  = get_language_instruction(lang_preset)

        tts_voice = live_config_loaded.get("tts_voice", "kavya")
        persona   = get_persona_for_voice(tts_voice)
        p_name    = persona["name"]
        p_verb    = persona["verb"]
        p_gender  = persona["gender"]

        # Adapt instructions for persona name & gender grammar
        adapted_base = base_instructions
        for old_name in ["Rahul", "Shreya", "Kavya", "Ritu", "Priya", "Neha", "Rohan", "Dev", "Shubh", "Amit", "Aryan"]:
            if old_name != p_name:
                adapted_base = adapted_base.replace(old_name, p_name)
        if p_gender == "female":
            adapted_base = (
                adapted_base
                .replace("bol raha hoon", "bol rahi hoon")
                .replace("kar sakta hoon", "kar sakti hoon")
                .replace("kar sakta", "kar sakti")
                .replace("karunga", "karungi")
                .replace("karta hoon", "karti hoon")
                .replace("karta", "karti")
                .replace("dunga", "dungi")
            )
        else:
            adapted_base = (
                adapted_base
                .replace("bol rahi hoon", "bol raha hoon")
                .replace("kar sakti hoon", "kar sakta hoon")
                .replace("kar sakti", "kar sakta")
                .replace("karungi", "karunga")
                .replace("karti hoon", "karta hoon")
                .replace("karti", "karta")
                .replace("dungi", "dunga")
            )
        raw_first_line = (live_config_loaded.get("first_line") or "Hello").strip()
        direction_ctx    = (
            "\n[CALL DIRECTION: Speak naturally as an employee handling the call."
            f" The Voice Platform opened with ONLY a simple '{raw_first_line}' — your name and company were NOT yet mentioned."
            " When the other person first replies (e.g. says hello, haan, ha, etc.), THEN introduce yourself:"
            " state your name and company naturally, then explain the purpose of the call."
            " Never ask 'aap kaun hain'. Speak naturally as the employee.]"
        )
        persona_context  = f"\n\n[DYNAMIC PERSONA: Your name is {p_name}. You are a {p_gender} employee at Kona Kona Interiors. Always use {p_gender} grammar ('{p_verb}', '{persona['ability']}').]{direction_ctx}"

        # ── Knowledge Base injection (#KB) ────────────────────────────────────
        try:
            kb_block = db.get_kb_for_prompt()
            if kb_block:
                logger.info(f"[KB] Injecting knowledge base into system prompt ({len(kb_block)} chars)")
        except Exception as _kb_err:
            kb_block = ""
            logger.warning(f"[KB] Could not load knowledge base: {_kb_err}")

        final_instructions = adapted_base + persona_context + kb_block + ist_context + lang_instruction

        # Token counter (#11)
        token_count = count_tokens(final_instructions)
        logger.info(f"[PROMPT] System prompt for persona {p_name} ({p_gender}): {token_count} tokens")
        if token_count > 600:
            logger.warning(f"[PROMPT] Prompt exceeds 600 tokens — consider trimming for latency")

        super().__init__(instructions=final_instructions, tools=tools)

    async def on_enter(self):
        # Greeting is explicitly triggered in entrypoint once audio connection is ready
        pass


def format_number_for_voicelink(phone: str) -> str:
    """Format phone number for VoiceLink SIP Outbound gateway (expects E.164 format with +91)."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return "+91" + digits
    elif digits.startswith("91") and len(digits) == 12:
        return "+" + digits
    elif digits.startswith("0") and len(digits) == 11:
        return "+91" + digits[1:]
    return "+" + digits if not phone.startswith("+") else phone


agent_is_speaking = False
_last_speech_end_time: float = 0.0   # timestamp when agent last finished speaking
_ECHO_SUPPRESS_S: float = 1.5         # suppress STT input this many seconds after agent stops speaking

async def entrypoint(ctx: JobContext):
    global agent_is_speaking

    # ── Connect ───────────────────────────────────────────────────────────
    await ctx.connect()
    logger.info(f"[ROOM] Connected: {ctx.room.name}")

    # ── Extract caller info ───────────────────────────────────────────────
    phone_number = None
    caller_name  = ""
    caller_phone = "unknown"

    # Try metadata first (outbound dispatch vs demo call)
    is_outbound = False
    is_demo = False
    phone = None
    metadata = ctx.job.metadata or ""
    if metadata:
        try:
            meta = json.loads(metadata)
            if isinstance(meta, str):
                phone = meta
                is_demo = meta.lower() == "demo"
            elif isinstance(meta, dict):
                is_demo = meta.get("is_demo", False)
                phone = meta.get("phone_number") or meta.get("phone")
            if phone and not is_demo and str(phone).lower() != "demo":
                is_outbound = True
                norm = normalize_indian_number(str(phone))
                phone_number = norm["full_e164"]
                logger.info(f"[OUTBOUND] Target phone number from metadata: {phone_number}")
        except Exception as e:
            logger.warning(f"[METADATA] Failed to parse job metadata: {e}")

    # Fallback: Infer outbound target from room name (e.g. call-919766573966-5401)
    if not is_outbound and ctx.room is not None and ctx.room.name:
        import re as _re
        rname = ctx.room.name
        if "call-" in rname or "outbound-" in rname or "call_" in rname:
            m = _re.search(r"(?:call|outbound)[-_](\d{10,15})[-_]?", rname)
            if m:
                raw_num = m.group(1)
                norm = normalize_indian_number(raw_num)
                phone_number = norm["full_e164"]
                is_outbound = True
                logger.info(f"[OUTBOUND] Target phone number inferred from room name '{rname}': {phone_number}")

    # ── DEMO OVERRIDE: Web demo calls (demo-XXXXX or is_demo=True) run as OUTBOUND flow ──
    room_name_str = ctx.room.name if (ctx.room is not None and ctx.room.name) else ""
    is_demo_call = (
        is_demo
        or (isinstance(phone, str) and phone.lower() == "demo")
        or ("demo-" in room_name_str or "demo_" in room_name_str)
    )
    if is_demo_call:
        is_outbound = True
        phone_number = None   # do not attempt SIP dial for web demo
        logger.info(f"[DEMO] Web demo call detected ({room_name_str or 'unknown'}) — forcing OUTBOUND flow")

    # ── INBOUND SAFEGUARD: rooms created by the SIP dispatch rule always have "inbound_" prefix ──
    # This overrides any false-positive outbound detection (e.g. if LiveKit injects caller
    # phone in job metadata, which looks like a phone number to the parser above).
    if room_name_str.startswith("inbound_"):
        if is_outbound:
            logger.warning(
                f"[INBOUND] Room '{room_name_str}' starts with 'inbound_' but metadata suggested "
                f"outbound (phone={phone_number}). Overriding to INBOUND — will NOT dial out."
            )
            phone_number = None   # discard any spurious phone extracted from metadata
        is_outbound = False
        logger.info(f"[INBOUND] Room prefix confirms inbound call: {room_name_str}")

    # Extract from SIP participants
    for identity, participant in ctx.room.remote_participants.items():
        # Name from caller ID (#32)
        if participant.name and participant.name not in ("", "Caller", "Unknown"):
            caller_name = participant.name
            logger.info(f"[CALLER-ID] Name from SIP: {caller_name}")
        if not phone_number:
            attr = participant.attributes or {}
            phone_number = attr.get("sip.phoneNumber") or attr.get("phoneNumber")
        if not phone_number and "+" in identity:
            import re as _re
            m = _re.search(r"\+\d{7,15}", identity)
            if m:
                phone_number = m.group()

    caller_phone = phone_number or "unknown"

    # ── Load config ───────────────────────────────────────────────────────
    live_config   = get_live_config(caller_phone)

    # ── Outbound SIP / VoiceLink Dialing ──────────────────────────────────
    if is_outbound and phone_number:
        norm = normalize_indian_number(phone_number)
        national_10 = norm["national_10"]
        full_e164   = norm["full_e164"]

        voicelink_mode = (
            live_config.get("voicelink_mode")
            or os.getenv("VOICELINK_MODE", "sip").lower()
        )

        # Mode 1: VoiceLink REST API Add Lead (/v1/add_lead)
        if voicelink_mode == "api":
            raw_did = (
                live_config.get("voicelink_outbound_number")
                or os.getenv("VOICELINK_OUTBOUND_NUMBER")
                or os.getenv("VOICELINK_DID_NUMBER")
                or "919429391395"
            )
            logger.info(f"[OUTBOUND-API] Originating VoiceLink call to {national_10} via DID {raw_did}...")
            try:
                client = VoiceLinkClient()
                res = client.originate_call(
                    did_number=raw_did,
                    phone_number=national_10,
                    custom_parameters=json.dumps({"room": ctx.room.name}),
                )
                logger.info(f"[OUTBOUND-API] VoiceLink call originated successfully: {res}")
            except Exception as e:
                logger.error(f"[OUTBOUND-API] Failed to originate VoiceLink API call: {e}")

        # Mode 2: LiveKit SIP Trunking (Default)
        else:
            trunk_id = (
                live_config.get("sip_trunk_id")
                or live_config.get("outbound_trunk_id")
                or os.getenv("SIP_TRUNK_ID")
                or os.getenv("OUTBOUND_TRUNK_ID")
                or os.getenv("VOICELINK_SIP_TRUNK_ID")
                or "ST_UFXhWiBxXpbg"
            )
            if trunk_id:
                tech_prefix = (
                    live_config.get("voicelink_tech_prefix")
                    or os.getenv("VOICELINK_TECH_PREFIX")
                    or "45454"
                )
                include_cc = os.getenv("VOICELINK_INCLUDE_COUNTRY_PREFIX", "false").lower() == "true"
                
                # VoiceLink carrier expects 10-digit national number after tech prefix (e.g. 454549766573966)
                if tech_prefix:
                    num_for_dial = f"91{national_10}" if include_cc else national_10
                    dial_target = tech_prefix + num_for_dial if not num_for_dial.startswith(tech_prefix) else num_for_dial
                else:
                    dial_target = full_e164

                # Outbound caller DID
                raw_caller = (
                    live_config.get("voicelink_outbound_number")
                    or os.getenv("VOICELINK_OUTBOUND_NUMBER")
                    or "919429391395"
                )
                caller_norm = normalize_indian_number(raw_caller)
                caller_id = f"91{caller_norm['national_10']}"

                logger.info(f"[OUTBOUND-SIP] Dialing target {dial_target} (national: {national_10}) from DID {caller_id} via SIP Trunk ({trunk_id})...")
                try:
                    from livekit.api import CreateSIPParticipantRequest as _SipReq
                    sip_domain = os.getenv("VOICELINK_SIP_DOMAIN", "160.30.71.89:3300")
                    sip_headers = {
                        "P-Asserted-Identity": f"<sip:{caller_id}@{sip_domain}>",
                        "Remote-Party-ID": f"<sip:{caller_id}@{sip_domain}>;party=calling;privacy=off",
                    }
                    sip_req = _SipReq(
                        sip_trunk_id=trunk_id,
                        sip_call_to=dial_target,   # Target recipient number with Tech Prefix
                        sip_number=caller_id,       # Our outbound DID caller ID
                        room_name=ctx.room.name,
                        participant_identity=f"sip_{dial_target}",
                        participant_name="Recipient",
                        headers=sip_headers,
                    )
                    await ctx.api.sip.create_sip_participant(sip_req)
                    logger.info(f"[OUTBOUND-SIP] SIP call successfully initiated from DID {caller_id} to {dial_target}")
                except Exception as e:
                    logger.error(f"[OUTBOUND-SIP] Failed to create SIP participant for {dial_target}: {e}")
            else:
                logger.error("[OUTBOUND] Cannot dial: sip_trunk_id missing in DB and env")



    # ── Rate limiting (#37) ───────────────────────────────────────────────
    if is_rate_limited(caller_phone):
        logger.warning(f"[RATE-LIMIT] Blocked {caller_phone} — too many calls in 1h")
        return

    delay_setting = live_config.get("stt_min_endpointing_delay", 0.35)
    
    # Provider detection: explicit in live_config > GROQ_API_KEY present in env/config > default openai
    _openai_key = live_config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    _groq_key = live_config.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
    _gemini_key = live_config.get("gemini_api_key") or live_config.get("google_api_key") or os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")

    llm_provider = live_config.get("llm_provider") or os.environ.get("LLM_PROVIDER")
    if llm_provider:
        llm_provider = llm_provider.strip().lower()
    
    if not llm_provider:
        if _groq_key:
            llm_provider = "groq"
        elif _gemini_key:
            llm_provider = "google"
        elif _openai_key:
            llm_provider = "openai"

    llm_model = live_config.get("llm_model") or os.environ.get("LLM_MODEL")
    if llm_provider == "groq" and (not llm_model or llm_model.startswith("gpt-") or llm_model.startswith("claude") or llm_model.startswith("gemini")):
        llm_model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    tts_voice     = live_config.get("tts_voice", "kavya")
    tts_language  = live_config.get("tts_language", "hi-IN")
    tts_provider  = live_config.get("tts_provider", "sarvam")
    stt_provider  = live_config.get("stt_provider", "sarvam")
    stt_language  = live_config.get("stt_language")
    if not stt_language or stt_language == "unknown":
        stt_language = "hi-IN"
    max_turns     = live_config.get("max_turns", 25)

    # Override OS env vars from UI config
    for key in ["LIVEKIT_URL","LIVEKIT_API_KEY","LIVEKIT_API_SECRET","GROQ_API_KEY",
                "OPENAI_API_KEY","ANTHROPIC_API_KEY","LLM_PROVIDER","LLM_MODEL",
                "SARVAM_API_KEY","CAL_API_KEY","TELEGRAM_BOT_TOKEN","SUPABASE_URL","SUPABASE_KEY"]:
        val = live_config.get(key.lower(), "")
        if val:
            os.environ[key] = val

    # ── Caller memory (#15) ───────────────────────────────────────────────
    async def get_caller_history(phone: str) -> str:
        if phone == "unknown":
            return ""
        try:
            sb = db.get_supabase()
            if not sb:
                return ""
            result = (sb.table("call_logs")
                        .select("summary, created_at")
                        .eq("phone_number", phone)
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute())
            if result.data:
                last: dict = result.data[0]  # type: ignore[assignment]
                return f"\n\n[CALLER HISTORY: Last call {last['created_at'][:10]}. Summary: {last['summary']}]"
        except Exception as e:
            logger.warning(f"[MEMORY] Could not load history: {e}")
        return ""

    caller_history = await get_caller_history(caller_phone)
    if caller_history:
        logger.info(f"[MEMORY] Loaded caller history for {caller_phone}")
        # Append to live_config instructions
        live_config["agent_instructions"] = (live_config.get("agent_instructions","") + caller_history)

    # ── Instantiate tools ─────────────────────────────────────────────────
    agent_tools = AgentTools(caller_phone=caller_phone, caller_name=caller_name)
    agent_tools._sip_identity = (
        f"sip_{caller_phone.replace('+','')}" if phone_number else "inbound_caller"
    )
    agent_tools.ctx_api   = ctx.api
    agent_tools.room_name = ctx.room.name

    # ── Build LLM (#8 Groq / Gemini / Claude support) ──────────────────────
    logger.info(f"[DEBUG] llm_provider is: {llm_provider!r}")
    if llm_provider in ("google", "gemini"):
        _gemini_key = (
            live_config.get("gemini_api_key")
            or live_config.get("google_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY", "")
        )
        agent_llm = openai.LLM(
            model=llm_model or "gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=_gemini_key,
            max_completion_tokens=120,
        )
        logger.info(f"[LLM] Using Gemini/Google: {llm_model or 'gemini-2.5-flash'}")
    elif llm_provider == "groq":
        _groq_key = live_config.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
        agent_llm = openai.LLM(
            model=llm_model or "llama-3.1-8b-instant",
            base_url="https://api.groq.com/openai/v1",
            api_key=_groq_key,
            max_completion_tokens=120,
        )
        logger.info(f"[LLM] Using Groq: {llm_model or 'llama-3.1-8b-instant'}")
    elif llm_provider == "claude":
        # Claude Haiku 3.5 via Anthropic API (#27)
        _anthropic_key = live_config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        agent_llm = openai.LLM(
            model=llm_model or "claude-haiku-3-5-latest",
            base_url="https://api.anthropic.com/v1/",
            api_key=_anthropic_key,
            max_completion_tokens=120,
        )
        logger.info(f"[LLM] Using Claude via Anthropic: {llm_model or 'claude-haiku-3-5-latest'}")
    else:
        _openai_key = live_config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        agent_llm = openai.LLM(model=llm_model or "gpt-4o-mini", api_key=_openai_key, max_completion_tokens=120)
        logger.info(f"[LLM] Using OpenAI: {llm_model or 'gpt-4o-mini'}")

    # ── Build STT (#1 16kHz, #20 auto-detect, #9 Deepgram) ──────────────
    if stt_provider == "deepgram":
        try:
            from livekit.plugins import deepgram
            agent_stt = deepgram.STT(
                model="nova-2-general",
                language="multi",        # multilingual mode
                interim_results=False,
            )
            logger.info("[STT] Using Deepgram Nova-2")
        except ImportError:
            logger.warning("[STT] deepgram plugin not installed — falling back to Sarvam")
            agent_stt = sarvam.STT(
                language=stt_language,
                model="saaras:v3",
                mode="transcribe",
                flush_signal=True,
                sample_rate=16000,
            )
    else:
        agent_stt = sarvam.STT(
            language=stt_language,      # "hi-IN" default
            model="saaras:v3",
            mode="transcribe",
            flush_signal=True,
            sample_rate=16000,          # force 16kHz (#1)
        )
        logger.info(f"[STT] Using Sarvam Saaras v3 — language: {stt_language}")

    # ── Build TTS (#2 24kHz, #10 ElevenLabs) ────────────────────────────
    if tts_provider == "elevenlabs":
        try:
            from livekit.plugins import elevenlabs
            _el_voice_id = live_config.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
            agent_tts = elevenlabs.TTS(
                model="eleven_turbo_v2_5",
                voice_id=_el_voice_id,
            )
            logger.info(f"[TTS] Using ElevenLabs Turbo v2.5 — voice: {_el_voice_id}")
        except ImportError:
            logger.warning("[TTS] elevenlabs plugin not installed — falling back to Sarvam")
            agent_tts = sarvam.TTS(
                target_language_code=tts_language,
                model="bulbul:v3",
                speaker=tts_voice,
                speech_sample_rate=24000,
            )
    elif tts_provider == "deepgram":
        try:
            from livekit.plugins import deepgram
            agent_tts = deepgram.TTS(model="aura-stella-en")
            logger.info("[TTS] Using Deepgram Aura TTS (aura-stella-en)")
        except Exception as e:
            logger.warning(f"[TTS] Deepgram TTS init failed ({e}) — falling back to Sarvam")
            agent_tts = sarvam.TTS(
                target_language_code=tts_language,
                model="bulbul:v3",
                speaker=tts_voice,
                speech_sample_rate=24000,
            )
    else:
        try:
            agent_tts = sarvam.TTS(
                target_language_code=tts_language,
                model="bulbul:v3",
                speaker=tts_voice,
                speech_sample_rate=24000,          # force 24kHz (#2)
            )
            logger.info(f"[TTS] Using Sarvam Bulbul v3 — voice: {tts_voice} lang: {tts_language}")
        except Exception as e:
            logger.warning(f"[TTS] Sarvam TTS init failed ({e}) — falling back to Deepgram")
            from livekit.plugins import deepgram
            agent_tts = deepgram.TTS(model="aura-stella-en")

    # ── Sentence chunker (keep responses short for voice) ─────────────────
    def before_tts_cb(agent_response: str) -> str:
        sentences = re.split(r'(?<=[।.!?])\s+', agent_response.strip())
        return sentences[0] if sentences else agent_response

    # ── Turn counter + auto-close (#29) ──────────────────────────────────
    turn_count    = 0
    interrupt_count = 0  # (#30)

    # ── Build agent ───────────────────────────────────────────────────────
    agent = OutboundAssistant(
        agent_tools=agent_tools,
        first_line=live_config.get("first_line", ""),
        live_config=live_config,
        is_outbound=is_outbound,
    )

    # ── Build session (#3 noise cancellation attempted) ───────────────────
    _noise_cancel = None
    if os.getenv("ENABLE_NOISE_CANCELLATION", "false").lower() == "true":
        try:
            from livekit.agents import noise_cancellation as nc
            _noise_cancel = nc.BVC()
            logger.info("[AUDIO] BVC noise cancellation enabled")
        except Exception as e:
            logger.info(f"[AUDIO] Noise cancellation unavailable: {e}")

    room_input = RoomInputOptions(close_on_disconnect=False)
    if _noise_cancel:
        try:
            room_input = RoomInputOptions(close_on_disconnect=False, noise_cancellation=_noise_cancel)
        except Exception:
            room_input = RoomInputOptions(close_on_disconnect=False)

    session = AgentSession(
        stt=agent_stt,
        llm=agent_llm,
        tts=agent_tts,
        turn_detection="stt",
        min_endpointing_delay=float(delay_setting),  # 0.05 default (#6)
        allow_interruptions=True,
    )

    await session.start(room=ctx.room, agent=agent, room_input_options=room_input)

    # ── Speak Initial Greeting (Simple opener for all calls) ────────────
    persona_info = get_persona_for_voice(tts_voice)
    p_name = persona_info["name"]
    p_verb = persona_info["verb"]

    # Simple opener for all calls — the LLM introduces itself on the NEXT turn after response.
    greeting_text = (live_config.get("first_line") or "Hello").strip()

    async def wait_for_participant_audio():
        # Check if any remote participant already exists or has an audio track
        for p in ctx.room.remote_participants.values():
            for pub in p.track_publications.values():
                kind = getattr(pub, "kind", None)
                is_audio = (
                    kind == 1
                    or (hasattr(kind, "value") and getattr(kind, "value", None) == 1)
                    or "audio" in str(kind).lower()
                )
                if getattr(pub, "subscribed", False) or is_audio:
                    logger.info(f"[CALL] Audio track already present for {p.identity} (kind={kind}) — proceeding immediately")
                    return p

        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def on_track_subscribed(track, publication, participant):
            if not fut.done():
                logger.info(f"[CALL] Audio track subscribed from {participant.identity} — audio ready!")
                fut.set_result(participant)

        ctx.room.on("track_subscribed", on_track_subscribed)
        try:
            p = await asyncio.wait_for(fut, timeout=2.5)
            return p
        except asyncio.TimeoutError:
            logger.info("[CALL] Ready to speak greeting — proceeding without delay")
            return None

    if is_outbound:
        logger.info("[OUTBOUND] Waiting for recipient to ANSWER the phone call...")
        await wait_for_participant_audio()
        await asyncio.sleep(0.2)
    else:
        logger.info("[INBOUND/DEMO] Waiting for caller audio track to connect...")
        await wait_for_participant_audio()
        await asyncio.sleep(0.2)

    logger.info(f"[CALL] Speaking initial greeting instantly: {greeting_text}")
    try:
        await session.say(greeting_text, allow_interruptions=True)
        session.history.add_message(role="assistant", content=greeting_text)
    except Exception as e:
        logger.warning(f"[CALL] session.say failed ({e}), generating reply")
        await session.generate_reply(instructions=f"Say exactly this phrase: '{greeting_text}'")

    # ── TTS pre-warm (#12) ────────────────────────────────────────────────
    try:
        if session.tts is not None and hasattr(session.tts, "prewarm"):
            res = session.tts.prewarm()
            if asyncio.iscoroutine(res):
                await res
            logger.info("[TTS] Pre-warmed successfully")
    except Exception as e:
        logger.debug(f"[TTS] Pre-warm skipped: {e}")

    logger.info("[AGENT] Session live — waiting for caller audio.")
    call_start_time = datetime.now()

    # ── Recording → Supabase Storage ─────────────────────────────────────
    egress_id = None
    try:
        rec_api = api.LiveKitAPI(
            url=os.environ["LIVEKIT_URL"],
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
        )
        egress_resp = await rec_api.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(
                room_name=ctx.room.name,
                audio_only=True,
                file_outputs=[api.EncodedFileOutput(
                    file_type=api.EncodedFileType.OGG,
                    filepath=f"recordings/{ctx.room.name}.ogg",
                    s3=api.S3Upload(
                        access_key=os.environ["SUPABASE_S3_ACCESS_KEY"],
                        secret=os.environ["SUPABASE_S3_SECRET_KEY"],
                        bucket="call-recordings",
                        region=os.environ.get("SUPABASE_S3_REGION", "ap-south-1"),
                        endpoint=os.environ["SUPABASE_S3_ENDPOINT"],
                        force_path_style=True,
                    )
                )]
            )
        )
        egress_id = egress_resp.egress_id
        await rec_api.aclose()
        logger.info(f"[RECORDING] Started egress: {egress_id}")
    except Exception as e:
        logger.warning(f"[RECORDING] Failed to start recording: {e}")

    # ── Upsert active_calls (#38) ─────────────────────────────────────────
    async def upsert_active_call(status: str):
        try:
            sb = db.get_supabase()
            if sb:
                sb.table("active_calls").upsert({
                    "room_id":     ctx.room.name,
                    "phone":       caller_phone,
                    "caller_name": caller_name,
                    "status":      status,
                    "last_updated": datetime.now(pytz.utc).isoformat(),
                }).execute()
        except Exception as e:
            logger.debug(f"[ACTIVE-CALL] {e}")

    await upsert_active_call("active")

    # ── Real-time transcript streaming (#33) ─────────────────────────────
    async def _log_transcript(role: str, content: str):
        try:
            sb = db.get_supabase()
            if sb:
                sb.table("call_transcripts").insert({
                    "call_room_id": ctx.room.name,
                    "phone":        caller_phone,
                    "role":         role,
                    "content":      content,
                }).execute()
        except Exception as e:
            logger.debug(f"[TRANSCRIPT-STREAM] {e}")

    # ── Session event handlers ────────────────────────────────────────────
    @session.on("agent_state_changed")
    def _agent_state_changed(ev):
        global agent_is_speaking, _last_speech_end_time
        # AgentState values: 'listening', 'thinking', 'speaking'
        state = getattr(ev, "state", None) or getattr(ev, "new_state", None)
        if state is not None:
            was_speaking = agent_is_speaking
            agent_is_speaking = str(state) in ("speaking", "AgentState.SPEAKING")
            # When agent STOPS speaking, record the timestamp so we can suppress
            # PSTN/SIP echo that arrives 300-800ms after TTS finishes.
            if was_speaking and not agent_is_speaking:
                _last_speech_end_time = time.time()
                logger.debug(f"[ECHO-GUARD] Agent stopped speaking — suppressing STT input for {_ECHO_SUPPRESS_S}s")

    # Interrupt logging (#30) — use agent_false_interruption as closest proxy
    @session.on("agent_false_interruption")
    def _on_interrupted(ev):
        nonlocal interrupt_count
        interrupt_count += 1
        logger.info(f"[INTERRUPT] Agent interrupted. Total: {interrupt_count}")

    FILLER_WORDS = {
        "okay.", "okay", "ok", "uh", "hmm", "hm", "yeah", "yes",
        "no", "um", "ah", "oh", "right", "sure", "fine", "good",
        "haan", "han", "theek", "theek hai", "accha", "ji", "ha",
    }

    @session.on("user_input_transcribed")
    def on_user_speech_committed(ev):
        nonlocal turn_count
        global agent_is_speaking, _last_speech_end_time

        # Support both old and new event field names
        transcript = (
            getattr(ev, "user_transcript", None)
            or getattr(ev, "transcript", None)
            or ""
        ).strip()
        transcript_lower = transcript.lower().rstrip(".")

        # Echo suppression: drop input if agent is currently speaking OR within
        # the post-speech suppression window (catches PSTN echo arriving after TTS).
        time_since_speech = time.time() - _last_speech_end_time
        if agent_is_speaking or time_since_speech < _ECHO_SUPPRESS_S:
            logger.debug(
                f"[FILTER-ECHO] Dropped (speaking={agent_is_speaking}, "
                f"{time_since_speech:.2f}s since speech): '{transcript}'"
            )
            return
        if not transcript or len(transcript) < 3:
            return
        if transcript_lower in FILLER_WORDS:
            logger.debug(f"[FILTER-FILLER] Dropped: '{transcript}'")
            return

        # Real-time transcript stream
        asyncio.create_task(_log_transcript("user", transcript))

        # Turn counter + auto-close (#29)
        turn_count += 1
        logger.info(f"[TRANSCRIPT] Turn {turn_count}/{max_turns}: '{transcript}'")
        if turn_count >= max_turns:
            logger.info(f"[LIMIT] Reached {max_turns} turns — wrapping up")
            async def _wrap_up():
                await session.generate_reply(
                    instructions="Politely wrap up: thank the caller, say they can call back anytime, and say a warm goodbye."
                )
            asyncio.create_task(_wrap_up())

    @ctx.room.on("participant_connected")
    def on_participant_connected(participant):
        identity = str(participant.identity or "")
        logger.info(f"[ROOM] Participant connected: {identity}")
        # For inbound calls, the SIP participant joins AFTER the agent connects.
        # Auto-update the SIP identity so transfer/end_call tools can address them.
        if agent_tools._sip_identity in ("inbound_caller", "") and (
            "sip_" in identity.lower() or "+" in identity or identity.startswith("sip")
        ):
            agent_tools._sip_identity = identity
            logger.info(f"[INBOUND] SIP identity auto-set from connecting participant: {identity}")

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        global agent_is_speaking
        logger.info(f"[HANGUP] Participant disconnected: {participant.identity}")
        agent_is_speaking = False
        asyncio.create_task(unified_shutdown_hook(ctx))

    # ══════════════════════════════════════════════════════════════════════
    # POST-CALL SHUTDOWN HOOK
    # ══════════════════════════════════════════════════════════════════════

    async def unified_shutdown_hook(shutdown_ctx: JobContext):
        logger.info("[SHUTDOWN] Sequence started.")

        duration = int((datetime.now() - call_start_time).total_seconds())

        # Booking
        booking_status_msg = "No booking"
        if agent_tools.booking_intent:
            from calendar_tools import async_create_booking
            intent = agent_tools.booking_intent
            result = await async_create_booking(
                start_time=intent["start_time"],
                caller_name=intent["caller_name"] or "Unknown Caller",
                caller_phone=intent["caller_phone"],
                notes=intent["notes"],
            )
            if result.get("success"):
                notify_booking_confirmed(
                    caller_name=intent["caller_name"],
                    caller_phone=intent["caller_phone"],
                    booking_time_iso=intent["start_time"],
                    booking_id=result.get("booking_id") or "",
                    notes=intent["notes"],
                    tts_voice=tts_voice,
                    ai_summary="",
                )
                booking_status_msg = f"Booking Confirmed: {result.get('booking_id')}"
            else:
                booking_status_msg = f"Booking Failed: {result.get('message')}"
        else:
            notify_call_no_booking(
                caller_name=agent_tools.caller_name,
                caller_phone=agent_tools.caller_phone,
                call_summary="Caller did not schedule during this call.",
                tts_voice=tts_voice,
                duration_seconds=duration,
            )

        # Build transcript
        transcript_text = ""
        try:
            messages = agent.chat_ctx.messages
            if callable(messages):
                messages = messages()
            lines = []
            for msg in messages:
                if getattr(msg, "role", None) in ("user", "assistant"):
                    content = getattr(msg, "content", "")
                    if isinstance(content, list):
                        content = " ".join(str(c) for c in content if isinstance(c, str))  # type: ignore[assignment]
                    lines.append(f"[{msg.role.upper()}] {content}")
            transcript_text = "\n".join(lines)
        except Exception as e:
            logger.error(f"[SHUTDOWN] Transcript read failed: {e}")
            transcript_text = "unavailable"

        # Sentiment analysis (#14)
        sentiment = "unknown"
        if transcript_text and transcript_text != "unavailable":
            try:
                import openai as _oai
                _groq_key = os.environ.get("GROQ_API_KEY", "")
                _oai_key  = os.environ.get("OPENAI_API_KEY", "")
                if _groq_key:
                    _client = _oai.AsyncOpenAI(api_key=_groq_key, base_url="https://api.groq.com/openai/v1")
                    _sent_model = "llama-3.3-70b-versatile"
                else:
                    _client = _oai.AsyncOpenAI(api_key=_oai_key)
                    _sent_model = "gpt-4o-mini"
                resp = await _client.chat.completions.create(
                    model=_sent_model, max_tokens=10,
                    messages=[{"role":"user","content":
                        f"Classify this call as one word: positive, neutral, negative, or frustrated.\n\n{transcript_text[:800]}"}]
                )
                _raw = resp.choices[0].message.content or ""
                sentiment = _raw.strip().lower()
                logger.info(f"[SENTIMENT] {sentiment}")
            except Exception as e:
                logger.warning(f"[SENTIMENT] Failed: {e}")

        # Cost estimation (#34)
        def estimate_cost(dur: int, chars: int) -> float:
            return round(
                (dur / 60) * 0.002 +
                (dur / 60) * 0.006 +
                (chars / 1000) * 0.003 +
                (chars / 4000) * 0.0001,
                5
            )
        estimated_cost = estimate_cost(duration, len(transcript_text))
        logger.info(f"[COST] Estimated: ${estimated_cost}")

        # Analytics timestamps (#19)
        ist = pytz.timezone("Asia/Kolkata")
        call_dt = call_start_time.astimezone(ist)

        # Stop recording
        recording_url = ""
        if egress_id:
            try:
                stop_api = api.LiveKitAPI(
                    url=os.environ["LIVEKIT_URL"],
                    api_key=os.environ["LIVEKIT_API_KEY"],
                    api_secret=os.environ["LIVEKIT_API_SECRET"],
                )
                await stop_api.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
                await stop_api.aclose()
                recording_url = (
                    f"{os.environ.get('SUPABASE_URL','')}/storage/v1/object/public/"
                    f"call-recordings/recordings/{ctx.room.name}.ogg"
                )
                logger.info(f"[RECORDING] Stopped. URL: {recording_url}")
            except Exception as e:
                logger.warning(f"[RECORDING] Stop failed: {e}")

        # Update active_calls to completed (#38)
        await upsert_active_call("completed")

        # n8n webhook (#39)
        _n8n_url = os.getenv("N8N_WEBHOOK_URL")
        if _n8n_url:
            try:
                import httpx
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: httpx.post(_n8n_url, json={
                        "event":        "call_completed",
                        "phone":        caller_phone,
                        "caller_name":  agent_tools.caller_name,
                        "duration":     duration,
                        "booked":       bool(agent_tools.booking_intent),
                        "sentiment":    sentiment,
                        "summary":      booking_status_msg,
                        "recording_url":recording_url,
                        "interrupt_count": interrupt_count,
                    }, timeout=5.0)
                )
                logger.info("[N8N] Webhook triggered")
            except Exception as e:
                logger.warning(f"[N8N] Webhook failed: {e}")

        # Save to Supabase
        from db import save_call_log
        save_call_log(
            phone=caller_phone,
            duration=duration,
            transcript=transcript_text,
            summary=booking_status_msg,
            recording_url=recording_url,
            caller_name=agent_tools.caller_name or "",
            sentiment=sentiment,
            estimated_cost_usd=estimated_cost,
            call_date=call_dt.date().isoformat(),
            call_hour=call_dt.hour,
            call_day_of_week=call_dt.strftime("%A"),
            was_booked=bool(agent_tools.booking_intent),
            interrupt_count=interrupt_count,
        )

    async def _shutdown_no_arg() -> None:
        await unified_shutdown_hook(ctx)

    ctx.add_shutdown_callback(_shutdown_no_arg)


# ══════════════════════════════════════════════════════════════════════════════
# WORKER ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        initialize_process_timeout=30.0,
        port=0,
    ))
