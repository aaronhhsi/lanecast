"""The `lanecast` command: everything for an install in one folder.

    lanecast start       start laneq (if it isn't running) and the worker + dashboard, and open the dashboard
    lanecast stop        stop them (a post going up finishes first)
    lanecast status      what's running, and where
    lanecast open        open the dashboard
    lanecast github      sign in to GitHub and make the outbox repo Instagram downloads from
    lanecast update      get the newest lanecast and laneq, then restart
    lanecast autostart on|off   start at login (on by default after install)
    lanecast uninstall   remove the login item and the whole folder
    lanecast run         the worker in this window (what start runs in the background)

An install made by install.ps1 / install.sh looks like:

    <root>/app/      the code (a git clone)
    <root>/env/      its Python packages
    <root>/bin/      laneq
    <root>/data/     tokens, settings, uploads, logs, and laneq's queue (laneq-data/)
    <root>/lanecast(.cmd)  this command
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# An install keeps everything under one folder (the one holding app/ and env/). Run from a plain
# checkout instead, lanecast keeps its data in ~/.lanecast (or $LANECAST_HOME) and laneq in its bin/.
ROOT = Path(os.environ.get("LANECAST_DIR") or Path(__file__).resolve().parents[2])
INSTALLED = (ROOT / "app").is_dir() and (ROOT / "env").is_dir()
if INSTALLED:
    APP, ENV, BIN, DATA = ROOT / "app", ROOT / "env", ROOT / "bin", ROOT / "data"
else:
    DATA = Path(os.environ.get("LANECAST_HOME") or Path.home() / ".lanecast")
    ROOT, APP, ENV, BIN = DATA, None, None, DATA / "bin"
WINDOWS = os.name == "nt"
LANEQ_RELEASES = "https://github.com/aaronhhsi/laneq/releases/latest/download"
TASK = "lanecast"


def say(msg=""):
    print(msg, flush=True)


# --- settings for this install ------------------------------------------------------

def settings():
    try:
        return json.loads((DATA / "install.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_settings(**fields):
    DATA.mkdir(parents=True, exist_ok=True)
    s = {**settings(), **fields}
    (DATA / "install.json").write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    return s


def laneq_addr():
    return os.environ.get("LANECAST_LANEQ_ADDR") or settings().get("laneq", "127.0.0.1:7070")


def dashboard_addr():
    return os.environ.get("LANECAST_DASHBOARD_ADDR") or settings().get("dashboard", "127.0.0.1:7080")


def up(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def wait(check, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.3)
    return check()


def python():
    """This install's Python, preferring pythonw on Windows so nothing opens a window."""
    exe = Path(sys.executable)
    if WINDOWS and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return exe.parent / "pythonw.exe"
    return exe


# --- laneq --------------------------------------------------------------------------

def laneq_name():
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    system = {"windows": "windows", "darwin": "darwin"}.get(platform.system().lower(), "linux")
    return f"laneq-{system}-{arch}" + (".exe" if system == "windows" else "")


def laneq_path():
    return BIN / ("laneq.exe" if WINDOWS else "laneq")


def download_laneq():
    """Put laneq in bin/: a copy of LANECAST_LANEQ_BIN, or the latest release, checked against its checksum."""
    BIN.mkdir(parents=True, exist_ok=True)
    dest, local = laneq_path(), os.environ.get("LANECAST_LANEQ_BIN")
    if local:
        shutil.copy2(local, dest)
        return dest
    name = laneq_name()
    say(f"  downloading laneq ({name})")
    with urllib.request.urlopen(f"{LANEQ_RELEASES}/checksums.txt", timeout=60) as r:
        sums = dict(line.split()[::-1] for line in r.read().decode().splitlines() if line.strip())
    with urllib.request.urlopen(f"{LANEQ_RELEASES}/{name}", timeout=300) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != sums.get(name):
        raise SystemExit("laneq's download doesn't match its checksum; try again later")
    tmp = dest.with_suffix(".download")
    tmp.write_bytes(data)
    if not WINDOWS:
        tmp.chmod(0o755)
    os.replace(tmp, dest)
    return dest


