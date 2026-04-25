"""
Splunk Importer — Production-Ready Integration
================================================
Converts Splunk webhook payloads (alert actions) and search results into
Incident Post-Mortem Writer scenario format.

USAGE:
    python tools/splunk_importer.py samples/splunk/incident_db_outage.json
    python tools/splunk_importer.py <path-to-json> --output env/scenarios/imported.json

SUPPORTED SPLUNK FORMATS:
    - Webhook alert actions (alert_actions.conf webhook)
    - Saved search results (notable events)
    - Splunk Enterprise Security incidents

DESIGN PHILOSOPHY:
    Splunk gives us raw search results + saved search metadata + sourcetype
    information. It does NOT give us gold-standard root cause. The importer
    builds a minimal scenario suitable for inference, not training.
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

def _parse_timestamp(ts: Any) -> str:
    """Normalize Splunk timestamps to HH:MM:SS."""
    if ts is None or ts == "":
        return "00:00:00"
    # Unix epoch
    if isinstance(ts, (int, float)):
        try:
            t = ts / 1000 if ts > 1e12 else ts
            return datetime.fromtimestamp(t).strftime("%H:%M:%S")
        except (ValueError, OSError):
            return "00:00:00"
    s = str(ts).replace("Z", "+00:00")
    # Splunk timestamp format: 2025-10-14T03:41:00.000+00:00
    try:
        return datetime.fromisoformat(s).strftime("%H:%M:%S")
    except ValueError:
        # Try other Splunk formats
        for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).strftime("%H:%M:%S")
            except ValueError:
                pass
    m = re.search(r"(\d{2}:\d{2}:\d{2})", s)
    return m.group(1) if m else "00:00:00"


def _severity_from_splunk(splunk_severity: Any) -> str:
    """Map Splunk severity (numeric or string) -> our levels."""
    if splunk_severity is None:
        return "INFO"
    # Splunk numeric: 1 (informational) to 5 (critical)
    if isinstance(splunk_severity, (int, float)):
        s = int(splunk_severity)
        if s >= 5: return "CRITICAL"
        if s == 4: return "ERROR"
        if s == 3: return "WARN"
        return "INFO"
    p = str(splunk_severity).upper()
    mapping = {
        "CRITICAL": "CRITICAL", "HIGH": "CRITICAL", "5": "CRITICAL",
        "ERROR": "ERROR", "MEDIUM": "ERROR", "4": "ERROR",
        "WARN": "WARN", "WARNING": "WARN", "LOW": "WARN", "3": "WARN",
        "INFO": "INFO", "INFORMATIONAL": "INFO", "DEBUG": "INFO",
        "1": "INFO", "2": "INFO",
    }
    return mapping.get(p, "INFO")


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract service from Splunk fields, sourcetype, or saved search name."""
    # Check common Splunk fields where service info lives
    for field in ("service", "sourcetype", "host", "source"):
        val = payload.get(field) or payload.get("result", {}).get(field)
        if isinstance(val, str) and val and val != "*":
            # Strip common prefixes from sourcetype
            cleaned = re.sub(r"^(json:|kvstore:|access_combined_)", "", val)
            return cleaned.split(":")[0].split("/")[-1]
    # Try saved search name
    name = (payload.get("search_name")
            or payload.get("savedsearch_name")
            or payload.get("alert_name", ""))
    m = re.search(r"([a-z0-9\-_]+)[-_](?:alert|outage|down|errors?|spike)", str(name), re.IGNORECASE)
    if m:
        return m.group(1)
    return "unknown-service"


# ─────────────────────────────────────────────────────────────────
# Main importer
# ─────────────────────────────────────────────────────────────────

