# Torrey Pines shaker — API reference

Base: the service's `http://<host>:8030`. All timestamps UTC ISO-8601.
Request/response schemas: `/openapi.json`. Tags: `spec`, `claim`, `control`,
`documentation`.

## Read (always available, side-effect-free)

| route | returns |
|---|---|
| `GET /` | `ProbeResponse` — `equipment_id`, `equipment_name`, `protocol_version` (`"1.2"`) |
| `GET /health` | `{"status": "healthy"}` |
| `GET /status` | full `EquipmentStatus` envelope: `equipment_status`, `activity`, `activity_since`, `components.motor` / `components.heater`, `metrics.speed_level` / `metrics.cycles_total` (+ temperatures when the heater readback works), `allowed_actions`, `required_actions`, `last_error`, `details.claimed_by`, `details.com_port`, `details.temperature_tolerance_c` |
| `GET /openapi.json`, `GET /docs` | OpenAPI document, Swagger UI |
| `GET /agent-docs`, `GET /agent-docs/api-reference`, `GET /llms.txt` | this documentation (`text/markdown`, `text/plain`) |

## Claim protocol

| route | body / header | responses |
|---|---|---|
| `POST /control/claim` | `{owner, session_id, ttl_s?}` | 200 `{claim_token, heartbeat_interval_s, expires_at}`; 409 `{detail, claimed_by, retry_after_s}` when held by another session; idempotent for the same `session_id` |
| `POST /control/heartbeat` | header `X-Claim-Token` | 200 with new `expires_at`; 401 unknown/expired |
| `POST /control/release` | header `X-Claim-Token` | 204, idempotent |

## Control (header `X-Claim-Token` required → 423 without it)

| route | body | notes |
|---|---|---|
| `POST /control/startup` | — | opens the serial port, verifies the device; 503 with `detail` on failure |
| `POST /control/shutdown` | — | stops the motor, closes the port; device stays `requires_init` until `startup` (not auto-reconnected); logged with the claim owner |
| `POST /control/shake/set_temperature` | `{temperature_c: float}` −20..+110 | 412 `blocked_subsystem: "heater"` while heater readbacks fail |
| `POST /control/shake/set_speed` | `{speed_level: int}` 1..9 | 412 `blocked_subsystem: "motor"` while the speed readback fails; use `shake/stop` to halt |
| `POST /control/shake/start` | `{speed_level, temperature_c, duration_s, wait_for_temperature?}` | server-timed cycle; 412 per the precondition table; 409 if a cycle is already running or the driver is not connected |
| `POST /control/shake/stop` | — | idempotent; cancels the watchdog, speed → 0 |

All control responses are `CommandResponse` `{message}` on 2xx. Refusals:
**423** no/invalid claim · **409** state conflict · **412** precondition (body
shape identifies which) · **503** startup failure. 412 never touches
`last_error`; a 2xx from an operational action clears it.

## Skill names (what `allowed_actions` lists)

`startup`, `shutdown`, `shake.start`, `shake.stop`, `shake.set_speed`,
`shake.set_temperature` — the same names the `lab-skills` catalog uses for
`kind: shaker`.
