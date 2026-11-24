"""Real AWS connector (boto3).

Turn it on per system in config/connectors.yaml (mode: real). It starts in
`dry_run: true`, which logs every AWS call it would make and returns mock IDs,
so you can run the full demo against a real config without touching AWS.
Set `dry_run: false` once the account, OU and SCP IDs in connectors.yaml are
filled in.

Every action is written to be safe to retry: it looks for the resource first
(by name, tag or CallerReference) and only creates what is missing. CreateAccount
also checkpoints its request id in the activity heartbeat, so if a worker dies
mid-provisioning, the next attempt resumes polling instead of creating a
second account.

Closing an account on rollback is destructive, so it is off unless
`allow_destructive_compensation: true`.

Written against the boto3 API but not yet run against a live organization:
dry-run it first.
"""

import asyncio
from typing import Any, Dict, List

from ..subnets import plan_subnets
from .base import Connector, StepContext, StepFailed
from .mock import MockConnector

RETRYABLE_CODES = {"Throttling", "ThrottlingException", "TooManyRequestsException", "ConcurrentModificationException",
                   "ServiceUnavailable", "InternalFailure", "RequestLimitExceeded", "PriorRequestNotComplete"}

# What dry-run mode prints for each action.
DRY_RUN_CALLS: Dict[str, List[str]] = {
    "create_account": ["organizations:ListAccounts (idempotency check)", "organizations:CreateAccount", "organizations:DescribeCreateAccountStatus (poll)"],
    "move_to_ou": ["organizations:ListParents", "organizations:MoveAccount"],
    "attach_scps": ["organizations:AttachPolicy (per SCP)"],
    "tag_account": ["organizations:TagResource"],
    "create_permission_sets": ["sso-admin:ListInstances", "sso-admin:CreatePermissionSet", "sso-admin:AttachManagedPolicyToPermissionSet"],
    "assign_groups": ["identitystore:GetGroupId", "sso-admin:CreateAccountAssignment (per permission set)"],
    "create_vpc": ["sts:AssumeRole (member account)", "ec2:DescribeVpcs (by request tag)", "ec2:CreateVpc", "ec2:CreateSubnet x3"],
    "attach_tgw": ["ec2:CreateTransitGatewayVpcAttachment", "ec2:DescribeTransitGatewayVpcAttachments (poll)"],
    "create_private_zone": ["route53:ListHostedZonesByName", "route53:CreateHostedZone (private)"],
    "enable_detectors": ["guardduty:ListDetectors", "guardduty:CreateDetector", "securityhub:EnableSecurityHub"],
    "deploy_conformance_pack": ["config:PutConformancePack"],
    "configure_log_delivery": ["cloudtrail:DescribeTrails (organization trail)"],
    "create_budget": ["budgets:CreateBudget"],
}


