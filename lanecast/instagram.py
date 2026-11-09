"""Instagram's publishing API, made safe to retry.

Instagram fetches media from a public URL, so prepared files are force-pushed to a
public GitHub "outbox" repo for the length of one post, then wiped (every push is a
single parentless commit, so earlier posts don't stay in its history).

A publish runs in steps, and the job checkpoints each step's result, so a retry
after a crash or timeout resumes instead of starting over:

  stage     push the media to the outbox
  contain   create Instagram media containers and wait until they're processed  → checkpoint
  publish   publish the container                                               → checkpoint

The queue delivers at least once, so publish must never post twice. Before
publishing it asks Instagram whether the container was already published (a
previous attempt may have died right after publishing), and it won't publish at
all once it has lost its lease: another attempt may own the job by then.
"""
import calendar
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store
from .media import API_MAX_CAROUSEL, PostError, run
from .queue import Permanent, RetryAfter

VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
GRAPH = f"https://graph.instagram.com/{VERSION}"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

# Instagram error codes that mean "slow down", not "broken".
RATE_LIMIT_CODES = {4, 17, 32, 613, 9}
# error_subcode for "Action blocked ... to protect our community" (reported with code 4).
ACTION_BLOCK_SUBCODES = {2207051}
# error_subcode for "media download failed": Instagram couldn't fetch the media URL.
MEDIA_DOWNLOAD_SUBCODES = {2207052}


class IGError(PostError):
    def __init__(self, msg, status, code, subcode=None):
        super().__init__(msg)
        self.status, self.code, self.subcode = status, code, subcode


def token_key(account):
    return f"IG_TOKEN_{account.upper().replace('-', '_')}"


def load_token(account):
    key = token_key(account)
    token = store.secret(key)
    if not token:
        raise Permanent(f"{key} is missing from {store.SECRETS}; paste it in the dashboard's Settings")
    return token


def save_token(account, token):
    store.put(store.SECRETS, token_key(account), token)


