import json
import logging
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from dotenv import load_dotenv
from contextlib import asynccontextmanager

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ui-server")

_agent_process = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        ensure_agent_worker_running()
    except Exception as e:
        logger.warning(f"[LIFESPAN] Startup worker check: {e}")
    yield
    # Shutdown
    global _agent_process
    if _agent_process:
        try:
            _agent_process.terminate()
            logger.info("[WORKER] Background LiveKit agent worker stopped.")
        except Exception:
            pass

app = FastAPI(title="RapidX AI Dashboard", lifespan=lifespan)

CONFIG_FILE = "config.json"

# Keys that come from environment only (used to connect to DB — can't store in DB)
_ENV_ONLY_KEYS = ("supabase_url", "supabase_key")


def _read_local_config() -> dict:
    """Read raw config.json (no env fallback, no DB)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def read_config() -> dict:
    """
    Merged config priority (highest → lowest):
      1. Supabase DB  (agent_config table)
      2. config.json  (local file)
      3. .env / Render environment variables
    Supabase credentials themselves always come from env (chicken-and-egg).
    """
    # Layer 3: env defaults
    def env_val(env_key, default=""):
        return os.getenv(env_key, default)

    base: dict = {
        "first_line": env_val("FIRST_LINE", "Hello ji... Main Rahul bol raha hoon, Kona Kona Interiors se. Ritesh Sir ne aapka number diya tha. Kya aapke paas 2 minute hain baat karne ke liye?"),
        "agent_instructions": env_val("AGENT_INSTRUCTIONS", ""),
        "llm_provider": env_val("LLM_PROVIDER", "groq" if os.getenv("GROQ_API_KEY") else "openai"),
        "llm_model": env_val("LLM_MODEL", "llama-3.3-70b-versatile" if os.getenv("GROQ_API_KEY") else "gpt-4o-mini"),
        "tts_voice": env_val("TTS_VOICE", "kavya"),
        "tts_language": env_val("TTS_LANGUAGE", "hi-IN"),
        "stt_min_endpointing_delay": float(env_val("STT_MIN_ENDPOINTING_DELAY", 0.35)),
        "lang_preset": env_val("LANG_PRESET", "hinglish"),
        "livekit_url": env_val("LIVEKIT_URL", ""),
        "sip_trunk_id": env_val("SIP_TRUNK_ID", env_val("OUTBOUND_TRUNK_ID", "")),
        "livekit_api_key": env_val("LIVEKIT_API_KEY", ""),
        "livekit_api_secret": env_val("LIVEKIT_API_SECRET", ""),
        "groq_api_key": env_val("GROQ_API_KEY", ""),
        "openai_api_key": env_val("OPENAI_API_KEY", ""),
        "anthropic_api_key": env_val("ANTHROPIC_API_KEY", ""),
        "sarvam_api_key": env_val("SARVAM_API_KEY", ""),
        "deepgram_api_key": env_val("DEEPGRAM_API_KEY", ""),
        "cal_api_key": env_val("CAL_API_KEY", ""),
        "cal_event_type_id": env_val("CAL_EVENT_TYPE_ID", ""),
        "telegram_bot_token": env_val("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": env_val("TELEGRAM_CHAT_ID", ""),
        "vobiz_sip_domain": env_val("VOBIZ_SIP_DOMAIN", env_val("VOICELINK_SIP_DOMAIN", "160.30.71.89:3300")),
        "vobiz_username": env_val("VOBIZ_USERNAME", "pvan2709"),
        "vobiz_password": env_val("VOBIZ_PASSWORD", "Pui27@.1234.x"),
        "vobiz_outbound_number": env_val("VOBIZ_OUTBOUND_NUMBER", "+919429391395"),
        "supabase_url": env_val("SUPABASE_URL", ""),
        "supabase_key": env_val("SUPABASE_KEY", ""),
    }

    # Layer 2: local config.json (override env defaults)
    local = _read_local_config()
    for k, v in local.items():
        if v not in ("", None):
            base[k] = v
    # Always keep supabase creds from env or local config.json
    base["supabase_url"] = env_val("SUPABASE_URL", "") or local.get("supabase_url", "")
    base["supabase_key"] = env_val("SUPABASE_KEY", "") or local.get("supabase_key", "")

    # Layer 1: Supabase DB (highest priority — set via UI, survives redeployments)
    ensure_supabase_env_from(base)
    # Merge DB config if available and valid
    try:
        import db
        db_cfg = db.load_config_from_db()
        if db_cfg and isinstance(db_cfg, dict):
            for k, v in db_cfg.items():
                if k not in _ENV_ONLY_KEYS and v not in ("", None):
                    base[k] = v
    except Exception as e:
        logger.debug(f"DB config load skipped: {e}")

    # Fallback guard: Ensure first_line & agent_instructions are never empty or missing
    default_first_line = "Hello ji... Main Rahul bol raha hoon, Kona Kona Interiors se. Ritesh Sir ne aapka number diya tha. Kya aapke paas 2 minute hain baat karne ke liye?"
    if not base.get("first_line") or not str(base.get("first_line", "")).strip():
        base["first_line"] = default_first_line

    if not base.get("agent_instructions") or len(str(base.get("agent_instructions", "")).strip()) < 20:
        local_bak = _read_local_config()
        base["agent_instructions"] = local_bak.get("agent_instructions") or ""

    # Ensure provider & model consistency (e.g. if provider is Groq, model shouldn't be gpt-*)
    provider = base.get("llm_provider", "groq" if os.getenv("GROQ_API_KEY") else "openai")
    model = base.get("llm_model", "")
    if provider == "groq" and (not model or model.startswith("gpt-") or model.startswith("claude-")):
        base["llm_model"] = "llama-3.3-70b-versatile"
    elif provider == "openai" and (not model or not model.startswith("gpt-")):
        base["llm_model"] = "gpt-4o-mini"
    elif provider == "claude" and (not model or not model.startswith("claude-")):
        base["llm_model"] = "claude-haiku-3-5-latest"

    return base


def write_config(data: dict):
    """
    Save config to both Supabase DB (primary) and config.json (local backup).
    """
    current = _read_local_config()
    current.update(data)
    # Remove connection-only keys so they never leak into local config or database payload
    current.pop("supabase_url", None)
    current.pop("supabase_key", None)

    # 1. Write local backup
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(current, f, indent=4)
    except Exception as e:
        logger.warning(f"Could not write config.json: {e}")

    # 2. Write to Supabase DB
    ensure_supabase_env_from(current)
    try:
        import db
        db.save_config_to_db(current)
    except Exception as e:
        logger.warning(f"Could not save config to DB: {e}")


def ensure_supabase_env_from(cfg: dict):
    """Set environment variables from config dict if missing (excludes Supabase connection keys)."""
    for k, env_name in [
        ("livekit_url", "LIVEKIT_URL"),
        ("livekit_api_key", "LIVEKIT_API_KEY"),
        ("livekit_api_secret", "LIVEKIT_API_SECRET"),
        ("groq_api_key", "GROQ_API_KEY"),
        ("sarvam_api_key", "SARVAM_API_KEY"),
        ("deepgram_api_key", "DEEPGRAM_API_KEY"),
        ("vobiz_sip_domain", "VOBIZ_SIP_DOMAIN"),
        ("vobiz_username", "VOBIZ_USERNAME"),
        ("vobiz_password", "VOBIZ_PASSWORD"),
        ("vobiz_outbound_number", "VOBIZ_OUTBOUND_NUMBER"),
    ]:
        val = cfg.get(k)
        if val:
            os.environ[env_name] = str(val)

# ── API Endpoints ──────────────────────────────────────────────────────────────

def ensure_supabase_env():
    """Set SUPABASE env vars from config (DB → config.json → .env)."""
    cfg = read_config()
    ensure_supabase_env_from(cfg)

# ── API Endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/config")
async def api_get_config():
    return read_config()

@app.post("/api/config")
async def api_post_config(request: Request):
    data = await request.json()
    write_config(data)
    logger.info("Configuration updated via UI.")
    return {"status": "success"}

@app.get("/api/logs")
async def api_get_logs():
    ensure_supabase_env()
    import db
    try:
        logs = db.fetch_call_logs(limit=50)
        return logs
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return []

@app.get("/api/logs/{log_id}/transcript")
async def api_get_transcript(log_id: str):
    ensure_supabase_env()
    import db
    try:
        from supabase import create_client
        supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        res = supabase.table("call_logs").select("*").eq("id", log_id).single().execute()
        data = res.data or {}
        if isinstance(data, dict):
            text = f"Call Log — {data.get('created_at', '')}\n"
            text += f"Phone: {data.get('phone_number', 'Unknown')}\n"
            text += f"Duration: {data.get('duration_seconds', 0)}s\n"
            text += f"Summary: {data.get('summary', '')}\n\n"
            text += "--- TRANSCRIPT ---\n"
            text += str(data.get("transcript") or "No transcript available.")
        else:
            text = "No transcript data available."
        return PlainTextResponse(content=text, media_type="text/plain",
                                 headers={"Content-Disposition": f"attachment; filename=transcript_{log_id}.txt"})
    except Exception as e:
        return PlainTextResponse(content=f"Error: {e}", status_code=500)

@app.get("/api/bookings")
async def api_get_bookings():
    ensure_supabase_env()
    import db
    try:
        return db.fetch_bookings()
    except Exception as e:
        logger.error(f"Error fetching bookings: {e}")
        return []

@app.get("/api/stats")
async def api_get_stats():
    ensure_supabase_env()
    import db
    try:
        return db.fetch_stats()
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return {"total_calls": 0, "total_bookings": 0, "avg_duration": 0, "booking_rate": 0}

@app.get("/api/contacts")
async def api_get_contacts():
    """CRM endpoint — groups call_logs by phone number, deduplicates into contacts."""
    ensure_supabase_env()
    try:
        from supabase import create_client
        supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        res = supabase.table("call_logs") \
            .select("phone_number, caller_name, summary, created_at") \
            .order("created_at", desc=True) \
            .limit(500) \
            .execute()
        rows = res.data or []

        # Deduplicate by phone number
        contacts: dict = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            phone = str(r.get("phone_number") or "unknown")
            if phone not in contacts:
                contacts[phone] = {
                    "phone_number": phone,
                    "caller_name": str(r.get("caller_name") or ""),
                    "total_calls": 0,
                    "last_seen": r.get("created_at"),
                    "is_booked": False,
                }
            c = contacts[phone]
            c["total_calls"] += 1
            # Use the most recent non-empty name
            if not c["caller_name"] and r.get("caller_name"):
                c["caller_name"] = str(r["caller_name"])
            # Mark booked if any call had a confirmed booking
            summary_val = str(r.get("summary") or "")
            if summary_val and "Confirmed" in summary_val:
                c["is_booked"] = True

        return sorted(contacts.values(), key=lambda x: x["last_seen"] or "", reverse=True)
    except Exception as e:
        logger.error(f"Error fetching contacts: {e}")
        return []


# ── Knowledge Base API ─────────────────────────────────────────────────────────

@app.get("/api/knowledge")
async def api_get_knowledge():
    """List all knowledge base entries (active and inactive)."""
    ensure_supabase_env()
    import db
    try:
        return db.fetch_knowledge_base(active_only=False)
    except Exception as e:
        logger.error(f"Error fetching knowledge base: {e}")
        return []

@app.post("/api/knowledge")
async def api_add_knowledge(request: Request):
    """Add a new knowledge base entry."""
    ensure_supabase_env()
    import db
    try:
        data = await request.json()
        title      = str(data.get("title", "")).strip()
        content    = str(data.get("content", "")).strip()
        sort_order = int(data.get("sort_order", 0))
        if not content:
            return {"success": False, "message": "Content is required"}
        return db.add_knowledge_entry(title=title, content=content, sort_order=sort_order)
    except Exception as e:
        logger.error(f"Error adding knowledge base entry: {e}")
        return {"success": False, "message": str(e)}

@app.put("/api/knowledge/{entry_id}")
async def api_update_knowledge(entry_id: str, request: Request):
    """Update a knowledge base entry."""
    ensure_supabase_env()
    import db
    try:
        updates = await request.json()
        return db.update_knowledge_entry(entry_id=entry_id, updates=updates)
    except Exception as e:
        logger.error(f"Error updating knowledge base entry: {e}")
        return {"success": False, "message": str(e)}

@app.delete("/api/knowledge/{entry_id}")
async def api_delete_knowledge(entry_id: str):
    """Delete a knowledge base entry."""
    ensure_supabase_env()
    import db
    try:
        return db.delete_knowledge_entry(entry_id=entry_id)
    except Exception as e:
        logger.error(f"Error deleting knowledge base entry: {e}")
        return {"success": False, "message": str(e)}


DEMO_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Voice Demo — RapidX AI</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Inter',sans-serif;background:#0f1117;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;flex-direction:column;gap:24px;padding:24px}
    .card{background:#1c2333;border:1px solid #2a3448;border-radius:20px;padding:40px;max-width:440px;width:100%;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.4)}
    h1{font-size:22px;font-weight:700;margin-bottom:6px}
    .sub{color:#8892a4;font-size:13px;margin-bottom:28px}
    .avatar{width:80px;height:80px;border-radius:50%;background:linear-gradient(135deg,#6c63ff,#a855f7);display:flex;align-items:center;justify-content:center;font-size:36px;margin:0 auto 24px}
    .btn{width:100%;padding:14px;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;border:none;transition:all 0.2s}
    .btn-start{background:#6c63ff;color:#fff}
    .btn-start:hover{background:#5a52e0;box-shadow:0 0 24px rgba(108,99,255,0.4)}
    .btn-end{background:#ef4444;color:#fff;display:none}
    .btn-end:hover{background:#dc2626}
    #status{font-size:13px;color:#8892a4;margin-top:16px;min-height:20px}
    .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:6px;animation:pulse 1.5s infinite}
    @keyframes pulse{0%,100%{box-shadow:0 0 4px #22c55e}50%{box-shadow:0 0 12px #22c55e}}
    .vol-bar{display:flex;gap:3px;align-items:flex-end;justify-content:center;height:32px;margin-top:12px;display:none}
    .vol-bar span{width:4px;background:#6c63ff;border-radius:2px;transition:height 0.1s}
  </style>
</head>
<body>
  <div class="card">
    <div class="avatar">🎙</div>
    <h1>Talk to Aryan</h1>
    <div class="sub">AI-powered multilingual consultant · RapidX AI</div>
    <button class="btn btn-start" id="startBtn" onclick="startCall()">📞 Start Demo Call</button>
    <button class="btn btn-end" id="endBtn" onclick="endCall()">📵 End Call</button>
    <div id="status">Click to start a live voice demo</div>
    <div class="vol-bar" id="volBar">
      <span id="b1" style="height:8px"></span><span id="b2" style="height:14px"></span>
      <span id="b3" style="height:20px"></span><span id="b4" style="height:14px"></span>
      <span id="b5" style="height:8px"></span>
    </div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
  <script>
    let room;
    async function startCall() {
      document.getElementById('status').textContent = 'Connecting...';
      document.getElementById('startBtn').disabled = true;
      try {
        const res = await fetch('/api/demo-token').then(r => r.json());
        if (res.error) throw new Error(res.error);
        room = new LivekitClient.Room();
        await room.connect(res.url, res.token, {autoSubscribe: true});
        await room.localParticipant.setMicrophoneEnabled(true);
        document.getElementById('startBtn').style.display = 'none';
        document.getElementById('endBtn').style.display = 'block';
        document.getElementById('volBar').style.display = 'flex';
        setStatus('<span class="pulse"></span>Connected — speak now!');
        animateBars();
      } catch(e) {
        setStatus('❌ ' + e.message);
        document.getElementById('startBtn').disabled = false;
      }
    }
    async function endCall() {
      if (room) { await room.disconnect(); room = null; }
      document.getElementById('startBtn').style.display = 'block';
      document.getElementById('startBtn').disabled = false;
      document.getElementById('endBtn').style.display = 'none';
      document.getElementById('volBar').style.display = 'none';
      setStatus('Call ended. Click to start again.');
    }
    function setStatus(html) { document.getElementById('status').innerHTML = html; }
    function animateBars() {
      if (!room) return;
      ['b1','b2','b3','b4','b5'].forEach(id => {
        document.getElementById(id).style.height = (4 + Math.random()*24) + 'px';
      });
      setTimeout(animateBars, 150);
    }
  </script>
</body>
</html>"""