def detached(cmd, log):
    """Start a background process that outlives this one, with its output in log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab")
    kw = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": str(ROOT)}
    if WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED, NEW_PROCESS_GROUP, NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([str(c) for c in cmd], **kw).pid


def start_laneq():
    addr = laneq_addr()
    if up(f"http://{addr}/healthz"):
        return False  # one is running already (ours, or another app's to share)
    exe = laneq_path() if laneq_path().exists() else download_laneq()
    pid = detached([exe, "-addr", addr, "-data", DATA / "laneq-data"], DATA / "laneq.log")
    if not wait(lambda: up(f"http://{addr}/healthz"), 20):
        raise SystemExit(f"laneq didn't start; see {DATA / 'laneq.log'}")
    save_settings(laneq_pid=pid)
    return True


# --- the worker ---------------------------------------------------------------------

def start(open_browser=True):
    DATA.mkdir(parents=True, exist_ok=True)
    started = start_laneq()
    say(f"laneq: {'started' if started else 'running'} on http://{laneq_addr()}")
    req = urllib.request.Request(f"http://{laneq_addr()}/v1/apps/lanecast/start", data=b"", method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()  # lift a Stop from laneq's dashboard
    except OSError:
        pass
    if not up(f"http://{dashboard_addr()}/healthz"):
        pid = detached([python(), "-m", "lanecast", "run", "--mode", "live", "--server", f"http://{laneq_addr()}",
                        "--dashboard", dashboard_addr(), "--log", DATA / "worker.log"], DATA / "worker.out")
        save_settings(worker_pid=pid)
        if not wait(lambda: up(f"http://{dashboard_addr()}/healthz"), 30):
            raise SystemExit(f"the worker didn't start; see {DATA / 'worker.log'}")
    url = f"http://{dashboard_addr()}"
    say(f"lanecast: running, dashboard on {url}")
    if open_browser:
        webbrowser.open(url)


def kill(pid):
    if not pid:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def stop():
    s, base = settings(), f"http://{laneq_addr()}"
    if up(f"http://{dashboard_addr()}/healthz"):
        say("stopping the worker (a post going up finishes first)")
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/v1/apps/lanecast/stop", data=b"", method="POST"), timeout=5).read()
        except OSError:
            pass
        if not wait(lambda: not up(f"http://{dashboard_addr()}/healthz"), 300):
            kill(s.get("worker_pid"))
    if s.get("laneq_pid") and up(f"{base}/healthz"):
        say("stopping laneq")
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/v1/admin/shutdown", data=b"", method="POST"), timeout=5).read()
        except OSError:
            pass
        if not wait(lambda: not up(f"{base}/healthz"), 30):
            kill(s["laneq_pid"])
        save_settings(laneq_pid=None)
    say("stopped")


def status():
    say(f"folder:    {ROOT}")
    say(f"laneq:     {'running' if up(f'http://{laneq_addr()}/healthz') else 'not running'} (http://{laneq_addr()})")
    say(f"dashboard: {'running' if up(f'http://{dashboard_addr()}/healthz') else 'not running'} (http://{dashboard_addr()})")
    say(f"autostart: {'on' if autostart_on() else 'off'}")


# --- start at login -----------------------------------------------------------------

def _plist():
    return Path.home() / "Library" / "LaunchAgents" / "com.lanecast.plist"


def _unit():
    return Path.home() / ".config" / "systemd" / "user" / "lanecast.service"


def autostart(on):
    cmd = [str(python()), "-m", "lanecast", "start", "--no-browser"]
    system = platform.system()
    if WINDOWS:
        if on:
            ps = (f"$a = New-ScheduledTaskAction -Execute '{cmd[0]}' -Argument '{' '.join(cmd[1:])}' -WorkingDirectory '{ROOT}'; "
                  "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
                  "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0; "
                  f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Force | Out-Null")
        else:
            ps = f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction SilentlyContinue"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"couldn't change the login task: {r.stderr.strip()}")
    elif system == "Darwin":
        plist = _plist()
        if on:
            plist.parent.mkdir(parents=True, exist_ok=True)
            args = "".join(f"<string>{c}</string>" for c in cmd)
            plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.lanecast</string>
<key>ProgramArguments</key><array>{args}</array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
        elif plist.exists():
            plist.unlink()
    else:
        unit = _unit()
        if on:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(f"""[Unit]
