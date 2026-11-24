"""Mock connector: realistic timing, log output and IDs, no external calls.

IDs are derived from the request id, so a retried step returns the same
account, VPC or ticket. That mirrors what the real connectors do (look up
before create), which is what makes Temporal retries safe.
"""

from typing import Any, Callable, Dict, List

from ..ai.tasks import stable_hash
from ..subnets import plan_subnets
from .base import Connector, StepContext


def _hex(ctx: StepContext, salt: str, n: int) -> str:
    out, i = "", 0
    while len(out) < n:
        out += format(stable_hash(ctx.request_id, salt, i), "012x")
        i += 1
    return out[:n]


def account_id(ctx: StepContext) -> str:
    return ctx.context.get("account_id") or str(100_000_000_000 + stable_hash(ctx.request_id, "acct") % 899_999_999_999)


def _name(ctx: StepContext) -> str:
    return ctx.spec.get("account_name", "workload")


def _cidr(ctx: StepContext) -> str:
    size = ctx.spec.get("cidr_size", "/22")
    return f"10.48.{(stable_hash(ctx.request_id, 'cidr') % 63) * 4}.0{size}"


# action -> (output builder, log script)
Builder = Callable[[StepContext], Dict[str, Any]]


def _outputs() -> Dict[str, Builder]:
    return {
        "create_change": lambda c: {
            "change_number": f"CHG{stable_hash(c.request_id, 'chg') % 10_000_000:07d}",
            "change_url": f"https://example.service-now.com/change_request.do?sysparm_query=number=CHG{stable_hash(c.request_id, 'chg') % 10_000_000:07d}",
            "change_state": "Assess",
        },
        "update_change": lambda c: {"change_state": c.params.get("state", "Implement")},
        "create_account": lambda c: {"account_id": account_id(c), "account_email": f"aws+{_name(c)}@example.com", "account_name": _name(c)},
        "move_to_ou": lambda c: {"ou_id": f"ou-{_hex(c, 'ou', 4)}-{_hex(c, 'ou2', 8)}", "ou_path": f"Root/Workloads/{c.spec.get('environment', 'dev').title()}"},
        "attach_scps": lambda c: {"scps": ["DenyRootUser", "RegionRestriction", "RequireIMDSv2", "ProtectSecurityServices"]},
        "create_group": lambda c: {"okta_group_id": f"00g{_hex(c, 'okta', 17)}", "okta_group": f"aws-{_name(c)}-users"},
        "create_permission_sets": lambda c: {"permission_sets": ["ReadOnlyAccess", "PowerUserAccess", "AdministratorAccess"]},
        "assign_groups": lambda c: {"assignments": 3},
        "create_vpc": lambda c: {
            "vpc_id": f"vpc-{_hex(c, 'vpc', 17)}",
            "subnet_cidrs": plan_subnets(c.context.get("vpc_cidr") or _cidr(c)),
            "subnet_ids": [f"subnet-{_hex(c, 'sn' + str(i), 17)}" for i in range(3)],
            "vpc_cidr": c.context.get("vpc_cidr") or _cidr(c),
        },
        "attach_tgw": lambda c: {"tgw_attachment_id": f"tgw-attach-{_hex(c, 'tgw', 17)}"},
        "create_private_zone": lambda c: {"hosted_zone_id": f"Z0{_hex(c, 'zone', 12).upper()}", "private_zone": f"{_name(c)}.aws.internal"},
        "enable_detectors": lambda c: {"guardduty_detector_id": _hex(c, "gd", 32), "security_hub": "enabled"},
        "deploy_conformance_pack": lambda c: {"conformance_pack": "Operational-Best-Practices-for-FFIEC"},
        "configure_log_delivery": lambda c: {"log_archive_bucket": f"org-log-archive-{_hex(c, 'logs', 6)}", "cloudtrail": "organization-trail"},
        "create_hec_input": lambda c: {"splunk_index": f"aws_{_name(c).replace('-', '_')}", "hec_token": f"••••{_hex(c, 'hec', 4)}"},
        "create_budget": lambda c: {"budget_name": f"{_name(c)}-monthly", "budget_limit_usd": int(c.spec.get("monthly_budget_usd", 1500))},
        "tag_account": lambda c: {"tags": {"CostCenter": c.spec.get("cost_center", ""), "Environment": c.spec.get("environment", ""), "DataClassification": c.spec.get("data_classification", "")}},
        "create_ci": lambda c: {"ci_number": f"CI{stable_hash(c.request_id, 'ci') % 10_000_000:07d}", "ci_sys_id": _hex(c, "ci", 32)},
        "link_ci": lambda c: {"ci_linked": True},
    }


