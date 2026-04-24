"""
Pydantic models for the Incident Post-Mortem Writer OpenEnv environment.
Defines all typed Observation, Action, and Reward structures.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    WRITE_SECTION      = "WRITE_SECTION"
    QUERY_LOGS         = "QUERY_LOGS"
    ASSIGN_ACTION_ITEM = "ASSIGN_ACTION_ITEM"
    SUBMIT             = "SUBMIT"
    # Multi-agent extension — Phase 1
    REQUEST_REVIEW     = "REQUEST_REVIEW"
    REVISE_SECTION     = "REVISE_SECTION"


class SectionName(str, Enum):
    SUMMARY      = "summary"
    TIMELINE     = "timeline"
    ROOT_CAUSE   = "root_cause"
    IMPACT       = "impact"
    ACTION_ITEMS = "action_items"


class SectionState(str, Enum):
    UNWRITTEN       = "unwritten"
    WRITTEN_INVALID = "written_invalid"
    WRITTEN_VALID   = "written_valid"


class RootCauseCategory(str, Enum):
    NULL_REF            = "null_ref"
    TIMEOUT             = "timeout"
    MEMORY_LEAK         = "memory_leak"
    CONFIG_ERROR        = "config_error"
    DEPENDENCY_FAILURE  = "dependency_failure"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPLOYMENT_BUG      = "deployment_bug"
    NETWORK_FAILURE     = "network_failure"


# ---------------------------------------------------------------------------
# Sub-models used inside Observation
# ---------------------------------------------------------------------------

class AlertLog(BaseModel):
    timestamp: str = Field(..., description="ISO-like time string e.g. '03:41:05'")
    service:   str = Field(..., description="Service that fired the alert")
    severity:  str = Field(..., description="INFO | WARN | ERROR | CRITICAL")
    message:   str = Field(..., description="Alert message text")


class SlackMessage(BaseModel):
    timestamp: str
    author:    str
    text:      str


class ServiceDependency(BaseModel):
    service:  str = Field(..., description="Upstream service name")
    depends_on: List[str] = Field(default_factory=list,
                                   description="Services this one calls")


class SectionStatus(BaseModel):
    name:    SectionName
    state:   SectionState = SectionState.UNWRITTEN
    content: Optional[str] = None


class QueryRecord(BaseModel):
    service:    str
    from_time:  str
    to_time:    str
    was_correct: bool
    step:       int


class ActionItem(BaseModel):
    description: str
    owner:       str
    due_date:    str


# ---------------------------------------------------------------------------
# Core Observation — what the agent sees each step
# ---------------------------------------------------------------------------

class Observation(BaseModel):
    """Everything the agent can see at each step."""

    # Static incident data (always visible)
    goal: str = Field(..., description="Natural language goal for this episode")
    incident_id: str
    incident_title: str
    alerts: List[AlertLog] = Field(
        default_factory=list,
        description="Alert logs visible from the start (NOT the hidden evidence)"
    )
    slack_thread: List[SlackMessage] = Field(
        default_factory=list,
        description="Slack messages from the incident channel"
    )
    service_graph: List[ServiceDependency] = Field(
        default_factory=list,
        description="Dependency graph — which service calls which"
    )

    # Dynamic episode state
    step: int = 0
    max_steps: int = 25
    queries_used: int = 0
    max_queries: int = 8
    sections: List[SectionStatus] = Field(
        default_factory=list,
        description="Current state of all 5 postmortem sections"
    )
    action_items_assigned: List[ActionItem] = Field(default_factory=list)
    query_history: List[QueryRecord] = Field(default_factory=list)

    # Last step feedback
    last_action_result: Optional[str] = None
    last_reward: float = 0.0
    done: bool = False

    # Retrieved log lines (populated after a QUERY_LOGS call)
    retrieved_logs: Optional[List[AlertLog]] = Field(
        default=None,
        description="Log lines returned by the last QUERY_LOGS call. None if no query made yet."
    )

    # Multi-agent extension — Skeptic critiques (populated after REQUEST_REVIEW)
    skeptic_critiques: List[str] = Field(
        default_factory=list,
        description="Critiques from the skeptic agent on current post-mortem draft. Addressing these via REVISE_SECTION earns reward."
    )
    critiques_addressed: int = Field(
        default=0,
        description="Count of skeptic critiques the agent has addressed via REVISE_SECTION."
    )
    reviews_requested: int = Field(
        default=0,
        description="Total REQUEST_REVIEW calls made this episode (soft-capped at 3)."
    )


# ---------------------------------------------------------------------------
# Action — what the agent can do
# ---------------------------------------------------------------------------

class Action(BaseModel):
    """A single action the agent takes."""

    action_type: ActionType

    # For WRITE_SECTION
    section_name: Optional[SectionName] = None
    section_content: Optional[str] = Field(
        default=None,
        description="Full text content to write into the section"
    )

    # For QUERY_LOGS
    query_service: Optional[str] = Field(
        default=None,
        description="Service name to query logs for"
    )
    query_from: Optional[str] = Field(
        default=None,
        description="Start of time window, e.g. '03:42'"
    )
    query_to: Optional[str] = Field(
        default=None,
        description="End of time window, e.g. '03:45'"
    )

    # For ASSIGN_ACTION_ITEM
    action_item_description: Optional[str] = None
    action_item_owner: Optional[str] = None
    action_item_due_date: Optional[str] = None

    # For REVISE_SECTION (multi-agent extension)
    # Reuses section_name + section_content from WRITE_SECTION fields above.
    critique_addressed_index: Optional[int] = Field(
        default=None,
        description="Index into skeptic_critiques list that this revision addresses (0-based)."
    )


# ---------------------------------------------------------------------------
# Reward — returned alongside observation after each step
# ---------------------------------------------------------------------------

class RewardBreakdown(BaseModel):
    """Detailed breakdown so the agent (and judges) can see exactly why."""
    section_written:         float = 0.0
    correct_query:           float = 0.0
    action_item_assigned:    float = 0.0
    overwrite_penalty:       float = 0.0
    bad_query_penalty:       float = 0.0
    missing_section_penalty: float = 0.0
    no_submit_penalty:       float = 0.0
    # Multi-agent extension — Phase 1
    review_requested:        float = 0.0  # +0.04 for valid REQUEST_REVIEW
    critique_addressed:      float = 0.0  # +0.06 for REVISE_SECTION that addresses a critique
    spurious_revision:       float = 0.0  # -0.03 for REVISE_SECTION without an outstanding critique


class Reward(BaseModel):
    total: float = Field(..., description="Net reward this step")
    breakdown: RewardBreakdown
    cumulative: float = Field(..., description="Total reward so far this episode")


# ---------------------------------------------------------------------------
# Step result — what env.step() returns
# ---------------------------------------------------------------------------

class StepResult(BaseModel):
    observation: Observation
    reward: Reward
    done: bool
    info: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Final grader output — what env.grade() returns at SUBMIT
# ---------------------------------------------------------------------------

class GradeResult(BaseModel):
    total_score: float = Field(..., ge=0.0, le=1.0)

    # Sub-scores (all 0.0–1.0)
    root_cause_score:     float = 0.0
    timeline_score:       float = 0.0
    action_items_score:   float = 0.0
    impact_score:         float = 0.0
    completeness_score:   float = 0.0
    # Multi-agent extension — Phase 1
    collaboration_score:  float = 0.0  # 0.0 if no critiques addressed; 1.0 if all addressed

    # Modifiers
    timeline_root_cause_cap_applied: bool = False
    correct_queries_made: int = 0
    critiques_received:   int = 0
    critiques_addressed:  int = 0

    explanation: str = ""
