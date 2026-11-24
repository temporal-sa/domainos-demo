"""Firewall-rule drafting agent: prompt, tools, policy, and an offline brain.

The agent gets the new account's VPC CIDR and the on-prem systems it needs,
then works like a careful network engineer would:

  1. look up each system in the CMDB (exact hosts and ports, no guessing)
  2. check Panorama for rules that already cover them
  3. draft least-privilege rules
  4. run them through the bank's network policy and fix violations
  5. submit the draft for Network Security to review

Everything here is pure Python (no I/O, no Temporal imports), so the workflow
can import it to build prompts, activities can run the tools, and tests can
drive it. The CMDB and Panorama data are mock data shaped like the real thing;
swapping in real lookups means changing `run_tool`, nothing else.
"""

import ipaddress
import json
import re
from typing import Any, Dict, List, Optional, Tuple

DATA_CENTER_RANGE = ipaddress.ip_network("10.20.0.0/16")
MAX_RULES = 8
NAME_RE = re.compile(r"^aws-[a-z0-9-]+-to-[a-z0-9-]+$")

# ---------------------------------------------------------------------------
# Mock systems the tools read
# ---------------------------------------------------------------------------

GENERIC_KEYWORDS = {"data center", "datacenter", "shared services", "on-prem services", "database", "db"}

CMDB: List[Dict[str, Any]] = [
    {"ci": "ORA-LOANS-PRD01", "name": "Oracle loan origination database", "ip": "10.20.14.25", "zone": "datacenter",
     "owner": "Mortgage Technology", "keywords": ["oracle", "loan", "database", "db"],
     "services": [{"port": 1521, "protocol": "tcp", "service": "Oracle TNS", "slug": "oracle-loans"}]},
    {"ci": "ZOS-PRD-LPAR2", "name": "Mainframe (z/OS) core banking region", "ip": "10.20.2.10", "zone": "datacenter",
     "owner": "Core Banking Platform", "keywords": ["mainframe", "z/os", "zos", "cics", "core banking"],
     "services": [{"port": 1414, "protocol": "tcp", "service": "IBM MQ", "slug": "mainframe-mq"},
                  {"port": 992, "protocol": "tcp", "service": "TN3270 over TLS", "slug": "mainframe-tn3270"}]},
    {"ci": "DC-DNS-01", "name": "Data center DNS resolvers", "ip": "10.20.0.53", "zone": "datacenter",
     "owner": "Network Services", "keywords": ["dns", "data center", "datacenter", "shared services", "on-prem services"],
     "services": [{"port": 53, "protocol": "udp", "service": "DNS", "slug": "dns"}]},
    {"ci": "DC-NTP-01", "name": "Data center NTP servers", "ip": "10.20.0.123", "zone": "datacenter",
     "owner": "Network Services", "keywords": ["ntp", "time", "data center", "datacenter", "shared services", "on-prem services"],
     "services": [{"port": 123, "protocol": "udp", "service": "NTP", "slug": "ntp"}]},
    {"ci": "AD-DC-01", "name": "Active Directory domain controllers", "ip": "10.20.1.10", "zone": "datacenter",
     "owner": "Identity & Access Management", "keywords": ["active directory", "ldap", "domain controller", "kerberos"],
     "services": [{"port": 636, "protocol": "tcp", "service": "LDAPS", "slug": "ad-ldaps"},
                  {"port": 88, "protocol": "tcp", "service": "Kerberos", "slug": "ad-kerberos"}]},
    {"ci": "SPLUNK-HF-01", "name": "Splunk heavy forwarders", "ip": "10.20.30.40", "zone": "datacenter",
     "owner": "Security Engineering", "keywords": ["splunk", "siem", "logging"],
     "services": [{"port": 9997, "protocol": "tcp", "service": "Splunk forwarding", "slug": "splunk"}]},
]

EXISTING_RULES: List[Dict[str, Any]] = [
    {"name": "aws-shared-to-dns", "source": "10.48.0.0/14", "destination": "10.20.0.53", "port": 53, "protocol": "udp",
     "note": "Every workload VPC can already resolve on-prem DNS"},
    {"name": "aws-shared-to-ad-ldaps", "source": "10.48.0.0/14", "destination": "10.20.1.10", "port": 636, "protocol": "tcp",
     "note": "Every workload VPC can already reach LDAPS"},
]

