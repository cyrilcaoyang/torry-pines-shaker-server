"""STATUS_SPEC v1.2 conformance tests (§2.3, §6.2, §9 checklist).

The motivating scenario is this exact device: a chronic heater RTD cal fault
(readback failures on the temperature queries) with a perfectly healthy
motor. Pre-v1.2 the device reported `busy` mid-cycle (masking the fault) and
`degraded` between cycles with `allowed_actions: ["shutdown"]` while still
honoring POST /control/shake/start — the §6.2 violation caught live on
2026-07-25.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from torry_pines_shaker_server.api import create_app
from torry_pines_shaker_server.service import ShakerService
from torry_pines_shaker_server.shaker_driver import StubShaker


class FailingHeaterStub(StubShaker):
    """Heater readbacks fail (the SC25XR cal-fault shape); writes and the
    motor side stay healthy — exactly what the bench showed live."""

    def get_actual_temperature(self) -> float:
        raise RuntimeError(
            "High Point Measured Cal Value is Lower than Low Point Measured Value"
        )

    def get_target_temperature(self) -> float | None:
        raise RuntimeError("cal fault: target unreadable")


class FailingMotorStub(StubShaker):
    """Motor speed readback fails; heater side stays healthy."""

    def get_speed(self) -> int:
        raise RuntimeError("speed register unreadable")


def _service(driver_factory) -> ShakerService:
    # dry_run=False so _compose_status exercises the real health path;
    # enforce_claims off — claims are covered by test_api.py.
    return ShakerService(
        dry_run=False, driver_factory=driver_factory, enforce_claims=False
    )


async def _started(driver_factory) -> ShakerService:
    svc = _service(driver_factory)
    await svc.startup()
    return svc


# ---------------------------------------------------------------------------
# §2.3 — degraded + running: neither fact hides the other
# ---------------------------------------------------------------------------


async def test_degraded_and_running_are_both_reported():
    svc = await _started(FailingHeaterStub)
    try:
        status = await svc.get_status()
        assert status.protocol_version == "1.2"
        assert status.equipment_status == "degraded"
        assert status.activity == "idle"

        await svc.start_shake(speed_level=3, temperature_c=25.0, duration_s=60.0)
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        # The motivating envelope: health stays degraded, activity runs.
        assert status.equipment_status == "degraded"
        assert status.activity == "running"
        assert "shaking continues" in (status.message or "")
        assert status.components["motor"].state == "running"
        # No second concurrent run; abort stays reachable (§2.3).
        assert "shake.start" not in status.allowed_actions
        assert "shake.stop" in status.allowed_actions

        await svc.stop_shake()
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        assert status.equipment_status == "degraded"
        assert status.activity == "idle"
    finally:
        await svc.shutdown()


async def test_healthy_cycle_is_busy_and_running():
    svc = await _started(StubShaker)
    try:
        await svc.start_shake(speed_level=3, temperature_c=25.0, duration_s=60.0)
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        # busy ≡ healthy + running (§2.3 invariant)
        assert status.equipment_status == "busy"
        assert status.activity == "running"
        await svc.stop_shake()
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        # ready ⇒ idle (§2.3 invariant)
        assert status.equipment_status == "ready"
        assert status.activity == "idle"
    finally:
        await svc.shutdown()


async def test_requires_init_implies_idle():
    svc = _service(StubShaker)  # never started
    status = await svc.get_status()
    assert status.equipment_status == "requires_init"
    assert status.activity == "idle"
    assert status.allowed_actions == ["startup"]


async def test_unreadable_motor_is_unknown_never_false_idle():
    svc = await _started(FailingMotorStub)
    try:
        status = await svc.get_status()
        assert status.equipment_status == "degraded"
        assert status.activity == "unknown"
        assert status.components["motor"].state == "unknown"
        # Motor-side actions withheld; heater-side still offered.
        assert "shake.start" not in status.allowed_actions
        assert "shake.set_speed" not in status.allowed_actions
        assert "shake.set_temperature" in status.allowed_actions
    finally:
        await svc.shutdown()


# ---------------------------------------------------------------------------
# §2.3 — activity_since spans
# ---------------------------------------------------------------------------


async def test_activity_since_stamps_transitions():
    svc = await _started(StubShaker)
    try:
        status = await svc.get_status()
        idle_since = status.activity_since
        assert idle_since is not None

        await svc.start_shake(speed_level=2, temperature_c=25.0, duration_s=60.0)
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        running_since = status.activity_since
        assert running_since is not None and running_since > idle_since

        # Unchanged activity does not restart the span.
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        assert status.activity_since == running_since
    finally:
        await svc.shutdown()


# ---------------------------------------------------------------------------
# §2.3.1 — cycles_total counts what sampling misses
# ---------------------------------------------------------------------------


async def test_cycles_total_counts_watchdog_and_operator_stops():
    svc = await _started(StubShaker)
    try:
        # A cycle far shorter than any poll interval, ended by the watchdog.
        await svc.start_shake(speed_level=1, temperature_c=25.0, duration_s=0.05)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not svc._busy:
                break
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        assert status.metrics["cycles_total"].value == 1

        # An operator-stopped cycle counts too.
        await svc.start_shake(speed_level=1, temperature_c=25.0, duration_s=60.0)
        await svc.stop_shake()
        # A stop with no engaged cycle does NOT count.
        await svc.stop_shake()
        svc._invalidate_readings_cache()
        status = await svc.get_status()
        assert status.metrics["cycles_total"].value == 2
    finally:
        await svc.shutdown()


# ---------------------------------------------------------------------------
# §6.2 — allowed_actions and /control/* refusals cannot disagree
# ---------------------------------------------------------------------------


async def test_gate_agrees_with_allowed_actions_heater_fault():
    svc = await _started(FailingHeaterStub)
    try:
        status = await svc.get_status()
        # Advertised: motor-side yes, heater-side no.
        assert "shake.start" in status.allowed_actions
        assert "shake.set_speed" in status.allowed_actions
        assert "shake.set_temperature" not in status.allowed_actions

        # Gate mirrors exactly: advertised → None, withheld → 412 body.
        assert await svc.evaluate_action_gate("shake.start") is None
        assert await svc.evaluate_action_gate("shake.set_speed") is None
        body = await svc.evaluate_action_gate("shake.set_temperature")
        assert body is not None and body["blocked_subsystem"] == "heater"
        # Per-request extra: waiting on an unreadable heater is refused
        # even though plain shake.start is allowed.
        body = await svc.evaluate_action_gate(
            "shake.start", wait_for_temperature=True
        )
        assert body is not None and body["blocked_subsystem"] == "heater"
    finally:
        await svc.shutdown()


def test_http_412_mirrors_the_advertised_list():
    """End-to-end §6.2: what /status omits, /control refuses with 412 —
    and what it lists is honored (no 412)."""
    svc = _service(FailingHeaterStub)
    app = create_app(service=svc)
    with TestClient(app) as client:
        s = client.get("/status").json()
        assert s["equipment_status"] == "degraded"
        assert "shake.set_temperature" not in s["allowed_actions"]

        r = client.post(
            "/control/shake/set_temperature", json={"temperature_c": 25.0}
        )
        assert r.status_code == 412
        assert r.json()["blocked_subsystem"] == "heater"

        # The advertised action is honored — the live 2026-07-25 violation,
        # inverted: a heater fault no longer forbids (or falsely forbids)
        # shaking.
        r = client.post(
            "/control/shake/start",
            json={"speed_level": 3, "temperature_c": 25.0, "duration_s": 60.0},
        )
        assert r.status_code == 200
        r = client.post("/control/shake/stop")
        assert r.status_code == 200


def test_fixture_snapshots_validate_against_the_contract():
    """§9 v1.2 checklist: snapshot fixtures parse under sdl-lab-contract and
    respect the §2.3 consistency invariants."""
    import json
    from pathlib import Path

    from sdl_lab_contract import EquipmentStatus

    required_activity = {
        "busy": {"running"},
        "ready": {"idle"},
        "requires_init": {"idle"},
        "e_stop": {"idle"},
        "degraded": {"running", "idle"},
    }
    fixtures = sorted((Path(__file__).parent / "fixtures").glob("status_*.json"))
    assert len(fixtures) >= 3  # ready+idle, busy+running, degraded+running
    for path in fixtures:
        status = EquipmentStatus.model_validate(json.loads(path.read_text()))
        assert status.protocol_version == "1.2", path.name
        allowed_activities = required_activity.get(status.equipment_status)
        if allowed_activities is not None:
            assert status.activity in allowed_activities, path.name
