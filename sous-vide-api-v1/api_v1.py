"""
Sous Vide Assistant - scripting API (v1)
========================================

Adds a documented, script-friendly HTTP API under /api/v1/ on top of the
existing app. Nothing in the UI or the cook engine changes: every endpoint here
reads the same App object or calls the same methods the touchscreen UI uses
(start_session / action / settings), so scripts and the UI always agree.

Full reference: docs/API.md   (machine-readable: docs/openapi.yaml)

Wired in from app.py with:

    api_v1.install(web_app, app_state, sys.modules[__name__], token=...)

`deps` is the app module itself; it is passed in (instead of imported) so this
file never imports app.py, which would load it a second time when app.py is
run as a script.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import hmac
import io
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

log = logging.getLogger("sousvide.api")

API_VERSION = "1.0"

ACTIONS = ("water_ok", "start", "cooker_started", "pause", "resume",
           "food_in", "stop", "cancel", "dismiss")
LIVE_PHASES = ("preheat", "ready", "cook", "done")
SIM_ACTIONS = ("food", "lowwater", "refill", "unplug")

SCALAR_SETTINGS = {
    "power_w": "num", "efficiency": "num", "dip_threshold_c": "num",
    "ready_tol_c": "num", "ready_reminder_s": "num", "unplug_end_s": "num",
    "detect_pause_from_motor": "bool", "volume": "num",
}
POT_FIELDS = {"label": "str", "range": "str", "liters": "num"}
FOOD_FIELDS = {"name": "str", "emoji": "str", "temp_c": "num", "time_min": "num",
               "frozen_extra_min": "num", "liters": "liters"}
POT_IDS = ("small", "medium", "large")

AUTO_START_TIMEOUT_S = 900


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _c2f(c):
    return None if c is None else round(c * 9 / 5 + 32, 2)


def _iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _sse(event: str, data, eid=None) -> bytes:
    head = f"id: {eid}\n" if eid is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def available_actions(phase: str, paused: bool) -> list[str]:
    """Mirror of the rules in App.action(): what a script may call right now."""
    if phase == "water":
        return ["water_ok", "cancel"]
    if phase == "connect":
        return ["cancel"]
    if phase == "armed":
        return ["start", "cooker_started", "cancel"]
    if phase in LIVE_PHASES:
        acts = ["resume" if paused else "pause"]
        if phase == "ready":
            acts.append("food_in")
        acts.append("stop")
        return acts
    if phase == "ended":
        return ["dismiss"]
    return []


# --------------------------------------------------------------------------- #
# API object
# --------------------------------------------------------------------------- #

class Api:
    def __init__(self, state, deps, token):
        self.state = state
        self.deps = deps
        self.ApiError = deps.ApiError
        self.token = token or None
        self._tasks: set[asyncio.Task] = set()
        self._auto_session = None

    # ---- errors / parsing ---------------------------------------------

    def err(self, msg, status=400):
        return self.ApiError(msg, status)

    async def body(self, request, *, required=False) -> dict:
        if not request.can_read_body:
            if required:
                raise self.err("A JSON request body is required")
            return {}
        try:
            data = await request.json()
        except Exception:
            raise self.err("Request body must be valid JSON")
        if not isinstance(data, dict):
            raise self.err("Request body must be a JSON object")
        return data

    # ---- middleware ---------------------------------------------------

    @web.middleware
    async def middleware(self, request, handler):
        if self.token and request.path.rstrip("/") != "/api/v1/health":
            supplied = ""
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            elif "X-API-Key" in request.headers:
                supplied = request.headers["X-API-Key"].strip()
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return web.json_response(
                    {"error": "Unauthorized: send 'Authorization: Bearer <token>'"},
                    status=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            return await handler(request)
        except self.ApiError as e:
            return _json({"error": str(e)}, e.status)
        except web.HTTPException as e:
            return _json({"error": e.reason or "error"}, e.status)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("API handler failed: %s %s", request.method, request.path)
            return _json({"error": "Internal error"}, 500)

    # ---- status builders ----------------------------------------------

    def status(self) -> dict:
        st = self.state
        snap = st.snapshot()
        conn, tel, sess = snap["conn"], snap["tel"], snap["session"]
        connected = bool(st.adapter.connected)
        out = {
            "api_version": API_VERSION,
            "timestamp": _iso(),
            "simulator": snap["sim"],
            "connection": {
                "state": conn["state"],
                "connected": connected,
                "message": conn.get("msg") or None,
            },
            "cooker": {
                "water_temp_c": _r(tel["temp_c"]),
                "water_temp_f": _c2f(tel["temp_c"]),
                "water_low": tel["water_low"],
                "motor_speed": st.tel.get("motor") if connected else None,
                "telemetry_age_s": tel["age_s"],
            },
            "last_event_id": snap["last_event_id"],
            "cook": None,
        }
        if snap["sim"]:
            out["sim_speed"] = snap["speed"]
        if sess is not None:
            out["cook"] = self._cook_view(sess)
        return out

    def _cook_view(self, s: dict) -> dict:
        c = {
            "phase": s["phase"],
            "paused": s["paused"],
            "pause_source": s["pause_src"],
            "name": s["name"],
            "emoji": s["emoji"],
            "manual": s["manual"],
            "frozen": s["frozen"],
            "pot": s["pot"],
            "liters": s["liters"],
            "target_c": s["target_c"],
            "target_f": _c2f(s["target_c"]),
            "total_s": round(s["total_s"], 1),
            "base_s": s["base_s"],
            "extra_s": s["extra_s"],
            "elapsed_s": round(s["elapsed_s"], 1),
            "remaining_s": round(s["remaining_s"], 1),
            "overtime_s": round(s["overtime_s"], 1),
            "cooker_link_lost": s["lost"],
            "end_reason": s["end_reason"],
            "session_s": round(s["session_s"], 1),
            "auto_start_pending": self._auto_session is not None
                                  and self._auto_session is self.state.session,
            "available_actions": available_actions(s["phase"], s["paused"]),
            "preheat": None,
            "dip": None,
        }
        pre = s.get("pre")
        if pre:
            c["preheat"] = {
                "start_temp_c": _r(pre["start_temp"]),
                "eta_s": _r(pre["eta_s"], 0),
                "eta_source": pre["eta_src"],
                "progress": _r(pre["progress"], 3),
                "duration_s": round(pre["duration_s"], 1),
            }
        dip = s.get("dip")
        if dip:
            c["dip"] = {
                "active": dip["active"],
                "low_c": dip["low_c"],
                "depth_c": dip["depth_c"],
                "recovery_s": round(dip["recovery_s"], 1),
                "low_after_s": round(dip["low_after_s"], 1),
            }
        return c

    # ---- read endpoints -----------------------------------------------

    async def health(self, request):
        return _json({"ok": True, "api_version": API_VERSION,
                      "simulator": self.state.sim, "auth_required": bool(self.token)})

    async def get_status(self, request):
        return _json(self.status())

    async def get_cook(self, request):
        return _json({"cook": self.status()["cook"]})

    async def get_events(self, request):
        try:
            after = int(request.query.get("after", 0))
        except ValueError:
            raise self.err("'after' must be an integer")
        st = self.state
        return _json({
            "events": [e for e in st.events if e["id"] > after],
            "last_event_id": st._eid,
        })

    async def get_history(self, request):
        st = self.state
        s = st.session
        if s is None:
            raise self.err("No cook in progress", 404)
        q = request.query
        try:
            since = float(q.get("since", -1))
            max_points = int(q.get("max_points", 0))
        except ValueError:
            raise self.err("'since' must be a number and 'max_points' an integer")
        fmt = q.get("format", "json")
        if fmt not in ("json", "csv"):
            raise self.err("'format' must be json or csv")

        speed = st.clock.speed or 1.0
        wall_start = time.time() - (st.clock.now() - s.created) / speed
        rows = [(round(t - s.created, 1), round(v, 3)) for t, v in s.samples
                if (t - s.created) > since]
        if max_points >= 2:
            rows = self.deps.decimate(rows, max_points)

        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["t_s", "timestamp", "temp_c"])
            for t, v in rows:
                w.writerow([t, _iso(wall_start + t / speed), v])
            return web.Response(text=buf.getvalue(), content_type="text/csv",
                                headers={"Cache-Control": "no-store"})

        return _json({
            "session_started_at": _iso(wall_start),
            "name": s.name,
            "phase": s.phase,
            "columns": ["t_s", "temp_c"],
            "samples": [list(r) for r in rows],
            "markers": [{"t_s": round(t - s.created, 1), "kind": k} for t, k in s.markers],
            "next_since": rows[-1][0] if rows else max(since, 0),
        })

    async def stream(self, request):
        try:
            interval = float(request.query.get("interval", 1.0))
        except ValueError:
            raise self.err("'interval' must be a number")
        interval = max(0.25, min(30.0, interval))
        st = self.state
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        last_id = st._eid
        try:
            await resp.write(b"retry: 3000\n\n")
            while True:
                for e in [e for e in st.events if e["id"] > last_id]:
                    last_id = e["id"]
                    await resp.write(_sse("cook_event", e, eid=e["id"]))
                await resp.write(_sse("status", self.status()))
                await asyncio.sleep(interval)
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass
        return resp

    # ---- settings -----------------------------------------------------

    async def get_settings(self, request):
        return _json(self.state.settings)

    async def put_settings(self, request):
        body = await self.body(request, required=True)
        return self._commit_settings(self.deps.sanitize_settings(body))

    async def patch_settings(self, request):
        body = await self.body(request, required=True)
        return self._commit_settings(self._merge(self.state.settings, body))

    def _commit_settings(self, new: dict):
        self.state.settings = new
        self.deps.save_settings(new)
        return _json(new)

    def _check(self, kind, field, v):
        if kind == "num":
            ok = _is_num(v)
        elif kind == "bool":
            ok = isinstance(v, bool)
        elif kind == "str":
            ok = isinstance(v, str)
        elif kind == "liters":
            ok = isinstance(v, dict) and all(
                k in POT_IDS and (x is None or _is_num(x)) for k, x in v.items())
        else:
            ok = False
        if not ok:
            want = {"num": "a number", "bool": "true or false", "str": "a string",
                    "liters": "an object like {\"small\": 5.0, \"medium\": null}"}[kind]
            raise self.err(f"'{field}' must be {want}", 422)

    def _merge(self, cur: dict, patch: dict) -> dict:
        """Apply a partial update. Unknown fields are rejected (422);
        values are then clamped by the same sanitiser the UI uses."""
        unknown = [k for k in patch if k not in SCALAR_SETTINGS and k not in ("pots", "foods")]
        if unknown:
            raise self.err(f"Unknown setting(s): {', '.join(unknown)}", 422)
        new = copy.deepcopy(cur)
        for key, val in patch.items():
            if key in SCALAR_SETTINGS:
                self._check(SCALAR_SETTINGS[key], key, val)
                new[key] = val
                continue
            if not isinstance(val, list):
                raise self.err(f"'{key}' must be a list of objects with an 'id'", 422)
            types = POT_FIELDS if key == "pots" else FOOD_FIELDS
            index = {x["id"]: x for x in new[key]}
            for item in val:
                if not isinstance(item, dict) or "id" not in item:
                    raise self.err(f"Every entry in '{key}' needs an 'id'", 422)
                tgt = index.get(item["id"])
                if tgt is None:
                    raise self.err(f"Unknown {key[:-1]} id '{item['id']}'", 422)
                for f, v in item.items():
                    if f == "id":
                        continue
                    if f not in types:
                        raise self.err(f"Unknown {key[:-1]} field '{f}'", 422)
                    self._check(types[f], f, v)
                    if f == "liters" and key == "foods":
                        tgt["liters"].update(v)
                    else:
                        tgt[f] = v
        return self.deps.sanitize_settings(new)

    # ---- presets / pots -----------------------------------------------

    @staticmethod
    def _preset_view(i: int, f: dict) -> dict:
        return {"slot": i + 1, "empty": not f["name"], **f}

    async def list_presets(self, request):
        return _json({"presets": [self._preset_view(i, f)
                                  for i, f in enumerate(self.state.settings["foods"])]})

    def _find_preset(self, pid):
        for i, f in enumerate(self.state.settings["foods"]):
            if f["id"] == pid:
                return i, f
        raise self.err(f"No preset with id '{pid}'", 404)

    async def get_preset(self, request):
        i, f = self._find_preset(request.match_info["id"])
        return _json(self._preset_view(i, f))

    async def patch_preset(self, request):
        pid = request.match_info["id"]
        self._find_preset(pid)
        body = await self.body(request, required=True)
        if body.get("id", pid) != pid:
            raise self.err("'id' in the body does not match the URL", 422)
        body["id"] = pid
        self._commit_settings(self._merge(self.state.settings, {"foods": [body]}))
        i, f = self._find_preset(pid)
        return _json(self._preset_view(i, f))

    async def list_pots(self, request):
        return _json({"pots": self.state.settings["pots"]})

    async def patch_pot(self, request):
        pid = request.match_info["id"]
        if pid not in POT_IDS:
            raise self.err(f"No pot with id '{pid}'", 404)
        body = await self.body(request, required=True)
        if body.get("id", pid) != pid:
            raise self.err("'id' in the body does not match the URL", 422)
        body["id"] = pid
        self._commit_settings(self._merge(self.state.settings, {"pots": [body]}))
        return _json(next(p for p in self.state.settings["pots"] if p["id"] == pid))

    # ---- cook control -------------------------------------------------

    def _resolve_pot(self, v) -> str:
        pots = self.state.settings["pots"]
        if isinstance(v, str):
            for p in pots:
                if v.strip().lower() in (p["id"].lower(), p["label"].lower()):
                    return p["id"]
        raise self.err("'pot' is required and must be one of: "
                       + ", ".join(f"{p['id']} ({p['label']})" for p in pots), 422)

    def _resolve_preset(self, v) -> str:
        foods = self.state.settings["foods"]
        if isinstance(v, str):
            key = v.strip().lower()
            for f in foods:
                if f["id"].lower() == key:
                    return f["id"]
            for f in foods:
                if f["name"] and f["name"].lower() == key:
                    return f["id"]
        raise self.err(f"Unknown preset {v!r}; use a preset id (f1..f{len(foods)}) or its exact name", 422)

    async def start_cook(self, request):
        body = await self.body(request, required=True)
        unknown = [k for k in body if k not in
                   ("preset", "manual", "pot", "frozen", "confirm_water", "auto_start")]
        if unknown:
            raise self.err(f"Unknown field(s): {', '.join(unknown)}", 422)
        if ("preset" in body) == ("manual" in body):
            raise self.err("Provide exactly one of 'preset' or 'manual'", 422)
        for flag in ("frozen", "confirm_water", "auto_start"):
            if flag in body and not isinstance(body[flag], bool):
                raise self.err(f"'{flag}' must be true or false", 422)
        auto = body.get("auto_start", False)
        confirm = body.get("confirm_water", False) or auto
        if auto and not body.get("confirm_water", False):
            raise self.err("'auto_start' also needs 'confirm_water': true "
                           "(you are asserting the pot is filled)", 422)
        payload = {"pot": self._resolve_pot(body.get("pot"))}
        if "preset" in body:
            payload["food_id"] = self._resolve_preset(body["preset"])
            payload["frozen"] = bool(body.get("frozen", False))
        else:
            m = body["manual"]
            if not isinstance(m, dict):
                raise self.err("'manual' must be an object", 422)
            bad = [k for k in m if k not in ("temp_c", "time_min", "liters")]
            if bad:
                raise self.err(f"Unknown manual field(s): {', '.join(bad)}", 422)
            for k in ("temp_c", "time_min"):
                if not _is_num(m.get(k)):
                    raise self.err(f"manual.{k} is required and must be a number", 422)
            if m.get("liters") is not None and not _is_num(m["liters"]):
                raise self.err("manual.liters must be a number", 422)
            payload["manual"] = {k: m.get(k) for k in ("temp_c", "time_min", "liters")}

        st = self.state
        st.start_session(payload)          # raises 409 if a cook is already running
        session = st.session
        if confirm:
            await st.action("water_ok")
        if auto:
            self._schedule_auto_start(session)
        return _json({"ok": True, "status": self.status()}, 201)

    def _schedule_auto_start(self, session):
        self._auto_session = session
        task = asyncio.create_task(self._auto_start(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _auto_start(self, session):
        st = self.state
        deadline = time.monotonic() + AUTO_START_TIMEOUT_S
        try:
            while time.monotonic() < deadline:
                if st.session is not session or session.phase not in ("connect", "armed"):
                    return
                if session.phase == "armed":
                    await st.action("start")
                    return
                await asyncio.sleep(0.5)
            log.warning("auto_start gave up: cooker never reached 'armed' within %ds",
                        AUTO_START_TIMEOUT_S)
        except self.ApiError as e:
            log.warning("auto_start could not start the cooker: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto_start failed")
        finally:
            if self._auto_session is session:
                self._auto_session = None

    async def do_action(self, request):
        name = request.match_info["action"]
        if name not in ACTIONS:
            raise self.err(f"Unknown action '{name}'. Valid: {', '.join(ACTIONS)}", 404)
        await self.state.action(name)
        return _json({"ok": True, "action": name, "status": self.status()})

    async def do_sim(self, request):
        name = request.match_info["name"]
        if name not in SIM_ACTIONS:
            raise self.err(f"Unknown sim action '{name}'. Valid: {', '.join(SIM_ACTIONS)}", 404)
        self.state.sim_action(name)
        return _json({"ok": True, "sim_action": name})

    async def openapi(self, request):
        path = Path(__file__).resolve().parent / "docs" / "openapi.yaml"
        if not path.exists():
            raise self.err("docs/openapi.yaml not found", 404)
        return web.Response(text=path.read_text(encoding="utf-8"),
                            content_type="application/yaml")


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

def install(web_app: web.Application, state, deps, token: str | None = None) -> Api:
    api = Api(state, deps, token)
    sub = web.Application(middlewares=[api.middleware])
    sub.add_routes([
        web.get("/health", api.health),
        web.get("/openapi.yaml", api.openapi),
        # telemetry
        web.get("/status", api.get_status),
        web.get("/stream", api.stream),
        web.get("/events", api.get_events),
        web.get("/history", api.get_history),
        # cook control
        web.get("/cook", api.get_cook),
        web.post("/cook", api.start_cook),
        web.post("/cook/actions/{action}", api.do_action),
        # settings
        web.get("/settings", api.get_settings),
        web.put("/settings", api.put_settings),
        web.patch("/settings", api.patch_settings),
        web.get("/presets", api.list_presets),
        web.get("/presets/{id}", api.get_preset),
        web.patch("/presets/{id}", api.patch_preset),
        web.get("/pots", api.list_pots),
        web.patch("/pots/{id}", api.patch_pot),
        # simulator hooks (404 unless started with --sim)
        web.post("/sim/{name}", api.do_sim),
    ])
    web_app.add_subapp("/api/v1", sub)
    return api