class AwsConnector(Connector):
    mode = "real"

    def __init__(self, system: str, config: Dict[str, Any]):
        super().__init__(system, config)
        self.dry_run = bool(config.get("dry_run", True))
        self._mock = MockConnector(system, config)
        self._session = None

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["mode"] = "real (dry run)" if self.dry_run else "real"
        return d

    # --- plumbing ----------------------------------------------------------
    def _base_session(self):
        if self._session is None:
            import boto3

            self._session = boto3.Session(profile_name=self.config.get("profile") or None, region_name=self.config.get("region", "us-east-1"))
        return self._session

    def _client(self, service: str, session=None):
        return (session or self._base_session()).client(service)

    async def _member_session(self, account_id: str):
        import boto3

        role = f"arn:aws:iam::{account_id}:role/{self.config.get('member_role_name', 'OrganizationAccountAccessRole')}"
        creds = (await self._aws(self._client("sts").assume_role, RoleArn=role, RoleSessionName="domainos-vending"))["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.config.get("region", "us-east-1"),
        )

    async def _aws(self, fn, **kwargs):
        from botocore.exceptions import ClientError

        try:
            return await asyncio.to_thread(fn, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            raise StepFailed(f"{code}: {e.response.get('Error', {}).get('Message', str(e))}", code, retryable=code in RETRYABLE_CODES) from e

    def _acct(self, ctx: StepContext) -> str:
        acct = ctx.context.get("account_id") or (ctx.original_output or {}).get("account_id")
        if not acct:
            raise StepFailed("No account_id from an earlier step", "MissingInput", retryable=False)
        return acct

    def _tags(self, ctx: StepContext) -> List[Dict[str, str]]:
        s = ctx.spec
        return [
            {"Key": "domainos:request", "Value": ctx.request_id},
            {"Key": "CostCenter", "Value": str(s.get("cost_center", ""))},
            {"Key": "Environment", "Value": str(s.get("environment", ""))},
            {"Key": "DataClassification", "Value": str(s.get("data_classification", ""))},
        ]

    # --- entry points ------------------------------------------------------
    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        if self.dry_run:
            for call in DRY_RUN_CALLS.get(ctx.action, [f"{ctx.action} (no dry-run plan)"]):
                ctx.progress(f"[dry run] {call}")
                await ctx.wait(max(0.3, ctx.seconds * ctx.speed / 4))
            return self._mock.build_output(ctx)
        fn = getattr(self, f"_do_{ctx.action}", None)
        if not fn:
            raise StepFailed(f"AWS connector has no action '{ctx.action}'", "UnknownAction", retryable=False)
        return await fn(ctx)

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        undo = ctx.action
        if self.dry_run:
            ctx.progress(f"[dry run] undo {undo} for {ctx.original_output}")
            return {"undone": True, "dry_run": True}
        fn = getattr(self, f"_undo_{undo}", None)
        if not fn:
            ctx.progress(f"No undo implemented for {undo}")
            return {"undone": False}
        return await fn(ctx)

    # --- organizations -----------------------------------------------------
    async def _do_create_account(self, ctx: StepContext) -> Dict[str, Any]:
        org = self._client("organizations")
        name = ctx.spec["account_name"]
        email = self.config.get("account_email_template", "aws+{name}@example.com").format(name=name)
        pages = org.get_paginator("list_accounts").paginate()
        for page in await asyncio.to_thread(list, pages):
            for a in page["Accounts"]:
                if a["Name"] == name or a["Email"] == email:
                    ctx.progress(f"Account {a['Id']} already exists; reusing it")
                    return {"account_id": a["Id"], "account_email": a["Email"], "account_name": name}
        req_id = ctx.resume.get("create_account_request_id")
        if req_id:
            ctx.progress(f"Resuming CreateAccount request {req_id} from previous attempt")
        else:
            resp = await self._aws(org.create_account, Email=email, AccountName=name,
                                   RoleName=self.config.get("member_role_name", "OrganizationAccountAccessRole"),
                                   IamUserAccessToBilling="DENY", Tags=self._tags(ctx))
            req_id = resp["CreateAccountStatus"]["Id"]
            ctx.progress(f"CreateAccount request {req_id}", checkpoint={"create_account_request_id": req_id})
        while True:
            st = (await self._aws(org.describe_create_account_status, CreateAccountRequestId=req_id))["CreateAccountStatus"]
            if st["State"] == "SUCCEEDED":
                ctx.progress(f"CreateAccountStatus: SUCCEEDED AccountId={st['AccountId']}")
                return {"account_id": st["AccountId"], "account_email": email, "account_name": name}
            if st["State"] == "FAILED":
                reason = st.get("FailureReason", "UNKNOWN")
                raise StepFailed(f"CreateAccount failed: {reason}", "ConstraintViolationException" if "LIMIT" in reason else "CreateAccountFailed", retryable=False)
            ctx.progress("CreateAccountStatus: IN_PROGRESS", checkpoint={"create_account_request_id": req_id})
            await ctx.wait(10)

    async def _undo_close_account(self, ctx: StepContext) -> Dict[str, Any]:
        acct = self._acct(ctx)
        if not self.config.get("allow_destructive_compensation"):
            ctx.progress(f"Not closing {acct}: allow_destructive_compensation is false. Flagged for manual review.")
            return {"undone": False, "manual_review": True}
        await self._aws(self._client("organizations").close_account, AccountId=acct)
        ctx.progress(f"organizations:CloseAccount {acct}")
        return {"undone": True}

    async def _do_move_to_ou(self, ctx: StepContext) -> Dict[str, Any]:
        org, acct = self._client("organizations"), self._acct(ctx)
        env = ctx.spec.get("environment", "dev")
        target = (self.config.get("ou_ids") or {}).get(env)
        if not target:
            raise StepFailed(f"No OU configured for environment '{env}' (aws.ou_ids in connectors.yaml)", "MissingConfig", retryable=False)
        parent = (await self._aws(org.list_parents, ChildId=acct))["Parents"][0]["Id"]
        if parent != target:
            await self._aws(org.move_account, AccountId=acct, SourceParentId=parent, DestinationParentId=target)
        ctx.progress(f"Account {acct} is in {target}")
        return {"ou_id": target, "ou_path": f"Workloads/{env.title()}"}

    async def _do_attach_scps(self, ctx: StepContext) -> Dict[str, Any]:
        org, acct = self._client("organizations"), self._acct(ctx)
        attached = []
        for pid in self.config.get("scp_ids", []):
            try:
                await self._aws(org.attach_policy, PolicyId=pid, TargetId=acct)
            except StepFailed as e:
                if e.error_type != "DuplicatePolicyAttachmentException":
                    raise
            attached.append(pid)
            ctx.progress(f"SCP {pid} attached")
        return {"scps": attached}

    async def _undo_detach_scps(self, ctx: StepContext) -> Dict[str, Any]:
        org, acct = self._client("organizations"), self._acct(ctx)
        for pid in (ctx.original_output or {}).get("scps", []):
            try:
                await self._aws(org.detach_policy, PolicyId=pid, TargetId=acct)
            except StepFailed as e:
                if e.error_type != "PolicyNotAttachedException":
                    raise
        return {"undone": True}

    async def _do_tag_account(self, ctx: StepContext) -> Dict[str, Any]:
        acct = self._acct(ctx)
        tags = self._tags(ctx)
        await self._aws(self._client("organizations").tag_resource, ResourceId=acct, Tags=tags)
        return {"tags": {t["Key"]: t["Value"] for t in tags}}

    # --- IAM Identity Center ----------------------------------------------
    async def _instance(self):
        sso = self._client("sso-admin")
        inst = (await self._aws(sso.list_instances))["Instances"][0]
        return sso, inst["InstanceArn"], inst["IdentityStoreId"]

    async def _do_create_permission_sets(self, ctx: StepContext) -> Dict[str, Any]:
        sso, arn, _ = await self._instance()
        wanted = self.config.get("permission_sets") or [
            {"name": "ReadOnlyAccess", "policies": ["arn:aws:iam::aws:policy/ReadOnlyAccess"]},
            {"name": "PowerUserAccess", "policies": ["arn:aws:iam::aws:policy/PowerUserAccess"]},
        ]
        existing: Dict[str, str] = {}
        for ps_arn in (await self._aws(sso.list_permission_sets, InstanceArn=arn))["PermissionSets"]:
            d = (await self._aws(sso.describe_permission_set, InstanceArn=arn, PermissionSetArn=ps_arn))["PermissionSet"]
            existing[d["Name"]] = ps_arn
        arns: Dict[str, str] = {}
        for ps in wanted:
            ps_arn = existing.get(ps["name"])
            if not ps_arn:
                ps_arn = (await self._aws(sso.create_permission_set, InstanceArn=arn, Name=ps["name"], SessionDuration="PT8H"))["PermissionSet"]["PermissionSetArn"]
                ctx.progress(f"Created permission set {ps['name']}")
            for pol in ps.get("policies", []):
                try:
                    await self._aws(sso.attach_managed_policy_to_permission_set, InstanceArn=arn, PermissionSetArn=ps_arn, ManagedPolicyArn=pol)
                except StepFailed as e:
                    if e.error_type != "ConflictException":
                        raise
            arns[ps["name"]] = ps_arn
        return {"permission_sets": list(arns), "permission_set_arns": arns}

    async def _do_assign_groups(self, ctx: StepContext) -> Dict[str, Any]:
        sso, arn, store_id = await self._instance()
        acct = self._acct(ctx)
        group_name = ctx.context.get("okta_group") or f"aws-{ctx.spec['account_name']}-users"
        ids = self._client("identitystore")
        gid = (await self._aws(ids.get_group_id, IdentityStoreId=store_id,
                               AlternateIdentifier={"UniqueAttribute": {"AttributePath": "displayName", "AttributeValue": group_name}}))["GroupId"]
        n = 0
        for ps_arn in (ctx.context.get("permission_set_arns") or {}).values():
            try:
                await self._aws(sso.create_account_assignment, InstanceArn=arn, TargetId=acct, TargetType="AWS_ACCOUNT",
                                PermissionSetArn=ps_arn, PrincipalType="GROUP", PrincipalId=gid)
            except StepFailed as e:
                if e.error_type != "ConflictException":
                    raise
            n += 1
            ctx.progress(f"Assigned {group_name} to {acct}")
        return {"assignments": n, "identity_store_group_id": gid}

    # --- networking (member account) --------------------------------------
    async def _do_create_vpc(self, ctx: StepContext) -> Dict[str, Any]:
        sess = await self._member_session(self._acct(ctx))
        ec2 = self._client("ec2", sess)
        cidr = ctx.context.get("vpc_cidr")
        if not cidr:
            raise StepFailed("No CIDR allocated by IPAM", "MissingInput", retryable=False)
        found = (await self._aws(ec2.describe_vpcs, Filters=[{"Name": "tag:domainos:request", "Values": [ctx.request_id]}]))["Vpcs"]
        if found:
            vpc_id = found[0]["VpcId"]
            ctx.progress(f"VPC {vpc_id} already exists; reusing it")
        else:
            vpc_id = (await self._aws(ec2.create_vpc, CidrBlock=cidr, TagSpecifications=[{"ResourceType": "vpc", "Tags": self._tags(ctx)}]))["Vpc"]["VpcId"]
            ctx.progress(f"ec2:CreateVpc {vpc_id} {cidr}")
            await asyncio.to_thread(ec2.get_waiter("vpc_available").wait, VpcIds=[vpc_id])
        existing = (await self._aws(ec2.describe_subnets, Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]))["Subnets"]
        subnet_ids = [s["SubnetId"] for s in existing]
        if not subnet_ids:
            azs = [z["ZoneName"] for z in (await self._aws(ec2.describe_availability_zones))["AvailabilityZones"]][:3]
            for az, sub in zip(azs, plan_subnets(cidr, len(azs))):
                sid = (await self._aws(ec2.create_subnet, VpcId=vpc_id, CidrBlock=str(sub), AvailabilityZone=az,
                                       TagSpecifications=[{"ResourceType": "subnet", "Tags": self._tags(ctx)}]))["Subnet"]["SubnetId"]
                subnet_ids.append(sid)
                ctx.progress(f"Subnet {sid} {sub} in {az}")
        return {"vpc_id": vpc_id, "subnet_ids": subnet_ids, "vpc_cidr": cidr}

    async def _undo_delete_vpc(self, ctx: StepContext) -> Dict[str, Any]:
        out = ctx.original_output or {}
        sess = await self._member_session(self._acct(ctx))
        ec2 = self._client("ec2", sess)
        for sid in out.get("subnet_ids", []):
            try:
                await self._aws(ec2.delete_subnet, SubnetId=sid)
            except StepFailed as e:
                if "NotFound" not in e.error_type:
                    raise
        try:
            await self._aws(ec2.delete_vpc, VpcId=out["vpc_id"])
        except StepFailed as e:
            if "NotFound" not in e.error_type:
                raise
        return {"undone": True}

    async def _do_attach_tgw(self, ctx: StepContext) -> Dict[str, Any]:
        tgw = self.config.get("transit_gateway_id")
        if not tgw:
            raise StepFailed("aws.transit_gateway_id is not set in connectors.yaml", "MissingConfig", retryable=False)
        sess = await self._member_session(self._acct(ctx))
        ec2 = self._client("ec2", sess)
        vpc_id = ctx.context["vpc_id"]
        found = (await self._aws(ec2.describe_transit_gateway_vpc_attachments,
                                 Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "transit-gateway-id", "Values": [tgw]}]))["TransitGatewayVpcAttachments"]
        live = [a for a in found if a["State"] not in ("deleted", "deleting", "failed")]
        if live:
            att = live[0]["TransitGatewayAttachmentId"]
        else:
            att = (await self._aws(ec2.create_transit_gateway_vpc_attachment, TransitGatewayId=tgw, VpcId=vpc_id,
                                   SubnetIds=ctx.context.get("subnet_ids", []),
                                   TagSpecifications=[{"ResourceType": "transit-gateway-attachment", "Tags": self._tags(ctx)}]))["TransitGatewayVpcAttachment"]["TransitGatewayAttachmentId"]
        while True:
            a = (await self._aws(ec2.describe_transit_gateway_vpc_attachments, TransitGatewayAttachmentIds=[att]))["TransitGatewayVpcAttachments"][0]
            ctx.progress(f"Attachment {att}: {a['State']}")
            if a["State"] == "available":
                return {"tgw_attachment_id": att}
            if a["State"] == "pendingAcceptance":
                return {"tgw_attachment_id": att, "tgw_note": "Waiting for the network account to accept (enable auto-accept on the TGW)"}
            if a["State"] in ("failed", "rejected"):
                raise StepFailed(f"Transit Gateway attachment {att} is {a['State']}", "TgwAttachmentFailed", retryable=False)
            await ctx.wait(5)

    async def _undo_detach_tgw(self, ctx: StepContext) -> Dict[str, Any]:
        sess = await self._member_session(self._acct(ctx))
        att = (ctx.original_output or {}).get("tgw_attachment_id")
        if att:
            await self._aws(self._client("ec2", sess).delete_transit_gateway_vpc_attachment, TransitGatewayAttachmentId=att)
        return {"undone": True}

    async def _do_create_private_zone(self, ctx: StepContext) -> Dict[str, Any]:
        sess = await self._member_session(self._acct(ctx))
        r53 = self._client("route53", sess)
        name = f"{ctx.spec['account_name']}.{self.config.get('private_zone_suffix', 'aws.internal')}"
        zones = (await self._aws(r53.list_hosted_zones_by_name, DNSName=name, MaxItems="1"))["HostedZones"]
        if zones and zones[0]["Name"].rstrip(".") == name:
            zid = zones[0]["Id"].split("/")[-1]
        else:
            zid = (await self._aws(r53.create_hosted_zone, Name=name, CallerReference=f"{ctx.request_id}-{name}",
                                   VPC={"VPCRegion": self.config.get("region", "us-east-1"), "VPCId": ctx.context["vpc_id"]},
                                   HostedZoneConfig={"PrivateZone": True, "Comment": f"Domainos {ctx.request_id}"}))["HostedZone"]["Id"].split("/")[-1]
        return {"hosted_zone_id": zid, "private_zone": name}

    async def _undo_delete_private_zone(self, ctx: StepContext) -> Dict[str, Any]:
        sess = await self._member_session(self._acct(ctx))
        zid = (ctx.original_output or {}).get("hosted_zone_id")
        if zid:
            await self._aws(self._client("route53", sess).delete_hosted_zone, Id=zid)
        return {"undone": True}

    # --- security & logging -----------------------------------------------
    async def _do_enable_detectors(self, ctx: StepContext) -> Dict[str, Any]:
        sess = await self._member_session(self._acct(ctx))
        gd = self._client("guardduty", sess)
        dets = (await self._aws(gd.list_detectors))["DetectorIds"]
        det = dets[0] if dets else (await self._aws(gd.create_detector, Enable=True))["DetectorId"]
        try:
            await self._aws(self._client("securityhub", sess).enable_security_hub, EnableDefaultStandards=True)
        except StepFailed as e:
            if e.error_type != "ResourceConflictException":
                raise
        return {"guardduty_detector_id": det, "security_hub": "enabled"}

    async def _do_deploy_conformance_pack(self, ctx: StepContext) -> Dict[str, Any]:
        name = self.config.get("conformance_pack_name", "Operational-Best-Practices-for-FFIEC")
        uri = self.config.get("conformance_pack_template_s3")
        if not uri:
            ctx.progress("conformance_pack_template_s3 not set; skipping deployment")
            return {"conformance_pack": None}
        sess = await self._member_session(self._acct(ctx))
        await self._aws(self._client("config", sess).put_conformance_pack, ConformancePackName=name, TemplateS3Uri=uri)
        return {"conformance_pack": name}

    async def _do_configure_log_delivery(self, ctx: StepContext) -> Dict[str, Any]:
        trails = (await self._aws(self._client("cloudtrail").describe_trails))["trailList"]
        org = next((t for t in trails if t.get("IsOrganizationTrail")), None)
        if not org:
            raise StepFailed("No organization CloudTrail found in the management account", "NoOrganizationTrail", retryable=False)
        return {"cloudtrail": org["Name"], "log_archive_bucket": org.get("S3BucketName")}

    # --- billing -----------------------------------------------------------
    async def _do_create_budget(self, ctx: StepContext) -> Dict[str, Any]:
        acct = self._acct(ctx)
        sess = await self._member_session(acct)
        name = f"{ctx.spec['account_name']}-monthly"
        limit = int(ctx.spec.get("monthly_budget_usd", 1500))
        emails = [o for o in ctx.spec.get("owners", []) if "@" in o] or [self.config.get("budget_alert_email", "cloud-finops@example.com")]
        try:
            await self._aws(self._client("budgets", sess).create_budget, AccountId=acct,
                            Budget={"BudgetName": name, "BudgetLimit": {"Amount": str(limit), "Unit": "USD"}, "TimeUnit": "MONTHLY", "BudgetType": "COST"},
                            NotificationsWithSubscribers=[
                                {"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": t, "ThresholdType": "PERCENTAGE"},
                                 "Subscribers": [{"SubscriptionType": "EMAIL", "Address": e} for e in emails[:10]]}
                                for t in (80.0, 100.0)
                            ])
        except StepFailed as e:
            if e.error_type != "DuplicateRecordException":
                raise
        return {"budget_name": name, "budget_limit_usd": limit}

    async def _undo_delete_budget(self, ctx: StepContext) -> Dict[str, Any]:
        acct = self._acct(ctx)
        sess = await self._member_session(acct)
        try:
            await self._aws(self._client("budgets", sess).delete_budget, AccountId=acct, BudgetName=(ctx.original_output or {}).get("budget_name", ""))
        except StepFailed as e:
            if e.error_type != "NotFoundException":
                raise
        return {"undone": True}

    async def verify(self, spec: Dict[str, Any], outputs: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.dry_run or not outputs.get("account_id"):
            return []
        acct = outputs["account_id"]
        d = (await self._aws(self._client("organizations").describe_account, AccountId=acct))["Account"]
        return [{"name": "Account is ACTIVE in AWS Organizations", "ok": d.get("Status") == "ACTIVE", "detail": d.get("Status")}]
