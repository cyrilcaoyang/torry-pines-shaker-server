"""Configuration loader for the Torrey Pines shaker REST service.

Reads ``config.toml`` from the project root (next to ``pyproject.toml``)
and exposes the values via :func:`get`. Falls back to built-in defaults
when the file is missing so the service can run in dry-run on a fresh
checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


def _find_config_file() -> Path | None:
    here = Path(__file__).resolve().parent
    for parent in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = parent / "config.toml"
        if candidate.is_file():
            return candidate
    cwd = Path.cwd() / "config.toml"
    if cwd.is_file():
        return cwd
    return None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if tomllib is None:
        raise RuntimeError(
            "No TOML parser available. "
            "Install tomli (`pip install tomli`) or use Python >= 3.11."
        )
    if path is None:
        found = _find_config_file()
        if found is None:
            raise FileNotFoundError(
                "config.toml not found. "
                "Place it next to pyproject.toml or pass an explicit path."
            )
        path = found
    path = Path(path)
    with open(path, "rb") as f:
        return tomllib.load(f)


_DEFAULTS: dict[str, Any] = {
    "instrument": {
        "com_port": "COM3",
        "baudrate": 9600,
        "timeout": 1.0,
        "max_temp_c": 110,
        "min_temp_c": -20,
    },
    "service": {
        "host": "0.0.0.0",
        "port": 8030,
        "dry_run": False,
        "cors_origins": ["*"],
        "startup_connect_timeout_s": 10.0,
        "enforce_claims": True,
        # When True (default), the watchdog stops the motor on process
        # shutdown. Strongly recommended in production; the recipe's
        # "device owns the duration" contract relies on it.
        "watchdog_stop_on_exit": True,
        # Tolerance (in C) inside which ``actual_temperature`` is
        # considered to have reached ``setpoint_temperature``. Used by
        # the ``wait_for_temperature`` precondition.
        "temperature_tolerance_c": 1.0,
        # Upper bound on how long ``/control/shake/start`` waits for
        # temperature before refusing with HTTP 412. The hardware ramp
        # is slow; an operator overriding this should expect to wait.
        "wait_for_temperature_timeout_s": 1800.0,
    },
    "dashboard": {
        "equipment_id": "torry_pines_shaker",
        "equipment_name": "Torrey Pines Shaker",
        "equipment_version": None,
    },
}

try:
    _cfg = load_config()
except (FileNotFoundError, RuntimeError):
    _cfg = _DEFAULTS


def get(section: str, key: str, default: Any = None) -> Any:
    return _cfg.get(section, {}).get(
        key, _DEFAULTS.get(section, {}).get(key, default)
    )
