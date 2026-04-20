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
- Easy: 0.86 · Medium: 0.84 · Hard: 0.68 · Expert: 0.54

## The Key Innovation: Evidence Gating

Most AI environments hand the agent everything upfront. Ours doesn't.

The agent must actively investigate using `QUERY_LOGS(service, from_time, to_time)`. If it queries the wrong service or time window, it gets penalized and sees noise. Only the correct narrow query surfaces the real evidence.

This forces agents to **reason about where the evidence is**, not pattern-match on alert messages. In the expert task, the correct evidence window is 3 minutes wide — any agent that queries the most-alerted service (rate-limiter, auth) instead of the attack vector (api-gateway) misses it entirely.

Combined with a 3-layer root cause grader (service 0.40 + category 0.35 + keywords 0.25) and position-based penalties (L1 drops from 0.40 to 0.15 if false cause named before real), the environment makes shortcuts measurably impossible.

---

## Training Results: +32.8% Relative Reward Improvement

For Round 2, we fine-tuned **Qwen 2.5-0.5B** using HuggingFace TRL's `SFTTrainer` via Rejection Sampling Fine-Tuning — the same technique OpenAI used for the first InstructGPT.

**Pipeline:**
1. **Teacher rollouts**: Llama-3.1-8B plays 40 episodes across all 4 difficulties, capturing (prompt, action) pairs
2. **Rejection sampling**: Keep only trajectories with final reward ≥ 0.50 (234 high-quality pairs)
3. **TRL SFT training**: 5 epochs on a T4 GPU, 290 steps, loss 3.09 → 0.035
4. **Before/after eval**: Student plays all 4 tasks pre- and post-training

**Results:**

| Difficulty | Before | After | Change |
|:----------:|:------:|:-----:|:------:|
| Easy       | 0.800  | 0.840 | +0.040 |
| Medium     | 0.616  | 0.857 | **+0.241** |
| Hard       | 0.412  | 0.650 | **+0.238** |
| Expert     | 0.321  | 0.508 | **+0.187** |
| **Average**| **0.537** | **0.714** | **+0.176** |

**Relative improvement: +32.8%.** Medium, hard, and expert all jumped by more than 0.18. Easy had less headroom since the baseline was already strong.

![Reward Improvement Chart — before vs after TRL fine-tuning across all 4 difficulty levels](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/reward_improvement.png)

Training loss descended cleanly from 3.09 to 0.035 over 290 steps, showing the student genuinely learned the high-reward trajectory patterns rather than failing to converge.

![Training Loss Curve — TRL SFT training convergence over 290 steps](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/training_loss_curve.png)

Full training metadata and per-task before/after numbers are available in [`training_results.json`](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/blob/main/training_results.json) for reproducibility.

---

## Why This Matters for Enterprise AI

This environment maps directly to **Theme #3.1 — World Modeling: Professional Tasks** and the Scaler AI Labs "Multi-App Enterprise Workflow" bonus:

- **Multi-app workflow**: alerts dashboard, Slack, service graph, log query system
- **Real enterprise setting**: SRE + DevOps + security operations
- **Business rule nuances**: GDPR notification requirements, escalation policies, action item ownership
- **No shortcuts possible**: evidence gating prevents pattern-matching

The deterministic grader means any lab can benchmark GPT-4, Claude, Gemini, and open-source models against each other fairly. The 4-level staircase means the environment doesn't saturate — there's always harder ground for stronger agents to climb.

---

## What's Next

Environment Innovation (40%), Reward Pipeline (10%), Showing Improvement (20%) — these are the Round 2 judging axes where the training results above speak for themselves. Storytelling (30%) is where we'll deliver in Bangalore on 25-26 April.

Real-world extensions we're excited about:
- Connecting to live incident data (PagerDuty, Datadog, Splunk)
- Multi-incident episode chaining (what if the same root cause causes 3 incidents over a week?)
- Automated post-mortem generation for on-call engineers as a copilot

---

## Technical Details

- **Environment**: FastAPI + Pydantic typed models, deployed on HuggingFace Spaces (CPU-basic tier)
- **Grader**: Pure Python deterministic function — 3-layer root cause, timeline matching, action item validation, impact checks, completeness
- **Inference spec**: OpenAI-compatible client with required `[START]/[STEP]/[END]` stdout logging format
- **Training**: TRL `SFTTrainer` on Colab T4, Qwen 2.5-0.5B base, rejection sampling threshold 0.50
- **Reproducibility**: Fixed seed (42), deterministic grader, saved model checkpoint

Environment URL: https://jeevan2717-incident-postmortem-writer.hf.space
GitHub: https://github.com/Jeevan2798/incident-postmortem-writer

Thanks to the OpenEnv team at Meta/PyTorch and HuggingFace for an excellent framework, and Scaler School of Technology for hosting the finale.
