"""Agent documentation routes + the claim-owner log lines on startup/shutdown."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from torry_pines_shaker_server.documentation import router


def test_agent_docs_routes(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.get("/agent-docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "shutdown" in r.text and "allowed_actions" in r.text
    r = unclaimed_client.get("/agent-docs/api-reference")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "/control/shake/start" in r.text
    r = unclaimed_client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "(agent-docs/api-reference)" in r.text and "(openapi.json)" in r.text


@pytest.mark.parametrize("prefix", ["", "/shaker", "/api/equipment/test/documentation"])
def test_index_links_without_hardware(prefix: str) -> None:
    service = FastAPI()
    service.include_router(router)
    app = FastAPI()
    app.mount(prefix or "/", service)
    with TestClient(app) as client:
        response = client.get(f"{prefix}/llms.txt")
        assert response.status_code == 200
        links = re.findall(r"\]\(([^)]+)\)", response.text)
        assert set(links) == {"agent-docs", "agent-docs/api-reference", "openapi.json"}
        for link in links:
            resolved = urljoin(str(response.url), link)
            assert resolved == f"http://testserver{prefix}/{link}"
            assert client.get(resolved).status_code == 200


def test_openapi_lists_documentation_routes(unclaimed_client: TestClient) -> None:
    paths = unclaimed_client.get("/openapi.json").json()["paths"]
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt"} <= set(paths)


def test_shutdown_and_startup_log_the_claim_owner(
    unclaimed_client: TestClient, caplog
) -> None:
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "agent:docs-test", "session_id": "s-1", "ttl_s": 30},
    )
    unclaimed_client.headers["X-Claim-Token"] = r.json()["claim_token"]
    with caplog.at_level(logging.INFO):
        assert unclaimed_client.post("/control/shutdown").status_code == 200
        assert unclaimed_client.post("/control/startup").status_code == 200
    text = caplog.text
    assert "shutdown requested by agent:docs-test/s-1" in text
    assert "startup requested by agent:docs-test/s-1" in text
