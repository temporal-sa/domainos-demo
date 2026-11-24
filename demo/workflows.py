"""Workflows for the account vending demo.

AccountVendingWorkflow (one per request, the "pizza tracker")
    intake -> approvals -> provisioning (fan-out DAG of child workflows)
    -> verification -> delivered
    Rejection, cancellation or an operator's "roll back" undoes completed work
    in reverse order (saga).

ProvisioningTrackWorkflow (one per team)
    Runs that team's steps in order. A step that fails for good does not fail
    the workflow: the track pauses, Claude diagnoses the failure, and an
    operator chooses retry, skip or roll back (a Workflow Update).

FirewallRuleAgentWorkflow (one per agent run, started by the firewall track)
    A Claude agent with tools, no framework: the workflow is the agent loop.
    It drafts rules, then waits for Network Security to approve, reject or
    ask for changes (a Workflow Update), and revises until approved.
    The same agent also runs on LangChain and on LangGraph: see demo/frameworks.

All side effects happen in activities (see activities.py). Workflow code only
decides what happens next, so every decision is replayable from history.
"""

import asyncio
import json
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError, RetryState, is_cancelled_exception
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from demo import agents as agent_registry
    from demo import compat, frameworks
    from demo.models import (
        TERMINAL_TRACK_STATES,
        AgentInput,
        AgentResult,
        ApprovalDecision,
        IntakeInput,
        Plan,
        PlanInput,
        Resolution,
        ReviewDecision,
        StepInput,
        StepResult,
        StepSpec,
        TrackInput,
        TrackResult,
        TrackSpec,
        VendingInput,
    )
    from demo.settings import CORE_QUEUE

AI_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=15), maximum_attempts=4)
ITSM_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=10), maximum_attempts=12)
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=10), maximum_attempts=6)
UNDO_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=15), maximum_attempts=10)

# Statuses a child may report through the progress signal. Terminal statuses
# are set only when the parent has the child's result in hand, so dependent
# tracks never start before their inputs are available.
REPORTABLE = {"running", "blocked", "awaiting_review", "compensating", "finishing"}


class AgentDidNotFinish(Exception):
    """The agent ran out of turns without submitting a draft."""


def _now() -> str:
    return workflow.now().isoformat()


def _error_info(e: ActivityError, max_attempts: int) -> Dict[str, Any]:
    cause = e.cause
    info: Dict[str, Any] = {"type": "ActivityError", "message": str(cause or e), "retryable": True, "details": []}
    if isinstance(cause, ApplicationError):
        info.update(type=cause.type or "ApplicationError", message=cause.message, retryable=not cause.non_retryable,
                    details=[d for d in (cause.details or []) if isinstance(d, (dict, str, int, float))])
    elif isinstance(cause, TemporalTimeoutError):
        kind = cause.type.name.replace("_", " ").title().replace(" ", "") if cause.type else "Unknown"
        info.update(type=f"{kind}Timeout", message=f"Activity {kind.lower()} timeout: the worker stopped responding")
    rs = getattr(e, "retry_state", None)
    info["retry_state"] = rs.name if rs is not None else None
    if rs == RetryState.MAXIMUM_ATTEMPTS_REACHED:
        info["attempts"] = max_attempts
        info["message"] = f"{info['message']} (gave up after {max_attempts} attempts)"
    elif rs == RetryState.NON_RETRYABLE_FAILURE:
        info["retryable"] = False
    return info


# ===========================================================================
# Per-team child workflow
# ===========================================================================