class Instagram:
    def __init__(self, token):
        self.token = token
        me = self.call("GET", "me", fields="user_id,username")
        self.uid, self.username = me["user_id"], me.get("username", "?")

    def call(self, method, path, **params):
        params["access_token"] = self.token
        query = urllib.parse.urlencode(params)
        req = (urllib.request.Request(f"{GRAPH}/{path}?{query}") if method == "GET" else
               urllib.request.Request(f"{GRAPH}/{path}", data=query.encode(), method=method))
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace").replace(self.token, "***")
            code = subcode = None
            try:
                err = json.loads(body).get("error", {})
                code, subcode = err.get("code"), err.get("error_subcode")
            except ValueError:
                pass
            raise IGError(f"Instagram {method} /{path} failed ({e.code}): {body}", e.code, code, subcode) from None

    def quota_left(self):
        data = self.call("GET", f"{self.uid}/content_publishing_limit", fields="quota_usage,config")["data"][0]
        return data.get("config", {}).get("quota_total", 25) - data.get("quota_usage", 0)

    def status(self, container):
        return self.call("GET", container, fields="status_code").get("status_code")

    def wait(self, container, minutes=10):
        for _ in range(minutes * 12):
            status = self.call("GET", container, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                info = self.call("GET", container, fields="status").get("status", "")
                raise PostError(f"Instagram could not process media: {status} {info}")
            time.sleep(5)
        raise PostError("timed out waiting for Instagram to process media")

    def newest_media(self):
        data = self.call("GET", f"{self.uid}/media", fields="id", limit=1).get("data", [])
        return data[0]["id"] if data else ""

    def find_published(self, before, since=None):
        """The post this job published, if an earlier attempt got that far.

        Captions can repeat, so they can't identify a post. But posts in a lane go
        up one at a time, so a post newer than `before` (the account's newest post
        just before this job published) can only be this job's. `since` (unix
        seconds) is a fallback for checkpoints saved before `before` was recorded.
        """
        recent = self.call("GET", f"{self.uid}/media", fields="id,permalink,timestamp", limit=5).get("data", [])
        if not recent:
            return None, None
        newest = recent[0]
        if before is not None:
            return (newest["id"], newest.get("permalink", "")) if newest["id"] != before else (None, None)
        ts = calendar.timegm(time.strptime(newest["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))  # UTC
        if since and ts >= since - 5:
            return newest["id"], newest.get("permalink", "")
        return None, None


def github_token():
    """The token the outbox pushes with: GITHUB_TOKEN (env or secrets.env), else the GitHub CLI's login."""
    token = store.secret("GITHUB_TOKEN") or os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise Permanent("no GitHub token for the outbox: set GITHUB_TOKEN in Settings, or log in with `gh auth login`") from None


class Outbox:
    """A public repo holding only the current post's files (history wiped each push)."""

    def __init__(self, workdir, repo):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
            raise Permanent("no outbox repo set: put a public GitHub repo (owner/name) in Settings → Outbox repo")
        self.repo = repo
        self.dir = workdir / "outbox"
        self.dir.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=self.dir)
        run("git", "remote", "add", "origin", f"https://x-access-token:{github_token()}@github.com/{repo}.git", cwd=self.dir)

    def publish(self, files):
        """Replace the outbox with exactly these files; return {name: public url}."""
        self.batch = getattr(self, "batch", 0) + 1
        run("git", "checkout", "-q", "--orphan", f"batch-{self.batch}", cwd=self.dir)
        for p in self.dir.iterdir():
            if p.name != ".git":
                p.unlink()
        (self.dir / "README.md").write_text("Temporary staging for Instagram uploads by lanecast. Emptied after every post.\n")
        for src in files:
            shutil.copyfile(src, self.dir / src.name)
        run("git", "add", "-A", cwd=self.dir)
        run("git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "commit", "-q", "-m", "Staging", cwd=self.dir)
        run("git", "push", "-q", "--force", "origin", "HEAD:main", cwd=self.dir)
        sha = run("git", "rev-parse", "HEAD", cwd=self.dir)
        return {src.name: f"https://raw.githubusercontent.com/{self.repo}/{sha}/{src.name}" for src in files}

    def wipe(self):
        self.publish([])


def reuse_container(ig, job):
    """The container a previous attempt made, if it's still usable."""
    container = job.checkpoint.get("container")
    if not container:
        return None
    status = ig.status(container)
    if status == "IN_PROGRESS":
        ig.wait(container)
        return container
    if status in ("FINISHED", "PUBLISHED"):
        return container
    return None  # EXPIRED (24 h old) or ERROR: start over with fresh containers


def contain(ig, items, urls, caption, job):
    """Create containers and wait until Instagram has processed them."""
    if len(items) > API_MAX_CAROUSEL:
        raise Permanent(f"{len(items)} items; the API allows {API_MAX_CAROUSEL} per carousel")
    # Videos go through the outbox like photos: this API rejects direct (resumable)
    # uploads with "video_url is required".
    if len(items) == 1:
        kind, path = items[0]
        if kind == "image":
            container = ig.call("POST", f"{ig.uid}/media", image_url=urls[path.name], caption=caption)["id"]
        else:
            container = ig.call("POST", f"{ig.uid}/media", media_type="REELS", video_url=urls[path.name],
                                caption=caption)["id"]
    else:
        children = []
        for kind, path in items:
            job.ensure_lease()
            media = {"image_url": urls[path.name]} if kind == "image" else \
                {"media_type": "VIDEO", "video_url": urls[path.name]}
            children.append(ig.call("POST", f"{ig.uid}/media", is_carousel_item="true", **media)["id"])
        for c in children:
            ig.wait(c)
        container = ig.call("POST", f"{ig.uid}/media", media_type="CAROUSEL",
                            children=",".join(children), caption=caption)["id"]
    ig.wait(container)
    job.save(container=container)
    return container


def publish(ig, container, job):
    """Safe to repeat: never posts the same container twice."""
    if job.checkpoint.get("media_id"):
        return job.checkpoint["media_id"], job.checkpoint.get("permalink", "")
    if ig.status(container) == "PUBLISHED":
        # A previous attempt published and died before recording it.
        media_id, permalink = ig.find_published(job.checkpoint.get("before_media"), job.checkpoint.get("publishing_since"))
        if not media_id:
            raise Permanent(f"container {container} is published but its post wasn't found; check the account")
        job.save(media_id=media_id, permalink=permalink)
        return media_id, permalink
    # Durable *before* the side effect: if this attempt dies anywhere past here,
    # the next one finds the post instead of publishing again.
    job.save(publishing_since=time.time(), before_media=ig.newest_media())
    job.ensure_lease()
    try:
        media_id = ig.call("POST", f"{ig.uid}/media_publish", creation_id=container)["id"]
    except IGError as e:
        if e.subcode not in ACTION_BLOCK_SUBCODES:
            raise
        # Seen live, twice: Instagram answers "action blocked" yet publishes the
        # post. Check now (it can take a moment to appear) instead of backing off
        # for hours; only a block that really stopped the post is retried later.
        for _ in range(4):
            time.sleep(5)
            media_id, permalink = ig.find_published(job.checkpoint["before_media"])
            if media_id:
                job.save(media_id=media_id, permalink=permalink)
                return media_id, permalink
        raise
    permalink = ig.call("GET", media_id, fields="permalink").get("permalink", "")
    job.save(media_id=media_id, permalink=permalink)
    return media_id, permalink


def classify(err):
    """Turn an exception into how the queue should treat it."""
    if isinstance(err, IGError):
        if err.code == 190:
            return Permanent(f"Instagram token is invalid or expired; make a new one. ({err})")
        if err.subcode in ACTION_BLOCK_SUBCODES:
            # Instagram's anti-spam "action blocked". Retrying soon can extend it, and
            # the post may have gone up anyway (the retry checks before publishing).
            return RetryAfter(f"Instagram blocked the action for now (anti-spam); waiting 12 h: {err}", 12 * 3600)
        if err.subcode in MEDIA_DOWNLOAD_SUBCODES:
            # Instagram couldn't fetch the media URL (often a just-pushed outbox file); the file is fine.
            return RetryAfter(f"Instagram couldn't download the media; retrying in 10 min: {err}", 600)
        if err.code in RATE_LIMIT_CODES or err.status == 429:
            return RetryAfter(f"rate limited: {err}", 3600)
        if 400 <= err.status < 500:
            return Permanent(str(err))
        return err  # 5xx: transient
    if isinstance(err, PostError) and "could not process media" in str(err):
        return Permanent(str(err))
    return err
