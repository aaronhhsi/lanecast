"""Talking to laneq: the HTTP client, a leased job (heartbeats + checkpoints), and
the app check-in that lets laneq's own dashboard stop or kill this worker."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from . import store

LEASE = 60  # seconds; renewed every LEASE/4 while a job runs


def say(msg):
    """A line in the worker's log (the dashboard's Log tab shows it live)."""
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def tz_offset(t):
    off = -time.altzone if t.tm_isdst > 0 else -time.timezone
    return f"{'+' if off >= 0 else '-'}{abs(off) // 3600:02d}:{abs(off) % 3600 // 60:02d}"


def rfc3339(t):
    """A time.struct_time (local) as laneq's run_at."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + tz_offset(t)


class Permanent(Exception):
    """Retrying won't help (bad media, revoked token); a person needs to look."""


class RetryAfter(Exception):
    def __init__(self, msg, seconds):
        super().__init__(msg)
        self.seconds = seconds


class LeaseLost(Exception):
    """The queue gave this job to someone else; stop without side effects."""


class Laneq:
    def __init__(self, server="http://127.0.0.1:7070"):
        self.server = server.rstrip("/")

    def call(self, method, path, body=None, timeout=90):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": raw.decode(errors="replace")}

    def claim(self, worker, types, wait=30):
        code, body = self.call("POST", "/v1/claim", {"worker": worker, "types": types,
                                                    "lease_seconds": LEASE, "wait_seconds": wait})
        if code == 204:
            return None
        if code != 200:
            raise RuntimeError(f"claim failed ({code}): {body}")
        return body


class Job:
    """A leased job: heartbeats in the background and saves checkpoints."""

    def __init__(self, laneq, lease):
        self.q = laneq
        self.job = lease["job"]
        self.id = self.job["id"]
        self.token = lease["token"]
        self.checkpoint = self.job.get("checkpoint") or {}
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()

    @property
    def payload(self):
        return self.job.get("payload") or {}

    def _heartbeat(self, checkpoint=None):
        with self._lock:
            body = {"token": self.token, "lease_seconds": LEASE}
            if checkpoint is not None:
                body["checkpoint"] = checkpoint
            code, resp = self.q.call("POST", f"/v1/jobs/{self.id}/heartbeat", body)
            if code == 409:
                self.lost.set()
            return code

    def _beat(self):
        while not self._stop.wait(LEASE / 4):
            try:
                self._heartbeat()
            except OSError:
                pass  # the server may be restarting; the lease has slack
            if self.lost.is_set():
                return

    def ensure_lease(self):
        if self.lost.is_set():
            raise LeaseLost(f"job {self.id}: lease lost")

    def save(self, **fields):
        """Merge fields into the checkpoint and make it durable before going on."""
        self.ensure_lease()
        self.checkpoint = {**self.checkpoint, **fields}
        if self._heartbeat(self.checkpoint) != 200:
            self.ensure_lease()
            raise RuntimeError(f"job {self.id}: could not save checkpoint")

    def finish(self, path, body):
        self._stop.set()
        code, resp = self.q.call("POST", f"/v1/jobs/{self.id}/{path}", {"token": self.token, **body})
        if code == 409:
            raise LeaseLost(f"job {self.id}: lease lost before {path}")
        if code != 200:
            raise RuntimeError(f"{path} failed ({code}): {resp}")

    def complete(self, result):
        self.finish("complete", {"result": result})

    def fail(self, error, retry_after=0, permanent=False):
        self.finish("fail", {"error": error[:2000], "retry_after_seconds": retry_after, "permanent": permanent})


class AppLink:
    """Checks in with laneq every few seconds as this app, so laneq's dashboard shows
    it and can stop it. Asked to stop, the worker exits at once if it's idle, or right
    after the job it's on."""

    def __init__(self, laneq, title, dashboard="", start_hint=""):
        self.q, self.stop, self.busy = laneq, threading.Event(), False
        self.info = {"title": title, "pid": os.getpid(), "start_hint": start_hint}
        if dashboard:
            self.info["dashboard"] = f"http://{dashboard}"
        threading.Thread(target=self._beat, daemon=True, name="applink").start()

    def check_in(self):
        try:
            code, body = self.q.call("POST", f"/v1/apps/{store.APP}/heartbeat", self.info, timeout=10)
        except OSError:
            return  # laneq is down or restarting; keep going
        if code == 200 and body and body.get("stop"):
            self.stop.set()

    def _beat(self):
        while True:
            self.check_in()
            if self.stop.is_set() and not self.busy:
                self.exit()
            time.sleep(5)

    def exit(self):
        print("stopping: laneq asked this app to stop", flush=True)
        try:
            self.q.call("POST", f"/v1/apps/{store.APP}/bye", {"pid": os.getpid()}, timeout=5)
        except OSError:
            pass
        sys.stdout.flush()
        os._exit(0)  # 0: a clean stop, so a service manager doesn't restart it
