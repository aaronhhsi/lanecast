"""The lanecast worker: takes jobs from laneq and posts to Instagram.

    python -m lanecast [--mode live|rehearse|local] [--once] [--server URL] [--dashboard ADDR] [--log FILE]

Job types:
  publish_post   payload {"account", "caption"?, "media": [{"path", "type"}], "preview"?}:
                 post the files (paths inside <home>/media) as a photo, reel or carousel
  refresh_token  payload {"account"}: renew the account's Instagram token (they last 60 days)
plus whatever a plugin adds. laneq runs each account's jobs one at a time in order,
so the grid stays in the order the posts were queued.

Modes: `live` posts. `rehearse` prepares, stages and makes Instagram containers for
real, then skips publishing (unpublished containers expire on their own). `local`
only prepares the media and checks the token (read-only).
"""
import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import instagram as ig_api
from . import media, store
from .plugin import Plugin
from .queue import AppLink, Job, Laneq, LeaseLost, Permanent, RetryAfter, rfc3339, say

PLUGIN = Plugin()


def account_of(payload):
    """The account a job is for ("member" is an older name for it)."""
    return payload.get("account") or payload.get("member")


def uploaded(m):
    """A file from <home>/media, the dashboard's uploads (nothing outside it)."""
    root = store.MEDIA.resolve()
    path = (root / m["path"]).resolve()
    if path.parent != root or not path.is_file():
        raise Permanent(f"media file missing: {path}")
    return path.read_bytes()


def stage(job, n, text):
    """Log a step, and save it on the job so dashboards can show how far posting got ("2/4 Uploading")."""
    say(f"  {text}")
    job.save(stage=n, stages=4, stage_text=text, stage_at=time.time())


def run_publish(job, mode):
    payload = job.payload
    account = account_of(payload)
    name = (payload.get("post") or {}).get("post") or payload.get("name") or f"post-{job.id}"
    # The post's own caption, else the account's default from the dashboard, else the plugin's.
    caption = (payload.get("caption") or store.settings(account).get("caption") or "").strip() \
        or PLUGIN.default_caption(account)
    ig = ig_api.Instagram(ig_api.load_token(account))

    # A previous attempt got as far as publishing. Instagram can publish and still
    # return an error, and leave the container reporting ERROR rather than
    # PUBLISHED (seen live: code 4 / subcode 2207051), so the container can't be
    # trusted: look for the post itself before doing anything that could repeat it.
    since = job.checkpoint.get("publishing_since")
    if since and not job.checkpoint.get("media_id") and mode == "live":
        media_id, permalink = ig.find_published(job.checkpoint.get("before_media"), since)
        if media_id:
            job.save(media_id=media_id, permalink=permalink)
            PLUGIN.after_publish(job, permalink)
            return {"post": name, "mode": mode, "media_id": media_id, "permalink": permalink,
                    "recovered": "published by an earlier attempt that reported an error"}

    work = Path(tempfile.mkdtemp(prefix="lanecast-post-"))
    try:
        stage(job, 1, f"preparing {name} for {account}")
        items = PLUGIN.prepare_media(job, work)
        if items is None:
            items = media.prepare(name, payload.get("media") or [], work, fetch=uploaded)
        if not items:
            raise Permanent("the post has no photos or videos")
        say(f"  {len(items)} file(s): " + ", ".join(f"{k} {p.name} ({p.stat().st_size >> 10} KB)" for k, p in items))
        summary = {"post": name, "items": [k for k, _ in items], "mode": mode}
        if mode == "local":
            summary["quota_left"] = ig.quota_left()
            return summary

        container = ig_api.reuse_container(ig, job)
        if container:
            stage(job, 3, f"reusing Instagram container {container} from the last attempt")
        else:
            for _, path in items:
                if path.stat().st_size > 95 * 2**20:  # GitHub refuses files over 100 MB
                    raise Permanent(f"{path.name} is {path.stat().st_size >> 20} MB; too big for the outbox")
            outbox = ig_api.Outbox(work, store.config().get("outbox_repo") or PLUGIN.outbox_repo)
            try:
                stage(job, 2, f"uploading to the outbox ({outbox.repo}); Instagram downloads from there")
                urls = outbox.publish([path for _, path in items])
                stage(job, 3, "uploaded; Instagram is fetching and processing the media")
                container = ig_api.contain(ig, items, urls, caption, job)
                say(f"  container {container} ready")
            finally:
                outbox.wipe()
        summary["container"] = container
        if mode == "rehearse":
            return summary

        stage(job, 4, "publishing")
        media_id, permalink = ig_api.publish(ig, container, job)
        PLUGIN.after_publish(job, permalink)
        return {**summary, "media_id": media_id, "permalink": permalink}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_refresh(job, mode):
    account = account_of(job.payload)
    token = ig_api.load_token(account)
    url = "https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": token})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").replace(token, "***")
        raise ig_api.IGError(f"token refresh failed ({e.code}): {body}", e.code, None) from None
    if mode == "live":
        ig_api.save_token(account, data["access_token"])
    return {"member": account, "expires_in_days": round(data.get("expires_in", 0) / 86400, 1), "saved": mode == "live"}


REFRESH_EVERY = 30 * 86400  # a token lasts 60 days; renewing every 30 leaves a month to spare


