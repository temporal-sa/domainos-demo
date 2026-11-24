"""The four places the demo uses Claude, each with a deterministic fallback.

1. parse_request   - free-text request -> structured account spec
2. risk_review     - spec + plan -> briefing for the approvers
3. diagnose        - failed step -> root cause and a recommended operator action
4. summarize       - finished request -> plain-language note for the requester

Fallbacks keep the demo running with no API key (or a dead network). Their
output is marked `source: heuristic` so the UI can say which one produced it.
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Tuple

from .. import settings
from . import claude

# ---------------------------------------------------------------------------
# Schemas (structured outputs require additionalProperties: false everywhere)
# ---------------------------------------------------------------------------

INTAKE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "account_name", "business_unit", "application", "environment",
        "data_classification", "onprem_connectivity", "onprem_targets", "region",
        "cidr_size", "owners", "cost_center", "monthly_budget_usd", "justification",
        "assumptions", "missing_info",
    ],
    "properties": {
        "title": {"type": "string", "description": "Short human title, e.g. 'Mortgage Analytics production account'"},
        "account_name": {"type": "string", "description": "kebab-case AWS account name, e.g. 'mortgage-analytics-prod'"},
        "business_unit": {"type": "string"},
        "application": {"type": "string"},
        "environment": {"type": "string", "enum": ["sandbox", "dev", "test", "prod"]},
        "data_classification": {"type": "string", "enum": ["Public", "Internal", "Confidential", "Restricted"]},
        "onprem_connectivity": {"type": "boolean", "description": "Needs network access to on-prem systems"},
        "onprem_targets": {"type": "array", "items": {"type": "string"}},
        "region": {"type": "string", "description": "AWS region, default us-east-1"},
        "cidr_size": {"type": "string", "enum": ["/24", "/23", "/22", "/21"]},
        "owners": {"type": "array", "items": {"type": "string"}},
        "cost_center": {"type": "string"},
        "monthly_budget_usd": {"type": "integer"},
        "justification": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Things you inferred rather than read"},
        "missing_info": {"type": "array", "items": {"type": "string"}, "description": "Things a human should confirm"},
    },
}

RISK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["risk_level", "summary", "concerns", "recommendation"],
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
    },
}

DIAGNOSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["root_cause", "impact", "recommended_action", "confidence", "operator_steps", "requester_note"],
    "properties": {
        "root_cause": {"type": "string"},
        "impact": {"type": "string", "description": "What is blocked downstream"},
        "recommended_action": {"type": "string", "enum": ["retry", "skip", "rollback"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "operator_steps": {"type": "array", "items": {"type": "string"}},
        "requester_note": {"type": "string", "description": "One sentence for the requester, no jargon"},
    },
}

SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "message", "next_steps"],
    "properties": {
        "headline": {"type": "string"},
        "message": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}


def _system(role: str) -> str:
    return (
        f"You are {role} for the cloud platform team at {settings.ORG_NAME}, a regulated US bank. "
        "Be precise and brief. Never invent account IDs, ticket numbers or people. "
        "Respond only with JSON that matches the schema."
    )


async def _call(role: str, prompt: str, schema: Dict[str, Any], fallback, *fallback_args) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not claude.enabled():
        reason = "ANTHROPIC_API_KEY not set" if not settings.ai_configured() else "anthropic package not installed"
        return fallback(*fallback_args), claude.heuristic_meta(reason)
    try:
        return await claude.structured(_system(role), prompt, schema)
    except claude.AIFatal as e:
        return fallback(*fallback_args), claude.heuristic_meta("Claude call failed", str(e))
    # AIRetryable propagates so Temporal retries the activity.


# ---------------------------------------------------------------------------
# 1. Intake
# ---------------------------------------------------------------------------

async def parse_request(text: str, requester: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = (
        "Turn this request for a new AWS account into a provisioning spec.\n"
        "Rules: customer, member or borrower data means Confidential; card data (PCI) means Restricted; "
        "anything that mentions the data center, on-prem databases, mainframe or internal-only services "
        "needs on-prem connectivity. Default region us-east-1, default CIDR /22 for prod and /24 otherwise, "
        "default budget 1500 for sandbox and 8000 for prod. List every default you applied in assumptions.\n\n"
        f"Requester: {requester}\nRequest:\n{text}"
    )
    spec, meta = await _call("the intake assistant", prompt, INTAKE_SCHEMA, heuristic_parse, text, requester)
    return normalize_spec(spec, text, requester), meta


def normalize_spec(spec: Dict[str, Any], text: str, requester: str) -> Dict[str, Any]:
    spec = dict(spec)
    spec.setdefault("assumptions", [])
    spec.setdefault("missing_info", [])
    spec["environment"] = str(spec.get("environment") or "dev").lower()
    name = re.sub(r"[^a-z0-9-]+", "-", str(spec.get("account_name") or "").lower()).strip("-")
    if not name:
        name = f"workload-{spec['environment']}"
    spec["account_name"] = name[:50]
    spec["requester"] = requester
    spec["request_text"] = text
    spec["owners"] = list(spec.get("owners") or [requester])
    spec["onprem_connectivity"] = bool(spec.get("onprem_connectivity"))
    spec["region"] = spec.get("region") or "us-east-1"
    return spec


_ENV_PATTERNS = [
    ("prod", r"\b(prod|production|live)\b"),
    ("sandbox", r"\b(sandbox|playground|poc|proof of concept|experiment)\b"),
    ("test", r"\b(test|qa|uat|staging)\b"),
    ("dev", r"\b(dev|development)\b"),
]


_NEGATION = re.compile(r"\b(no|not|without|never|zero)\b[^.;,]{0,40}$")


def _mentions(t: str, pattern: str) -> bool:
    """True if `pattern` appears without a negation shortly before it ("no on-prem access")."""
    for m in re.finditer(pattern, t):
        if not _NEGATION.search(t[max(0, m.start() - 45):m.start()]):
            return True
    return False


def heuristic_parse(text: str, requester: str) -> Dict[str, Any]:
    t = text.lower()
    assumptions: List[str] = []
    env = next((e for e, pat in _ENV_PATTERNS if re.search(pat, t)), None)
    if not env:
        env = "dev"
        assumptions.append("No environment stated; assumed dev")

    if _mentions(t, r"\b(pci|card ?holder|card data|pan)\b"):
        cls = "Restricted"
    elif _mentions(t, r"\b(pii|customer|member|borrower|account holder|ssn|npi|confidential)\b"):
        cls = "Confidential"
    else:
        cls = "Internal"
        assumptions.append("No sensitive data mentioned; classified Internal")

    onprem = _mentions(t, r"on-?prem|data ?cent(er|re)|\bdc\b|mainframe|oracle|internal (db|database|service)")
    targets = []
    if onprem:
        for label, pat in [("Oracle database", r"oracle"), ("Mainframe", r"mainframe"), ("Data center services", r"data ?cent(er|re)|on-?prem")]:
            if _mentions(t, pat):
                targets.append(label)

    team = re.search(r"(?:for|for the)\s+([A-Z][\w&]*(?:\s+[A-Z][\w&]*){0,3})\s+team", text)
    unit = team.group(1) if team else "Unassigned business unit"
    if not team:
        assumptions.append("Business unit not found in the request")

    cc = re.search(r"cost ?cent(?:er|re)\s*#?\s*(\w[\w-]*)", t)
    budget = re.search(r"\$\s?(\d[\d,]*)(\s?k)?", t)
    budget_usd = 8000 if env == "prod" else 1500
    if budget:
        budget_usd = int(budget.group(1).replace(",", "")) * (1000 if budget.group(2) else 1)
    else:
        assumptions.append(f"Default monthly budget ${budget_usd:,}")

    owners = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    if not owners:
        m = re.search(r"owners?\s+(?:are\s+|is\s+)?([a-z][\w.]+)(?:\s*(?:,|and)\s*([a-z][\w.]+))?", t)
        owners = [o for o in (m.groups() if m else []) if o] or [requester]

    region = (re.search(r"\b(us|eu|ap|ca|sa)-(east|west|central|north|south|southeast|northeast)-\d\b", t) or [None])[0] or "us-east-1"
    slug_src = unit if team else (re.search(r"for (?:the )?([\w ]{3,30})", t).group(1) if re.search(r"for (?:the )?([\w ]{3,30})", t) else "workload")
    slug = re.sub(r"[^a-z0-9]+", "-", slug_src.lower()).strip("-")
    title_unit = unit if team else slug.replace("-", " ").title()
    return {
        "title": f"{title_unit} {'production' if env == 'prod' else env} account",
        "account_name": f"{slug}-{env}",
        "business_unit": unit,
        "application": title_unit,
        "environment": env,
        "data_classification": cls,
        "onprem_connectivity": onprem,
        "onprem_targets": targets,
        "region": region,
        "cidr_size": "/22" if env == "prod" else "/24",
        "owners": owners,
        "cost_center": cc.group(1).upper() if cc else "UNKNOWN",
        "monthly_budget_usd": budget_usd,
        "justification": text.strip()[:240],
        "assumptions": assumptions + ["Parsed by keyword rules (Claude not used)"],
        "missing_info": [] if cc else ["Cost center"],
    }


# ---------------------------------------------------------------------------
# 2. Risk review for approvers
# ---------------------------------------------------------------------------

async def risk_review(spec: Dict[str, Any], plan: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    compact = {k: spec.get(k) for k in ("title", "environment", "data_classification", "onprem_connectivity", "onprem_targets", "cost_center", "monthly_budget_usd", "owners", "missing_info")}
    prompt = (
        "Write a briefing for the people approving this AWS account request. "
        "Call out anything an approver should push back on. Keep the summary under 45 words "
        "and at most 4 concerns.\n\n"
        f"Spec: {json.dumps(compact)}\n"
        f"Approvals required: {[a['name'] for a in plan.get('approvals', [])]}\n"
        f"Provisioning tracks: {[t['name'] for t in plan.get('tracks', [])]}"
    )
    return await _call("the approvals analyst", prompt, RISK_SCHEMA, heuristic_risk, spec)


def heuristic_risk(spec: Dict[str, Any]) -> Dict[str, Any]:
    concerns = []
    level = "low"
    if spec.get("data_classification") in ("Confidential", "Restricted"):
        concerns.append(f"Holds {spec['data_classification'].lower()} data: confirm encryption and data-owner sign-off")
        level = "medium"
    if spec.get("onprem_connectivity"):
        concerns.append("Opens a network path into the data center: review the firewall rule set")
        level = "high" if level == "medium" else "medium"
    if spec.get("environment") == "prod":
        concerns.append(f"Production budget of ${spec.get('monthly_budget_usd', 0):,}/month")
    for m in spec.get("missing_info", []):
        concerns.append(f"Missing: {m}")
    return {
        "risk_level": level,
        "summary": f"{spec.get('environment', '').title()} account for {spec.get('business_unit', 'an unknown team')} "
        f"with {spec.get('data_classification', 'Internal').lower()} data"
        f"{' and on-prem connectivity' if spec.get('onprem_connectivity') else ''}.",
        "concerns": concerns[:4],
        "recommendation": "Approve with standard guardrails" if level != "high" else "Approve once the firewall rules are reviewed",
    }


# ---------------------------------------------------------------------------
# 3. Failure diagnosis
# ---------------------------------------------------------------------------

async def diagnose(failure: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = (
        "A provisioning step failed and automatic retries are exhausted or the error is not retryable. "
        "Diagnose it for the on-call platform engineer. Choose recommended_action:\n"
        "- retry: the cause is fixable outside this workflow (lock released, quota raised, pool expanded) and the step is idempotent\n"
        "- skip: the step is optional for this request and nothing downstream needs its output\n"
        "- rollback: the request cannot be completed as specified\n"
        "Give 2-4 concrete operator_steps.\n\n"
        f"Failure context: {json.dumps(failure, default=str)[:6000]}"
    )
    return await _call("the on-call diagnostics assistant", prompt, DIAGNOSIS_SCHEMA, heuristic_diagnose, failure)


def heuristic_diagnose(failure: Dict[str, Any]) -> Dict[str, Any]:
    err = failure.get("error", {}) or {}
    etype = err.get("type", "")
    msg = err.get("message", "")
    step = failure.get("step_name", "the step")
    downstream = failure.get("downstream", [])
    impact = f"Blocks {', '.join(downstream)}" if downstream else "Nothing downstream depends on this step"
    playbook = {
        "IpamPoolExhausted": ("The workload IP pool in Infoblox has no free block of the requested size.",
                              ["Extend the 10.48.0.0/14 container or add an overflow pool in Infoblox", "Confirm no stale reservations from decommissioned accounts", "Retry the step; the playbook allocates the next free block"]),
        "PanoramaCommitLocked": ("Another administrator holds the Panorama commit lock, so the push was refused.",
                                 ["Ask the lock holder to commit or release the lock", "Retry once the lock is released; staged rules are preserved"]),
        "ConstraintViolationException": ("AWS Organizations reached its member-account quota.",
                                         ["Request a quota increase through AWS Support", "Close unused sandbox accounts", "Retry after the quota is raised"]),
        "HeartbeatTimeout": ("The worker running this step stopped responding.",
                             ["Check worker health", "Retry; the step is idempotent"]),
    }
    root, steps = playbook.get(etype, (f"{step} failed: {msg[:160]}", ["Check the upstream system", "Retry when it is healthy"]))
    return {
        "root_cause": root,
        "impact": impact,
        "recommended_action": "retry",
        "confidence": "high" if etype in playbook else "low",
        "operator_steps": steps,
        "requester_note": f"{failure.get('team', 'A team')} hit a snag on {step.lower()} and is working on it.",
    }


# ---------------------------------------------------------------------------
# 4. Completion summary for the requester
# ---------------------------------------------------------------------------

async def summarize(spec: Dict[str, Any], outputs: Dict[str, Any], tracks: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = (
        "Write the 'your account is ready' note for the requester. Plain language, under 70 words in message, "
        "3 next steps. Use only the facts given.\n\n"
        f"Request: {spec.get('title')} ({spec.get('environment')})\n"
        f"Outputs: {json.dumps({k: v for k, v in outputs.items() if not isinstance(v, (dict, list))})}\n"
        f"Teams involved: {[t.get('team') for t in tracks]}"
    )
    return await _call("the requester liaison", prompt, SUMMARY_SCHEMA, heuristic_summary, spec, outputs, tracks)


def heuristic_summary(spec: Dict[str, Any], outputs: Dict[str, Any], tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    acct = outputs.get("account_id", "your new account")
    teams = len({t.get("team") for t in tracks})
    return {
        "headline": f"{spec.get('title', 'Your account')} is ready",
        "message": f"Account {acct} ({spec.get('account_name')}) is live in {spec.get('region', 'us-east-1')}. "
        f"{teams} teams finished their work from one request, and the change ticket is closed.",
        "next_steps": [
            "Sign in through Okta and pick the new account in the AWS access portal",
            "Request additional roles through the same intake form",
            "Budget alerts go to the account owners at 80% and 100%",
        ],
    }


def stable_hash(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)
