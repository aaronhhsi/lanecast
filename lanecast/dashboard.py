"""The lanecast dashboard: http://127.0.0.1:7080

One tab per account (its caption, the posts going up, the nightly schedule, adding
posts), Settings (tokens, the outbox repo, usage stats) and Log. The page is
web/index.html; a plugin can add a script (/plugin.js) and routes of its own.

  GET  /                                 the page
  GET  /plugin.js                        the plugin's page script (empty without one)
  GET  /api/dashboard                    accounts, their settings and setup status (never a secret's value)
  POST /api/accounts                     add an account                    {id, title}
  POST /api/upload?name=<file>           upload a photo or video (the body is the file)
  POST /api/accounts/<id>/posts          queue a post                      {media: [{path}], caption}
  GET  /media/<file>                     an uploaded file (byte ranges, so videos can seek)
  POST /api/tabs/<lane>/fields/<i>       save an account setting           {value}
  POST /api/tabs/<lane>/setup/<i>/value  save a setup value (e.g. a token) {value}
  POST /api/tabs/<lane>/setup/<i>/open   open a setup item's folder or file on this PC
  POST /api/settings/fields/<i>          save a shared setting             {value}
  GET  /api/stats  POST /api/stats       usage totals, and turning the usage report on/off {enabled}
  GET  /api/worker-log                   the end of the worker's own log
  GET  /api/logs  GET /api/logs/<name>   the logs there are, and the end of one
  *    /v1/...                           passed through to laneq (the queue)

It listens on localhost only and refuses writes from pages on other sites (the
browser sends their Origin).
"""
import json
import mimetypes
import shutil
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import instagram, posts, store
from .plugin import Plugin

PAGE = Path(__file__).resolve().parent / "web" / "index.html"
PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "X-Total-Count",
                "Last-Modified", "Cache-Control")
PLUGIN = Plugin()


class NotFound(Exception):
    pass


# --- what the page shows: tabs and settings ---------------------------------------

def accounts():
    listed = PLUGIN.accounts()
    return listed if listed is not None else store.accounts()


def tab(acc):
    account, title = acc["id"], acc.get("title") or acc["id"]
    settings_file = str(store.ACCOUNTS / f"{account}.json")
    return PLUGIN.tab(account, {
        "lane": store.lane(account), "title": title, "account": account,
        "fields": [{"label": "Default caption", "file": settings_file, "key": "caption", "multiline": True,
                    "placeholder": PLUGIN.default_caption(account),
                    "help": "Used for every post without a caption of its own. Applies to posts already queued too."}],
        "actions": [],
        "setup": [{"label": "Instagram token", "file": str(store.SECRETS), "key": instagram.token_key(account),
                   "settings": True,
                   "help": "The account's Instagram API token (see the README). It's saved to secrets.env and never shown again."}],
    })


def settings_config():
    return PLUGIN.settings({
        "fields": [
            {"label": "Outbox repo", "file": str(store.CONFIG), "key": "outbox_repo", "section": "Outbox",
             "placeholder": PLUGIN.outbox_repo or "your-name/outbox",
             "help": "A public GitHub repo (owner/name) that Instagram downloads each post's files from. "
                     "It holds only the post going up, and is emptied after every post."},
            {"label": "GitHub token", "file": str(store.SECRETS), "key": "GITHUB_TOKEN", "secret": True, "section": "Outbox",
             "optional": True,
             "help": "Allowed to push to the outbox repo. Leave empty to use the GitHub CLI's login (gh auth login)."},
        ],
        "actions": [],
    })


def config():
    return {"name": PLUGIN.title, "app": store.APP, "tabs": [tab(a) for a in accounts()],
            "settings": settings_config(), "accounts_editable": PLUGIN.accounts() is None}


def is_set(path, key):
    return bool(store.get(path, key).strip())


def field_status(f):
    v = store.get(f["file"], f["key"])
    out = {k: f[k] for k in ("label", "help", "section", "secret", "multiline", "placeholder", "settings", "optional") if k in f}
    out["set"] = bool(v.strip())
    if not f.get("secret"):
        out["value"] = v
    return out


def check_done(c):
    if "key" in c:
        return is_set(c["file"], c["key"])
    p = Path(c["path"])
    return p.is_file() or (p.is_dir() and any(p.iterdir()))


def check_status(c):
    out = {k: c[k] for k in ("label", "help", "command", "job", "settings") if k in c}
    out["done"] = check_done(c)
    out["action"] = "paste" if "key" in c else "file" if Path(c.get("open", c.get("path", ""))).is_file() else "folder"
    return out


def action_status(a):
    out = {k: a[k] for k in ("label", "help", "confirm", "jobs") if k in a}
    out["missing"] = [r["label"] for r in a.get("requires", []) if not is_set(r["file"], r["key"])]
    return out


