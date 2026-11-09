"""Where lanecast keeps its settings, and the one way to read and write them.

    <home>/secrets.env              KEY=value lines: IG_TOKEN_<ACCOUNT>, GITHUB_TOKEN, ...
    <home>/config.json              this install: its accounts, the outbox repo, usage stats on/off
    <home>/accounts/<account>.json  each account's dashboard settings (caption, ...)
    <home>/media/                   files uploaded in the dashboard, waiting to be posted
    <home>/logs/                    logs of long jobs (one file each)

<home> is ~/.lanecast unless configure() points it elsewhere (a plugin can keep
its own folder, e.g. ~/.illitlauver). The worker (token refreshes) and the
dashboard (pasted tokens, captions) both write these, from different threads of
the same process, so every write takes LOCK and replaces the file in one step.

laneq is shared with other apps, so every lane is named "<app>/<account>", plus
side lanes "<app>/<account>:<side>" (e.g. ":tokens" for token renewals) that
never hold up the posts.

Modules read these through the module (store.HOME), never `from store import
HOME`, so configure() reaches them.
"""
import json
import os
import re
import threading
from pathlib import Path

LOCK = threading.Lock()
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

HOME = SECRETS = ACCOUNTS = MEDIA = LOGS = CONFIG = None
APP = "lanecast"


def configure(home=None, app=None):
    """Point lanecast at a data folder and a laneq app name (both have defaults)."""
    global HOME, SECRETS, ACCOUNTS, MEDIA, LOGS, CONFIG, APP
    HOME = Path(home or os.environ.get("LANECAST_HOME") or Path.home() / ".lanecast")
    SECRETS, ACCOUNTS, MEDIA = HOME / "secrets.env", HOME / "accounts", HOME / "media"
    LOGS, CONFIG = HOME / "logs", HOME / "config.json"
    APP = app or os.environ.get("LANECAST_APP") or "lanecast"


configure()


def lane(account, side=""):
    return f"{APP}/{account}" + (f":{side}" if side else "")


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def get(path, key):
    """The value stored at key in a .env or .json file; "" if it isn't there."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    if path.suffix.lower() == ".json":
        v = json.loads(text or "{}").get(key, "")
        return v if isinstance(v, str) else ""
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == key:
            return v.strip()
    return ""


def put(path, key, value):
    """Store a value, keeping every other key. .env values must be one line."""
    path = Path(path)
    with LOCK:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            text = ""
        if path.suffix.lower() == ".json":
            data = json.loads(text or "{}")
            data[key] = value
            _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            return
        if "\n" in value or "\r" in value:
            raise ValueError("must be one line")
        lines, found = text.splitlines(), False
        for i, line in enumerate(lines):
            if line.partition("=")[0].strip() == key and "=" in line:
                lines[i], found = f"{key}={value}", True
        if not found:
            lines.append(f"{key}={value}")
        _atomic_write(path, "\n".join(lines) + "\n")


def secret(key):
    """A secret from the environment or secrets.env; "" if it isn't set."""
    return os.environ.get(key, "").strip() or get(SECRETS, key)


def settings(account):
    """An account's dashboard settings (caption, ...)."""
    try:
        return json.loads((ACCOUNTS / f"{account}.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def config():
    """This install's config.json ({} until something is saved)."""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def set_config(**fields):
    with LOCK:
        data = config()
        data.update(fields)
        _atomic_write(CONFIG, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def accounts():
    """[{"id", "title"}] in the order they were added."""
    return [a for a in config().get("accounts", []) if ACCOUNT_ID.fullmatch(a.get("id", ""))]


def add_account(account_id, title=""):
    account_id = (account_id or "").strip().lower()
    if not ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("an account id is lowercase letters, digits, - or _ (up to 32)")
    with LOCK:
        data = config()
        listed = data.setdefault("accounts", [])
        if any(a["id"] == account_id for a in listed):
            raise ValueError(f"there's already an account {account_id!r}")
        listed.append({"id": account_id, "title": (title or "").strip() or account_id})
        _atomic_write(CONFIG, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data["accounts"]
