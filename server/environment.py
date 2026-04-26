"""
Incident Post-Mortem Writer — Core Environment Logic
Implements OpenEnv Environment base class with full step/reset/state API.
All 7 exploit fixes are implemented here.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

# OpenEnv base classes
try:
    from core.env_server import Environment
except ImportError:
    # Fallback base class for local dev without OpenEnv installed
    class Environment:
        def reset(self):
            raise NotImplementedError
        def step(self, action):
            raise NotImplementedError
        @property
        def state(self):
            raise NotImplementedError

from env.models import (
    Action, ActionType, ActionItem,
    AlertLog, GradeResult, Observation,
    QueryRecord, Reward, RewardBreakdown,
    SectionName, SectionState, SectionStatus,
    StepResult,
)

SCENARIOS_DIR = Path(__file__).parent.parent / "env" / "scenarios"

# ---------------------------------------------------------------------------
# Skeptic Agent — External LLM call for multi-agent review
# ---------------------------------------------------------------------------

import os
import urllib.request
import urllib.error

# Generic fallback critiques used when LLM is unavailable or returns garbage.
# Keeps the environment functional without API access.
_FALLBACK_CRITIQUES = [
    "Verify the service you named as root cause actually appears in the retrieved log evidence, not just in Slack opinions.",
    "Cross-check your timeline against the alert timestamps — are you missing events that happened before the first loud symptom?",
    "Your root cause should name the specific mechanism (e.g. deployment bug, config error, compromised credential) — not just the symptom.",
    "Consider whether any Slack messages are confidently wrong — authority figures can be mistaken.",
    "Your action items should be specific to the root cause, not generic monitoring improvements.",
]

_SKEPTIC_SYSTEM_PROMPT = """You are a senior SRE reviewing an incident post-mortem draft written by another engineer.
Your job is to identify SPECIFIC factual or reasoning problems in the draft.