def dashboard():
    cfg = config()
    tabs = []
    for t in cfg["tabs"]:
        setup = [check_status(c) for c in t.get("setup", [])]
        tabs.append({"lane": t["lane"], "title": t["title"], "account": t["account"],
                     "fields": [field_status(f) for f in t.get("fields", [])],
                     "setup": setup, "actions": [action_status(a) for a in t.get("actions", [])],
                     "ready": all(c["done"] for c in setup)})
    s = cfg["settings"]
    return {"name": cfg["name"], "app": cfg["app"], "tabs": tabs, "accounts_editable": cfg["accounts_editable"],
            "plugin": PLUGIN.page_extra() is not None, **PLUGIN.dashboard_extra(),
            "setup": {"guide": PLUGIN.guide, "outbox": bool(store.config().get("outbox_repo") or PLUGIN.outbox_repo),
                      "gh": shutil.which("gh") is not None, "home": str(store.HOME)},
            "settings": {"fields": [field_status(f) for f in s["fields"]], "actions": [action_status(a) for a in s["actions"]]}}


def item(items, i):
    if not 0 <= i < len(items):
        raise NotFound
    return items[i]


def find_tab(lane):
    for t in config()["tabs"]:
        if t["lane"] == lane:
            return t
    raise NotFound


def save(f, value):
    value = value if f.get("multiline") and not f.get("secret") else value.strip()
    store.put(f["file"], f["key"], value)


def save_check(c, value):
    if "key" not in c:
        raise NotFound
    value = value.strip()
    if not value:
        raise ValueError("empty")
    # An Instagram token is ~160 letters and digits starting "IG"; catch a pasted link or a partial copy.
    if c["key"].startswith("IG_TOKEN_") and not re.fullmatch(r"IG[A-Za-z0-9_-]{60,}", value):
        raise ValueError("that doesn't look like an Instagram token (they start with IG and are about 160 "
                         "characters). Copy it again from Generate token")
    store.put(c["file"], c["key"], value)


def open_item(c):
    target = Path(c.get("open") or c.get("file") or c["path"])
    if target.is_file():
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(target)])  # .env and .json have no default app
        return
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(target)  # Explorer


# --- logs ----------------------------------------------------------------------------

PROGRESS = re.compile(r"^(.*?):\s*(\d{1,3})%\s*$")  # "<spinner> Members / moka: 73%"
LOG_NAME = re.compile(r"[a-z]+-[a-z0-9_-]+-\d{8}-\d{6}\.log")  # <kind>-<account>-<stamp>.log


def read_lines(path, max_bytes=512 << 10):
    """The end of a log. Our scripts write UTF-8; some tools write the console's code page."""
    with path.open("rb") as f:  # Python opens files shared, so this works while the worker appends
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    out = []
    for raw in data.split(b"\n"):
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            line = raw.decode("cp437", errors="replace")
        out.append(line.rstrip("\r"))
    return out[1:] if size > max_bytes else out  # the first line was cut


def collapse(lines):
    """Keep only the latest of each run of progress lines, so the log reads like a terminal would show it."""
    out = []
    for line in lines:
        m = PROGRESS.match(line)
        if m and out and (p := PROGRESS.match(out[-1])) and p[1] == m[1]:
            out[-1] = line
        else:
            out.append(line)
    return out


def worker_log_path():
    return store.HOME / "worker.log"


def worker_log():
    path = worker_log_path()
    return {"path": str(path), "lines": read_lines(path, 128 << 10)[-200:] if path.exists() else []}


