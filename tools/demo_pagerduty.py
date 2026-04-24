"""
Phase 3 Demo — Run Agent on Imported PagerDuty Incident
========================================================
This is the end-to-end flow that demonstrates production readiness:

    1. Take a real PagerDuty incident JSON
    2. Import it to our scenario format (using pagerduty_importer)
    3. Feed it to our LLM agent via the deployed HF Space
    4. Get a structured post-mortem back

For the PITCH: this is your 30-second "real-world proof" moment.

Usage:
    python tools/demo_pagerduty.py samples/pagerduty/incident_payments_outage.json

Required env vars (same as inference.py):
    API_BASE_URL, MODEL_NAME, HF_TOKEN
    (uses http://localhost:7860 by default, or ENV_BASE_URL if set)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

# Reuse importer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pagerduty_importer import import_pagerduty_incident

# Agent imports — reuse the same OpenAI client from inference.py
try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package not installed. Run: pip install openai", file=sys.stderr)
    sys.exit(1)

API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME   = os.environ.get("MODEL_NAME",   "gpt-4o-mini")
HF_TOKEN     = os.environ.get("HF_TOKEN",     "")

client = OpenAI(api_key=HF_TOKEN or "dummy", base_url=API_BASE_URL)


SYSTEM_PROMPT = (
    "You are an expert Site Reliability Engineer writing an incident post-mortem. "
    "You will receive a real incident from PagerDuty (alerts, on-call notes, service info). "
    "Write a structured post-mortem with these 5 sections: "
    "SUMMARY, TIMELINE, ROOT_CAUSE, IMPACT, ACTION_ITEMS. "
    "Be specific — name the affected service, the mechanism (deploy/config/leak/etc), "
    "and concrete numbers where available. Format output as plain text with clear section headers."
)


def build_user_prompt(scenario: Dict[str, Any]) -> str:
    alerts = scenario.get("initial_alerts", [])
    slack  = scenario.get("slack_thread", [])
    services = scenario.get("service_graph_names", [])

    alerts_text = "\n".join(
        f"[{a['timestamp']}] [{a['severity']}] {a['service']}: {a['message']}"
        for a in alerts
    )
    slack_text = "\n".join(
        f"[{m['timestamp']}] {m['author']}: {m['text']}"
        for m in slack
    )

    return (
        f"INCIDENT: {scenario.get('incident_title', '')}\n"
        f"SOURCE:   PagerDuty — {scenario.get('incident_id', '')}\n\n"
        f"ALERTS:\n{alerts_text}\n\n"
        f"ON-CALL NOTES (Slack thread):\n{slack_text}\n\n"
        f"Services involved: {services}\n\n"
        "Write the full post-mortem now."
    )


def run_agent(scenario: Dict[str, Any]) -> str:
    """Call the configured LLM to generate a post-mortem from the scenario."""
    user_prompt = build_user_prompt(scenario)
    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=1500,
    )
    return completion.choices[0].message.content or ""


def main():
    parser = argparse.ArgumentParser(description="Demo: Run agent on PagerDuty incident.")
    parser.add_argument("input", help="Path to PagerDuty incident JSON.")
    parser.add_argument("--output", "-o", default=None, help="Save post-mortem to file.")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("  Phase 3 Demo — PagerDuty → Agent → Post-Mortem")
    print("=" * 60)

    # Step 1: Load and import
    print(f"\n[1/3] Loading PagerDuty JSON: {input_path}")
    with input_path.open("r", encoding="utf-8") as f:
        pd_json = json.load(f)

    print(f"[2/3] Converting to scenario format...")
    scenario = import_pagerduty_incident(pd_json)
    print(f"      incident_id:    {scenario['incident_id']}")
    print(f"      incident_title: {scenario['incident_title']}")
    print(f"      alerts:         {len(scenario['initial_alerts'])}")
    print(f"      slack messages: {len(scenario['slack_thread'])}")

    # Step 2: Run agent
    print(f"\n[3/3] Running agent ({MODEL_NAME})...")
    if not HF_TOKEN:
        print("\nWARNING: HF_TOKEN env var not set. Agent call will fail.")
        print("Set env vars first:")
        print("  set API_BASE_URL=https://api.groq.com/openai/v1")
        print("  set MODEL_NAME=llama-3.1-8b-instant")
        print("  set HF_TOKEN=your-groq-key")
        sys.exit(1)

    try:
        postmortem = run_agent(scenario)
    except Exception as exc:
        print(f"\nERROR: Agent call failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Output
    print("\n" + "=" * 60)
    print("  GENERATED POST-MORTEM")
    print("=" * 60)
    print(postmortem)
    print("=" * 60)

    if args.output:
        Path(args.output).write_text(postmortem, encoding="utf-8")
        print(f"\nSaved to: {args.output}")

    print()
    print("✓ End-to-end demo complete.")
    print("  Real PagerDuty JSON → Importer → Agent → Structured post-mortem.")
    print("  This is the production deployment pattern.")


if __name__ == "__main__":
    main()