PLAYBOOK_OUTPUTS: Dict[str, Builder] = {
    "ad_groups.yml": lambda c: {"ad_groups": [f"SG-AWS-{account_id(c)}-{r}" for r in ("Admin", "Developer", "ReadOnly")]},
    "ipam_allocate.yml": lambda c: {"vpc_cidr": _cidr(c), "ipam_network_ref": f"network/ZG5z{_hex(c, 'ipam', 10)}"},
    "firewall_rules.yml": lambda c: {"staged_rules": [r.get("name") for r in (c.context.get("approved_rules") or [])], "device_group": "DG-AWS-Workloads"},
    "panorama_commit.yml": lambda c: {"panorama_job_id": str(4000 + stable_hash(c.request_id, "pan") % 999)},
    "dns_forwarders.yml": lambda c: {"dns_forwarder": f"{_name(c)}.aws.internal -> 10.48.0.2"},
}


def _script(ctx: StepContext, out: Dict[str, Any]) -> List[str]:
    a, name = ctx.action, _name(ctx)
    scripts = {
        "create_change": ["POST /api/now/table/change_request (standard change: AWS account vending)", f"Created {out.get('change_number')} in state Assess"],
        "create_account": [f"organizations:CreateAccount AccountName={name}", "CreateAccountStatus: IN_PROGRESS", "Waiting for account to be provisioned", f"CreateAccountStatus: SUCCEEDED AccountId={out.get('account_id')}"],
        "move_to_ou": [f"organizations:MoveAccount -> {out.get('ou_path')}", "Account placed in OU"],
        "attach_scps": [f"Attaching {len(out.get('scps', []))} service control policies", "Guardrails active"],
        "create_group": [f"POST /api/v1/groups name={out.get('okta_group')}", "PUT /api/v1/apps/aws-identity-center/groups (push group)", "Group push: ACTIVE"],
        "create_permission_sets": ["sso-admin:CreatePermissionSet x3", "Attached managed policies"],
        "assign_groups": [f"sso-admin:CreateAccountAssignment target={account_id(ctx)}", "3 assignments provisioned"],
        "create_vpc": [f"ec2:CreateVpc CidrBlock={out.get('vpc_cidr')}", "Subnets: " + ", ".join(out.get("subnet_cidrs") or []), f"VPC {out.get('vpc_id')} available"],
        "attach_tgw": ["ec2:CreateTransitGatewayVpcAttachment", "Attachment state: pending", "Attachment state: available", "Propagating routes to on-prem route table"],
        "create_private_zone": [f"route53:CreateHostedZone {out.get('private_zone')} (private)", "Associated with VPC"],
        "enable_detectors": ["guardduty:CreateDetector", "securityhub:EnableSecurityHub (CIS, FSBP standards)"],
        "deploy_conformance_pack": ["config:PutConformancePack Operational-Best-Practices-for-FFIEC", "Evaluating 112 rules"],
        "configure_log_delivery": ["Verifying organization trail covers the account", "Log delivery to archive bucket confirmed"],
        "create_hec_input": [f"POST /services/data/inputs/http name=aws-{name}", f"Index {out.get('splunk_index')} ready"],
        "create_budget": [f"budgets:CreateBudget limit=${out.get('budget_limit_usd', 0):,}", "Alerts at 80% and 100% to account owners"],
        "tag_account": ["organizations:TagResource", "Tags applied: " + ", ".join(f"{k}={v}" for k, v in (out.get('tags') or {}).items())],
        "create_ci": ["POST /api/now/table/cmdb_ci_cloud_service_account", f"Created {out.get('ci_number')}"],
        "link_ci": ["POST /api/now/table/task_ci", "CI linked to change"],
    }
    return scripts.get(a, [f"Calling {ctx.system_label}: {a}", "Done"])


PRE_LINES = {
    "create_vpc": lambda c: f"Planning subnets for {c.context.get('vpc_cidr') or _cidr(c)} across 3 availability zones",
}


class MockConnector(Connector):
    mode = "mock"

    def build_output(self, ctx: StepContext) -> Dict[str, Any]:
        if ctx.action == "run_playbook":
            fn = PLAYBOOK_OUTPUTS.get(ctx.params.get("playbook", ""), lambda c: {})
            return fn(ctx)
        fn = _outputs().get(ctx.action)
        return fn(ctx) if fn else {}

    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        pre = PRE_LINES.get(ctx.action)
        if pre:  # some steps do a little work before computing their result
            ctx.progress(pre(ctx))
            await ctx.wait(min(1.0, ctx.seconds * ctx.speed * 0.3))
        out = self.build_output(ctx)
        lines = _script(ctx, out)
        await self.play(ctx, lines)
        return out

    async def play(self, ctx: StepContext, lines: List[str]) -> None:
        total = max(0.2, ctx.seconds * ctx.speed)
        per = total / max(1, len(lines))
        for i, line in enumerate(lines):
            ctx.progress(line, pct=round(100 * i / len(lines)))
            await ctx.wait(per)
        ctx.progress(None, pct=100)

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        orig = ctx.original_output or {}
        ident = next((str(v) for k, v in orig.items() if k.endswith("_id") or k.endswith("_number")), "")
        await self.play(ctx, [f"Undo {ctx.step_name.lower()} {ident}".strip(), "Rolled back"])
        return {"undone": True}