def log_list():
    files = []
    for p in store.LOGS.glob("*.log") if store.LOGS.exists() else []:
        if LOG_NAME.fullmatch(p.name):
            kind, account = p.stem.rsplit("-", 2)[0].split("-", 1)
            st = p.stat()
            files.append({"name": p.name, "kind": kind, "member": account, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda f: -f["mtime"])
    w = worker_log_path()
    return {"worker": w.stat().st_mtime if w.exists() else None, "extra": PLUGIN.extra_logs(), "files": files[:60]}


def log_view(name):
    extra = PLUGIN.read_log(name)
    if extra is not None:
        return {"name": name, **extra}
    path = worker_log_path() if name == "worker" else store.LOGS / name if LOG_NAME.fullmatch(name) else None
    if not path or not path.exists():
        raise NotFound
    return {"name": name, "path": str(path), "lines": collapse(read_lines(path, 384 << 10))[-2000:],
            "mtime": path.stat().st_mtime}


# --- HTTP ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    laneq = "http://127.0.0.1:7070"

    def log_message(self, *args):  # the worker's log is for jobs, not page loads
        pass

    def reply(self, code, body=None, ctype="application/json"):
        data = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        if data:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def same_site(self):
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host')}"

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path, query = urllib.parse.unquote(url.path), urllib.parse.parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/plugin.js":
                extra = PLUGIN.page_extra()
                return self.reply(200, Path(extra).read_bytes() if extra else b"/* no plugin */", "text/javascript; charset=utf-8")
            if path == "/api/dashboard":
                return self.reply(200, dashboard())
            if path == "/api/worker-log":
                return self.reply(200, worker_log())
            if path == "/api/logs":
                return self.reply(200, log_list())
            if m := re.fullmatch(r"/api/logs/([a-z0-9._-]+)", path):
                return self.reply(200, log_view(m[1]))
            if path == "/api/stats":
                from . import stats
                return self.reply(200, stats.view(PLUGIN))
            if m := re.fullmatch(r"/media/([^/]+)", path):
                root = store.MEDIA.resolve()
                file = (root / m[1]).resolve()
                if file.parent != root or not file.is_file():
                    raise NotFound
                return self.send_file(file, cache="max-age=86400")
            if path == "/healthz":
                return self.reply(200, {"status": "ok"})
            if path.startswith("/v1/"):
                return self.proxy()
            if PLUGIN.get(self, path, query):
                return
        except NotFound:
            return self.reply(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - report it to the page
            return self.reply(500, {"error": str(e)})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.same_site():
            return self.reply(403, {"error": "only accepted from this dashboard"})
        if self.path.startswith("/v1/"):
            return self.proxy(self.body())
        body = self.body()
        try:
            if not self.api_post(body) and not PLUGIN.post(self, self.path, body):
                self.reply(404, {"error": "not found"})
        except NotFound:
            self.reply(404, {"error": "no such item"})
        except (ValueError, posts.Invalid) as e:
            self.reply(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            self.reply(502, {"error": f"laneq refused it: {e.read().decode(errors='replace')}"})
        except OSError as e:
            self.reply(502, {"error": f"laneq isn't answering ({e})"})
        except Exception as e:  # noqa: BLE001 - report it to the page
            self.reply(500, {"error": str(e)})

    def do_PUT(self):
        if not self.same_site():
            return self.reply(403, {"error": "only accepted from this dashboard"})
        if self.path.startswith("/v1/"):
            return self.proxy(self.body())
        self.reply(404, {"error": "not found"})

    def api_post(self, body):
        """The dashboard's own POST routes; False if the path isn't one."""
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        if path == "/api/upload":
            self.reply(201, posts.save_upload(query.get("name", [""])[0], body))
            return True
        data = json.loads(body or b"{}") if path.startswith("/api/") else {}
        value = lambda: str(data.get("value", ""))  # noqa: E731
        if path == "/api/accounts":
            if PLUGIN.accounts() is not None:
                raise ValueError("this install's accounts are set by its plugin")
            store.add_account(data.get("id"), data.get("title"))
            self.reply(201, {"ok": True})
            return True
        if m := re.fullmatch(r"/api/accounts/([a-z0-9_-]+)/posts", path):
            if not any(a["id"] == m[1] for a in accounts()):
                raise NotFound
            self.reply(201, posts.add_post(self.laneq, m[1], data.get("media") or [], data.get("caption") or ""))
            return True
        if path == "/api/stats":
            from . import stats
            stats.set_enabled(bool(data.get("enabled")))
            self.reply(200, stats.view(PLUGIN))
            return True
        if m := re.fullmatch(r"/api/settings/fields/(\d+)", path):
            save(item(settings_config()["fields"], int(m[1])), value())
        # A tab is named by its lane ("app/account"), which the page sends encoded as one segment.
        elif m := re.fullmatch(r"/api/tabs/([^/]+)/fields/(\d+)", path):
            save(item(find_tab(urllib.parse.unquote(m[1])).get("fields", []), int(m[2])), value())
        elif m := re.fullmatch(r"/api/tabs/([^/]+)/setup/(\d+)/value", path):
            save_check(item(find_tab(urllib.parse.unquote(m[1])).get("setup", []), int(m[2])), value())
        elif m := re.fullmatch(r"/api/tabs/([^/]+)/setup/(\d+)/open", path):
            open_item(item(find_tab(urllib.parse.unquote(m[1])).get("setup", []), int(m[2])))
        else:
            return False
        self.reply(204)
        return True

    def send_file(self, path, cache="max-age=3600", download=None):
        """A file, with byte ranges so audio and video can seek."""
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (max(0, size - int(m[2])), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
        self.send_response(206 if m and (m[1] or m[2]) else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        if m and (m[1] or m[2]):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(256 << 10, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except ConnectionError:  # broken pipe, reset or aborted (WinError 10053)
            pass  # the browser stopped loading (e.g. a video it no longer needs)

    def proxy(self, data=None):
        """Pass the request to laneq and its answer back."""
        headers = {k: self.headers[k] for k in ("Content-Type", "Range") if self.headers.get(k)}
        req = urllib.request.Request(self.laneq + self.path, data=data, method=self.command, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        except OSError as e:
            return self.reply(502, {"error": f"laneq isn't answering ({e})"})
        with resp:
            self.send_response(resp.status)
            for k in PASS_HEADERS:
                if resp.headers.get(k):
                    self.send_header(k, resp.headers[k])
            self.end_headers()
            try:
                while chunk := resp.read(64 << 10):
                    self.wfile.write(chunk)
            except ConnectionError:
                pass


def serve(addr="127.0.0.1:7080", laneq="http://127.0.0.1:7070", plugin=None):
    """Start the dashboard in a background thread; returns the server."""
    global PLUGIN
    if plugin is not None:
        PLUGIN = plugin
    host, port = addr.rsplit(":", 1)
    Handler.laneq = laneq.rstrip("/")
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
    return srv
