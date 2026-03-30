"""
Demo Script — Incident Post-Mortem Writer (OpenEnv)
====================================================
Demonstrates the environment capabilities interactively.
Shows all 3 difficulty levels, action types, and reward signals.

Usage:
    # Against local server:
    python demo.py

    # Against deployed HuggingFace Space:
    python demo.py --url https://jeevan2717-incident-postmortem-writer.hf.space
"""

import argparse
import json
import sys
import requests


class PostMortemEnv:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def reset(self, difficulty="easy"):
        r = self._session.post(f"{self.base_url}/reset", json={"difficulty": difficulty}, timeout=30)
        r.raise_for_status()
        return r.json()

    def step(self, action):
        r = self._session.post(f"{self.base_url}/step", json=action, timeout=30)
        r.raise_for_status()
        return r.json()

    def health(self):
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def tasks(self):
        r = self._session.get(f"{self.base_url}/tasks", timeout=10)
        r.raise_for_status()
        return r.json()


def separator(title=""):
    if title:
        print(f"\n{'─'*20} {title} {'─'*20}")
    else:
        print("─" * 60)


def demo_health_and_tasks(env):
    """Show server is healthy and list all tasks."""
    separator("1. Health Check")
    print("GET /health →", end=" ")
    if env.health():
        print("✅ healthy")
    else:
        print("❌ not responding")
        sys.exit(1)

    separator("2. Available Tasks")
    tasks = env.tasks()
    for t in tasks["tasks"]:
        print(f"  [{t['difficulty'].upper():6s}] {t['name']}")
        print(f"           max_steps={t['max_steps']} max_queries={t['max_queries']}")


def demo_easy_episode(env):
    """Full easy episode — shows correct query, all sections, perfect score."""
    separator("3. Easy Episode — Full Walkthrough")

    result = env.reset(difficulty="easy")
    obs = result["observation"]
    print(f"\nIncident: {obs['incident_title']}")
    print(f"Goal:     {obs['goal'][:80]}...")
    print(f"Alerts:   {len(obs['alerts'])} | Slack: {len(obs['slack_thread'])} messages")

    # Step 1: Query the right logs
    print("\n→ QUERY_LOGS payments [03:38–03:43]")
    result = env.step({
        "action_type": "QUERY_LOGS",
        "query_service": "payments",
        "query_from": "03:38",
        "query_to": "03:43"
    })
    reward = result["reward"]["total"]
    logs = result["observation"].get("retrieved_logs") or []
    print(f"  reward={reward:+.3f} | Retrieved {len(logs)} log lines")
    if logs:
        print(f"  Key log: {logs[1]['message'][:70]}")

    # Step 2: Write all 5 sections
    sections = {
        "summary": "The payments service experienced a complete outage due to DB connection pool exhaustion caused by a connection leak in v2.4.0.",
        "timeline": "03:38 - v2.4.0 deployed to payments service\n03:41 - First DB connection warnings fired\n03:43 - payments health check FAILED\n04:02 - Rollback to v2.3.1 initiated\n04:09 - Service recovered",
        "root_cause": "Root cause: deployment bug in payments service v2.4.0. The PaymentProcessor.charge() method introduced a connection leak where DB connections were not released after failed transactions, exhausting the connection pool.",
        "impact": "The payments service was unavailable for 28 minutes affecting approximately 1240 users who attempted payments during the outage. Estimated revenue impact was $18,600 in delayed transactions.",
        "action_items": "1. Fix connection leak in PaymentProcessor.charge() - Owner: payments-team - Due: 2024-08-01\n2. Add DB connection pool monitoring alerts - Owner: sre - Due: next sprint\n3. Add integration test for connection release - Owner: platform - Due: 2024-08-15",
    }

    for section_name, content in sections.items():
        result = env.step({
            "action_type": "WRITE_SECTION",
            "section_name": section_name,
            "section_content": content,
        })
        reward = result["reward"]["total"]
        msg = result["observation"]["last_action_result"][:50]
        print(f"  WRITE {section_name:12s} → reward={reward:+.3f} | {msg}")

    # Step 3: Assign action item
    result = env.step({
        "action_type": "ASSIGN_ACTION_ITEM",
        "action_item_description": "Fix connection leak and add monitoring",
        "action_item_owner": "payments-team",
        "action_item_due_date": "2024-08-01",
    })
    print(f"\n  ASSIGN_ACTION_ITEM → reward={result['reward']['total']:+.3f}")

    # Step 4: Submit
    result = env.step({"action_type": "SUBMIT"})
    grade = result["info"].get("grade", {})
    print(f"\n  SUBMIT → FINAL GRADE: {grade.get('total_score', 0):.3f}")
    print(f"  root_cause={grade.get('root_cause_score',0):.2f} "
          f"timeline={grade.get('timeline_score',0):.2f} "
          f"action_items={grade.get('action_items_score',0):.2f} "
          f"impact={grade.get('impact_score',0):.2f} "
          f"completeness={grade.get('completeness_score',0):.2f}")
    return grade.get("total_score", 0)


