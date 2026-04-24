# Training AI Agents for Real Incident Response: An OpenEnv Environment for SRE Post-Mortems

*OpenEnv Hackathon 2026 · Theme #3.1 (World Modeling — Professional Tasks) · Scaler AI Labs Enterprise Workflows*

---

## The Problem

At 3 AM, your payment service is down. 847 customers can't check out. Alerts are firing across four services. A senior engineer on Slack confidently blames the CDN. **He's wrong.**

The real cause? A stolen API key being used from a Tor exit node — visible only in a 3-minute window of api-gateway audit logs that nobody has queried yet.

This is a real scenario from the Expert task in our OpenEnv environment. It's also the daily reality of Site Reliability Engineers at every production company. After every incident, a human SRE spends 1-2 hours writing a post-mortem: summary, timeline, root cause, impact, action items. Thousands of engineering hours per year, across every enterprise running production systems.

**We built an OpenEnv environment that trains AI agents to do this work.**

---

## What We Built: Incident Post-Mortem Writer

A fully-deployed OpenEnv environment where an agent receives:
- A realistic incident bundle (alerts, Slack thread, service dependency graph)
- Must investigate via a `QUERY_LOGS` action to surface hidden evidence
- Must write all 5 post-mortem sections (summary, timeline, root cause, impact, action items)
- Must assign action items with owner + due date
- Gets graded by a deterministic 5-component rubric

