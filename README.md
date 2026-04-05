---
title: Incident Post-Mortem Writer
emoji: 🚨
colorFrom: red
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
tags:
  - openenv
  - rl
  - environment
  - sre
  - nlp
---

# Incident Post-Mortem Writer

An [OpenEnv](https://github.com/meta-pytorch/OpenEnv) environment where AI agents learn to write structured incident post-mortems from raw alert logs, Slack threads, and service dependency graphs.

## Why This Environment

Every SRE team writes post-mortems after incidents. It's a high-stakes, time-pressured task that requires:
- Reconstructing a timeline from noisy, incomplete logs
- Identifying root cause despite misleading signals and red herrings
- Assigning concrete action items with owners and deadlines

This environment trains and evaluates agents on exactly this workflow — one of the most practically valuable skills in modern software operations.

---

## Key Innovations

This environment goes beyond standard task simulation by introducing:

**Evidence gating via QUERY_LOGS** — critical root cause evidence is hidden behind precise service + time window queries. Incorrect queries return realistic noise logs, forcing intentional investigation rather than guessing.

**Adversarial Slack signals** — threads include senior engineers confidently blaming the wrong service, misleading correlations between symptoms and causes, and red herrings designed to trap pattern-matching agents.

**Delayed and partial observability** — the agent never sees full logs upfront. It must actively explore under a hard query budget (max 8 queries with escalating penalties), simulating real incident response under time pressure.

**Multi-layer deterministic grading** — root cause is evaluated across service identification, cause category classification, and semantic keyword validation. Not string matching. A correct answer written in different words still scores correctly.

These design choices simulate real-world incident response, where incomplete information, misleading signals, and time pressure are the norm — and where the difference between a good engineer and a great one is knowing where to look.

---

## Why This Is Challenging for Agents

This environment is difficult for AI agents because:

**Hidden evidence** — critical logs are not visible upfront and must be discovered through precise `QUERY_LOGS` calls targeting the right service and time window.

**Conflicting information** — Slack threads contain confident but incorrect hypotheses from senior engineers, forcing the agent to trust data over authority.

**Limited exploration budget** — queries are constrained (max 8) with escalating penalties, preventing brute-force search strategies.

**Structured output requirement** — the agent must not only reason correctly but also produce a coherent, validated 5-section document with specific content requirements per section.

These constraints test true agentic reasoning — not just text generation.

---

## Environment Description

The agent receives a realistic incident bundle: timestamped alert logs, a Slack thread from the on-call team, and a service dependency graph. It must investigate the incident and produce a complete 5-section post-mortem document.

The key mechanic is **QUERY_LOGS** — the agent must identify which service and time window to investigate. The real root cause evidence is hidden behind a specific log query. Wrong queries are penalized with escalating costs. This forces intentional reasoning rather than pattern matching.

### Four Difficulty Levels

| Task | Incident | Key Challenge |
|------|----------|---------------|
| **Easy** | Single-service DB connection leak | Clean signals, clear root cause |
| **Medium** | Cascading failure from Redis TTL misconfiguration | Multiple services affected, deployment buried in Slack |
| **Hard** | Multi-service degradation with planted false root causes | Senior engineer confidently wrong in Slack, real evidence in non-obvious log window |
| **Expert** | Security breach via compromised API key | 3 false root causes, senior security engineer wrong twice, 3-minute evidence window, GDPR action items required |

The hard task deliberately plants two false root causes. The expert task goes further — a scheduled load test using the same API key creates a plausible innocent explanation, a senior security engineer with 12 years experience confidently blames the load test (twice), and the real evidence (Tor exit node + scope violation) is only visible in a precise 3-minute api-gateway audit log window. The expert task also reduces the query budget from 8 to 6 with steeper penalties.

---

## Action Space

| Action | Fields | Description |
|--------|--------|-------------|
| `QUERY_LOGS` | `query_service`, `query_from`, `query_to` | Query logs for a specific service and time window |
| `WRITE_SECTION` | `section_name`, `section_content` | Write one of 5 post-mortem sections |
| `ASSIGN_ACTION_ITEM` | `action_item_description`, `action_item_owner`, `action_item_due_date` | Assign a structured action item |
| `SUBMIT` | — | Finalize and submit for grading |

Valid `section_name` values: `summary`, `timeline`, `root_cause`, `impact`, `action_items`

---

## Observation Space

Each step returns a typed observation containing:

```python
{
  "goal": str,                    # Natural language task description
  "incident_id": str,             # Incident identifier
  "incident_title": str,          # Human-readable incident name
  "alerts": List[AlertLog],       # Timestamped alert logs (severity, service, message)
  "slack_thread": List[SlackMessage], # On-call Slack conversation
  "service_graph": List[ServiceDependency], # Which service depends on which
  "step": int,                    # Current step number
  "max_steps": int,               # Episode limit (25)
  "queries_used": int,            # Queries consumed
  "max_queries": int,             # Query limit (8)
  "sections": List[SectionStatus], # State of each section (unwritten/invalid/valid)
  "last_action_result": str,      # Feedback from last action
  "retrieved_logs": List[AlertLog] | None  # Logs from last QUERY_LOGS call
}
```

---

## Reward Function

Rewards are shaped throughout the episode — not just at the end:

| Signal | Reward |
|--------|--------|
| Correct `QUERY_LOGS` (right service + time window) | +0.06 |
| Valid section written | +0.03 |
| Structured action item assigned | +0.08 |
| Wrong `QUERY_LOGS` (1st mistake) | −0.05 |
| Wrong `QUERY_LOGS` (2nd mistake) | −0.08 |
| Wrong `QUERY_LOGS` (3rd+ mistake) | −0.12 to −0.18 |
| Overwriting an already-valid section | −0.02 |
| Missing section at SUBMIT | −0.10 per section |
| **Final grader score at SUBMIT** | **0.0 – 1.0** |

The final grader score (added at SUBMIT) covers 60–70% of total reward and uses a weighted 5-component formula.

---

## Grader Design

Each task is scored by a deterministic grader (0.0–1.0):

| Component | Weight | How it's measured |
|-----------|--------|-------------------|
| Root cause | 30% | 3-layer: correct service (L1=0.40) + cause category (L2=0.35) + keywords (L3=0.25) |
| Timeline | 25% | Events matched within ±3 min tolerance against gold standard |
| Action items | 20% | Owner + due date + theme coverage |
| Impact | 15% | Word count + service mention + duration + scale |
| Completeness | 10% | All 5 sections present and validated |

The environment is fully deterministic — scenarios are static JSON, grading is a pure function, and identical action sequences always produce identical scores.

**Root cause special rules:**
- If L1 (service identification) = 0, score capped at 0.65
- If false root cause service mentioned before real service, L1 reduced to 0.15
- Timeline score < 0.4 caps root cause at 0.60 (forces reasoning over guessing)

---

## Baseline Scores

Using `llama-3.1-8b-instant` via Groq API (runtime: ~200 seconds):

```
easy  : 1.000  ████████████████████
medium: 0.985  ███████████████████
hard  : 0.838  ████████████████
expert: 0.662  █████████████
avg   : 0.871
```

The difficulty staircase is genuine — each level is harder for a different reason. The hard task misleads agents with a confidently wrong senior engineer and CDN red herrings. The expert task adds a third false root cause, a senior security engineer who is wrong twice, a 3-minute evidence window, and 4 hidden timeline events that require querying the correct log window to unlock. The baseline intentionally does not achieve perfect scores on medium, hard, or expert tasks, demonstrating that the environment is challenging yet solvable. Scores are consistent across runs and deterministic given the same action sequence.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check — returns `{"status": "healthy"}` |
| POST | `/reset` | Start new episode. Body: `{"difficulty": "easy\|medium\|hard"}` |
| POST | `/step` | Execute action. Body: action JSON |
| GET | `/state` | Current episode state |
| GET | `/tasks` | List all 3 tasks |
| WS | `/ws` | WebSocket persistent session |
| GET | `/docs` | Interactive API documentation |

---

## Setup & Usage

### Local

```bash
git clone https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer
cd incident-postmortem-writer

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

uvicorn server.app:app --host 0.0.0.0 --port 7860 --reload
```

Test it:
```bash
curl http://localhost:7860/health
# {"status":"healthy"}
```

### Docker

```bash
docker build -t postmortem-env .
docker run -p 7860:7860 postmortem-env
```

### Run Baseline Inference

```bash
export API_BASE_URL=https://api.groq.com/openai/v1
export MODEL_NAME=llama-3.1-8b-instant
export HF_TOKEN=your_api_key_here

python inference.py
```

The inference script emits structured stdout logs in the OpenEnv-required format:

```
[START] task=easy env=incident-postmortem-writer model=llama-3.1-8b-instant
[STEP] step=1 action=QUERY_LOGS reward=0.06 done=false error=null
[STEP] step=2 action=WRITE_SECTION_summary reward=0.03 done=false error=null
...
[END] success=true steps=8 score=1.000 rewards=0.06,0.03,0.03,0.03,0.03,0.03,0.08,1.00
```

### Use as Client

```python
from client import PostMortemEnv

with PostMortemEnv(base_url="http://localhost:7860") as env:
    result = env.reset(difficulty="easy")
    print(result["observation"]["goal"])

    # Query logs for evidence
    result = env.query_logs("payments", "03:38", "03:45")

    # Write sections
    result = env.write_section("root_cause",
        "Root cause: DB connection leak in payments service v2.4.0...")

    # Submit
    result = env.submit()
    print(result["info"]["grade"]["total_score"])
```

### WebSocket Session

```python
import asyncio, json, websockets

async def run():
    async with websockets.connect(
        "wss://jeevan2717-incident-postmortem-writer.hf.space/ws"
    ) as ws:
        await ws.send(json.dumps({"command": "reset", "difficulty": "hard"}))
        result = json.loads(await ws.recv())
        print(result["data"]["observation"]["goal"])

asyncio.run(run())
```

---

## Project Structure

```
postmortem-env/
├── env/
│   ├── models.py              # Pydantic typed models (Observation, Action, Reward)
│   └── scenarios/
│       ├── easy.json          # Single-service DB outage
│       ├── medium.json        # Cascading Redis TTL failure
│       ├── hard.json          # Multi-service with false root causes
│       └── expert.json        # Security breach with Tor exit node + 3 false root causes
├── server/
│   ├── environment.py         # Core step/reset/state logic + deterministic grader
│   └── app.py                 # FastAPI server (REST + WebSocket)
├── client.py                  # Typed HTTP client
├── inference.py               # Baseline agent script
├── openenv.yaml               # OpenEnv manifest
├── Dockerfile
└── requirements.txt
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_BASE_URL` | `https://api.openai.com/v1` | LLM API endpoint |
| `MODEL_NAME` | `gpt-4o-mini` | Model identifier |
| `HF_TOKEN` | — | API key |
| `WORKERS` | `2` | Uvicorn worker processes |
| `MAX_CONCURRENT_ENVS` | `100` | Max WebSocket sessions |
| `DIFFICULTY` | `easy` | Default task difficulty (easy/medium/hard/expert) |
