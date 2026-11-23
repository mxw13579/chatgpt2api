from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.ai import ImageGenerationRequest
from api.errors import install_exception_handlers


def test_openai_compatible_validation_errors_return_400() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/v1/images/generations")
    def generations(body: ImageGenerationRequest) -> dict:
        return {}

    @app.post("/api/other")
    def other(body: ImageGenerationRequest) -> dict:
        return {}

    client = TestClient(app)
    response = client.post("/v1/images/generations", json={"prompt": ""})
    assert response.status_code == 400
    assert "prompt" in response.text
    assert client.post("/api/other", json={"prompt": ""}).status_code == 422