@workflow.defn
class ProvisioningTrackWorkflow:
    @workflow.init
    def __init__(self, inp: TrackInput) -> None:
        self._inp = inp
        self._status = "running"
        self._resolution: Optional[Resolution] = None
        self._abort: Optional[str] = None
        self._agent_runs = 0
        self._reject_reason: Optional[str] = None
        self._steps: List[Dict[str, Any]] = [
            {
                "id": s.id, "name": s.name, "system": s.system, "system_label": s.system_label,
                "location": s.location, "queue": s.task_queue, "status": "pending", "attempt": None,
                "worker": None, "started_at": None, "completed_at": None, "duration_ms": None,
                "error": None, "diagnosis": None, "log": [], "output": {}, "history": [], "undo": s.undo,
                "kind": s.kind, "agent_workflow_id": None, "agent_run_id": None, "agent_status": None,
            }
            for s in inp.track.steps
        ]

    @workflow.signal
    def abort(self, reason: str) -> None:
        """Parent asks this track to stop and undo its work (request-level rollback)."""
        self._abort = reason

    @workflow.signal
    async def agent_status(self, p: Dict[str, Any]) -> None:
        """The agent child tells this track when it is waiting on a reviewer."""
        s = next((x for x in self._steps if x.get("agent_workflow_id") == p.get("agent_workflow_id")), None)
        if s:
            s["agent_status"] = p.get("status")
        new = "awaiting_review" if p.get("status") == "awaiting_review" else "running"
        if self._status in ("running", "awaiting_review") and new != self._status:
            self._status = new
            await self._report(s["name"] if s else None, review={"rules": p.get("rule_count"), "round": p.get("round"), "status": p.get("status")})

    @workflow.update
    def resolve(self, r: Resolution) -> str:
        """Operator decision for a blocked step: retry, skip or rollback."""
        self._resolution = r
        return f"{r.action} accepted"

    @resolve.validator
    def _validate_resolve(self, r: Resolution) -> None:
        if self._status != "blocked" or self._resolution is not None:
            raise ValueError("This track is not waiting for a decision")
        if r.action not in ("retry", "skip", "rollback"):
            raise ValueError(f"Unknown action '{r.action}'")

    @workflow.query
    def get_steps(self) -> Dict[str, Any]:
        return {"status": self._status, "steps": self._steps}

    @workflow.run
    async def run(self, inp: TrackInput) -> TrackResult:
        results: List[StepResult] = []
        context = dict(inp.context)
        produced: Dict[str, Any] = {}

        for i, step in enumerate(inp.track.steps):
            s = self._steps[i]
            while True:
                if self._abort:
                    return await self._undo(results, "rolled_back", self._abort)
                s.update(status="running", started_at=_now(), error=None)
                await self._report(step.name)
                try:
                    if step.kind == "agent":
                        res = await self._run_agent(step, s, context)
                        if res is None:  # the reviewer rejected the agent's draft
                            s["status"] = "failed"
                            return await self._undo(results, "aborted", self._reject_reason or "Draft rejected")
                    else:
                        res = await self._run_step(step, context)
                except (ActivityError, AgentDidNotFinish) as e:
                    if isinstance(e, ActivityError) and is_cancelled_exception(e):
                        raise
                    err = _error_info(e, inp.max_attempts) if isinstance(e, ActivityError) else {
                        "type": "AgentDidNotFinish", "message": str(e), "retryable": True, "details": []}
                    action = await self._handle_failure(step, s, err)
                    if action == "abort":
                        s["status"] = "failed"
                        return await self._undo(results, "rolled_back", self._abort or "Request rolled back")
                    if action == "retry":
                        continue
                    if action == "skip":
                        s.update(status="skipped", completed_at=_now())
                        break
                    s["status"] = "failed"
                    msg = (s["error"] or {}).get("message", "")[:140]
                    return await self._undo(results, "aborted", f"Operator rolled back after '{step.name}' failed: {msg}")

                s.update(status="completed", attempt=res.attempt, worker=res.worker, completed_at=_now(),
                         duration_ms=res.duration_ms, log=res.log, output=res.output)
                results.append(res)
                context.update(res.output)
                produced.update(res.output)
                break

        self._status = "finishing"
        await self._report(None)
        return TrackResult(inp.track.id, "completed", results, produced)

    # [snippet:retries]
    async def _run_step(self, step: StepSpec, context: Dict[str, Any]) -> StepResult:
        """One step = one activity. Temporal retries it with backoff; a dead worker is
        noticed by its missing heartbeat and the step moves to another worker."""
        forever = f"{self._inp.track.id}.{step.id}" in self._inp.retry_forever  # bug mode: wait for a fix
        return await workflow.execute_activity(  # [mark:step-run]
            "run_step",
            StepInput(self._inp.request_id, self._inp.track.id, step, self._inp.spec, context),
            result_type=StepResult,
            task_queue=step.task_queue,  # cloud or on-prem worker pool
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(seconds=self._inp.heartbeat_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=5 if forever else 10),
                maximum_attempts=0 if forever else self._inp.max_attempts,  # 0 = until it succeeds
            ),
            **compat.activity_kwargs(f"{step.name} ({step.system_label})"),
        )

    async def _handle_failure(self, step: StepSpec, s: Dict[str, Any], err: Dict[str, Any]) -> str:
        """Pause the track, get a diagnosis, wait for an operator. Returns the action."""
        s.update(status="blocked", error=err, diagnosis=None)
        s["history"].append({"at": _now(), "event": "failed", "error": err.get("message")})
        self._status = "blocked"
        await self._report(step.name, err)

        diag = await workflow.execute_activity(
            "ai_diagnose_failure",
            self._diagnosis_input(step, err),
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=AI_RETRY,
            **compat.activity_kwargs("Claude: diagnose the failure"),
        )
        s["diagnosis"] = {**diag["result"], "meta": diag["meta"]}
        await self._report(step.name, err, s["diagnosis"])

        # Paused, durably, until an operator decides. No polling, no cost while waiting.
        await workflow.wait_condition(lambda: self._resolution is not None or self._abort is not None)  # [mark:blocked-wait]
        self._status = "running"
        if self._abort:
            return "abort"
        r = self._resolution
        assert r is not None
        self._resolution = None
        s["history"].append({"at": _now(), "event": r.action, "by": r.operator or "operator", "note": r.note})
        return r.action
    # [/snippet]

    def _diagnosis_input(self, step: StepSpec, err: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "request_id": self._inp.request_id,
            "track": self._inp.track.name,
            "team": self._inp.track.team,
            "step_id": step.id,
            "step_name": step.name,
            "system": step.system_label,
            "location": step.location,
            "error": err,
            "retryable": err.get("retryable"),
            "downstream": self._inp.downstream,
            "spec": {k: self._inp.spec.get(k) for k in ("title", "environment", "data_classification", "onprem_connectivity", "cidr_size")},
            "completed_steps": [x["name"] for x in self._steps if x["status"] == "completed"],
        }

    async def _run_agent(self, step: StepSpec, s: Dict[str, Any], context: Dict[str, Any]) -> Optional[StepResult]:
        """Run the agent as its own child workflow. Returns None if the reviewer rejects."""
        self._agent_runs += 1
        agent_id = f"{workflow.info().workflow_id}.{step.id}-{self._agent_runs}"
        started = workflow.now()
        mode = frameworks.MODES.get(self._inp.agent_mode) or frameworks.MODES["plain"]
        handle = await workflow.start_child_workflow(
            mode["workflow"],  # FirewallRuleAgentWorkflow unless a LangChain/LangGraph mode is chosen
            AgentInput(
                request_id=self._inp.request_id,
                parent_workflow_id=workflow.info().workflow_id,
                agent=step.agent or "firewall_rules",
                spec=self._inp.spec,
                context=context,
                reviewer=step.reviewer or "Network Security",
                mode=self._inp.agent_mode if self._inp.agent_mode in frameworks.MODES else "plain",
                heartbeat_seconds=max(self._inp.heartbeat_seconds, 8),
            ),
            id=agent_id,
            task_queue=CORE_QUEUE,
            result_type=AgentResult,
            **compat.child_kwargs(f"Firewall-rule agent for {self._inp.request_id} ({mode['label']})", mode["summary"]),
        )
        s.update(agent_workflow_id=agent_id, agent_run_id=getattr(handle, "first_execution_run_id", None), agent_status="drafting")
        await self._report(step.name)
        result: AgentResult = await handle
        if self._status == "awaiting_review":
            self._status = "running"
        if result.status == "approved":
            return StepResult(
                step_id=step.id,
                status="completed",
                output={"approved_rules": result.rules, "approved_rule_count": len(result.rules),
                        "rules_reviewed_by": result.reviewer, "agent_rationale": result.rationale},
                worker="agent",
                attempt=1,
                duration_ms=int((workflow.now() - started).total_seconds() * 1000),
                log=[f"{result.llm_calls} model turns, {result.tool_calls} tool calls, {result.rounds} review round(s)",
                     f"Approved by {result.reviewer}" + (f": {result.comment}" if result.comment else "")],
            )
        if result.status == "rejected":
            self._reject_reason = f"{result.reviewer} rejected the firewall rules: {result.comment or 'no comment'}"
            return None
        raise AgentDidNotFinish(f"The agent used {result.llm_calls} turns without submitting rules")

    async def _undo(self, results: List[StepResult], status: str, reason: str) -> TrackResult:
        self._status = "compensating"
        await self._report("Rolling back")
        by_id = {st.id: st for st in self._inp.track.steps}
        for res in reversed(results):
            step = by_id[res.step_id]
            s = next(x for x in self._steps if x["id"] == res.step_id)
            if not step.undo:
                continue
            s["status"] = "compensating"
            try:
                await workflow.execute_activity(
                    "compensate_step",
                    StepInput(self._inp.request_id, self._inp.track.id, step, self._inp.spec, self._inp.context, original_output=res.output),
                    result_type=StepResult,
                    task_queue=step.task_queue,
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(seconds=self._inp.heartbeat_seconds),
                    retry_policy=UNDO_RETRY,
                    **compat.activity_kwargs(f"Undo: {step.name}"),
                )
                s["status"] = "rolled_back"
            except ActivityError as e:
                s["status"] = "undo_failed"
                s["history"].append({"at": _now(), "event": "undo_failed", "error": str(e.cause or e)})
        self._status = "rolled_back"
        await self._report(None)
        return TrackResult(self._inp.track.id, status, results, {}, reason)

    async def _report(self, current: Optional[str], error: Optional[Dict[str, Any]] = None, diagnosis: Optional[Dict[str, Any]] = None,
                      review: Optional[Dict[str, Any]] = None) -> None:
        running = next((x for x in self._steps if x["status"] == "running"), None)
        payload = {
            "track_id": self._inp.track.id,
            "status": self._status,
            "steps_done": sum(1 for x in self._steps if x["status"] in ("completed", "skipped")),
            "steps_total": len(self._steps),
            "current_step": current,
            "error": error,
            "diagnosis": diagnosis,
            "blocked_step": current if self._status == "blocked" else None,
            "steps": [{"id": x["id"], "status": x["status"]} for x in self._steps],
            "worker": running["worker"] if running else None,
            "review": review if self._status == "awaiting_review" else None,
        }
        await workflow.get_external_workflow_handle(self._inp.parent_workflow_id).signal("track_progress", payload)


