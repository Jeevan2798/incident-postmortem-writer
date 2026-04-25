# Slack Bot — Production Deployment Pattern

A working Slack slash command that demonstrates the production deployment pattern from this OpenEnv submission.

## Demo

[![Watch the demo](https://img.youtube.com/vi/-OH_jnZ18rk/maxresdefault.jpg)](https://youtu.be/-OH_jnZ18rk)

**60-second video** showing the bot generating a structured post-mortem from a real PagerDuty incident JSON URL.

## How it works

```
Slack: /postmortem <incident-json-url>
   ↓
ngrok webhook → FastAPI (slackbot/app.py)
   ↓
PagerDuty importer (tools/pagerduty_importer.py)
   ↓
LLM agent (Llama 3.1 8B via Groq)
   ↓
Structured post-mortem posted back to Slack channel
```

## Why this matters

The hackathon environment trains agents on synthetic scenarios, but the real value is in production deployment. This bot proves the integration end-to-end:

- **Real Slack workspace** — not a mock
- **Real production JSON format** — PagerDuty Incident API v2 schema
- **Real LLM agent** — same model used throughout the submission
- **Real post-mortem output** — all 5 sections, specific service/version/mechanism/impact/action items

The same pattern extends to Datadog and Splunk via the existing importers in `tools/`.

## Setup

### 1. Slack app

1. Create app at https://api.slack.com/apps
2. Add slash command `/postmortem` with Request URL pointing at `<your-host>/slack/postmortem`
3. Install to a workspace
4. Note the Signing Secret

### 2. Environment

```bash
pip install -r slackbot/requirements.txt

export SLACK_SIGNING_SECRET=<from-slack-app>
export API_BASE_URL=https://api.groq.com/openai/v1
export MODEL_NAME=llama-3.1-8b-instant
export HF_TOKEN=<your-groq-key>
```

### 3. Run locally

```bash
# Terminal 1: bot
uvicorn slackbot.app:app --port 8000 --reload

# Terminal 2: tunnel
ngrok http 8000

# Update Slack slash command Request URL with the ngrok URL + /slack/postmortem
```

### 4. Test in Slack

```
/postmortem https://raw.githubusercontent.com/Jeevan2798/incident-postmortem-writer/main/samples/pagerduty/incident_payments_outage.json
```

## Architecture notes

- **3-second rule:** Slack requires a 200 response within 3 seconds. The bot acknowledges immediately and processes the incident in a background task (`BackgroundTasks` in FastAPI).
- **Signature verification:** Validates each request with HMAC-SHA256 against the Slack signing secret to prevent spoofing.
- **Async processing:** Long-running LLM calls (~15-20s) happen in the background; the response is posted via Slack's `response_url`.

## Production deployment

For real deployment beyond ngrok:

- **Hosting:** Render.com / Railway / Fly.io (any platform supporting FastAPI + persistent HTTPS)
- **Webhook source:** Replace slash command with PagerDuty webhook → automatic post-mortem on every incident resolution
- **Storage:** Persist generated post-mortems to a database for the human-review feedback loop
- **Monitoring:** Track which post-mortems get accepted unchanged vs heavily edited — that's the real quality signal

This is the loop the BLOG describes: *PagerDuty webhook fires → importer converts → agent drafts → human reviews → validated post-mortems become the next training cycle.*