def import_splunk_incident(splunk_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a Splunk alert webhook payload into our scenario format.

    Input: Dict matching Splunk webhook alert_actions schema or notable event.
    Output: Dict compatible with env/scenarios/*.json schema (inference only).
    """
    incident_id = (splunk_payload.get("sid")
                   or splunk_payload.get("notable_id")
                   or splunk_payload.get("alert_id")
                   or "SPLUNK-UNKNOWN")
    incident_title = (splunk_payload.get("search_name")
                      or splunk_payload.get("alert_name")
                      or splunk_payload.get("title")
                      or splunk_payload.get("savedsearch_name")
                      or "Splunk Alert")
    service_name = _extract_service(splunk_payload)

    # ─ Build alerts from search results ─
    alerts: List[Dict[str, Any]] = []

    # Primary trigger alert
    alerts.append({
        "timestamp": _parse_timestamp(splunk_payload.get("trigger_time")
                                      or splunk_payload.get("_time")
                                      or splunk_payload.get("timestamp")),
        "service":   service_name,
        "severity":  _severity_from_splunk(splunk_payload.get("severity")
                                           or splunk_payload.get("urgency", "high")),
        "message":   incident_title,
    })

    # Search result rows become additional alerts
    results = (splunk_payload.get("result")
               or splunk_payload.get("results")
               or splunk_payload.get("events", []))
    if isinstance(results, dict):
        results = [results]
    for result in (results or [])[:10]:  # Cap at 10 to avoid noise
        if not isinstance(result, dict):
            continue
        alerts.append({
            "timestamp": _parse_timestamp(result.get("_time") or result.get("timestamp")),
            "service":   result.get("service") or result.get("sourcetype", service_name),
            "severity":  _severity_from_splunk(result.get("severity", 3)),
            "message":   (result.get("_raw") or result.get("message")
                          or result.get("description", "Splunk result"))[:200],
        })

    # ─ Build Slack-equivalent thread ─
    slack_thread: List[Dict[str, Any]] = []
    slack_thread.append({
        "timestamp": _parse_timestamp(splunk_payload.get("trigger_time") or splunk_payload.get("_time")),
        "author":    "splunk-bot",
        "text":      f"🚨 Search alert fired: {incident_title} (service: {service_name})",
    })
    for comment in splunk_payload.get("comments", []) or splunk_payload.get("notes", []) or []:
        slack_thread.append({
            "timestamp": _parse_timestamp(comment.get("timestamp") or comment.get("created")),
            "author":    comment.get("user") or comment.get("author", "oncall"),
            "text":      comment.get("text") or comment.get("comment") or comment.get("body", ""),
        })

    # ─ Service graph ─
    known_services = {service_name}
    for r in (results or []):
        if isinstance(r, dict):
            svc = r.get("service") or r.get("sourcetype")
            if isinstance(svc, str):
                known_services.add(svc.split(":")[0].split("/")[-1])
    service_graph = [{"service": s, "depends_on": []} for s in known_services]

    # ─ Build scenario ─
    scenario = {
        "scenario_id":       f"splunk-{incident_id}",
        "difficulty":        "easy",
        "incident_id":       f"SP-{incident_id}",
        "incident_title":    incident_title,
        "goal":              f"Write a post-mortem for Splunk incident {incident_id}.",
        "initial_alerts":    alerts,
        "slack_thread":      slack_thread,
        "service_graph":     service_graph,
        "relevant_services": list(known_services),
        "evidence_windows":  [],
        "query_limits":      {"max_queries": 8, "penalty_schedule": [0.05] * 8},
        "noise_logs":        [],
        "metadata": {
            "source":           "splunk",
            "incident_id":      incident_id,
            "splunk_app":       splunk_payload.get("app", "unknown"),
            "splunk_owner":     splunk_payload.get("owner", "unknown"),
            "search_name":      splunk_payload.get("search_name", ""),
            "note":             "INFERENCE ONLY — no gold_standard, agent output not graded.",
        },
        "service_graph_names": list(known_services),
    }
    return scenario


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import a Splunk incident JSON and convert to scenario format.")
    parser.add_argument("input", help="Path to Splunk webhook/event JSON file.")
    parser.add_argument("--output", "-o", default=None, help="Output scenario JSON path (default: stdout).")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with input_path.open("r", encoding="utf-8") as f:
        splunk_payload = json.load(f)

    scenario = import_splunk_incident(splunk_payload)
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
