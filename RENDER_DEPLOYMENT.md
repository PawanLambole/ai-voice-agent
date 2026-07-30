# Deploying the Voice Agent to Render.com 🚀

This guide explains step-by-step how to deploy your AI Voice Agent (`agent.py` + `ui_server.py`) to **Render.com**.

---

## Architecture Overview

The system runs **two core components**:
1. **Agent Worker (`agent.py`)**: Connects to LiveKit Cloud via outbound WebSockets to process live audio, STT, LLM, and TTS.
2. **Dashboard Server (`ui_server.py`)**: A FastAPI web app providing the management UI and configuration endpoints.

Both services run simultaneously inside a single Docker container managed by **Supervisor** (`supervisord.conf`), making deployment to Render straightforward.

---

## Step 1: Push Code to GitHub / GitLab

1. Ensure all your local code (including `Dockerfile` and `supervisord.conf`) is committed and pushed to your repository.
2. **Do NOT commit your `.env` file** to public Git repositories.

---

## Step 2: Create a New Web Service on Render

1. Log in to [Render Dashboard](https://dashboard.render.com/).
2. Click the **New +** button in the top right and select **Web Service**.
3. Connect your **GitHub** or **GitLab** account (if not already connected).
4. Search for and select your repository (e.g., `InboundAIVoice` or `ai-voice-agent`).

---

## Step 3: Configure Service Details

Fill in the following fields in the Render dashboard:

- **Name**: `inbound-ai-voice` (or any preferred name)
- **Region**: Choose a region closest to your users or LiveKit server (e.g., *Singapore*, *Oregon*, *Frankfurt*).
- **Branch**: `main` (or your active deployment branch)
- **Root Directory**: Leave blank (default)
- **Runtime**: Select **Docker**
- **Dockerfile Path**: `./Dockerfile`
- **Instance Type**: 
  - ⚠️ **Important**: Select **Starter** ($7/mo) or higher.
  - *Note on Free Tier*: Render's free tier spins down after 15 minutes of inactivity. For LiveKit inbound call handling, the agent must stay online 24/7.

---

## Step 4: Add Environment Variables

In the Render Web Service creation page, scroll down to **Environment Variables** (or go to **Environment** tab after creating):

1. **Add `PORT`**:
   - `PORT`: `8000` *(Tells Render to route web traffic to port 8000)*

2. **Add credentials from your `.env` file**:

| Variable Name | Description / Example |
| :--- | :--- |
| `LIVEKIT_URL` | `wss://<your-project>.livekit.cloud` |
| `LIVEKIT_API_KEY` | Your LiveKit API Key |
| `LIVEKIT_API_SECRET` | Your LiveKit API Secret |
| `DEEPGRAM_API_KEY` | Deepgram API Key (if using Deepgram) |
| `GROQ_API_KEY` | Groq API Key |
| `SARVAM_API_KEY` | Sarvam AI API Key |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_KEY` | Supabase Service / Anon Key |
| `VOICELINK_SIP_DOMAIN` | VoiceLink SIP domain/host |
| `VOICELINK_USERNAME` | VoiceLink SIP Username |
| `VOICELINK_PASSWORD` | VoiceLink SIP Password |
| `VOICELINK_OUTBOUND_NUMBER` | VoiceLink outbound phone number |
| `SIP_TRUNK_ID` | LiveKit SIP Trunk ID |
| `OUTBOUND_TRUNK_ID` | LiveKit Outbound Trunk ID |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token for alerts |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID for alerts |
| `CAL_API_KEY` | Cal.com API Key |
| `CAL_EVENT_TYPE_ID` | Cal.com Event Type ID |

---

## Step 5: Deploy & Monitor

1. Click **Create Web Service**.
2. Render will pull your repository, build the multi-stage Docker image, and run `/usr/bin/supervisord`.
3. Monitor the **Logs** tab in Render:
   - You should see `supervisord` start both `agent` and `ui_server`.
   - `agent` log: `registered worker ...` (Successfully connected to LiveKit Cloud).
   - `ui_server` log: `Uvicorn running on http://0.0.0.0:8000`.

---

## Step 6: Access Your Dashboard

Once deployment is complete (status shows **Live**):
- Render will provide a public URL like: `https://inbound-ai-voice.onrender.com`.
- Open this URL in your browser to access your RapidX AI Dashboard!

---

## Troubleshooting & Tips

- **Agent disconnects / sleeps**: Ensure you are on a paid instance (Starter tier or higher) so Render doesn't put the service to sleep.
- **Port issue**: Make sure the environment variable `PORT=8000` is explicitly set in Render settings.
- **Viewing Logs**: Check Render's **Logs** panel to view real-time call logs, LLM responses, and Supervisor logs.
