#!/usr/bin/env python3
"""
Sous Vide Assistant
===================
Touchscreen web UI + cook-session engine for the Anova Precision Cooker Nano.

Run on the laptop that has Bluetooth:

    pip install -r requirements.txt
    python3 app.py                 # real cooker, http://127.0.0.1:8080
    python3 app.py --sim --speed 60   # simulated cooker, 60x time (for testing the UI)

The cooker is only touched while a cook session is active. When idle, no BLE
traffic is generated at all.

Cook flow (all driven from this file):
    water -> connect -> armed -> preheat -> ready -> cook -> done(overtime) -> ended
The app never tells the cooker to stop on its own; the cooker keeps holding the
setpoint after the timer finishes until you press Stop here or unplug it.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from aiohttp import web

import api_v1  # scripting API under /api/v1 (see docs/API.md)

log = logging.getLogger("sousvide")

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
INDEX_FILE = BASE / "index.html"

C_WATER = 4186.0  # J / (kg*K); 1 L of water ~ 1 kg
SERVICE_UUID = "0e140000-0af1-4582-a242-773e63054c68"

CONN_PHASES = ("connect", "armed", "preheat", "ready", "cook", "done")
LIVE_PHASES = ("preheat", "ready", "cook", "done")
POLL_PHASES = ("armed",) + LIVE_PHASES


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def default_settings() -> dict:
    pots = [
        {"id": "small", "label": "8 qt", "range": "1–2 lb", "liters": 5.0},
        {"id": "medium", "label": "12 qt", "range": "3–6 lb", "liters": 8.0},
        {"id": "large", "label": "16 qt", "range": "6+ lb", "liters": 11.0},
    ]
    # name, emoji, temp C, time min, extra minutes when started from frozen
    presets = [
        ("Chicken Breast", "🐔", 65.0, 90, 45),
        ("Chicken Thighs", "🍗", 74.0, 120, 60),
        ("Steak (Med-Rare)", "🥩", 54.5, 90, 45),
        ("Pork Chops", "🐷", 60.0, 90, 45),
        ("Pulled Pork", "🍖", 74.0, 1440, 120),
        ("Beef Roast", "🐄", 57.0, 1440, 180),
        ("Salmon", "🐟", 52.0, 45, 20),
        ("Pasteurize Eggs", "🥚", 57.0, 75, 0),
        ("Soft Eggs", "🍳", 63.0, 45, 0),
        ("Vegetables", "🥕", 85.0, 60, 15),
        ("", "", 60.0, 60, 0),
        ("", "", 60.0, 60, 0),
    ]
    foods = []
    for i, (name, emoji, temp, mins, frozen) in enumerate(presets, start=1):
        foods.append({
            "id": f"f{i}", "name": name, "emoji": emoji, "temp_c": temp,
            "time_min": mins, "frozen_extra_min": frozen,
            "liters": {"small": None, "medium": None, "large": None},
        })
    return {
        "pots": pots,
        "foods": foods,
        "power_w": 750,
        "efficiency": 0.85,          # fraction of electrical power that ends up heating the bath
        "dip_threshold_c": 1.0,      # drop below target that means "food went in"
        "ready_tol_c": 0.2,          # within this of target counts as "at temperature"
        "ready_reminder_s": 60,      # repeat the "bath ready" chime (0 = once)
        "unplug_end_s": 120,         # overtime + BLE gone this long => treat as unplugged
        "detect_pause_from_motor": True,
        "volume": 0.7,
    }


def _num(v, lo, hi, default, nd=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    x = max(lo, min(hi, x))
    return round(x, nd) if nd is not None else x


def sanitize_settings(raw: dict) -> dict:
    d = default_settings()
    if not isinstance(raw, dict):
        return d
    pots_in = raw.get("pots") if isinstance(raw.get("pots"), list) else []
    for i, pot in enumerate(d["pots"]):
        src = pots_in[i] if i < len(pots_in) and isinstance(pots_in[i], dict) else {}
        pot["label"] = str(src.get("label", pot["label"]))[:12] or pot["label"]
        pot["range"] = str(src.get("range", pot["range"]))[:16]
        pot["liters"] = _num(src.get("liters"), 0.5, 30, pot["liters"], 2)
    foods_in = raw.get("foods") if isinstance(raw.get("foods"), list) else []
    for i, food in enumerate(d["foods"]):
        if i >= len(foods_in) or not isinstance(foods_in[i], dict):
            continue
        src = foods_in[i]
        food["name"] = str(src.get("name", food["name"]))[:24].strip()
        food["emoji"] = str(src.get("emoji", food["emoji"]))[:8]
        food["temp_c"] = _num(src.get("temp_c"), 20, 92, food["temp_c"], 1)
        food["time_min"] = int(_num(src.get("time_min"), 1, 4320, food["time_min"]))
        food["frozen_extra_min"] = int(_num(src.get("frozen_extra_min"), 0, 4320, food["frozen_extra_min"]))
        lit = src.get("liters") if isinstance(src.get("liters"), dict) else {}
        for pid in ("small", "medium", "large"):
            v = lit.get(pid)
            food["liters"][pid] = None if v in (None, "", 0) else _num(v, 0.5, 30, None, 2)
    d["power_w"] = int(_num(raw.get("power_w"), 100, 3000, d["power_w"]))
    d["efficiency"] = _num(raw.get("efficiency"), 0.3, 1.0, d["efficiency"], 3)
    d["dip_threshold_c"] = _num(raw.get("dip_threshold_c"), 0.3, 10, d["dip_threshold_c"], 2)
    d["ready_tol_c"] = _num(raw.get("ready_tol_c"), 0.05, 2, d["ready_tol_c"], 2)
    d["ready_reminder_s"] = int(_num(raw.get("ready_reminder_s"), 0, 600, d["ready_reminder_s"]))
    d["unplug_end_s"] = int(_num(raw.get("unplug_end_s"), 30, 1800, d["unplug_end_s"]))
    d["detect_pause_from_motor"] = bool(raw.get("detect_pause_from_motor", d["detect_pause_from_motor"]))
    d["volume"] = _num(raw.get("volume"), 0.0, 1.0, d["volume"], 2)
    return d


def load_settings() -> dict:
    try:
        return sanitize_settings(json.loads(SETTINGS_FILE.read_text()))
    except FileNotFoundError:
        return default_settings()
    except Exception as e:  # corrupt file: keep a copy, start fresh
        log.warning("settings.json unreadable (%s); using defaults", e)
        try:
            SETTINGS_FILE.rename(SETTINGS_FILE.with_suffix(".json.bad"))
        except OSError:
            pass
        return default_settings()


def save_settings(s: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    os.replace(tmp, SETTINGS_FILE)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

class Clock:
    """Wall clock with an optional speed-up (simulator only)."""

    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self._t0 = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed


def lsq_slope(pts):
    n = len(pts)
    mt = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mt) ** 2 for p in pts)
    return 0.0 if den == 0 else sum((p[0] - mt) * (p[1] - my) for p in pts) / den


def decimate(pts, n=500):
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    out = [pts[int(i * step)] for i in range(n)]
    out.append(pts[-1])
    return out


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- #
# Cooker adapters
# --------------------------------------------------------------------------- #

def load_library():
    """Load the Anova Nano library from the repo.

    The file is called nano-interactive.py (hyphen => not importable by name), so
    it is loaded by path. If you rename it to anova_nano.py that works too.
    """
    sys.path.insert(0, str(BASE))
    try:
        import anova_nano  # type: ignore
        return anova_nano
    except ImportError:
        pass
    path = BASE / "nano-interactive.py"
    if not path.exists():
        raise SystemExit("Could not find nano-interactive.py next to app.py")
    spec = importlib.util.spec_from_file_location("anova_nano", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anova_nano"] = mod
    spec.loader.exec_module(mod)
    # the library logs every command at INFO; keep the console readable
    logging.getLogger("anova_nano").setLevel(logging.WARNING)
    return mod


class AnovaAdapter:
    """Thin async wrapper around AnovaNanoDevice.

    Adds: own BLE discovery (bleak >= 1.0 removed BLEDevice.metadata, which the
    library's discover_and_connect relies on), command serialisation + timeouts,
    and C/F normalisation so the rest of the app only ever sees Celsius.
    """

    def __init__(self, lib):
        self.lib = lib
        self.dev = None
        self.unit = "C"
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        try:
            return self.dev is not None and self.dev.client.is_connected
        except Exception:
            return False

    async def connect(self):
        bleak = self.lib.bleak
        found = await bleak.BleakScanner.discover(timeout=8.0, return_adv=True)
        cands = []
        for dev, adv in found.values():
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            if SERVICE_UUID in uuids:
                cands.append((adv.rssi if adv.rssi is not None else -999, dev))
        if not cands:
            raise self.lib.DeviceNotFoundError("Cooker not found")
        cands.sort(key=lambda c: -c[0])
        device = cands[0][1]
        client = bleak.BleakClient(device)
        await asyncio.wait_for(client.connect(), 20)
        dev = self.lib.AnovaNanoDevice(device, client)
        try:
            await asyncio.wait_for(dev.prepare_for_use(), 15)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self.dev = dev
        self.unit = self.lib.map_unit_value(dev._temp_unit)
        if self.unit not in ("C", "F"):
            self.unit = "C"
        log.info("Connected to %s (device unit %s)", device.address, self.unit)

    async def disconnect(self):
        dev, self.dev = self.dev, None
        if dev is not None:
            try:
                await asyncio.wait_for(dev.disconnect(), 8)
            except Exception as e:
                log.debug("disconnect: %s", e)

    async def _call(self, fn, *a, timeout=8.0):
        if self.dev is None:
            raise ConnectionError("not connected")
        async with self._lock:
            q = self.dev._response_queue
            while not q.empty():  # drop stale/late responses
                q.get_nowait()
            return await asyncio.wait_for(fn(*a), timeout)

    def _to_dev(self, c):
        v = c * 9 / 5 + 32 if self.unit == "F" else c
        return round(v, 1) + 1e-6  # library does int(v * 10)

    def _from_dev(self, v):
        return (v - 32) * 5 / 9 if self.unit == "F" else v

    async def poll(self) -> dict:
        st = await self._call(self.dev.get_status)
        temp = st.get("current_temp")
        if temp is None:
            raise RuntimeError("no water temperature in sensor list")
        sv = {}
        for s in st.get("sensor_values", []):
            sv[s["sensorType"]] = s["value"]
        return {
            "temp_c": self._from_dev(temp),
            "water_low": bool(sv["WaterLow"]) if "WaterLow" in sv else None,
            "motor": sv.get("MotorSpeed"),
        }

    async def ensure_target_c(self, c: float) -> float:
        cur = self._from_dev(await self._call(self.dev.read_target_temp))
        if abs(cur - c) > 0.06:
            await self._call(self.dev.set_target_temp, self._to_dev(c))
            cur = self._from_dev(await self._call(self.dev.read_target_temp))
        if abs(cur - c) > 0.15:
            raise RuntimeError(f"cooker setpoint is {cur:.1f}, wanted {c:.1f}")
        return cur

    async def start(self):
        await self._call(self.dev.start_cooking)

    async def stop(self):
        await self._call(self.dev.stop_cooking)


class SimAdapter:
    """Physical model of a 750 W circulator in a pot, for testing without hardware."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.connected = False
        self.temp = 21.0 + random.random() * 3
        self.room = 22.0
        self.target = 57.0
        self.running = False
        self.low = False
        self.liters = 5.0
        self.food = None
        self._t = clock.now()

    def configure(self, liters: float):
        self._advance()
        self.liters = liters

    def _advance(self):
        now = self.clock.now()
        dt = now - self._t
        self._t = now
        while dt > 1e-9:
            h = min(dt, 1.0)
            dt -= h
            cap = (self.liters + 0.35) * C_WATER
            p = min(750 * 0.93, max(0.0, 2000.0 * (self.target - self.temp))) if self.running else 0.0
            q = p - 2.4 * (self.temp - self.room)
            if self.food:
                g = 10.0 * (self.temp - self.food["t"])
                q -= g
                self.food["t"] += g * h / self.food["c"]
            self.temp += q * h / cap

    # --- test hooks -------------------------------------------------------
    def add_food(self, kg=1.0):
        self._advance()
        self.food = {"c": kg * 3500.0, "t": 4.0}
        self.temp -= 2.2 * (5.0 / self.liters)

    # --- adapter API ------------------------------------------------------
    async def connect(self):
        await asyncio.sleep(2.5)
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def poll(self):
        self._advance()
        return {
            "temp_c": self.temp + random.uniform(-0.03, 0.03),
            "water_low": self.low,
            "motor": 1800 if self.running else 0,
        }

    async def ensure_target_c(self, c):
        self.target = c
        return c

    async def start(self):
        self._advance()
        self.running = True

    async def stop(self):
        self._advance()
        self.running = False


# --------------------------------------------------------------------------- #
# Cook session
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self, now, *, name, emoji, target_c, base_s, extra_s, frozen, pot, liters, manual):
        self.name, self.emoji, self.manual = name, emoji, manual
        self.target_c, self.frozen = target_c, frozen
        self.base_s, self.extra_s = base_s, extra_s
        self.total_s = base_s + extra_s
        self.pot, self.liters = pot, liters
        self.phase = "water"
        self.created = now
        self.paused = False
        self.pause_src = None
        self.samples = []      # (t, temp)
        self.markers = []      # (t, kind)
        self.last_temp = None
        # preheat
        self.pre = {"start_t": None, "start_temp": None, "eta_abs": None, "eta_src": "model",
                    "last_calc": None, "win_start_t": None}
        self.ready_t = None
        self.ready_hits = 0
        self.last_ready_chime = 0.0
        self.dip_armed = True
        # cook
        self.cook_start_t = None
        self.elapsed = 0.0
        self.last_acc = None
        self.done_t = None
        self.dip = None
        # bookkeeping
        self.grace_until = 0.0
        self.motor_seen_running = False
        self.motor_off_hits = 0
        self.lost_since = None
        self.last_lost_chime = 0.0
        self.end_reason = None
        self.ended_t = None

    def accrue(self, now):
        if self.phase in ("cook", "done") and not self.paused and self.last_acc is not None:
            self.elapsed += max(0.0, now - self.last_acc)
        self.last_acc = now

    def mark(self, t, kind):
        self.markers.append((t, kind))


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, sim: bool, speed: float, lib=None):
        self.sim = sim
        self.clock = Clock(speed if sim else 1.0)
        self.adapter = SimAdapter(self.clock) if sim else AnovaAdapter(lib)
        self.settings = load_settings()
        self.session: Session | None = None
        self.conn = {"state": "idle", "msg": ""}
        self.tel = {"temp_c": None, "water_low": None, "motor": None, "wall": None}
        self.events: list[dict] = []
        self._eid = 0
        self.needs_setpoint = False
        self.tel_errors = 0
        self.last_low_chime = 0.0
        self.poll_s = max(0.15, min(2.0, 2.0 / self.clock.speed))

    # ---- events ----------------------------------------------------------
    def emit(self, kind: str):
        self._eid += 1
        self.events.append({"id": self._eid, "type": kind})
        self.events = self.events[-30:]

    # ---- session creation ------------------------------------------------
    def start_session(self, p: dict):
        if self.session is not None:
            raise ApiError("A cook is already in progress", 409)
        cfg = self.settings
        pot = next((x for x in cfg["pots"] if x["id"] == p.get("pot")), None)
        if pot is None:
            raise ApiError("Pick a pot size")
        if p.get("manual"):
            m = p["manual"]
            temp = _num(m.get("temp_c"), 0, 1000, None, 1)
            mins = _num(m.get("time_min"), 0, 100000, None)
            liters = _num(m.get("liters"), 0, 1000, None, 2) if m.get("liters") else pot["liters"]
            if temp is None or not (20 <= temp <= 92):
                raise ApiError("Temperature must be between 20 and 92 °C")
            if mins is None or not (1 <= mins <= 4320):
                raise ApiError("Time must be between 1 minute and 72 hours")
            if not (0.5 <= liters <= 30):
                raise ApiError("Water volume must be between 0.5 and 30 L")
            name, emoji, base, extra, frozen, manual = "Manual", "🎛️", int(mins) * 60, 0, False, True
        else:
            food = next((f for f in cfg["foods"] if f["id"] == p.get("food_id")), None)
            if food is None or not food["name"]:
                raise ApiError("That slot is empty")
            frozen = bool(p.get("frozen"))
            temp = food["temp_c"]
            base = food["time_min"] * 60
            extra = food["frozen_extra_min"] * 60 if frozen else 0
            liters = food["liters"].get(pot["id"]) or pot["liters"]
            name, emoji, manual = food["name"], food["emoji"], False
        now = self.clock.now()
        s = Session(now, name=name, emoji=emoji, target_c=temp, base_s=base, extra_s=extra,
                    frozen=frozen, pot={"label": pot["label"], "range": pot["range"], "id": pot["id"]},
                    liters=liters, manual=manual)
        self.session = s
        self.tel_errors = 0
        self.last_low_chime = 0.0
        if self.sim:
            self.adapter.configure(liters)
        log.info("Session: %s %.1fC %ds %.1fL", name, temp, s.total_s, liters)

    # ---- connection manager ---------------------------------------------
    def _friendly(self, e: Exception) -> str:
        n = type(e).__name__
        if n == "DeviceNotFoundError":
            return "Cooker not found yet — is it plugged in and out of range of other phones/apps?"
        if isinstance(e, asyncio.TimeoutError):
            return "Timed out talking to the cooker — retrying"
        return f"{n}: {e}"[:140]

    async def conn_loop(self):
        while True:
            s = self.session
            want = bool(s and s.phase in CONN_PHASES)
            try:
                if want and not self.adapter.connected:
                    self.conn = {"state": "scanning", "msg": self.conn.get("msg", "")}
                    if s.phase != "connect":
                        self.conn["state"] = "reconnecting"
                    await self.adapter.connect()
                    self.conn = {"state": "connected", "msg": ""}
                    self.needs_setpoint = True
                    self.tel_errors = 0
                elif want and self.needs_setpoint:
                    await self.apply_setpoint()
                elif not want and self.adapter.connected:
                    await self.adapter.disconnect()
                    self.conn = {"state": "idle", "msg": ""}
                elif not want and self.conn["state"] != "idle":
                    self.conn = {"state": "idle", "msg": ""}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("connection: %s", e)
                self.conn = {"state": "scanning" if not self.adapter.connected else "connected",
                             "msg": self._friendly(e)}
                await asyncio.sleep(2.0)
            await asyncio.sleep(0.4)

    async def apply_setpoint(self):
        s = self.session
        if not s:
            self.needs_setpoint = False
            return
        await self.adapter.ensure_target_c(s.target_c)
        self.needs_setpoint = False
        if s.phase == "connect":
            s.phase = "armed"
            s.mark(self.clock.now(), "armed")
        s.lost_since = None

    # ---- telemetry / state machine --------------------------------------
    async def tick_loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(self.poll_s)

    async def tick(self):
        s = self.session
        if s is None or s.phase not in POLL_PHASES:
            return
        now = self.clock.now()
        mono = time.monotonic()
        s.accrue(now)
        self._timers(s, now)

        if not self.adapter.connected:
            if s.lost_since is None:
                s.lost_since = mono
            self._lost_checks(s, now, mono)
            return
        s.lost_since = None
        try:
            r = await self.adapter.poll()
            self.tel_errors = 0
        except Exception as e:
            self.tel_errors += 1
            log.warning("poll failed (%d): %s", self.tel_errors, e)
            if self.tel_errors >= 3:
                await self.adapter.disconnect()
            return
        temp = r["temp_c"]
        self.tel.update(temp_c=temp, water_low=r["water_low"], motor=r["motor"], wall=mono)
        s.last_temp = temp
        if not s.samples or now - s.samples[-1][0] >= 1.5:
            s.samples.append((now, temp))

        # water low alarm
        if r["water_low"] and mono - self.last_low_chime > 8:
            self.last_low_chime = mono
            self.emit("lowwater")

        self._motor_logic(s, r.get("motor"), now)
        if s.phase == "armed":
            if r.get("motor") and r["motor"] > 0:
                self.enter_preheat(s, now, temp)
        elif s.phase == "preheat":
            self._preheat(s, now, temp)
        elif s.phase == "ready":
            self._ready(s, now, temp, mono)
        if s.phase in ("cook", "done"):
            self._cook(s, now, temp)

    def _timers(self, s, now):
        # completion check runs even when telemetry is down: the timer is wall-clock
        if s.phase == "cook" and s.elapsed >= s.total_s:
            s.phase = "done"
            s.done_t = now
            s.mark(now, "done")
            self.emit("complete")

    def _lost_checks(self, s, now, mono):
        if s.phase in LIVE_PHASES and mono - s.lost_since > 30 and mono - s.last_lost_chime > 60:
            s.last_lost_chime = mono
            self.emit("lost")
        if s.phase == "done" and mono - s.lost_since > self.settings["unplug_end_s"]:
            self.end_session(s, now, "Cooker disconnected (unplugged?)")

    def _motor_logic(self, s, motor, now):
        if motor is None or s.phase not in LIVE_PHASES or not self.settings["detect_pause_from_motor"]:
            return
        running = motor > 0
        if running:
            s.motor_seen_running = True
            s.motor_off_hits = 0
            if s.paused and now >= s.grace_until:
                s.accrue(now)
                s.paused = False
                s.pause_src = None
                self._after_resume(s, now)
        elif s.motor_seen_running and now >= s.grace_until and not s.paused:
            s.motor_off_hits += 1
            if s.motor_off_hits >= 2:
                s.accrue(now)
                s.paused = True
                s.pause_src = "cooker"
                self._after_pause(s)

    def _after_pause(self, s):
        s.dip_armed = False

    def _after_resume(self, s, now):
        s.pre["last_calc"] = None
        s.pre["win_start_t"] = now
        s.ready_hits = 0

    def enter_preheat(self, s, now, temp):
        s.phase = "preheat"
        s.pre.update(start_t=now, start_temp=temp, win_start_t=now, last_calc=None)
        s.grace_until = now + 20
        s.mark(now, "start")
        self._eta(s, now, temp)

    def _eta(self, s, now, temp):
        p = s.pre
        if p["last_calc"] is not None and now - p["last_calc"] < 60:
            return
        p["last_calc"] = now
        rem = max(0.0, s.target_c - temp)
        cfg = self.settings
        eta = s.liters * C_WATER * rem / (cfg["power_w"] * cfg["efficiency"])
        src = "model"
        win = [(t, v) for t, v in s.samples if t >= p["win_start_t"] and t >= now - 240]
        if len(win) >= 4 and win[-1][0] - win[0][0] >= 90:
            slope = lsq_slope(win)
            if slope > 1e-4:
                eta, src = rem / slope, "measured"
        p["eta_abs"] = now + eta
        p["eta_src"] = src

    def _preheat(self, s, now, temp):
        if s.paused:
            return
        self._eta(s, now, temp)
        if temp >= s.target_c - self.settings["ready_tol_c"]:
            s.ready_hits += 1
        else:
            s.ready_hits = 0
        if s.ready_hits >= 2:
            s.phase = "ready"
            s.ready_t = now
            s.dip_armed = True
            s.last_ready_chime = time.monotonic()
            s.mark(now, "ready")
            self.emit("ready")

    def _ready(self, s, now, temp, mono):
        if s.paused:
            return
        cfg = self.settings
        rem = cfg["ready_reminder_s"]
        if rem and mono - s.last_ready_chime >= rem:
            s.last_ready_chime = mono
            self.emit("ready")
        if not s.dip_armed:
            if temp >= s.target_c - cfg["ready_tol_c"]:
                s.dip_armed = True
            return
        if temp <= s.target_c - cfg["dip_threshold_c"]:
            # back-date the start to when the bath actually began to fall
            t_b = now
            for t, v in reversed(s.samples):
                if v >= s.target_c - 0.25:
                    t_b = t
                    break
            self.begin_cook(s, now, t_b, dip=True, temp=temp)

    def begin_cook(self, s, now, start_t, dip, temp=None):
        s.phase = "cook"
        s.cook_start_t = start_t
        s.elapsed = max(0.0, now - start_t)
        s.last_acc = now
        s.mark(start_t, "food")
        if dip and temp is not None:
            s.dip = {"start_t": start_t, "low_c": temp, "low_t": now, "rec_t": None, "active": True}
        self.emit("cookstart")

    def _cook(self, s, now, temp):
        d = s.dip
        if d and d["active"]:
            if temp < d["low_c"]:
                d["low_c"], d["low_t"] = temp, now
            if temp >= s.target_c - self.settings["ready_tol_c"] and now - d["start_t"] > 20 and not s.paused:
                d["active"] = False
                d["rec_t"] = now
                s.mark(now, "recovered")
        self._timers(s, now)

    def end_session(self, s, now, reason):
        s.accrue(now)
        s.phase = "ended"
        s.ended_t = now
        s.end_reason = reason
        s.mark(now, "end")

    # ---- user actions ------------------------------------------------------
    async def _fresh_temp(self, s) -> float:
        """Best current bath temperature (used as the preheat starting point)."""
        try:
            r = await self.adapter.poll()
            self.tel.update(temp_c=r["temp_c"], water_low=r["water_low"], motor=r["motor"], wall=time.monotonic())
            s.last_temp = r["temp_c"]
        except Exception as e:
            log.warning("fresh temp read failed: %s", e)
        return s.last_temp if s.last_temp is not None else s.target_c

    async def action(self, name: str):
        s = self.session
        if s is None:
            raise ApiError("No cook in progress", 409)
        now = self.clock.now()
        ph = s.phase

        if name == "water_ok" and ph == "water":
            s.phase = "connect"
            self.needs_setpoint = True
        elif name == "cancel" and ph in ("water", "connect", "armed"):
            self.session = None
        elif name == "start" and ph == "armed":
            try:
                await self.adapter.start()
            except Exception as e:
                raise ApiError(f"Could not start the cooker: {self._friendly(e)}", 502)
            self.enter_preheat(s, now, await self._fresh_temp(s))
        elif name == "cooker_started" and ph == "armed":
            self.enter_preheat(s, now, await self._fresh_temp(s))
        elif name == "pause" and ph in LIVE_PHASES and not s.paused:
            try:
                await self.adapter.stop()
            except Exception as e:
                raise ApiError(f"Could not pause the cooker: {self._friendly(e)}", 502)
            s.accrue(now)
            s.paused, s.pause_src = True, "ui"
            s.grace_until = now + 20
            self._after_pause(s)
        elif name == "resume" and ph in LIVE_PHASES and s.paused:
            try:
                await self.adapter.start()
            except Exception as e:
                raise ApiError(f"Could not resume the cooker: {self._friendly(e)}", 502)
            s.accrue(now)
            s.paused, s.pause_src = False, None
            s.grace_until = now + 20
            self._after_resume(s, now)
        elif name == "food_in" and ph == "ready":
            self.begin_cook(s, now, now, dip=False)
        elif name == "stop" and ph in LIVE_PHASES:
            try:
                if self.adapter.connected:
                    await self.adapter.stop()
            except Exception as e:
                log.warning("stop command failed: %s", e)
            self.end_session(s, now, "Stopped")
        elif name == "dismiss" and ph == "ended":
            self.session = None
        else:
            raise ApiError(f"'{name}' is not available right now", 409)

    # ---- snapshot ----------------------------------------------------------
    def _chart(self, s, now):
        if s.phase in ("armed", "preheat") or s.ready_t is None:
            t0 = s.pre["start_t"] if s.pre["start_t"] is not None else s.created
        else:
            t0 = max(s.created, s.ready_t - 300)
        pts = [[round(t - s.created, 1), round(v, 2)] for t, v in s.samples if t >= t0]
        marks = [{"t": round(t - s.created, 1), "k": k} for t, k in s.markers if t >= t0 - 1]
        return {"pts": decimate(pts), "marks": marks}

    def _dip_chart(self, s, now):
        d = s.dip
        if not d:
            return None
        end = (d["rec_t"] or now) + 120
        pts = [[round(t - s.created, 1), round(v, 2)] for t, v in s.samples
               if d["start_t"] - 90 <= t <= end]
        return decimate(pts, 300)

    def snapshot(self) -> dict:
        now = self.clock.now()
        s = self.session
        mono = time.monotonic()
        out = {
            "sim": self.sim,
            "speed": self.clock.speed,
            "conn": dict(self.conn),
            "tel": {
                "temp_c": self.tel["temp_c"] if self.adapter.connected else None,
                "water_low": bool(self.tel["water_low"]) and self.adapter.connected,
                "age_s": None if self.tel["wall"] is None else round(mono - self.tel["wall"], 1),
            },
            "events": self.events[-20:],
            "last_event_id": self._eid,
            "volume": self.settings["volume"],
            "session": None,
        }
        if s is not None:
            s.accrue(now)
            elapsed = s.elapsed
            sd = {
                "phase": s.phase, "paused": s.paused, "pause_src": s.pause_src,
                "name": s.name, "emoji": s.emoji, "manual": s.manual, "frozen": s.frozen,
                "pot": s.pot, "liters": s.liters, "target_c": s.target_c,
                "total_s": s.total_s, "base_s": s.base_s, "extra_s": s.extra_s,
                "elapsed_s": elapsed,
                "remaining_s": max(0.0, s.total_s - elapsed),
                "overtime_s": max(0.0, elapsed - s.total_s),
                "temp_c": s.last_temp if self.adapter.connected else None,
                "lost": s.lost_since is not None and s.phase in LIVE_PHASES,
                "motor_detect": s.motor_seen_running and self.settings["detect_pause_from_motor"],
                "chart": self._chart(s, now),
                "dip_chart": self._dip_chart(s, now),
                "end_reason": s.end_reason,
                "session_s": (s.ended_t or now) - s.created,
            }
            p = s.pre
            if p["start_t"] is not None:
                span = max(0.1, s.target_c - p["start_temp"])
                cur = s.last_temp if s.last_temp is not None else p["start_temp"]
                sd["pre"] = {
                    "start_temp": p["start_temp"],
                    "eta_s": None if p["eta_abs"] is None else max(0.0, p["eta_abs"] - now),
                    "eta_src": p["eta_src"],
                    "progress": max(0.0, min(1.0, (cur - p["start_temp"]) / span)),
                    "duration_s": ((s.ready_t or now) - p["start_t"]),
                }
            d = s.dip
            if d:
                sd["dip"] = {
                    "active": d["active"], "low_c": round(d["low_c"], 2),
                    "depth_c": round(s.target_c - d["low_c"], 2),
                    "recovery_s": (d["rec_t"] or now) - d["start_t"],
                    "low_after_s": d["low_t"] - d["start_t"],
                }
            out["session"] = sd
        return out

    # ---- simulator hooks ---------------------------------------------------
    def sim_action(self, name):
        if not self.sim:
            raise ApiError("Simulator is not enabled", 404)
        a = self.adapter
        if name == "food":
            a.add_food(1.0)
        elif name == "lowwater":
            a.low = True
        elif name == "refill":
            a.low = False
        elif name == "unplug":
            a.connected = False
        else:
            raise ApiError("unknown sim action")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def make_web_app(app_state: App, api_token: str | None = None) -> web.Application:
    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=e.status)

    async def index(_):
        return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-store"})

    async def state(_):
        return web.json_response(app_state.snapshot())

    async def settings_get(_):
        return web.json_response(app_state.settings)

    async def settings_put(request):
        try:
            body = await request.json()
        except Exception:
            raise ApiError("Bad JSON")
        app_state.settings = sanitize_settings(body)
        save_settings(app_state.settings)
        return web.json_response(app_state.settings)

    async def start(request):
        app_state.start_session(await request.json())
        return web.json_response({"ok": True})

    async def action(request):
        body = await request.json()
        await app_state.action(str(body.get("action", "")))
        return web.json_response({"ok": True})

    async def sim(request):
        app_state.sim_action(request.match_info["name"])
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[errors])
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/api/settings", settings_get),
        web.put("/api/settings", settings_put),
        web.post("/api/session/start", start),
        web.post("/api/session/action", action),
        web.post("/api/sim/{name}", sim),
    ])

    api_v1.install(app, app_state, sys.modules[__name__], token=api_token)

    async def on_start(app_):
        app_["tasks"] = [asyncio.create_task(app_state.conn_loop()),
                         asyncio.create_task(app_state.tick_loop())]

    async def on_cleanup(app_):
        for t in app_["tasks"]:
            t.cancel()
        await app_state.adapter.disconnect()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    ap = argparse.ArgumentParser(description="Sous Vide Assistant")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from other devices (no auth!)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--sim", action="store_true", help="simulated cooker (no Bluetooth needed)")
    ap.add_argument("--speed", type=float, default=1.0, help="simulator time multiplier")
    ap.add_argument("--api-token", default=os.environ.get("SOUSVIDE_API_TOKEN"),
                    help="require this bearer token on /api/v1/* (or set SOUSVIDE_API_TOKEN)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    lib = None if a.sim else load_library()
    state = App(a.sim, a.speed, lib)
    print(f"Sous Vide Assistant on http://{a.host}:{a.port}  ({'SIMULATOR x%g' % a.speed if a.sim else 'real cooker'})")
    print(f"Scripting API on http://{a.host}:{a.port}/api/v1/ "
          f"({'bearer token required' if a.api_token else 'NO AUTH'})")
    web.run_app(make_web_app(state, a.api_token), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
