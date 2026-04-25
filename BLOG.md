# Teaching an AI to write incident post-mortems: my OpenEnv hackathon journey

I'm a software engineer. I ship code to production. And the part of the job nobody talks about in interviews is what happens *after* a deployment goes out — those few days where you're nervously watching dashboards, scrubbing logs, hoping nothing breaks.

When something does break — and eventually it always does — the work doesn't end with the rollback. The hard part starts the next morning. You sit down with cold coffee, open a blank doc, and try to reconstruct what happened. Pull alerts from one tool, scroll through a Slack thread that has fifteen wrong opinions and one right one buried in the middle, dig through audit logs you should have queried six hours ago. Then write it all up.

Summary. Timeline. Root cause. Impact. Action items.

One to two hours, every single time. Multiplied by every incident, at every company running production systems.

I've done this enough times to know exactly which parts hurt. The investigation hurts because the evidence is scattered across five tools. The writeup hurts because you're tired and you have to be precise. And the worst part is that the post-mortem is the thing that prevents the next incident — so doing it badly costs you twice.

So when I read about the OpenEnv Hackathon and saw Theme #3.1 — "World Modeling: Professional Tasks" — I had a very specific thought: this is the workflow I'd want an AI agent to do for me. Not summarize. Not chat. *Actually do the post-mortem work.*

I wanted to see if I could build that.

---

## The first naive attempt

My first instinct was the obvious one: just give an LLM the alerts and the Slack thread, ask it to write a post-mortem. It produces something that looks impressive — five sections, professional formatting, all the right vocabulary. But when I read it carefully, something was off. The agent had pattern-matched on the loudest service in the alerts. It had named the wrong root cause. The timeline was made up.

It hadn't actually *investigated* anything. It just summarized what was already in front of it.

That's when I realized this couldn't be a passive task. A real SRE doesn't just summarize alerts — they pull up the audit logs, they ignore the senior engineer who confidently blamed the CDN, they trace upstream from the cascading symptoms to find the actual cause. The investigation is the work.

So I rebuilt it as an environment where the agent has to ask for evidence. A `QUERY_LOGS` action that takes a service name and a time window. If you query the wrong service or the wrong window, you get noise and a penalty. Only the precise correct query — like asking the api-gateway for logs in a 3-minute window between 02:58 and 03:01 — surfaces the real evidence.

I called this **evidence gating**. It became the heart of the environment.

---

## Building four levels of difficulty

I wrote four scenarios, each designed to break agents in a different way.

**Easy** is straightforward — a payments service has a database connection leak after a deployment. The signals are clean. Any decent agent should solve it. (Most do — Llama-3.1-8B scores 1.000 here.)

**Medium** is where things get interesting. The visible symptoms are checkout failures and auth errors, but the actual root cause is a Redis TTL config change made 3 minutes earlier. To get this right, the agent has to trace upstream from the symptom to the cause. Pure pattern-matching fails.

**Hard** introduces adversarial Slack messages. Two engineers in the channel confidently blame the CDN — and they're wrong. The actual cause is a memory leak in the auth service, visible only if you ignore the loud opinions and follow the data. (Llama-3.1-8B drops to 0.797 here.)

**Expert** is the one I'm most proud of. A security breach via a compromised API key. Three different false root causes get proposed in Slack. Two senior engineers get it wrong. The real evidence — a Tor exit node hammering api-gateway with a stolen service account key — exists in a 3-minute window of audit logs that no one has queried yet. The agent has to ignore the noise, trust the data, and find the breach. (Llama-3.1-8B scores 0.662 — even a strong model gets this wrong about a third of the time.)

When I plotted the baseline scores I got a clean staircase: 1.000 → 0.970 → 0.797 → 0.662. That told me the difficulties were actually graduated, not arbitrary. Each level was testing something the previous one couldn't.

---

## The first training run

Round 2 of the hackathon was about training. I had to fine-tune a small model on the environment and show measurable improvement.

