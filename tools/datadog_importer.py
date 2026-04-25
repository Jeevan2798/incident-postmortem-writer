"""
Datadog Importer — Production-Ready Integration
=================================================
Converts Datadog monitor webhook payloads into Incident Post-Mortem Writer
scenario format, so the trained agent can analyze Datadog alerts.

USAGE:
    python tools/datadog_importer.py samples/datadog/incident_payments_5xx.json
    python tools/datadog_importer.py <path-to-json> --output env/scenarios/imported.json

SUPPORTED DATADOG FORMATS:
    - Monitor webhook payloads (notifications)
    - Events API responses
    - Composite monitor alerts

DESIGN PHILOSOPHY:
    Datadog gives us alerts + tags + monitor metadata. It does NOT give us
    gold-standard root cause (that's what we want the agent to produce).
    So the importer synthesizes a MINIMAL scenario suitable for inference,
    not training-grade with evidence_windows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _parse_timestamp(ts_str: str | int | float) -> str:
    """Normalize Datadog timestamps (ISO-8601 or unix epoch) to HH:MM:SS."""
    if not ts_str:
        return "00:00:00"
    # Unix epoch (seconds or milliseconds)
    if isinstance(ts_str, (int, float)):
        try:
            ts = ts_str / 1000 if ts_str > 1e12 else ts_str
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except (ValueError, OSError):
            return "00:00:00"
    # ISO 8601
    s = str(ts_str).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).strftime("%H:%M:%S")
    except ValueError:
        m = re.search(r"(\d{2}:\d{2}:\d{2})", s)
        return m.group(1) if m else "00:00:00"


def _severity_from_dd(dd_priority: str | int) -> str:
    """Map Datadog priority/alert_type -> our severity levels."""
    if not dd_priority:
        return "INFO"
    if isinstance(dd_priority, int):
        # Datadog priority: 1 (high) → 5 (low)
        return "CRITICAL" if dd_priority <= 1 else "ERROR" if dd_priority == 2 else "WARN" if dd_priority == 3 else "INFO"
    p = str(dd_priority).upper()
    mapping = {
        "P1": "CRITICAL", "P2": "ERROR", "P3": "WARN", "P4": "INFO", "P5": "INFO",
        "ERROR": "ERROR", "WARNING": "WARN", "WARN": "WARN", "INFO": "INFO",
        "ALERT": "CRITICAL", "CRITICAL": "CRITICAL", "HIGH": "CRITICAL",
        "MEDIUM": "WARN", "LOW": "INFO",
    }
    return mapping.get(p, "INFO")


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract the affected service from Datadog tags or monitor metadata."""
    # Datadog stores service in tags as 'service:name'
    tags = payload.get("tags") or payload.get("monitor", {}).get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("service:"):
            return tag.split(":", 1)[1]
    # Fall back to monitor name parsing
    name = payload.get("monitor", {}).get("name") or payload.get("alert_title", "")
    m = re.search(r'service[:\s=]+([a-z0-9\-_]+)', str(name), re.IGNORECASE)
    if m:
        return m.group(1)
    return "unknown-service"


# ─────────────────────────────────────────────────────────────────
# Main importer
# ─────────────────────────────────────────────────────────────────

