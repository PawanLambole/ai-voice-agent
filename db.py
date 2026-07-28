import os
import time
import json
import logging
from datetime import datetime
from supabase import create_client, Client

logger = logging.getLogger("db")

# ─── Columns added by supabase_migration_v2.sql ───────────────────────────────
# If the migration hasn't been run yet, these columns won't exist.
# We detect PGRST204 (schema cache miss) and retry with just base columns.
_ANALYTICS_COLUMNS = {
    "sentiment", "was_booked", "interrupt_count",
    "estimated_cost_usd", "call_date", "call_hour", "call_day_of_week",
}
_BASE_COLUMNS = {"phone_number", "duration_seconds", "transcript", "summary",
                 "recording_url", "caller_name"}

# ─── Retry helper ─────────────────────────────────────────────────────────────
_MAX_RETRIES = 3
_RETRY_DELAYS = [1.0, 2.0, 4.0]   # seconds — covers transient SSL 525 errors


def _is_retryable(err_str: str) -> bool:
    """True if the error is a transient network, DNS or SSL failure worth retrying."""
    transient = ("525", "ssl", "timeout", "connection", "network", "502", "503", "504", "name or service not known", "gai_error", "getaddrinfo")
    el = err_str.lower()
    return any(k in el for k in transient)


def _is_schema_error(err_str: str) -> bool:
    """True if Supabase returned PGRST204 — column not found in schema cache."""
    return "PGRST204" in err_str or "schema cache" in err_str.lower()


# ─── Client ───────────────────────────────────────────────────────────────────

def get_supabase() -> Client | None:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        logger.error(f"Failed to init Supabase client: {e}")
        return None


# ─── agent_config (cloud settings) ───────────────────────────────────────────

def load_config_from_db() -> dict | None:
    """
    Load agent configuration from the `agent_config` table in Supabase.
    Returns the stored dict, or None if DB is not configured / table missing.
    """
    supabase = get_supabase()
    if not supabase:
        return None
    for attempt in range(_MAX_RETRIES):
        try:
            res = (
                supabase.table("agent_config")
                .select("data")
                .eq("id", "default")
                .single()
                .execute()
            )
            row = res.data
            if isinstance(row, dict) and "data" in row:
                data = row["data"]
                if isinstance(data, dict):
                    return data
            return None
        except Exception as e:
            err = str(e)
            if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            # Table might not exist yet — not an error worth logging loudly
            if "does not exist" in err.lower() or "PGRST116" in err or "relation" in err.lower():
                logger.info("agent_config table not found — run supabase_migration_config.sql first.")
            else:
                logger.warning(f"Could not load config from DB: {err[:120]}")
            return None
    return None


def save_config_to_db(data: dict) -> bool:
    """
    Upsert the full agent configuration dict into `agent_config`.
    Returns True on success, False on failure.
    """
    # Never persist supabase_url / supabase_key into the DB itself
    # (they are needed to connect — chicken-and-egg problem)
    safe_data = {k: v for k, v in data.items() if k not in ("supabase_url", "supabase_key")}

    supabase = get_supabase()
    if not supabase:
        logger.info("Supabase not configured — skipping cloud config save.")
        return False
    for attempt in range(_MAX_RETRIES):
        try:
            supabase.table("agent_config").upsert(
                {"id": "default", "data": safe_data},
                on_conflict="id"
            ).execute()
            logger.info("Agent config saved to Supabase.")
            return True
        except Exception as e:
            err = str(e)
            if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"Failed to save config to DB: {err[:120]}")
            return False
    return False


# ─── knowledge_base ───────────────────────────────────────────────────────────