I picked Qwen 2.5-0.5B as the student. The teacher was Llama-3.1-8B running on Groq. The plan was simple: have the teacher play 40 episodes across all 4 difficulties, keep the trajectories where the final reward was at least 0.50, and use those high-reward (prompt → response) pairs to fine-tune the student via TRL's `SFTTrainer`.

This is rejection sampling — and it works because the student only ever sees what successful behavior looks like. There's no contradictory signal.

I ran it on a free Colab T4. 40 episodes, 234 surviving pairs, 5 epochs of training, 290 gradient steps. The loss descended cleanly from 3.09 to 0.035 — the kind of textbook curve you get when the student is genuinely learning, not just memorizing.

Then I ran the trained student against the four difficulties:

| Difficulty | Before | After | Change |
|:----------:|:------:|:-----:|:------:|
| Easy       | 0.800  | 0.840 | +0.040 |
| Medium     | 0.616  | 0.857 | **+0.241** |
| Hard       | 0.412  | 0.650 | **+0.238** |
| Expert     | 0.321  | 0.508 | **+0.187** |
| **Average**| **0.537** | **0.714** | **+0.176** |

A **32.8% relative improvement** across all four tasks. The student model, just 0.5 billion parameters — a fraction of the teacher's 8B — was now scoring within striking distance of its teacher on Easy and Medium, and showing real signs of learning the harder tasks too.

I sat there for a minute looking at the numbers. This was the first time I'd seen training actually work on something I built. Not a textbook example. My environment, my scenarios, my pipeline. It worked.

![V1 Reward Improvement](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/reward_improvement.png)

*V1 — Qwen 2.5-0.5B before vs after TRL SFT fine-tuning. Average reward improved from 0.537 to 0.714 (+32.8%).*

![V1 Training Loss Curve](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/training_loss_curve.png)

*V1 training loss over 290 steps — clean convergence from 3.09 to 0.035, the kind of curve you get when the student is genuinely learning high-reward patterns rather than memorizing surface features.*

---

## Adding a second voice

Single agents are limiting. In real SRE post-mortems, the best documents come from dialogue — one engineer writes, another challenges, the final version is sharper than either alone. I wanted that pattern in the environment.

So I added two new actions:

- **`REQUEST_REVIEW`** — the primary agent asks a skeptic LLM to critique the current draft
- **`REVISE_SECTION`** — the primary agent rewrites a section addressing a specific critique

The skeptic runs server-side. When configured with a Groq API key, it generates tailored critiques per draft — pointing out vague mechanisms, missing service names, unsupported claims. When no key is set, it falls back to five generic critiques that still surface common writing flaws. Both modes work. The first is sharper.

I also added a new dimension to the grader: `collaboration_score`. Agents that address critiques get up to +0.10 bonus on top of their base score.

The results, on the deployed Hugging Face Space:

| Task | Single-Agent | Multi-Agent | Change |
|:----:|:------------:|:-----------:|:------:|
| Easy   | 1.000 | 1.000 | = |
| Medium | 0.970 | 1.000 | +0.030 |
| Hard   | 0.797 | 0.807 | +0.010 |
| Expert | 0.662 | 0.712 | **+0.050** |
| **Avg** | **0.857** | **0.880** | **+0.022** |

Notice where the biggest gain shows up: the Expert task. The hardest one, the one with the most adversarial signals. That's exactly where you'd predict a skeptic would help — when the primary agent's confidence is wrong, the skeptic forces it to reconsider.

This is real evidence that primary-skeptic collaboration improves agent reasoning on adversarial SRE tasks. Not just "we built a multi-agent system" — but a measurable +0.050 on the task that matters most.

---

## The training run that didn't go as planned

I wanted to push further. V1 had been trained before the multi-agent actions existed. The student model didn't know what `REQUEST_REVIEW` or `REVISE_SECTION` even were. So I ran V2: the same training pipeline, but with multi-agent rollouts mixed in.

I generated 40 new teacher episodes, half single-agent and half multi-agent. Of those, 257 (prompt → response) pairs survived rejection sampling, including 17 specifically of the new `revise_root_cause` phase. The student would now learn not just what to write but **when to ask for review** and **how to revise** based on a critique.

