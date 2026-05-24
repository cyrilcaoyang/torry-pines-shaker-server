"""Shared pytest fixtures.

All tests run the FastAPI app with ``dry_run=True`` so no serial /
Windows dependencies are required. ``enforce_claims=True`` is the
production default and is exercised here; ``client`` pre-acquires a
claim and attaches ``X-Claim-Token`` to every subsequent request.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from torry_pines_shaker_server.api import create_app


@pytest.fixture
def unclaimed_client() -> Iterator[TestClient]:
    """A TestClient that does not pre-acquire a claim.

    Use for tests of the public spec surface (`/`, `/health`, `/status`,
    `/openapi.json`) and for tests that explicitly assert `/control/*`
    returns 423 when no token is provided.
    """
    app = create_app(dry_run=True, enforce_claims=True)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(unclaimed_client: TestClient) -> TestClient:
    """Default TestClient for `/control/*` tests.

    Pre-acquires a claim and attaches `X-Claim-Token` to every request.
    """
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "pytest", "session_id": "pytest-default", "ttl_s": 60.0},
    )
    assert r.status_code == 200, r.text
    unclaimed_client.headers["X-Claim-Token"] = r.json()["claim_token"]
    return unclaimed_client


@pytest.fixture
def advisory_client() -> Iterator[TestClient]:
    """A TestClient with `enforce_claims=False`."""
    app = create_app(dry_run=True, enforce_claims=False)
    with TestClient(app) as c:
        yield c
