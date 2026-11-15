from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from api.support import require_admin, require_identity
from services.config import config
from services.image_storage_service import ImageStorageError, image_storage_service
from services.r2_client import R2Error
from services.r2_provision import provision


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProvisionRequest(BaseModel):
    api_token: str = Field(min_length=1, max_length=512)
    domain: str = Field(min_length=1, max_length=253)


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
    except R2Error as exc:
        status = f" HTTP {exc.status_code}" if exc.status_code else ""
        raise HTTPException(status_code=502, detail={"error": "R2 请求失败" + status}) from None
    except ImageStorageError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)}) from None
    except ValueError as exc:
        # Config validation messages name fields only, never submitted values.
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from None


def _overview() -> dict:
    return {
        "settings": config.get_public_image_storage_settings(),
        "accounts": image_storage_service.account_summaries(),
    }


def _save(settings: dict) -> dict:
    config.update({"image_storage": settings})
    return _overview()


def _provision(api_token: str, domain: str) -> dict:
    account = {"id": str(uuid4()), **provision(api_token, domain)}
    accounts = config.get_image_storage_settings()["r2_accounts"]
    return _save({"r2_accounts": [*accounts, account]})


def _resolve(images: list[dict]) -> list[dict]:
    fields = {"image_id", "account_id", "expires_at", "url", "expired", "unavailable"}
    return [{key: value for key, value in image_storage_service.resolve_reference(ref).items() if key in fields}
            for ref in images]


def create_router() -> APIRouter:
    router = APIRouter()
    admin = APIRouter(prefix="/api/image-storage", dependencies=[Depends(_admin)])

    @router.post("/api/image-storage/resolve")
    async def resolve_images(body: ResolveRequest, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return {"items": await _call(_resolve, [item.model_dump(exclude_none=True) for item in body.images])}

    @admin.get("")
    async def overview():
        return await _call(_overview)

    @admin.put("")
    async def save_settings(body: SettingsRequest):
        return await _call(_save, body.model_dump(mode="python"))

    @admin.post("/accounts/provision")
    async def provision_account(body: ProvisionRequest):
        return await _call(_provision, body.api_token, body.domain)

    @admin.post("/accounts/{account_id}/test")
    async def test_connection(account_id: str):
        return {"result": await _call(image_storage_service.test_account, account_id)}

    @admin.post("/cleanup")
    async def cleanup():
        return {"result": await _call(image_storage_service.cleanup_expired)}

    router.include_router(admin)
    return router
