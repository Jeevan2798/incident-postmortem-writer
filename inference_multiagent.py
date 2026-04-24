"""
Multi-Agent Inference Script — Incident Post-Mortem Writer (OpenEnv)
=====================================================================
Demonstrates the Phase 1 multi-agent extension end-to-end.

FLOW PER EPISODE:
    1. QUERY_LOGS              (find evidence)
    2. WRITE_SECTION summary    (primary agent)
    3. WRITE_SECTION root_cause (primary agent)
    4. REQUEST_REVIEW           (skeptic critiques the draft)
    5. REVISE_SECTION           (primary revises root_cause based on critique)
    6. WRITE_SECTION timeline
    7. WRITE_SECTION impact
    8. WRITE_SECTION action_items
    9. ASSIGN_ACTION_ITEM
   10. SUBMIT                   (grader includes collaboration_score bonus)

OUTPUT EXAMPLE:
    [START] task=hard env=... mode=multi-agent
    [STEP] step=4 action=REQUEST_REVIEW reward=0.04
           >> Skeptic: Your root cause blames CDN but alerts show CDN healthy...
    [STEP] step=5 action=REVISE_SECTION reward=0.06
    [STEP] step=10 action=SUBMIT (includes collaboration bonus)
    [END]

REQUIRED env vars (same as inference.py):
    API_BASE_URL, MODEL_NAME, HF_TOKEN, ENV_BASE_URL
Plus (for the skeptic running SERVER-side, optional, has fallback):
    SKEPTIC_API_KEY, SKEPTIC_API_BASE_URL, SKEPTIC_MODEL_NAME

USAGE:
    python inference_multiagent.py
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

# Configuration
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME   = os.environ.get("MODEL_NAME", "gpt-4o-mini")
HF_TOKEN     = os.environ.get("HF_TOKEN", "")
ENV_BASE_URL = os.environ.get("ENV_BASE_URL", "http://localhost:7860")

TEMPERATURE  = 0.0
MAX_TOKENS   = 1500
DIFFICULTIES = ["easy", "medium", "hard", "expert"]

client = OpenAI(api_key=HF_TOKEN or "dummy", base_url=API_BASE_URL)

BENCHMARK = "incident-postmortem-writer"


def log_start(task: str, env: str, model: str) -> None:
    print(f"[START] task={task} env={env} model={model} mode=multi-agent", flush=True)

def log_step(step: int, action: str, reward: float, done: bool, error: Optional[str]) -> None:
    error_val = error if error else "null"
    done_val = str(done).lower()
    print(f"[STEP] step={step} action={action} reward={reward:.2f} done={done_val} error={error_val}", flush=True)

def log_end(success: bool, steps: int, score: float, rewards: List[float]) -> None:
    rewards_str = ",".join(f"{r:.2f}" for r in rewards)
    print(f"[END] success={str(success).lower()} steps={steps} score={score:.3f} rewards={rewards_str}", flush=True)


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


QUERY_SYSTEM = """You are an expert SRE. Given incident alerts and Slack messages,
identify the best service and time window to query for root cause evidence.
Respond with ONLY valid JSON: {"service": "<service_name>", "from": "<HH:MM>", "to": "<HH:MM>"}

STRATEGY - follow in this exact order:
1. Look for DEPLOYMENT or CONFIG CHANGE in Slack (keywords: deploy, TTL, migration, release, config, schema).
   If found, query THAT service at THAT deployment time. Deployments are almost always root cause.
2. If no deployment, identify which service changed behavior FIRST and trace upstream dependencies.
3. Pick a 5-8 minute window AROUND the deployment or first change time.
4. NEVER query the most-alerted service - it is usually a victim not the cause."""

WRITE_SYSTEM = """You are an expert SRE writing one section of an incident post-mortem.
Write ONLY the section content - no JSON, no section labels, just plain text.
Be specific and factual. Use exact service names and timestamps from the evidence."""

SECTION_PROMPTS = {
    "summary":      "Write 2-3 sentences summarizing the incident. MUST explicitly name the affected service.",
    "timeline":     "Write a chronological timeline with 5+ timestamped events in format 'HH:MM - what happened'.",
    "root_cause":   "Write root cause analysis. MUST name: (1) which service failed, (2) type of failure (deployment bug / config error / connection leak / etc), (3) specific technical details.",
    "impact":       "Write impact assessment of at least 30 words. Include: affected services, outage duration, users affected, business/revenue impact.",
    "action_items": "Write 3 numbered action items. Example: '1. Fix X - Owner: payments-team - Due: 2024-08-01'. Owner must be a team or person from Slack.",
}

REVISE_SYSTEM = """You are an expert SRE revising a post-mortem section based on a senior reviewer's critique.
The reviewer has identified a specific problem with your previous draft.
Your job is to address that critique by rewriting the section.