# ── Outbound Calls ────────────────────────────────────────────────────────────

@app.post("/api/call/single")
async def api_call_single(request: Request):
    """Dispatch a single outbound call via LiveKit."""
    ensure_agent_worker_running()
    data = await request.json()
    phone = (data.get("phone") or "").strip()
    if not phone.startswith("+"):
        return {"status": "error", "message": "Phone number must start with + and country code"}
    config = read_config()
    try:
        import random, json as _json
        from livekit import api as lkapi
        lk = lkapi.LiveKitAPI(
            url=config.get("livekit_url") or os.environ.get("LIVEKIT_URL",""),
            api_key=config.get("livekit_api_key") or os.environ.get("LIVEKIT_API_KEY",""),
            api_secret=config.get("livekit_api_secret") or os.environ.get("LIVEKIT_API_SECRET",""),
        )
        room_name = f"call-{phone.replace('+','')}-{random.randint(1000,9999)}"
        dispatch = await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=_json.dumps({"phone_number": phone}),
            )
        )
        await lk.aclose()
        logger.info(f"Outbound call dispatched to {phone}: {dispatch.id}")
        return {"status": "ok", "dispatch_id": dispatch.id, "room": room_name, "phone": phone}
    except Exception as e:
        logger.error(f"Call dispatch error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/call/bulk")
async def api_call_bulk(request: Request):
    """Dispatch outbound calls to multiple numbers (one per line)."""
    ensure_agent_worker_running()
    import random, json as _json
    from livekit import api as lkapi
    data = await request.json()
    numbers = [n.strip() for n in (data.get("numbers") or "").splitlines() if n.strip()]
    results = []
    cfg = read_config()
    lk_url    = cfg.get("livekit_url")    or os.environ.get("LIVEKIT_URL","")
    lk_key    = cfg.get("livekit_api_key")    or os.environ.get("LIVEKIT_API_KEY","")
    lk_secret = cfg.get("livekit_api_secret") or os.environ.get("LIVEKIT_API_SECRET","")
    for phone in numbers:
        if not phone.startswith("+"):
            results.append({"phone": phone, "status": "error", "message": "Must start with +"})
            continue
        try:
            lk = lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret)
            room_name = f"call-{phone.replace('+','')}-{random.randint(1000,9999)}"
            dispatch = await lk.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    agent_name="outbound-caller",
                    room=room_name,
                    metadata=_json.dumps({"phone_number": phone}),
                )
            )
            await lk.aclose()
            results.append({"phone": phone, "status": "ok", "dispatch_id": dispatch.id})
            logger.info(f"Bulk outbound dispatched to {phone}: {dispatch.id}")
        except Exception as e:
            results.append({"phone": phone, "status": "error", "message": str(e)})
    return {"results": results, "total": len(results)}

# ── Demo Link ─────────────────────────────────────────────────────────────────

@app.get("/api/demo-token")
async def api_demo_token():
    """Generate a LiveKit room + access token for browser-based demo call."""
    ensure_agent_worker_running()
    config = read_config()
    try:
        from livekit.api import AccessToken, VideoGrants
        import time, random
        room_name = f"demo-{random.randint(10000,99999)}"
        api_key    = config.get("livekit_api_key") or os.environ.get("LIVEKIT_API_KEY","")
        api_secret = config.get("livekit_api_secret") or os.environ.get("LIVEKIT_API_SECRET","")
        livekit_url = config.get("livekit_url") or os.environ.get("LIVEKIT_URL","")

        from datetime import timedelta
        token = AccessToken(api_key, api_secret) \
            .with_identity("demo-user") \
            .with_name("Demo Caller") \
            .with_grants(VideoGrants(room_join=True, room=room_name)) \
            .with_ttl(timedelta(seconds=3600)) \
            .to_jwt()

        # Also dispatch the agent into the room
        import json as _json
        from livekit import api as lkapi
        lk = lkapi.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)
        await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=_json.dumps({"phone_number": "demo", "is_demo": True}),
            )
        )
        await lk.aclose()
        return {"token": token, "room": room_name, "url": livekit_url}
    except Exception as e:
        logger.error(f"Demo token error: {e}")
        return {"error": str(e)}

@app.get("/demo", response_class=HTMLResponse)
async def get_demo_page():
    """Browser-based demo call page using LiveKit JS SDK."""
    return HTMLResponse(content=DEMO_PAGE_HTML)


# ── Prometheus Metrics (#40) ──────────────────────────────────────────────────
try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response as _Resp

    _voice_calls_total   = Counter("voice_calls_total",   "Total calls handled by the agent")
    _voice_calls_booked  = Counter("voice_calls_booked_total", "Calls that resulted in a booking")
    _voice_call_duration = Histogram("voice_call_duration_seconds", "Call duration in seconds",
                                      buckets=[10, 30, 60, 120, 300, 600, 1200])
    _voice_calls_active  = Gauge("voice_calls_active", "Currently active calls")

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        """Prometheus metrics scrape endpoint."""
        return _Resp(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/record-call", include_in_schema=False)
    async def record_call_metric(request: Request):
        """Called by agent.py at shutdown to update Prometheus counters."""
        data = await request.json()
        _voice_calls_total.inc()
        if data.get("booked"):
            _voice_calls_booked.inc()
        if data.get("duration"):
            _voice_call_duration.observe(data["duration"])
        return {"ok": True}

    logger.info("[METRICS] Prometheus metrics enabled at /metrics")

except ImportError:
    logger.warning("[METRICS] prometheus_client not installed — /metrics disabled")

import subprocess
import sys

_agent_process = None

def ensure_agent_worker_running():
    """Verify that an agent worker process is running before dispatching calls."""
    global _agent_process
    if _agent_process and _agent_process.poll() is None:
        return True

    # Check if any agent.py process is currently running on the system
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'cmdline']):
            cmd = proc.info.get('cmdline') or []
            if any("agent.py" in str(c) for c in cmd):
                return True
    except Exception:
        pass

    # If worker is not running, auto-start it!
    logger.info("[WORKER] Worker process not running — auto-starting background LiveKit agent worker...")
    config = read_config()
    lk_url = config.get("livekit_url") or os.getenv("LIVEKIT_URL")
    lk_key = config.get("livekit_api_key") or os.getenv("LIVEKIT_API_KEY")
    lk_secret = config.get("livekit_api_secret") or os.getenv("LIVEKIT_API_SECRET")

    if lk_url and lk_key and lk_secret:
        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            cmd = [sys.executable, "agent.py", "start"]
            _agent_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"[WORKER] Successfully auto-started background LiveKit agent worker (PID {_agent_process.pid})")
            import time
            time.sleep(1.5)  # Pause for worker registration with LiveKit Cloud
            return True
        except Exception as e:
            logger.error(f"[WORKER] Failed to auto-start agent worker: {e}")
            return False
    return False

@app.get("/api/worker/status")
def get_worker_status():
    """Return status of agent worker process."""
    is_running = ensure_agent_worker_running()
    return {"status": "online" if is_running else "offline", "running": is_running}