APPROVED_PORTS = {("tcp", 1521): "Oracle TNS", ("tcp", 1414): "IBM MQ", ("udp", 53): "DNS", ("udp", 123): "NTP",
                  ("tcp", 443): "HTTPS", ("tcp", 9997): "Splunk forwarding", ("tcp", 636): "LDAPS", ("tcp", 88): "Kerberos"}
NEEDS_EXCEPTION = {992: "Interactive TN3270 sessions from the cloud need a CISO exception",
                   22: "SSH from the cloud needs a CISO exception", 3389: "RDP from the cloud needs a CISO exception"}
PROHIBITED = {23: "Telnet is prohibited", 21: "FTP is prohibited", 389: "Unencrypted LDAP is prohibited; use 636"}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def lookup_cmdb(query: str) -> Dict[str, Any]:
    q = (query or "").lower()
    scored = []
    for ci in CMDB:
        hits = [k for k in ci["keywords"] if k in q]
        if hits:
            scored.append((sum(1 for k in hits if k not in GENERIC_KEYWORDS), ci))
    best = max((s for s, _ in scored), default=0)
    matches = [ci for s, ci in scored if s == best] if best > 0 else [ci for _, ci in scored]
    return {"query": query, "matches": [{k: ci[k] for k in ("ci", "name", "ip", "zone", "owner", "services")} for ci in matches]}


def check_existing_rules(source_cidr: str, destination: str) -> Dict[str, Any]:
    try:
        src = ipaddress.ip_network(source_cidr, strict=False)
    except ValueError:
        return {"destination": destination, "existing": [], "error": f"Not a CIDR: {source_cidr}"}
    found = []
    for r in EXISTING_RULES:
        if r["destination"] == destination and src.subnet_of(ipaddress.ip_network(r["source"])):
            found.append(dict(r))
    return {"destination": destination, "device_group": "DG-AWS-Workloads", "existing": found}


def check_policy(rules: List[Dict[str, Any]], vpc_cidr: str) -> Dict[str, Any]:
    violations: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    seen = set()
    if len(rules) > MAX_RULES:
        violations.append({"rule": "*", "problem": f"At most {MAX_RULES} rules per account; got {len(rules)}"})
    for r in rules:
        name = str(r.get("name", ""))
        label = name or "(unnamed rule)"
        missing = [f for f in ("name", "source", "destination", "port", "protocol") if r.get(f) in (None, "")]
        if missing:
            violations.append({"rule": label, "problem": f"Missing {', '.join(missing)}"})
            continue
        if not NAME_RE.match(name):
            violations.append({"rule": label, "problem": "Names must follow aws-<account>-to-<service>"})
        if str(r["source"]) != vpc_cidr:
            violations.append({"rule": label, "problem": f"Source must be this account's VPC CIDR {vpc_cidr}"})
        dest = str(r["destination"]).lower()
        if dest in ("any", "0.0.0.0/0", "*"):
            violations.append({"rule": label, "problem": "Destination 'any' is not allowed"})
        else:
            try:
                net = ipaddress.ip_network(dest, strict=False)
                if net.prefixlen < 32:
                    violations.append({"rule": label, "problem": "Destination must be a single host (/32)"})
                elif not net.subnet_of(DATA_CENTER_RANGE):
                    violations.append({"rule": label, "problem": f"Destination is outside the data center range {DATA_CENTER_RANGE}"})
            except ValueError:
                violations.append({"rule": label, "problem": f"Destination '{r['destination']}' is not an IP address"})
        proto = str(r["protocol"]).lower()
        try:
            port = int(r["port"])
        except (TypeError, ValueError):
            violations.append({"rule": label, "problem": "Port must be a single number"})
            continue
        if proto not in ("tcp", "udp"):
            violations.append({"rule": label, "problem": "Protocol must be tcp or udp"})
        if port in PROHIBITED:
            violations.append({"rule": label, "problem": PROHIBITED[port]})
        elif port in NEEDS_EXCEPTION:
            violations.append({"rule": label, "problem": NEEDS_EXCEPTION[port]})
        elif (proto, port) not in APPROVED_PORTS:
            violations.append({"rule": label, "problem": f"{proto}/{port} is not on the approved service list"})
        key = (dest, port, proto)
        if key in seen:
            violations.append({"rule": label, "problem": "Duplicate of another rule in this draft"})
        seen.add(key)
        for ex in EXISTING_RULES:
            if (ex["destination"], ex["port"], ex["protocol"]) == key:
                warnings.append({"rule": label, "problem": f"Already allowed by {ex['name']}"})
    return {"compliant": not violations, "checked": len(rules), "violations": violations, "warnings": warnings}


