# Sous Vide Assistant — HTTP API

Everything the touchscreen UI can do is available over HTTP, so scripts, cron jobs, Home Assistant, shell one-liners, etc. can read telemetry, start and control cooks, and edit settings.

There are two layers:

| Layer | Base path | Use it for |
|---|---|---|
| **Scripting API v1** (this document's main part) | `/api/v1/` | Scripts and integrations. Documented, validated, optional token auth, live stream. |
| **Legacy UI endpoints** (unchanged) | `/api/…` | What `index.html` talks to. Documented at the bottom for completeness. |

Both layers drive the same in-memory cook session, so the UI and your scripts always agree. If a script starts a cook, the touchscreen shows it; if someone presses *Pause* on the screen, `/api/v1/status` reports `paused: true`.

- [Quick start](#quick-start)
- [Conventions](#conventions)
- [Authentication](#authentication)
- [The cook lifecycle](#the-cook-lifecycle)
- [Telemetry endpoints](#telemetry-endpoints) — `status`, `stream`, `events`, `history`
- [Cook control endpoints](#cook-control-endpoints) — `POST /cook`, actions
- [Settings endpoints](#settings-endpoints) — `settings`, `presets`, `pots`
- [Simulator endpoints](#simulator-endpoints)
- [Object reference](#object-reference)
- [Recipes](#recipes)
- [Legacy UI endpoints](#legacy-ui-endpoints)
- [Behavior notes and caveats](#behavior-notes-and-caveats)

---

## Quick start

```bash
# server (real cooker)
python3 app.py --host 0.0.0.0 --api-token "$(openssl rand -hex 16)"
# or, with no hardware, at 60x speed:
python3 app.py --sim --speed 60 --api-token test

export SV=http://127.0.0.1:8080/api/v1
export AUTH="Authorization: Bearer test"

curl -s -H "$AUTH" $SV/status                      # telemetry snapshot

curl -s -H "$AUTH" -H 'Content-Type: application/json' -X POST $SV/cook -d '{
  "preset": "Chicken Breast", "pot": "small",
  "confirm_water": true, "auto_start": true
}'                                                   # start a full cook, hands-free

curl -s -H "$AUTH" -X POST $SV/cook/actions/food_in  # tell it the food is in the bath
curl -N -H "$AUTH" $SV/stream                        # live telemetry + chime events
```

A ready-made command-line client using only the Python standard library is in [`examples/svctl.py`](../examples/svctl.py).

The same API spec in machine-readable form is in [`docs/openapi.yaml`](openapi.yaml) and is also served by the app at `GET /api/v1/openapi.yaml`.

---

## Conventions

- **Format:** requests and responses are JSON (`Content-Type: application/json`), UTF-8. Responses carry `Cache-Control: no-store`.
- **Units:** temperatures are **°C** everywhere in requests, storage and responses. Read-only convenience fields ending in `_f` (`water_temp_f`, `target_f`) give °F. Durations are **seconds** (`*_s`), except preset/manual cook times, which are **minutes** (`time_min`, `frozen_extra_min`), as in the UI. Volumes are litres.
- **Timestamps:** ISO-8601 UTC with milliseconds, e.g. `2026-11-02T04:35:49.002Z`.
- **Errors:** every error has the form `{"error": "human readable message"}` with one of these HTTP statuses:

| Status | Meaning |
|---|---|
| `400` | Request rejected by the cook engine's own validation (e.g. manual temperature out of range), or malformed JSON/query. |
| `401` | Missing or wrong token (only when the server was started with `--api-token`). |
| `404` | Unknown route/preset/pot/action, or no cook in progress for `/history`. |
| `409` | Request is valid but not allowed right now (a cook is already running; action not available in the current phase). |
| `422` | Request was understood but a field is missing, unknown, or the wrong type. |
| `502` | The Bluetooth command to the cooker failed (`start`, `pause`, `resume`). The session state is unchanged. |
| `500` | Unexpected server error (see the server log). |

- **Idempotency:** `GET`s are safe to repeat. Actions are *not* idempotent: repeating `pause` returns `409` because the cook is already paused. Use `available_actions` in the status to know what is legal.
- **CORS:** no CORS headers are sent; the API is meant for scripts, not browser pages on other origins.

---

## Authentication

Start the server with a token:

```bash
python3 app.py --api-token 'your-long-random-string'
# or
SOUSVIDE_API_TOKEN='your-long-random-string' python3 app.py
```

Every `/api/v1/*` request except `GET /api/v1/health` must then send one of:

```
Authorization: Bearer your-long-random-string
X-API-Key: your-long-random-string
```

Without `--api-token`, `/api/v1/*` is open — exactly like the existing UI. The default bind address is still `127.0.0.1`, so nothing is reachable from the network until you pass `--host 0.0.0.0`.

> **Important:** the token only protects `/api/v1/*`. The legacy UI endpoints (`/api/state`, `/api/settings`, `/api/session/*`, `/api/sim/*`) and the UI page are unchanged and stay open. If you expose the server beyond your own machine, put it on a trusted LAN/VPN or behind a reverse proxy, because anyone who can reach the legacy endpoints can also start a cook. This API can switch on a 750 W heater.

Tokens are compared in constant time. Browser `EventSource` cannot send headers, so use `curl -N`, `fetch()` with a stream reader, or a Python/Node SSE client for `/stream`.

---

## The cook lifecycle

Both the UI and the API move a single session through these phases (`cook.phase`):

```
        POST /cook                water_ok            (cooker found, setpoint written)
 idle ───────────────▶ water ───────────▶ connect ─────────────────▶ armed
                         │                   │                         │
                         └──── cancel ───────┴──────────── cancel ─────┤
                                                                       │ start  |  cooker_started
                                                                       ▼
 dismiss      stop                         food_in / dip detected   timer ends
 ◀──── ended ◀──── (any live phase) ◀── preheat ──▶ ready ──────────▶ cook ─────────▶ done (overtime)
```

| Phase | Meaning | `available_actions` |
|---|---|---|
| `water` | Session created; waiting for you to put the stated litres in the pot. | `water_ok`, `cancel` |
| `connect` | Scanning for / connecting to the cooker over Bluetooth, writing the target temperature. | `cancel` |
| `armed` | Connected, setpoint written, waiting to be started. | `start`, `cooker_started`, `cancel` |
| `preheat` | Heating the bath; `cook.preheat.eta_s` counts down. | `pause`/`resume`, `stop` |
| `ready` | Bath is at temperature (a "ready" chime event repeats at `ready_reminder_s`). The cook timer starts when the bath temperature dips by `dip_threshold_c` (food added) or you call `food_in`. | `pause`/`resume`, `food_in`, `stop` |
| `cook` | Timer running; `remaining_s` counts down. | `pause`/`resume`, `stop` |
| `done` | Timer finished (`complete` event). `overtime_s` counts up. **The cooker keeps holding temperature**; the app never stops it by itself. | `pause`/`resume`, `stop` |
| `ended` | Session over (`end_reason` says why). | `dismiss` |

Notes:

- No Bluetooth traffic exists while no cook is active. The connection is only opened for phases `connect` … `done`.
- If the cooker disappears during `done` for longer than `unplug_end_s`, the session ends by itself with reason *"Cooker disconnected (unplugged?)"*.
- A pause detected from the cooker's motor (someone pressed the button on the cooker) shows `paused: true` with `pause_source: "cooker"`; a pause from the UI or API shows `"ui"`.

### Actions

| Action | Allowed in | Effect |
|---|---|---|
| `water_ok` | `water` | Confirms the pot is filled; moves to `connect`. |
| `start` | `armed` | **Sends *start* to the cooker** over Bluetooth and begins preheat. |
| `cooker_started` | `armed` | You pressed start on the cooker yourself; begins preheat without sending a command. |
| `pause` | live phases, not paused | **Sends *stop* to the cooker** and pauses the timers. |
| `resume` | live phases, paused | **Sends *start* to the cooker** and resumes. |
| `food_in` | `ready` | Starts the cook timer now, instead of waiting for the temperature dip. |
| `stop` | live phases | Sends *stop* to the cooker (if connected), ends the session (`ended`, reason "Stopped"). |
| `cancel` | `water`, `connect`, `armed` | Discards the session. Nothing was heating yet. |
| `dismiss` | `ended` | Clears the finished session so a new cook can start. |

---

## Telemetry endpoints

### `GET /api/v1/health`

Liveness check. Never requires a token.

```json
{"ok": true, "api_version": "1.0", "simulator": false, "auth_required": true}
```

### `GET /api/v1/status`

The one call a script usually needs: connection, current water temperature, and (if a cook exists) the whole cook state. Cheap to poll once per second.

Example (during preheat, from the simulator):

```json
{
  "api_version": "1.0",
  "timestamp": "2026-11-02T04:35:49.002Z",
  "simulator": true,
  "connection": { "state": "connected", "connected": true, "message": null },
  "cooker": {
    "water_temp_c": 23.97,
    "water_temp_f": 75.14,
    "water_low": false,
    "motor_speed": 1800,
    "telemetry_age_s": 0.0
  },
  "last_event_id": 0,
  "cook": {
    "phase": "preheat",
    "paused": false,
    "pause_source": null,
    "name": "Steak (Med-Rare)",
    "emoji": "🥩",
    "manual": false,
    "frozen": false,
    "pot": { "label": "8 qt", "range": "1–2 lb", "id": "small" },
    "liters": 5.0,
    "target_c": 54.5,
    "target_f": 130.1,
    "total_s": 5400,
    "base_s": 5400,
    "extra_s": 0,
    "elapsed_s": 0.0,
    "remaining_s": 5400.0,
    "overtime_s": 0.0,
    "cooker_link_lost": false,
    "end_reason": null,
    "session_s": 1090.7,
    "auto_start_pending": false,
    "available_actions": ["pause", "stop"],
    "preheat": { "start_temp_c": 22.84, "eta_s": 1001, "eta_source": "model", "progress": 0.035, "duration_s": 37.9 },
    "dip": null
  },
  "sim_speed": 300.0
}
```

When idle: `"cook": null`, and `cooker.water_temp_c` is `null` (the app is not connected to the cooker, so it has no reading).

Field details are in the [object reference](#object-reference).

### `GET /api/v1/cook`

Same `cook` object as in `/status`, on its own: `{"cook": {…}}` or `{"cook": null}`.

### `GET /api/v1/stream`  (Server-Sent Events)

A live feed. Query parameter `interval` (seconds, default `1`, clamped to `0.25 … 30`).

Two event types:

- `status` — data is exactly the `/status` object, sent every `interval` seconds, starting immediately.
- `cook_event` — data is `{"id": 2, "type": "cookstart"}`; the SSE `id:` field equals the event id. Only events created *after* you connected are sent. These are the same events that make the touchscreen play its chimes (see [Events](#events)).

```
retry: 3000

event: status
data: {"api_version":"1.0","timestamp":"2026-11-02T04:35:54.137Z", … "cook":{"phase":"cook", …}}

id: 3
event: cook_event
data: {"id":3,"type":"complete"}
```

```bash
curl -N -H "$AUTH" "$SV/stream?interval=2"
```

### `GET /api/v1/events`

Polling alternative to the stream. Returns the app's ring buffer of the last 30 events.

Query: `after` (integer event id, default `0`) — only events with a higher id are returned.

```json
{ "events": [ {"id": 1, "type": "ready"}, {"id": 2, "type": "cookstart"} ], "last_event_id": 2 }
```

Reading events does not consume them; the UI is unaffected.

#### Events

| `type` | Raised when |
|---|---|
| `ready` | Bath reached target (and repeated every `ready_reminder_s` while waiting for food). |
| `cookstart` | The cook timer started (dip detected or `food_in`). |
| `complete` | The cook timer finished; overtime begins. |
| `lowwater` | The cooker's low-water sensor is set (repeats at most every 8 s). |
| `lost` | The Bluetooth link has been down for 30 s during a live phase (repeats at most once a minute). |

### `GET /api/v1/history`

Temperature log of the **current** session (sampled at most every 1.5 s) plus phase markers. Returns `404` when no cook exists; the log is discarded on `dismiss` — save it before then (see the [logging recipe](#log-a-cook-to-csv)).

Query parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `since` | `-1` | Only samples with `t_s` greater than this (seconds since the session was created). Use the previous response's `next_since` for incremental reads. |
| `max_points` | `0` (all) | Down-sample to about this many points (min 2). |
| `format` | `json` | `json` or `csv`. |

JSON:

```json
{
  "session_started_at": "2026-11-02T04:34:34.802Z",
  "name": "Steak (Med-Rare)",
  "phase": "cook",
  "columns": ["t_s", "temp_c"],
  "samples": [[947.1, 23.463], [992.3, 23.434]],
  "markers": [
    {"t_s": 927.5, "kind": "armed"}, {"t_s": 1052.2, "kind": "start"},
    {"t_s": 2079.1, "kind": "ready"}, {"t_s": 2305.2, "kind": "food"},
    {"t_s": 2531.8, "kind": "recovered"}
  ],
  "next_since": 2985.5
}
```

Marker kinds: `armed`, `start` (preheat began), `ready`, `food` (cook timer start; back-dated to when the bath began to fall), `recovered` (bath back at target after the food dip), `done`, `end`.

CSV (`?format=csv`): header `t_s,timestamp,temp_c`, one row per sample.

---

## Cook control endpoints

### `POST /api/v1/cook` — start a cook

Creates the session. Only one cook can exist at a time (`409` otherwise; `dismiss` or `cancel` the old one first).

Body — give **exactly one** of `preset` or `manual`:

| Field | Type | Required | Description |
|---|---|---|---|
| `pot` | string | yes | Pot id (`small`, `medium`, `large`) or its label as currently configured (e.g. `"8 qt"`; case-insensitive). Sets the default water volume. |
| `preset` | string | one of | A preset id (`f1`…`f12`) or its exact name (case-insensitive, e.g. `"Pork Chops"`). Empty slots are rejected. |
| `frozen` | bool | no | With `preset`: add the preset's `frozen_extra_min` to the cook time. Default `false`. |
| `manual` | object | one of | `{"temp_c": 63, "time_min": 45, "liters": 6.5}`. `temp_c` 20–92, `time_min` 1–4320 (72 h). `liters` optional (0.5–30; defaults to the pot's volume). |
| `confirm_water` | bool | no | Also performs `water_ok`: you assert the pot already holds the right amount of water. Default `false`. |
| `auto_start` | bool | no | After the cooker is found and armed, automatically perform `start`. Requires `confirm_water: true`. Default `false`. |

Water volume for a preset is the preset's per-pot override (`liters.<pot>`) if set, otherwise the pot's default. This is the number the UI tells you to dispense, and it drives the preheat ETA model.

Response `201`:

```json
{ "ok": true, "status": { …same as GET /status… } }
```

Examples:

```bash
# Interactive-style: create, then confirm and start yourself later
curl -H "$AUTH" -H 'Content-Type: application/json' -X POST $SV/cook \
     -d '{"preset":"f1","pot":"small"}'

# Manual 63 °C / 45 min soft egg in the 12 qt pot, water already in
curl -H "$AUTH" -H 'Content-Type: application/json' -X POST $SV/cook \
     -d '{"manual":{"temp_c":63,"time_min":45},"pot":"medium","confirm_water":true}'

# Fully hands-free frozen steak
curl -H "$AUTH" -H 'Content-Type: application/json' -X POST $SV/cook \
     -d '{"preset":"Steak (Med-Rare)","pot":"8 qt","frozen":true,"confirm_water":true,"auto_start":true}'
```

> **Safety:** `confirm_water` and `auto_start` are *your* statement that the pot is filled and the cooker is clamped in and plugged in. With `auto_start` nothing asks a person before the heater turns on. The cooker's low-water sensor still raises `lowwater` events, but the app cannot prevent dry running. `auto_start` waits up to 15 minutes for the cooker to be found; if it fails to send *start*, the session stays `armed` and you (or the touchscreen) can still start it. The status shows `auto_start_pending: true` while it is waiting.

Errors: `409` cook already in progress; `422` bad/missing fields; `400` value out of range (manual).

### `POST /api/v1/cook/actions/{action}`

Performs one of the [actions](#actions). No body. Returns `200`:

```json
{ "ok": true, "action": "food_in", "status": { …same as GET /status… } }
```

`409` if the action is not allowed in the current phase (check `cook.available_actions`) or no cook exists; `404` for an unknown action name; `502` if the cooker did not accept `start`/`pause`/`resume`.

`start` waits for the cooker's reply, so it takes a moment. `water_ok` returns immediately; the connection then happens in the background — poll `/status` until `phase` is `armed` before calling `start` (or use `auto_start`).

---

## Settings endpoints

Settings are the same data the UI's settings page edits, stored in `data/settings.json`. Changes take effect immediately and persist across restarts.

> Changing presets, pots, or the physical constants affects **future** cooks. A cook already in progress keeps the temperature, time and volume it started with. The `volume` and detection/chime thresholds (`ready_*`, `dip_*`, `unplug_*`, `detect_pause_from_motor`) are read live.

### `GET /api/v1/settings`

Returns the full settings object:

```json
{
  "pots": [ {"id": "small", "label": "8 qt", "range": "1–2 lb", "liters": 5.0}, … ],
  "foods": [
    { "id": "f1", "name": "Chicken Breast", "emoji": "🐔", "temp_c": 65.0,
      "time_min": 90, "frozen_extra_min": 45,
      "liters": {"small": null, "medium": null, "large": null} },
    …
  ],
  "power_w": 750, "efficiency": 0.85,
  "dip_threshold_c": 1.0, "ready_tol_c": 0.2, "ready_reminder_s": 60,
  "unplug_end_s": 120, "detect_pause_from_motor": true, "volume": 0.7
}
```

### `PATCH /api/v1/settings` — partial update (recommended)

Send only what you want to change. Anything you leave out is untouched.

- Top-level scalars: set directly.
- `pots` and `foods`: lists of partial objects, each identified by `id`; only the fields you send change. For `foods[].liters`, only the pot keys you send change; send `null` to clear one.
- Unknown fields, unknown ids, or wrong types are rejected with `422` and nothing is saved.
- Valid values outside the allowed range are **clamped** (same as the UI); the response is the resulting settings so you can see what was stored.

```bash
curl -H "$AUTH" -H 'Content-Type: application/json' -X PATCH $SV/settings -d '{
  "volume": 0.4,
  "pots":  [ {"id": "medium", "liters": 8.5} ],
  "foods": [ {"id": "f3", "temp_c": 55.5, "time_min": 120, "liters": {"small": 4.5}} ]
}'
```

### `PUT /api/v1/settings` — full replace

Same semantics as the UI's save: the body is run through the sanitiser, and **any field you omit is reset to its factory default**. Use `PATCH` unless you are restoring a complete backup (`GET` → store → `PUT`).

### Presets

Twelve slots, `f1` … `f12`. A slot with an empty `name` is blank (and cannot be started).

| Request | Description |
|---|---|
| `GET /api/v1/presets` | `{"presets": [ … ]}` — every slot, each with `slot` (1–12) and `empty` added. |
| `GET /api/v1/presets/{id}` | One preset. |
| `PATCH /api/v1/presets/{id}` | Partial update of one preset; returns it. To blank a slot, set `"name": ""`. |

```bash
curl -H "$AUTH" -H 'Content-Type: application/json' -X PATCH $SV/presets/f11 \
     -d '{"name":"Lamb Chops","emoji":"🐑","temp_c":57,"time_min":120,"frozen_extra_min":60}'
```

### Pots

| Request | Description |
|---|---|
| `GET /api/v1/pots` | `{"pots": [ … ]}` |
| `PATCH /api/v1/pots/{id}` | `id` is `small`, `medium` or `large`. Fields: `label`, `range`, `liters`. Returns the pot. |

### Setting limits

Values are clamped into these ranges:

| Field | Range | Notes |
|---|---|---|
| `pots[].label` | ≤ 12 chars | |
| `pots[].range` | ≤ 16 chars | Display text only. |
| `pots[].liters` | 0.5 – 30 | |
| `foods[].name` | ≤ 24 chars | Empty = blank slot. |
| `foods[].emoji` | ≤ 8 chars | |
| `foods[].temp_c` | 20 – 92 | |
| `foods[].time_min` | 1 – 4320 | |
| `foods[].frozen_extra_min` | 0 – 4320 | |
| `foods[].liters.<pot>` | 0.5 – 30, or `null` | `null` / `0` = use the pot's default. |
| `power_w` | 100 – 3000 | Heater power used by the preheat ETA model. |
| `efficiency` | 0.3 – 1.0 | Fraction of power that heats the bath. |
| `dip_threshold_c` | 0.3 – 10 | Drop below target that means "food went in". |
| `ready_tol_c` | 0.05 – 2 | Within this of target counts as at temperature. |
| `ready_reminder_s` | 0 – 600 | Repeat the `ready` event; `0` = once. |
| `unplug_end_s` | 30 – 1800 | Overtime + no Bluetooth this long ⇒ session ends. |
| `detect_pause_from_motor` | bool | Treat the cooker's motor stopping as a pause. |
| `volume` | 0 – 1 | UI chime volume. |

---

## Simulator endpoints

### `POST /api/v1/sim/{name}`

Only meaningful when the server was started with `--sim`; otherwise `404`. `name` is one of:

| `name` | Effect |
|---|---|
| `food` | Drops a cold 1 kg item into the simulated bath (triggers the dip detection). |
| `lowwater` / `refill` | Sets / clears the low-water sensor. |
| `unplug` | Simulates the cooker disappearing from Bluetooth. |

Returns `{"ok": true, "sim_action": "food"}`. This lets you test scripts end-to-end without hardware: `python3 app.py --sim --speed 120`.

---

## Object reference

### Status object (`GET /status`, `/stream`, action responses)

| Field | Type | Description |
|---|---|---|
| `api_version` | string | `"1.0"` |
| `timestamp` | string | When this snapshot was taken (UTC). |
| `simulator` | bool | `true` when run with `--sim`. |
| `sim_speed` | number | Only present in simulator mode: time multiplier. |
| `connection.state` | string | `idle` (no cook needs Bluetooth), `scanning`, `reconnecting`, `connected`. |
| `connection.connected` | bool | Bluetooth link is up right now. |
| `connection.message` | string \| null | Latest connection problem in plain words. |
| `cooker.water_temp_c` / `_f` | number \| null | Latest bath temperature; `null` when not connected. |
| `cooker.water_low` | bool | Low-water sensor. |
| `cooker.motor_speed` | number \| null | Raw motor-speed sensor from the cooker (`0` = stopped). |
| `cooker.telemetry_age_s` | number \| null | Seconds since the last successful reading (`null` = none yet). A growing value means the link is stale. |
| `last_event_id` | integer | Id of the newest [event](#events); compare with your last seen id. |
| `cook` | object \| null | See below; `null` when no session exists. |

### Cook object

| Field | Type | Description |
|---|---|---|
| `phase` | string | See the [lifecycle](#the-cook-lifecycle). |
| `paused` | bool | Timers and ETA frozen. |
| `pause_source` | `"ui"` \| `"cooker"` \| null | Who paused (UI/API vs. the cooker's own button). |
| `name`, `emoji` | string | Preset name/emoji, or `"Manual"`. |
| `manual` | bool | Started with a `manual` body. |
| `frozen` | bool | Frozen extra time included. |
| `pot` | object | `{id, label, range}` of the selected pot. |
| `liters` | number | Water volume for this cook. |
| `target_c`, `target_f` | number | Setpoint. |
| `base_s`, `extra_s`, `total_s` | number | Cook time, frozen extra, and their sum. |
| `elapsed_s` | number | Time on the cook timer (keeps counting in overtime). |
| `remaining_s` | number | `max(0, total_s - elapsed_s)`. |
| `overtime_s` | number | `max(0, elapsed_s - total_s)`. |
| `cooker_link_lost` | bool | In a live phase while Bluetooth is down. |
| `end_reason` | string \| null | Set in `ended`. |
| `session_s` | number | Seconds since the session was created (frozen at the end). |
| `auto_start_pending` | bool | A script-requested `auto_start` is still waiting. |
| `available_actions` | string[] | Actions that will currently succeed. |
| `preheat` | object \| null | Present once heating began: `start_temp_c`, `eta_s` (seconds until target; recomputed every minute), `eta_source` (`model` from power/volume, `measured` once the real heating rate is known), `progress` (0–1), `duration_s` (preheat length so far, or total once finished). |
| `dip` | object \| null | Present after food was detected by temperature dip: `active` (still recovering), `low_c` (lowest temp), `depth_c` (target − low), `recovery_s` (time from the fall start until back at target), `low_after_s` (time to the minimum). |

---

## Recipes

### Start a cook, wait for it to be ready, then notify

```python
import json, os, time, urllib.request

BASE = "http://127.0.0.1:8080/api/v1"
TOKEN = os.environ.get("SOUSVIDE_API_TOKEN")

def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

call("POST", "/cook", {"preset": "Salmon", "pot": "small",
                       "confirm_water": True, "auto_start": True})

while (cook := call("GET", "/status")["cook"])["phase"] != "ready":
    time.sleep(5)
print("Bath is ready — add the food!")
```

### Follow live events with `curl`

`cook_event` payloads start with `{"id"`, status payloads with `{"api_version"`, so a plain filter is enough:

```bash
curl -sN -H "$AUTH" $SV/stream \
  | grep --line-buffered '^data: {"id"' | sed -u 's/^data: //' \
  | jq --unbuffered -r '"chime: " + .type'
```

### Log a cook to CSV

```bash
python3 examples/svctl.py log cook.csv     # polls /history?since=… every 5 s until the cook is dismissed
```

or one shot, before dismissing: `curl -H "$AUTH" "$SV/history?format=csv" > cook.csv`.

### Change a preset from a script

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -X PATCH $SV/presets/f1 -d '{"time_min": 105}'
```

### Home Assistant (REST sensor)

```yaml
sensor:
  - platform: rest
    name: Sous vide water temp
    resource: http://sousvide.local:8080/api/v1/status
    headers: { Authorization: "Bearer YOUR_TOKEN" }
    value_template: "{{ value_json.cooker.water_temp_c }}"
    unit_of_measurement: "°C"
    json_attributes_path: "$.cook"
    json_attributes: [phase, remaining_s, available_actions]
    scan_interval: 10
```

---

## Legacy UI endpoints

These are what `index.html` uses. They are unchanged and unauthenticated; prefer `/api/v1/` for scripts.

| Request | Description |
|---|---|
| `GET /` | The touchscreen UI (`index.html`). |
| `GET /api/state` | UI snapshot: `sim`, `speed`, `conn`, `tel`, `events` (last 20), `last_event_id`, `volume`, and `session` (cook state plus `chart` and `dip_chart` point arrays, and `pre` / `dip` sub-objects). Temperatures in °C, durations in seconds. |
| `GET /api/settings` | Settings object (same as `GET /api/v1/settings`). |
| `PUT /api/settings` | Full replace (omitted fields reset to defaults). Same as `PUT /api/v1/settings` but malformed JSON is rejected with `400`. |
| `POST /api/session/start` | Body `{"food_id": "f3", "pot": "small", "frozen": false}` or `{"pot": "small", "manual": {"temp_c": 63, "time_min": 45, "liters": null}}`. Returns `{"ok": true}`. `409` if a cook exists. Does **not** confirm the water; follow with `water_ok`. |
| `POST /api/session/action` | Body `{"action": "water_ok"}` (any [action](#actions)). Returns `{"ok": true}`; `409` if not allowed; `502` on cooker command failure. |
| `POST /api/sim/{name}` | Simulator hooks (`food`, `lowwater`, `refill`, `unplug`). |

---

## Behavior notes and caveats

- **One cook at a time.** The engine holds a single session. A second `POST /cook` gets `409`.
- **Polling cost.** `/status` is computed from in-memory state and does not talk to the cooker, so polling it never adds Bluetooth traffic. The app itself polls the cooker every ~2 s while a cook is live and not at all when idle.
- **`water_temp_c` is `null` while idle** by design: with no cook, there is no Bluetooth connection and therefore no reading.
- **Event ring.** Only the last 30 events are kept, and events have no timestamps. Use `/stream`, or poll `/events?after=<last id>` more often than events can occur (a few per cook).
- **History is per session.** `/history` covers the current session only (including the preheat and the food-dip waveform) and is gone after `dismiss`.
- **The app never turns the cooker off by itself.** After `done`, the cooker keeps holding temperature until you call `stop` (or unplug it). `pause` and `stop` are the only actions that send a stop command.
- **Settings clamp rather than fail** for out-of-range numbers (as the UI does); wrong types and unknown fields do fail (`422`).
- **Time in the simulator.** With `--speed N`, all durations and timestamps in `status`/`history` run N× faster than wall-clock; real-time `interval`s on `/stream` are unaffected.
- **Security** — see [Authentication](#authentication): the token covers `/api/v1/*` only.
