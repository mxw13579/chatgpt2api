from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.support import require_admin, require_identity
from services.cloudflare_api import CloudflareAPIError
from services.image_storage_service import ImageStorageError
from services.r2_client import R2Error
from services.r2_management_service import R2ManagementError, cloud_error, r2_management_service


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class DomainRequest(BaseModel):
    domain: str
    zone_id: str


class DomainUpdateRequest(BaseModel):
    active: bool | None = None
    enabled: bool | None = None


class DomainCheckRequest(BaseModel):
    domain_id: str | None = None


class ProtectionRequest(BaseModel):
    rate_limit_requests: int = Field(ge=1, strict=True)
    restrict_methods: bool
    cors_origins: list[str]


class ConfirmRequest(BaseModel):
    confirm: bool = Field(default=False, strict=True)


class ImageReference(BaseModel):
    image_id: str = Field(min_length=1, max_length=512)
    account_id: str | None = None
    expires_at: str | None = None


class ResolveRequest(BaseModel):
    images: list[ImageReference] = Field(max_length=500)


def _admin(authorization: str | None = Header(default=None)) -> dict:
    return require_admin(authorization)


async def _call(method, *args):
    try:
        return await run_in_threadpool(method, *args)
    except R2ManagementError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from None
    except (CloudflareAPIError, R2Error) as exc:
        raise HTTPException(status_code=502, detail={"error": cloud_error(exc)}) from None
    except ValueError:
        # Config validation can receive secrets. Do not echo incoming values or exceptions.
        raise HTTPException(status_code=400, detail={"error": "图片存储配置或请求参数无效"}) from None
    except ImageStorageError:
        raise HTTPException(status_code=409, detail={"error": "图片存储操作不可用，请检查账号状态"}) from None


def create_router() -> APIRouter:
    router = APIRouter()
    admin = APIRouter(prefix="/api/image-storage", dependencies=[Depends(_admin)])

    @router.post("/api/image-storage/resolve")
    async def resolve_images(body: ResolveRequest, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        items = await _call(r2_management_service.resolve_images, [item.model_dump(exclude_none=True) for item in body.images])
        return {"items": items}

    @admin.get("")
    async def overview():
        return await _call(r2_management_service.overview)

    @admin.put("")
    async def save_settings(body: SettingsRequest):
        return await _call(r2_management_service.save_settings, body.model_dump(mode="python"))

    @admin.post("/accounts/{account_id}/test")
    async def test_connection(account_id: str):
        return {"result": await _call(r2_management_service.test_connection, account_id)}

    @admin.post("/accounts/{account_id}/usage/refresh")
    async def refresh_usage(account_id: str):
        return {"result": await _call(r2_management_service.refresh_usage, account_id)}

    @admin.get("/accounts/{account_id}/zones")
    async def list_zones(account_id: str):
        return {"items": await _call(r2_management_service.list_zones, account_id)}

    @admin.get("/accounts/{account_id}/domains")
    async def list_domains(account_id: str):
        return await _call(r2_management_service.list_domains, account_id)

    @admin.post("/accounts/{account_id}/domains")
    async def add_domain(account_id: str, body: DomainRequest):
        return {"item": await _call(r2_management_service.add_domain, account_id, body.domain, body.zone_id)}

    @admin.patch("/accounts/{account_id}/domains/{domain_id}")
    async def update_domain(account_id: str, domain_id: str, body: DomainUpdateRequest, identity: dict = Depends(_admin)):
        return {"item": await _call(r2_management_service.update_domain, account_id, domain_id,
                                   body.model_dump(exclude_none=True), str(identity["id"]))}

    @admin.delete("/accounts/{account_id}/domains/{domain_id}")
    async def delete_domain(account_id: str, domain_id: str):
        await _call(r2_management_service.delete_domain, account_id, domain_id)
        return {"ok": True}

    @admin.post("/accounts/{account_id}/domains/check")
    async def check_domains(account_id: str, body: DomainCheckRequest):
        return {"items": await _call(r2_management_service.check_domains, account_id, body.domain_id)}

    @admin.put("/accounts/{account_id}/protection")
    async def apply_protection(account_id: str, body: ProtectionRequest):
        return {"result": await _call(r2_management_service.apply_protection, account_id,
                                     body.rate_limit_requests, body.restrict_methods, body.cors_origins)}

    @admin.post("/accounts/{account_id}/access/restore")
    async def restore_access(account_id: str):
        return {"result": await _call(r2_management_service.restore_access, account_id)}

    @admin.post("/cleanup")
    async def cleanup():
        return {"result": await _call(r2_management_service.cleanup)}

    @admin.post("/legacy/cleanup")
    async def cleanup_legacy(body: ConfirmRequest):
        if body.confirm is not True:
            raise HTTPException(status_code=400, detail={"error": "清理旧图片必须显式确认"})
        return {"result": await _call(r2_management_service.cleanup_legacy)}

    router.include_router(admin)
    return router
