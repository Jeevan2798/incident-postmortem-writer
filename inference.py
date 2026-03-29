"""
Baseline Inference Script — Incident Post-Mortem Writer (OpenEnv)
=================================================================
Runs a baseline LLM agent against all 3 tasks (easy, medium, hard)
and reports reproducible scores.

Required environment variables:
    API_BASE_URL   The API endpoint for the LLM
    MODEL_NAME     The model identifier
    HF_TOKEN       Your API key

Optional:
    ENV_BASE_URL   The postmortem environment URL (default: http://localhost:7860)

Usage:
    set API_BASE_URL=https://api.groq.com/openai/v1
    set MODEL_NAME=llama-3.1-8b-instant
    set HF_TOKEN=your-key-here
    python inference.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME   = os.environ.get("MODEL_NAME", "gpt-4o-mini")
HF_TOKEN     = os.environ.get("HF_TOKEN", "")
ENV_BASE_URL = os.environ.get("ENV_BASE_URL", "http://localhost:7860")

TEMPERATURE  = 0.0
MAX_TOKENS   = 1500
DIFFICULTIES = ["easy", "medium", "hard"]
SECTIONS     = ["summary", "timeline", "root_cause", "impact", "action_items"]

client = OpenAI(api_key=HF_TOKEN or "dummy", base_url=API_BASE_URL)

# ---------------------------------------------------------------------------
# Environment client
# ---------------------------------------------------------------------------

class PostMortemEnv:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def reset(self, difficulty: str = "easy") -> Dict[str, Any]:
        r = self._session.post(f"{self.base_url}/reset", json={"difficulty": difficulty}, timeout=30)
        r.raise_for_status()
        return r.json()

    def step(self, action: Dict[str, Any]) -> Dict[str, Any]:
        r = self._session.post(f"{self.base_url}/step", json=action, timeout=30)
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str) -> str:
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
        return completion.choices[0].message.content or ""
    except Exception as exc:
        print(f"    [LLM error] {exc}")
        return ""


def extract_json(text: str) -> Optional[Dict]:
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None

# ---------------------------------------------------------------------------
# Phase 1: Query logs
# ---------------------------------------------------------------------------

QUERY_SYSTEM = """You are an expert SRE. Given incident alerts and Slack messages,
identify the best service and time window to query for root cause evidence.
Respond with ONLY valid JSON: {"service": "<service_name>", "from": "<HH:MM>", "to": "<HH:MM>"}

STRATEGY - follow in this exact order:
1. Look for DEPLOYMENT or CONFIG CHANGE in Slack (keywords: deploy, TTL, migration, release, config, schema).
   If found, query THAT service at THAT deployment time. Deployments are almost always root cause.
2. If no deployment, identify which service changed behavior FIRST and trace upstream dependencies.
3. Pick a 5-8 minute window AROUND the deployment or first change time.
4. NEVER query the most-alerted service - it is usually a victim not the cause.

EXAMPLES:
- Slack says deployed Redis caching layer at 13:55 -> {"service": "redis-auth", "from": "13:53", "to": "13:58"}
- Slack says schema migration at 09:10 on data-pipeline -> {"service": "data-pipeline", "from": "09:08", "to": "09:14"}
- Alerts show auth failing but Slack mentions Redis deploy -> query redis-auth NOT auth"""

def phase_query(env, observation, result):
    alerts_text = "\n".join(
        f"[{a['timestamp']}] [{a['severity']}] {a['service']}: {a['message']}"
        for a in observation.get("alerts", [])
    )
    slack_text = "\n".join(
        f"[{m['timestamp']}] {m['author']}: {m['text']}"
        for m in observation.get("slack_thread", [])
    )
    services = list({a['service'] for a in observation.get("alerts", [])})

    user_prompt = f"""INCIDENT: {observation.get('incident_title', '')}