DEFAULT_KARYAH_KB = [
    {
        "id": "karyah-kb-1",
        "title": "Karyah Application & Training Knowledge Base",
        "content": (
            "# Karyah Application - AI Agent Knowledge Base\n\n"
            "## About Karyah\n"
            "Karyah is a work management application developed for Kona Kona Interiors. "
            "It helps supervisors and site workers coordinate daily work, track project progress, and maintain communication efficiently.\n\n"
            "## Purpose\n"
            "The application is designed to:\n"
            "* Assign work to site workers.\n"
            "* Manage daily tasks.\n"
            "* Track work progress.\n"
            "* Maintain proof of completed work.\n"
            "* Improve communication between supervisors and workers.\n\n"
            "## How It Works\n"
            "1. A supervisor creates a project.\n"
            "2. The supervisor assigns tasks to workers.\n"
            "3. Workers can view their assigned tasks in the Karyah application.\n"
            "4. Workers update the progress of their assigned tasks.\n"
            "5. When required, workers upload photos as proof of completed work.\n"
            "6. Supervisors review the updates and monitor overall project progress.\n\n"
            "## Main Features\n"
            "* Project management & Task assignment\n"
            "* Daily work updates & Progress tracking\n"
            "* Image upload as task proof\n"
            "* Work status monitoring\n\n"
            "## Purpose of This Call\n"
            "Inviting workers to a short 5-10 minute online Google Meet training session about the Karyah application.\n"
            "* Introduce yourself as Rahul calling from Kona Kona Interiors.\n"
            "* Confirm the person's identity.\n"
            "* Explain that the call is about Karyah application training (takes 5-10 mins).\n"
            "* Schedule a time via Cal.com or request worker to send 'Hi' on WhatsApp to receive the Google Meet link and Karyah application link."
        ),
        "is_active": True,
        "sort_order": 0
    }
]

def _read_local_kb():
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                data = json.load(f)
                kb = data.get("knowledge_base")
                if kb:
                    return kb
        except Exception:
            pass
    # Auto-seed initial Karyah KB into config.json
    _write_local_kb(DEFAULT_KARYAH_KB)
    return DEFAULT_KARYAH_KB

def _write_local_kb(kb_list):
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                data = json.load(f)
            data["knowledge_base"] = kb_list
            with open("config.json", "w") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

def _seed_default_knowledge(supabase) -> list:
    """Helper to seed initial default Karyah knowledge entries if table is empty."""
    try:
        clean_entries = [{k: v for k, v in item.items() if k != "id"} for item in DEFAULT_KARYAH_KB]
        res = supabase.table("knowledge_base").insert(clean_entries).execute()
        logger.info("Auto-seeded Karyah knowledge base entries into Supabase.")
        return res.data or DEFAULT_KARYAH_KB
    except Exception as e:
        logger.warning(f"Could not auto-seed default knowledge: {e}")
        return DEFAULT_KARYAH_KB


def fetch_knowledge_base(active_only: bool = False) -> list:
    """
    Fetch knowledge base entries (Supabase DB primary, config.json fallback).
    Auto-seeds Karyah Knowledge Base if empty.
    """
    supabase = get_supabase()
    if supabase:
        for attempt in range(_MAX_RETRIES):
            try:
                q = supabase.table("knowledge_base").select("*").order("created_at")
                if active_only:
                    q = q.eq("is_active", True)
                res = q.execute()
                data = res.data or []
                if not data and not active_only:
                    data = _seed_default_knowledge(supabase)
                return data
            except Exception as e:
                err = str(e)
                if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                logger.info(f"Using local knowledge base fallback ({err[:60]})")
                break

    # Fallback to local config.json if DB unavailable or table missing
    entries = _read_local_kb()
    if active_only:
        return [e for e in entries if e.get("is_active", True)]
    return entries


def get_kb_for_prompt() -> str:
    """
    Returns all active knowledge base entries formatted as a single
    [KNOWLEDGE BASE] block ready to be injected into the system prompt.
    """
    entries = fetch_knowledge_base(active_only=True)
    if not entries:
        return ""
    lines = ["\n\n[KNOWLEDGE BASE]\nUse the following information to answer caller questions accurately:\n"]
    for entry in entries:
        if isinstance(entry, dict):
            title   = entry.get("title", "").strip()
            content = entry.get("content", "").strip()
            if content:
                lines.append(f"## {title}\n{content}" if title else content)
    if len(lines) <= 1:
        return ""
    return "\n\n".join(lines)


