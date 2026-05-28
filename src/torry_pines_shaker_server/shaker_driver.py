"""Adapter around the ``matterlab_shakers.TorreyPinesShaker`` driver.

The vendor driver is synchronous, serial-port-based, and uses
property setters for ``temp``, ``speed``, ``idle``. This module hides
those quirks behind a small façade::

    drv = open_real_driver(com_port="COM3", max_temp=110, min_temp=-20)
    drv.set_speed(5)
    drv.set_temperature(25)
    drv.set_idle(False)             # engage motor
    actual_c = drv.get_actual_temperature()
    drv.set_speed(0); drv.set_idle(True)
    drv.close()

A drop-in :class:`StubShaker` mirrors the same surface so the FastAPI
service can run on macOS / Linux / CI without serial hardware.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _ShakerDriverProtocol(Protocol):
    """Surface the service depends on."""

    com_port: str

    def get_actual_temperature(self) -> float: ...
    def get_target_temperature(self) -> float | None: ...
    def get_speed(self) -> int: ...
    def get_idle(self) -> bool: ...
    def get_device_model(self) -> str: ...
    def get_serial_number(self) -> str: ...

    def set_temperature(self, temp_c: float) -> None: ...
    def set_speed(self, level: int) -> None: ...
    def set_idle(self, idle: bool) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Real driver façade
# ---------------------------------------------------------------------------


class TorreyPinesShakerFacade:
    """Adapter that wraps :class:`matterlab_shakers.TorreyPinesShaker`.

    The vendor driver uses ``@open_close`` on every transaction, so the
    serial port is opened and closed for each command. There is no
    persistent connection to tear down; :meth:`close` is therefore a
    best-effort no-op against the underlying serial object.
    """

    def __init__(
        self,
        com_port: str,
        *,
        max_temp_c: int = 110,
        min_temp_c: int = -20,
        baudrate: int = 9600,
        timeout: float = 1.0,
    ) -> None:
        # Imported lazily so a dry-run service does not require pyserial /
        # the vendor package to be importable on macOS / CI.
        from matterlab_shakers.torrey_pines_shakers import TorreyPinesShaker

        self.com_port = com_port
        self._shaker = TorreyPinesShaker(
            com_port=com_port,
            max_temp=max_temp_c,
            min_temp=min_temp_c,
            baudrate=baudrate,
            timeout=timeout,
        )
        # Cache identity reads; they're stable for the lifetime of the
        # process and the vendor driver makes a serial round-trip every
        # call.
        self._model: str | None = None
        self._serial: str | None = None

    # ---- reads --------------------------------------------------------------

    def get_actual_temperature(self) -> float:
        return float(self._shaker.temp)

    def get_target_temperature(self) -> float | None:
        return self._shaker.target_temp  # may be None when idle

    def get_speed(self) -> int:
        return int(self._shaker.speed)

    def get_idle(self) -> bool:
        return bool(self._shaker.idle)

    def get_device_model(self) -> str:
        if self._model is None:
            self._model = str(self._shaker.device_model)
        return self._model

    def get_serial_number(self) -> str:
        if self._serial is None:
            self._serial = str(self._shaker.serial_number)
        return self._serial

    # ---- writes -------------------------------------------------------------

    def set_temperature(self, temp_c: float) -> None:
        # The vendor driver's setter is typed as int, but the device
        # accepts integer-degree setpoints. Round defensively.
        self._shaker.temp = int(round(float(temp_c)))

    def set_speed(self, level: int) -> None:
        self._shaker.speed = int(level)

    def set_idle(self, idle: bool) -> None:
        self._shaker.idle = bool(idle)

    def close(self) -> None:
        # `@open_close` already closes after every transaction. If the
        # underlying SerialDevice happens to hold an open handle (e.g.
        # the operator was mid-query during teardown) we close it
        # best-effort here.
        try:
            ser = getattr(self._shaker, "ser", None)
            if ser is not None and getattr(ser, "is_open", False):
                ser.close()
        except Exception:
            logger.debug("close: best-effort serial close failed", exc_info=True)


# ---------------------------------------------------------------------------
# Stub driver
# ---------------------------------------------------------------------------


class StubShaker:
    """In-memory mock for dry-run / CI.

    Implements the same surface as :class:`TorreyPinesShakerFacade` but
    services every read from memory. The "temperature" tracks the
    setpoint instantly (no ramp), which is fine for tests; the
    ``wait_for_temperature`` precondition is exercised in test code by
    setting a setpoint mid-flight via direct attribute access if needed.
    """

    def __init__(self) -> None:
        self.com_port = "DRY-RUN"
        self._target_c: float | None = None
        self._actual_c: float = 22.0  # ambient
        self._speed: int = 0
        self._idle: bool = True

    # reads
    def get_actual_temperature(self) -> float:
        return self._actual_c

    def get_target_temperature(self) -> float | None:
        return self._target_c

    def get_speed(self) -> int:
        return self._speed

    def get_idle(self) -> bool:
        return self._idle

    def get_device_model(self) -> str:
        return "DRY-RUN-SC20"

    def get_serial_number(self) -> str:
        return "DRY-RUN-0001"

    # writes
    def set_temperature(self, temp_c: float) -> None:
        self._target_c = float(temp_c)
        self._actual_c = float(temp_c)  # instantaneous ramp in dry-run

    def set_speed(self, level: int) -> None:
        if level < 0 or level > 9:
            raise ValueError("Orbital speed setting must be 0-9 level")
        self._speed = int(level)

    def set_idle(self, idle: bool) -> None:
        self._idle = bool(idle)

    def close(self) -> None:
        return None


__all__ = [
    "StubShaker",
    "TorreyPinesShakerFacade",
    "_ShakerDriverProtocol",
]
