"""CLI entry point for the Torrey Pines shaker REST API.

Run either of::

    python -m torry_pines_shaker_server
    torry-pines-shaker-serve

Bind address and port are read from ``config.toml``::

    [service]
    host = "0.0.0.0"
    port = 8030
    dry_run = false

Pass ``--dry-run`` to force dry-run mode regardless of config.
"""

from __future__ import annotations

import argparse
import logging

from . import config as _config
from .api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="torry-pines-shaker-serve",
        description="Run the Torrey Pines shaker REST API (lab status spec v1.1).",
    )
    parser.add_argument("--host", default=None, help="Override bind host")
    parser.add_argument("--port", type=int, default=None, help="Override port")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode (no hardware) regardless of config.toml",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    host = args.host or _config.get("service", "host", "0.0.0.0")
    port = args.port or int(_config.get("service", "port", 8030))
    dry_run = True if args.dry_run else None

    import uvicorn

    app = create_app(dry_run=dry_run)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
