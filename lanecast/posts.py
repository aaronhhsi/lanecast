"""Putting posts in an account's line: uploads, the publish_post job, and the nightly schedule."""
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store

PER_NIGHT = 5  # posts a night for a new account's schedule
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MAX_UPLOAD = 500 << 20


class Invalid(ValueError):
    pass


def kind_of(name):
    ext = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None


def save_upload(name, data):
    """Keep an uploaded photo or video in <home>/media; returns {"path", "type", "url"}."""
    kind = kind_of(name or "")
    if not kind:
        raise Invalid(f"{name!r} isn't a photo or video lanecast can post")
    if not data or len(data) > MAX_UPLOAD:
        raise Invalid("the file is empty or bigger than 500 MB")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[-80:] or "file"
    path = f"{time.strftime('%Y%m%d')}-{secrets.token_hex(4)}-{safe}"
    store.MEDIA.mkdir(parents=True, exist_ok=True)
    (store.MEDIA / path).write_bytes(data)
    return {"path": path, "type": kind, "url": "/media/" + urllib.parse.quote(path)}


def _call(server, method, path, body=None):
    req = urllib.request.Request(server.rstrip("/") + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else b"" if method != "GET" else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def ensure_schedule(server, lane, count=PER_NIGHT):
    """A new account gets a nightly schedule, stopped: nothing posts until Resume posting."""
    path = urllib.parse.quote(lane, safe="")  # "app/account" is one path segment
    _, schedules = _call(server, "GET", "/v1/schedules")
    if any(s["lane"] == lane for s in schedules or []):
        return False
    _call(server, "PUT", f"/v1/schedules/{path}", {"lane": lane, "enabled": True, "at": "00:00", "count": count})
    _call(server, "POST", f"/v1/lanes/{path}/pause")
    return True


def add_post(server, account, media, caption=""):
    """Queue a post at the end of the account's line (held for its nightly schedule)."""
    if not media:
        raise Invalid("add at least one photo or video")
    if len(media) > 10:
        raise Invalid("a carousel can have at most 10 items")
    clean = []
    for m in media:
        path = str(m.get("path", ""))
        if "/" in path or "\\" in path or not (store.MEDIA / path).is_file() or kind_of(path) is None:
            raise Invalid(f"no uploaded file {path!r}")
        clean.append({"path": path, "type": kind_of(path)})
    caption = (caption or "").strip()[:2200]
    kind = "carousel" if len(clean) > 1 else "image" if clean[0]["type"] == "image" else "reel"
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    title = caption.split("\n")[0][:80] if caption else f"Post {time.strftime('%b %d %H:%M')}"
    lane = store.lane(account)
    _, job = _call(server, "POST", "/v1/jobs", {
        "type": "publish_post", "lane": lane, "key": f"{account}:{name}", "hold": True,
        "payload": {"account": account, "name": name, "caption": caption, "media": clean,
                    "preview": {"title": title, "subtitle": f"{len(clean)} items" if len(clean) > 1 else "",
                                "kind": kind, "caption": caption or "(the account's default caption)",
                                "media": [{"url": "/media/" + urllib.parse.quote(m["path"]), "type": m["type"]} for m in clean]}},
    })
    created = ensure_schedule(server, lane)
    return {"job": job, "schedule_created": created}
