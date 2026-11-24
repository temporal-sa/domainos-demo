"""Real connectors for ServiceNow, Okta and Splunk (REST via httpx).

Credentials come from connectors.yaml, where values like `env:SN_PASSWORD`
are read from the environment. Each call maps HTTP errors onto retryable
(429, 5xx, network) or non-retryable (4xx) StepFailed errors, so Temporal's
retry policy does the right thing.
"""

import os
from typing import Any, Dict, Optional

from .base import Connector, StepContext, StepFailed


def resolve(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        return os.getenv(value[4:], "")
    return value


class _Http(Connector):
    mode = "real"

    async def _request(self, method: str, url: str, *, auth=None, headers: Optional[Dict[str, str]] = None, json: Any = None, data: Any = None, verify: Any = True) -> Any:
        try:
            import httpx
        except ImportError as e:  # pragma: no cover
            raise StepFailed("httpx is not installed (pip install httpx)", "WorkerMisconfigured", retryable=False) from e
        try:
            async with httpx.AsyncClient(timeout=30, verify=verify) as client:
                r = await client.request(method, url, auth=auth, headers=headers, json=json, data=data)
        except httpx.TransportError as e:
            raise StepFailed(f"Could not reach {url}: {e}", "ConnectionError", retryable=True) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise StepFailed(f"HTTP {r.status_code} from {url}", "RateLimited" if r.status_code == 429 else "ServiceUnavailable", retryable=True)
        if r.status_code >= 400:
            raise StepFailed(f"HTTP {r.status_code} from {url}: {r.text[:300]}", f"Http{r.status_code}", retryable=False)
        return r.json() if r.content else {}


class ServiceNowConnector(_Http):
    def _base(self) -> str:
        return resolve(self.config.get("instance_url", "")).rstrip("/")

    def _auth(self):
        return (resolve(self.config.get("user", "")), resolve(self.config.get("password", "")))

    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        base = self._base()
        if ctx.action == "create_change":
            body = {
                "short_description": f"AWS account vending: {ctx.spec.get('title')}",
                "description": ctx.spec.get("justification", ""),
                "type": "standard",
                "correlation_id": ctx.request_id,  # makes retries findable
                "std_change_producer_version": self.config.get("standard_change_template", ""),
            }
            found = await self._request("GET", f"{base}/api/now/table/change_request?sysparm_query=correlation_id={ctx.request_id}&sysparm_limit=1", auth=self._auth())
            rec = (found.get("result") or [None])[0] or (await self._request("POST", f"{base}/api/now/table/change_request", auth=self._auth(), json=body))["result"]
            ctx.progress(f"Change {rec['number']}")
            return {"change_number": rec["number"], "change_sys_id": rec["sys_id"], "change_url": f"{base}/change_request.do?sys_id={rec['sys_id']}", "change_state": rec.get("state")}
        if ctx.action == "update_change":
            sys_id = ctx.context.get("change_sys_id")
            body = {"work_notes": ctx.params.get("note", "")}
            if ctx.params.get("state_value"):
                body["state"] = ctx.params["state_value"]
            if sys_id:
                await self._request("PATCH", f"{base}/api/now/table/change_request/{sys_id}", auth=self._auth(), json=body)
            return {"change_state": ctx.params.get("state", "")}
        if ctx.action == "create_ci":
            table = self.config.get("ci_table", "cmdb_ci_cloud_service_account")
            found = await self._request("GET", f"{base}/api/now/table/{table}?sysparm_query=account_id={ctx.context.get('account_id')}&sysparm_limit=1", auth=self._auth())
            rec = (found.get("result") or [None])[0] or (await self._request("POST", f"{base}/api/now/table/{table}", auth=self._auth(), json={
                "name": ctx.spec.get("account_name"), "account_id": ctx.context.get("account_id"), "object_id": ctx.context.get("account_id"),
            }))["result"]
            return {"ci_sys_id": rec["sys_id"], "ci_number": rec.get("number") or rec["sys_id"][:10]}
        if ctx.action == "link_ci":
            await self._request("POST", f"{base}/api/now/table/task_ci", auth=self._auth(), json={"task": ctx.context.get("change_sys_id"), "ci_item": ctx.context.get("ci_sys_id")})
            return {"ci_linked": True}
        raise StepFailed(f"ServiceNow connector has no action '{ctx.action}'", "UnknownAction", retryable=False)

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        if ctx.action == "retire_ci" and (ctx.original_output or {}).get("ci_sys_id"):
            table = self.config.get("ci_table", "cmdb_ci_cloud_service_account")
            await self._request("PATCH", f"{self._base()}/api/now/table/{table}/{ctx.original_output['ci_sys_id']}", auth=self._auth(), json={"install_status": "7"})
            return {"undone": True}
        return {"undone": False}


class OktaConnector(_Http):
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"SSWS {resolve(self.config.get('api_token', ''))}", "Accept": "application/json", "Content-Type": "application/json"}

    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        org = resolve(self.config.get("org_url", "")).rstrip("/")
        if ctx.action != "create_group":
            raise StepFailed(f"Okta connector has no action '{ctx.action}'", "UnknownAction", retryable=False)
        name = f"aws-{ctx.spec['account_name']}-users"
        found = await self._request("GET", f"{org}/api/v1/groups?q={name}&limit=1", headers=self._headers())
        group = next((g for g in found if g["profile"]["name"] == name), None) if isinstance(found, list) else None
        if not group:
            group = await self._request("POST", f"{org}/api/v1/groups", headers=self._headers(),
                                        json={"profile": {"name": name, "description": f"AWS {ctx.spec.get('title')} ({ctx.request_id})"}})
            ctx.progress(f"Created Okta group {name}")
        app = resolve(self.config.get("aws_app_id"))
        if app:
            await self._request("PUT", f"{org}/api/v1/apps/{app}/groups/{group['id']}", headers=self._headers(), json={})
            ctx.progress("Assigned group to the AWS IAM Identity Center app")
        return {"okta_group_id": group["id"], "okta_group": name}

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        gid = (ctx.original_output or {}).get("okta_group_id")
        if gid:
            org = resolve(self.config.get("org_url", "")).rstrip("/")
            await self._request("DELETE", f"{org}/api/v1/groups/{gid}", headers=self._headers())
        return {"undone": bool(gid)}