Description=lanecast (and laneq)
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={ROOT}
ExecStart={' '.join(cmd)}

[Install]
WantedBy=default.target
""", encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", "lanecast.service"], capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", "lanecast.service"], capture_output=True)
            if unit.exists():
                unit.unlink()
    save_settings(autostart=on)


def autostart_on():
    if WINDOWS:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", f"Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue"],
                           capture_output=True, text=True)
        return bool(r.stdout.strip())
    return _plist().exists() if platform.system() == "Darwin" else _unit().exists()


# --- GitHub: the outbox ----------------------------------------------------------------

def github():
    """Sign in with the GitHub CLI and make a public outbox repo, so Settings → Outbox is done."""
    from . import store
    store.configure(home=DATA)
    gh = shutil.which("gh")
    if not gh:
        say("The GitHub CLI isn't installed. Install it from https://cli.github.com, then run: lanecast github")
        say("(Or set Settings → Outbox by hand; the dashboard explains how.)")
        return 1
    if subprocess.run([gh, "auth", "status"], capture_output=True).returncode:
        say("Signing in to GitHub: your browser opens; copy the code shown here into it.")
        if subprocess.run([gh, "auth", "login", "--web", "--git-protocol", "https", "--hostname", "github.com"]).returncode:
            say("GitHub sign-in didn't finish; run `lanecast github` to try again.")
            return 1
    login = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True).stdout.strip()
    repo = f"{login}/lanecast-outbox"
    if subprocess.run([gh, "repo", "view", repo], capture_output=True).returncode:
        r = subprocess.run([gh, "repo", "create", repo, "--public", "--add-readme",
                            "--description", "Temporary staging for Instagram uploads by lanecast. Emptied after every post."],
                           capture_output=True, text=True)
        if r.returncode:
            say(f"Couldn't make the outbox repo: {r.stderr.strip()}")
            return 1
    store.set_config(outbox_repo=repo)
    say(f"Outbox ready: {repo} (Instagram downloads each post's files from here while it goes up).")
    return 0


# --- update and uninstall ----------------------------------------------------------------

def update():
    stop()
    if APP and (APP / ".git").exists():
        say("getting the newest lanecast")
        subprocess.run(["git", "-C", str(APP), "pull", "--ff-only"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(APP)], check=True)
    if laneq_path().exists() and not os.environ.get("LANECAST_LANEQ_BIN"):
        say("getting the newest laneq")
        download_laneq()
    start()


def uninstall(yes=False):
    if not yes:
        answer = input(f"Remove lanecast and everything in {ROOT} (its tokens and queued posts too)? Type yes: ")
        if answer.strip().lower() != "yes":
            say("Nothing removed.")
            return 1
    stop()
    autostart(False)
    for wrapper in (Path.home() / ".local" / "bin" / "lanecast",):
        if wrapper.is_symlink() or (wrapper.exists() and str(ROOT) in wrapper.read_text(errors="ignore")):
            wrapper.unlink()
    say(f"Removing {ROOT}")
    remove_tree(ROOT)
    say("lanecast is uninstalled.")
    return 0


def remove_tree(root):
    """Delete a folder, including the files of the Python running this: Windows won't delete a
    program that's running, but it will move it, so those few go to the temp folder instead."""
    import tempfile
    import uuid
    trash = Path(tempfile.gettempdir()) / "lanecast-removed"
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            f = Path(dirpath) / name
            try:
                f.unlink()
            except OSError:
                try:
                    trash.mkdir(exist_ok=True)
                    os.replace(f, trash / f"{uuid.uuid4().hex}-{name}")
                except OSError:
                    pass
        for name in dirnames:
            try:
                (Path(dirpath) / name).rmdir()
            except OSError:
                pass
    try:
        Path(root).rmdir()
    except OSError:
        say(f"Some files are still in use; delete {root} after closing this window.")