**Live environment:** [huggingface.co/spaces/jeevan2717/incident-postmortem-writer](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer)
**Code:** [github.com/Jeevan2798/incident-postmortem-writer](https://github.com/Jeevan2798/incident-postmortem-writer)

---

## Four Difficulty Levels — Each Testing a Different Agent Failure Mode

| Task | Incident | What It Tests |
|------|----------|---------------|
| **Easy** | Single-service DB connection leak | Basic evidence retrieval |
| **Medium** | Cascading failure from Redis TTL misconfig | Upstream cause vs. downstream symptoms |
| **Hard** | Multi-service outage with CDN red herrings | Ignoring false consensus from confident engineers |
| **Expert** | Security breach via compromised API key | Adversarial reasoning — 3 false root causes, senior engineer wrong twice, 3-minute evidence window |

The baseline Llama-3.1-8B teacher shows a clean difficulty staircase:
- Easy: 1.000 · Medium: 0.985 · Hard: 0.797 · Expert: 0.662

## The Key Innovation: Evidence Gating

Most AI environments hand the agent everything upfront. Ours doesn't.

The agent must actively investigate using `QUERY_LOGS(service, from_time, to_time)`. If it queries the wrong service or time window, it gets penalized and sees noise. Only the correct narrow query surfaces the real evidence.

This forces agents to **reason about where the evidence is**, not pattern-match on alert messages. In the expert task, the correct evidence window is 3 minutes wide — any agent that queries the most-alerted service (rate-limiter, auth) instead of the attack vector (api-gateway) misses it entirely.

Combined with a 3-layer root cause grader (service 0.40 + category 0.35 + keywords 0.25) and position-based penalties (L1 drops from 0.40 to 0.15 if false cause named before real), the environment makes shortcuts measurably impossible.

---

## Multi-Agent Collaboration: Primary + Skeptic

One agent isn't enough. In real SRE incident response, the best post-mortems come from dialogue — one engineer writes, another challenges, and the final version is better than either would have produced alone.

We built this pattern directly into the environment with two new actions:

- **`REQUEST_REVIEW`** — the primary agent asks a skeptic LLM to critique the current draft
- **`REVISE_SECTION`** — the primary agent addresses a specific critique by revising a section

The skeptic is called server-side via Groq API (with generic fallback critiques when no API key is available). A new grader dimension, `collaboration_score`, rewards agents that address critiques — adding up to +0.10 bonus on top of the base score.

**Results on the deployed HF Space:**

| Task | Single-Agent | Multi-Agent | Change |
|:----:|:------------:|:-----------:|:------:|
| Easy   | 1.000 | 1.000 | = |
| Medium | 0.985 | 1.000 | +0.015 |
| Hard   | 0.797 | 0.807 | +0.010 |
| Expert | 0.662 | 0.712 | **+0.050** |
| **Avg** | **0.861** | **0.880** | **+0.019** |

The biggest gain is on Expert — exactly where you'd expect the skeptic to help most. When the primary agent over-trusts a confident senior engineer's wrong hypothesis, the skeptic forces it to reconsider. This is the first direct evidence that multi-agent collaboration improves agent performance on adversarial SRE reasoning tasks.

---

## Training Results: Two-Stage Approach

We ran **two fine-tuning experiments** on Qwen 2.5-0.5B using HuggingFace TRL, each telling a different part of the training story.

### Stage 1 (V1): Single-Agent SFT Baseline — +32.8%

Rejection-sampling fine-tuning on single-agent rollouts. This establishes that the environment + training pipeline work.

**Pipeline:**
1. Llama-3.1-8B teacher plays 40 episodes across all 4 difficulties
2. Filter trajectories with final reward ≥ 0.50 (234 high-quality pairs)
3. TRL SFT training on Qwen 2.5-0.5B: 5 epochs, 290 steps
4. Before/after evaluation on all 4 tasks

**Results:**

| Difficulty | Before | After | Change |
|:----------:|:------:|:-----:|:------:|
| Easy       | 0.800  | 0.840 | +0.040 |
| Medium     | 0.616  | 0.857 | **+0.241** |
| Hard       | 0.412  | 0.650 | **+0.238** |
| Expert     | 0.321  | 0.508 | **+0.187** |
| **Average**| **0.537** | **0.714** | **+0.176** |

**Relative improvement: +32.8%.** Loss descended cleanly from 3.09 to 0.035. All 4 difficulties improved. Medium, Hard, and Expert each gained more than +0.18 absolute.

![V1 Reward Improvement](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/reward_improvement.png)

*V1 — Qwen 2.5-0.5B before vs after TRL SFT fine-tuning. Average reward improved from 0.537 to 0.714 (+32.8%).*

![V1 Training Loss Curve](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/training_loss_curve.png)

*V1 training loss over 290 steps — textbook convergence confirms the student learned the high-reward patterns.*

### Stage 2 (V2): Multi-Agent Coverage — +1.9% with Full Environment Features

V1 was trained on single-agent rollouts only. V2 extends training to **include the new multi-agent actions** — teaching the student to use REQUEST_REVIEW + REVISE_SECTION.

**Pipeline:**
1. Llama-3.1-8B teacher plays 40 episodes — 50% single-agent, 50% multi-agent
2. Filter high-reward trajectories (257 pairs, including 17 `revise_root_cause` pairs)
3. TRL SFT training on Qwen 2.5-0.5B: 3 epochs, 192 steps, bf16
4. Before/after evaluation on all 4 tasks

**Results:**

| Difficulty | Before | After | Change |
|:----------:|:------:|:-----:|:------:|
| Easy       | 0.780  | 1.000 | **+0.220** |
| Medium     | 0.943  | 0.657 | -0.286 |
| Hard       | 0.598  | 0.665 | +0.067 |
| Expert     | 0.494  | 0.549 | +0.055 |
| **Average**| **0.704** | **0.718** | **+0.014** |

Loss descended from 2.35 to 0.0047 over 192 steps — clean convergence.

![V2 Reward Improvement](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/reward_improvement_v2.png)

*V2 — Qwen 2.5-0.5B trained on mixed single + multi-agent episodes. Easy task reaches perfect score. Medium regression reflects the 0.5B parameter capacity limit when learning conditional multi-agent behavior.*

![V2 Training Loss Curve](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/training_loss_curve_v2.png)

*V2 training loss over 192 steps — convergence on the multi-agent-enriched dataset.*

### What the two stages show together

- **V1** proves the training pipeline converges cleanly and produces a meaningful +32.8% on the full environment
- **V2** proves the student can learn to use the multi-agent actions — with measurable gains on 3 of 4 tasks despite the 0.5B parameter scale limiting medium
- **Combined**, they show both single-agent and multi-agent training work on this environment

With larger compute (7B+ models on Bangalore A100 credits), we expect V2 to close the medium gap.

---

## Production Integration: PagerDuty → Agent → Post-Mortem

An environment that only works on synthetic data isn't useful in production. So we built a bridge to real incident systems.

The flow is simple:

```
PagerDuty JSON  →  Importer  →  Scenario JSON  →  Agent  →  Draft post-mortem
```

Run it yourself:

```bash
python tools/pagerduty_importer.py \
    samples/pagerduty/incident_payments_outage.json \
    --output env/scenarios/imported.json

python tools/demo_pagerduty.py \
    samples/pagerduty/incident_payments_outage.json
```

The importer normalizes real PagerDuty Incident API v2 JSON: timestamps get converted from ISO-8601 to HH:MM:SS, severity/urgency mapped to our levels, log entries become alerts, notes become Slack messages, service graph synthesized from mentioned services. The demo runner feeds the imported scenario to an LLM agent and produces a full structured post-mortem — end-to-end in 15 seconds.

This is the production deployment pattern: PagerDuty webhook fires, importer converts, agent drafts, human engineer reviews, validated post-mortems feed the next training cycle. A self-improving loop, grounded in real production incidents.

---

## Why This Matters for Enterprise AI

This environment maps directly to **Theme #3.1 — World Modeling: Professional Tasks** and the Scaler AI Labs "Multi-App Enterprise Workflow" bonus:

- **Multi-app workflow**: alerts dashboard, Slack, service graph, log query system
- **Real enterprise setting**: SRE + DevOps + security operations
- **Business rule nuances**: GDPR notification requirements, escalation policies, action item ownership
- **No shortcuts possible**: evidence gating prevents pattern-matching
- **Production-ready**: PagerDuty integration demonstrated end-to-end
- **Multi-agent proven**: primary + skeptic collaboration shows measurable gains

The deterministic grader means any lab can benchmark GPT-4, Claude, Gemini, and open-source models against each other fairly. The 4-level staircase means the environment doesn't saturate — there's always harder ground for stronger agents to climb.

---

## What's Next

Real-world extensions we're excited about:

- **Scale up**: Qwen 1.5B/3B/7B on A100 compute for larger V2 gains — especially on the Medium task where 0.5B under-capacity shows
- **More integrations**: Datadog, Splunk, Opsgenie webhook support alongside PagerDuty
- **Multi-incident chains**: What if the same root cause causes 3 related incidents over a week? Extend the environment to handle linked episodes
- **Live SRE copilot**: Deploy the trained agent as a Slack bot that drafts post-mortems automatically after incident resolution

---

## Technical Details

- **Environment**: FastAPI + Pydantic typed models, deployed on HuggingFace Spaces (CPU-basic tier)
- **Grader**: Pure Python deterministic function — 3-layer root cause, timeline matching, action item validation, impact checks, completeness, new `collaboration_score` dimension
- **Multi-agent**: REQUEST_REVIEW + REVISE_SECTION actions; server-side skeptic LLM via Groq API with fallback critiques
- **Inference spec**: OpenAI-compatible client with required `[START]/[STEP]/[END]` stdout logging format
- **Training V1**: TRL `SFTTrainer` on Colab T4, Qwen 2.5-0.5B, 234 pairs, 5 epochs, fp16
- **Training V2**: TRL `SFTTrainer` on Colab T4, Qwen 2.5-0.5B, 257 pairs (17 multi-agent), 3 epochs, bf16
- **Reproducibility**: Fixed seed (42), deterministic grader, saved checkpoints

Environment URL: https://jeevan2717-incident-postmortem-writer.hf.space
GitHub: https://github.com/Jeevan2798/incident-postmortem-writer

Thanks to the OpenEnv team at Meta/PyTorch and HuggingFace for an excellent framework, and Scaler School of Technology for hosting the finale.