@app.get("/health")
def health_check():
    """Health check endpoint for Coolify monitoring (#22)."""
    return {
        "status": "ok",
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "service": "rapidx-ai-voice-agent",
        "worker_status": "online" if ensure_agent_worker_running() else "offline",
    }

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    config = read_config()

    def sel(key, val):
        return "selected" if config.get(key) == val else ""

    has_groq = bool(config.get("groq_api_key") or os.getenv("GROQ_API_KEY"))
    has_openai = bool(config.get("openai_api_key") or os.getenv("OPENAI_API_KEY"))
    has_claude = bool(config.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY"))
    has_sarvam = bool(config.get("sarvam_api_key") or os.getenv("SARVAM_API_KEY"))
    has_deepgram = bool(config.get("deepgram_api_key") or os.getenv("DEEPGRAM_API_KEY"))

    # Fallback if no keys set at all
    if not (has_groq or has_openai or has_claude):
        has_groq = True
        has_openai = True
    if not (has_sarvam or has_deepgram or has_openai):
        has_sarvam = True

    provider_options = []
    if has_groq:
        provider_options.append(f'<option value="groq" {sel("llm_provider","groq")}>Groq (Ultra-Fast &amp; High Quality)</option>')
    if has_openai:
        provider_options.append(f'<option value="openai" {sel("llm_provider","openai")}>OpenAI</option>')
    if has_claude:
        provider_options.append(f'<option value="claude" {sel("llm_provider","claude")}>Claude (Anthropic)</option>')

    provider_options_html = "\n".join(provider_options)

    model_optgroups = []
    if has_groq:
        model_optgroups.append(f'''<optgroup label="Groq Models (Recommended)">
              <option value="llama-3.3-70b-versatile" {sel('llm_model','llama-3.3-70b-versatile')}>llama-3.3-70b-versatile (Recommended)</option>
              <option value="llama-3.1-8b-instant" {sel('llm_model','llama-3.1-8b-instant')}>llama-3.1-8b-instant (Ultra-Fast)</option>
              <option value="mixtral-8x7b-32768" {sel('llm_model','mixtral-8x7b-32768')}>mixtral-8x7b-32768</option>
              <option value="gemma2-9b-it" {sel('llm_model','gemma2-9b-it')}>gemma2-9b-it</option>
            </optgroup>''')
    if has_openai:
        model_optgroups.append(f'''<optgroup label="OpenAI Models">
              <option value="gpt-4o-mini" {sel('llm_model','gpt-4o-mini')}>gpt-4o-mini — Fast &amp; Cheap</option>
              <option value="gpt-4o" {sel('llm_model','gpt-4o')}>gpt-4o — High Intelligence</option>
              <option value="gpt-4.1-mini" {sel('llm_model','gpt-4.1-mini')}>gpt-4.1-mini — Fast &amp; Latest</option>
            </optgroup>''')
    if has_claude:
        model_optgroups.append(f'''<optgroup label="Claude (Anthropic) Models">
              <option value="claude-haiku-3-5-latest" {sel('llm_model','claude-haiku-3-5-latest')}>claude-haiku-3-5-latest — Ultra-Fast</option>
              <option value="claude-3-5-sonnet-latest" {sel('llm_model','claude-3-5-sonnet-latest')}>claude-3-5-sonnet-latest — Most Capable</option>
            </optgroup>''')

    model_optgroups_html = "\n".join(model_optgroups)

    voice_optgroups = []
    if has_sarvam:
        voice_optgroups.append(f'''<optgroup label="Sarvam AI (Indian Languages)">
              <option value="kavya" {sel('tts_voice','kavya')}>Kavya — Female, Friendly (Hindi/Hinglish)</option>
              <option value="ritu" {sel('tts_voice','ritu')}>Ritu — Female, Soft</option>
              <option value="priya" {sel('tts_voice','priya')}>Priya — Female, Warm</option>
              <option value="neha" {sel('tts_voice','neha')}>Neha — Female, Energetic</option>
              <option value="shreya" {sel('tts_voice','shreya')}>Shreya — Female, Clear</option>
              <option value="rahul" {sel('tts_voice','rahul')}>Rahul — Male, Deep</option>
              <option value="rohan" {sel('tts_voice','rohan')}>Rohan — Male, Balanced</option>
              <option value="dev" {sel('tts_voice','dev')}>Dev — Male, Professional</option>
              <option value="shubh" {sel('tts_voice','shubh')}>Shubh — Male, Formal</option>
              <option value="amit" {sel('tts_voice','amit')}>Amit — Male, Casual</option>
            </optgroup>''')
    if has_deepgram:
        voice_optgroups.append(f'''<optgroup label="Deepgram Aura Voices">
              <option value="aura-stella-en" {sel('tts_voice','aura-stella-en')}>Aura Stella — Female, Expressive</option>
              <option value="aura-asteria-en" {sel('tts_voice','aura-asteria-en')}>Aura Asteria — Female, Clear</option>
              <option value="aura-luna-en" {sel('tts_voice','aura-luna-en')}>Aura Luna — Female, Soft</option>
              <option value="aura-zeus-en" {sel('tts_voice','aura-zeus-en')}>Aura Zeus — Male, Deep</option>
              <option value="aura-orion-en" {sel('tts_voice','aura-orion-en')}>Aura Orion — Male, Natural</option>
              <option value="aura-arcas-en" {sel('tts_voice','aura-arcas-en')}>Aura Arcas — Male, Calm</option>
            </optgroup>''')
    if has_openai:
        voice_optgroups.append(f'''<optgroup label="OpenAI TTS Voices">
              <option value="alloy" {sel('tts_voice','alloy')}>Alloy — Neutral</option>
              <option value="echo" {sel('tts_voice','echo')}>Echo — Male</option>
              <option value="fable" {sel('tts_voice','fable')}>Fable — Accent</option>
              <option value="onyx" {sel('tts_voice','onyx')}>Onyx — Deep Male</option>
              <option value="nova" {sel('tts_voice','nova')}>Nova — Female</option>
              <option value="shimmer" {sel('tts_voice','shimmer')}>Shimmer — Female</option>
            </optgroup>''')

    voice_optgroups_html = "\n".join(voice_optgroups)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Voice Agent — Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0f1117;
      --sidebar: #161b27;
      --card: #1c2333;
      --border: #2a3448;
      --accent: #6c63ff;
      --accent-glow: rgba(108,99,255,0.18);
      --text: #e2e8f0;
      --muted: #8892a4;
      --green: #22c55e;
      --red: #ef4444;
      --yellow: #f59e0b;
      --sidebar-w: 240px;
    }}
    body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }}

    /* ── Sidebar ── */
    #sidebar {{
      width: 250px; min-width: 250px;
      background: #111520; border-right: 1px solid #1e2638;
      display: flex; flex-direction: column; height: 100vh;
      position: relative; z-index: 10; overflow: hidden;
    }}
    .sidebar-brand {{
      padding: 18px 20px;
      border-bottom: 1px solid #1e2638;
      display: flex; align-items: center; gap: 12px;
      flex-shrink: 0; background: rgba(0,0,0,0.2);
    }}
    .sidebar-brand .logo {{
      width: 36px; height: 36px;
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      border-radius: 10px; display: flex; align-items: center; justify-content: center;
      font-size: 18px; box-shadow: 0 4px 12px rgba(99,102,241,0.35);
    }}
    .sidebar-brand .brand-text {{ font-weight: 700; font-size: 14.5px; color: #f8fafc; letter-spacing: -0.01em; }}
    .sidebar-brand .brand-sub {{ font-size: 11px; color: #64748b; font-weight: 500; margin-top: 1px; }}
    .sidebar-nav {{ padding: 12px 10px; flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }}
    .sidebar-nav::-webkit-scrollbar {{ width: 4px; }}
    .sidebar-nav::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.08); border-radius: 4px; }}
    .nav-section {{ padding: 14px 12px 6px; font-size: 10px; font-weight: 700; color: #475569; text-transform: uppercase; letter-spacing: 0.1em; }}
    .nav-item {{
      display: flex; align-items: center; gap: 10px;
      padding: 9px 12px; cursor: pointer; font-size: 13px; font-weight: 500;
      color: #94a3b8; border-radius: 8px;
      transition: all 0.15s ease; user-select: none; margin: 1px 0;
    }}
    .nav-item:hover {{ color: #f8fafc; background: rgba(255,255,255,0.05); }}
    .nav-item.active {{
      color: #ffffff; font-weight: 600;
      background: linear-gradient(90deg, rgba(99,102,241,0.22) 0%, rgba(99,102,241,0.08) 100%);
      border: 1px solid rgba(99,102,241,0.3);
      box-shadow: 0 2px 8px rgba(99,102,241,0.15);
    }}
    .nav-item .icon {{ font-size: 15px; width: 22px; display: flex; align-items: center; justify-content: center; }}
    .sidebar-footer {{
      padding: 14px 18px;
      border-top: 1px solid #1e2638;
      font-size: 11.5px; color: #94a3b8; font-weight: 500;
      flex-shrink: 0; background: #0e111a;
      display: flex; align-items: center; justify-content: space-between;
    }}
    .status-dot {{
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: var(--green); margin-right: 6px; box-shadow: 0 0 6px var(--green);
    }}

    /* ── Main ── */
    #main {{ flex: 1; overflow-y: auto; background: var(--bg); }}
    .page {{ display: none; padding: 32px 36px; min-height: 100%; }}
    .page.active {{ display: block; }}
    .page-header {{ margin-bottom: 28px; }}
    .page-title {{ font-size: 22px; font-weight: 700; }}
    .page-sub {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}

    /* ── Cards ── */
    .card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 20px;
    }}
    .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }}
    .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
    .stat-label {{ font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }}
    .stat-value {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
    .stat-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

    /* ── Table ── */
    .table-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    thead th {{ padding: 12px 16px; text-align: left; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; background: rgba(255,255,255,0.03); border-bottom: 1px solid var(--border); }}
    tbody td {{ padding: 13px 16px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover {{ background: rgba(255,255,255,0.025); }}
    .badge {{ display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
    .badge-green {{ background: rgba(34,197,94,0.12); color: var(--green); }}
    .badge-gray {{ background: rgba(255,255,255,0.07); color: var(--muted); }}
    .badge-yellow {{ background: rgba(245,158,11,0.12); color: var(--yellow); }}

    /* ── Forms ── */
    label {{ display: block; font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }}
    input[type=text], input[type=password], input[type=number], select, textarea {{
      width: 100%; background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px 12px; color: var(--text); font-family: inherit;
      font-size: 13.5px; outline: none; transition: border-color 0.15s;
    }}
    input:focus, select:focus, textarea:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }}
    textarea {{ font-family: 'JetBrains Mono', 'Fira Code', monospace; resize: vertical; }}
    .form-group {{ margin-bottom: 20px; }}
    .form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .hint {{ font-size: 11.5px; color: var(--muted); margin-top: 5px; }}
    .section-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; }}
    .section-title {{ font-size: 14px; font-weight: 600; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }}

    /* ── Buttons ── */
    .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 9px 18px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; transition: all 0.15s; }}
    .btn-primary {{ background: var(--accent); color: #fff; }}
    .btn-primary:hover {{ background: #5a52e0; box-shadow: 0 0 16px var(--accent-glow); }}
    .btn-ghost {{ background: transparent; border: 1px solid var(--border); color: var(--muted); }}
    .btn-ghost:hover {{ border-color: var(--accent); color: var(--accent); }}
    .btn-sm {{ padding: 5px 12px; font-size: 12px; }}
    .save-bar {{
      position: sticky; bottom: 0; left: 0; right: 0;
      background: rgba(22,27,39,0.95); backdrop-filter: blur(12px);
      border-top: 1px solid var(--border);
      padding: 14px 36px; display: flex; align-items: center; justify-content: space-between; z-index: 20;
    }}
    .save-status {{ font-size: 13px; font-weight: 500; color: var(--green); opacity: 0; transition: opacity 0.3s; }}

    /* ── Calendar ── */
    .cal-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
    .cal-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }}
    .cal-day-name {{ text-align: center; font-size: 11px; color: var(--muted); font-weight: 600; padding: 8px 0; text-transform: uppercase; letter-spacing: 0.06em; }}
    .cal-cell {{
      min-height: 80px; background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 10px; cursor: pointer; transition: all 0.18s; position: relative;
    }}
    .cal-cell:hover {{ border-color: var(--accent); background: var(--accent-glow); transform: scale(1.03); box-shadow: 0 4px 20px rgba(108,99,255,0.15); }}
    .cal-cell.today {{ border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-glow); }}
    .cal-cell.other-month {{ opacity: 0.3; }}
    .cal-num {{ font-size: 13px; font-weight: 700; }}
    .cal-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent); margin-top: 6px; box-shadow: 0 0 6px var(--accent); }}
    .cal-booking-count {{ font-size: 10px; color: var(--accent); font-weight: 600; margin-top: 3px; }}
    .day-panel {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-top: 20px; display: none; }}
    .day-panel.show {{ display: block; animation: fadeIn 0.2s ease; }}
    .booking-item {{ padding: 14px; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px; transition: border-color 0.15s; }}
    .booking-item:hover {{ border-color: var(--accent); }}
    .booking-item:last-child {{ margin-bottom: 0; }}

    /* ── Modal ── */
    .modal-overlay {{
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.7); backdrop-filter: blur(6px);
      z-index: 1000; align-items: center; justify-content: center;
    }}
    .modal-overlay.open {{ display: flex; animation: fadeIn 0.2s ease; }}
    .modal-box {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 16px; padding: 28px; min-width: 480px; max-width: 600px; width: 90%;
      box-shadow: 0 24px 60px rgba(0,0,0,0.5);
      animation: slideUp 0.25s ease;
    }}
    .modal-title {{ font-size: 18px; font-weight: 700; margin-bottom: 6px; }}
    .modal-sub {{ font-size: 12px; color: var(--muted); margin-bottom: 20px; }}
    .modal-close {{
      position: absolute; top: 20px; right: 24px;
      background: none; border: none; color: var(--muted);
      font-size: 20px; cursor: pointer; line-height: 1;
    }}
    .modal-close:hover {{ color: var(--text); }}
    @keyframes fadeIn {{ from {{ opacity:0 }} to {{ opacity:1 }} }}
    @keyframes slideUp {{ from {{ transform:translateY(20px); opacity:0 }} to {{ transform:translateY(0); opacity:1 }} }}

    /* ── Premium extras ── */
    .stat-card {{ transition: transform 0.15s, box-shadow 0.15s; }}
    .stat-card:hover {{ transform: translateY(-3px); box-shadow: 0 8px 30px rgba(108,99,255,0.12); }}
    .stat-accent {{ color: var(--accent); }}
    .pulse {{ animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100% {{ box-shadow: 0 0 6px var(--green); }} 50% {{ box-shadow: 0 0 14px var(--green); }} }}
  </style>
</head>
<body>

<!-- ── Day Detail Modal ── -->
<div class="modal-overlay" id="day-modal" onclick="if(event.target===this)closeDayModal()">
  <div class="modal-box" style="position:relative;">
    <button class="modal-close" onclick="closeDayModal()">✕</button>
    <div class="modal-title" id="modal-date-title">Bookings</div>
    <div class="modal-sub" id="modal-date-sub"></div>
    <div id="modal-bookings-body"></div>
  </div>
</div>