def install(open_browser=True, ask_github=True):
    """The last step of install.ps1 / install.sh: laneq, the command, start at login, start, GitHub."""
    DATA.mkdir(parents=True, exist_ok=True)
    save_settings(laneq=laneq_addr(), dashboard=dashboard_addr())
    if not laneq_path().exists():
        download_laneq()
    write_wrapper()
    if not os.environ.get("LANECAST_NO_AUTOSTART"):
        autostart(True)
        say("  lanecast will start on its own when you log in")
    config = DATA / "config.json"
    has_outbox = config.exists() and '"outbox_repo"' in config.read_text(encoding="utf-8")
    if ask_github and not has_outbox:
        say("")
        say("Instagram downloads each post's files from a public GitHub repo while it goes up, so lanecast")
        say("needs a (free) GitHub account. This makes the repo for you.")
        answer = input("Set up GitHub now? Your browser opens to sign in. [Y/n] ").strip().lower() if sys.stdin.isatty() else "n"
        if answer in ("", "y", "yes"):
            github()
        else:
            say("Skipped: the dashboard shows how to do it later (or run: lanecast github).")
    say("")
    start(open_browser)
    say("")
    say("Next: the dashboard's Getting started list walks you through your accounts and Instagram tokens.")
    say(f"To control lanecast later, use: {wrapper_path()} start | stop | status | update | uninstall")


def wrapper_path():
    return ROOT / ("lanecast.cmd" if WINDOWS else "lanecast")


def write_wrapper():
    exe = Path(sys.executable)
    if WINDOWS:
        # one line, so cmd has nothing left to read if `uninstall` deletes this file
        wrapper_path().write_text(f'@"{exe}" -m lanecast %* & exit /b\r\n', encoding="utf-8")
    else:
        wrapper_path().write_text(f'#!/bin/sh\nexec "{exe}" -m lanecast "$@"\n', encoding="utf-8")
        wrapper_path().chmod(0o755)
        link = Path.home() / ".local" / "bin" / "lanecast"
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(wrapper_path())
        except OSError:
            pass


# --- the command ---------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    os.environ.setdefault("LANECAST_HOME", str(DATA))  # the worker started from here keeps its data there
    if argv in (["-h"], ["--help"], ["help"]):
        print(__doc__)
        return 0
    if not argv or argv[0] in ("run",) or argv[0].startswith("-"):
        from .worker import main as worker_main
        return worker_main(argv=argv[1:] if argv and argv[0] == "run" else argv)
    ap = argparse.ArgumentParser(prog="lanecast", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start"); p.add_argument("--no-browser", action="store_true")
    sub.add_parser("stop"); sub.add_parser("status"); sub.add_parser("open"); sub.add_parser("github"); sub.add_parser("update")
    p = sub.add_parser("autostart"); p.add_argument("state", choices=("on", "off"))
    p = sub.add_parser("uninstall"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("install"); p.add_argument("--no-browser", action="store_true"); p.add_argument("--no-github", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "start":
        start(not args.no_browser)
    elif args.cmd == "stop":
        stop()
    elif args.cmd == "status":
        status()
    elif args.cmd == "open":
        webbrowser.open(f"http://{dashboard_addr()}")
    elif args.cmd == "github":
        return github()
    elif args.cmd == "update":
        update()
    elif args.cmd == "autostart":
        autostart(args.state == "on")
        say(f"start at login: {args.state}")
    elif args.cmd == "uninstall":
        return uninstall(args.yes)
    elif args.cmd == "install":
        install(not args.no_browser and not os.environ.get("LANECAST_NO_BROWSER"), not args.no_github)
    return 0
