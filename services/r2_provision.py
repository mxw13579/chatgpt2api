"""One-shot R2 account setup through the Cloudflare API. The token is never stored."""
from __future__ import annotations

import hashlib
import re

from curl_cffi import requests

from services.image_storage_service import ImageStorageError

_BASE_URL = "https://api.cloudflare.com/client/v4"


def _call(session, token: str, step: str, method: str, path: str, *, json=None, params=None, tolerate: int = 0):
    response = session.request(
        method, _BASE_URL + path, headers={"Authorization": f"Bearer {token}"},
        json=json, params=params, timeout=30.0, allow_redirects=False,
    )
    if response.status_code == tolerate:
        return None
    payload = response.json()
    if not payload.get("success"):
        errors = "；".join(f"{item.get('code')} {item.get('message')}" for item in payload.get("errors") or [])
        raise ImageStorageError(f"{step}失败（Cloudflare HTTP {response.status_code}）：{errors}")
    return payload["result"]


def provision(api_token: str, domain: str) -> dict:
    """Create the bucket and bind the custom domain; return the R2 account fields."""
    token = api_token.strip()
    domain = domain.strip().lower().rstrip(".")
    if not token or not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise ValueError("请填写 Cloudflare API Token 和图片域名（如 img.example.com）")
    session = requests.Session(verify=True, trust_env=False)
    try:
        labels = domain.split(".")
        zone = None
        for start in range(len(labels) - 1):
            zones = _call(session, token, "查询站点", "GET", "/zones", params={"name": ".".join(labels[start:])})
            if zones:
                zone = zones[0]
                break
        if zone is None:
            raise ImageStorageError("该 Token 下找不到此域名所在的 Cloudflare 站点")
        account_id = zone["account"]["id"]
        # User tokens verify under /user; account-owned tokens answer 401 there.
        verified = (_call(session, token, "验证 Token", "GET", "/user/tokens/verify", tolerate=401)
                    or _call(session, token, "验证 Token", "GET", f"/accounts/{account_id}/tokens/verify"))
        bucket = domain.replace(".", "-")[:63].strip("-")
        buckets = f"/accounts/{account_id}/r2/buckets"
        # 409 = already created by an earlier, partly failed attempt.
        _call(session, token, "建桶", "POST", buckets, json={"name": bucket}, tolerate=409)
        _call(session, token, "绑定域名", "POST", f"{buckets}/{bucket}/domains/custom",
              json={"domain": domain, "zoneId": zone["id"], "enabled": True}, tolerate=409)
    finally:
        session.close()
    # R2 S3 credentials are derived from the API token: id + SHA-256 of its value.
    return {
        "name": domain, "enabled": True, "account_id": account_id, "bucket": bucket,
        "access_key_id": verified["id"], "secret_access_key": hashlib.sha256(token.encode()).hexdigest(),
        "public_base_url": f"https://{domain}",
    }