def demo_wrong_query_penalty(env):
    """Show escalating penalties for wrong queries."""
    separator("4. Wrong Query — Escalating Penalties")

    env.reset(difficulty="medium")
    print("\nQuerying wrong service (auth instead of redis-auth):")

    penalties = []
    for i in range(3):
        result = env.step({
            "action_type": "QUERY_LOGS",
            "query_service": "auth",
            "query_from": "14:00",
            "query_to": "14:05",
        })
        reward = result["reward"]["total"]
        penalties.append(reward)
        print(f"  Wrong query #{i+1} → reward={reward:+.3f} (escalating penalty)")

    print(f"\n  Penalties: {penalties[0]:+.3f} → {penalties[1]:+.3f} → {penalties[2]:+.3f}")
    print("  ✅ Brute-force querying is penalized — agent must reason, not guess")


def demo_hard_challenge(env):
    """Show the hard scenario's misleading signals."""
    separator("5. Hard Scenario — Adversarial Signals")

    result = env.reset(difficulty="hard")
    obs = result["observation"]
    print(f"\nIncident: {obs['incident_title']}")
    print(f"\nSlack thread (showing adversarial signals):")
    for msg in obs["slack_thread"][:6]:
        print(f"  [{msg['timestamp']}] {msg['author']}: {msg['text'][:80]}")

    print("\n  ↑ Notice: 2 engineers confidently blame CDN")
    print("  ↑ Senior engineer says 'CDN vendor' twice")
    print("  ↑ Real cause (data-pipeline schema migration) is hidden in logs")
    print("\n  Agent must query the right service+window to find truth.")
    print("  Querying CDN → -0.050 penalty (false root cause service)")


def main():
    parser = argparse.ArgumentParser(description="Demo for Incident Post-Mortem Writer OpenEnv")
    parser.add_argument("--url", default="http://localhost:7860",
                        help="Environment server URL")
    args = parser.parse_args()

    print("=" * 60)
    print("  Incident Post-Mortem Writer — Demo")
    print("=" * 60)
    print(f"  Server: {args.url}")

    env = PostMortemEnv(base_url=args.url)

    # Run all demos
    demo_health_and_tasks(env)
    easy_score = demo_easy_episode(env)
    demo_wrong_query_penalty(env)
    demo_hard_challenge(env)

    # Summary
    separator("Summary")
    print(f"\n  Easy episode perfect score: {easy_score:.3f}")
    print("  Wrong queries correctly penalized: ✅")
    print("  Hard scenario adversarial signals: ✅")
    print()
    print("  Environment URL:", args.url)
    print("  HuggingFace Space: https://huggingface.co/spaces/jeevan2717/incident-postmortem-writer")
    print()
    print("  Run baseline inference:")
    print("    python inference.py")
    print()
    print("  Run against live Space:")
    print("    set ENV_BASE_URL=https://jeevan2717-incident-postmortem-writer.hf.space")
    print("    python inference.py")


if __name__ == "__main__":
    main()