def add_knowledge_entry(title: str, content: str, sort_order: int = 0) -> dict:
    """Insert a new knowledge base entry."""
    import uuid
    new_entry = {
        "id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "is_active": True,
        "sort_order": sort_order,
        "created_at": datetime.now().isoformat()
    }
    supabase = get_supabase()
    if supabase:
        try:
            res = supabase.table("knowledge_base").insert({
                "title": title,
                "content": content,
                "is_active": True,
                "sort_order": sort_order,
            }).execute()
            row = res.data[0] if res.data else new_entry
            return {"success": True, "data": row}
        except Exception as e:
            logger.warning(f"Could not add entry to DB, saving locally: {e}")

    # Fallback local save
    local_kb = _read_local_kb()
    local_kb.append(new_entry)
    _write_local_kb(local_kb)
    return {"success": True, "data": new_entry}


def update_knowledge_entry(entry_id: str, updates: dict) -> dict:
    """Update an existing knowledge base entry by UUID."""
    allowed = {"title", "content", "is_active", "sort_order"}
    safe_updates = {k: v for k, v in updates.items() if k in allowed}
    if not safe_updates:
        return {"success": False, "message": "No valid fields to update"}

    supabase = get_supabase()
    if supabase:
        try:
            res = supabase.table("knowledge_base").update(safe_updates).eq("id", entry_id).execute()
            return {"success": True, "data": res.data}
        except Exception as e:
            logger.warning(f"Could not update entry in DB, updating locally: {e}")

    # Fallback local update
    local_kb = _read_local_kb()
    for e in local_kb:
        if str(e.get("id")) == entry_id:
            e.update(safe_updates)
            break
    _write_local_kb(local_kb)
    return {"success": True, "data": safe_updates}


def delete_knowledge_entry(entry_id: str) -> dict:
    """Permanently delete a knowledge base entry by UUID."""
    supabase = get_supabase()
    if supabase:
        try:
            supabase.table("knowledge_base").delete().eq("id", entry_id).execute()
        except Exception as e:
            logger.warning(f"Could not delete entry from DB: {e}")

    local_kb = _read_local_kb()
    local_kb = [e for e in local_kb if str(e.get("id")) != entry_id]
    _write_local_kb(local_kb)
    return {"success": True}


# ─── save_call_log ────────────────────────────────────────────────────────────