class SplunkConnector(_Http):
    async def execute(self, ctx: StepContext) -> Dict[str, Any]:
        base = resolve(self.config.get("management_url", "")).rstrip("/")
        auth = (resolve(self.config.get("user", "")), resolve(self.config.get("password", "")))
        index = f"aws_{ctx.spec['account_name'].replace('-', '_')}"
        name = f"aws-{ctx.spec['account_name']}"
        verify = self.config.get("verify_tls", True)
        try:
            res = await self._request("POST", f"{base}/services/data/inputs/http?output_mode=json", auth=auth, data={"name": name, "index": index}, verify=verify)
        except StepFailed as e:
            if e.error_type != "Http409":  # already exists
                raise
            res = await self._request("GET", f"{base}/services/data/inputs/http/{name}?output_mode=json", auth=auth, verify=verify)
        token = (res.get("entry") or [{}])[0].get("content", {}).get("token", "")
        return {"splunk_index": index, "hec_token": f"••••{token[-4:]}" if token else "created"}

    async def compensate(self, ctx: StepContext) -> Dict[str, Any]:
        base = resolve(self.config.get("management_url", "")).rstrip("/")
        auth = (resolve(self.config.get("user", "")), resolve(self.config.get("password", "")))
        await self._request("DELETE", f"{base}/services/data/inputs/http/aws-{ctx.spec['account_name']}?output_mode=json", auth=auth, verify=self.config.get("verify_tls", True))
        return {"undone": True}
