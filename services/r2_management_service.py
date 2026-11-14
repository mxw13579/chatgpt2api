"""R2 administration. No scheduler or cloud requests are started at import time."""
from __future__ import annotations

import copy
import ipaddress
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from urllib.parse import quote

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from services.cloudflare_api import CloudflareAPI, CloudflareAPIError
from services.config import IMAGE_STORAGE_LOCK, config
from services.image_storage_service import image_storage_service
from services.log_service import log_service
from services.r2_client import CloudflareR2Client, R2Error


class R2ManagementError(RuntimeError):
    """An administrator-facing error containing no transport payload or credentials."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def billing_period_start(now: datetime, day: int) -> datetime:
    start = now.astimezone(UTC).replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if now < start:
        start = start.replace(year=start.year - 1, month=12) if start.month == 1 else start.replace(month=start.month - 1)
    return start


def cloud_error(exc: CloudflareAPIError | R2Error) -> str:
    # Never forward response bodies, URLs, request headers or exception messages.
    status = getattr(exc, "status_code", None)
    codes = [str(code) for code in getattr(exc, "codes", ()) if type(code) is int]
    detail = f" HTTP {status}" if type(status) is int else ""
    if codes:
        detail += " codes=" + ",".join(codes)
    return "Cloudflare 请求失败" + detail


def _hostname(value: str) -> str:
    host = value.strip().lower()
    if len(host) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", host):
        raise R2ManagementError("域名必须是 HTTPS 主机名，不能包含协议、端口或路径")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise R2ManagementError("不能使用 IP 地址作为图片域名")


def _active(item: dict) -> bool:
    status = item.get("status") or {}
    return status.get("ownership") == "active" and status.get("ssl") == "active"


class R2ManagementService:
    def __init__(self) -> None:
        # Serialize admin changes with maintenance, never hold the storage/index lock over I/O.
        self._lock = RLock()

    @contextmanager
    def _api(self, account: dict):
        if not all(account.get(key) for key in ("account_id", "bucket", "api_token")):
            raise R2ManagementError("请先配置 CF Account ID、桶和管理 API Token")
        client = CloudflareAPI(account["account_id"], account["api_token"])
        try:
            yield client
        finally:
            client.close()

    def overview(self) -> dict:
        settings = config.get_public_image_storage_settings()
        from services.image_service import legacy_storage_stats
        return {"settings": settings, "accounts": image_storage_service.account_summaries(),
                "legacy": legacy_storage_stats()}

    def save_settings(self, settings: dict) -> dict:
        with self._lock:
            with IMAGE_STORAGE_LOCK:
                old = config.get_image_storage_settings()
                old_accounts = {item["id"]: item for item in old["r2_accounts"]}
                incoming_accounts = settings.get("r2_accounts", old["r2_accounts"])
                if not isinstance(incoming_accounts, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("id", ""), str) for item in incoming_accounts
                ):
                    raise R2ManagementError("R2 账号配置必须是包含字符串 ID 的对象列表")
                for item in incoming_accounts:
                    previous = old_accounts.get(item.get("id", "").strip().lower())
                    if previous and item.get("active_domain_id", previous["active_domain_id"]) != previous["active_domain_id"]:
                        raise R2ManagementError("当前域名必须通过域名管理手动切换")
                # Validate/commit first; keep the reservation lock until invalidation
                # finishes so uploads cannot observe unchecked settings or a rejected edit.
                config.update({"image_storage": settings})
                for item in incoming_accounts:
                    account_id = item.get("id", "").strip().lower()
                    previous = old_accounts.get(account_id)
                    if previous and any(item.get(key, previous[key]) != previous[key] for key in ("account_id", "bucket")):
                        image_storage_service.update_r2_usage(account_id, {"complete": False, "error": "存储位置已变更，请重新刷新用量"})
                    if previous and (any(item.get(key, previous.get(key)) != previous.get(key) for key in
                                         ("account_id", "bucket", "domains", "rate_limit_requests", "restrict_methods"))
                                     or settings.get("cors_origins", old["cors_origins"]) != old["cors_origins"]):
                        self._invalidate(account_id)
            return self.overview()

    def _save_account(self, account: dict) -> None:
        settings = copy.deepcopy(config.get_image_storage_settings())
        settings["r2_accounts"] = [account if item["id"] == account["id"] else item for item in settings["r2_accounts"]]
        config.update({"image_storage": settings})

    def _invalidate(self, account_id: str) -> None:
        image_storage_service.update_r2_usage(account_id, {
            "verified_domains": [], "domains_safe": False, "domains_checked": False,
        })

    def _record_domains(self, account: dict, domains: list[dict], managed: dict) -> dict:
        state = image_storage_service.get_r2_usage(account["id"])
        configured = {item["domain"]: item for item in account["domains"]}
        verified = set(state.get("verified_domains", []))
        checks = state.get("domain_checks", {})
        items = []
        for raw in domains:
            host = raw["domain"]
            local = configured.get(host)
            protected = bool(local and local["zone_id"] == raw.get("zoneId") and host in verified)
            items.append({"id": host, "domain": host, "zone_id": raw.get("zoneId", ""),
                          "enabled": bool(raw.get("enabled")), "status": raw.get("status", {}),
                          "protected": protected, "check": checks.get(host), "configured": local is not None,
                          "sample_url": checks.get(host, {}).get("sample_url", "")})
        actual = {item["domain"]: item for item in items}
        for host, local in configured.items():
            if host not in actual:
                items.append({"id": host, "domain": host, "zone_id": local["zone_id"], "enabled": False,
                              "status": {"ownership": "missing", "ssl": "missing"}, "protected": False,
                              "configured": True, "check": checks.get(host)})
        safe = (managed.get("enabled") is False
                and all(item["protected"] for item in items if item["enabled"])
                and all(host in actual and actual[host]["enabled"] and actual[host]["protected"] and _active(actual[host])
                        for host, local in configured.items() if local["enabled"]))
        image_storage_service.update_r2_usage(account["id"], {
            "domains": items, "managed_disabled": managed.get("enabled") is False,
            "domains_checked": True, "domains_safe": safe, "domains_checked_at": _iso(_now()),
        })
        return {"items": items, "managed_enabled": managed.get("enabled") is not False}

    def list_domains(self, account_id: str) -> dict:
        with self._lock:
            account = image_storage_service.account(account_id)
            try:
                with self._api(account) as api:
                    return self._record_domains(account, api.list_domains(account["bucket"]), api.get_managed_domain(account["bucket"]))
            except (CloudflareAPIError, R2ManagementError):
                image_storage_service.update_r2_usage(account_id, {"domains_checked": False, "domains_safe": False})
                raise

    def list_zones(self, account_id: str) -> list[dict]:
        with self._api(image_storage_service.account(account_id)) as api:
            return [{"id": item["id"], "name": item["name"]} for item in api.list_zones()]

    def _check_zone(self, zones: list[dict], domain: str, zone_id: str) -> None:
        if not any(item["id"] == zone_id and (domain == item["name"] or domain.endswith("." + item["name"])) for item in zones):
            raise R2ManagementError("域名必须属于当前 CF 账号中的所选 Zone")

    def add_domain(self, account_id: str, domain: str, zone_id: str) -> dict:
        domain = _hostname(domain)
        zone_id = zone_id.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", zone_id):
            raise R2ManagementError("Zone ID 无效")
        with self._lock:
            account = copy.deepcopy(image_storage_service.account(account_id))
            if any(item["domain"] == domain for item in account["domains"]):
                raise R2ManagementError("域名已登记")
            with self._api(account) as api:
                self._check_zone(api.list_zones(), domain, zone_id)
                api.add_domain(account["bucket"], domain, zone_id, enabled=False)
            account["domains"].append({"id": domain, "domain": domain, "zone_id": zone_id, "enabled": False})
            self._invalidate(account_id)
            self._save_account(account)
            return next(item for item in self.list_domains(account_id)["items"] if item["id"] == domain)

    def update_domain(self, account_id: str, domain_id: str, changes: dict, actor: str = "") -> dict:
        with self._lock:
            account = copy.deepcopy(image_storage_service.account(account_id))
            local = next((item for item in account["domains"] if item["id"] == domain_id), None)
            if local is None:
                raise R2ManagementError("请先登记此域名")
            if changes.get("active") is False:
                raise R2ManagementError("请通过选择另一个当前域名进行切换")
            if changes.get("active") and changes.get("enabled") is False:
                raise R2ManagementError("不能同时停用域名并设为当前域名")
            paused = image_storage_service.get_r2_usage(account_id).get("access_paused")
            if paused and (changes.get("enabled") or changes.get("active")):
                raise R2ManagementError("额度停读中，请使用恢复公开访问操作")
            listing = self.list_domains(account_id)
            item = next(item for item in listing["items"] if item["id"] == domain_id)
            if changes.get("enabled") or changes.get("active"):
                if not item["protected"] or not _active(item) or listing["managed_enabled"]:
                    raise R2ManagementError("域名必须已激活、防护已应用且 r2.dev 已关闭")
                if any(other["enabled"] and not other["protected"] for other in listing["items"]):
                    raise R2ManagementError("请先保护或关闭所有公开域名")
            if "enabled" in changes:
                image_storage_service.update_r2_usage(account_id, {"domains_safe": False, "domains_checked": False})
                with self._api(account) as api:
                    api.update_domain(account["bucket"], local["domain"], changes["enabled"])
                    actual = next((domain for domain in api.list_domains(account["bucket"]) if domain["domain"] == local["domain"]), None)
                    if actual is None or actual.get("enabled") is not changes["enabled"]:
                        raise R2ManagementError("CF 未确认域名状态变更")
                local["enabled"] = changes["enabled"]
            old_domain = account["active_domain_id"]
            if changes.get("active"):
                if not local["enabled"] or ("enabled" not in changes and not item["enabled"]):
                    raise R2ManagementError("当前域名必须已启用")
                account["active_domain_id"] = domain_id
            self._save_account(account)
            result = item
            if "enabled" in changes:
                result = next(item for item in self.list_domains(account_id)["items"] if item["id"] == domain_id)
            if old_domain != account["active_domain_id"]:
                log_service.add("image-storage", "手动切换 R2 图片域名", detail={
                    "account_id": account_id, "old_domain": old_domain, "new_domain": domain_id,
                    "actor": actor, "changed_at": _iso(_now()),
                })
            return result

    def delete_domain(self, account_id: str, domain_id: str) -> None:
        with self._lock:
            account = copy.deepcopy(image_storage_service.account(account_id))
            local = next((item for item in account["domains"] if item["id"] == domain_id), None)
            if local is None:
                raise R2ManagementError("域名未登记")
            if account["enabled"] and account["active_domain_id"] == domain_id:
                raise R2ManagementError("请先切换当前域名或停用账号")
            with self._api(account) as api:
                api.delete_domain(account["bucket"], local["domain"])
            account["domains"] = [item for item in account["domains"] if item["id"] != domain_id]
            if account["active_domain_id"] == domain_id:
                account["active_domain_id"] = ""
            self._invalidate(account_id)
            self._save_account(account)
            self.list_domains(account_id)

    def _upsert_rule(self, api: CloudflareAPI, zone_id: str, phase: str, rule: dict) -> dict:
        base = f"/zones/{zone_id}/rulesets"
        rulesets = api.request("GET", base)
        entry = next((item for item in rulesets if item.get("phase") == phase and item.get("kind") == "zone"), None)
        if entry is None:
            if not rule["enabled"]:
                return {}
            return api.request("POST", base, json={"name": "chatgpt2api R2 images", "kind": "zone", "phase": phase, "rules": [rule]})
        ruleset = api.request("GET", f"{base}/{entry['id']}")
        owned = [item for item in ruleset.get("rules", []) if item.get("ref") == rule["ref"]]
        if len(owned) > 1:
            raise R2ManagementError("本项目规则引用重复，请在 Cloudflare 中检查")
        if owned:
            return api.request("PATCH", f"{base}/{entry['id']}/rules/{owned[0]['id']}", json=rule)
        if not rule["enabled"]:
            return {}
        return api.request("POST", f"{base}/{entry['id']}/rules", json=rule)

    def _zone_templates(self, api: CloudflareAPI, zone_id: str, hosts: list[str], account: dict) -> dict:
        path = 'starts_with(http.request.uri.path, "/images/")'
        host_set = " ".join('"' + _hostname(host) + '"' for host in sorted(hosts))
        expression = f"(http.host in {{{host_set}}} and {path})"
        templates = [
            ("http_ratelimit", {"ref": "chatgpt2api-r2-images-rate", "description": "R2 images: Free IP rate limit",
                                "expression": path, "action": "block", "enabled": True,
                                "ratelimit": {"characteristics": ["cf.colo.id", "ip.src"], "period": 10,
                                              "requests_per_period": account["rate_limit_requests"], "mitigation_timeout": 10}}),
            ("http_request_firewall_custom", {"ref": "chatgpt2api-r2-images-methods", "description": "R2 images: read only",
                                              "expression": f'({expression} and not http.request.method in {{"GET" "HEAD" "OPTIONS"}})',
                                              "action": "block", "enabled": account["restrict_methods"]}),
            ("http_request_cache_settings", {"ref": "chatgpt2api-r2-images-cache", "description": "R2 images: respect origin TTL",
                                              "expression": expression, "action": "set_cache_settings", "enabled": True,
                                              "action_parameters": {"cache": True, "edge_ttl": {"mode": "respect_origin"},
                                                                    "browser_ttl": {"mode": "respect_origin"},
                                                                    "serve_stale": {"disable_stale_while_updating": True}}}),
        ]
        result = {}
        for phase, rule in templates:
            try:
                applied = self._upsert_rule(api, zone_id, phase, rule)
            except CloudflareAPIError as exc:
                raise R2ManagementError("免费防护规则应用失败；请检查 Token 权限及免费规则名额，不会删除现有规则或启用付费。" + cloud_error(exc)) from None
            if applied:
                owned = next((item for item in applied.get("rules", []) if item.get("ref") == rule["ref"]), None)
                if not owned or not owned.get("id"):
                    raise R2ManagementError("Cloudflare 未返回本项目规则 ID，不能确认防护成功")
                result[phase] = {"ruleset_id": applied["id"], "rule_id": owned["id"], "ref": rule["ref"]}
        return result

    def apply_protection(self, account_id: str, rate_limit_requests: int, restrict_methods: bool, cors_origins: list[str]) -> dict:
        with self._lock:
            settings = copy.deepcopy(config.get_image_storage_settings())
            account = next((item for item in settings["r2_accounts"] if item["id"] == account_id), None)
            if account is None:
                raise R2ManagementError("账号不存在")
            account.update(rate_limit_requests=rate_limit_requests, restrict_methods=restrict_methods)
            settings["cors_origins"] = cors_origins
            self.save_settings(settings)
            account = image_storage_service.account(account_id)
            cors_origins = config.get_image_storage_settings()["cors_origins"]
            self._invalidate(account_id)
            with self._api(account) as api:
                try:
                    api.set_managed_domain(account["bucket"], False)
                    domains = api.list_domains(account["bucket"])
                    configured = {item["domain"]: item for item in account["domains"]}
                    # Unknown public bindings cannot bypass the template. Keep local intent intact.
                    for item in domains:
                        if item["enabled"] and item["domain"] not in configured:
                            api.update_domain(account["bucket"], item["domain"], False)
                    zones = api.list_zones()
                    by_zone: dict[str, list[str]] = {}
                    actual = {item["domain"]: item for item in domains}
                    for host, local in configured.items():
                        self._check_zone(zones, host, local["zone_id"])
                        if host not in actual or actual[host].get("zoneId") != local["zone_id"]:
                            raise R2ManagementError("请先完成所有配置域名的桶绑定")
                        by_zone.setdefault(local["zone_id"], []).append(host)
                    rules = {zone: self._zone_templates(api, zone, hosts, account) for zone, hosts in by_zone.items()}
                    # Treat bucket CORS writes conservatively as Class A, including
                    # uncertain outcomes, just like the object connection test.
                    image_storage_service.update_r2_usage(account_id, {"a_local_increment": 1})
                    api.set_cors(account["bucket"], cors_origins)
                    image_storage_service.update_r2_usage(account_id, {
                        "verified_domains": list(configured), "protection_rules": rules, "protection_error": "",
                    })
                    result = self._record_domains(account, api.list_domains(account["bucket"]), api.get_managed_domain(account["bucket"]))
                    if result["managed_enabled"] or any(item["enabled"] and not item["protected"] for item in result["items"]):
                        raise R2ManagementError("仍有未保护的公开入口")
                    return {"ok": True, "domains": result["items"], "rules": rules}
                except (CloudflareAPIError, R2ManagementError) as exc:
                    self._invalidate(account_id)
                    message = cloud_error(exc) if isinstance(exc, CloudflareAPIError) else str(exc)
                    image_storage_service.update_r2_usage(account_id, {"protection_error": message})
                    self._close_public(account, api)
                    raise R2ManagementError(message) from None

    def _close_public(self, account: dict, api: CloudflareAPI) -> dict:
        """One shutdown attempt plus verification; failures remain visible for the next tick."""
        errors = []
        domain_errors = {}
        bucket = account["bucket"]
        try:
            api.set_managed_domain(bucket, False)
        except CloudflareAPIError as exc:
            errors.append(cloud_error(exc))
        try:
            domains = api.list_domains(bucket)
        except CloudflareAPIError as exc:
            domains = []
            errors.append(cloud_error(exc))
        for item in domains:
            if item["enabled"]:
                try:
                    api.update_domain(bucket, item["domain"], False)
                except CloudflareAPIError as exc:
                    domain_errors[item["domain"]] = cloud_error(exc)
                    errors.append(cloud_error(exc))
        try:
            actual = api.list_domains(bucket)
            managed = api.get_managed_domain(bucket)
            self._record_domains(account, actual, managed)
            closed = managed.get("enabled") is False and all(item.get("enabled") is False for item in actual)
        except CloudflareAPIError as exc:
            errors.append(cloud_error(exc))
            closed = False
            image_storage_service.update_r2_usage(account["id"], {"domains_checked": False, "managed_disabled": False})
        if not closed and not errors:
            errors.append("仍有公开入口未关闭")
        state = image_storage_service.update_r2_usage(account["id"], {
            "shutdown_ok": closed, "shutdown_error": "; ".join(errors), "domain_shutdown_errors": domain_errors,
            "shutdown_checked_at": _iso(_now()), "domains_safe": False,
        })
        return state

    def _mark_budget_paused(self, account: dict, observed_b: int = 0) -> bool:
        """Persist the pause gate and report whether public closure is still needed."""
        state = image_storage_service.get_r2_usage(account["id"])
        paused = bool(observed_b >= account["b_budget"] or int(state.get("cloud_b", 0)) >= account["b_budget"] or state.get("access_paused"))
        if paused:
            # Persist the application GET/PUT gate even if client creation/credentials fail.
            updates = {"access_paused": True, "domains_safe": False}
            if not state.get("access_paused"):
                updates["shutdown_ok"] = False
            state = image_storage_service.update_r2_usage(account["id"], updates)
        return paused and state.get("shutdown_ok") is not True

    def _enforce_budget(self, account: dict, api: CloudflareAPI, observed_b: int = 0) -> None:
        if self._mark_budget_paused(account, observed_b):
            self._close_public(account, api)

    def refresh_usage(self, account_id: str) -> dict:
        with self._lock:
            account = image_storage_service.account(account_id)
            now = _now()
            image_storage_service.update_r2_usage(account_id, {"last_usage_attempt_at": _iso(now)})
            self._mark_budget_paused(account)
            try:
                with self._api(account) as api:
                    observed_b = 0
                    try:
                        if not account["billing_cycle_confirmed"]:
                            raise R2ManagementError("请先确认账号的 UTC 计费周期")
                        # Preserve local operations issued while this query runs. The
                        # marker precedes its cutoff, so ambiguous overlap is conservative.
                        marker = image_storage_service.get_r2_usage(account_id)
                        query_end = _now()
                        start = _iso(billing_period_start(query_end, account["billing_cycle_day"]))
                        result = api.query_usage(start, _iso(query_end), account["bucket"])
                        observed_b = result["b_operations"]
                        if result.get("complete") is not True:
                            raise R2ManagementError("用量统计不完整，保留上次计数；不能据此分配新上传")
                        snapshot = image_storage_service.record_usage_snapshot(account_id, {
                            **result, "fetched_at": _iso(query_end),
                            "a_query_cycle": marker.get("a_observed_cycle_start"),
                            "a_query_count": marker.get("a_observed_cycle_operations", 0),
                        })
                        if snapshot.get("complete") is not True:
                            raise R2ManagementError("用量快照已过期或计费周期不完整")
                        image_storage_service.update_r2_usage(account_id, {
                            "error": "", "b_warning": observed_b >= account["b_warn"],
                        })
                    finally:
                        # Only first/unfinished closure needs an attempt, even on query failure.
                        self._enforce_budget(account, api, observed_b)
            except (CloudflareAPIError, R2ManagementError) as exc:
                message = cloud_error(exc) if isinstance(exc, CloudflareAPIError) else str(exc)
                image_storage_service.update_r2_usage(account_id, {"error": message, "complete": False})
                raise R2ManagementError(message) from None
            return image_storage_service.account_usage(account_id)

    def restore_access(self, account_id: str) -> dict:
        with self._lock:
            self.refresh_usage(account_id)
            account = image_storage_service.account(account_id)
            state = image_storage_service.get_r2_usage(account_id)
            if not state.get("complete") or int(state.get("cloud_b", 0)) >= account["b_budget"]:
                raise R2ManagementError("B 类用量仍超出预算，不能恢复公开访问")
            intended = [item for item in account["domains"] if item["enabled"]]
            if not intended or account["active_domain_id"] not in {item["id"] for item in intended}:
                raise R2ManagementError("请先配置计划恢复的域名和当前域名")
            image_storage_service.update_r2_usage(account_id, {"access_paused": True})
            if not state.get("access_paused"):
                with self._api(account) as api:
                    state = self._close_public(account, api)
            if state.get("shutdown_ok") is not True:
                raise R2ManagementError("公开入口尚未确认全部关闭，请处理停读错误后再恢复")
            settings = config.get_image_storage_settings()
            self.apply_protection(account_id, account["rate_limit_requests"], account["restrict_methods"], settings["cors_origins"])
            with self._api(account) as api:
                try:
                    for item in intended:
                        api.update_domain(account["bucket"], item["domain"], True)
                    listing = self._record_domains(account, api.list_domains(account["bucket"]), api.get_managed_domain(account["bucket"]))
                    if not image_storage_service.get_r2_usage(account_id)["domains_safe"]:
                        raise R2ManagementError("域名未激活或公开入口防护检查失败")
                except (CloudflareAPIError, R2ManagementError) as exc:
                    self._close_public(account, api)
                    message = cloud_error(exc) if isinstance(exc, CloudflareAPIError) else str(exc)
                    image_storage_service.update_r2_usage(account_id, {"restore_error": message})
                    raise R2ManagementError(message) from None
            image_storage_service.update_r2_usage(account_id, {
                "access_paused": False, "restore_error": "", "shutdown_ok": False, "shutdown_error": "",
            })
            return {"ok": True, "domains": listing["items"]}

    def test_connection(self, account_id: str) -> dict:
        with self._lock:
            account = image_storage_service.account(account_id)
            if not all(account.get(key) for key in ("account_id", "bucket", "access_key_id", "secret_access_key", "api_token")):
                raise R2ManagementError("请先填写完整的 R2 凭据和 CF Token")
            state = image_storage_service.get_r2_usage(account_id)
            if max(int(state.get("a_local", 0)), int(state.get("cloud_a", 0))) >= account["a_budget"]:
                raise R2ManagementError("A 类预算已用尽，不能发送连接测试 LIST")
            client = CloudflareR2Client({**account, "prefix": "images"})
            try:
                # B must consume this increment atomically, not persist it as an ordinary field.
                image_storage_service.update_r2_usage(account_id, {"a_local_increment": 1})
                result = client.test_connection()
            finally:
                client.close()
            zones = self.list_zones(account_id)
            domains = self.list_domains(account_id)
            return {"ok": bool(result["ok"]), "object_test": {"ok": bool(result["ok"]), "status": result.get("status")},
                    "zones": len(zones), "domains": domains["items"], "managed_enabled": domains["managed_enabled"],
                    "note": "对象 LIST 与 Zone/桶绑定读取成功不代表防护写入或 Analytics 权限已验证"}

    def check_domains(self, account_id: str, domain_id: str | None = None) -> list[dict]:
        with self._lock:
            account = image_storage_service.account(account_id)
            selected = [item for item in account["domains"] if domain_id is None or item["id"] == domain_id]
            if domain_id is not None and not selected:
                raise R2ManagementError("域名未登记")
            state = image_storage_service.get_r2_usage(account_id)
            listing = {item["id"]: item for item in self.list_domains(account_id)["items"]}
            now = _now()
            sample = next((item for item in image_storage_service.list_items("")
                           if item.get("storage") == "r2" and item.get("account_id") == account_id
                           and not item.get("pending") and item.get("expires_at") and _date(item["expires_at"]) > now), None)
            checks = []
            for domain in selected:
                check = {"domain_id": domain["id"], "ok": False, "checked_at": _iso(now), "source": "server"}
                actual = listing[domain["id"]]
                if state.get("access_paused"):
                    check["error"] = "额度停读中，不发送图片 HEAD"
                elif not actual["enabled"] or not _active(actual):
                    check["error"] = "CF 绑定未启用或证书/所有权未激活"
                elif sample is None:
                    check["error"] = "无未过期图片探测样本"
                else:
                    host = _hostname(domain["domain"])
                    key = sample["object_key"]
                    if not key.startswith("images/") or any(part in {"", ".", ".."} for part in key.split("/")):
                        raise R2ManagementError("图片样本对象路径无效")
                    url = f"https://{host}/{quote(key, safe='/')}"
                    check["sample_url"] = url
                    check.update(self._head(url))
                checks.append(check)
            combined = {**state.get("domain_checks", {}), **{item["domain_id"]: item for item in checks}}
            runtime_domains = image_storage_service.get_r2_usage(account_id).get("domains", [])
            for item in runtime_domains:
                item["check"] = combined.get(item["id"])
                item["sample_url"] = combined.get(item["id"], {}).get("sample_url", "")
            image_storage_service.update_r2_usage(account_id, {"domain_checks": combined, "domains": runtime_domains})
            return checks

    def _head(self, url: str) -> dict:
        # The check describes this server's network, like the R2/API clients.
        session = requests.Session(verify=True, trust_env=False)
        try:
            response = session.request("HEAD", url, timeout=15, allow_redirects=False)
            if 200 <= response.status_code < 300:
                return {"ok": True, "http_status": response.status_code}
            return {"ok": False, "http_status": response.status_code,
                    "error": "HTTP 重定向未跟随" if 300 <= response.status_code < 400 else f"HTTP {response.status_code}"}
        except RequestException as exc:
            stage = {5: "代理 DNS", 6: "DNS", 7: "连接", 28: "超时", 35: "TLS", 51: "TLS 证书", 60: "TLS 证书"}.get(exc.code, "网络")
            return {"ok": False, "error": stage + "检查失败"}
        finally:
            session.close()

    def resolve_images(self, images: list[dict]) -> list[dict]:
        fields = {"image_id", "account_id", "expires_at", "url", "expired", "unavailable"}
        return [{key: value for key, value in image_storage_service.resolve_reference(ref).items() if key in fields} for ref in images]

    def cleanup(self) -> dict:
        return image_storage_service.cleanup_expired()

    def cleanup_legacy(self) -> dict:
        # E owns implementation; never route this to R2 deletion or WebDAV remote deletion.
        from services.image_service import cleanup_legacy_images
        return cleanup_legacy_images()

    def maintenance_tick(self) -> dict:
        """Called only by E's existing maintenance worker. No health checks or failover."""
        with self._lock:
            settings = config.get_image_storage_settings()
            result: dict = {"accounts": [], "cleanup": None}
            for account in settings["r2_accounts"]:
                state = image_storage_service.get_r2_usage(account["id"])
                if (not account["enabled"] and not state.get("access_paused")
                        and not any(domain["enabled"] for domain in account["domains"])
                        and not image_storage_service.account_usage(account["id"])["image_count"]):
                    continue
                try:
                    last = state.get("last_usage_attempt_at") or state.get("fetched_at")
                    due = not last or (_now() - _date(last)).total_seconds() >= settings["usage_refresh_seconds"]
                    if due:
                        self.refresh_usage(account["id"])
                    elif self._mark_budget_paused(account):
                        with self._api(account) as api:
                            self._close_public(account, api)
                    image_storage_service.update_r2_usage(account["id"], {"maintenance_error": ""})
                    result["accounts"].append({"id": account["id"], "ok": True})
                except Exception as exc:
                    # Per-account isolation is deliberate: one failure must not starve another
                    # account's shutdown/refresh or expiry deletion. Never log exception text.
                    error = str(exc) if isinstance(exc, R2ManagementError) else cloud_error(exc) if isinstance(exc, (CloudflareAPIError, R2Error)) else "R2 维护失败"
                    image_storage_service.update_r2_usage(account["id"], {"maintenance_error": error})
                    result["accounts"].append({"id": account["id"], "ok": False, "error": error})
        result["cleanup"] = self.cleanup()
        return result


r2_management_service = R2ManagementService()