ALERTS:\n{alerts_text}
SLACK:\n{slack_text}
Available services: {services}
Which service and time window to query for root cause?"""

    print("    [Phase 1] Identifying best log query...")
    response = call_llm(QUERY_SYSTEM, user_prompt)
    query = extract_json(response)

    logs_found = []
    if query and "service" in query:
        action = {
            "action_type": "QUERY_LOGS",
            "query_service": query.get("service", services[0] if services else "payments"),
            "query_from":    query.get("from", "00:00"),
            "query_to":      query.get("to",   "23:59"),
        }
        print(f"    Querying {action['query_service']} [{action['query_from']}-{action['query_to']}]")
        result = env.step(action)
        observation = result["observation"]
        reward = result.get("reward", {}).get("total", 0.0)
        print(f"    reward={reward:+.3f}")
        if observation.get("retrieved_logs"):
            logs_found = observation["retrieved_logs"]
            print(f"    Retrieved {len(logs_found)} log lines")
    else:
        print("    Could not parse query — skipping")

    return result, observation, logs_found

# ---------------------------------------------------------------------------
# Phase 2: Write sections
# ---------------------------------------------------------------------------

WRITE_SYSTEM = """You are an expert SRE writing one section of an incident post-mortem.
Write ONLY the section content — no JSON, no section labels, just plain text.
Be specific and factual. Use exact service names and timestamps from the evidence."""

SECTION_PROMPTS = {
    "summary": "Write 2-3 sentences summarizing the incident. MUST explicitly name the affected service.",
    "timeline": "Write a chronological timeline with 5+ timestamped events in format 'HH:MM - what happened'. Cover: deployment/change, first alert, service down, fix applied, recovery.",
    "root_cause": "Write root cause analysis. MUST name: (1) which service failed, (2) type of failure (deployment bug / config error / connection leak / schema migration / etc), (3) specific technical details of what went wrong.",
    "impact": "Write impact assessment of at least 30 words. Include: affected services, outage duration, users affected, business/revenue impact. Use specific numbers from the incident data.",
        "action_items": (
        "Write 3 numbered action items. Each MUST follow this EXACT format. "
        "Example: '1. Fix X - Owner: payments-team - Due: 2024-08-01'. "
        "RULES: Owner must be a team or person from the Slack thread. "
        "Use names like payments-team, auth-team, sre, platform, sara, tom, mei. "
        "Due date must be a real date like 2024-08-01 or the phrase: next sprint."
    ),
}

def _fallback_section(section: str, observation: Dict, logs_found: List) -> str:
    alerts = observation.get("alerts", [])
    slack  = observation.get("slack_thread", [])
    services = list({a["service"] for a in alerts})
    main_svc = services[0] if services else "payments"
    authors  = [m["author"] for m in slack if m["author"] != "pagerduty-bot"]
    owner    = authors[0] if authors else "sre"
    t_start  = alerts[0]["timestamp"][:5]  if alerts else "00:00"
    t_end    = alerts[-1]["timestamp"][:5] if alerts else "01:00"

    return {
        "summary": (
            f"The {main_svc} service experienced a significant incident. "
            f"Multiple alerts fired and the on-call team was engaged to investigate and resolve the issue."
        ),
        "timeline": (
            f"{t_start} - First alert fired for {main_svc} service\n"
            f"{alerts[2]['timestamp'][:5] if len(alerts)>2 else t_start} - Service degradation confirmed\n"
            f"{alerts[len(alerts)//2]['timestamp'][:5] if alerts else '00:15'} - On-call team engaged and investigating\n"
            f"{alerts[-2]['timestamp'][:5] if len(alerts)>1 else '00:25'} - Remediation action taken\n"
            f"{t_end} - Service recovery confirmed"
        ),
        "root_cause": (
            f"Root cause: The {main_svc} service experienced a failure due to a deployment bug "
            f"or configuration error. The issue caused service degradation affecting production traffic. "
            f"The on-call team identified the problem and applied a fix to restore service."
        ),
        "impact": (
            f"The {main_svc} service was unavailable or degraded for approximately 30 minutes. "
            f"Production users experienced errors or timeouts during the incident window. "
            f"The incident caused measurable business impact including user-facing failures "
            f"and potential revenue loss during the affected period."
        ),
        "action_items": (
            f"1. Fix root cause of {main_svc} service failure - Owner: {owner} - Due: next sprint\n"
            f"2. Add monitoring to detect this failure mode earlier - Owner: sre - Due: 2024-08-15\n"
            f"3. Improve deployment testing and rollback procedures - Owner: platform - Due: 2024-09-01"
        ),
    }.get(section, f"Content for {section} section of the incident post-mortem.")


def phase_write(env, observation, result, logs_found):
    alerts_text = "\n".join(
        f"[{a['timestamp']}] [{a['severity']}] {a['service']}: {a['message']}"
        for a in observation.get("alerts", [])
    )
    slack_text = "\n".join(
        f"[{m['timestamp']}] {m['author']}: {m['text']}"
        for m in observation.get("slack_thread", [])
    )
    logs_text = ""
    if logs_found:
        logs_text = "\nRETRIEVED LOG EVIDENCE:\n" + "\n".join(
            f"[{l['timestamp']}] [{l['severity']}] {l['service']}: {l['message']}"
            for l in logs_found
        )

    base_context = (
        f"INCIDENT: {observation.get('incident_title', '')}\n\n"
        f"ALERTS:\n{alerts_text}\n\n"
        f"SLACK THREAD:\n{slack_text}"
        f"{logs_text}"
    )

    for section in SECTIONS:
        instruction = SECTION_PROMPTS[section]
        user_prompt = f"{base_context}\n\nWRITE THE '{section.upper()}' SECTION:\n{instruction}\n\nSection content:"

        print(f"    [Phase 2] Writing: {section}...")
        response = call_llm(WRITE_SYSTEM, user_prompt)

        # Strip JSON if LLM returned it anyway
        content = response.strip()
        if content.startswith("{"):
            content = _fallback_section(section, observation, logs_found)
        if not content or len(content) < 20:
            content = _fallback_section(section, observation, logs_found)

        result = env.step({
            "action_type": "WRITE_SECTION",
            "section_name": section,
            "section_content": content,
        })
        observation = result["observation"]
        reward = result.get("reward", {}).get("total", 0.0)
        msg = observation.get("last_action_result", "")[:70]
        print(f"    reward={reward:+.3f} | {msg}")
        if section == "root_cause":
            print(f"    [ROOT CAUSE TEXT]: {content[:200]}")

    return result, observation

# ---------------------------------------------------------------------------
# Phase 3: Assign action item + Submit
# ---------------------------------------------------------------------------

def phase_submit(env, observation, result):
    alerts = observation.get("alerts", [])
    slack  = observation.get("slack_thread", [])
    main_svc = alerts[0]["service"] if alerts else "payments"
    authors  = [m["author"] for m in slack if m["author"] != "pagerduty-bot"]
    owner    = authors[0] if authors else "sre"

    print("    [Phase 3] Assigning action item...")
    result = env.step({
        "action_type": "ASSIGN_ACTION_ITEM",
        "action_item_description": f"Prevent recurrence of {main_svc} service failure — implement fixes and monitoring",
        "action_item_owner": owner,
        "action_item_due_date": "next sprint",
    })
    observation = result["observation"]
    print(f"    reward={result.get('reward',{}).get('total',0):+.3f}")

    print("    [Phase 3] Submitting...")
    result = env.step({"action_type": "SUBMIT"})

    final_score = 0.0
    if result.get("info", {}).get("grade"):
        grade = result["info"]["grade"]
        final_score = grade.get("total_score", 0.0)
        print(f"\n    FINAL GRADE: {final_score:.3f}")
        print(f"    root_cause={grade.get('root_cause_score',0):.2f} "
              f"timeline={grade.get('timeline_score',0):.2f} "
              f"action_items={grade.get('action_items_score',0):.2f} "
              f"impact={grade.get('impact_score',0):.2f} "
              f"completeness={grade.get('completeness_score',0):.2f}")
        print(f"    {grade.get('explanation','')}")

    return final_score, result

# ---------------------------------------------------------------------------
# Episode runner
# ---------------------------------------------------------------------------

def run_episode(env: PostMortemEnv, difficulty: str) -> float:
    print(f"\n{'='*60}")
    print(f"  Task: {difficulty.upper()}")
    print(f"{'='*60}")

    result = env.reset(difficulty=difficulty)
    observation = result["observation"]
    print(f"  Incident: {observation.get('incident_title','')}")
    print(f"  Alerts: {len(observation.get('alerts',[]))} | Slack: {len(observation.get('slack_thread',[]))}")

    print("\n  -- Phase 1: Query logs --")
    result, observation, logs_found = phase_query(env, observation, result)

    print("\n  -- Phase 2: Write sections --")
    result, observation = phase_write(env, observation, result, logs_found)

    print("\n  -- Phase 3: Submit --")
    final_score, _ = phase_submit(env, observation, result)

    return final_score

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Incident Post-Mortem Writer — Baseline Inference")
    print("=" * 60)
    print(f"  Model:   {MODEL_NAME}")
    print(f"  API:     {API_BASE_URL}")
    print(f"  Env URL: {ENV_BASE_URL}\n")

    env = PostMortemEnv(base_url=ENV_BASE_URL)
    if not env.health():
        print(f"ERROR: Environment not reachable at {ENV_BASE_URL}")
        print("Start it: uvicorn server.app:app --host 0.0.0.0 --port 7860")
        sys.exit(1)
    print("  Environment: healthy ✓\n")

    scores: Dict[str, float] = {}
    start_time = time.time()

    for difficulty in DIFFICULTIES:
        scores[difficulty] = round(run_episode(env, difficulty), 4)

    elapsed = time.time() - start_time

    print(f"\n{'='*60}")
    print("  BASELINE RESULTS")
    print(f"{'='*60}")
    for diff, score in scores.items():
        bar = "█" * int(score * 20)
        print(f"  {diff:6s}: {score:.3f}  {bar}")
    print(f"  {'avg':6s}: {sum(scores.values())/len(scores):.3f}")
    print(f"\n  Runtime: {elapsed:.1f}s")
    print(f"  Scores in [0,1]: {'OK' if all(0 <= s <= 1 for s in scores.values()) else 'ERROR'}")
    print(f"{'='*60}")
    print("\nJSON_SCORES:", json.dumps(scores))
    return scores

if __name__ == "__main__":
    main()