<!-- ── Sidebar ── -->
<nav id="sidebar">
  <div class="sidebar-brand">
    <div class="logo">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="12" cy="12" r="10" fill="rgba(255,255,255,0.12)"/>
        <path d="M8 12c0-2.21 1.79-4 4-4s4 1.79 4 4" stroke="white" stroke-width="1.8" stroke-linecap="round"/>
        <circle cx="12" cy="15" r="2" fill="white"/>
        <path d="M6 18c1.5-1.5 3.5-2.5 6-2.5s4.5 1 6 2.5" stroke="white" stroke-width="1.4" stroke-linecap="round" opacity="0.6"/>
      </svg>
    </div>
    <div>
      <div class="brand-text">Voice Agent</div>
      <div class="brand-sub">RapidX AI</div>
    </div>
  </div>
  <div class="sidebar-nav">
    <div class="nav-section">Overview</div>
    <div class="nav-item active" onclick="goTo('dashboard', this)"><span class="icon">📊</span> Dashboard</div>
    <div class="nav-item" onclick="goTo('demo', this)"><span class="icon">🎙️</span> Web Demo Call</div>
    <div class="nav-item" onclick="goTo('calendar', this); loadCalendar();"><span class="icon">📅</span> Calendar</div>
    <div class="nav-section" style="margin-top:12px;">Configuration</div>
    <div class="nav-item" onclick="goTo('agent', this)"><span class="icon">🤖</span> Agent Settings</div>
    <div class="nav-item" onclick="goTo('models', this)"><span class="icon">🎙️</span> Models & Voice</div>
    <div class="nav-item" onclick="goTo('credentials', this); initCustomCreds();"><span class="icon">🔑</span> API Credentials</div>
    <div class="nav-section" style="margin-top:12px;">Data</div>
    <div class="nav-item" onclick="goTo('logs', this); loadLogs();"><span class="icon">📞</span> Call Logs</div>
    <div class="nav-item" onclick="goTo('crm', this); loadCRM();"><span class="icon">👥</span> CRM Contacts</div>
    <div class="nav-section" style="margin-top:12px;">Calling</div>
    <div class="nav-item" onclick="goTo('outbound', this)"><span class="icon">📲</span> Outbound Calls</div>
    <div class="nav-item" onclick="goTo('languages', this); renderLangGrid();"><span class="icon">🌐</span> Language Presets</div>
    <div class="nav-item" onclick="goTo('knowledge', this); loadKnowledgeBase();"><span class="icon">🧠</span> Knowledge Base</div>
    <div class="nav-item" onclick="goTo('demo', this)"><span class="icon">✨</span> Shareable Link</div>
  </div>
  <div class="sidebar-footer">
    <span class="status-dot pulse"></span>Agent Online
  </div>
</nav>

<!-- ── Main Content ── -->
<div id="main">

  <!-- ── Dashboard ── -->
  <div id="page-dashboard" class="page active">
    <div class="page-header">
      <div class="page-title">Dashboard</div>
      <div class="page-sub">Real-time overview of your AI voice agent performance</div>
    </div>
    <div class="stat-grid" id="stat-grid">
      <div class="stat-card"><div class="stat-label">Total Calls</div><div class="stat-value" id="stat-calls">—</div><div class="stat-sub">All time</div></div>
      <div class="stat-card"><div class="stat-label">Bookings Made</div><div class="stat-value" id="stat-bookings">—</div><div class="stat-sub">Confirmed appointments</div></div>
      <div class="stat-card"><div class="stat-label">Avg Duration</div><div class="stat-value" id="stat-duration">—</div><div class="stat-sub">Seconds per call</div></div>
      <div class="stat-card"><div class="stat-label">Booking Rate</div><div class="stat-value" id="stat-rate">—</div><div class="stat-sub">Calls that converted</div></div>
    </div>
    <div class="section-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
        <div class="section-title" style="border:none;padding:0;margin:0;">Recent Calls</div>
        <button class="btn btn-ghost btn-sm" onclick="loadDashboard()">↻ Refresh</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Date</th><th>Phone</th><th>Duration</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="dash-table-body"><tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── Calendar ── -->
  <div id="page-calendar" class="page">
    <div class="page-header">
      <div class="page-title">Booking Calendar</div>
      <div class="page-sub">View confirmed appointments by date</div>
    </div>
    <div class="section-card">
      <div class="cal-header">
        <button class="btn btn-ghost btn-sm" onclick="changeMonth(-1)">← Prev</button>
        <div style="font-size:16px;font-weight:700;" id="cal-month-label">Month Year</div>
        <button class="btn btn-ghost btn-sm" onclick="changeMonth(1)">Next →</button>
      </div>
      <div class="cal-grid" id="cal-grid"></div>
      <div class="day-panel" id="day-panel">
        <div style="font-size:14px;font-weight:700;margin-bottom:12px;" id="day-panel-title">Selected Day</div>
        <div id="day-panel-body"></div>
      </div>
    </div>
  </div>

  <!-- ── Agent Settings ── -->
  <div id="page-agent" class="page">
    <div class="page-header">
      <div class="page-title">Agent Settings</div>
      <div class="page-sub">Configure AI personality, opening line, and sensitivity</div>
    </div>
    <div class="section-card">
      <div class="section-title">Opening Greeting</div>
      <div class="form-group">
        <label>First Line (What the agent says when a call connects)</label>
        <input type="text" id="first_line" value="{config.get('first_line', '')}" placeholder="Namaste! This is Aryan from RapidX AI...">
        <div class="hint">This is the very first thing the agent says. Keep it concise and warm.</div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">System Prompt</div>
      <div class="form-group">
        <label>Master System Prompt</label>
        <textarea id="agent_instructions" rows="16" placeholder="Enter the AI's full personality and instructions...">{config.get('agent_instructions', '')}</textarea>
        <div class="hint">Date and time context are injected automatically. Do not hardcode today's date.</div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">Listening Sensitivity</div>
      <div class="form-group" style="max-width:220px;">
        <label>Endpointing Delay (seconds)</label>
        <input type="number" id="stt_min_endpointing_delay" step="0.05" min="0.1" max="3.0" value="{config.get('stt_min_endpointing_delay', 0.6)}">
        <div class="hint">Seconds the AI waits after silence before responding. Default: 0.6</div>
      </div>
    </div>
    <div class="save-bar">
      <span class="save-status" id="save-status-agent">✅ Saved!</span>
      <button class="btn btn-primary" onclick="saveConfig('agent')">💾 Save Agent Settings</button>
    </div>
  </div>

  <!-- ── Models & Voice ── -->
  <div id="page-models" class="page">
    <div class="page-header">
      <div class="page-title">Models & Voice</div>
      <div class="page-sub">Select the LLM provider, brain model, and TTS voice persona</div>
    </div>
    <div class="section-card">
      <div class="section-title">Language Model (LLM)</div>
      <div class="form-row" style="max-width:720px;">
        <div class="form-group">
          <label>LLM Provider</label>
          <select id="llm_provider" onchange="onProviderChange()">
{provider_options_html}
          </select>
        </div>
        <div class="form-group">
          <label>Model</label>
          <select id="llm_model">
{model_optgroups_html}
          </select>
        </div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">Voice Synthesis (Sarvam bulbul:v3)</div>
      <div class="form-row" style="max-width:720px;">
        <div class="form-group">
          <label>Speaker Voice</label>
          <select id="tts_voice">
{voice_optgroups_html}
          </select>
        </div>
        <div class="form-group">
          <label>Language</label>
          <select id="tts_language">
            <option value="hi-IN" {sel('tts_language','hi-IN')}>Hindi (hi-IN)</option>
            <option value="en-IN" {sel('tts_language','en-IN')}>English India (en-IN)</option>
            <option value="ta-IN" {sel('tts_language','ta-IN')}>Tamil (ta-IN)</option>
            <option value="te-IN" {sel('tts_language','te-IN')}>Telugu (te-IN)</option>
            <option value="kn-IN" {sel('tts_language','kn-IN')}>Kannada (kn-IN)</option>
            <option value="ml-IN" {sel('tts_language','ml-IN')}>Malayalam (ml-IN)</option>
            <option value="mr-IN" {sel('tts_language','mr-IN')}>Marathi (mr-IN)</option>
            <option value="gu-IN" {sel('tts_language','gu-IN')}>Gujarati (gu-IN)</option>
            <option value="bn-IN" {sel('tts_language','bn-IN')}>Bengali (bn-IN)</option>
          </select>
        </div>
      </div>
    </div>
    <div class="save-bar">
      <span class="save-status" id="save-status-models">✅ Saved!</span>
      <button class="btn btn-primary" onclick="saveConfig('models')">💾 Save Model Settings</button>
    </div>
  </div>

  <!-- ── API Credentials ── -->
  <!-- CRM Contacts Page -->
  <div id="page-crm" class="page">
    <div class="page-header">
      <div class="page-title">👥 CRM Contacts</div>
      <div class="page-sub">Every caller recorded automatically — name, phone, call history</div>
    </div>
    <div class="section-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div class="section-title" style="margin:0;">All Contacts</div>
        <button class="btn btn-ghost btn-sm" onclick="loadCRM()">&#x21bb; Refresh</button>
      </div>
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="border-bottom:1px solid var(--border);">
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Name</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Phone</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Total Calls</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Last Seen</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Status</th>
            </tr>
          </thead>
          <tbody id="crm-tbody">
            <tr><td colspan="5" style="text-align:center;padding:32px;color:var(--muted);">Loading contacts...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── Language Presets Page ── -->
  <div id="page-languages" class="page">
    <div class="page-header">
      <div class="page-title">🌐 Language Presets</div>
      <div class="page-sub">One-click language configuration — saves immediately and takes effect on the next call</div>
    </div>
    <div class="section-card">
      <div class="section-title">Select a Language Mode</div>
      <div id="lang-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;"></div>
    </div>
    <div class="section-card" style="margin-top:0;">
      <div class="section-title">About Multilingual Mode</div>
      <p style="font-size:13px;color:var(--muted);line-height:1.7;">
        In <strong style="color:var(--text);">Multilingual (Auto)</strong> mode the agent listens to the caller's first message and 
        automatically replies in the same language for the rest of the call. 
        Ideal for showcasing the agent across different audiences.<br><br>
        Language changes take effect on the <strong style="color:var(--accent);">next incoming call</strong>. 
        The TTS voice and language code are updated automatically.
      </p>
    </div>
  </div>

  <!-- ── Outbound Calls Page ── -->
  <div id="page-outbound" class="page">
    <div class="page-header">
      <div class="page-title">📲 Outbound Calls</div>
      <div class="page-sub">Dispatch the AI agent to call any number instantly</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div class="section-card">
        <div class="section-title">Single Call</div>
        <div class="form-group">
          <label>Phone Number (with country code)</label>
          <input type="text" id="call-single-num" placeholder="+91XXXXXXXXXX" style="font-family:monospace;">
          <div class="hint">Must start with + and country code e.g. +91</div>
        </div>
        <button class="btn btn-primary" onclick="makeSingleCall()" style="width:100%;">📞 Call Now</button>
        <div id="single-call-status" style="margin-top:12px;font-size:13px;"></div>
      </div>
      <div class="section-card">
        <div class="section-title">Bulk Call</div>
        <div class="form-group">
          <label>Phone Numbers (one per line)</label>
          <textarea id="call-bulk-nums" rows="6" placeholder="+91XXXXXXXXXX&#10;+91YYYYYYYYYY&#10;+44ZZZZZZZZZ"></textarea>
          <div class="hint">Each line is a separate call dispatched simultaneously</div>
        </div>
        <button class="btn btn-primary" onclick="makeBulkCall()" style="width:100%;">🚀 Call All Numbers</button>
        <div id="bulk-call-status" style="margin-top:12px;font-size:13px;"></div>
      </div>
    </div>
    <div class="section-card" id="call-results-card" style="display:none;">
      <div class="section-title">Call Results</div>
      <div id="call-results-body"></div>
    </div>
  </div>

  <!-- ── Web Demo Call Page ── -->
  <div id="page-demo" class="page">
    <div class="page-header">
      <div class="page-title">🎙️ Web Demo Call</div>
      <div class="page-sub">Talk directly to Rahul (AI Voice Agent) from your browser microphone</div>
    </div>
    
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;max-width:960px;">
      <!-- Interactive Web Call Widget -->
      <div class="section-card" style="text-align:center;padding:36px 24px;">
        <div style="width:80px;height:80px;border-radius:50%;background:linear-gradient(135deg,#6c63ff,#a855f7);display:flex;align-items:center;justify-content:center;font-size:36px;margin:0 auto 20px;box-shadow:0 0 24px rgba(108,99,255,0.3);">🎙️</div>
        <div style="font-size:20px;font-weight:700;margin-bottom:6px;">Talk to Rahul</div>
        <div style="font-size:13px;color:var(--muted);margin-bottom:28px;">Kona Kona Interiors · Karyah Training Calling Agent</div>
        
        <button class="btn btn-primary" id="dashStartBtn" onclick="startDashCall()" style="padding:14px 28px;font-size:15px;width:100%;justify-content:center;">📞 Start Demo Call</button>
        <button class="btn btn-danger" id="dashEndBtn" onclick="endDashCall()" style="padding:14px 28px;font-size:15px;width:100%;justify-content:center;display:none;background:#ef4444;color:#fff;">📵 End Call</button>
        
        <div id="dashStatus" style="font-size:13px;color:var(--muted);margin-top:18px;">Click to start a live voice demo in your browser</div>
        <div id="dashVolBar" style="display:none;gap:4px;align-items:flex-end;justify-content:center;height:32px;margin-top:16px;">
          <span id="db1" style="width:4px;height:8px;background:var(--accent);border-radius:2px;transition:height 0.1s;"></span>
          <span id="db2" style="width:4px;height:14px;background:var(--accent);border-radius:2px;transition:height 0.1s;"></span>
          <span id="db3" style="width:4px;height:22px;background:var(--accent);border-radius:2px;transition:height 0.1s;"></span>
          <span id="db4" style="width:4px;height:14px;background:var(--accent);border-radius:2px;transition:height 0.1s;"></span>
          <span id="db5" style="width:4px;height:8px;background:var(--accent);border-radius:2px;transition:height 0.1s;"></span>
        </div>
      </div>

      <!-- Shareable Link Generator -->
      <div class="section-card">
        <div class="section-title">✨ Shareable Public Link</div>
        <p style="font-size:13px;color:var(--muted);margin-bottom:20px;line-height:1.7;">
          Generate a public browser link to send to clients or team members. Anyone with the link can test the AI voice agent live without logging in.
        </p>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;">
          <button class="btn btn-primary" onclick="generateDemo()">✨ Generate Public Link</button>
          <button class="btn btn-ghost" id="copy-demo-btn" onclick="copyDemoLink()" style="display:none;">📋 Copy Link</button>
          <a id="open-demo-btn" href="#" target="_blank" class="btn btn-ghost" style="display:none;">↗ Open Demo</a>
        </div>
        <div id="demo-link-box" style="padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;font-family:monospace;font-size:12px;color:var(--accent);display:none;word-break:break-all;"></div>
        <div id="demo-status" style="margin-top:10px;font-size:12.5px;color:var(--muted);"></div>
      </div>
    </div>
  </div>

    <div id="page-credentials" class="page">
    <div class="page-header">
      <div class="page-title">🔑 API Credentials</div>
      <div class="page-sub">Click any category card below to view or edit API configurations</div>
    </div>

    <!-- Category Grid Cards -->
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px;">
      
      <!-- Card 1: LiveKit -->
      <div class="stat-card" style="padding:24px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;" onclick="openCredModal('livekit')">
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="font-size:32px;">📡</div>
            <span class="badge badge-green">LiveKit</span>
          </div>
          <div style="font-size:16px;font-weight:700;margin-bottom:4px;">LiveKit &amp; Telephony</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5;">LiveKit WebSocket URL, SIP Trunk ID, API Key, and Secret</div>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:12px;">Configure →</button>
      </div>

      <!-- Card 2: AI Providers -->
      <div class="stat-card" style="padding:24px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;" onclick="openCredModal('ai')">
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="font-size:32px;">🤖</div>
            <span class="badge badge-green">AI Providers</span>
          </div>
          <div style="font-size:16px;font-weight:700;margin-bottom:4px;">AI Provider Keys</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5;">Groq, OpenAI, Anthropic (Claude), and Sarvam AI keys</div>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:12px;">Configure →</button>
      </div>

      <!-- Card 3: Telegram Bot -->
      <div class="stat-card" style="padding:24px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;" onclick="openCredModal('telegram')">
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="font-size:32px;">✈️</div>
            <span class="badge badge-green">Telegram</span>
          </div>
          <div style="font-size:16px;font-weight:700;margin-bottom:4px;">Telegram Bot</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5;">Bot Token and Chat ID for instant call notifications</div>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:12px;">Configure →</button>
      </div>

      <!-- Card 4: Cal.com -->
      <div class="stat-card" style="padding:24px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;" onclick="openCredModal('cal')">
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="font-size:32px;">📅</div>
            <span class="badge badge-green">Cal.com</span>
          </div>
          <div style="font-size:16px;font-weight:700;margin-bottom:4px;">Cal.com Booking</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5;">Cal.com API key and Event Type ID for auto-scheduling</div>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:12px;">Configure →</button>
      </div>

      <!-- Card 5: Supabase -->
      <div class="stat-card" style="padding:24px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;" onclick="openCredModal('supabase')">
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="font-size:32px;">⚡</div>
            <span class="badge badge-green">Supabase</span>
          </div>
          <div style="font-size:16px;font-weight:700;margin-bottom:4px;">Supabase Database</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5;">Supabase URL and Anon key for configuration storage</div>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:12px;">Configure →</button>
      </div>

      <!-- Card 6: Custom API Keys -->
      <div class="stat-card" style="padding:24px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;" onclick="openCredModal('custom')">
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="font-size:32px;">⚙️</div>
            <span class="badge badge-gray">Custom</span>
          </div>
          <div style="font-size:16px;font-weight:700;margin-bottom:4px;">Custom API Keys</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5;">Add custom API keys and environment variables</div>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:12px;">Configure →</button>
      </div>

    </div>
  </div>

  <!-- ── Call Logs ── -->
  <div id="page-logs" class="page">
    <div class="page-header">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <div>
          <div class="page-title">Call Logs</div>
          <div class="page-sub">Full history of all incoming calls and transcripts</div>
        </div>
        <button class="btn btn-ghost" onclick="loadLogs()">↻ Refresh</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date & Time</th>
            <th>Phone</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Summary</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="logs-table-body"><tr><td colspan="6" style="text-align:center;padding:32px;color:var(--muted);">Click Refresh to load call logs</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ── Knowledge Base ── -->
  <div id="page-knowledge" class="page">
    <div class="page-header">
      <div class="page-title">🧠 Knowledge Base</div>
      <div class="page-sub">Text blocks injected into the agent's system prompt at the start of every call</div>
    </div>
    <div class="section-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;">
        <div class="section-title" style="border:none;padding:0;margin:0;">Knowledge Entries</div>
        <button class="btn btn-primary btn-sm" onclick="openKbModal()">+ Add Entry</button>
      </div>
      <div id="kb-list">
        <div style="text-align:center;padding:40px;color:var(--muted);">Loading knowledge base...</div>
      </div>
    </div>
    <div class="section-card" style="margin-top:20px;">
      <div class="section-title">How it works</div>
      <div style="font-size:13px;color:var(--muted);line-height:1.7;margin-top:10px;">
        <p>Every <strong style="color:var(--text)">active</strong> entry is appended to the agent's system prompt at the start of each call — right after your agent instructions.</p>
        <p style="margin-top:8px;">Use this to give the agent knowledge about your <strong style="color:var(--text)">products, services, pricing, FAQs, team, policies</strong> etc. — anything the caller might ask about.</p>
        <p style="margin-top:8px;">💡 Keep entries focused. One topic per entry makes it easier to enable/disable specific knowledge.</p>
      </div>
    </div>
  </div>

