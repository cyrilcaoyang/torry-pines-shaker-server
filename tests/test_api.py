"""API-level tests against the dry-run service."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Spec surface
# ---------------------------------------------------------------------------


def test_probe(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_id"] == "torry_pines_shaker"
    assert body["protocol_version"] == "1.1"


def test_health(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


def test_openapi_doc(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/control/shake/start" in paths
    assert "/control/shake/stop" in paths
    assert "/control/claim" in paths


def test_status_dry_run_envelope(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.get("/status")
    assert r.status_code == 200
    s = r.json()
    assert s["protocol_version"] == "1.1"
    assert s["equipment_kind"] == "shaker"
    # Lifespan auto-connect succeeds in dry-run -> kind is reported as
    # `dry_run`, not `requires_init`.
    assert s["equipment_status"] == "dry_run"
    # dry_run advertises every skill so an operator UI works in dev.
    assert "shake.start" in s["allowed_actions"]
    assert "shake.stop" in s["allowed_actions"]


# ---------------------------------------------------------------------------
# Claim protocol
# ---------------------------------------------------------------------------


def test_control_requires_claim(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.post("/control/shake/stop")
    assert r.status_code == 423
    body = r.json()
    assert "claimed_by" in body
    assert body["claimed_by"] is None


def test_claim_acquire_heartbeat_release(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "tester", "session_id": "s1", "ttl_s": 30.0},
    )
    assert r.status_code == 200
    token = r.json()["claim_token"]

    r = unclaimed_client.post(
        "/control/heartbeat", headers={"X-Claim-Token": token}
    )
    assert r.status_code == 200

    r = unclaimed_client.post(
        "/control/release", headers={"X-Claim-Token": token}
    )
    assert r.status_code == 204

    # After release, the previous token is no longer valid.
    r = unclaimed_client.post(
        "/control/shake/stop", headers={"X-Claim-Token": token}
    )
    assert r.status_code == 423


def test_claim_conflict(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "a", "session_id": "session-a", "ttl_s": 30.0},
    )
    assert r.status_code == 200
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "b", "session_id": "session-b", "ttl_s": 30.0},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["claimed_by"]["session_id"] == "session-a"
    assert body["retry_after_s"] is not None


def test_advisory_mode(advisory_client: TestClient) -> None:
    # Advisory: control works without a token, but /status still
    # publishes `claimed_by` when one is held.
    r = advisory_client.post(
        "/control/shake/set_temperature", json={"temperature_c": 30.0}
    )
    assert r.status_code == 200

    r = advisory_client.post(
        "/control/claim",
        json={"owner": "ux", "session_id": "ux-1", "ttl_s": 30.0},
    )
    assert r.status_code == 200
    r = advisory_client.get("/status")
    assert r.json()["details"]["claimed_by"]["session_id"] == "ux-1"


# ---------------------------------------------------------------------------
# Shake control surface
# ---------------------------------------------------------------------------


def test_set_temperature_validation(client: TestClient) -> None:
    r = client.post(
        "/control/shake/set_temperature", json={"temperature_c": 150.0}
    )
    assert r.status_code == 422


def test_set_speed_validation(client: TestClient) -> None:
    # Level 0 is rejected: the recipe spec says use /shake/stop instead.
    r = client.post("/control/shake/set_speed", json={"speed_level": 0})
    assert r.status_code == 422
    r = client.post("/control/shake/set_speed", json={"speed_level": 10})
    assert r.status_code == 422


def test_shake_start_validation(client: TestClient) -> None:
    # duration_s must be > 0
    r = client.post(
        "/control/shake/start",
        json={
            "speed_level": 3,
            "temperature_c": 25.0,
            "duration_s": 0,
        },
    )
    assert r.status_code == 422


def test_shake_start_round_trip(client: TestClient) -> None:
    r = client.post(
        "/control/shake/start",
        json={
            "speed_level": 5,
            "temperature_c": 25.0,
            "duration_s": 60.0,
            "wait_for_temperature": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["details"]["speed_level"] == 5
    assert body["details"]["duration_s"] == 60.0

    # While busy, /status shows the cycle metadata in `details` and
    # `allowed_actions` flips to the busy set (even in dry-run, so the
    # operator UI can't fire a second start).
    s = client.get("/status").json()
    assert s["details"]["cycle_duration_s"] == 60.0
    assert s["details"]["cycle_speed_level"] == 5
    assert "cycle_ends_at" in s["details"]
    assert "shake.stop" in s["allowed_actions"]
    assert "shake.start" not in s["allowed_actions"]

    # The watchdog hasn't fired yet (60 s timer); stop it now.
    r = client.post("/control/shake/stop")
    assert r.status_code == 200

    s = client.get("/status").json()
    assert "cycle_duration_s" not in s["details"]
    assert "shake.start" in s["allowed_actions"]


def test_shake_start_refuses_when_already_running(client: TestClient) -> None:
    r = client.post(
        "/control/shake/start",
        json={"speed_level": 3, "temperature_c": 25.0, "duration_s": 30.0},
    )
    assert r.status_code == 200

    r = client.post(
        "/control/shake/start",
        json={"speed_level": 3, "temperature_c": 25.0, "duration_s": 30.0},
    )
    assert r.status_code == 409

    client.post("/control/shake/stop")


def test_shake_stop_is_idempotent(client: TestClient) -> None:
    # Calling /shake/stop with no cycle running still 2xx-es.
    r = client.post("/control/shake/stop")
    assert r.status_code == 200
    r = client.post("/control/shake/stop")
    assert r.status_code == 200


def test_watchdog_stops_motor_at_end_of_cycle(client: TestClient) -> None:
    """A 1 s cycle should self-terminate within a few hundred ms of expiry."""
    r = client.post(
        "/control/shake/start",
        json={"speed_level": 4, "temperature_c": 25.0, "duration_s": 1.0},
    )
    assert r.status_code == 200

    s = client.get("/status").json()
    assert "cycle_duration_s" in s["details"]
    assert "shake.stop" in s["allowed_actions"]

    # Give the watchdog a generous window: 1 s sleep + a bit for the
    # lock + thread hop.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        s = client.get("/status").json()
        if "cycle_duration_s" not in s["details"]:
            break
        time.sleep(0.2)
    else:
        pytest.fail("watchdog did not stop the cycle within 5 s")

    s = client.get("/status").json()
    assert "cycle_duration_s" not in s["details"]
    assert s["metrics"]["speed_level"]["value"] == 0


def test_shutdown_during_cycle_stops_motor(unclaimed_client: TestClient) -> None:
    """The lifespan teardown must stop the motor if a cycle is still running."""
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "shutdown-test", "session_id": "stx", "ttl_s": 60.0},
    )
    token = r.json()["claim_token"]
    unclaimed_client.headers["X-Claim-Token"] = token

    r = unclaimed_client.post(
        "/control/shake/start",
        json={"speed_level": 6, "temperature_c": 25.0, "duration_s": 300.0},
    )
    assert r.status_code == 200

    service = unclaimed_client.app.state.service
    assert service._busy is True

    # Trigger /control/shutdown explicitly (the lifespan also runs at
    # TestClient exit, but doing it here exercises the watchdog cancel
    # path with the loop still alive).
    r = unclaimed_client.post("/control/shutdown")
    assert r.status_code == 200

    assert service._busy is False
    assert service._watchdog_task is None or service._watchdog_task.done()


# ---------------------------------------------------------------------------
# wait_for_temperature precondition
# ---------------------------------------------------------------------------


def test_wait_for_temperature_short_circuits_when_in_band(client: TestClient) -> None:
    """In dry-run, the stub clamps actual to setpoint instantly, so
    wait_for_temperature=True succeeds without timing out."""
    r = client.post(
        "/control/shake/start",
        json={
            "speed_level": 3,
            "temperature_c": 25.0,
            "duration_s": 30.0,
            "wait_for_temperature": True,
        },
    )
    assert r.status_code == 200
    client.post("/control/shake/stop")