def account_ids():
    listed = PLUGIN.accounts()
    return [a["id"] for a in (listed if listed is not None else store.accounts())]


def schedule_refreshes(laneq):
    """Keep one token renewal queued per account whose token is set: 30 days after the
    last successful one, or within the hour for a token that has never been renewed
    (a fresh token can't be, for its first 24 hours: that attempt just retries)."""
    for account in account_ids():
        if not store.secret(ig_api.token_key(account)):
            continue
        tokens = store.lane(account, "tokens")
        q = lambda **kw: laneq.call("GET", "/v1/jobs?" + urllib.parse.urlencode({"lane": tokens, **kw}))[1] or []  # noqa: E731
        if q(state="queued,running,held,dead", limit=1):
            continue
        last = q(state="succeeded", newest="true", limit=1)
        due = time.time() + 3600
        if last:
            finished = time.mktime(time.strptime(last[0]["finished_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            due = max(due, finished + REFRESH_EVERY)
        when = time.localtime(due)
        laneq.call("POST", "/v1/jobs", {
            "type": "refresh_token", "lane": tokens, "key": f"refresh:{account}:{time.strftime('%Y-%m-%d', when)}",
            # "member" too, so a worker from before the rename can still run it
            "payload": {"account": account, "member": account}, "run_at": rfc3339(when),
        })
        say(f"token renewal for {account} queued for {time.strftime('%Y-%m-%d %H:%M', when)}")


def handlers():
    return {"publish_post": run_publish, "refresh_token": run_refresh, **PLUGIN.handlers()}


def handle(laneq, lease, mode):
    job = Job(laneq, lease)
    label = f"#{job.id} {job.job['type']} {job.job.get('key', '')}"
    say(f"→ {label} (attempt {job.job['attempts']})")
    try:
        result = handlers()[job.job["type"]](job, mode)
    except LeaseLost as e:
        say(f"  ✗ {e}; another attempt owns it now")
        return
    except Exception as e:  # noqa: BLE001 — every failure is reported to the queue
        err = ig_api.classify(e)
        detail = "".join(traceback.format_exception_only(type(e), e)).strip()
        try:
            if isinstance(err, Permanent):
                job.fail(str(err), permanent=True)
                say(f"  ✗ needs attention: {err}")
            elif isinstance(err, RetryAfter):
                job.fail(str(err), retry_after=err.seconds)
                say(f"  ↻ {err}; retrying in {err.seconds // 60} min")
            else:
                job.fail(detail)
                say(f"  ↻ {detail}; will retry")
        except LeaseLost:
            pass
        return
    try:
        job.complete(result)
        say(f"  ✓ {json.dumps(result, ensure_ascii=False)}")
    except LeaseLost as e:
        say(f"  ✗ finished but {e}")


def main(plugin=None, argv=None):
    global PLUGIN
    PLUGIN = plugin or Plugin()
    store.configure(home=PLUGIN.home, app=PLUGIN.app)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:7070")
    ap.add_argument("--mode", choices=("live", "rehearse", "local"), default="rehearse")
    ap.add_argument("--once", action="store_true", help="exit when no job is runnable")
    ap.add_argument("--types", default="", help="job types to take (default: all this worker knows)")
    ap.add_argument("--dashboard", default="127.0.0.1:7080", help='serve the dashboard here ("" for none)')
    ap.add_argument("--log", type=Path, help="append output to this file instead of the console")
    args = ap.parse_args(argv)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", errors="replace", buffering=1)
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # captions can be any language

    laneq = Laneq(args.server)
    worker = f"{socket.gethostname()}-{os.getpid()}"
    types = [t for t in args.types.split(",") if t] or list(handlers())
    say(f"{PLUGIN.title} worker {worker} ({args.mode}) on {args.server} for {', '.join(types)}; data in {store.HOME}")
    if args.dashboard:
        from . import dashboard
        try:
            dashboard.serve(args.dashboard, args.server, PLUGIN)
            say(f"dashboard on http://{args.dashboard}")
        except OSError as e:  # e.g. the port is taken; posting doesn't depend on the page
            say(f"dashboard not started ({e})")
    from . import stats
    stats.start(laneq, PLUGIN, args.mode)
    link = None
    if not args.once:
        link = AppLink(laneq, PLUGIN.title, args.dashboard, PLUGIN.start_hint)
        link.check_in()  # stopped from laneq's dashboard: don't take a job, just exit
        if link.stop.is_set():
            link.exit()
    chains_checked = 0
    while True:
        try:
            if time.time() - chains_checked > 600:  # settings and tokens may change in the dashboard
                if "refresh_token" in types and args.mode == "live":
                    schedule_refreshes(laneq)
                PLUGIN.periodic(laneq, types)
                chains_checked = time.time()
            lease = laneq.claim(worker, types, wait=1 if args.once else 30)
        except (OSError, RuntimeError) as e:
            say(f"queue unreachable ({e}); retrying in 5 s")
            time.sleep(5)
            continue
        if lease is None:
            if args.once:
                return 0
            continue
        if link:
            link.busy = True
        handle(laneq, lease, args.mode)
        if link:
            link.busy = False
            if link.stop.is_set():
                link.exit()
