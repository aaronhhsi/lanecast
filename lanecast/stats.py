"""Usage stats: a few totals sent once a day, so the project can show how much it's used.

On by default, shown in Settings → Usage stats (with exactly what's sent), and off
with one click there, `"usage_stats": false` in config.json, or LANECAST_USAGE_STATS=off.
A report is {install_id, kind, version, totals}: a random id made on first run and
numbers only. Never account names, captions, files or tokens.
"""
import json
import os
import secrets
import threading
import time
import urllib.request

from . import __version__, store

STATS_URL = "https://bpplyuckjuiuuyazcvzi.supabase.co/functions/v1/stats"  # laneq's usage-stats function (laneq/usage-stats)
EXPLAIN = ("Once a day this install sends a random install id, the lanecast version, and four numbers: accounts, "
           "posts published, posts waiting and media stored. Never account names, captions, files or tokens. "
           "It shows how much lanecast is used, across everyone who runs it.")
_laneq = None
_plugin = None


def enabled():
    return store.config().get("usage_stats", True) is not False and os.environ.get("LANECAST_USAGE_STATS") != "off"


def set_enabled(on):
    store.set_config(usage_stats=bool(on))


def install_id():
    path = store.HOME / "install-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) == 32:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_hex(16)
    store.HOME.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def _folder_bytes(path):
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        total += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return total


def totals(plugin):
    """Accounts, posts published and waiting (from laneq's lane counts), and media stored."""
    listed = plugin.accounts()
    accounts = listed if listed is not None else store.accounts()
    published = waiting = 0
    laneq = _laneq
    if laneq is None:  # the dashboard on its own, without the worker
        from .dashboard import Handler
        from .queue import Laneq
        laneq = Laneq(Handler.laneq)
    if laneq is not None:
        try:
            _, lanes = laneq.call("GET", "/v1/lanes", timeout=15)
        except OSError:
            lanes = []
        prefix = store.APP + "/"
        for lane in lanes or []:
            name = lane["name"]
            side = name.partition(":")[2]
            if name.startswith(prefix) and side in ("", "reels"):  # the lanes posts go up from
                counts = lane.get("counts") or {}
                published += counts.get("succeeded", 0)
                waiting += counts.get("held", 0) + counts.get("queued", 0) + counts.get("running", 0)
    extra = plugin.stats() or {}
    return {"accounts": len(accounts), "posts_published": published, "posts_queued": waiting,
            "media_bytes": _folder_bytes(store.MEDIA) + int(extra.get("media_bytes", 0))}


def report(plugin):
    return {"install_id": install_id(), "kind": "lanecast", "version": __version__, "totals": totals(plugin)}


def view(plugin):
    """What Settings → Usage stats shows."""
    out = {"enabled": enabled(), "explain": EXPLAIN, "endpoint": STATS_URL or None,
           "last_sent": store.config().get("usage_last_sent")}
    try:
        payload = report(plugin)
        out.update(totals=payload["totals"], payload=payload)
    except Exception as e:  # noqa: BLE001 - the panel still shows the switch
        out["error"] = str(e)
    return out


def send(plugin):
    body = json.dumps(report(plugin)).encode()
    req = urllib.request.Request(STATS_URL, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    store.set_config(usage_last_sent=time.time())


def start(laneq, plugin, mode):
    """Keep laneq and the plugin for totals; send a report a minute from now, then daily."""
    global _laneq, _plugin
    _laneq, _plugin = laneq, plugin
    if not STATS_URL or mode != "live":
        return
    from .queue import say
    if enabled():
        say("usage stats: sending daily totals (accounts, posts, media size; no names or content); "
            "turn off in Settings → Usage stats")

    def loop():
        time.sleep(60)
        while True:
            last = store.config().get("usage_last_sent") or 0
            if enabled() and time.time() - last > 23 * 3600:
                try:
                    send(plugin)
                except Exception as e:  # noqa: BLE001 - never affects posting
                    say(f"usage stats not sent ({e}); trying again later")
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="usage-stats").start()
