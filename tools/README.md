# Production Integration Tools

This directory contains tools that bridge the Incident Post-Mortem Writer environment to real production incident management systems.

## Overview

The environment is trained on synthetic scenarios with gold-standard labels. In production, incidents come from real monitoring systems (PagerDuty, Datadog, Splunk) without gold labels — the agent generates the post-mortem that human engineers then validate.

These tools enable that workflow across multiple incident sources.

---

## Supported Sources

| Source | Importer | Sample |
|---|---|---|
| **PagerDuty** (Incident API v2) | `tools/pagerduty_importer.py` | `samples/pagerduty/` |
| **Datadog** (Monitor webhook) | `tools/datadog_importer.py` | `samples/datadog/` |
| **Splunk** (Alert action / notable event) | `tools/splunk_importer.py` | `samples/splunk/` |

Each importer follows the same contract:

```bash
python tools/<source>_importer.py <input.json>                    # print scenario to stdout
python tools/<source>_importer.py <input.json> --output <out>     # save to file
```

---

## PagerDuty Importer

```bash
# Print imported scenario
python tools/pagerduty_importer.py samples/pagerduty/incident_payments_outage.json

# Save to scenario file
python tools/pagerduty_importer.py \
    samples/pagerduty/incident_payments_outage.json \
    --output env/scenarios/imported_payments.json
```

Extracts: alerts from `log_entries`, on-call notes from `notes`, services from `service.summary`. Maps PagerDuty urgency/severity to our severity levels.

**Sample data:**
- `samples/pagerduty/incident_payments_outage.json` — Payments DB connection leak (post-deploy)
- `samples/pagerduty/incident_redis_ttl.json` — Cascading auth failure from Redis TTL change

---

## Datadog Importer

```bash
python tools/datadog_importer.py samples/datadog/incident_payments_5xx.json
```

Extracts: alert from monitor trigger event, additional alerts from `related_events`, service from tags (`service:name`), comments from monitor.

**Sample data:**
- `samples/datadog/incident_payments_5xx.json` — 5xx spike with related events + comments

Maps Datadog priority (P1-P5) to our severity levels.

---

## Splunk Importer

```bash
python tools/splunk_importer.py samples/splunk/incident_checkout_cascade.json
```

Extracts: trigger from saved search metadata, additional alerts from `results` array, service from sourcetype/host, comments from `notes` or `comments`.

**Sample data:**
- `samples/splunk/incident_checkout_cascade.json` — Cascading checkout failure from Redis TTL change

Maps Splunk severity (numeric 1-5 or string) to our levels.

---

## End-to-End Demo

`tools/demo_pagerduty.py` runs the full PagerDuty pipeline:

```bash
# Set env vars (use Groq for the LLM)
export API_BASE_URL=https://api.groq.com/openai/v1
export MODEL_NAME=llama-3.1-8b-instant
export HF_TOKEN=your-groq-key

# Run end-to-end
python tools/demo_pagerduty.py samples/pagerduty/incident_payments_outage.json
```

The same pattern works for Datadog and Splunk — just swap the importer in the demo script. (Demo for those two is left as an exercise to keep the code minimal — it's a 5-line change.)

---

## Production Deployment Pattern

```
┌──────────────────┐      ┌──────────────────────┐      ┌──────────────┐
│  Incident System │ ───> │  Importer            │ ───> │  Scenario    │
│  Webhook         │      │  (pagerduty/datadog/ │      │  JSON        │
│                  │      │   splunk)            │      │              │
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

**Key insight:** the post-mortem the agent produces goes to a human reviewer first. Validated post-mortems then feed the next training cycle. A self-improving loop, no synthetic data needed once the system is running on real incidents.

---

## What These Importers Do NOT Do

By design, none of the importers synthesize:

- ❌ `gold_standard` fields (root_cause_truth, timeline_events, etc.)
- ❌ `evidence_windows` (we don't know the true root cause yet — that's what we want the agent to discover)

Imported scenarios are for **agent inference only**, not training or grading. This is intentional and matches the production deployment pattern: the agent's output is a draft for human review, not a graded benchmark answer.

---

## Adding More Importers

The pattern generalizes to any alerting system:

```python
# tools/your_source_importer.py
def import_your_source_incident(payload: Dict) -> Dict:
    # 1. Normalize timestamps to HH:MM:SS
    # 2. Map your source's severity levels to: CRITICAL / ERROR / WARN / INFO
    # 3. Extract service name from your source's metadata
    # 4. Build alerts list from your event/log structure
    # 5. Build slack_thread from comments/notes
    # 6. Synthesize service_graph from mentioned services
    # 7. Return scenario dict matching env/scenarios/*.json schema
    ...
```

Target output schema: see any file in `env/scenarios/*.json` or check the existing importers as references.

Examples to add next: Opsgenie, VictorOps, ServiceNow ITSM, Sentry.
