"""Workflow-side bookkeeping shared by the LangChain and LangGraph agent workflows.

Holds what the UI and the parent track need from any agent run: status, the
draft under review, the review round, a transcript, and call counts. It also
implements the human review wait. Each framework workflow keeps its own
handlers and loop, and delegates the bookkeeping here, so get_agent returns
the same shape whatever runs the agent.

Workflow code: deterministic, no I/O.
"""

from typing import Any, Dict, List, Optional

from temporalio import workflow

from ..models import AgentInput, AgentResult, ReviewDecision


class AgentSession:
    def __init__(self, inp: AgentInput, agent: Any) -> None:
        self.inp = inp
        self.agent = agent
        self.ctx = agent.agent_context(inp.spec, inp.context)
        self.status = "drafting"
        self.round = 1
        self.draft: Optional[Dict[str, Any]] = None
        self.policy: Optional[Dict[str, Any]] = None
        self.review: Optional[ReviewDecision] = None
        self.transcript: List[Dict[str, Any]] = []
        self.units: List[Dict[str, Any]] = []  # durability units (activities) in order, for the lab
        self.stats = {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
                      "llm_retries": 0, "tool_retries": 0}

    # --- what the UI sees --------------------------------------------------------
    def view(self, **extra: Any) -> Dict[str, Any]:
        return {
            "status": self.status,
            "round": self.round,
            "mode": self.inp.mode,
            "reviewer": self.inp.reviewer,
            "context": {k: self.ctx.get(k) for k in ("account_name", "vpc_cidr", "targets")},
            "draft": {**self.draft, "policy": self.policy} if self.draft else None,
            "transcript": self.transcript,
            "units": self.units,
            "stats": self.stats,
            **extra,
        }

    # --- recording ---------------------------------------------------------------
    def record_turn(self, turn: Dict[str, Any], unit: Optional[str] = None) -> None:
        self.stats["llm_calls"] += 1
        self.stats["llm_retries"] += max(0, int(turn.get("attempt") or 1) - 1)
        for k in ("input_tokens", "output_tokens"):
            self.stats[k] += int(turn.get(k) or 0)
        entry = {
            "kind": "turn", "at": workflow.now().isoformat(), "n": self.stats["llm_calls"], "round": self.round,
            "text": (turn.get("text") or "")[:700], "tools": [c["name"] for c in turn.get("calls") or []],
            "source": turn.get("source"), "model": turn.get("model"), "latency_ms": turn.get("latency_ms"),
            "attempt": turn.get("attempt"), "worker": turn.get("worker"),
        }
        self.transcript.append(entry)
        if unit:
            self.units.append({"kind": unit, "label": f"Turn {entry['n']}", "attempt": entry["attempt"], "worker": entry["worker"], "at": entry["at"]})

    def record_tool(self, name: str, args: Dict[str, Any], out: Dict[str, Any], unit: Optional[str] = None) -> None:
        self.stats["tool_calls"] += 1
        self.stats["tool_retries"] += max(0, int(out.get("attempt") or 1) - 1)
        if name == "check_policy":
            self.policy = out.get("output")
        entry = {
            "kind": "tool", "at": workflow.now().isoformat(), "round": self.round, "name": name, "input": args,
            "summary": self.agent.describe_call(name, args, out.get("output")),
            "attempt": out.get("attempt"), "worker": out.get("worker"),
        }
        self.transcript.append(entry)
        if unit:
            self.units.append({"kind": unit, "label": name, "attempt": entry["attempt"], "worker": entry["worker"], "at": entry["at"]})

    # --- human review --------------------------------------------------------------
    def validate_review(self, d: ReviewDecision) -> None:
        if self.status != "awaiting_review" or self.review is not None:
            raise ValueError("No draft is waiting for review")
        if d.decision not in ("approve", "changes", "reject"):
            raise ValueError("Decision must be approve, changes or reject")
        if d.decision == "changes" and not d.comment.strip():
            raise ValueError("Say what should change")

    async def wait_for_review(self, draft: Dict[str, Any]) -> ReviewDecision:
        """Show the draft, wait (durably) for the reviewer, record the decision."""
        self.draft = draft
        await self.set_status("awaiting_review")
        await workflow.wait_condition(lambda: self.review is not None)
        review, self.review = self.review, None
        assert review is not None
        self.transcript.append({"kind": "review", "at": workflow.now().isoformat(), "decision": review.decision,
                                "reviewer": review.reviewer, "comment": review.comment, "round": self.round})
        if review.decision == "changes":
            self.round += 1
            await self.set_status("revising")
        else:
            await self.set_status("approved" if review.decision == "approve" else "rejected")
        return review

    async def set_status(self, status: str) -> None:
        self.status = status
        if not self.inp.parent_workflow_id:  # Agent lab runs have no parent track
            return
        await workflow.get_external_workflow_handle(self.inp.parent_workflow_id).signal("agent_status", {
            "agent_workflow_id": workflow.info().workflow_id, "status": status, "round": self.round,
            "rule_count": len((self.draft or {}).get("rules") or []),
        })

    def result(self, status: str, review: Optional[ReviewDecision] = None) -> AgentResult:
        if status == "failed":
            self.status = "failed"
        return AgentResult(
            status=status,
            rules=list((self.draft or {}).get("rules") or []) if status == "approved" else [],
            rationale=(self.draft or {}).get("rationale", ""),
            reviewer=review.reviewer if review else "",
            comment=review.comment if review else "",
            rounds=self.round,
            llm_calls=self.stats["llm_calls"],
            tool_calls=self.stats["tool_calls"],
        )
