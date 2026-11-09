"""How an install customizes lanecast without forking it.

A plugin is a subclass of Plugin passed to lanecast.worker.main(). Every method
has a default that gives plain lanecast, so a plugin overrides only what it needs:

    class Mine(Plugin):
        app = "mine"                       # laneq lanes are "mine/<account>"
        title = "My scheduler"
        def default_caption(self, account):
            return "#my #tags"

Hooks, by where they run:

  worker      handlers(), periodic(), prepare_media(), default_caption(), after_publish()
  dashboard   accounts(), tab(), settings(), get(), post(), page_extra(), dashboard_extra(),
              extra_logs(), read_log()
  stats       stats()
"""
from pathlib import Path


class Plugin:
    app = "lanecast"           # laneq app name: lanes are "<app>/<account>"
    title = "lanecast"         # shown in the dashboard and in laneq's app list
    home = None                # data folder (default ~/.lanecast, or $LANECAST_HOME)
    start_hint = ""            # how to start this worker again, shown in laneq's dashboard
    outbox_repo = ""           # default public GitHub repo for staging media (Settings can change it)
    guide = True               # show the dashboard's Getting started list until setup is done

    # --- accounts and the dashboard's tabs ---
    def accounts(self):
        """[{"id", "title"}], or None for the accounts added in the dashboard (config.json)."""
        return None

    def tab(self, account, tab):
        """Change an account's tab (fields, actions, setup checks); return it."""
        return tab

    def settings(self, settings):
        """Change the Settings tab (shared fields and actions); return it."""
        return settings

    def page_extra(self):
        """A JS file the dashboard loads after its own script (served as /plugin.js), or None.
        It can call lanecast.extend({...}) to add header links, panels and log sources."""
        return None

    def dashboard_extra(self):
        """Extra keys for /api/dashboard (the page's plugin script can read them)."""
        return {}

    def extra_logs(self):
        """More entries for the Log tab: [{"name", "title", "sub", "running"}]."""
        return []

    def read_log(self, name):
        """{"path", "lines", "running"} for one of extra_logs(), or None."""
        return None

    def get(self, handler, path, query):
        """Serve a GET the dashboard doesn't know. Return True if handled."""
        return False

    def post(self, handler, path, body):
        """Serve a POST the dashboard doesn't know. Return True if handled."""
        return False

    # --- the worker ---
    def handlers(self):
        """{job type: fn(job, mode)} for job types beyond publish_post and refresh_token."""
        return {}

    def periodic(self, laneq, types):
        """Called every 10 minutes by the worker, with the job types it takes (e.g. to keep a
        weekly job queued)."""

    def prepare_media(self, job, workdir):
        """[(kind, local path)] ready to upload for a publish_post job, or None to use
        payload["media"] (files in <home>/media)."""
        return None

    def default_caption(self, account):
        """The caption when neither the post nor the account's settings have one."""
        return ""

    def after_publish(self, job, permalink):
        """Called once a post is up (e.g. to record it somewhere)."""

    # --- usage stats ---
    def stats(self):
        """Extra totals for the usage report, e.g. {"media_bytes": ...} for media kept elsewhere."""
        return {}


def here(file, *parts):
    """A path next to a plugin's own file, for page_extra() and similar."""
    return Path(file).resolve().parent.joinpath(*parts)
