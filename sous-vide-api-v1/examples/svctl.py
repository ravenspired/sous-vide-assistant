#!/usr/bin/env python3
"""
svctl - tiny command-line client for the Sous Vide Assistant scripting API.
Standard library only.

  export SOUSVIDE_URL=http://192.168.1.50:8080     # default http://127.0.0.1:8080
  export SOUSVIDE_API_TOKEN=...                    # only if the server uses --api-token

  svctl.py status                       one-line summary (add --json for the raw object)
  svctl.py watch                        live stream (status + chime events)
  svctl.py start "Chicken Breast" --pot small --water-ok --auto-start
  svctl.py start --manual 63 45 --pot medium --water-ok
  svctl.py act food_in                  water_ok|start|cooker_started|pause|resume|food_in|stop|cancel|dismiss
  svctl.py log temps.csv                append temperature samples until the cook is dismissed (Ctrl-C to quit)
  svctl.py settings                     print settings JSON
  svctl.py set volume=0.5 power_w=750   change global settings
  svctl.py preset f3 time_min=100 temp_c=55
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

BASE = os.environ.get("SOUSVIDE_URL", "http://127.0.0.1:8080").rstrip("/") + "/api/v1"
TOKEN = os.environ.get("SOUSVIDE_API_TOKEN")


def req(method, path, body=None, stream=False):
    r = urllib.request.Request(BASE + path, method=method,
                               data=None if body is None else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    if TOKEN:
        r.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        resp = urllib.request.urlopen(r, timeout=None if stream else 15)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", e.reason)
        except Exception:
            msg = e.reason
        sys.exit(f"error {e.code}: {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {BASE}: {e.reason}")
    return resp if stream else json.loads(resp.read().decode() or "{}")


def mmss(s):
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def line(st):
    c, k = st["cook"], st["cooker"]
    t = "--" if k["water_temp_c"] is None else f"{k['water_temp_c']:.1f}C"
    if not c:
        return f"idle  water {t}  link {st['connection']['state']}"
    extra = ""
    if c["phase"] == "preheat" and c["preheat"] and c["preheat"]["eta_s"] is not None:
        extra = f"  eta {mmss(c['preheat']['eta_s'])} ({c['preheat']['eta_source']})"
    elif c["phase"] == "cook":
        extra = f"  left {mmss(c['remaining_s'])}"
    elif c["phase"] == "done":
        extra = f"  overtime +{mmss(c['overtime_s'])}"
    pause = " PAUSED" if c["paused"] else ""
    return (f"{c['phase']}{pause}  {c['name']}  water {t} -> {c['target_c']}C{extra}"
            f"  [{', '.join(c['available_actions'])}]")


def kv(items):
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    sub.add_parser("watch")
    p = sub.add_parser("start")
    p.add_argument("preset", nargs="?"); p.add_argument("--manual", nargs=2, metavar=("TEMP_C", "MINUTES"), type=float)
    p.add_argument("--liters", type=float); p.add_argument("--pot", required=True)
    p.add_argument("--frozen", action="store_true"); p.add_argument("--water-ok", action="store_true")
    p.add_argument("--auto-start", action="store_true")
    p = sub.add_parser("act"); p.add_argument("action")
    p = sub.add_parser("log"); p.add_argument("file")
    sub.add_parser("settings")
    p = sub.add_parser("set"); p.add_argument("pairs", nargs="+")
    p = sub.add_parser("preset"); p.add_argument("id"); p.add_argument("pairs", nargs="*")
    a = ap.parse_args()

    if a.cmd == "status":
        st = req("GET", "/status")
        print(json.dumps(st, indent=2) if a.json else line(st))
    elif a.cmd == "watch":
        resp = req("GET", "/stream", stream=True)
        ev = None
        for raw in resp:
            raw = raw.decode().rstrip("\n")
            if raw.startswith("event:"):
                ev = raw[6:].strip()
            elif raw.startswith("data:"):
                d = json.loads(raw[5:])
                print(line(d) if ev == "status" else f"*** event: {d['type']}", flush=True)
    elif a.cmd == "start":
        body = {"pot": a.pot, "frozen": a.frozen, "confirm_water": a.water_ok, "auto_start": a.auto_start}
        if a.manual:
            body["manual"] = {"temp_c": a.manual[0], "time_min": a.manual[1]}
            if a.liters:
                body["manual"]["liters"] = a.liters
        elif a.preset:
            body["preset"] = a.preset
        else:
            sys.exit("give a preset name/id or --manual TEMP MINUTES")
        print(line(req("POST", "/cook", body)["status"]))
    elif a.cmd == "act":
        print(line(req("POST", f"/cook/actions/{a.action}")["status"]))
    elif a.cmd == "log":
        since, new = -1.0, not os.path.exists(a.file)
        with open(a.file, "a", buffering=1) as f:
            if new:
                f.write("t_s,timestamp,temp_c\n")
            try:
                while True:
                    try:
                        h = urllib.request.urlopen(urllib.request.Request(
                            f"{BASE}/history?format=csv&since={since}",
                            headers={"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}), timeout=15)
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            print("cook ended; log closed"); break
                        raise
                    rows = h.read().decode().splitlines()[1:]
                    for r in rows:
                        f.write(r + "\n")
                        since = float(r.split(",")[0])
                    time.sleep(5)
            except KeyboardInterrupt:
                pass
    elif a.cmd == "settings":
        print(json.dumps(req("GET", "/settings"), indent=2, ensure_ascii=False))
    elif a.cmd == "set":
        print(json.dumps(req("PATCH", "/settings", kv(a.pairs)), indent=2, ensure_ascii=False))
    elif a.cmd == "preset":
        r = req("PATCH", f"/presets/{a.id}", kv(a.pairs)) if a.pairs else req("GET", f"/presets/{a.id}")
        print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