The training itself went through three failure modes before I got it working. First an FP16 gradient unscaling error — Qwen 0.5B doesn't play nicely with `fp16=True` in TRL 0.11.4. I switched to fp32 weights, and immediately ran into out-of-memory because fp32 doubled the footprint. Then I tried bf16 — T4 supports it — and OOMed again because previous failed runs had left the GPU memory fragmented. Finally with a fresh runtime, batch size 1, sequence length 768, gradient accumulation 4, it ran. Loss converged from 2.35 to 0.0047 over 192 steps.

Then I evaluated:

| Difficulty | Before | After | Change |
|:----------:|:------:|:-----:|:------:|
| Easy       | 0.780  | 1.000 | **+0.220** |
| Medium     | 0.943  | 0.657 | **−0.286** |
| Hard       | 0.598  | 0.665 | +0.067 |
| Expert     | 0.494  | 0.549 | +0.055 |
| **Average**| **0.704** | **0.718** | **+0.014** |

Easy went to a perfect score. Hard and Expert improved. But Medium **regressed by 0.29 points.**

That stung at first. But when I sat with it, I realized what had happened. V1 only had to learn one thing: how to write sections from context. V2 was asking the same 0.5B parameter model to learn that **plus** the conditional behavior of "when should I trigger a review, when should I revise?" That conditional pattern costs capacity. Easy is simple enough that the cost doesn't bite. Medium needs every bit of capacity for its cascading-failure reasoning, and giving up some of that capacity to the multi-agent decision logic broke it.

This isn't a bug in the pipeline. It's a documented finding in tool-use research: small models struggle with conditional multi-task behavior. Bigger models close the gap.

So now I have two stories. **V1 proves the training pipeline works** — clean +32.8% on the full environment. **V2 proves the environment can be trained on its multi-agent extension** — measurable gains on three of four tasks, with the medium regression honestly disclosed as a capacity limit. With a 7B-class model, V2 should keep V1's gains and add the multi-agent capability without trade-off.

![V2 Reward Improvement](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/reward_improvement_v2.png)

*V2 — Qwen 2.5-0.5B trained on mixed single + multi-agent episodes. Easy task reaches perfect score. Medium regression reflects the 0.5B parameter capacity limit when learning conditional multi-agent behavior.*

![V2 Training Loss Curve](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer/resolve/main/training_loss_curve_v2.png)

*V2 training loss over 192 steps — convergence from 2.35 to 0.0047 on the multi-agent-enriched dataset.*

---

## The production bridge

By this point I had a working environment, a trained model, and multi-agent extensions. But everything still felt synthetic. The scenarios were hand-written by me. The incidents weren't real.

I wanted to close that gap.

Real SRE teams don't write incident JSON by hand — they use PagerDuty, or Datadog, or Splunk. PagerDuty's Incident API v2 returns rich JSON with alert log entries, on-call notes, service metadata, severity levels, status transitions. So I built an importer.

`tools/pagerduty_importer.py` takes any real PagerDuty incident JSON and converts it to my environment's scenario format. ISO-8601 timestamps become HH:MM:SS. PagerDuty severity maps to our levels. Log entries become alerts. On-call notes become Slack messages. The service graph synthesizes from any services mentioned in the data.

Then `tools/demo_pagerduty.py` runs the full pipeline end-to-end:

```bash
python tools/demo_pagerduty.py samples/pagerduty/incident_payments_outage.json
```

I tested it with a realistic incident — payments service v2.4.0 deployed at 03:38, connection pool exhausted at 03:41, 62% checkout conversion drop, rolled back at 03:48, recovered at 04:09. PagerDuty would record all this metadata, but it wouldn't write the post-mortem. The agent did. In about 15 seconds, it produced a structured document:

> **SUMMARY:** payments-api outage ~23 min, 5xx spike, checkout failures.
> **ROOT_CAUSE:** DB connection leak in payments-api v2.4.0 deployment. Pool exhausted at max, preventing new connections.
> **IMPACT:** 62% checkout conversion drop. 28s p99 latency (baseline 180ms). Revenue impact.
> **ACTION_ITEMS:** (1) Code review of v2.4.0. (2) Pre-deploy canary. (3) Connection pool monitoring.

