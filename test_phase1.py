"""
Phase 1 Multi-Agent Smoke Test (v2 — corrected data)
=====================================================

Run from project root:
  python test_phase1.py

Expected: 6 ✅ checks pass.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from env.models import Action, ActionType, SectionName
from server.environment import PostMortemEnvironment

print('=' * 60)
print(' Phase 1 Multi-Agent Smoke Test')
print('=' * 60)

# ───────────────────────────────────────────────────────────
# TEST 1: Multi-agent flow
# ───────────────────────────────────────────────────────────
print('\n[TEST 1] Multi-agent flow — REQUEST_REVIEW + REVISE_SECTION')
print('-' * 60)

env = PostMortemEnvironment(difficulty='easy')
result = env.reset()
assert result.observation.skeptic_critiques == []
assert result.observation.reviews_requested == 0
print('  reset() initializes multi-agent state correctly')

# REQUEST_REVIEW too early → should deny
result = env.step(Action(action_type=ActionType.REQUEST_REVIEW))
assert 'too early' in result.observation.last_action_result.lower()
print('  REQUEST_REVIEW denied before 2 sections written')

# Correct query for easy scenario
result = env.step(Action(
    action_type=ActionType.QUERY_LOGS,
    query_service='payments',
    query_from='03:38',
    query_to='03:43',
))
print(f'  [debug] QUERY_LOGS reward={result.reward.total:+.3f}')

# Summary — must mention 'payments' (a relevant service)
result = env.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.SUMMARY,
    section_content='The payments service experienced an outage from 03:41 to 04:09 affecting 1240 users. Root cause was a deployment bug in v2.4.0.',
))
assert 'validated' in result.observation.last_action_result, f'Summary rejected: {result.observation.last_action_result}'
print(f'  [debug] WRITE summary reward={result.reward.total:+.3f}')

result = env.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.ROOT_CAUSE,
    section_content='Root cause: the payments service v2.4.0 deployment introduced a connection leak in PaymentProcessor.charge() that exhausted the database connection pool under error conditions.',
))
assert 'validated' in result.observation.last_action_result, f'Root cause rejected: {result.observation.last_action_result}'
print(f'  [debug] WRITE root_cause reward={result.reward.total:+.3f}')

# Now REQUEST_REVIEW should work
result = env.step(Action(action_type=ActionType.REQUEST_REVIEW))
assert result.reward.total == 0.04, (
    f'Expected +0.04, got {result.reward.total}. '
    f'Result: {result.observation.last_action_result}'
)
assert len(result.observation.skeptic_critiques) == 1
print(f'  REQUEST_REVIEW succeeds after 2 sections (+0.04)')
print(f'     Critique preview: "{result.observation.skeptic_critiques[0][:80]}..."')

# REVISE_SECTION addressing the critique
result = env.step(Action(
    action_type=ActionType.REVISE_SECTION,
    section_name=SectionName.ROOT_CAUSE,
    section_content='REVISED — Root cause: payments service v2.4.0 deployment introduced a connection leak in PaymentProcessor.charge() that failed to close DB connections on exception paths. Confirmed via audit logs 03:38-03:43. Pool exhausted at 03:41, 3 minutes post-deploy.',
    critique_addressed_index=0,
))
assert result.reward.total == 0.06, (
    f'Expected +0.06, got {result.reward.total}. '
    f'Result: {result.observation.last_action_result}'
)
assert result.observation.critiques_addressed == 1
print(f'  REVISE_SECTION addresses critique (+0.06)')

# Duplicate revision → penalty
result = env.step(Action(
    action_type=ActionType.REVISE_SECTION,
    section_name=SectionName.ROOT_CAUSE,
    section_content='Another revision attempt with different enough text to pass the length check.',
    critique_addressed_index=0,
))
assert result.reward.total == -0.03, f'Expected -0.03, got {result.reward.total}'
print(f'  Duplicate revision penalized (-0.03)')

# ───────────────────────────────────────────────────────────
# TEST 2: Single-agent flow unchanged
# ───────────────────────────────────────────────────────────
print('\n[TEST 2] Single-agent flow — no REQUEST_REVIEW used')
print('-' * 60)

env2 = PostMortemEnvironment(difficulty='easy')
env2.reset()

env2.step(Action(
    action_type=ActionType.QUERY_LOGS,
    query_service='payments',
    query_from='03:38',
    query_to='03:43',
))

env2.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.SUMMARY,
    section_content='The payments service experienced an outage from 03:41 to 04:09 affecting 1240 users due to a v2.4.0 deployment bug.',
))

env2.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.TIMELINE,
    section_content='03:38 v2.4.0 deployed. 03:41 first connection errors. 03:43 pool exhausted. 03:47 on-call paged. 04:09 rollback completed.',
))

env2.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.ROOT_CAUSE,
    section_content='Root cause: payments service v2.4.0 deployment bug introduced a connection leak in PaymentProcessor.charge() that exhausted the database connection pool.',
))

env2.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.IMPACT,
    section_content='The payments service outage lasted 28 minutes from 03:41 until 04:09 recovery. Approximately 1240 users were affected during this time with 18600 USD in lost revenue. Customer checkout failed with payment error messages.',
))

env2.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.ACTION_ITEMS,
    section_content='Add connection pool monitoring to payments service before next deploy. Owner: backend-team. Due date: 2024-08-15. Also implement pre-deploy canary for v2.4.x releases.',
))

env2.step(Action(
    action_type=ActionType.ASSIGN_ACTION_ITEM,
    action_item_description='Add connection pool monitoring to payments service before next deploy.',
    action_item_owner='backend-team',
    action_item_due_date='2024-08-15',
))

result = env2.step(Action(action_type=ActionType.SUBMIT))
assert result.done
grade = result.info.get('grade')
assert grade is not None
assert grade['critiques_received'] == 0
assert grade['collaboration_score'] == 0.0
assert 'collaboration' not in grade['explanation']
print(f'  Single-agent episode completes normally')
print(f'     Final score: {grade["total_score"]:.3f}')

# ───────────────────────────────────────────────────────────
# TEST 3: Multi-agent grading
# ───────────────────────────────────────────────────────────
print('\n[TEST 3] Multi-agent episode — final grade with collaboration_score')
print('-' * 60)

env.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.TIMELINE,
    section_content='03:38 v2.4.0 deployed. 03:41 first errors. 03:43 pool exhausted. 04:09 rollback done.',
))
env.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.IMPACT,
    section_content='Payments service outage lasted 28 minutes from 03:41 to 04:09, affecting 1240 users and costing 18600 USD in lost revenue from failed checkout transactions.',
))
env.step(Action(
    action_type=ActionType.WRITE_SECTION,
    section_name=SectionName.ACTION_ITEMS,
    section_content='Add connection pool monitoring to payments before deploy. Owner: backend-team. Due: 2024-08-15. Also add canary for v2.4.x.',
))
env.step(Action(
    action_type=ActionType.ASSIGN_ACTION_ITEM,
    action_item_description='Add connection pool monitoring to payments service.',
    action_item_owner='backend-team',
    action_item_due_date='2024-08-15',
))

result = env.step(Action(action_type=ActionType.SUBMIT))
grade = result.info.get('grade')
assert grade['critiques_received'] == 1, f'Expected 1, got {grade["critiques_received"]}'
assert grade['critiques_addressed'] == 1
assert grade['collaboration_score'] == 1.0
assert 'collaboration' in grade['explanation']
print(f'  Multi-agent episode grade includes collaboration_score')
print(f'     critiques_received: {grade["critiques_received"]}')
print(f'     critiques_addressed: {grade["critiques_addressed"]}')
print(f'     collaboration_score: {grade["collaboration_score"]:.2f}')
print(f'     Final score (with +0.10 bonus): {grade["total_score"]:.3f}')

print('\n' + '=' * 60)
print(' ALL PHASE 1 TESTS PASSED')
print('=' * 60)
print()
print('What this proves:')
print('  1. REQUEST_REVIEW gates work correctly')
print('  2. Skeptic critique received (fallback mode - no API key)')
print('  3. REVISE_SECTION properly addresses critiques and rewards')
print('  4. Duplicate revisions are penalized')
print('  5. Single-agent flow is 100% backward compatible')
print('  6. Multi-agent mode adds collaboration_score as bonus')
print()
print('Next: set SKEPTIC_API_KEY env var to enable real Groq LLM skeptic.')