</div><!-- /main -->

<!-- ── Category Modal: LiveKit ── -->
<div class="modal-overlay" id="cred-modal-livekit" onclick="if(event.target===this)closeCredModal('livekit')">
  <div class="modal-box" style="position:relative;min-width:540px;">
    <button class="modal-close" onclick="closeCredModal('livekit')">✕</button>
    <div class="modal-title">📡 LiveKit &amp; Telephony</div>
    <div class="modal-sub">LiveKit connection credentials and SIP Trunk settings</div>
    <div class="form-row" style="margin-bottom:14px;">
      <div class="form-group"><label>LiveKit URL</label><input type="text" id="livekit_url" value="{config.get('livekit_url', '')}"></div>
      <div class="form-group"><label>SIP Trunk ID</label><input type="text" id="sip_trunk_id" value="{config.get('sip_trunk_id', '')}"></div>
      <div class="form-group"><label>API Key</label><input type="password" id="livekit_api_key" value="{config.get('livekit_api_key', '')}"></div>
      <div class="form-group"><label>API Secret</label><input type="password" id="livekit_api_secret" value="{config.get('livekit_api_secret', '')}"></div>
    </div>
    <!-- Extra Fields Container -->
    <div id="extra-cred-livekit" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
    <div style="display:flex;gap:10px;justify-content:space-between;align-items:center;">
      <button class="btn btn-ghost btn-sm" onclick="addExtraField('livekit')">+ Add New Field</button>
      <div style="display:flex;gap:10px;align-items:center;">
        <span class="save-status" id="save-status-livekit">✅ Saved!</span>
        <button class="btn btn-ghost btn-sm" onclick="closeCredModal('livekit')">Close</button>
        <button class="btn btn-primary btn-sm" onclick="saveConfigCategory('livekit')">💾 Save LiveKit</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Category Modal: AI Providers ── -->
<div class="modal-overlay" id="cred-modal-ai" onclick="if(event.target===this)closeCredModal('ai')">
  <div class="modal-box" style="position:relative;min-width:540px;">
    <button class="modal-close" onclick="closeCredModal('ai')">✕</button>
    <div class="modal-title">🤖 AI Provider Keys</div>
    <div class="modal-sub">API keys for LLM and Speech services</div>
    <div class="form-row" style="margin-bottom:14px;">
      <div class="form-group"><label>Groq API Key</label><input type="password" id="groq_api_key" value="{config.get('groq_api_key', '')}"></div>
      <div class="form-group"><label>OpenAI API Key</label><input type="password" id="openai_api_key" value="{config.get('openai_api_key', '')}"></div>
      <div class="form-group"><label>Anthropic API Key</label><input type="password" id="anthropic_api_key" value="{config.get('anthropic_api_key', '')}"></div>
      <div class="form-group"><label>Sarvam API Key</label><input type="password" id="sarvam_api_key" value="{config.get('sarvam_api_key', '')}"></div>
    </div>
    <!-- Extra Fields Container -->
    <div id="extra-cred-ai" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
    <div style="display:flex;gap:10px;justify-content:space-between;align-items:center;">
      <button class="btn btn-ghost btn-sm" onclick="addExtraField('ai')">+ Add New Provider Key</button>
      <div style="display:flex;gap:10px;align-items:center;">
        <span class="save-status" id="save-status-ai">✅ Saved!</span>
        <button class="btn btn-ghost btn-sm" onclick="closeCredModal('ai')">Close</button>
        <button class="btn btn-primary btn-sm" onclick="saveConfigCategory('ai')">💾 Save AI Keys</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Category Modal: Telegram Bot ── -->
<div class="modal-overlay" id="cred-modal-telegram" onclick="if(event.target===this)closeCredModal('telegram')">
  <div class="modal-box" style="position:relative;min-width:540px;">
    <button class="modal-close" onclick="closeCredModal('telegram')">✕</button>
    <div class="modal-title">✈️ Telegram Bot Notifications</div>
    <div class="modal-sub">Receive instant Telegram notifications for every call and appointment</div>
    <div class="form-row" style="margin-bottom:14px;">
      <div class="form-group"><label>Telegram Bot Token</label><input type="password" id="telegram_bot_token" value="{config.get('telegram_bot_token', '')}"></div>
      <div class="form-group"><label>Telegram Chat ID</label><input type="text" id="telegram_chat_id" value="{config.get('telegram_chat_id', '')}"></div>
    </div>
    <!-- Extra Fields Container -->
    <div id="extra-cred-telegram" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
    <div style="display:flex;gap:10px;justify-content:space-between;align-items:center;">
      <button class="btn btn-ghost btn-sm" onclick="addExtraField('telegram')">+ Add New Field</button>
      <div style="display:flex;gap:10px;align-items:center;">
        <span class="save-status" id="save-status-telegram">✅ Saved!</span>
        <button class="btn btn-ghost btn-sm" onclick="closeCredModal('telegram')">Close</button>
        <button class="btn btn-primary btn-sm" onclick="saveConfigCategory('telegram')">💾 Save Telegram</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Category Modal: Cal.com ── -->
<div class="modal-overlay" id="cred-modal-cal" onclick="if(event.target===this)closeCredModal('cal')">
  <div class="modal-box" style="position:relative;min-width:540px;">
    <button class="modal-close" onclick="closeCredModal('cal')">✕</button>
    <div class="modal-title">📅 Cal.com Calendar Booking</div>
    <div class="modal-sub">Cal.com API key and Event Type ID for auto-scheduling appointments during calls</div>
    <div class="form-row" style="margin-bottom:14px;">
      <div class="form-group"><label>Cal.com API Key</label><input type="password" id="cal_api_key" value="{config.get('cal_api_key', '')}"></div>
      <div class="form-group"><label>Cal.com Event Type ID</label><input type="text" id="cal_event_type_id" value="{config.get('cal_event_type_id', '')}"></div>
    </div>
    <!-- Extra Fields Container -->
    <div id="extra-cred-cal" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
    <div style="display:flex;gap:10px;justify-content:space-between;align-items:center;">
      <button class="btn btn-ghost btn-sm" onclick="addExtraField('cal')">+ Add New Field</button>
      <div style="display:flex;gap:10px;align-items:center;">
        <span class="save-status" id="save-status-cal">✅ Saved!</span>
        <button class="btn btn-ghost btn-sm" onclick="closeCredModal('cal')">Close</button>
        <button class="btn btn-primary btn-sm" onclick="saveConfigCategory('cal')">💾 Save Cal.com</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Category Modal: Supabase ── -->
