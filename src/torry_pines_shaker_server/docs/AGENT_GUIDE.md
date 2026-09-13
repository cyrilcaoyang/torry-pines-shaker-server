# Torrey Pines SC25XR shaker — agent guide

This service fronts one Torrey Pines Scientific orbital shaker with heater
(SC20/SC25 series) over a serial link and speaks the AC Organic lab's
STATUS_SPEC **v1.2** (`equipment_kind: "shaker"`). Read this before driving
it; the [API reference](/agent-docs/api-reference) lists every route, and
`/openapi.json` carries the exact request and response schemas.

## What "primary operation" means here

The orbital motor turning. `activity` is observed from the motor's speed
readback (`components.motor.state` in `{idle, shaking}`), never derived from
the health word. `metrics.cycles_total` counts completed shake cycles since the
service started; a default 30 s cycle can start and finish between two
dashboard polls, so use that counter — not sampled `activity` — for usage.

## Health vs. activity (STATUS_SPEC §2.2 / §2.3)

`equipment_status` answers "is it fit for a run"; `activity` answers "is it
running". Both are reported independently:

| `equipment_status` | meaning here |
|---|---|
| `ready` | connected, idle, both readbacks healthy |
| `busy` | connected, a shake cycle in progress, readbacks healthy |
| `degraded` | connected, but a readback fails — typically the heater RTD calibration fault (`last_error.code: calibration_error`). **Shaking is still allowed**; temperature control is withheld. `required_actions` says what a human must do at the instrument. |
| `error` | an operational `/control/*` action failed in the last 60 s |
| `requires_init` | the service is up but **not connected to the shaker** — see *Startup and shutdown* |
| `dry_run` | simulation; no hardware |

## Claims (STATUS_SPEC §5)

Every `/control/*` request needs a valid `X-Claim-Token` (`enforce_claims`
defaults to true). Acquire with `POST /control/claim` (`{owner, session_id,
ttl_s}`), heartbeat at the returned interval, release when done. Without a
valid token, control calls return **423**; a claim held by someone else
returns **409/423** with `claimed_by`. `details.claimed_by` on `/status` shows
the current holder. The `lab_skills.ClaimManager` does all of this for you.

## Startup and shutdown — read this twice

- `POST /control/startup` opens the serial port and verifies the device.
- `POST /control/shutdown` stops the motor and **closes the port**. The
  service then reports `requires_init` and will **not** reconnect on its own:
  a shutdown is treated as a deliberate operator decision, and the boot-time
  auto-connect retry deliberately does not fight it. The device stays
  disconnected until someone POSTs `/control/startup` — on 2026-09-04 an
  agent session ended this way and the shaker sat unusable for eight days.
- Therefore: **do not end a session with the device shut down** unless you
  were asked to. Release your claim and leave the device connected. Reserve
  `shutdown` for servicing, and say so in your run notes.
- Each `startup`/`shutdown` is logged with the claim owner.

## Preconditions (STATUS_SPEC §6)

Availability is decided per subsystem by one helper that feeds both
`/status.allowed_actions` and the `/control/*` **412** gates, so the two never
disagree. Read `allowed_actions` first; if an action is missing, a POST would
412 with a body you can branch on by shape:

| precondition | withholds | 412 body |
|---|---|---|
| motor speed readback failing | `shake.start`, `shake.set_speed` | `{detail, blocked_subsystem: "motor", readback_errors, retry_after_s}` |
| heater readbacks failing | `shake.set_temperature`; `shake.start` only with `wait_for_temperature=true` | `{detail, blocked_subsystem: "heater", readback_errors, retry_after_s}` |
| recent operational error (60 s window) | everything except `shutdown` | `{detail, blocked_subsystem: "service", last_error_code, retry_after_s}` |
| setpoint not reached in time | `shake.start` with `wait_for_temperature=true` | `{detail, actual_c, setpoint_c, tolerance_c, retry_after_s}` |

While `activity == "running"`, start/set actions are withheld and `shake.stop`
stays reachable. Coarse state conflicts — driver not connected, a cycle
already running — are **409**, not 412.

## `last_error.code` (branch on the code, never the message)

`serial_init_failed`, `serial_timeout`, `serial_other` (severity `error`,
operational, cleared by the next successful action); `rtd_disconnected`,
`rtd_shorted`, `calibration_error` (severity `warning`, readback faults that
clear themselves when the readback succeeds and drive `degraded`, not
`error`); `process_internal` (a bug in this service).

## The shake cycle

`POST /control/shake/start` `{speed_level 1..9, temperature_c -20..110,
duration_s, wait_for_temperature?}` — the **server** owns the timer and stops
the motor after `duration_s` (watchdog); `POST /control/shake/stop` cancels it
and drops speed to 0 (idempotent). A hard kill of the process bypasses the
watchdog and the motor keeps its last setpoint — there is no hardware
watchdog — so never `taskkill /F` this service while a cycle runs.

## Discovery

`/llms.txt` (this index), `/agent-docs` (this guide),
`/agent-docs/api-reference`, `/openapi.json`, `/docs` (Swagger UI).