This is the production deployment pattern: PagerDuty webhook fires, importer converts the JSON, agent generates a draft post-mortem, a human engineer reviews and validates. The validated post-mortems then become the next training cycle. A self-improving loop, grounded in real production incidents — no synthetic data needed once the system is running.

That's what I want this environment to enable. Not just an academic benchmark. A real tool that an SRE team could deploy on Monday and get value from on Tuesday.

---

## What I'd do differently with more compute

The biggest thing limiting V2's multi-agent training was the 0.5B model size. With access to a single A100 or L4 GPU, the next steps are obvious:

**Scale to Qwen 1.5B, 3B, or 7B.** Re-run V2. The Medium regression should disappear once the model has enough capacity to hold both the writing pattern and the conditional review logic without crowding either out.

**Add more importers.** PagerDuty was first, but the same pattern works for Datadog (`monitor.webhook` payloads), Splunk (`splunk-webhook`), Opsgenie. Each follows the same `import_*()` contract. A team running multiple monitoring tools could use this environment as a unified post-mortem layer.

**Multi-incident chains.** What if the same root cause causes three related incidents over a week? That's a real scenario in production, and the environment doesn't model it yet. Linked episodes would test long-context reasoning across days.

**Live deployment.** Take the trained agent, wrap it in a Slack bot, hook it to a real PagerDuty webhook at a small company, and watch what happens. The metric becomes "what percentage of agent drafts make it through human review unchanged?" That's the real benchmark.

---

## Why this matters

Most agent benchmarks test pattern-matching on synthetic data. SRE post-mortem writing is something else: a real workflow with measurable business value (1-2 hours per incident saved, multiplied by every incident at every company), a multi-app workflow (alerts, Slack, service graphs, log queries, monitoring tools), and an objective ground truth (the validated post-mortem).

The environment maps cleanly onto Theme #3.1 — World Modeling: Professional Tasks. It also fits the Scaler AI Labs "Multi-App Enterprise Workflow" angle: alerts dashboard, Slack thread, service dependency graph, log query system, business rule nuances around GDPR notification and escalation policies. No shortcuts possible because of evidence gating. Production-ready because of the PagerDuty integration. Multi-agent proven because of the +0.050 gain on the Expert task.

The deterministic grader means any lab can benchmark GPT-4, Claude, Gemini, and open-source models against each other fairly on the same tasks. The 4-level difficulty staircase means the environment doesn't saturate — there's always harder ground for stronger agents to climb.

---

## Closing thoughts

I built this as a solo entry. Three weeks ago I'd never trained an LLM. Now I have a deployed Hugging Face Space, two TRL fine-tuning runs documented end-to-end, multi-agent extensions with measurable gains, and a real production integration pattern.

The tooling has gotten so much better in the last year. OpenEnv made the environment scaffolding clean. TRL made the training accessible. Hugging Face Spaces made deployment trivial. Groq made fast inference free for hackathon use. None of this would have been buildable by one person on weekends a year ago.

If you want to try it yourself:

- **Live environment:** [huggingface.co/spaces/jeevan2717/incident-postmortem-writer](https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer)
- **Code, notebooks, samples:** [github.com/Jeevan2798/incident-postmortem-writer](https://github.com/Jeevan2798/incident-postmortem-writer)
- **Training notebooks** to reproduce the V1 +32.8% and V2 multi-agent results, both runnable on free Colab T4

Honest open question I'd love feedback on: how do you think about the V2 medium regression? Is the right move to scale up the student model, or should the training data be re-balanced to give the multi-agent rollouts less weight relative to the simpler writing examples? I have intuition either way but no clean answer yet.

Thanks for reading. And thanks to the OpenEnv team at Meta and PyTorch, the Hugging Face TRL team, and Scaler School of Technology for hosting the finale. Looking forward to seeing what everyone builds next.

— Jeevan