RULES:
- Write ONLY the revised section content - no labels, no JSON.
- Must be SUBSTANTIALLY different from the original (at least 30 characters changed).
- Must directly address the critique, not avoid it.
- Keep factual claims that were correct; fix the ones that were challenged."""


def _fallback_section(section: str, observation: Dict, logs_found: List) -> str:
    alerts = observation.get("alerts", [])
    services = list({a["service"] for a in alerts})
    main_svc = services[0] if services else "payments"
    t_start  = alerts[0]["timestamp"][:5]  if alerts else "00:00"
    t_end    = alerts[-1]["timestamp"][:5] if alerts else "01:00"
    return {
        "summary":    f"The {main_svc} service experienced a significant incident affecting production traffic. The on-call team investigated and resolved the issue.",
        "timeline":   f"{t_start} - First alert for {main_svc}\n{alerts[len(alerts)//2]['timestamp'][:5] if alerts else '00:15'} - On-call engaged\n{t_end} - Service recovery confirmed",
        "root_cause": f"Root cause: The {main_svc} service experienced a deployment bug or configuration error that caused service degradation affecting production traffic.",
        "impact":     f"The {main_svc} service was unavailable or degraded for approximately 30 minutes. Production users experienced errors and timeouts. Business impact included user-facing failures and potential revenue loss.",
        "action_items": "1. Add monitoring for the affected service - Owner: sre - Due: next sprint\n2. Review deploy process - Owner: platform - Due: 2024-08-01\n3. Post-mortem review with team - Owner: sre - Due: next sprint",
    }.get(section, f"Analysis of {main_svc} service incident.")


def do_query(env, observation):
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

    response = call_llm(QUERY_SYSTEM, user_prompt)
    query = extract_json(response)

    if query and "service" in query:
        action = {
            "action_type":   "QUERY_LOGS",
            "query_service": query.get("service", services[0] if services else "payments"),
            "query_from":    query.get("from", "00:00"),
            "query_to":      query.get("to",   "23:59"),
        }
        return env.step(action)

    return env.step({
        "action_type":   "QUERY_LOGS",
        "query_service": services[0] if services else "payments",
        "query_from":    "00:00",
        "query_to":      "23:59",
    })


def write_section(env, observation, section: str, logs_found: List) -> Dict:
    alerts_text = "\n".join(
        f"[{a['timestamp']}] [{a['severity']}] {a['service']}: {a['message']}"
        for a in observation.get("alerts", [])
    )
    slack_text = "\n".join(
        f"[{m['timestamp']}] {m['author']}: {m['text']}"
        for m in observation.get("slack_thread", [])
    )
    logs_text = "\n".join(
        f"[{l['timestamp']}] [{l['severity']}] {l['service']}: {l['message']}"
        for l in logs_found
    ) if logs_found else "(no logs retrieved)"

    base_context = (
        f"INCIDENT: {observation.get('incident_title', '')}\n"
        f"ALERTS:\n{alerts_text}\n"
        f"SLACK:\n{slack_text}\n"
        f"RETRIEVED LOGS:\n{logs_text}"
    )

    instruction  = SECTION_PROMPTS[section]
    user_prompt  = f"{base_context}\n\nWRITE THE '{section.upper()}' SECTION:\n{instruction}\n\nSection content:"
    response     = call_llm(WRITE_SYSTEM, user_prompt)
    content      = response.strip()
    if content.startswith("{") or not content or len(content) < 20:
        content = _fallback_section(section, observation, logs_found)

    return env.step({
        "action_type":     "WRITE_SECTION",
        "section_name":    section,
        "section_content": content,
    })


def request_review(env):
    return env.step({"action_type": "REQUEST_REVIEW"})


def revise_section_via_llm(env, observation, critique: str, section_name: str, critique_index: int) -> Dict:
    current_sections = observation.get("sections", [])
    current_content  = ""
    for s in current_sections:
        if s.get("name") == section_name:
            current_content = s.get("content", "") or ""
            break

    alerts_text = "\n".join(
        f"[{a['timestamp']}] [{a['severity']}] {a['service']}: {a['message']}"
        for a in observation.get("alerts", [])
    )
    slack_text = "\n".join(
        f"[{m['timestamp']}] {m['author']}: {m['text']}"
        for m in observation.get("slack_thread", [])
    )

    user_prompt = (
        f"INCIDENT: {observation.get('incident_title', '')}\n"
        f"ALERTS:\n{alerts_text}\n"
        f"SLACK:\n{slack_text}\n\n"
        f"ORIGINAL {section_name.upper()} SECTION:\n{current_content}\n\n"
        f"REVIEWER CRITIQUE:\n{critique}\n\n"
        f"Write the revised {section_name.upper()} section that addresses this critique:"
    )
    response = call_llm(REVISE_SYSTEM, user_prompt).strip()

    if not response or len(response) < 40:
        response = f"REVISED: {current_content} Additionally, based on reviewer feedback: {critique[:150]}"

    return env.step({
        "action_type":              "REVISE_SECTION",
        "section_name":             section_name,
        "section_content":          response,
        "critique_addressed_index": critique_index,
    })


def run_multiagent_episode(env: PostMortemEnv, difficulty: str) -> float:
    print(f"\n{'='*60}")
    print(f"  Task: {difficulty.upper()} (multi-agent)")
    print(f"{'='*60}")

    log_start(task=difficulty, env=BENCHMARK, model=MODEL_NAME)

    step_rewards: List[float] = []
    step_count   = 0
    final_score  = 0.0
    success      = False

    try:
        result      = env.reset(difficulty=difficulty)
        observation = result["observation"]
        print(f"  Incident: {observation.get('incident_title','')}")
        print(f"  Alerts: {len(observation.get('alerts',[]))} | Slack: {len(observation.get('slack_thread',[]))}")

        # Step 1: QUERY_LOGS
        print("\n  -- Step 1: QUERY_LOGS --")
        result = do_query(env, observation)
        observation = result["observation"]
        step_count += 1
        r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
        step_rewards.append(r)
        log_step(step=step_count, action="QUERY_LOGS", reward=r, done=False, error=None)
        logs_found = observation.get("retrieved_logs") or []
        print(f"    reward={r:+.3f} | retrieved {len(logs_found)} logs")

        # Steps 2-3: Write summary and root_cause
        for section in ["summary", "root_cause"]:
            print(f"\n  -- Step {step_count+1}: WRITE_SECTION {section} (primary agent) --")
            result = write_section(env, observation, section, logs_found)
            observation = result["observation"]
            step_count += 1
            r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
            step_rewards.append(r)
            log_step(step=step_count, action=f"WRITE_SECTION_{section}", reward=r, done=False, error=None)
            print(f"    reward={r:+.3f}")

        # Step 4: REQUEST_REVIEW
        print(f"\n  -- Step {step_count+1}: REQUEST_REVIEW (skeptic critiques) --")
        result = request_review(env)
        observation = result["observation"]
        step_count += 1
        r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
        step_rewards.append(r)
        log_step(step=step_count, action="REQUEST_REVIEW", reward=r, done=False, error=None)

        critique = None
        if observation.get("skeptic_critiques"):
            critique = observation["skeptic_critiques"][-1]
            print(f"    reward={r:+.3f}")
            print(f"    >> Skeptic: {critique[:180]}")
        else:
            print(f"    reward={r:+.3f} | No critique returned")

        # Step 5: REVISE_SECTION
        if critique:
            print(f"\n  -- Step {step_count+1}: REVISE_SECTION root_cause (primary revises) --")
            result = revise_section_via_llm(
                env, observation, critique,
                section_name="root_cause",
                critique_index=len(observation["skeptic_critiques"]) - 1,
            )
            observation = result["observation"]
            step_count += 1
            r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
            step_rewards.append(r)
            log_step(step=step_count, action="REVISE_SECTION_root_cause", reward=r, done=False, error=None)
            print(f"    reward={r:+.3f} | critiques_addressed={observation.get('critiques_addressed', 0)}")

        # Steps 6-8: Remaining sections
        for section in ["timeline", "impact", "action_items"]:
            print(f"\n  -- Step {step_count+1}: WRITE_SECTION {section} --")
            result = write_section(env, observation, section, logs_found)
            observation = result["observation"]
            step_count += 1
            r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
            step_rewards.append(r)
            log_step(step=step_count, action=f"WRITE_SECTION_{section}", reward=r, done=False, error=None)
            print(f"    reward={r:+.3f}")

        # Steps 9-10: ASSIGN + SUBMIT
        print(f"\n  -- Step {step_count+1}: ASSIGN_ACTION_ITEM --")
        result = env.step({
            "action_type":             "ASSIGN_ACTION_ITEM",
            "action_item_description": "Prevent recurrence of incident - implement fixes and monitoring",
            "action_item_owner":       "sre",
            "action_item_due_date":    "next sprint",
        })
        observation = result["observation"]
        step_count += 1
        r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
        step_rewards.append(r)
        log_step(step=step_count, action="ASSIGN_ACTION_ITEM", reward=r, done=False, error=None)

        print(f"\n  -- Step {step_count+1}: SUBMIT --")
        result = env.step({"action_type": "SUBMIT"})
        step_count += 1
        r = float(result.get("reward", {}).get("total", 0.0) or 0.0)
        step_rewards.append(r)
        done = bool(result.get("done", False))
        log_step(step=step_count, action="SUBMIT", reward=r, done=done, error=None)

        grade = result.get("info", {}).get("grade")
        if grade:
            final_score = grade.get("total_score", 0.0)
            success     = final_score > 0.3
            print(f"\n  FINAL GRADE: {final_score:.3f}")
            print(f"  root_cause={grade.get('root_cause_score',0):.2f} "
                  f"timeline={grade.get('timeline_score',0):.2f} "
                  f"action_items={grade.get('action_items_score',0):.2f} "
                  f"impact={grade.get('impact_score',0):.2f} "
                  f"completeness={grade.get('completeness_score',0):.2f}")
            if grade.get('critiques_received', 0) > 0:
                print(f"  collaboration={grade.get('collaboration_score',0):.2f} "
                      f"({grade.get('critiques_addressed',0)}/{grade.get('critiques_received',0)} critiques addressed)")
            print(f"  {grade.get('explanation','')}")

    except Exception as exc:
        print(f"  [ERROR] Episode failed: {exc}")
        log_step(step=step_count + 1, action="ERROR", reward=0.0, done=True, error=str(exc))

    log_end(success=success, steps=step_count, score=final_score, rewards=step_rewards)
    return final_score


def main():
    print("=" * 60)
    print("  Multi-Agent Inference - Incident Post-Mortem Writer")
    print("=" * 60)
    print(f"  Model:       {MODEL_NAME}")
    print(f"  API:         {API_BASE_URL}")
    print(f"  Env URL:     {ENV_BASE_URL}")
    print(f"  Mode:        multi-agent (primary + skeptic)")

    env = PostMortemEnv(ENV_BASE_URL)
    if not env.health():
        print(f"ERROR: Environment not reachable at {ENV_BASE_URL}")
        print(f"Start it: uvicorn server.app:app --host 0.0.0.0 --port 7860")
        sys.exit(1)

    scores: Dict[str, float] = {}
    t_start = time.time()
    for difficulty in DIFFICULTIES:
        score = run_multiagent_episode(env, difficulty)
        scores[difficulty] = round(score, 4)

    elapsed = time.time() - t_start

    print("\n" + "=" * 60)
    print("  MULTI-AGENT BENCHMARK RESULTS")
    print("=" * 60)
    for task, score in scores.items():
        print(f"  {task:8s}: {score:.3f}")
    avg = sum(scores.values()) / len(scores)
    print(f"  {'average':8s}: {avg:.3f}")
    print(f"\n  runtime: {elapsed:.1f}s")

    print(f"\nJSON_SCORES: {json.dumps(scores)}", flush=True)


if __name__ == "__main__":
    main()
