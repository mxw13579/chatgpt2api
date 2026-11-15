"""One-shot R2 account setup through the Cloudflare API. The token is never stored."""
from __future__ import annotations

import hashlib
import re

from curl_cffi import requests

from services.image_storage_service import ImageStorageError

_BASE_URL = "https://api.cloudflare.com/client/v4"
_ALREADY_EXISTS = 409


def _call(session, token: str, method: str, path: str, *, json=None, params=None, allow_exists: bool = False):
    response = session.request(
        method, _BASE_URL + path, headers={"Authorization": f"Bearer {token}"},
        json=json, params=params, timeout=30.0, allow_redirects=False,
    )
    if allow_exists and response.status_code == _ALREADY_EXISTS:
        return None
    payload = response.json()
    if not payload.get("success"):
        errors = "；".join(f"{item.get('code')} {item.get('message')}" for item in payload.get("errors") or [])
        raise ImageStorageError(f"Cloudflare API 失败（HTTP {response.status_code}）：{errors}")
    return payload["result"]


def provision(api_token: str, domain: str) -> dict:
    """Create the bucket and bind the custom domain; return the R2 account fields."""
    token = api_token.strip()
    domain = domain.strip().lower().rstrip(".")
    if not token or not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise ValueError("请填写 Cloudflare API Token 和图片域名（如 img.example.com）")
    session = requests.Session(verify=True, trust_env=False)
    try:
        token_id = _call(session, token, "GET", "/user/tokens/verify")["id"]
        labels = domain.split(".")
        zone = None
        for start in range(len(labels) - 1):
            zones = _call(session, token, "GET", "/zones", params={"name": ".".join(labels[start:])})
            if zones:
                zone = zones[0]
                break
        if zone is None:
            raise ImageStorageError("该 Token 下找不到此域名所在的 Cloudflare 站点")
        account_id = zone["account"]["id"]
        bucket = domain.replace(".", "-")[:63].strip("-")
        buckets = f"/accounts/{account_id}/r2/buckets"
        # 409 = already created by an earlier, partly failed attempt.
        _call(session, token, "POST", buckets, json={"name": bucket}, allow_exists=True)
        _call(session, token, "POST", f"{buckets}/{bucket}/domains/custom",
              json={"domain": domain, "zoneId": zone["id"], "enabled": True}, allow_exists=True)
    finally:
        session.close()
    # R2 S3 credentials are derived from the API token: id + SHA-256 of its value.
    return {
        "name": domain, "enabled": True, "account_id": account_id, "bucket": bucket,
        "access_key_id": token_id, "secret_access_key": hashlib.sha256(token.encode()).hexdigest(),
        "public_base_url": f"https://{domain}",
    }