def import_datadog_incident(dd_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a Datadog monitor webhook payload into our scenario format.

    Input: Dict matching Datadog monitor webhook schema.
    Output: Dict compatible with env/scenarios/*.json schema (inference only).
    """
    # Datadog wraps payload in different shapes; normalize
    monitor    = dd_payload.get("monitor", dd_payload)
    alert      = dd_payload.get("alert", dd_payload)
    incident_id = (dd_payload.get("id")
                   or dd_payload.get("alert_id")
                   or monitor.get("id")
                   or "DD-UNKNOWN")
    incident_title = (dd_payload.get("alert_title")
                      or dd_payload.get("title")
                      or monitor.get("name")
                      or "Datadog Incident")
    service_name = _extract_service(dd_payload)

    # ─ Build alerts from monitor evaluation history ─
    alerts: List[Dict[str, Any]] = []

    # Primary alert from the trigger event
    alerts.append({
        "timestamp": _parse_timestamp(dd_payload.get("date") or dd_payload.get("timestamp") or alert.get("triggered_at")),
        "service":   service_name,
        "severity":  _severity_from_dd(dd_payload.get("priority") or alert.get("alert_type") or "P2"),
        "message":   incident_title,
    })

    # Additional alerts from related events (if present)
    for event in dd_payload.get("related_events", []) or []:
        alerts.append({
            "timestamp": _parse_timestamp(event.get("date") or event.get("timestamp")),
            "service":   event.get("source", {}).get("service", service_name) if isinstance(event.get("source"), dict) else service_name,
            "severity":  _severity_from_dd(event.get("priority", "P3")),
            "message":   event.get("text") or event.get("title", "Related event"),
        })

    # ─ Build Slack-equivalent thread from comments + monitor messages ─
    slack_thread: List[Dict[str, Any]] = []
    slack_thread.append({
        "timestamp": _parse_timestamp(dd_payload.get("date") or dd_payload.get("timestamp")),
        "author":    "datadog-bot",
        "text":      f"🚨 Monitor triggered: {service_name} — {incident_title}",
    })
    for comment in dd_payload.get("comments", []) or []:
        slack_thread.append({
            "timestamp": _parse_timestamp(comment.get("timestamp")),
            "author":    comment.get("user", {}).get("handle", "oncall") if isinstance(comment.get("user"), dict) else str(comment.get("user", "oncall")),
            "text":      comment.get("message") or "",
        })

    # ─ Service graph from tags ─
    known_services = {service_name}
    tags = dd_payload.get("tags") or monitor.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("service:"):
            known_services.add(tag.split(":", 1)[1])
    service_graph = [{"service": s, "depends_on": []} for s in known_services]

    # ─ Build scenario (INFERENCE ONLY) ─
    scenario = {
        "scenario_id":       f"datadog-{incident_id}",
        "difficulty":        "easy",  # imported scenarios run as easy difficulty
        "incident_id":       f"DD-{incident_id}",
        "incident_title":    incident_title,
        "goal":              f"Write a post-mortem for Datadog incident {incident_id}.",
        "initial_alerts":    alerts,
        "slack_thread":      slack_thread,
        "service_graph":     service_graph,
        "relevant_services": list(known_services),
        "evidence_windows":  [],
        "query_limits":      {"max_queries": 8, "penalty_schedule": [0.05] * 8},
        "noise_logs":        [],
        "metadata": {
            "source":          "datadog",
            "incident_id":     incident_id,
            "dd_priority":     dd_payload.get("priority", "unknown"),
            "dd_alert_type":   dd_payload.get("alert_type", "unknown"),
            "dd_org":          dd_payload.get("org", {}).get("name") if isinstance(dd_payload.get("org"), dict) else "unknown",
            "note":            "INFERENCE ONLY — no gold_standard, agent output not graded.",
        },
        "service_graph_names": list(known_services),
    }
    return scenario


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import a Datadog incident JSON and convert to scenario format.")
    parser.add_argument("input", help="Path to Datadog incident JSON file.")
    parser.add_argument("--output", "-o", default=None, help="Output scenario JSON path (default: stdout).")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with input_path.open("r", encoding="utf-8") as f:
        dd_payload = json.load(f)

    scenario = import_datadog_incident(dd_payload)
    out_json = json.dumps(scenario, indent=2, ensure_ascii=False)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_json, encoding="utf-8")
        print(f"Wrote scenario to: {out_path}")
        print(f"  incident_id:    {scenario['incident_id']}")
        print(f"  incident_title: {scenario['incident_title']}")
        print(f"  alerts:         {len(scenario['initial_alerts'])}")
        print(f"  slack_messages: {len(scenario['slack_thread'])}")
    else:
        print(out_json)


if __name__ == "__main__":
    main()
