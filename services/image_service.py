from __future__ import annotations

import threading
import time

from fastapi.responses import RedirectResponse

from services.config import config
from services.image_storage_service import image_storage_service
from utils.log import logger


def get_image_response(relative_path: str) -> RedirectResponse:
    # Public routes must go through the protected domain, never our S3 credentials.
    return RedirectResponse(
        image_storage_service.public_url(relative_path),
        status_code=307,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "no-store",
        },
    )


def _auto_cleanup_worker(stop_event: threading.Event) -> None:
    """Run R2 expiry cleanup every cleanup_interval_seconds while accounts are configured."""
    last_run = time.monotonic()
    while True:
        settings = config.get_image_storage_settings()
        # Re-read settings at least every minute.
        wait_seconds = 60.0
        if settings["r2_accounts"]:
            wait_seconds = min(wait_seconds, last_run + settings["cleanup_interval_seconds"] - time.monotonic())
        if stop_event.wait(max(0.0, wait_seconds)):
            return
        settings = config.get_image_storage_settings()
        now = time.monotonic()
        if not settings["r2_accounts"] or now - last_run < settings["cleanup_interval_seconds"]:
            continue
        last_run = now
        try:
            image_storage_service.cleanup_expired()
        except Exception:
            # No raw transport exceptions/URLs in the maintenance log.
            logger.warning({"event": "image_maintenance_failed"})


def start_image_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    thread = threading.Thread(
        target=_auto_cleanup_worker, args=(stop_event,), daemon=True, name="image-cleanup",
    )
    thread.start()
    return thread
