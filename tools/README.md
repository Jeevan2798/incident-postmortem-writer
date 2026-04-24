# Production Integration Tools

This directory contains tools that bridge our Incident Post-Mortem Writer environment to real production systems.

## Overview

Our environment is trained on synthetic scenarios with gold-standard labels. In production, incidents come from real monitoring systems (PagerDuty, Datadog, Splunk) without gold labels — the agent *generates* the ground truth that human engineers then validate.

These tools enable that production workflow.

---

## `pagerduty_importer.py`

Converts real PagerDuty Incident API JSON into our scenario format.

### Usage

```bash
# Print imported scenario to stdout
python tools/pagerduty_importer.py samples/pagerduty/incident_payments_outage.json

# Write to a scenario file
python tools/pagerduty_importer.py \
    samples/pagerduty/incident_payments_outage.json \
    --output env/scenarios/imported_payments.json
```

### What it does

1. Accepts PagerDuty Incident v2 JSON (standard API response or webhook payload)
2. Extracts alerts from `log_entries`, responder notes from `notes` array
3. Normalizes timestamps (ISO-8601 → `HH:MM:SS`)
4. Maps PagerDuty severity/urgency → our severity levels
5. Synthesizes a minimal service graph from services mentioned
6. Outputs a scenario JSON compatible with our environment

### What it does NOT do

- **Does not** synthesize `gold_standard` fields (root_cause, timeline_events, etc)
- **Does not** synthesize `evidence_windows` (we don't know the true root cause yet)

This is intentional. Imported scenarios are for **agent inference only**, not training or grading. The agent's output becomes a draft post-mortem that humans validate and then (optionally) feed back as training data for the next model generation.

---

## `demo_pagerduty.py`

End-to-end demo: PagerDuty JSON → agent → generated post-mortem.

### Usage

```bash
# Set up environment (same as inference.py)
set API_BASE_URL=https://api.groq.com/openai/v1
set MODEL_NAME=llama-3.1-8b-instant
set HF_TOKEN=your-groq-key

# Run the demo
python tools/demo_pagerduty.py samples/pagerduty/incident_payments_outage.json

# Save output to file
python tools/demo_pagerduty.py \
    samples/pagerduty/incident_payments_outage.json \
    --output generated_postmortem.txt
```

### Output

Prints the full generated post-mortem to stdout, structured with:
- SUMMARY
- TIMELINE
- ROOT_CAUSE
- IMPACT
- ACTION_ITEMS

---

## Sample Data

`samples/pagerduty/` contains realistic PagerDuty JSON examples:

| File | Incident | Complexity |
|---|---|---|
| `incident_payments_outage.json` | Payment service DB connection leak after deploy | Easy |
| `incident_redis_ttl.json` | Cascading auth failure from Redis TTL misconfig | Medium |

These are real PagerDuty Incident v2 format, suitable for testing the importer end-to-end.

---

## Production Deployment Pattern

```
┌──────────────────┐      ┌──────────────────────┐      ┌──────────────┐
│  PagerDuty API   │ ───> │  pagerduty_importer  │ ───> │  Scenario    │
│   (webhook or    │      │                      │      │   JSON       │
│    poll)         │      │                      │      │              │
└──────────────────┘      └──────────────────────┘      └──────┬───────┘
                                                               │
                                                               ▼
                                                  ┌─────────────────────┐
                                                  │  OpenEnv Agent      │
                                                  │  (fine-tuned Qwen)  │
                                                  └──────────┬──────────┘
                                                             │
                                                             ▼
                                                   ┌───────────────────┐
                                                   │  Draft Post-     │
                                                   │  Mortem          │
                                                   │  (human-review)  │
                                                   └───────────────────┘
```

**Key insight:** The post-mortem the agent produces goes to a human reviewer, not directly to stakeholders. Validated post-mortems feed the next training cycle, creating a self-improving loop.

---

## Extending to Other Sources

The importer pattern generalizes to any alerting system:

- **Datadog:** Use `monitor.webhook` payloads; alerts in `events` array
- **Splunk:** Use `splunk-webhook` payloads; alerts in `result` array
- **Custom:** Implement your own `import_*()` function following the same contract

Target format for all importers: the scenario JSON schema in `env/scenarios/*.json`.
