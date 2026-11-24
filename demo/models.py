"""Data shapes passed between the API, workflows and activities.

Plain dataclasses so Temporal's default data converter can serialize them.
This module must stay free of I/O: workflow code imports it.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DemoOptions:
    """Demo knobs captured when a request starts (kept in workflow history)."""

    auto_approve_seconds: int = 0  # 0 = wait for a person
    approval_sla_seconds: int = 60  # escalate after this long; 0 = never
    max_attempts: int = 6  # per step, for retryable failures
    heartbeat_seconds: int = 8  # how fast a dead worker is noticed
    # "track.step" keys that retry until they succeed (set when bug mode is on),
    # so a code bug waits for a deployed fix instead of escalating to a person.
    retry_forever: List[str] = field(default_factory=list)
    # Runtime for the firewall-rule agent: "plain" (no framework), "langchain"
    # or "langgraph". See demo/frameworks.
    agent_mode: str = "plain"


@dataclass
class VendingInput:
    request_id: str
    request_text: str
    requester: str
    demo: DemoOptions = field(default_factory=DemoOptions)


@dataclass
class StepSpec:
    id: str
    name: str
    system: str
    system_label: str
    location: str  # "cloud" | "onprem"
    task_queue: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    seconds: float = 3.0
    undo: Optional[str] = None
    kind: str = "action"  # "action" (one activity) | "agent" (an agent child workflow)
    agent: Optional[str] = None  # agent id when kind == "agent"
    reviewer: Optional[str] = None  # who reviews the agent's output


@dataclass
class TrackSpec:
    id: str
    name: str
    team: str
    depends_on: List[str] = field(default_factory=list)
    approval: Optional[str] = None
    reason: str = ""
    steps: List[StepSpec] = field(default_factory=list)


@dataclass
class ApprovalSpec:
    id: str
    name: str
    role: str
    approver: str
    escalation: str
    blocks: str  # "request" or a track id
    reason: str = ""
    auto: bool = False
    auto_reason: str = ""


@dataclass
class SkippedItem:
    id: str
    name: str
    reason: str


@dataclass
class Plan:
    tracks: List[TrackSpec] = field(default_factory=list)
    approvals: List[ApprovalSpec] = field(default_factory=list)
    skipped: List[SkippedItem] = field(default_factory=list)


@dataclass
class IntakeInput:
    request_id: str
    request_text: str
    requester: str


@dataclass
class PlanInput:
    request_id: str
    spec: Dict[str, Any]


@dataclass
class TrackInput:
    request_id: str
    parent_workflow_id: str
    track: TrackSpec
    spec: Dict[str, Any]
    context: Dict[str, Any]
    max_attempts: int = 6
    heartbeat_seconds: int = 8
    downstream: List[str] = field(default_factory=list)  # tracks waiting on this one
    retry_forever: List[str] = field(default_factory=list)
    agent_mode: str = "plain"


@dataclass
class StepInput:
    request_id: str
    track_id: str
    step: StepSpec
    spec: Dict[str, Any]
    context: Dict[str, Any]
    # Set when undoing a step: the output the step produced originally.
    original_output: Optional[Dict[str, Any]] = None


@dataclass
class StepResult:
    step_id: str
    status: str  # "completed" | "rolled_back"
    output: Dict[str, Any] = field(default_factory=dict)
    worker: str = ""
    attempt: int = 1
    duration_ms: int = 0
    log: List[str] = field(default_factory=list)


@dataclass
class TrackResult:
    track_id: str
    status: str  # "completed" | "rolled_back" | "aborted"
    steps: List[StepResult] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class ApprovalDecision:
    approval_id: str
    decision: str  # "approved" | "rejected"
    approver: str = ""
    comment: str = ""


@dataclass
class Resolution:
    action: str  # "retry" | "skip" | "rollback"
    operator: str = ""
    note: str = ""


@dataclass
class AgentInput:
    request_id: str
    parent_workflow_id: str  # the track workflow that started this agent; "" in the Agent lab
    agent: str  # e.g. "firewall_rules"
    spec: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    reviewer: str = ""
    mode: str = "plain"  # "plain" (no framework) | "langchain" | "langgraph"
    max_turns: int = 10
    heartbeat_seconds: int = 10  # a worker killed mid-turn is noticed this fast


@dataclass
class AgentResult:
    status: str  # "approved" | "rejected" | "failed"
    rules: List[Dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    reviewer: str = ""
    comment: str = ""
    rounds: int = 1
    llm_calls: int = 0
    tool_calls: int = 0


@dataclass
class ReviewDecision:
    decision: str  # "approve" | "changes" | "reject"
    reviewer: str = ""
    comment: str = ""


TERMINAL_TRACK_STATES = {"completed", "skipped", "rolled_back", "cancelled", "failed"}
TERMINAL_REQUEST_STATES = {"completed", "rejected", "rolled_back", "cancelled", "failed"}