<div class="modal-overlay" id="cred-modal-supabase" onclick="if(event.target===this)closeCredModal('supabase')">
  <div class="modal-box" style="position:relative;min-width:540px;">
    <button class="modal-close" onclick="closeCredModal('supabase')">✕</button>
    <div class="modal-title">⚡ Supabase Database</div>
    <div class="modal-sub">Supabase API credentials for agent configuration and CRM storage</div>
    <div class="form-row" style="margin-bottom:14px;">
      <div class="form-group"><label>Supabase URL</label><input type="text" id="supabase_url" value="{config.get('supabase_url', '')}"></div>
      <div class="form-group"><label>Supabase Anon Key</label><input type="password" id="supabase_key" value="{config.get('supabase_key', '')}"></div>
    </div>
    <!-- Extra Fields Container -->
    <div id="extra-cred-supabase" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
    <div style="display:flex;gap:10px;justify-content:space-between;align-items:center;">
      <button class="btn btn-ghost btn-sm" onclick="addExtraField('supabase')">+ Add New Field</button>
      <div style="display:flex;gap:10px;align-items:center;">
        <span class="save-status" id="save-status-supabase">✅ Saved!</span>
        <button class="btn btn-ghost btn-sm" onclick="closeCredModal('supabase')">Close</button>
        <button class="btn btn-primary btn-sm" onclick="saveConfigCategory('supabase')">💾 Save Supabase</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Category Modal: Custom Keys ── -->
<div class="modal-overlay" id="cred-modal-custom" onclick="if(event.target===this)closeCredModal('custom')">
  <div class="modal-box" style="position:relative;min-width:580px;">
    <button class="modal-close" onclick="closeCredModal('custom')">✕</button>
    <div class="modal-title">⚙️ Custom API Configurations &amp; Keys</div>
    <div class="modal-sub">Add custom environment keys and integration secrets</div>
    <div style="display:flex;justify-content:flex-end;margin-bottom:14px;">
      <button class="btn btn-primary btn-sm" onclick="addCustomCredRow()">+ Add Custom API Key</button>
    </div>
    <div id="custom-cred-list" style="display:flex;flex-direction:column;gap:12px;max-height:360px;overflow-y:auto;margin-bottom:16px;"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end;align-items:center;">
      <span class="save-status" id="save-status-custom">✅ Saved!</span>
      <button class="btn btn-ghost btn-sm" onclick="closeCredModal('custom')">Close</button>
      <button class="btn btn-primary btn-sm" onclick="saveConfigCategory('custom')">💾 Save Custom Keys</button>
    </div>
  </div>
</div>

<!-- ── KB Add/Edit Modal ── -->
<div class="modal-overlay" id="kb-modal" onclick="if(event.target===this)closeKbModal()">
  <div class="modal-box" style="position:relative;min-width:540px;">
    <button class="modal-close" onclick="closeKbModal()">✕</button>
    <div class="modal-title" id="kb-modal-title">Add Knowledge Entry</div>
    <div class="modal-sub">This content will be injected into the agent's system prompt during calls</div>
    <input type="hidden" id="kb-edit-id">
    <div style="margin-bottom:14px;">
      <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Title (optional — helps you identify the entry)</label>
      <input id="kb-title" type="text" placeholder="e.g. Pricing Plans, Company Overview, FAQ" style="width:100%;padding:10px 14px;background:var(--input-bg);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;">
    </div>
    <div style="margin-bottom:14px;">
      <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Content <span style="color:var(--accent)">*</span></label>
      <textarea id="kb-content" rows="8" placeholder="Write the knowledge content here. Be specific and clear. Example:\n\nOur Starter plan costs ₹999/month and includes 500 AI calls, 5 languages, and email support.\nOur Pro plan costs ₹2499/month and includes unlimited calls, all 10 languages, priority support, and custom voice." style="width:100%;padding:10px 14px;background:var(--input-bg);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:13px;line-height:1.6;resize:vertical;font-family:inherit;"></textarea>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end;">
      <button class="btn btn-ghost btn-sm" onclick="closeKbModal()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="saveKbEntry()" id="kb-save-btn">Save Entry</button>
    </div>
  </div>
</div>

<script>
// ── Navigation ──────────────────────────────────────────────────────────────
function goTo(pageId, el) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const target = document.getElementById('page-' + pageId);
  if (target) target.classList.add('active');
  if (el) el.classList.add('active');
}}

// ── Stats & Dashboard ───────────────────────────────────────────────────────
async function loadDashboard() {{
  try {{
    const [stats, logs] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/logs').then(r => r.json())
    ]);
    document.getElementById('stat-calls').textContent = stats.total_calls ?? '—';
    document.getElementById('stat-bookings').textContent = stats.total_bookings ?? '—';
    document.getElementById('stat-duration').textContent = stats.avg_duration ? stats.avg_duration + 's' : '—';
    document.getElementById('stat-rate').textContent = stats.booking_rate ? stats.booking_rate + '%' : '—';

    const tbody = document.getElementById('dash-table-body');
    if (!logs || logs.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);">No calls yet. Make a test call!</td></tr>';
      return;
    }}
    tbody.innerHTML = logs.slice(0, 10).map(log => `
      <tr>
        <td style="color:var(--muted)">${{new Date(log.created_at).toLocaleString()}}</td>
        <td style="font-weight:600">${{log.phone_number || 'Unknown'}}</td>
        <td>${{log.duration_seconds || 0}}s</td>
        <td>${{badgeFor(log.summary)}}</td>
        <td>
          ${{log.id ? `<a style="color:var(--accent);font-size:12px;text-decoration:none;" href="/api/logs/${{log.id}}/transcript" download="transcript_${{log.id}}.txt">⬇ Download</a>` : ''}}
        </td>
      </tr>`).join('');
  }} catch(e) {{
    document.getElementById('dash-table-body').innerHTML = '<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);">Could not load data — check Supabase credentials.</td></tr>';
  }}
}}

function badgeFor(summary) {{
  if (!summary) return '<span class="badge badge-gray">Ended</span>';
  if (summary.toLowerCase().includes('confirm')) return '<span class="badge badge-green">✓ Booked</span>';
  if (summary.toLowerCase().includes('cancel')) return '<span class="badge badge-yellow">✗ Cancelled</span>';
  return '<span class="badge badge-gray">Completed</span>';
}}

// ── Call Logs ───────────────────────────────────────────────────────────────
async function loadLogs() {{
  const tbody = document.getElementById('logs-table-body');
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--muted);">Loading...</td></tr>';
  try {{
    const logs = await fetch('/api/logs').then(r => r.json());
    if (!logs || logs.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--muted);">No call logs found.</td></tr>';
      return;
    }}
    tbody.innerHTML = logs.map(log => `
      <tr>
        <td style="color:var(--muted);white-space:nowrap">${{new Date(log.created_at).toLocaleString()}}</td>
        <td style="font-weight:600">${{log.phone_number || 'Unknown'}}</td>
        <td>${{log.duration_seconds || 0}}s</td>
        <td>${{badgeFor(log.summary)}}</td>
        <td style="color:var(--muted);font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{log.summary || ''}}">${{log.summary || '—'}}</td>
        <td>
          ${{log.id ? `<a class="btn btn-ghost btn-sm" style="text-decoration:none;" href="/api/logs/${{log.id}}/transcript" download="transcript_${{log.id}}.txt">⬇ Transcript</a>` : '—'}}
          ${{log.recording_url ? `<a class="btn btn-ghost btn-sm" style="text-decoration:none;margin-left:4px;" href="${{log.recording_url}}" target="_blank">🎧 Recording</a>` : ''}}
        </td>
      </tr>`).join('');
  }} catch(e) {{
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:#ef4444;">Error loading logs. Check Supabase credentials.</td></tr>';
  }}
}}

// ── Calendar ────────────────────────────────────────────────────────────────
let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth();
let allBookings = [];

async function loadCalendar() {{
  try {{ allBookings = await fetch('/api/bookings').then(r => r.json()); }} catch(e) {{ allBookings = []; }}
  renderCalendar();
}}

function changeMonth(dir) {{ calMonth += dir; if (calMonth > 11) {{ calMonth = 0; calYear++; }} else if (calMonth < 0) {{ calMonth = 11; calYear--; }} renderCalendar(); }}

function renderCalendar() {{
  const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  document.getElementById('cal-month-label').textContent = `${{months[calMonth]}} ${{calYear}}`;
  const grid = document.getElementById('cal-grid');
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const today = new Date();

  // Build booking map by date string YYYY-MM-DD
  const bookMap = {{}};
  allBookings.forEach(b => {{
    const d = b.created_at ? b.created_at.slice(0,10) : null;
    if (d) {{ bookMap[d] = bookMap[d] || []; bookMap[d].push(b); }}
  }});

  let html = days.map(d => `<div class="cal-day-name">${{d}}</div>`).join('');

  const first = new Date(calYear, calMonth, 1);
  const last = new Date(calYear, calMonth + 1, 0);
  const startPad = first.getDay();

  // Prev month padding
  for (let i = 0; i < startPad; i++) {{
    const d = new Date(calYear, calMonth, -startPad + i + 1);
    html += `<div class="cal-cell other-month"><div class="cal-num">${{d.getDate()}}</div></div>`;
  }}

  for (let day = 1; day <= last.getDate(); day++) {{
    const dateStr = `${{calYear}}-${{String(calMonth+1).padStart(2,'0')}}-${{String(day).padStart(2,'0')}}`;
    const bks = bookMap[dateStr] || [];
    const isToday = today.getFullYear()===calYear && today.getMonth()===calMonth && today.getDate()===day;
    html += `<div class="cal-cell${{isToday?' today':''}}" onclick="showDay('${{dateStr}}', ${{JSON.stringify(bks).replace(/'/g,"&apos;")}})">
      <div class="cal-num">${{day}}</div>
      ${{bks.length ? `<div class="cal-dot"></div><div class="cal-booking-count">${{bks.length}} booking${{bks.length>1?'s':''}}</div>` : ''}}
    </div>`;
  }}

  // Next month padding
  const endPad = 6 - last.getDay();
  for (let i = 1; i <= endPad; i++) {{
    html += `<div class="cal-cell other-month"><div class="cal-num">${{i}}</div></div>`;
  }}

  grid.innerHTML = html;
  document.getElementById('day-panel').classList.remove('show');
}}

function showDay(dateStr, bookings) {{
  // Update old inline panel too
  const panel = document.getElementById('day-panel');
  if (panel) {{
    panel.classList.add('show');
    document.getElementById('day-panel-title').textContent = `Bookings on ${{dateStr}}`;
  }}
  // Open modal overlay
  openDayModal(dateStr, bookings);
}}

