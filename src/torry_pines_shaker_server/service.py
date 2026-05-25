"""Service layer for the Torrey Pines shaker.

Owns:

* a single driver instance (real or stub),
* an :class:`asyncio.Lock` that serialises every call into the driver,
* a watchdog task that times the active shake cycle and stops the
  motor at the end (or on process exit). Per the recipe v2 §3.5, the
  device server — not the workflow — owns the duration timer.
* a small in-memory state machine (``_busy``, ``_cycle_*``,
  ``_last_error``) used to compute the spec ``equipment_status``.

The duration timer is implemented as an :class:`asyncio.Task` started
inside ``/control/shake/start``. When the task fires it takes the
service lock, drops the motor to speed 0 and sets idle. If the
service is asked to shut down while a cycle is active, the lifespan
teardown cancels the timer and stops the motor synchronously — the
watchdog contract from the recipe.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import config as _config
from .claims import ClaimStore
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
    MetricValue,
)
from .shaker_driver import StubShaker, TorreyPinesShakerFacade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# allowed_actions per equipment_status (v1.1)
#
# Mirrors the inverse of the (proposed) skill catalog `requires_states`
# for a shaker. Skill names follow the same dotted-namespace pattern as
# the SDK catalog (``seal.start``, ``stage.in``, ...) so a future
# ``skill_catalog/shaker.py`` can quote them verbatim.
# ---------------------------------------------------------------------------

_ALL_SHAKER_SKILLS = [
    "startup",
    "shutdown",
    "shake.start",
    "shake.stop",
    "shake.set_temperature",
    "shake.set_speed",
]

_ALLOWED_ACTIONS_BY_STATE: dict[str, list[str]] = {
    "requires_init": ["startup"],
    "ready": [
        "startup",
        "shutdown",
        "shake.start",
        "shake.set_temperature",
        "shake.set_speed",
    ],
    "busy": ["shutdown", "shake.stop"],
    "degraded": ["shutdown"],
    "error": ["shutdown"],
    "e_stop": [],
    "unknown": [],
    "dry_run": list(_ALL_SHAKER_SKILLS),
}


_RECENT_ERROR_WINDOW_S = 60.0


LAST_ERROR_CODES: frozenset[str] = frozenset(
    {
        "serial_init_failed",
        "serial_timeout",
        "serial_other",
        "rtd_disconnected",
        "rtd_shorted",
        "calibration_error",
        "process_internal",
    }
)


def _classify_error(method_name: str, exc: Exception) -> str:
    if isinstance(exc, (KeyError, AttributeError, TypeError, NameError)):
        return "process_internal"
    text = str(exc).lower()
    if "rtd sensor is not connected" in text:
        return "rtd_disconnected"
    if "rtd sensor has shorted" in text:
        return "rtd_shorted"
    if "cal" in text and "out of range" in text:
        return "calibration_error"
    if isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text:
        return "serial_timeout"
    if method_name == "startup":
        return "serial_init_failed"
    return "serial_other"


class TemperatureNotReady(Exception):
    """Raised by :meth:`ShakerService.start_shake` when the caller asked
    for ``wait_for_temperature=True`` but the setpoint was not reached
    within ``wait_for_temperature_timeout_s``.
    """

    def __init__(
        self,
        message: str,
        *,
        actual_c: float | None,
        setpoint_c: float | None,
        tolerance_c: float,
        retry_after_s: float | None,
    ) -> None:
        super().__init__(message)
        self.actual_c = actual_c
        self.setpoint_c = setpoint_c
        self.tolerance_c = tolerance_c
        self.retry_after_s = retry_after_s


class ShakerService:
    """Wraps a Torrey Pines shaker driver and produces spec-compliant
    :class:`EquipmentStatus` snapshots.

    Concurrency: all driver I/O happens inside ``self._lock``. The
    watchdog timer takes the same lock when it fires so it cannot
    interleave with an in-flight ``/control/*`` call.
    """

    def __init__(
        self,
        dry_run: bool = False,
        *,
        driver_factory: Callable[[], Any] | None = None,
        enforce_claims: bool = True,
        watchdog_stop_on_exit: bool = True,
    ) -> None:
        self.dry_run = dry_run
        self._driver_factory = driver_factory
        self._driver: Any | None = None
        self._lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._last_error: ErrorInfo | None = None

        # Shake-cycle state. ``_busy`` mirrors "motor engaged"; the
        # other fields are set/cleared together with ``_watchdog_task``.
        self._busy: bool = False
        self._cycle_started_at: datetime | None = None
        self._cycle_ends_at: datetime | None = None
        self._cycle_speed_level: int | None = None
        self._cycle_target_c: float | None = None
        self._cycle_duration_s: float | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

        self.enforce_claims = enforce_claims
        self.watchdog_stop_on_exit = watchdog_stop_on_exit
        self.claims = ClaimStore()

        self._temp_tolerance_c: float = float(
            _config.get("service", "temperature_tolerance_c", 1.0)
        )
        self._wait_for_temp_timeout_s: float = float(
            _config.get("service", "wait_for_temperature_timeout_s", 1800.0)
        )

        self.equipment_id: str = _config.get(
            "dashboard", "equipment_id", "torry_pines_shaker"
        )
        self.equipment_name: str = _config.get(
            "dashboard", "equipment_name", "Torrey Pines Shaker"
        )
        self.equipment_kind = "shaker"
        self.equipment_version: str | None = _config.get(
            "dashboard", "equipment_version", None
        )

    # ---- lifecycle ---------------------------------------------------------

    def _create_driver(self) -> Any:
        if self._driver_factory is not None:
            return self._driver_factory()
        if self.dry_run:
            return StubShaker()
        return TorreyPinesShakerFacade(
            com_port=_config.get("instrument", "com_port", "COM3"),
            max_temp_c=int(_config.get("instrument", "max_temp_c", 110)),
            min_temp_c=int(_config.get("instrument", "min_temp_c", -20)),
            baudrate=int(_config.get("instrument", "baudrate", 9600)),
            timeout=float(_config.get("instrument", "timeout", 1.0)),
        )

    async def startup(self) -> None:
        """Create and verify the driver.

        On failure, leaves the service in ``requires_init`` and re-raises.
        Does NOT clear ``self._last_error`` on success — the API layer
        owns that policy.
        """
        async with self._lock:
            if self._driver is not None:
                return
            try:
                driver = await asyncio.to_thread(self._create_driver)
            except Exception as exc:
                self._record_error(exc, "startup")
                raise
            self._driver = driver

    async def shutdown(self) -> None:
        """Best-effort teardown. Never raises.

        Stops the motor if a cycle is active (watchdog contract), then
        closes the serial port.
        """
        # Cancel watchdog first so it cannot race the close.
        await self._cancel_watchdog()
        async with self._lock:
            if self._driver is None:
                self._reset_cycle_state_locked()
                return
            if self.watchdog_stop_on_exit:
                try:
                    await asyncio.to_thread(self._driver.set_speed, 0)
                    await asyncio.to_thread(self._driver.set_idle, True)
                except Exception:
                    logger.exception("Watchdog: failed to stop motor on shutdown")
            try:
                await asyncio.to_thread(self._driver.close)
            except Exception:
                logger.exception("Error while closing driver")
            finally:
                self._driver = None
                self._reset_cycle_state_locked()

    # ---- control -----------------------------------------------------------

    async def set_temperature(self, temp_c: float) -> None:
        await self._do(
            "set_temperature",
            lambda d: d.set_temperature(float(temp_c)),
        )

    async def set_speed(self, level: int) -> None:
        if level < 1 or level > 9:
            # Recipe §3.5: shake step requires 1..9. Level 0 is "motor
            # off"; the operator should use /control/shake/stop instead.
            raise ValueError(
                "speed_level must be 1..9 (use /control/shake/stop to halt)"
            )
        await self._do("set_speed", lambda d: d.set_speed(int(level)))

    async def start_shake(
        self,
        *,
        speed_level: int,
        temperature_c: float,
        duration_s: float,
        wait_for_temperature: bool = False,
    ) -> dict[str, Any]:
        """Run a single shake cycle.

        Sets the temperature setpoint and speed, engages the motor,
        and starts the duration watchdog. The device server — not the
        caller — owns the timer; on process exit the watchdog stops
        the motor.

        Raises
        ------
        RuntimeError
            Driver not connected, or a cycle is already running.
        ValueError
            ``speed_level`` outside 1..9.
        TemperatureNotReady
            ``wait_for_temperature=True`` and the setpoint was not
            reached within the configured timeout.
        """
        if speed_level < 1 or speed_level > 9:
            raise ValueError("speed_level must be 1..9")
        if duration_s <= 0:
            raise ValueError("duration_s must be > 0")

        # Pre-flight outside the cycle lock so a wait can be cancelled
        # by a concurrent /control/shake/stop. The lock is taken below
        # for the actual motor engagement.
        async with self._lock:
            if self._driver is None:
                raise RuntimeError(
                    "Shaker is not connected. POST /control/startup first."
                )
            if self._busy:
                raise RuntimeError(
                    "Shaker is already running a cycle. POST /control/shake/stop first."
                )
            # Set the heater setpoint up front so the optional
            # temperature wait below sees the right target.
            try:
                await asyncio.to_thread(self._driver.set_temperature, float(temperature_c))
            except Exception as exc:
                self._record_error(exc, "set_temperature")
                raise

        if wait_for_temperature:
            await self._wait_for_temperature(float(temperature_c))

        async with self._lock:
            if self._driver is None:
                raise RuntimeError("Shaker disconnected during temperature wait")
            if self._busy:
                # Another caller raced us in. Refuse rather than overwrite.
                raise RuntimeError(
                    "Shaker is already running a cycle. POST /control/shake/stop first."
                )
            now = datetime.now(timezone.utc)
            try:
                # The vendor driver requires speed > 0 AND idle=False
                # to actually move the orbital head.
                await asyncio.to_thread(self._driver.set_idle, False)
                await asyncio.to_thread(self._driver.set_speed, int(speed_level))
            except Exception as exc:
                self._record_error(exc, "start_shake")
                # Best-effort: leave the head stopped if we failed to engage.
                try:
                    await asyncio.to_thread(self._driver.set_speed, 0)
                    await asyncio.to_thread(self._driver.set_idle, True)
                except Exception:
                    pass
                raise

            self._busy = True
            self._cycle_started_at = now
            self._cycle_ends_at = now + timedelta(seconds=float(duration_s))
            self._cycle_duration_s = float(duration_s)
            self._cycle_speed_level = int(speed_level)
            self._cycle_target_c = float(temperature_c)
            self._watchdog_task = asyncio.create_task(
                self._watchdog(float(duration_s)),
                name="shaker-watchdog",
            )
            return {
                "started_at": self._cycle_started_at,
                "ends_at": self._cycle_ends_at,
                "duration_s": self._cycle_duration_s,
                "speed_level": self._cycle_speed_level,
                "temperature_c": self._cycle_target_c,
            }

    async def stop_shake(self) -> None:
        """Halt the current cycle (operator-driven).

        Idempotent: stopping when no cycle is running is a no-op 2xx.
        Cancels the watchdog and drops the motor to speed 0.
        """
        await self._cancel_watchdog()
        async with self._lock:
            if self._driver is None:
                raise RuntimeError(
                    "Shaker is not connected. POST /control/startup first."
                )
            try:
                await asyncio.to_thread(self._driver.set_speed, 0)
                await asyncio.to_thread(self._driver.set_idle, True)
            except Exception as exc:
                self._record_error(exc, "stop_shake")
                raise
            self._reset_cycle_state_locked()

    # ---- watchdog ----------------------------------------------------------

    async def _watchdog(self, duration_s: float) -> None:
        """Fires at the end of a cycle. Stops the motor under the
        service lock so it cannot interleave with another control call.

        Cancellation: lifespan teardown / ``stop_shake`` cancel this
        task. CancelledError must NOT touch the driver — the caller
        owns the cleanup.
        """
        try:
            await asyncio.sleep(duration_s)
        except asyncio.CancelledError:
            raise
        try:
            async with self._lock:
                if self._driver is None or not self._busy:
                    return
                try:
                    await asyncio.to_thread(self._driver.set_speed, 0)
                    await asyncio.to_thread(self._driver.set_idle, True)
                except Exception as exc:
                    self._record_error(exc, "watchdog_stop")
                    return
                logger.info(
                    "watchdog: cycle complete (duration=%.1fs), motor stopped",
                    duration_s,
                )
                self._reset_cycle_state_locked()
        except Exception:
            logger.exception("watchdog: unexpected failure")

    async def _cancel_watchdog(self) -> None:
        """Cancel any active watchdog task and wait for it to exit.
        Called outside ``self._lock`` so the watchdog can finish any
        work that already holds the lock."""
        task = self._watchdog_task
        if task is None:
            return
        self._watchdog_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _reset_cycle_state_locked(self) -> None:
        """Clear the in-memory cycle bookkeeping. Caller MUST hold
        ``self._lock``."""
        self._busy = False
        self._cycle_started_at = None
        self._cycle_ends_at = None
        self._cycle_duration_s = None
        self._cycle_speed_level = None
        self._cycle_target_c = None
        # Note: the task reference is cleared by ``_cancel_watchdog`` or
        # by the watchdog itself before this call; safe to leave alone.

    # ---- preconditions -----------------------------------------------------

    async def _wait_for_temperature(self, setpoint_c: float) -> None:
        """Poll ``get_actual_temperature`` until it's within band or the
        configured timeout elapses.

        Polls every 1 s. Each read takes the service lock briefly so it
        cannot interleave with another control call.
        """
        deadline = time.monotonic() + self._wait_for_temp_timeout_s
        tolerance = self._temp_tolerance_c
        last_actual: float | None = None
        while time.monotonic() < deadline:
            async with self._lock:
                if self._driver is None:
                    raise RuntimeError("Shaker disconnected during temperature wait")
                try:
                    last_actual = float(
                        await asyncio.to_thread(self._driver.get_actual_temperature)
                    )
                except Exception as exc:
                    self._record_error(exc, "wait_for_temperature")
                    raise
            if last_actual is not None and abs(last_actual - setpoint_c) <= tolerance:
                return
            await asyncio.sleep(1.0)

        raise TemperatureNotReady(
            "Setpoint not reached within timeout",
            actual_c=last_actual,
            setpoint_c=setpoint_c,
            tolerance_c=tolerance,
            retry_after_s=None,
        )

    # ---- status (side-effect-free) -----------------------------------------

    async def get_status(self) -> EquipmentStatus:
        """Produce a fresh status snapshot. MUST NOT mutate hardware state."""
        async with self._lock:
            status = self._build_status()
        claimed_by = await self.claims.current()
        if claimed_by is not None:
            status.details["claimed_by"] = claimed_by.model_dump(mode="json")
        return status

    def _build_status(self) -> EquipmentStatus:
        now = datetime.now(timezone.utc)
        uptime = time.monotonic() - self._started_at
        host = socket.gethostname()

        if self._driver is None:
            return EquipmentStatus(
                protocol_version=PROTOCOL_VERSION,
                equipment_id=self.equipment_id,
                equipment_name=self.equipment_name,
                equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
                equipment_version=self.equipment_version,
                host=host,
                equipment_status="requires_init",
                message="Driver not connected. POST /control/startup to initialize.",
                required_actions=["startup"],
                allowed_actions=list(_ALLOWED_ACTIONS_BY_STATE["requires_init"]),
                device_time=now,
                uptime_seconds=uptime,
                last_error=self._last_error,
                details={},
            )

        metrics: dict[str, MetricValue] = {}
        details: dict[str, Any] = {}
        readback_errors: list[str] = []

        def _read(label: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:
                readback_errors.append(f"{label}: {exc}")
                return None

        actual_temp = _read("actual_temperature", self._driver.get_actual_temperature)
        if actual_temp is not None:
            metrics["actual_temperature"] = MetricValue(
                value=actual_temp, unit="C", timestamp=now
            )
        target_temp = _read("setpoint_temperature", self._driver.get_target_temperature)
        if target_temp is not None:
            metrics["setpoint_temperature"] = MetricValue(
                value=target_temp, unit="C", timestamp=now
            )
        speed = _read("speed_level", self._driver.get_speed)
        if speed is not None:
            metrics["speed_level"] = MetricValue(value=speed, unit="level")
        idle = _read("idle", self._driver.get_idle)

        try:
            model = self._driver.get_device_model()
            if model:
                details["device_model"] = model
        except Exception:
            pass
        try:
            serial_no = self._driver.get_serial_number()
            if serial_no:
                details["serial_number"] = serial_no
        except Exception:
            pass
        com_port = getattr(self._driver, "com_port", None)
        if com_port:
            details["com_port"] = com_port

        # Cycle metadata (only meaningful while busy)
        if self._cycle_ends_at is not None:
            details["cycle_ends_at"] = self._cycle_ends_at.isoformat()
            remaining = (self._cycle_ends_at - now).total_seconds()
            metrics["remaining_seconds"] = MetricValue(
                value=max(0.0, round(remaining, 1)), unit="s"
            )
        if self._cycle_duration_s is not None:
            details["cycle_duration_s"] = self._cycle_duration_s
        if self._cycle_speed_level is not None:
            details["cycle_speed_level"] = self._cycle_speed_level
        if self._cycle_target_c is not None:
            details["cycle_target_c"] = self._cycle_target_c

        details["temperature_tolerance_c"] = self._temp_tolerance_c

        # Components
        motor_state: str
        if self._busy:
            motor_state = "running"
        elif idle is False and speed and speed > 0:
            motor_state = "running"
        else:
            motor_state = "idle"

        components: dict[str, ComponentStatus] = {
            "motor": ComponentStatus(connected=True, state=motor_state),
            "heater": ComponentStatus(
                connected=True,
                state=_heater_state(actual_temp, target_temp, self._temp_tolerance_c),
            ),
        }

        # equipment_status
        if self.dry_run:
            state: str = "dry_run"
            details["dry_run"] = True
            if self._busy:
                message: str | None = (
                    f"[dry-run] shaking at level {self._cycle_speed_level} "
                    f"toward {self._cycle_target_c} C"
                )
            else:
                message = "Dry-run mode - no hardware connected"
        elif self._busy:
            state = "busy"
            message = (
                f"Shaking at level {self._cycle_speed_level} "
                f"toward {self._cycle_target_c} C"
            )
        elif self._last_error is not None and (
            (now - self._last_error.timestamp).total_seconds()
            < _RECENT_ERROR_WINDOW_S
        ):
            state = "error"
            message = self._last_error.message
        elif readback_errors:
            state = "degraded"
            message = "; ".join(readback_errors)
        else:
            state = "ready"
            message = "Idle, ready to shake"

        # In dry_run mode, advertise the busy set while a cycle is
        # active so an operator UI doesn't show "shake.start" against
        # an already-running simulated cycle.
        if state == "dry_run" and self._busy:
            allowed = list(_ALLOWED_ACTIONS_BY_STATE["busy"])
        else:
            allowed = list(_ALLOWED_ACTIONS_BY_STATE.get(state, []))

        return EquipmentStatus(
            protocol_version=PROTOCOL_VERSION,
            equipment_id=self.equipment_id,
            equipment_name=self.equipment_name,
            equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
            equipment_version=self.equipment_version,
            host=host,
            equipment_status=state,  # type: ignore[arg-type]
            message=message,
            allowed_actions=allowed,
            device_time=now,
            uptime_seconds=uptime,
            components=components,
            metrics=metrics,
            last_error=self._last_error,
            details=details,
        )

    # ---- helpers -----------------------------------------------------------

    def clear_last_error_on_success(self) -> None:
        """Drop ``self._last_error`` after a 2xx operational response.

        Policy mirrors STATUS_SPEC §6.4 (also see agilent_plateloc):
        clear on first 2xx from any operational ``/control/*`` endpoint;
        do not clear from ``/control/{claim,heartbeat,release}`` or
        from reads.
        """
        self._last_error = None

    def _record_error(self, exc: Exception, method_name: str) -> None:
        message = str(exc)
        code = _classify_error(method_name, exc)
        if code not in LAST_ERROR_CODES:
            code = "serial_other"
        self._last_error = ErrorInfo(
            code=code,
            message=message,
            severity="error",
            timestamp=datetime.now(timezone.utc),
        )
        logger.exception("Shaker error in %s (code=%s)", method_name, code)

    async def _do(self, name: str, fn: Callable[[Any], Any]) -> None:
        async with self._lock:
            if self._driver is None:
                raise RuntimeError(
                    "Shaker is not connected. POST /control/startup first."
                )
            try:
                await asyncio.to_thread(fn, self._driver)
            except Exception as exc:
                self._record_error(exc, name)
                raise


def _heater_state(
    actual: float | None, setpoint: float | None, tolerance: float
) -> str:
    if actual is None or setpoint is None:
        return "unknown"
    delta = actual - setpoint
    if abs(delta) <= tolerance:
        return "stable"
    return "heating" if delta < 0 else "cooling"


__all__ = [
    "LAST_ERROR_CODES",
    "ShakerService",
    "TemperatureNotReady",
]
