"""
PagerDuty Importer — Production-Ready Integration
===================================================
Converts real PagerDuty incident JSON into Incident Post-Mortem Writer
scenario format, so the trained agent can analyze live production incidents.

This is the bridge from simulated benchmarks to real-world deployment.

USAGE:
    python tools/pagerduty_importer.py samples/pagerduty/incident_12345.json
    python tools/pagerduty_importer.py <path-to-json> --output env/scenarios/imported.json
    python tools/pagerduty_importer.py <path-to-json> --run-agent

SUPPORTED PAGERDUTY FORMATS:
    - Standard PagerDuty Incident API v2 (2017+)
    - Enhanced format with log_entries + custom_details
    - Webhook payload format (partial support)

DESIGN PHILOSOPHY:
    PagerDuty gives us alerts + metadata + log entries. It does NOT give us
    gold-standard root cause or timeline events (those are what we want the
    agent to produce). So the importer synthesizes a MINIMAL scenario
    suitable for agent inference — not training-ready (no evidence_windows
    since we don't know the true root cause yet).

    This is the correct production pattern: agent generates post-mortem,
    human engineer validates, then validated post-mortems become training
    data for the next model generation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────
# Data extraction helpers
# ─────────────────────────────────────────────────────────────────

def _parse_timestamp(ts_str: str) -> str:
    """Normalize any ISO-8601 or PagerDuty timestamp to HH:MM:SS format."""
    if not ts_str:
        return "00:00:00"
    # Strip timezone suffix
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_str)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        # Fall back: try to extract HH:MM:SS from the string
        m = re.search(r"(\d{2}:\d{2}:\d{2})", ts_str)
        return m.group(1) if m else "00:00:00"


def _severity_from_pd(pd_severity: str) -> str:
    """Map PagerDuty urgency/severity -> our severity levels."""
    if not pd_severity:
        return "INFO"
    s = pd_severity.upper()
    if s in ("CRITICAL", "HIGH"):
        return "CRITICAL"
    if s in ("WARNING", "WARN", "MEDIUM"):
        return "WARN"
    if s in ("ERROR", "MAJOR"):
        return "ERROR"
    if s == "LOW":
        return "INFO"
    return s if s in ("CRITICAL", "WARN", "ERROR", "INFO") else "INFO"


def _extract_service(pd_incident: Dict[str, Any]) -> str:
    """Extract the affected service name from a PagerDuty incident."""
    # PagerDuty stores service under 'service' key with nested summary
    svc = pd_incident.get("service", {})
    if isinstance(svc, dict):
        return svc.get("summary") or svc.get("name") or svc.get("id") or "unknown-service"
    if isinstance(svc, str):
        return svc
    # Fall back: look at incident body for service mentions
    return "unknown-service"


# ─────────────────────────────────────────────────────────────────
# Main importer
# ─────────────────────────────────────────────────────────────────

def import_pagerduty_incident(pd_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a PagerDuty incident JSON into our scenario format.

    Input: Dict matching PagerDuty Incident API v2 response.
    Output: Dict compatible with env/scenarios/*.json schema (for inference only).
    """
    # Handle both single-incident and list-wrapped formats
    incident = pd_json.get("incident") or pd_json
    if isinstance(incident, list):
        incident = incident[0]

    # ─ Basic metadata ─
    incident_id    = incident.get("id") or incident.get("incident_number") or "PD-UNKNOWN"
    incident_title = incident.get("title") or incident.get("summary") or "Untitled Incident"
    service_name   = _extract_service(incident)

    # ─ Parse alerts from log_entries or alerts ─
    alerts: List[Dict[str, Any]] = []

    # PagerDuty stores per-event records in "log_entries"
    for entry in incident.get("log_entries", []):
        entry_type = entry.get("type", "")
        if "alert" not in entry_type and "trigger" not in entry_type:
            continue
        alerts.append({
            "timestamp": _parse_timestamp(entry.get("created_at", "")),
            "service":   service_name,
            "severity":  _severity_from_pd(entry.get("severity") or incident.get("urgency", "")),
            "message":   entry.get("summary") or entry.get("description") or "Alert fired",
        })

    # If no log_entries, try "alerts" array (webhook payload format)
    if not alerts:
        for alert in incident.get("alerts", []):
            body = alert.get("body", {})
            cef  = body.get("cef_details", {}) if isinstance(body, dict) else {}
            alerts.append({
                "timestamp": _parse_timestamp(alert.get("created_at", "")),
                "service":   cef.get("source_origin") or service_name,
                "severity":  _severity_from_pd(alert.get("severity", "")),
                "message":   alert.get("summary") or body.get("message", "Alert"),
            })

    # If still no alerts, synthesize one from the incident itself
    if not alerts:
        alerts = [{
            "timestamp": _parse_timestamp(incident.get("created_at", "")),
            "service":   service_name,
            "severity":  _severity_from_pd(incident.get("urgency", "HIGH")),
            "message":   incident_title,
        }]

    # ─ Parse Slack-like thread from notes/responders ─
    slack_thread: List[Dict[str, Any]] = []
    for note in incident.get("notes", []) or []:
        user = note.get("user", {})
        author = user.get("summary") if isinstance(user, dict) else str(user) or "oncall"
        slack_thread.append({
            "timestamp": _parse_timestamp(note.get("created_at", "")),
            "author":    author,
            "text":      note.get("content") or "",
        })

    # Always prepend a pagerduty-bot message as the opener
    slack_thread.insert(0, {
        "timestamp": _parse_timestamp(incident.get("created_at", "")),
        "author":    "pagerduty-bot",
        "text":      f"🚨 ALERT: {service_name} incident — {incident_title}",
    })

    # ─ Synthesize a minimal service graph ─
    # We don't know full topology from PagerDuty, so include just the affected service
    # plus any services mentioned in log_entries
    known_services = {service_name}
    for alert in alerts:
        known_services.add(alert["service"])
    service_graph = [
        {"service": s, "depends_on": []} for s in known_services
    ]

    # ─ Build scenario — INFERENCE ONLY, no gold_standard ─
    # This distinguishes imported scenarios from training scenarios.
    scenario = {
        "scenario_id":     f"pagerduty-{incident_id}",
        "difficulty":      "easy",   # signals "this is a real incident"
        "incident_id":     f"PD-{incident_id}",
        "incident_title":  incident_title,
        "goal":            f"Write a post-mortem for PagerDuty incident {incident_id}.",
        "initial_alerts":  alerts,
        "slack_thread":    slack_thread,
        "service_graph":   service_graph,
        "relevant_services": list(known_services),   # trust PagerDuty data
        "evidence_windows":  [],   # none — agent works with alerts + thread only
        "query_limits":      {"max_queries": 8, "penalty_schedule": [0.05]*8},
        "noise_logs":        [],
        "metadata": {
            "source":         "pagerduty",
            "incident_id":    incident_id,
            "pd_status":      incident.get("status", "unknown"),
            "pd_urgency":     incident.get("urgency", "unknown"),
            "pd_created_at":  incident.get("created_at", ""),
            "pd_resolved_at": incident.get("resolved_at", ""),
            "note":           "INFERENCE ONLY — no gold_standard, agent output not graded.",
        },
        "service_graph_names": list(known_services),
    }

    return scenario


# ─────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import a PagerDuty incident JSON and convert to scenario format.",
    )
    parser.add_argument(
        "input",
        help="Path to PagerDuty incident JSON file.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output path for the scenario JSON. Default: prints to stdout.",
    )
    parser.add_argument(
        "--run-agent",
        action="store_true",
        help="After import, run inference.py against the imported scenario.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with input_path.open("r", encoding="utf-8") as f:
        pd_json = json.load(f)

    scenario = import_pagerduty_incident(pd_json)

    # Output
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
        print(f"  services:       {len(scenario['service_graph'])}")
    else:
        print(out_json)

    # Optionally run agent
    if args.run_agent:
        print("\n" + "="*60)
        print("Note: --run-agent flag requires inference.py to be extended")
        print("  to accept custom scenario files. See tools/README.md.")
        print("="*60)


if __name__ == "__main__":
    main()