# [snippet:plain-tools]
def run_tool(name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if name == "lookup_cmdb":
        return lookup_cmdb(str(args.get("query", "")))
    if name == "check_existing_rules":
        return check_existing_rules(str(args.get("source_cidr", "")), str(args.get("destination", "")))
    if name == "check_policy":
        return check_policy(list(args.get("rules") or []), context.get("vpc_cidr", ""))
    return {"error": f"Unknown tool '{name}'"}
# [/snippet]


# Which demo system each tool "calls", for failure injection.
TOOL_SYSTEMS = {"lookup_cmdb": "servicenow", "check_existing_rules": "firewall", "check_policy": "policy"}


# ---------------------------------------------------------------------------
# Prompt and tool schemas
# ---------------------------------------------------------------------------

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "aws-<account>-to-<service>"},
        "source": {"type": "string", "description": "The account's VPC CIDR"},
        "destination": {"type": "string", "description": "Single host IP from the CMDB"},
        "destination_name": {"type": "string"},
        "port": {"type": "integer"},
        "protocol": {"type": "string", "enum": ["tcp", "udp"]},
        "purpose": {"type": "string"},
    },
    "required": ["name", "source", "destination", "port", "protocol", "purpose"],
}

TOOLS: List[Dict[str, Any]] = [
    {"name": "lookup_cmdb", "description": "Find on-prem systems in the CMDB by name or description. Returns hosts, IPs and the ports each service listens on.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "check_existing_rules", "description": "List Panorama rules that already allow traffic from a source CIDR to a destination host.",
     "input_schema": {"type": "object", "properties": {"source_cidr": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source_cidr", "destination"]}},
    {"name": "check_policy", "description": "Validate a draft rule set against the bank's network security policy. Returns violations to fix.",
     "input_schema": {"type": "object", "properties": {"rules": {"type": "array", "items": RULE_SCHEMA}}, "required": ["rules"]}},
    {"name": "submit_rules", "description": "Submit the final, policy-compliant rule set for human review by Network Security.",
     "input_schema": {"type": "object", "properties": {"rules": {"type": "array", "items": RULE_SCHEMA}, "rationale": {"type": "string", "description": "2-4 sentences: what you opened, what you left out and why"}}, "required": ["rules", "rationale"]}},
]


def system_prompt(org: str) -> str:
    return (
        f"You draft firewall rules for new AWS accounts on behalf of Network Security at {org}. "
        "Rules open traffic from the account's VPC to specific on-prem hosts. Requirements: source is exactly the VPC CIDR; "
        "destination is a single host IP taken from the CMDB; one port and protocol per rule; names follow "
        "aws-<account>-to-<service>. Never invent IPs or ports: look them up. Don't duplicate rules that already exist. "
        "Always run check_policy before submitting, and drop (and explain) anything the policy won't allow rather than "
        "submitting a violation. Keep any text between tool calls to one short sentence. When a reviewer asks for changes, "
        "make them, re-run check_policy, and submit again."
    )


def agent_context(spec: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    targets = [t for t in (spec.get("onprem_targets") or []) if t]
    if not targets and spec.get("onprem_connectivity"):
        targets = ["data center shared services"]
    return {
        "request_id": spec.get("request_id") or context.get("request_id"),
        "account_name": spec.get("account_name", "workload"),
        "environment": spec.get("environment"),
        "data_classification": spec.get("data_classification"),
        "vpc_cidr": context.get("vpc_cidr", ""),
        "targets": targets,
        "request_text": spec.get("request_text", ""),
    }


def task_prompt(ctx: Dict[str, Any]) -> str:
    return (
        "Draft firewall rules for a new AWS account.\n\n"
        f"Account: {ctx['account_name']} ({ctx.get('environment')}, {ctx.get('data_classification')} data)\n"
        f"VPC CIDR (rule source): {ctx['vpc_cidr']}\n"
        f"On-prem systems it needs to reach: {', '.join(ctx['targets']) or 'none listed'}\n"
        f"Original request: \"{ctx.get('request_text', '')}\"\n\n"
        "Look up each system, check for existing rules, draft, run check_policy, fix violations, then call submit_rules."
    )


def feedback_message(reviewer: str, comment: str) -> str:
    return f"Reviewer {reviewer} requested changes: {comment}\nRevise the rules, re-run check_policy, and submit again."


# ---------------------------------------------------------------------------
# Reading a conversation
# ---------------------------------------------------------------------------

def tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every tool call in the conversation, paired with its result when there is one."""
    uses: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if m["role"] == "assistant" and b.get("type") == "tool_use":
                uses[b["id"]] = {"id": b["id"], "name": b["name"], "input": b.get("input") or {}, "output": None}
                order.append(b["id"])
            elif m["role"] == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                raw = b.get("content")
                if isinstance(raw, list):
                    raw = "".join(x.get("text", "") for x in raw if isinstance(x, dict))
                try:
                    uses[b["tool_use_id"]]["output"] = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    uses[b["tool_use_id"]]["output"] = {"text": raw}
    return [uses[i] for i in order]


def feedback(messages: List[Dict[str, Any]]) -> List[str]:
    out = []
    for m in messages:
        if m["role"] != "user":
            continue
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        for b in blocks:
            text = b.get("text", "") if b.get("type") == "text" else ""
            if text.startswith("Reviewer ") and "requested changes:" in text:
                out.append(text.split("requested changes:", 1)[1].split("\nRevise", 1)[0].strip())
    return out


def describe_call(name: str, args: Dict[str, Any], output: Optional[Dict[str, Any]]) -> str:
    output = output or {}
    if name == "lookup_cmdb":
        found = output.get("matches", [])
        return f"“{args.get('query')}”: " + (", ".join(f"{m['name']} ({m['ip']})" for m in found) if found else "no match")
    if name == "check_existing_rules":
        ex = output.get("existing", [])
        return f"{args.get('destination')}: " + (", ".join(e["name"] for e in ex) if ex else "no existing rule")
    if name == "check_policy":
        if output.get("compliant"):
            return f"{output.get('checked', 0)} rules pass policy"
        return f"{len(output.get('violations', []))} violation(s): " + "; ".join(f"{v['rule']}: {v['problem']}" for v in output.get("violations", [])[:3])
    if name == "submit_rules":
        return f"Submitted {len(args.get('rules') or [])} rules for review"
    return json.dumps(output)[:160]


# ---------------------------------------------------------------------------
# Offline brain: what the agent does with no API key
# ---------------------------------------------------------------------------

_REMOVE_WORDS = ("drop", "remove", "delete", "no ", "don't", "do not", "without", "exclude", "not needed")


def _rules_key(rules: List[Dict[str, Any]]) -> List[Tuple[str, int, str]]:
    return sorted((str(r.get("destination")), int(r.get("port", 0) or 0), str(r.get("protocol"))) for r in rules)


def _candidates(hosts: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = []
    for h in hosts:
        for svc in h["services"]:
            rules.append({
                "name": f"aws-{ctx['account_name']}-to-{svc['slug']}",
                "source": ctx["vpc_cidr"],
                "destination": h["ip"],
                "destination_name": h["name"],
                "port": svc["port"],
                "protocol": svc["protocol"],
                "purpose": f"{svc['service']} to {h['name']}",
                "_service": svc["service"],
                "_slug": svc["slug"],
            })
    return rules


def _excluded_by_feedback(rule: Dict[str, Any], comments: List[str]) -> Optional[str]:
    for c in comments:
        low = c.lower()
        if not any(w in low for w in _REMOVE_WORDS):
            continue
        tokens = {rule["_slug"].split("-")[-1], rule["_service"].lower(), str(rule["port"])}
        tokens |= {w for w in rule["_service"].lower().split() if len(w) > 2 and w not in ("over", "the")}
        if any(t and t in low for t in tokens):
            return c
    return None


def _text(t: str) -> Dict[str, Any]:
    return {"type": "text", "text": t}


def _use(turn: int, i: int, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_offline_{turn}_{i}", "name": name, "input": args}


def heuristic_turn(messages: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decide the next assistant turn from the conversation so far (deterministic)."""
    calls = tool_calls(messages)
    comments = feedback(messages)
    turn = sum(1 for m in messages if m["role"] == "assistant") + 1
    targets = ctx.get("targets") or []

    looked = [c for c in calls if c["name"] == "lookup_cmdb" and c["output"] is not None]
    if len(looked) < len(targets):
        todo = targets[len(looked):]
        return [_text(f"Looking up {', '.join(todo)} in the CMDB for exact hosts and ports."),
                *[_use(turn, i, "lookup_cmdb", {"query": t}) for i, t in enumerate(todo)]]

    hosts: Dict[str, Dict[str, Any]] = {}
    for c in looked:
        for m in c["output"].get("matches", []):
            hosts.setdefault(m["ci"], m)
    host_list = list(hosts.values())

    checked = [c for c in calls if c["name"] == "check_existing_rules" and c["output"] is not None]
    if host_list and not checked:
        return [_text(f"Found {len(host_list)} system(s). Checking Panorama so I don't duplicate an existing rule."),
                *[_use(turn, i, "check_existing_rules", {"source_cidr": ctx["vpc_cidr"], "destination": h["ip"]}) for i, h in enumerate(host_list)]]

    covered = {}
    for c in checked:
        for r in c["output"].get("existing", []):
            covered[(r["destination"], r["port"], r["protocol"])] = r["name"]
    policy_calls = [c for c in calls if c["name"] == "check_policy" and c["output"] is not None]
    blocked = {}
    for c in policy_calls:
        for v in c["output"].get("violations", []):
            blocked[v["rule"]] = v["problem"]

    draft, skipped_existing, skipped_policy, skipped_review = [], [], [], []
    for r in _candidates(host_list, ctx):
        key = (r["destination"], r["port"], r["protocol"])
        if key in covered:
            skipped_existing.append(f"{r['_service']} (already allowed by {covered[key]})")
        elif r["name"] in blocked:
            skipped_policy.append(f"{r['_service']} ({blocked[r['name']]})")
        elif _excluded_by_feedback(r, comments):
            skipped_review.append(r["_service"])
        else:
            draft.append({k: v for k, v in r.items() if not k.startswith("_")})

    last = policy_calls[-1] if policy_calls else None
    if last is None or _rules_key(last["input"].get("rules") or []) != _rules_key(draft):
        if last is not None and not last["output"].get("compliant"):
            lead = f"Dropped what the policy won't allow, leaving {len(draft)} rule(s)"
        elif comments and last is not None:
            lead = "Revised the draft per the reviewer"
        else:
            lead = f"Drafted {len(draft)} least-privilege rule(s)"
        return [_text(f"{lead}; validating against network policy."), _use(turn, 0, "check_policy", {"rules": draft})]

    parts = [f"Opens {len(draft)} rule(s) from {ctx['vpc_cidr']} to specific hosts and ports taken from the CMDB."]
    if skipped_existing:
        parts.append("Left out " + "; ".join(skipped_existing) + ".")
    if skipped_policy:
        parts.append("Dropped " + "; ".join(skipped_policy) + ".")
    if skipped_review:
        parts.append("Removed per reviewer: " + ", ".join(skipped_review) + ".")
    if comments and not skipped_review:
        parts.append(f"Reviewer note considered: “{comments[-1]}”; no rule changes were needed.")
    return [_text("Policy check passed. Submitting for Network Security review."),
            _use(turn, 0, "submit_rules", {"rules": draft, "rationale": " ".join(parts)})]