function openDayModal(dateStr, bookings) {{
  const modal = document.getElementById('day-modal');
  const dateObj = new Date(dateStr + 'T00:00:00');
  const formatted = dateObj.toLocaleDateString('en-IN', {{weekday:'long', year:'numeric', month:'long', day:'numeric'}});
  document.getElementById('modal-date-title').textContent = formatted;
  document.getElementById('modal-date-sub').textContent =
    bookings.length ? `${{bookings.length}} booking${{bookings.length>1?'s':''}} on this day` : 'No bookings on this day';

  if (!bookings || bookings.length === 0) {{
    document.getElementById('modal-bookings-body').innerHTML =
      '<div style="text-align:center;padding:32px;color:var(--muted);font-size:14px;">📅 No bookings on this day.</div>';
  }} else {{
    document.getElementById('modal-bookings-body').innerHTML = bookings.map(b => `
      <div class="booking-item">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div style="font-weight:700;font-size:14px;">📞 ${{b.phone_number || 'Unknown'}}</div>
          <span class="badge badge-green">✅ Booked</span>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-top:6px;">🕐 ${{new Date(b.created_at).toLocaleTimeString('en-IN', {{hour:'2-digit',minute:'2-digit'}})}}</div>
        ${{b.summary ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px;background:rgba(255,255,255,0.04);border-radius:6px;">💬 ${{b.summary}}</div>` : ''}}
      </div>`).join('');
  }}
  modal.classList.add('open');
}}

function closeDayModal() {{
  document.getElementById('day-modal').classList.remove('open');
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeDayModal(); }});

// ── CRM ─────────────────────────────────────────────────────────────────────
async function loadCRM() {{
  const tbody = document.getElementById('crm-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:32px;color:var(--muted);">Loading contacts...</td></tr>';
  try {{
    const contacts = await fetch('/api/contacts').then(r => r.json());
    if (!contacts.length) {{
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--muted);">No contacts yet. They will appear here automatically after calls.</td></tr>';
      return;
    }}
    tbody.innerHTML = contacts.map(c => `
      <tr style="border-bottom:1px solid var(--border);transition:background 0.12s;" onmouseover="this.style.background='rgba(255,255,255,0.025)'" onmouseout="this.style.background=''">
        <td style="padding:14px 16px;font-weight:600;">${{c.caller_name || '<span style="color:var(--muted);font-weight:400;">Unknown</span>'}}</td>
        <td style="padding:14px 16px;font-family:monospace;font-size:13px;">${{c.phone_number || '—'}}</td>
        <td style="padding:14px 16px;text-align:center;"><span style="background:rgba(108,99,255,0.12);color:var(--accent);padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;">${{c.total_calls}}</span></td>
        <td style="padding:14px 16px;color:var(--muted);font-size:12px;">${{c.last_seen ? new Date(c.last_seen).toLocaleString('en-IN') : '—'}}</td>
        <td style="padding:14px 16px;">${{c.is_booked
          ? '<span class="badge badge-green">✅ Booked</span>'
          : '<span class="badge badge-gray">📵 No booking</span>'}}</td>
      </tr>`).join('');
  }} catch(e) {{
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:24px;color:#ef4444;">Error loading contacts. Check Supabase credentials.</td></tr>';
  }}
}}

// ── Save Config ─────────────────────────────────────────────────────────────
async function saveConfig(section) {{
  const get = id => {{ const el = document.getElementById(id); return el ? el.value : null; }};

  const payload = {{}};

  if (section === 'agent') {{
    Object.assign(payload, {{
      first_line: get('first_line'),
      agent_instructions: get('agent_instructions'),
      stt_min_endpointing_delay: parseFloat(get('stt_min_endpointing_delay')),
    }});
  }} else if (section === 'models') {{
    Object.assign(payload, {{
      llm_provider: get('llm_provider'),
      llm_model: get('llm_model'),
      tts_voice: get('tts_voice'),
      tts_language: get('tts_language'),
    }});
  }} else if (section === 'credentials') {{
    Object.assign(payload, {{
      livekit_url: get('livekit_url'), sip_trunk_id: get('sip_trunk_id'),
      livekit_api_key: get('livekit_api_key'), livekit_api_secret: get('livekit_api_secret'),
      groq_api_key: get('groq_api_key'), openai_api_key: get('openai_api_key'),
      anthropic_api_key: get('anthropic_api_key'), sarvam_api_key: get('sarvam_api_key'),
      cal_api_key: get('cal_api_key'), cal_event_type_id: get('cal_event_type_id'),
      telegram_bot_token: get('telegram_bot_token'), telegram_chat_id: get('telegram_chat_id'),
      supabase_url: get('supabase_url'), supabase_key: get('supabase_key'),
    }});
    document.querySelectorAll('.custom-cred-row').forEach(row => {{
      const k = row.querySelector('.custom-key').value.trim();
      const v = row.querySelector('.custom-val').value.trim();
      if (k) payload[k.toLowerCase()] = v;
    }});
  }}

  const res = await fetch('/api/config', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});

  const statusEl = document.getElementById('save-status-' + section);
  if (res.ok) {{
    if (statusEl) {{
      statusEl.style.opacity = '1';
      setTimeout(() => {{ statusEl.style.opacity = '0'; }}, 2500);
    }}
  }} else {{
    alert('Failed to save. Check server logs.');
  }}
}}

function openCredModal(cat) {{
  const modal = document.getElementById('cred-modal-' + cat);
  if (modal) modal.classList.add('open');
  if (cat === 'custom') initCustomCreds();
}}

function closeCredModal(cat) {{
  const modal = document.getElementById('cred-modal-' + cat);
  if (modal) modal.classList.remove('open');
}}

function addExtraField(cat, key = '', val = '') {{
  const container = document.getElementById('extra-cred-' + cat);
  if (!container) return;
  const div = document.createElement('div');
  div.className = 'custom-cred-row';
  div.style.cssText = 'display:flex;gap:12px;align-items:center;padding:6px 0;';
  div.innerHTML = `
    <div style="flex:1;">
      <input type="text" class="custom-key" placeholder="Key Name" value="${{key}}" style="width:100%;padding:8px 12px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;font-family:monospace;">
    </div>
    <div style="flex:2;">
      <input type="password" class="custom-val" placeholder="Value" value="${{val}}" style="width:100%;padding:8px 12px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;font-family:monospace;">
    </div>
    <button class="btn btn-ghost btn-sm" style="color:var(--red);padding:6px 10px;" onclick="this.parentElement.remove()" title="Delete">🗑</button>
  `;
  container.appendChild(div);
}}

async function saveConfigCategory(cat) {{
  const get = id => {{ const el = document.getElementById(id); return el ? el.value : null; }};
  const payload = {{}};

  if (cat === 'livekit') {{
    Object.assign(payload, {{
      livekit_url: get('livekit_url'),
      sip_trunk_id: get('sip_trunk_id'),
      livekit_api_key: get('livekit_api_key'),
      livekit_api_secret: get('livekit_api_secret'),
    }});
  }} else if (cat === 'ai') {{
    Object.assign(payload, {{
      groq_api_key: get('groq_api_key'),
      openai_api_key: get('openai_api_key'),
      anthropic_api_key: get('anthropic_api_key'),
      sarvam_api_key: get('sarvam_api_key'),
    }});
  }} else if (cat === 'telegram') {{
    Object.assign(payload, {{
      telegram_bot_token: get('telegram_bot_token'),
      telegram_chat_id: get('telegram_chat_id'),
    }});
  }} else if (cat === 'cal') {{
    Object.assign(payload, {{
      cal_api_key: get('cal_api_key'),
      cal_event_type_id: get('cal_event_type_id'),
    }});
  }} else if (cat === 'supabase') {{
    Object.assign(payload, {{
      supabase_url: get('supabase_url'),
      supabase_key: get('supabase_key'),
    }});
  }} else if (cat === 'integrations') {{
    Object.assign(payload, {{
      cal_api_key: get('cal_api_key'),
      cal_event_type_id: get('cal_event_type_id'),
      telegram_bot_token: get('telegram_bot_token'),
      telegram_chat_id: get('telegram_chat_id'),
      supabase_url: get('supabase_url'),
      supabase_key: get('supabase_key'),
    }});
  }}

  // Read extra custom fields inside this modal
  const modal = document.getElementById('cred-modal-' + cat);
  if (modal) {{
    modal.querySelectorAll('.custom-cred-row').forEach(row => {{
      const k = row.querySelector('.custom-key').value.trim();
      const v = row.querySelector('.custom-val').value.trim();
      if (k) payload[k.toLowerCase()] = v;
    }});
  }}

  const res = await fetch('/api/config', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(payload)
  }});

  const statusEl = document.getElementById('save-status-' + cat);
  if (res.ok) {{
    if (statusEl) {{
      statusEl.style.opacity = '1';
      setTimeout(() => {{ statusEl.style.opacity = '0'; }}, 2500);
    }}
  }} else {{
    alert('Failed to save. Check server logs.');
  }}
}}

function addCustomCredRow(key = '', val = '') {{
  const container = document.getElementById('custom-cred-list');
  if (!container) return;
  if (container.children.length === 1 && !container.children[0].classList.contains('custom-cred-row')) {{
    container.innerHTML = '';
  }}
  const div = document.createElement('div');
  div.className = 'custom-cred-row';
  div.style.cssText = 'display:flex;gap:12px;align-items:center;padding:12px;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:10px;';
  div.innerHTML = `
    <div style="flex:1;">
      <label style="display:block;font-size:11px;color:var(--muted);margin-bottom:4px;">Key Name</label>
      <input type="text" class="custom-key" placeholder="e.g. DEEPGRAM_API_KEY" value="${{key}}" style="width:100%;padding:8px 12px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;font-family:monospace;">
    </div>
    <div style="flex:2;">
      <label style="display:block;font-size:11px;color:var(--muted);margin-bottom:4px;">Credential Value</label>
      <input type="password" class="custom-val" placeholder="Paste API Key, Token, or Secret" value="${{val}}" style="width:100%;padding:8px 12px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;font-family:monospace;">
    </div>
    <button class="btn btn-ghost btn-sm" style="color:var(--red);margin-top:18px;padding:8px;" onclick="this.parentElement.remove()" title="Delete">🗑</button>
  `;
  container.appendChild(div);
}}

async function initCustomCreds() {{
  const container = document.getElementById('custom-cred-list');
  if (!container) return;
  try {{
    const cfg = await fetch('/api/config').then(r => r.json());
    const standardKeys = new Set([
      'first_line', 'agent_instructions', 'stt_min_endpointing_delay',
      'llm_provider', 'llm_model', 'tts_voice', 'tts_language',
      'livekit_url', 'sip_trunk_id', 'livekit_api_key', 'livekit_api_secret',
      'groq_api_key', 'openai_api_key', 'anthropic_api_key', 'sarvam_api_key',
      'cal_api_key', 'cal_event_type_id', 'telegram_bot_token', 'telegram_chat_id',
      'supabase_url', 'supabase_key'
    ]);
    container.innerHTML = '';
    let count = 0;
    for (const [k, v] of Object.entries(cfg)) {{
      if (!standardKeys.has(k.toLowerCase()) && v) {{
        addCustomCredRow(k.toUpperCase(), v);
        count++;
      }}
    }}
    if (count === 0) {{
      container.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:8px;">No custom credentials added yet. Click <strong>+ Add Custom API Key</strong> to add one.</div>';
    }}
  }} catch(e) {{}}
}}

function onProviderChange() {{
  const providerEl = document.getElementById('llm_provider');
  const modelSelect = document.getElementById('llm_model');
  if (!providerEl || !modelSelect) return;
  const provider = providerEl.value;

  const optgroups = modelSelect.querySelectorAll('optgroup');
  let matchFound = false;

  optgroups.forEach(group => {{
    const label = (group.label || '').toLowerCase();
    const isMatch = (provider === 'groq' && label.includes('groq')) ||
                    (provider === 'openai' && label.includes('openai')) ||
                    (provider === 'claude' && label.includes('claude'));

    group.style.display = isMatch ? '' : 'none';
    group.disabled = !isMatch;
    if (isMatch) {{
      const currentOpt = group.querySelector(`option[value="${{modelSelect.value}}"]`);
      if (currentOpt) matchFound = true;
    }}
  }});

  if (!matchFound) {{
    const defaults = {{
      groq: 'llama-3.3-70b-versatile',
      openai: 'gpt-4o-mini',
      claude: 'claude-haiku-3-5-latest'
    }};
    modelSelect.value = defaults[provider] || 'llama-3.3-70b-versatile';
  }}
}}


// ── Language Presets ─────────────────────────────────────────────────────────
const LANG_PRESETS = {{
  hinglish:    {{ flag:'🇮🇳', label:'Hinglish',                sub:'Hindi + English mix',        color:'#6c63ff' }},
  hindi:       {{ flag:'🇮🇳', label:'Hindi',                   sub:'Pure Hindi',                  color:'#a855f7' }},
  english:     {{ flag:'🇬🇧', label:'English (India)',          sub:'Indian English',              color:'#3b82f6' }},
  tamil:       {{ flag:'🇮🇳', label:'Tamil',                   sub:'தமிழ்',                       color:'#f59e0b' }},
  telugu:      {{ flag:'🇮🇳', label:'Telugu',                  sub:'తెలుగు',                      color:'#10b981' }},
  gujarati:    {{ flag:'🇮🇳', label:'Gujarati',                sub:'ગુજરાતી',                     color:'#ef4444' }},
  bengali:     {{ flag:'🇮🇳', label:'Bengali',                 sub:'বাংলা',                       color:'#f97316' }},
  marathi:     {{ flag:'🇮🇳', label:'Marathi',                 sub:'मराठी',                       color:'#14b8a6' }},
  kannada:     {{ flag:'🇮🇳', label:'Kannada',                 sub:'ಕನ್ನಡ',                       color:'#8b5cf6' }},
  malayalam:   {{ flag:'🇮🇳', label:'Malayalam',               sub:'മലയാളം',                      color:'#ec4899' }},
  multilingual:{{ flag:'🌍', label:'Multilingual (Auto)',       sub:"Detects caller's language",   color:'#22c55e' }},
}};

let currentLangPreset = 'hinglish';

async function initLanguagePage() {{
  try {{
    const cfg = await fetch('/api/config').then(r=>r.json());
    currentLangPreset = cfg.lang_preset || 'hinglish';
  }} catch(e) {{}}
  renderLangGrid();
}}

function renderLangGrid() {{
  const grid = document.getElementById('lang-grid');
  if (!grid) return;
  grid.innerHTML = Object.entries(LANG_PRESETS).map(([id, p]) => {{
    const active = id === currentLangPreset;
    const border = active ? p.color : 'var(--border)';
    const bg = active ? 'rgba(108,99,255,0.15)' : 'var(--bg)';
    const shadow = active ? 'box-shadow:0 0 16px rgba(108,99,255,0.2);' : '';
    const badge = active ? '<div style="font-size:10px;color:#22c55e;margin-top:6px;font-weight:600;">✓ ACTIVE</div>' : '';
    const textColor = active ? p.color : 'var(--text)';
    return `
    <div onclick="selectLangPreset('${{id}}')" style="background:${{bg}};border:2px solid ${{border}};border-radius:12px;padding:18px;cursor:pointer;transition:all 0.15s;${{shadow}}">
      <div style="font-size:28px;margin-bottom:8px;">${{p.flag}}</div>
      <div style="font-weight:700;font-size:14px;color:${{textColor}}">${{p.label}}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:3px;">${{p.sub}}</div>
      ${{badge}}
    </div>`;
  }}).join('');
}}

async function selectLangPreset(id) {{
  const p = LANG_PRESETS[id];
  if (!p) return;
  currentLangPreset = id;
  renderLangGrid();
  // Save lang_preset, tts_language, tts_voice to config
  try {{
    const cfg = await fetch('/api/config').then(r=>r.json());
    const voices = {{ hinglish:'kavya', hindi:'ritu', english:'dev', tamil:'priya', telugu:'kavya', gujarati:'rohan', bengali:'neha', marathi:'shubh', kannada:'rahul', malayalam:'ritu', multilingual:'kavya' }};
    const langs  = {{ hinglish:'hi-IN', hindi:'hi-IN', english:'en-IN', tamil:'ta-IN', telugu:'te-IN', gujarati:'gu-IN', bengali:'bn-IN', marathi:'mr-IN', kannada:'kn-IN', malayalam:'ml-IN', multilingual:'hi-IN' }};
    await fetch('/api/config', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ lang_preset: id, tts_language: langs[id], tts_voice: voices[id] }})
    }});
    const toast = document.createElement('div');
    toast.style.cssText='position:fixed;bottom:24px;right:24px;background:#22c55e;color:#fff;padding:12px 20px;border-radius:10px;font-size:13px;font-weight:600;z-index:9999;animation:slideUp 0.3s ease';
    toast.textContent = `✅ ${{p.label}} preset activated!`;
    document.body.appendChild(toast);
    setTimeout(()=>toast.remove(), 2500);
  }} catch(e) {{ alert('Failed to save: ' + e); }}
}}

// ── Outbound Calls ─────────────────────────────────────────────────────────── 
async function makeSingleCall() {{
  const phone = document.getElementById('call-single-num').value.trim();
  if (!phone) return;
  const el = document.getElementById('single-call-status');
  el.textContent = '⏳ Dispatching...';
  el.style.color = 'var(--muted)';
  try {{
    const res = await fetch('/api/call/single', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{phone}})
    }}).then(r=>r.json());
    if (res.status === 'ok') {{
      el.innerHTML = `✅ Call dispatched! Dispatch ID: <code>${{res.dispatch_id}}</code>`;
      el.style.color = 'var(--green)';
    }} else {{
      el.textContent = '❌ ' + res.message;
      el.style.color = 'var(--red)';
    }}
  }} catch(e) {{
    el.textContent = '❌ Error: ' + e;
    el.style.color = 'var(--red)';
  }}
}}

async function makeBulkCall() {{
  const nums = document.getElementById('call-bulk-nums').value.trim();
  if (!nums) return;
  const el = document.getElementById('bulk-call-status');
  el.textContent = '⏳ Dispatching all numbers...';
  try {{
    const res = await fetch('/api/call/bulk', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{numbers: nums}})
    }}).then(r=>r.json());
    const results = res.results || [];
    document.getElementById('call-results-card').style.display = 'block';
    document.getElementById('call-results-body').innerHTML = results.map(r => `
      <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);">
        <span style="font-family:monospace;">${{r.phone}}</span>
        <span class="badge ${{r.status==='ok'?'badge-green':'badge-gray'}}">${{r.status==='ok'?'✅ Sent':'❌ '+r.message}}</span>
      </div>`).join('');
    el.textContent = `✅ ${{results.filter(r=>r.status==='ok').length}}/${{results.length}} calls dispatched`;
    el.style.color = 'var(--green)';
  }} catch(e) {{
    el.textContent = '❌ Error: ' + e;
    el.style.color = 'var(--red)';
  }}
}}

// ── Demo Link ─────────────────────────────────────────────────────────────────
let demoUrl = '';
function initDemo() {{
  // no-op until user clicks generate
}}
async function generateDemo() {{
  const statusEl = document.getElementById('demo-status');
  statusEl.textContent = '⏳ Generating session...';
  try {{
    const origin = window.location.origin;
    demoUrl = origin + '/demo';
    document.getElementById('demo-link-box').textContent = demoUrl;
    document.getElementById('demo-link-box').style.display = 'block';
    document.getElementById('copy-demo-btn').style.display = 'inline-flex';
    document.getElementById('open-demo-btn').style.display = 'inline-flex';
    document.getElementById('open-demo-btn').href = demoUrl;
    document.getElementById('demo-iframe').src = demoUrl;
    document.getElementById('demo-iframe').style.display = 'block';
    statusEl.textContent = 'Session ready — share the link or use the preview below';
  }} catch(e) {{
    statusEl.textContent = '❌ ' + e;
  }}
}}
function copyDemoLink() {{
  navigator.clipboard.writeText(demoUrl);
  document.getElementById('copy-demo-btn').textContent = '✅ Copied!';
  setTimeout(()=>document.getElementById('copy-demo-btn').textContent='📋 Copy Link', 2000);
}}

// ── Knowledge Base ───────────────────────────────────────────────────────────
let _kbEntries = [];

async function loadKnowledgeBase() {{
  const list = document.getElementById('kb-list');
  if (!list) return;
  list.innerHTML = '<div style="text-align:center;padding:32px;color:var(--muted);">Loading...</div>';
  try {{
    _kbEntries = await fetch('/api/knowledge').then(r => r.json());
    renderKbList();
  }} catch(e) {{
    list.innerHTML = '<div style="text-align:center;padding:32px;color:#f87171;">Could not load knowledge base. Check your Supabase connection.</div>';
  }}
}}

function renderKbList() {{
  const list = document.getElementById('kb-list');
  if (!list) return;
  if (!_kbEntries || _kbEntries.length === 0) {{
    list.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted);">No knowledge entries yet. Click <strong>+ Add Entry</strong> to get started.</div>';
    return;
  }}
  list.innerHTML = _kbEntries.map(e => {{
    const active = e.is_active !== false;
    const preview = (e.content || '').substring(0, 140) + ((e.content || '').length > 140 ? '…' : '');
    return `<div style="padding:16px;border:1px solid ${{active ? 'var(--border)' : '#2a3448'}};border-radius:12px;margin-bottom:10px;background:${{active ? 'var(--card)' : 'rgba(0,0,0,0.2)'}};display:flex;gap:14px;align-items:flex-start;transition:all 0.2s;">
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
          <span style="font-size:13px;font-weight:600;color:${{active ? 'var(--text)' : 'var(--muted)'}}">${{e.title || 'Untitled Entry'}}</span>
          <span style="font-size:10px;padding:2px 8px;border-radius:20px;background:${{active ? 'rgba(34,197,94,0.12)' : 'rgba(148,163,184,0.1)'}};color:${{active ? '#22c55e' : 'var(--muted)'}}">${{active ? 'Active' : 'Inactive'}}</span>
        </div>
        <div style="font-size:12px;color:var(--muted);line-height:1.5;">${{preview}}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;">
        <button class="btn btn-ghost btn-sm" onclick="toggleKbEntry('${{e.id}}', ${{!active}})" title="${{active ? 'Deactivate' : 'Activate'}}">${{active ? '⏸' : '▶'}}</button>
        <button class="btn btn-ghost btn-sm" onclick="editKbEntry('${{e.id}}')" title="Edit">✏️</button>
        <button class="btn btn-ghost btn-sm" style="color:#f87171;" onclick="deleteKbEntry('${{e.id}}')" title="Delete">🗑</button>
      </div>
    </div>`;
  }}).join('');
}}

function openKbModal(id) {{
  document.getElementById('kb-modal-title').textContent = id ? 'Edit Knowledge Entry' : 'Add Knowledge Entry';
  document.getElementById('kb-save-btn').textContent = id ? 'Update Entry' : 'Save Entry';
  document.getElementById('kb-edit-id').value = id || '';
  if (!id) {{
    document.getElementById('kb-title').value = '';
    document.getElementById('kb-content').value = '';
  }}
  document.getElementById('kb-modal').classList.add('open');
  setTimeout(() => document.getElementById('kb-title').focus(), 100);
}}

function closeKbModal() {{
  document.getElementById('kb-modal').classList.remove('open');
}}

function editKbEntry(id) {{
  const e = _kbEntries.find(x => x.id === id);
  if (!e) return;
  openKbModal(id);
  document.getElementById('kb-title').value   = e.title   || '';
  document.getElementById('kb-content').value = e.content || '';
}}

async function saveKbEntry() {{
  const id      = document.getElementById('kb-edit-id').value;
  const title   = document.getElementById('kb-title').value.trim();
  const content = document.getElementById('kb-content').value.trim();
  if (!content) {{ alert('Content is required.'); return; }}
  const btn = document.getElementById('kb-save-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {{
    let res;
    if (id) {{
      res = await fetch('/api/knowledge/' + id, {{
        method: 'PUT', headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{ title, content }})
      }}).then(r => r.json());
    }} else {{
      res = await fetch('/api/knowledge', {{
        method: 'POST', headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{ title, content }})
      }}).then(r => r.json());
    }}
    if (res.success === false) {{ alert('Error: ' + (res.message || 'Unknown error')); return; }}
    closeKbModal();
    loadKnowledgeBase();
  }} catch(e) {{
    alert('Failed to save: ' + e.message);
  }} finally {{
    btn.disabled = false;
    btn.textContent = id ? 'Update Entry' : 'Save Entry';
  }}
}}

async function toggleKbEntry(id, newActive) {{
  try {{
    await fetch('/api/knowledge/' + id, {{
      method: 'PUT', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ is_active: newActive }})
    }});
    loadKnowledgeBase();
  }} catch(e) {{ alert('Failed to update: ' + e.message); }}
}}

async function deleteKbEntry(id) {{
  const e = _kbEntries.find(x => x.id === id);
  const name = (e && e.title) ? `"${{e.title}}"` : 'this entry';
  if (!confirm('Delete ' + name + '? This cannot be undone.')) return;
  try {{
    await fetch('/api/knowledge/' + id, {{ method: 'DELETE' }});
    loadKnowledgeBase();
  }} catch(e) {{ alert('Failed to delete: ' + e.message); }}
}}

// ── Dashboard Web Call ──────────────────────────────────────────────────────
let dashRoom = null;
async function startDashCall() {{
  const statusEl = document.getElementById('dashStatus');
  const startBtn = document.getElementById('dashStartBtn');
  const endBtn = document.getElementById('dashEndBtn');
  const volBar = document.getElementById('dashVolBar');

  if (!statusEl || !startBtn) return;
  statusEl.textContent = 'Connecting to AI Agent...';
  startBtn.disabled = true;
  try {{
    const res = await fetch('/api/demo-token').then(r => r.json());
    if (res.error) throw new Error(res.error);
    dashRoom = new LivekitClient.Room();
    dashRoom.on(LivekitClient.RoomEvent.TrackSubscribed, (track, publication, participant) => {{
      console.log('[LIVEKIT] Agent audio track subscribed:', track.kind, participant ? participant.identity : 'agent');
      if (track.kind === 'audio') {{
        const el = track.attach();
        document.body.appendChild(el);
        el.play().catch(err => console.warn('Audio playback error:', err));
      }}
    }});
    dashRoom.on(LivekitClient.RoomEvent.TrackUnsubscribed, (track) => {{
      track.detach().forEach(el => el.remove());
    }});
    await dashRoom.connect(res.url, res.token, {{autoSubscribe: true}});
    await dashRoom.localParticipant.setMicrophoneEnabled(true);

    startBtn.style.display = 'none';
    if (endBtn) endBtn.style.display = 'inline-flex';
    if (volBar) volBar.style.display = 'flex';
    statusEl.innerHTML = '<span class="status-dot pulse"></span>Connected — Speak now!';
    animateDashBars();
  }} catch(e) {{
    statusEl.textContent = '❌ Error: ' + e.message;
    startBtn.disabled = false;
  }}
}}

async function endDashCall() {{
  if (dashRoom) {{
    await dashRoom.disconnect();
    dashRoom = null;
  }}
  const startBtn = document.getElementById('dashStartBtn');
  const endBtn = document.getElementById('dashEndBtn');
  const volBar = document.getElementById('dashVolBar');
  const statusEl = document.getElementById('dashStatus');
  if (startBtn) {{ startBtn.style.display = 'inline-flex'; startBtn.disabled = false; }}
  if (endBtn) endBtn.style.display = 'none';
  if (volBar) volBar.style.display = 'none';
  if (statusEl) statusEl.textContent = 'Call ended. Click to start again.';
}}

function animateDashBars() {{
  if (!dashRoom) return;
  ['db1','db2','db3','db4','db5'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.style.height = (4 + Math.random()*24) + 'px';
  }});
  setTimeout(animateDashBars, 150);
}}

// ── Boot ────────────────────────────────────────────────────────────────────
loadDashboard();
initCustomCreds();
setTimeout(onProviderChange, 50);
</script>
</body>
</html>"""

    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ui_server:app", host="0.0.0.0", port=8000, reload=True)