def save_call_log(
    phone: str,
    duration: int,
    transcript: str,
    summary: str = "",
    recording_url: str = "",
    caller_name: str = "",
    sentiment: str = "unknown",
    estimated_cost_usd: float | None = None,
    call_date: str | None = None,
    call_hour: int | None = None,
    call_day_of_week: str | None = None,
    was_booked: bool = False,
    interrupt_count: int = 0,
) -> dict:
    """
    Insert a call log into Supabase.

    Strategy:
    1. Try with all columns (including analytics columns from migration_v2).
    2. If PGRST204 (column not in schema cache — migration not yet run),
       retry with only the base columns so the call is never silently lost.
    3. Retry up to 3× on transient SSL/network errors with exponential backoff.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        logger.info(f"Supabase not configured. Local log → {phone} {duration}s")
        return {"success": False, "message": "Supabase not configured"}

    supabase = get_supabase()
    if not supabase:
        return {"success": False, "message": "Supabase client failed"}

    # Build full payload
    full_data: dict = {
        "phone_number":    phone,
        "duration_seconds": duration,
        "transcript":      transcript,
        "summary":         summary,
        "sentiment":       sentiment,
        "was_booked":      was_booked,
        "interrupt_count": interrupt_count,
    }
    if recording_url:               full_data["recording_url"]      = recording_url
    if caller_name:                 full_data["caller_name"]         = caller_name
    if estimated_cost_usd is not None: full_data["estimated_cost_usd"] = estimated_cost_usd
    if call_date:                   full_data["call_date"]           = call_date
    if call_hour is not None:       full_data["call_hour"]           = call_hour
    if call_day_of_week:            full_data["call_day_of_week"]    = call_day_of_week

    # Base-only payload (fallback if migration not run)
    base_data: dict = {k: v for k, v in full_data.items() if k not in _ANALYTICS_COLUMNS}

    def _try_insert(data: dict, label: str) -> dict:
        for attempt in range(_MAX_RETRIES):
            try:
                res = supabase.table("call_logs").insert(data).execute()
                logger.info(f"Saved call log for {phone} ({label})")
                return {"success": True, "data": res.data}
            except Exception as e:
                err = str(e)
                if _is_schema_error(err):
                    # Column missing — propagate so caller can retry with base
                    raise RuntimeError("SCHEMA_ERROR:" + err)
                if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(f"Transient error (attempt {attempt+1}), retrying in {delay}s: {err[:80]}")
                    time.sleep(delay)
                    continue
                logger.error(f"Failed to save call log ({label}): {e}")
                return {"success": False, "message": err}
        return {"success": False, "message": "Max retries exceeded"}

    # Attempt 1: full payload
    try:
        return _try_insert(full_data, "full")
    except RuntimeError as e:
        err = str(e)
        if "SCHEMA_ERROR" in err:
            # Migration not run yet — fall back to base columns only
            logger.warning(
                "Analytics columns missing (run supabase_migration_v2.sql). "
                "Falling back to base columns for this call log."
            )
            return _try_insert(base_data, "base-fallback")
        raise


# ─── fetch_call_logs ──────────────────────────────────────────────────────────

def fetch_call_logs(limit: int = 50) -> list:
    supabase = get_supabase()
    if not supabase:
        return []
    for attempt in range(_MAX_RETRIES):
        try:
            res = (
                supabase.table("call_logs")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data
        except Exception as e:
            if _is_retryable(str(e)) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"Failed to fetch call logs: {e}")
            return []
    return []


# ─── fetch_bookings ───────────────────────────────────────────────────────────

def fetch_bookings() -> list:
    supabase = get_supabase()
    if not supabase:
        return []
    for attempt in range(_MAX_RETRIES):
        try:
            res = (
                supabase.table("call_logs")
                .select("id, phone_number, summary, created_at")
                .ilike("summary", "%Confirmed%")
                .order("created_at", desc=True)
                .limit(200)
                .execute()
            )
            return res.data
        except Exception as e:
            if _is_retryable(str(e)) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"Failed to fetch bookings: {e}")
            return []
    return []


# ─── fetch_stats ──────────────────────────────────────────────────────────────

def fetch_stats() -> dict:
    _empty = {"total_calls": 0, "total_bookings": 0, "avg_duration": 0, "booking_rate": 0}
    supabase = get_supabase()
    if not supabase:
        return _empty
    for attempt in range(_MAX_RETRIES):
        try:
            raw_rows = (supabase.table("call_logs").select("duration_seconds, summary").execute()).data or []
            rows = [r for r in raw_rows if isinstance(r, dict)]
            total = len(rows)
            bookings = sum(1 for r in rows if "Confirmed" in str(r.get("summary") or ""))
            durations = [int(r["duration_seconds"]) for r in rows if isinstance(r.get("duration_seconds"), (int, float))]
            avg_dur = round(sum(durations) / len(durations)) if durations else 0
            rate = round((bookings / total) * 100) if total else 0
            return {"total_calls": total, "total_bookings": bookings, "avg_duration": avg_dur, "booking_rate": rate}
        except Exception as e:
            if _is_retryable(str(e)) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"Failed to fetch stats: {e}")
            return _empty
    return _empty