Rules:
- Return ONE critique only, in 1-2 sentences.
- Be concrete: point to a specific claim, timestamp, or missing evidence.
- Do NOT rewrite the post-mortem. Only critique it.
- Do NOT acknowledge the draft is good. You are looking for issues.
- If the draft is genuinely clean, point out the weakest remaining claim anyway.
"""


def _call_skeptic_llm(current_sections: Dict[str, str], incident_title: str, alerts: List[Dict[str, Any]]) -> str:
    """Call an OpenAI-compatible LLM to generate a critique of current draft.

    Returns a single critique string (1-2 sentences).
    Falls back to a generic critique on any error — never raises.

    Configuration via env vars:
      SKEPTIC_API_BASE_URL  (default: https://api.groq.com/openai/v1)
      SKEPTIC_MODEL_NAME    (default: llama-3.1-8b-instant)
      SKEPTIC_API_KEY       (if unset, uses generic fallback)
    """
    api_key = os.environ.get("SKEPTIC_API_KEY") or os.environ.get("HF_TOKEN") or ""

    # No API key → return a varied generic fallback so agent still sees meaningful feedback
    if not api_key:
        # Pick a fallback based on how many sections have content (so repeat calls differ)
        written_count = sum(1 for v in current_sections.values() if v and v.strip())
        return _FALLBACK_CRITIQUES[written_count % len(_FALLBACK_CRITIQUES)]

    base_url = os.environ.get("SKEPTIC_API_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    model    = os.environ.get("SKEPTIC_MODEL_NAME", "llama-3.1-8b-instant")

    # Build compact prompt — keep under 2000 tokens to avoid rate limit
    draft = "\n".join(
        f"## {k.upper()}\n{v[:400] if v else '(not yet written)'}"
        for k, v in current_sections.items()
    )
    alerts_brief = "\n".join(
        f"[{a.get('timestamp','')}] {a.get('service','')}: {a.get('message','')[:100]}"
        for a in alerts[:8]
    )
    user_prompt = (
        f"INCIDENT: {incident_title}\n\n"
        f"RECENT ALERTS:\n{alerts_brief}\n\n"
        f"DRAFT POST-MORTEM:\n{draft}\n\n"
        "Provide ONE specific critique in 1-2 sentences."
    )

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SKEPTIC_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.0,   # try to minimize non-determinism
        "max_tokens": 150,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # User-Agent required to bypass Cloudflare WAF on api.groq.com
            # Without this, Cloudflare returns 403 with error code 1010 (bot detection)
            "User-Agent": "incident-postmortem-writer/1.0 (OpenEnv hackathon submission)",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if text and len(text) >= 20:
            return text
        # Garbage response → fallback
        return _FALLBACK_CRITIQUES[0]
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError, TimeoutError, Exception):
        return _FALLBACK_CRITIQUES[0]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time(t: str) -> int:
    """Convert 'HH:MM' or 'HH:MM:SS' to total minutes."""
    parts = t.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _window_overlap_minutes(from_q: str, to_q: str, from_w: str, to_w: str) -> int:
    """Return overlap in minutes between two time windows."""
    q_start, q_end = _parse_time(from_q), _parse_time(to_q)
    w_start, w_end = _parse_time(from_w), _parse_time(to_w)
    overlap = max(0, min(q_end, w_end) - max(q_start, w_start))
    return overlap


def _any_keyword(text: str, keywords: List[str]) -> bool:
    """Case-insensitive check for any keyword in text."""
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in keywords)


def _any_service(text: str, services: List[str]) -> bool:
    text_lower = text.lower()
    return any(s.lower() in text_lower for s in services)


def _count_timestamps(text: str) -> int:
    """Count time patterns like 03:41 or 14:02 in text."""
    return len(re.findall(r'\d{1,2}:\d{2}', text))


def _has_owner(text: str, known_teams: List[str]) -> bool:
    text_lower = text.lower().replace("-", " ").replace("_", " ")
    for t in known_teams:
        # Try exact match and fuzzy: "payments-team" matches "payments team" or "payments"
        t_norm = t.lower().replace("-", " ").replace("_", " ")
        if t_norm in text_lower:
            return True
        # Also match first word only (e.g. "payments" matches "payments-team")
        first_word = t_norm.split()[0]
        if len(first_word) >= 4 and first_word in text_lower:
            return True
    return False


def _has_due_date(text: str, patterns: List[str]) -> bool:
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Section validators — Fix 4: content validation before reward
# ---------------------------------------------------------------------------

def _validate_section(
    section_name: SectionName,
    content: str,
    scenario: dict,
) -> bool:
    """Returns True only if section content meets minimum requirements."""
    gs = scenario["gold_standard"]
    services = scenario["relevant_services"]

    if section_name == SectionName.SUMMARY:
        # Must mention at least one relevant service
        return _any_service(content, services)

    elif section_name == SectionName.ROOT_CAUSE:
        # Must mention a service AND a cause category keyword
        has_service = _any_service(content, scenario["service_graph_names"])
        has_category = _any_keyword(content, [
            "null", "timeout", "leak", "config", "deploy", "migration",
            "bug", "error", "crash", "failure", "exhaustion", "misconfigur",
            "schema", "TTL", "cache", "connection", "overflow",
            "compromised", "unauthorized", "breach", "stolen", "attacker",
            "credential", "tor", "api key", "api-key", "svc-reporting"
        ])
        # For security scenarios: accept security-specific identifiers as service context
        has_security_context = _any_keyword(content, [
            "api-gateway", "svc-reporting-prod", "compromised key",
            "stolen key", "185.220", "tor exit"
        ])
        return (has_service and has_category) or (has_security_context and has_category)

    elif section_name == SectionName.TIMELINE:
        # Must contain at least 3 timestamps
        return _count_timestamps(content) >= 3

    elif section_name == SectionName.IMPACT:
        # Must be at least 25 words AND mention a service or duration
        has_words = len(content.split()) >= 25
        has_service = _any_service(content, scenario.get("service_graph_names", []))
        has_time = bool(re.search(
            r'\b(\d+\s*(minute|hour|min|hr|second)s?|downtime|outage|unavailable|degraded|\d+)\b',
            content, re.IGNORECASE
        ))
        return has_words and (has_service or has_time)

    elif section_name == SectionName.ACTION_ITEMS:
        # Must mention an owner AND a due date pattern
        known_teams = gs.get("known_teams", [])
        due_patterns = gs.get("valid_due_date_patterns", [])
        return _has_owner(content, known_teams) and _has_due_date(content, due_patterns)

    return False


# ---------------------------------------------------------------------------
# Query evaluator — Fix 1 & 2: exact correct-query definition
# ---------------------------------------------------------------------------

def _evaluate_query(
    service: str,
    from_time: str,
    to_time: str,
    scenario: dict,
) -> tuple[bool, List[AlertLog]]:
    """
    Returns (is_correct, log_lines).
    Correct = service in relevant_services AND window overlaps evidence_window by >= required minutes.
    Fix 2: ALL three must be true — service match + window overlap + key evidence present.
    """
    relevant = scenario["relevant_services"]
    service_match = service.lower() in [s.lower() for s in relevant]

    # Gate: service must be in relevant_services to ever return correct
    # This ensures decoy evidence windows (like cdn) never grant +reward
    if not service_match:
        noise = [AlertLog(**l) for l in scenario.get("noise_logs", [])]
        return False, noise[:3]

    for window in scenario["evidence_windows"]:
        if window["service"].lower() != service.lower():
            continue
        overlap = _window_overlap_minutes(
            from_time, to_time,
            window["from_time"], window["to_time"]
        )
        required = window.get("overlap_required_minutes", 2)
        if overlap >= required:
            # Return the actual evidence logs
            logs = [AlertLog(**l) for l in window["logs"]]
            return True, logs

    # Correct service but wrong time window — return noise logs
    noise = [AlertLog(**l) for l in scenario.get("noise_logs", [])]
    return False, noise[:3]


# ---------------------------------------------------------------------------
# Grader — deterministic, 3-layer root cause, timeline matching
# ---------------------------------------------------------------------------

def _grade_submission(sections: Dict[str, str], scenario: dict) -> GradeResult:
    """
    Fully deterministic grader. Same inputs → same output always.
    Fix 3: timeline cap on root cause.
    Fix 2: 3-layer root cause scoring.
    """
    gs = scenario["gold_standard"]

    # ------------------------------------------------------------------
    # 1. Completeness (10%) — all 5 sections present and non-empty
    # ------------------------------------------------------------------
    required = {s.value for s in SectionName}
    present = {k for k, v in sections.items() if v and len(v.strip()) > 10}
    completeness = len(present & required) / len(required)

    # ------------------------------------------------------------------
    # 2. Timeline score (25%)
    # ------------------------------------------------------------------
    timeline_text = sections.get("timeline", "")
    gold_events = gs["timeline_events"]
    tolerance = gs.get("timeline_tolerance_minutes", 3)
    hidden_events = gs.get("hidden_timeline_events", [])
    correct_queries = scenario.get("_correct_queries_made", 0)
    matched = 0
    for event in gold_events:
        # Skip hidden events if no correct query was made
        if event["time"] in hidden_events and correct_queries == 0:
            continue
        gold_min = _parse_time(event["time"])
        found_times = re.findall(r'(\d{1,2}):(\d{2})', timeline_text)
        for h, m in found_times:
            candidate = int(h) * 60 + int(m)
            if abs(candidate - gold_min) <= tolerance:
                if _any_keyword(timeline_text, [event["service"], event["label"].split()[0]]):
                    matched += 1
                    break
    # Score against ALL events — hidden events count in denominator
    # Without correct query, agent can never match hidden events → lower score
    # With correct query, hidden events become matchable → higher score
    timeline_score = min(matched / max(len(gold_events), 1), 1.0)

    # ------------------------------------------------------------------
    # 3. Root cause score (30%) — 3-layer
    # Fix 2: service(0.4) + category(0.35) + keyword(0.25)
    # Fix 3: cap at 0.6 if timeline < 0.4
    # ------------------------------------------------------------------
    rc_text = sections.get("root_cause", "")
    rc_gold = gs["root_cause"]

    # Layer 1: correct service (0.40)
    # Match full name (redis-auth) OR first component (redis) OR last component (auth if unique)
    gold_service = rc_gold["service"]
    service_variants = [gold_service]
    if "-" in gold_service:
        parts = gold_service.split("-")
        # Only add first part if specific enough (not generic words like api, db, web)
        generic_words = ["api", "auth", "db", "web", "app", "data"]
        if parts[0] not in generic_words:
            service_variants.append(parts[0])
        # Only add last part if unique (not generic)
        if parts[-1] not in ["auth", "db", "service", "api", "cache", "gateway"]:
            service_variants.append(parts[-1])
    layer1 = 0.40 if _any_service(rc_text, service_variants) else 0.0

    # But penalize if a false root cause service is ALSO mentioned prominently
    # and the real service is only mentioned as secondary
    false_causes = gs.get("false_root_causes", [])
    if layer1 > 0 and false_causes:
        for fc in false_causes:
            fc_svc = fc["service"]
            rc_lower = rc_text.lower()
            # If false cause appears before real cause in text, reduce L1
            real_pos = rc_lower.find(gold_service.split("-")[0].lower())
            false_pos = rc_lower.find(fc_svc.lower())
            if false_pos != -1 and real_pos != -1 and false_pos < real_pos:
                # False cause mentioned first — likely primary blame
                layer1 = 0.15  # Partial credit only

    # Layer 2: cause category (0.35)
    category_keywords = {
        "null_ref":            ["null", "npe", "nullpointer", "uninitialized"],
        "timeout":             ["timeout", "timed out", "latency", "slow"],
        "memory_leak":         ["memory", "leak", "oom", "heap"],
        "config_error":        ["config", "misconfigur", "TTL", "setting", "parameter"],
        "dependency_failure":  ["dependency", "upstream", "downstream", "cascade"],
        "resource_exhaustion": ["exhaustion", "pool", "capacity", "connections"],
        "deployment_bug":      ["deploy", "release", "version", "migration", "schema", "v2", "v14", "v15"],
        "network_failure":     ["network", "dns", "packet", "route"],
        "security_breach":     ["breach", "compromised", "unauthorized", "exfiltration", "attacker", "tor", "stolen", "credential"],
    }
    gold_cat = rc_gold["category"]
    cat_kws = category_keywords.get(gold_cat, [])
    layer2 = 0.35 if _any_keyword(rc_text, cat_kws) else 0.0

    # Layer 3: specific keywords (0.25)
    layer3 = 0.25 if _any_keyword(rc_text, rc_gold["keywords"]) else 0.0

    raw_rc_score = layer1 + layer2 + layer3

    # Fix 3: timeline dependency cap
    timeline_cap_applied = False
    if timeline_score < 0.4:
        raw_rc_score = min(raw_rc_score, 0.6)
        timeline_cap_applied = True

    # L1 cap: if correct service not identified, cap root cause at 0.65
    if layer1 == 0.0:
        raw_rc_score = min(raw_rc_score, 0.65)

    # Track correct queries for timeline hidden events
    correct_queries = scenario.get("_correct_queries_made", 0)

    # Evidence gate for expert difficulty: root cause requires correct query
    # Expert scenario has specific log evidence that cannot be deduced from Slack alone
    if scenario.get("difficulty") == "expert" and correct_queries == 0:
        # Without querying the right window, agent is guessing from Slack
        # Cap L1 to prevent lucky guesses from scoring full root cause
        if layer1 > 0:
            layer1 = 0.10  # Heavy penalty — found service name in Slack but no evidence
        raw_rc_score = layer1 + layer2 + layer3
        raw_rc_score = min(raw_rc_score, 0.45)  # Hard cap at 0.45

    # Additional penalty: if ONLY false cause mentioned (no real service at all)
    false_causes = gs.get("false_root_causes", [])
    for fc in false_causes:
        if _any_service(rc_text, [fc["service"]]):
            if not _any_service(rc_text, service_variants):
                raw_rc_score *= 0.35

    # ------------------------------------------------------------------
    # 4. Impact score (15%)
    # ------------------------------------------------------------------
    impact_text = sections.get("impact", "")
    impact_score = 0.0

    # Layer 1 (0.25): minimum word count — real impact statements are substantive
    if len(impact_text.split()) >= 25:
        impact_score += 0.25

    # Layer 2 (0.25): must mention affected service by name
    impact_services = scenario.get("relevant_services", []) + scenario.get("service_graph_names", [])
    if _any_service(impact_text, impact_services):
        impact_score += 0.25

    # Layer 3 (0.25): must mention duration or time (minutes, hours, downtime, outage)
    has_duration = bool(re.search(
        r'\b(\d+\s*(minute|hour|min|hr|second)s?|downtime|outage|unavailable|degraded)\b',
        impact_text, re.IGNORECASE
    ))
    if has_duration:
        impact_score += 0.25

    # Layer 4 (0.25): must mention scale — users, customers, revenue, requests, or a number + unit
    has_scale = bool(re.search(
        r'\b(user|customer|request|revenue|transaction|\$|dollar|affected|impact)\b',
        impact_text, re.IGNORECASE
    )) and bool(re.search(r'\d+', impact_text))
    if has_scale:
        impact_score += 0.25

    impact_score = min(impact_score, 1.0)

    # ------------------------------------------------------------------
    # 5. Action items score (20%)
    # ------------------------------------------------------------------
    ai_text = sections.get("action_items", "")
    known_teams = gs.get("known_teams", [])
    due_patterns = gs.get("valid_due_date_patterns", [])
    required_themes = gs.get("required_action_item_themes", [])

    ai_score = 0.0
    if _has_owner(ai_text, known_teams):
        ai_score += 0.4
    if _has_due_date(ai_text, due_patterns):
        ai_score += 0.3
    theme_hits = sum(1 for t in required_themes if _any_keyword(ai_text, t.split()))
    ai_score += 0.3 * min(theme_hits / max(len(required_themes), 1), 1.0)
    ai_score = min(ai_score, 1.0)

    # ------------------------------------------------------------------
    # Multi-agent extension — collaboration score
    # ------------------------------------------------------------------
    critiques_received  = scenario.get("_critiques_received", 0)
    critiques_addressed = scenario.get("_critiques_addressed", 0)
    if critiques_received > 0:
        # Ratio of critiques addressed, capped at 1.0
        collaboration_score = min(critiques_addressed / critiques_received, 1.0)
    else:
        # No critiques requested → neutral (doesn't help or hurt)
        # We pass neutral 0.0 here but the weighting below reallocates its weight
        # to other components so single-agent episodes aren't penalized.
        collaboration_score = 0.0

    # ------------------------------------------------------------------
    # Weighted total
    # ------------------------------------------------------------------
    # If no critiques were requested (single-agent mode), use original weights.
    # If critiques WERE requested (multi-agent mode), add collaboration_score
    # as a bonus that can push the total up by up to +0.10 (capped at 1.0).
    if critiques_received > 0:
        # Multi-agent mode: collaboration_score contributes up to 10% bonus
        total = (
            raw_rc_score   * 0.30 +
            timeline_score * 0.25 +
            ai_score       * 0.20 +
            impact_score   * 0.15 +
            completeness   * 0.10
        ) + (collaboration_score * 0.10)   # bonus on top
    else:
        # Single-agent mode: unchanged from before
        total = (
            raw_rc_score   * 0.30 +
            timeline_score * 0.25 +
            ai_score       * 0.20 +
            impact_score   * 0.15 +
            completeness   * 0.10
        )
    total = round(min(max(total, 0.0), 1.0), 4)

    if critiques_received > 0:
        explanation = (
            f"root_cause={raw_rc_score:.2f}(L1={layer1:.2f},L2={layer2:.2f},L3={layer3:.2f}) "
            f"timeline={timeline_score:.2f}({matched}/{len(gold_events)} events) "
            f"action_items={ai_score:.2f} impact={impact_score:.2f} "
            f"completeness={completeness:.2f} "
            f"collaboration={collaboration_score:.2f}({critiques_addressed}/{critiques_received} critiques)"
        )
    else:
        explanation = (
            f"root_cause={raw_rc_score:.2f}(L1={layer1:.2f},L2={layer2:.2f},L3={layer3:.2f}) "
            f"timeline={timeline_score:.2f}({matched}/{len(gold_events)} events) "
            f"action_items={ai_score:.2f} impact={impact_score:.2f} "
            f"completeness={completeness:.2f}"
        )

    return GradeResult(
        total_score=total,
        root_cause_score=raw_rc_score,
        timeline_score=timeline_score,
        action_items_score=ai_score,
        impact_score=impact_score,
        completeness_score=completeness,
        collaboration_score=collaboration_score,
        timeline_root_cause_cap_applied=timeline_cap_applied,
        critiques_received=critiques_received,
        critiques_addressed=critiques_addressed,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Main Environment Class
# ---------------------------------------------------------------------------

class PostMortemEnvironment(Environment):
    """
    Incident Post-Mortem Writer OpenEnv Environment.
    Manages episode state, action dispatch, reward shaping, and grading.
    """

    SCENARIOS = {
        "easy":   "easy.json",
        "medium": "medium.json",
        "hard":   "hard.json",
        "expert": "expert.json",
    }

    def __init__(self, difficulty: str = "easy"):
        assert difficulty in self.SCENARIOS, f"difficulty must be one of {list(self.SCENARIOS)}"
        self.difficulty = difficulty
        self._scenario: dict = {}
        self._obs: Optional[Observation] = None
        self._cumulative_reward: float = 0.0
        self._section_states: Dict[str, SectionState] = {}
        self._written_sections: Dict[str, str] = {}
        self._query_count: int = 0
        self._wrong_query_count: int = 0
        self._correct_queries_made: int = 0
        self._step_count: int = 0
        self._done: bool = False
        self._grade_result: Optional[GradeResult] = None

    # ------------------------------------------------------------------
    # OpenEnv API
    # ------------------------------------------------------------------

    def reset(self) -> StepResult:
        """Start a fresh episode. Returns initial observation."""
        scenario_path = SCENARIOS_DIR / self.SCENARIOS[self.difficulty]
        with open(scenario_path) as f:
            self._scenario = json.load(f)

        # Enrich scenario with derived data
        self._scenario["service_graph_names"] = [
            s["service"] for s in self._scenario["service_graph"]
        ]

        # Reset all state
        self._cumulative_reward = 0.0
        self._section_states = {s.value: SectionState.UNWRITTEN for s in SectionName}
        self._written_sections = {s.value: "" for s in SectionName}
        self._query_count = 0
        self._wrong_query_count = 0
        self._correct_queries_made = 0
        self._step_count = 0
        self._done = False
        self._grade_result = None
        # Multi-agent extension — Phase 1
        self._skeptic_critiques: List[str] = []
        self._critiques_addressed_indices: set = set()   # which critique indices were addressed
        self._reviews_requested = 0
        self._max_reviews = 3   # soft cap on REQUEST_REVIEW calls

        obs = self._build_observation(
            last_action_result="Episode started. Read the alerts and Slack thread carefully. Use QUERY_LOGS to find hidden evidence before writing sections.",
            retrieved_logs=None,
        )
        self._obs = obs

        return StepResult(
            observation=obs,
            reward=Reward(
                total=0.0,
                breakdown=RewardBreakdown(),
                cumulative=0.0,
            ),
            done=False,
            info={"difficulty": self.difficulty, "scenario_id": self._scenario["scenario_id"]},
        )

    def step(self, action: Action) -> StepResult:
        """Execute one action. Returns (observation, reward, done, info)."""
        if self._done:
            return StepResult(
                observation=self._obs,
                reward=Reward(total=0.0, breakdown=RewardBreakdown(), cumulative=self._cumulative_reward),
                done=True,
                info={"message": "Episode already done. Call reset() to start a new episode."},
            )

        self._step_count += 1
        breakdown = RewardBreakdown()
        result_msg = ""
        retrieved_logs = None

        # ----------------------------------------------------------------
        # Dispatch action
        # ----------------------------------------------------------------

        if action.action_type == ActionType.QUERY_LOGS:
            result_msg, retrieved_logs, breakdown = self._handle_query(action, breakdown)

        elif action.action_type == ActionType.WRITE_SECTION:
            result_msg, breakdown = self._handle_write_section(action, breakdown)

        elif action.action_type == ActionType.ASSIGN_ACTION_ITEM:
            result_msg, breakdown = self._handle_assign_action_item(action, breakdown)

        elif action.action_type == ActionType.SUBMIT:
            result_msg, breakdown = self._handle_submit(breakdown)

        # Multi-agent extension — Phase 1
        elif action.action_type == ActionType.REQUEST_REVIEW:
            result_msg, breakdown = self._handle_request_review(breakdown)

        elif action.action_type == ActionType.REVISE_SECTION:
            result_msg, breakdown = self._handle_revise_section(action, breakdown)

        else:
            result_msg = f"Unknown action type: {action.action_type}"

        # ----------------------------------------------------------------
        # Episode termination — Fix 6: bounded episode
        # ----------------------------------------------------------------
        if self._step_count >= 25 and not self._done:
            # Auto-submit with penalty
            if not self._done:
                breakdown.no_submit_penalty = -0.10
                self._apply_submit_grading(breakdown)
                result_msg += " | MAX STEPS REACHED — auto-submitted with penalty."

        # ----------------------------------------------------------------
        # Compute total reward this step
        # ----------------------------------------------------------------
        step_reward = (
            (breakdown.section_written       or 0.0)
            + (breakdown.correct_query       or 0.0)
            + (breakdown.action_item_assigned or 0.0)
            + (breakdown.overwrite_penalty   or 0.0)
            + (breakdown.bad_query_penalty   or 0.0)
            + (breakdown.missing_section_penalty or 0.0)
            + (breakdown.no_submit_penalty   or 0.0)
            # Multi-agent extension
            + (breakdown.review_requested    or 0.0)
            + (breakdown.critique_addressed  or 0.0)
            + (breakdown.spurious_revision   or 0.0)
        )
        step_reward = float(step_reward) if step_reward is not None else 0.0
        self._cumulative_reward = round(self._cumulative_reward + step_reward, 4)

        obs = self._build_observation(
            last_action_result=result_msg,
            retrieved_logs=retrieved_logs,
        )
        self._obs = obs

        reward = Reward(
            total=round(step_reward, 4),
            breakdown=breakdown,
            cumulative=self._cumulative_reward,
        )

        info: Dict[str, Any] = {
            "step": self._step_count,
            "queries_used": self._query_count,
            "sections_valid": sum(
                1 for s in self._section_states.values()
                if s == SectionState.WRITTEN_VALID
            ),
        }
        if self._grade_result:
            info["grade"] = self._grade_result.dict()

        return StepResult(
            observation=obs,
            reward=reward,
            done=self._done,
            info=info,
        )

    @property
    def state(self) -> dict:
        """Return full current episode state. Used by GET /state."""
        return {
            "difficulty":        self.difficulty,
            "scenario_id":       self._scenario.get("scenario_id", ""),
            "step":              self._step_count,
            "done":              self._done,
            "cumulative_reward": self._cumulative_reward,
            "queries_used":      self._query_count,
            "section_states":    self._section_states,
            "grade":             self._grade_result.dict() if self._grade_result else None,
        }

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _handle_query(
        self, action: Action, breakdown: RewardBreakdown
    ) -> tuple[str, Optional[List[AlertLog]], RewardBreakdown]:
        """Fix 1: hard cap + escalating penalties. Fix 2: exact correct-query definition."""
        max_q = self._scenario["query_limits"]["max_queries"]
        penalty_schedule = self._scenario["query_limits"]["penalty_schedule"]

        if self._query_count >= max_q:
            return (
                f"Query limit reached ({max_q} queries used). No more queries allowed.",
                None,
                breakdown,
            )

        self._query_count += 1

        is_correct, logs = _evaluate_query(
            service=action.query_service or "",
            from_time=action.query_from or "00:00",
            to_time=action.query_to or "00:00",
            scenario=self._scenario,
        )

        record = QueryRecord(
            service=action.query_service or "",
            from_time=action.query_from or "",
            to_time=action.query_to or "",
            was_correct=is_correct,
            step=self._step_count,
        )

        if is_correct:
            self._correct_queries_made += 1
            breakdown.correct_query = 0.06
            msg = (
                f"QUERY HIT [last_query_result: relevant] — Retrieved {len(logs)} log lines from "
                f"{action.query_service} [{action.query_from}–{action.query_to}]. "
                f"Key evidence found! Tip: use this evidence to write root_cause and timeline sections."
            )
        else:
            # Fix 1: escalating penalty
            penalty_idx = min(self._wrong_query_count, len(penalty_schedule) - 1)
            penalty = -penalty_schedule[penalty_idx]
            self._wrong_query_count += 1
            breakdown.bad_query_penalty = penalty
            msg = (
                f"QUERY MISS [last_query_result: irrelevant] — No relevant evidence in "
                f"{action.query_service} [{action.query_from}–{action.query_to}]. "
                f"Penalty: {penalty:+.2f} (wrong query #{self._wrong_query_count}). "
                f"Tip: try a different service or time window closer to when the incident started."
            )

        return msg, logs, breakdown

    def _handle_write_section(
        self, action: Action, breakdown: RewardBreakdown
    ) -> tuple[str, RewardBreakdown]:
        """Fix 4: content validation. Fix 5: only first valid write rewarded."""
        if not action.section_name or not action.section_content:
            return "WRITE_SECTION requires section_name and section_content.", breakdown

        sname = action.section_name.value
        content = action.section_content.strip()
        current_state = self._section_states.get(sname, SectionState.UNWRITTEN)

        # Fix 5: overwrite penalty if already valid
        if current_state == SectionState.WRITTEN_VALID:
            breakdown.overwrite_penalty = -0.02
            self._written_sections[sname] = content  # Still update content
            return (
                f"Section '{sname}' was already valid. Overwrite accepted but penalised (−0.02). "
                f"No additional reward.",
                breakdown,
            )

        # Validate content
        is_valid = _validate_section(action.section_name, content, self._scenario)

        if is_valid:
            self._section_states[sname] = SectionState.WRITTEN_VALID
            self._written_sections[sname] = content
            breakdown.section_written = 0.03
            return (
                f"Section '{sname}' written and validated ✓ (+0.03). "
                f"Sections complete: {sum(1 for s in self._section_states.values() if s == SectionState.WRITTEN_VALID)}/5",
                breakdown,
            )
        else:
            self._section_states[sname] = SectionState.WRITTEN_INVALID
            self._written_sections[sname] = content
            return (
                f"Section '{sname}' written but FAILED validation. "
                f"No reward. Check: summary needs a service name, "
                f"root_cause needs service+cause type, timeline needs 3+ timestamps, "
                f"impact needs 20+ words, action_items needs owner+due date.",
                breakdown,
            )

    def _handle_assign_action_item(
        self, action: Action, breakdown: RewardBreakdown
    ) -> tuple[str, RewardBreakdown]:
        """Reward structured action item assignment."""
        gs = self._scenario["gold_standard"]
        known_teams = gs.get("known_teams", [])
        due_patterns = gs.get("valid_due_date_patterns", [])

        has_owner = bool(action.action_item_owner) and _has_owner(
            action.action_item_owner, known_teams
        )
        has_due = bool(action.action_item_due_date) and _has_due_date(
            action.action_item_due_date, due_patterns
        )
        has_desc = bool(action.action_item_description) and len(action.action_item_description) > 10

        if has_owner and has_due and has_desc:
            breakdown.action_item_assigned = 0.08
            return (
                f"Action item assigned ✓ (+0.08): '{action.action_item_description}' "
                f"→ {action.action_item_owner} by {action.action_item_due_date}",
                breakdown,
            )
        else:
            missing = []
            if not has_desc:   missing.append("description (>10 chars)")
            if not has_owner:  missing.append(f"valid owner (use one of: {known_teams[:3]}...)")
            if not has_due:    missing.append("due date (e.g. '2024-08-01' or 'next sprint')")
            return f"Action item incomplete. Missing: {', '.join(missing)}. No reward.", breakdown

    # ------------------------------------------------------------------
    # Multi-agent extension — REQUEST_REVIEW handler
    # ------------------------------------------------------------------

    def _handle_request_review(self, breakdown: RewardBreakdown) -> tuple[str, RewardBreakdown]:
        """Agent asks skeptic to critique current draft. Calls external LLM."""
        # Soft cap on review requests to prevent spam
        if self._reviews_requested >= self._max_reviews:
            return (
                f"REQUEST_REVIEW denied — already at max ({self._max_reviews}) reviews this episode. "
                f"Address existing critiques via REVISE_SECTION instead.",
                breakdown,
            )

        # Must have at least 2 sections written before review makes sense
        written_count = sum(
            1 for k, state in self._section_states.items()
            if state == SectionState.WRITTEN_VALID
        )
        if written_count < 2:
            return (
                f"REQUEST_REVIEW too early — only {written_count} section(s) written. "
                f"Write at least 2 sections first (e.g. root_cause + timeline).",
                breakdown,
            )

        # Call skeptic
        critique = _call_skeptic_llm(
            current_sections=self._written_sections,
            incident_title=self._scenario.get("incident_title", ""),
            alerts=self._scenario.get("initial_alerts", []),
        )

        self._skeptic_critiques.append(critique)
        self._reviews_requested += 1
        breakdown.review_requested = 0.04

        preview = critique[:140] + ("..." if len(critique) > 140 else "")
        return (
            f"Skeptic critique #{len(self._skeptic_critiques)} received (+0.04): {preview}",
            breakdown,
        )

    # ------------------------------------------------------------------
    # Multi-agent extension — REVISE_SECTION handler
    # ------------------------------------------------------------------

    def _handle_revise_section(self, action: Action, breakdown: RewardBreakdown) -> tuple[str, RewardBreakdown]:
        """Agent revises a section in response to a skeptic critique."""
        # Must have received at least one critique
        outstanding = [
            i for i in range(len(self._skeptic_critiques))
            if i not in self._critiques_addressed_indices
        ]
        if not self._skeptic_critiques:
            breakdown.spurious_revision = -0.03
            return (
                "REVISE_SECTION called with no critiques received (-0.03). "
                "Use REQUEST_REVIEW first, then address the critique here.",
                breakdown,
            )
        if not outstanding:
            breakdown.spurious_revision = -0.03
            return (
                "REVISE_SECTION called but all critiques already addressed (-0.03). "
                "Use REQUEST_REVIEW for a fresh critique, or SUBMIT.",
                breakdown,
            )

        # Validate section inputs
        if not action.section_name or not action.section_content:
            return (
                "REVISE_SECTION requires both section_name and section_content. No reward.",
                breakdown,
            )
        section_key = action.section_name.value
        if self._section_states.get(section_key) != SectionState.WRITTEN_VALID:
            return (
                f"Cannot revise '{section_key}' — section not yet written and validated. "
                f"Use WRITE_SECTION first.",
                breakdown,
            )

        # Determine which critique is being addressed
        idx = action.critique_addressed_index
        if idx is None:
            idx = outstanding[0]  # default: first outstanding
        if idx < 0 or idx >= len(self._skeptic_critiques):
            breakdown.spurious_revision = -0.03
            return (
                f"critique_addressed_index={idx} out of range (-0.03). "
                f"Valid range: 0..{len(self._skeptic_critiques)-1}.",
                breakdown,
            )
        if idx in self._critiques_addressed_indices:
            breakdown.spurious_revision = -0.03
            return (
                f"Critique #{idx} already addressed (-0.03). Outstanding critiques: {outstanding}.",
                breakdown,
            )

        # Check the revision actually changed the section meaningfully
        old_content = self._written_sections.get(section_key, "")
        new_content = (action.section_content or "").strip()
        if not new_content:
            return (
                "REVISE_SECTION section_content is empty. No change made.",
                breakdown,
            )
        # Require at least 30 chars difference to count as substantive revision
        # (prevents tiny tweaks farming reward)
        if len(new_content) < 30 or new_content == old_content:
            return (
                "Revision too small or identical to prior content. No reward.",
                breakdown,
            )

        # Accept the revision
        self._written_sections[section_key] = new_content[:2000]
        self._critiques_addressed_indices.add(idx)
        breakdown.critique_addressed = 0.06

        return (
            f"Critique #{idx} addressed via {section_key} revision (+0.06). "
            f"{len(self._critiques_addressed_indices)}/{len(self._skeptic_critiques)} critiques resolved.",
            breakdown,
        )

    def _handle_submit(self, breakdown: RewardBreakdown) -> tuple[str, RewardBreakdown]:
        """Run final grader on submitted sections."""
        # Penalty for any missing sections
        missing = [
            s for s, state in self._section_states.items()
            if state != SectionState.WRITTEN_VALID
        ]
        if missing:
            breakdown.missing_section_penalty = -0.10 * len(missing)

        self._apply_submit_grading(breakdown)
        grade = self._grade_result

        msg = (
            f"SUBMITTED ✓ | Final score: {grade.total_score:.3f} | "
            f"root_cause={grade.root_cause_score:.2f} "
            f"timeline={grade.timeline_score:.2f} "
            f"action_items={grade.action_items_score:.2f} "
            f"impact={grade.impact_score:.2f} "
            f"completeness={grade.completeness_score:.2f} | "
            f"{grade.explanation}"
        )
        return msg, breakdown

    def _apply_submit_grading(self, breakdown: RewardBreakdown) -> None:
        """Run the grader and set done=True."""
        grading_scenario = dict(self._scenario)
        grading_scenario["_correct_queries_made"] = self._correct_queries_made
        # Multi-agent extension — pass critique tracking to grader
        grading_scenario["_critiques_received"]   = len(self._skeptic_critiques)
        grading_scenario["_critiques_addressed"]  = len(self._critiques_addressed_indices)
        self._grade_result = _grade_submission(self._written_sections, grading_scenario)
        # Add grader score to cumulative (it's the bulk of the final score)
        self._cumulative_reward = round(
            self._cumulative_reward + self._grade_result.total_score, 4
        )
        self._done = True

    # ------------------------------------------------------------------
    # Observation builder
    # ------------------------------------------------------------------

    def _build_observation(
        self,
        last_action_result: str,
        retrieved_logs: Optional[List[AlertLog]],
    ) -> Observation:
        sc = self._scenario
        sections = [
            SectionStatus(
                name=SectionName(k),
                state=SectionState(v),
                content=self._written_sections.get(k),
            )
            for k, v in self._section_states.items()
        ]

        from env.models import SlackMessage, ServiceDependency
        return Observation(
            goal=sc.get("goal", ""),
            incident_id=sc.get("incident_id", ""),
            incident_title=sc.get("incident_title", ""),
            alerts=[AlertLog(**a) for a in sc.get("initial_alerts", [])],
            slack_thread=[SlackMessage(**m) for m in sc.get("slack_thread", [])],
            service_graph=[ServiceDependency(**s) for s in sc.get("service_graph", [])],
            step=self._step_count,
            max_steps=25,
            queries_used=self._query_count,
            max_queries=sc["query_limits"]["max_queries"],
            sections=sections,
            query_history=[],
            last_action_result=last_action_result,
            last_reward=self._cumulative_reward,
            done=self._done,
            retrieved_logs=retrieved_logs,
            # Multi-agent extension
            skeptic_critiques=list(self._skeptic_critiques),
            critiques_addressed=len(self._critiques_addressed_indices),
            reviews_requested=self._reviews_requested,
        )