# ===========================================================================
# The agent (no framework): the workflow *is* the agent loop
# ===========================================================================

@workflow.defn
class FirewallRuleAgentWorkflow:
    """Drafts firewall rules with Claude, then waits for Network Security.

    No agent framework: this workflow is the loop. Each model turn is one
    activity and each tool call is another, so a crash mid-run resumes at the
    next turn. Earlier turns come back from history; nothing is asked twice.
    """

    @workflow.init
    def __init__(self, inp: AgentInput) -> None:
        self._inp = inp
        self._agent = agent_registry.get(inp.agent)
        self._ctx = self._agent.agent_context(inp.spec, inp.context)
        self._messages: List[Dict[str, Any]] = [{"role": "user", "content": self._agent.task_prompt(self._ctx)}]
        self._transcript: List[Dict[str, Any]] = []
        self._status = "drafting"
        self._draft: Optional[Dict[str, Any]] = None
        self._pending_results: List[Dict[str, Any]] = []
        self._last_policy: Optional[Dict[str, Any]] = None
        self._review: Optional[ReviewDecision] = None
        self._round = 1
        self._stats = {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}

    # [snippet:human-review]
    @workflow.run
    async def run(self, inp: AgentInput) -> AgentResult:
        while True:
            draft = await self._think_and_act()
            if draft is None:
                self._status = "failed"
                return self._result("failed")
            self._draft = draft
            await self._set_status("awaiting_review")

            # Wait for a person. Minutes or days: the workflow sleeps, durably, at no cost.
            await workflow.wait_condition(lambda: self._review is not None)  # [mark:review-wait]
            review, self._review = self._review, None
            self._transcript.append({"kind": "review", "at": _now(), "decision": review.decision,
                                     "reviewer": review.reviewer, "comment": review.comment, "round": self._round})
            if review.decision in ("approve", "reject"):
                await self._set_status("approved" if review.decision == "approve" else "rejected")
                return self._result("approved" if review.decision == "approve" else "rejected", review)

            # "Request changes": hand the feedback to the agent and go round again.
            self._round += 1
            self._messages.append({"role": "user", "content": [
                *self._pending_results,
                {"type": "tool_result", "tool_use_id": draft["tool_use_id"], "content": "Not approved: the reviewer asked for changes."},
                {"type": "text", "text": self._agent.feedback_message(review.reviewer, review.comment)},
            ]})
            await self._set_status("revising")

    @workflow.update
    def review_draft(self, d: ReviewDecision) -> str:
        """Network Security's decision, sent from the UI (or Slack, or ServiceNow)."""
        self._review = d
        return f"{d.decision} recorded"

    @review_draft.validator
    def _validate_review(self, d: ReviewDecision) -> None:
        if self._status != "awaiting_review" or self._review is not None:
            raise ValueError("No draft is waiting for review")
        if d.decision not in ("approve", "changes", "reject"):
            raise ValueError("Decision must be approve, changes or reject")
        if d.decision == "changes" and not d.comment.strip():
            raise ValueError("Say what should change")
    # [/snippet]

    @workflow.query
    def get_agent(self) -> Dict[str, Any]:
        return {
            "status": self._status,
            "round": self._round,
            "mode": self._inp.mode,
            "reviewer": self._inp.reviewer,
            "context": {k: self._ctx.get(k) for k in ("account_name", "vpc_cidr", "targets")},
            "draft": {**self._draft, "policy": self._last_policy} if self._draft else None,
            "transcript": self._transcript,
            "stats": self._stats,
        }

    # [snippet:agent-loop]
    async def _think_and_act(self) -> Optional[Dict[str, Any]]:
        """Model turn, tool calls, model turn ... until the agent submits a draft."""
        for _ in range(self._inp.max_turns):
            turn = await workflow.execute_activity(  # [mark:llm-turn]
                "agent_llm_turn",
                {"agent": self._inp.agent, "messages": self._messages, "context": self._ctx,
                 "turn": self._stats["llm_calls"] + 1, "request_id": self._inp.request_id},
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=120),
                heartbeat_timeout=timedelta(seconds=self._inp.heartbeat_seconds),
                retry_policy=AI_RETRY,
                **compat.activity_kwargs(f"Claude: turn {self._stats['llm_calls'] + 1}"),
            )
            self._record_turn(turn)
            self._messages.append({"role": "assistant", "content": turn["content"]})

            calls = [b for b in turn["content"] if b.get("type") == "tool_use"]
            submit = next((c for c in calls if c["name"] == "submit_rules"), None)
            results = await asyncio.gather(*[self._call_tool(c) for c in calls if c is not submit])  # [mark:tool-call]
            if submit:
                self._pending_results = list(results)
                return {"tool_use_id": submit["id"], **submit["input"]}
            if not calls:
                self._messages.append({"role": "user", "content": "Call submit_rules with your final rule set."})
                continue
            self._messages.append({"role": "user", "content": list(results)})
        return None

    async def _call_tool(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """Each tool call is its own activity: retried on its own, never repeated once done."""
        out = await workflow.execute_activity(
            "agent_tool",
            {"agent": self._inp.agent, "name": call["name"], "input": call.get("input") or {},
             "context": self._ctx, "request_id": self._inp.request_id},
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=self._inp.heartbeat_seconds),
            retry_policy=TOOL_RETRY,
            **compat.activity_kwargs(f"Tool: {call['name']}"),
        )
        self._record_tool(call, out)
        return {"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(out["output"])}
    # [/snippet]

    def _record_turn(self, turn: Dict[str, Any]) -> None:
        self._stats["llm_calls"] += 1
        for k in ("input_tokens", "output_tokens"):
            self._stats[k] += int(turn.get(k) or 0)
        content = turn.get("content") or []
        self._transcript.append({
            "kind": "turn", "at": _now(), "n": self._stats["llm_calls"], "round": self._round,
            "text": " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()[:700],
            "tools": [b["name"] for b in content if b.get("type") == "tool_use"],
            "thought": any(b.get("type") in ("thinking", "redacted_thinking") for b in content),
            "source": turn.get("source"), "model": turn.get("model"), "latency_ms": turn.get("latency_ms"),
            "attempt": turn.get("attempt"), "worker": turn.get("worker"),
        })

    def _record_tool(self, call: Dict[str, Any], out: Dict[str, Any]) -> None:
        self._stats["tool_calls"] += 1
        if call["name"] == "check_policy":
            self._last_policy = out["output"]
        self._transcript.append({
            "kind": "tool", "at": _now(), "round": self._round, "name": call["name"], "input": call.get("input") or {},
            "summary": self._agent.describe_call(call["name"], call.get("input") or {}, out["output"]),
            "attempt": out.get("attempt"), "worker": out.get("worker"),
        })

    async def _set_status(self, status: str) -> None:
        self._status = status
        if not self._inp.parent_workflow_id:  # started on its own (Agent lab): nobody to tell
            return
        await workflow.get_external_workflow_handle(self._inp.parent_workflow_id).signal("agent_status", {
            "agent_workflow_id": workflow.info().workflow_id, "status": status, "round": self._round,
            "rule_count": len((self._draft or {}).get("rules") or []),
        })

    def _result(self, status: str, review: Optional[ReviewDecision] = None) -> AgentResult:
        return AgentResult(
            status=status,
            rules=list((self._draft or {}).get("rules") or []) if status == "approved" else [],
            rationale=(self._draft or {}).get("rationale", ""),
            reviewer=review.reviewer if review else "",
            comment=review.comment if review else "",
            rounds=self._round,
            llm_calls=self._stats["llm_calls"],
            tool_calls=self._stats["tool_calls"],
        )


# ===========================================================================
# Per-request parent workflow
# ===========================================================================

@workflow.defn
class AccountVendingWorkflow:
    @workflow.init
    def __init__(self, inp: VendingInput) -> None:
        self._inp = inp
        self._wf_id = workflow.info().workflow_id
        self._plan: Optional[Plan] = None
        self._specs: Dict[str, TrackSpec] = {}
        self._tracks: Dict[str, Dict[str, Any]] = {}
        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._handles: Dict[str, Any] = {}
        self._results: Dict[str, TrackResult] = {}
        self._completed_order: List[str] = []
        self._outputs: Dict[str, Any] = {}
        self._dirty = False
        self._cancel_reason: Optional[str] = None
        self._abort_reason: Optional[str] = None
        self._closing = False
        now = _now()
        self._state: Dict[str, Any] = {
            "request_id": inp.request_id,
            "requester": inp.requester,
            "request_text": inp.request_text,
            "title": None,
            "submitted_at": now,
            "status": "in_progress",
            "status_reason": None,
            "stage": "intake",
            "stage_times": {"intake": now},
            "closed_at": None,
            "spec": None,
            "intake": None,
            "ticket": None,
            "risk": None,
            "approvals": [],
            "tracks": [],
            "skipped": [],
            "verification": None,
            "summary": None,
            "outputs": self._outputs,
            "events": [],
            "demo": asdict(inp.demo),
        }

    # --- handlers ------------------------------------------------------------
    @workflow.query
    def get_state(self) -> Dict[str, Any]:
        return self._state

    @workflow.signal
    def track_progress(self, p: Dict[str, Any]) -> None:
        t = self._tracks.get(p.get("track_id", ""))
        if not t:
            return
        prev = t["status"]
        if p.get("status") in REPORTABLE and prev not in TERMINAL_TRACK_STATES:
            t["status"] = p["status"]
        for k in ("steps_done", "steps_total", "current_step", "error", "diagnosis", "blocked_step", "worker", "review"):
            if k in p:
                t[k] = p[k]
        if p.get("steps"):
            t["step_status"] = {x["id"]: x["status"] for x in p["steps"]}
        if t["status"] != prev:
            if t["status"] == "blocked":
                msg = (p.get("error") or {}).get("message", "")
                self._event("track_blocked", f"{t['name']} is blocked on '{p.get('current_step')}': {msg[:140]}", track=t["id"], tone="bad")
            elif prev == "blocked" and t["status"] == "running":
                self._event("track_resumed", f"{t['name']} resumed", track=t["id"])
            elif t["status"] == "awaiting_review":
                n = (p.get("review") or {}).get("rules")
                self._event("review_requested", f"Claude drafted {n} firewall rule(s); waiting on Network Security to review", track=t["id"], tone="wait")
            elif prev == "awaiting_review" and t["status"] == "running":
                self._event("review_done", f"Network Security responded on {t['name']}", track=t["id"])
        self._dirty = True

    @workflow.update
    def decide_approval(self, d: ApprovalDecision) -> Dict[str, Any]:
        self._decide(d.approval_id, d.decision, d.approver or self._approvals[d.approval_id]["approver"], d.comment)
        return self._approvals[d.approval_id]

    @decide_approval.validator
    def _validate_decision(self, d: ApprovalDecision) -> None:
        a = self._approvals.get(d.approval_id)
        if not a:
            raise ValueError(f"No approval '{d.approval_id}' on this request")
        if a["status"] != "pending":
            raise ValueError(f"{a['name']} is already {a['status']}")
        if d.decision not in ("approved", "rejected"):
            raise ValueError("Decision must be approved or rejected")

    @workflow.update
    def cancel_request(self, reason: str) -> str:
        self._cancel_reason = reason or "Cancelled by requester"
        self._dirty = True
        return "cancellation accepted"

    @cancel_request.validator
    def _validate_cancel(self, reason: str) -> None:
        if self._closing or self._cancel_reason or self._abort_reason:
            raise ValueError("This request is already finishing")

    # --- main flow -------------------------------------------------------------
    @workflow.run
    async def run(self, inp: VendingInput) -> Dict[str, Any]:
        await self._intake()
        if not await self._approval_phase():
            rejected = next((a for a in self._approvals.values() if a["status"] == "rejected"), None)
            if rejected:
                await self._close("rejected", f"{rejected['name']} rejected: {rejected.get('comment') or 'no comment'}")
            else:
                await self._close("cancelled", self._cancel_reason or "Cancelled")
            return self._final()

        if not await self._provision():
            reason = self._abort_reason or "Rolled back"
            await self._rollback(reason)
            await self._close("cancelled" if self._cancel_reason else "rolled_back", reason)
            return self._final()

        await self._verify()
        await self._deliver()
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self._final()

    async def _intake(self) -> None:
        self._event("intake", f"Request received from {self._inp.requester}")
        intake = await workflow.execute_activity(
            "ai_parse_request",
            IntakeInput(self._inp.request_id, self._inp.request_text, self._inp.requester),
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=AI_RETRY,
            **compat.activity_kwargs("Claude: read the request"),
        )
        spec = intake["spec"]
        self._state.update(spec=spec, title=spec.get("title"), intake={
            **intake["meta"], "assumptions": spec.get("assumptions", []), "missing_info": spec.get("missing_info", []),
        })
        src = "Claude" if intake["meta"].get("source") == "claude" else "keyword rules"
        self._event("intake", f"{src} read the request: {spec.get('environment')} account, {spec.get('data_classification')} data"
                              f"{', on-prem connectivity' if spec.get('onprem_connectivity') else ''}")

        plan = await workflow.execute_activity(
            "plan_request", PlanInput(self._inp.request_id, spec), result_type=Plan,
            start_to_close_timeout=timedelta(seconds=30), **compat.activity_kwargs("Build the provisioning plan"),
        )
        self._plan = plan
        self._specs = {t.id: t for t in plan.tracks}
        downstream: Dict[str, List[str]] = {t.id: [] for t in plan.tracks}
        for t in plan.tracks:
            for d in t.depends_on:
                downstream[d].append(t.name)
        for t in plan.tracks:
            self._tracks[t.id] = {
                "id": t.id, "name": t.name, "team": t.team, "depends_on": t.depends_on, "approval": t.approval,
                "reason": t.reason, "downstream": downstream[t.id], "status": "pending", "waiting_on": [],
                "steps_total": len(t.steps), "steps_done": 0, "current_step": None, "worker": None,
                "error": None, "diagnosis": None, "blocked_step": None, "workflow_id": None, "run_id": None,
                "started_at": None, "completed_at": None, "step_status": {},
                "steps": [{"id": s.id, "name": s.name, "system": s.system, "system_label": s.system_label,
                           "location": s.location, "queue": s.task_queue, "undo": s.undo, "kind": s.kind} for s in t.steps],
            }
        for a in plan.approvals:
            self._approvals[a.id] = {**asdict(a), "status": "pending", "requested_at": None, "decided_at": None,
                                     "decided_by": None, "comment": None, "escalated": False, "escalated_at": None}
        self._state["tracks"] = list(self._tracks.values())
        self._state["approvals"] = list(self._approvals.values())
        self._state["skipped"] = [asdict(s) for s in plan.skipped]
        self._event("plan", f"Plan: {len(plan.tracks)} teams, {sum(len(t.steps) for t in plan.tracks)} steps, {len(plan.approvals)} approvals")

        ticket = await workflow.execute_activity(
            "itsm_create_change", {"request_id": self._inp.request_id, "spec": spec}, result_type=dict,
            start_to_close_timeout=timedelta(seconds=60), retry_policy=ITSM_RETRY,
            **compat.activity_kwargs("Open one change request for every team"),
        )
        self._state["ticket"] = ticket
        self._outputs.update(ticket)
        self._event("ticket", f"Opened {ticket.get('change_number')} (one ticket instead of one per team)")

    async def _approval_phase(self) -> bool:
        self._set_stage("approvals")
        plan_view = {"approvals": [asdict(a) for a in self._plan.approvals], "tracks": [{"name": t.name, "team": t.team} for t in self._plan.tracks]}
        risk = await workflow.execute_activity(
            "ai_risk_review", {"spec": self._state["spec"], "plan": plan_view}, result_type=dict,
            start_to_close_timeout=timedelta(seconds=90), retry_policy=AI_RETRY,
            **compat.activity_kwargs("Claude: brief the approvers"),
        )
        self._state["risk"] = {**risk["result"], "meta": risk["meta"]}

        pending = []
        for a in self._approvals.values():
            a["requested_at"] = _now()
            if a["auto"]:
                self._decide(a["id"], "approved", "Policy", a.get("auto_reason") or "Auto-approved by policy")
            else:
                pending.append(a)
        if pending:
            await workflow.execute_activity(
                "notify",
                {"request_id": self._inp.request_id, "to": ", ".join(a["approver"] for a in pending),
                 "subject": f"Approval needed: {self._state['title']}", "channel": "email (mock)"},
                result_type=dict, start_to_close_timeout=timedelta(seconds=30),
                **compat.activity_kwargs("Notify approvers"),
            )
            for a in pending:
                self._event("approval_requested", f"Waiting on {a['name']} ({a['approver']})", approval=a["id"], tone="wait")
                asyncio.create_task(self._approval_timer(a["id"]))

        request_level = [a for a in self._approvals.values() if a["blocks"] == "request"]
        await workflow.wait_condition(
            lambda: self._cancel_reason is not None
            or any(a["status"] == "rejected" for a in self._approvals.values())
            or all(a["status"] == "approved" for a in request_level)
        )
        if self._cancel_reason or any(a["status"] == "rejected" for a in self._approvals.values()):
            return False
        return True

    # [snippet:approvals]
    async def _approval_timer(self, approval_id: str) -> None:
        """Demo auto-approver and SLA escalation, both durable timers."""
        a = self._approvals[approval_id]
        demo = self._inp.demo

        def decided() -> bool:
            return a["status"] != "pending" or self._closing

        if demo.auto_approve_seconds > 0:
            try:
                await workflow.wait_condition(decided, timeout=timedelta(seconds=demo.auto_approve_seconds))
            except asyncio.TimeoutError:
                self._decide(approval_id, "approved", "Demo auto-approver", f"Auto-approved after {demo.auto_approve_seconds}s (demo setting)")
            return
        if demo.approval_sla_seconds <= 0:
            return
        try:
            await workflow.wait_condition(decided, timeout=timedelta(seconds=demo.approval_sla_seconds))  # [mark:approval-wait]
        except asyncio.TimeoutError:
            a.update(escalated=True, escalated_at=_now())
            self._event("escalated", f"{a['name']} has waited {demo.approval_sla_seconds}s; escalated to {a['escalation']}", approval=approval_id, tone="warn")
            await workflow.execute_activity(
                "notify",
                {"request_id": self._inp.request_id, "to": a["escalation"], "subject": f"Escalation: {a['name']} approval overdue", "channel": "email (mock)"},
                result_type=dict, start_to_close_timeout=timedelta(seconds=30),
                **compat.activity_kwargs(f"Escalate {a['name']} approval"),
            )

    # [/snippet]

    def _decide(self, approval_id: str, decision: str, by: str, comment: Optional[str]) -> None:
        a = self._approvals[approval_id]
        a.update(status=decision, decided_at=_now(), decided_by=by, comment=comment)
        verb = "approved" if decision == "approved" else "rejected"
        self._event(f"approval_{verb}", f"{a['name']} {verb} by {by}" + (f": {comment}" if comment else ""), approval=approval_id,
                    tone="good" if decision == "approved" else "bad")
        self._dirty = True

    # [snippet:fan-out]
    async def _provision(self) -> bool:
        self._set_stage("provisioning")
        await self._update_ticket("Implement", "Approved. Provisioning started.")
        while True:
            if self._cancel_reason and not self._abort_reason:
                self._abort_reason = f"Cancelled: {self._cancel_reason}"
            rejected = next((a for a in self._approvals.values() if a["status"] == "rejected"), None)
            if rejected and not self._abort_reason:
                self._abort_reason = f"{rejected['name']} rejected the request"
            if self._abort_reason:
                return False

            for tid, spec in self._specs.items():
                st = self._tracks[tid]
                if st["status"] not in ("pending", "waiting", "awaiting_approval"):
                    continue
                waiting = [self._tracks[d]["name"] for d in spec.depends_on if self._tracks[d]["status"] not in ("completed", "skipped")]
                if waiting:
                    st.update(status="waiting", waiting_on=waiting)
                    continue
                if spec.approval and self._approvals[spec.approval]["status"] != "approved":
                    st.update(status="awaiting_approval", waiting_on=[f"{self._approvals[spec.approval]['name']} approval"])
                    continue
                st.update(status="starting", waiting_on=[])
                self._tasks[tid] = asyncio.create_task(self._run_track(spec))

            if all(self._tracks[t]["status"] in TERMINAL_TRACK_STATES for t in self._specs):
                return self._abort_reason is None
            self._dirty = False
            await workflow.wait_condition(lambda: self._dirty)  # [mark:fanout-wait]
    # [/snippet]

    async def _run_track(self, spec: TrackSpec) -> None:
        st = self._tracks[spec.id]
        child_id = f"{self._wf_id}.{spec.id}"
        handle = await workflow.start_child_workflow(
            ProvisioningTrackWorkflow.run,
            TrackInput(
                request_id=self._inp.request_id,
                parent_workflow_id=self._wf_id,
                track=spec,
                spec=self._state["spec"],
                context=dict(self._outputs),
                max_attempts=self._inp.demo.max_attempts,
                heartbeat_seconds=self._inp.demo.heartbeat_seconds,
                downstream=st["downstream"],
                retry_forever=list(self._inp.demo.retry_forever),
                agent_mode=self._inp.demo.agent_mode,
            ),
            id=child_id,
            task_queue=CORE_QUEUE,
            **compat.child_kwargs(f"{spec.name} ({spec.team})", f"{len(spec.steps)} steps for request {self._inp.request_id}"),
        )
        self._handles[spec.id] = handle
        st.update(workflow_id=child_id, run_id=getattr(handle, "first_execution_run_id", None), status="running", started_at=_now())
        self._event("track_started", f"{spec.team} started {spec.name}", track=spec.id)
        if self._abort_reason:
            await handle.signal(ProvisioningTrackWorkflow.abort, self._abort_reason)
        try:
            result: TrackResult = await handle
        except ChildWorkflowError as e:
            st.update(status="failed", error={"type": "ChildWorkflowError", "message": str(e.cause or e)}, completed_at=_now())
            self._abort_reason = self._abort_reason or f"{spec.name} failed unexpectedly"
            self._event("track_failed", f"{spec.name} failed: {e.cause or e}", track=spec.id, tone="bad")
            self._dirty = True
            return
        self._results[spec.id] = result
        if result.status == "completed":
            self._outputs.update(result.outputs)
            st.update(status="completed", completed_at=_now(), current_step=None, error=None, worker=None,
                      steps_done=sum(1 for _ in result.steps) + sum(1 for v in st.get("step_status", {}).values() if v == "skipped"))
            self._completed_order.append(spec.id)
            self._event("track_completed", f"{spec.team} finished {spec.name}", track=spec.id, tone="good")
        else:
            st.update(status="rolled_back", completed_at=_now(), current_step=None)
            self._event("track_rolled_back", f"{spec.name} rolled back: {result.reason}", track=spec.id, tone="warn")
            if result.status == "aborted" and not self._abort_reason:
                self._abort_reason = result.reason
        self._dirty = True

    async def _rollback(self, reason: str) -> None:
        self._state.update(status="rolling_back", status_reason=reason)
        self._closing = True
        self._event("rollback", f"Rolling back: {reason}", tone="bad")
        for tid, task in self._tasks.items():
            h = self._handles.get(tid)
            if h and not task.done() and self._tracks[tid]["status"] not in TERMINAL_TRACK_STATES:
                await h.signal(ProvisioningTrackWorkflow.abort, reason)
        running = [t for t in self._tasks.values() if not t.done()]
        if running:
            await asyncio.gather(*running)

        for tid in reversed(self._completed_order):
            st, spec, res = self._tracks[tid], self._specs[tid], self._results[tid]
            st["status"] = "compensating"
            self._event("compensating", f"Undoing {spec.name}", track=tid, tone="warn")
            outputs = {r.step_id: r for r in res.steps}
            for step in reversed(spec.steps):
                r = outputs.get(step.id)
                if not r or not step.undo:
                    continue
                try:
                    await workflow.execute_activity(
                        "compensate_step",
                        StepInput(self._inp.request_id, tid, step, self._state["spec"], dict(self._outputs), original_output=r.output),
                        result_type=StepResult, task_queue=step.task_queue,
                        start_to_close_timeout=timedelta(minutes=10),
                        heartbeat_timeout=timedelta(seconds=self._inp.demo.heartbeat_seconds),
                        retry_policy=UNDO_RETRY,
                        **compat.activity_kwargs(f"Undo: {step.name}"),
                    )
                except ActivityError as e:
                    self._event("undo_failed", f"Could not undo '{step.name}': {e.cause or e}. Flagged for manual cleanup.", track=tid, tone="bad")
            st.update(status="rolled_back", current_step=None)
        for st in self._tracks.values():
            if st["status"] not in TERMINAL_TRACK_STATES:
                st["status"] = "cancelled"

    async def _verify(self) -> None:
        self._set_stage("verification")
        v = await workflow.execute_activity(
            "verify_request",
            {"request_id": self._inp.request_id, "spec": self._state["spec"], "outputs": self._outputs},
            result_type=dict, start_to_close_timeout=timedelta(minutes=3), heartbeat_timeout=timedelta(seconds=30),
            **compat.activity_kwargs("Verify the account baseline"),
        )
        self._state["verification"] = v
        passed = sum(1 for c in v.get("checks", []) if c.get("ok"))
        self._event("verified", f"Verification: {passed}/{len(v.get('checks', []))} checks passed", tone="good" if v.get("passed") else "warn")
        await self._update_ticket("Review", "All provisioning tracks complete; verification attached.")

    async def _deliver(self) -> None:
        s = await workflow.execute_activity(
            "ai_completion_summary",
            {"spec": self._state["spec"], "outputs": self._outputs, "tracks": [{"name": t["name"], "team": t["team"]} for t in self._tracks.values()]},
            result_type=dict, start_to_close_timeout=timedelta(seconds=90), retry_policy=AI_RETRY,
            **compat.activity_kwargs("Claude: write the hand-off note"),
        )
        self._state["summary"] = {**s["result"], "meta": s["meta"]}
        self._closing = True
        await self._update_ticket("Closed", self._state["summary"].get("message", "Delivered"))
        await self._notify_requester(self._state["summary"].get("headline", "Your account is ready"))
        self._set_stage("delivered")
        self._state.update(status="completed", closed_at=_now())
        self._event("delivered", "Delivered to the requester", tone="good")

    async def _close(self, status: str, reason: str) -> None:
        self._closing = True
        self._state.update(status=status, status_reason=reason, closed_at=_now())
        self._event(status, reason, tone="bad" if status in ("rejected", "rolled_back") else "warn")
        if self._state.get("ticket"):
            await self._update_ticket("Cancelled", reason)
        await self._notify_requester(f"Request {status.replace('_', ' ')}: {reason}")

    async def _update_ticket(self, state: str, note: str) -> None:
        if not self._state.get("ticket"):
            return
        await workflow.execute_activity(
            "itsm_update_change",
            {"request_id": self._inp.request_id, "state": state, "note": note, "spec": self._state["spec"], "context": self._outputs},
            result_type=dict, start_to_close_timeout=timedelta(seconds=60), retry_policy=ITSM_RETRY,
            **compat.activity_kwargs(f"Change request -> {state}"),
        )
        self._state["ticket"]["change_state"] = state

    async def _notify_requester(self, subject: str) -> None:
        await workflow.execute_activity(
            "notify", {"request_id": self._inp.request_id, "to": self._inp.requester, "subject": subject, "channel": "email (mock)"},
            result_type=dict, start_to_close_timeout=timedelta(seconds=30), **compat.activity_kwargs("Notify the requester"),
        )

    # --- helpers ---------------------------------------------------------------
    def _set_stage(self, stage: str) -> None:
        self._state["stage"] = stage
        self._state["stage_times"][stage] = _now()

    def _event(self, kind: str, message: str, **extra: Any) -> None:
        self._state["events"].append({"t": _now(), "kind": kind, "message": message, **extra})
        if len(self._state["events"]) > 250:
            del self._state["events"][:50]

    def _final(self) -> Dict[str, Any]:
        return {
            "request_id": self._inp.request_id,
            "status": self._state["status"],
            "status_reason": self._state["status_reason"],
            "outputs": self._outputs,
            "summary": self._state.get("summary"),
        }
