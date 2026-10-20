#!/usr/bin/env python3
"""MapHub's driver: the daily routine, unattended, on the host computer.

    mh -d, --drive    start the driver (in the background: nohup mh -d >/dev/null 2>&1 &)
    mh --status       what the driver is doing, and the commits waiting to be pushed
    mh --hold         keep the listing under review from being pushed (push later with mh -p)
    mh --stop         stop the driver once the listing it is scoring is done, or at once if it is
                      sleeping or waiting for review; unpushed commits wait for mh -p

Each weekday from MORNING (Eastern time) it runs `mh -l` until the day's listing
has a table: a usage limit or a busy arXiv only delays it. The new table is then
committed and waits REVIEW for the maintainer: `mh -p` pushes it at once, `mh --hold`
keeps it waiting; otherwise the driver pushes it. The rest of the time it runs
`mh -bb`, backfilling until the usage limit stops it, and sleeps until the reset
time Claude Code reports (a few minutes after; FALLBACK if none is given).
Listings missed while a weekly limit lasts are gaps that `mh -bb` fills first once
it resets. Code pushed from another computer is picked up on its own: every run pulls
first (restarting with new scorer code before it scores), and when the scorer's code
has changed the driver restarts itself between runs. When the morning comes during a backfill, the backfill stops after the
listing it is scoring. The driver logs to .cache/driver/driver-<month>.log (the
last two months kept).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scorer"))
import daily_table as dtab  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
MORNING = dt.time(6, 0)  # Eastern time; arXiv announces at 20:00 the evening before
REVIEW = dt.timedelta(hours=1)  # how long a new listing waits before the driver pushes it
FALLBACK = dt.timedelta(hours=5)  # sleep after a stop when no reset time is given
RETRY = dt.timedelta(minutes=30)  # sleep after a failure that is not a usage limit
MARGIN = dt.timedelta(minutes=3)  # after a reported reset time
TICK = 60  # seconds between checks while sleeping or waiting

STATE = ROOT / ".cache" / "driver"
LOG_KEEP = 2  # monthly logs kept: this month and the last
STATUS = STATE / "status.json"
PID = STATE / "driver.pid"
STOP, HOLD, YIELD = STATE / "stop", STATE / "hold", STATE / "yield"  # flags; local_run.py checks YIELD and STOP
RUNNER = [sys.executable, str(ROOT / "scorer" / "local_run.py")]
RESET = re.compile(r"resets ([^()\n]+?)(?:\s*\(([^)\n]+)\))?\s*$", re.M)


def now() -> dt.datetime:
    return dt.datetime.now(EASTERN)


def log_path() -> Path:
    """This month's log (driver-2026-10.log); older months beyond LOG_KEEP are removed."""
    path = STATE / f"driver-{now():%Y-%m}.log"
    if not path.exists():
        for old in sorted(STATE.glob("driver-*.log"))[:-(LOG_KEEP - 1) or None]:
            old.unlink()
    return path


def log(message: str) -> None:
    with open(log_path(), "a", encoding="utf-8") as out:
        out.write(f"[{now():%Y-%m-%d %H:%M:%S}] {message}\n")


def status(doing: str, until: dt.datetime | None = None) -> None:
    STATUS.write_text(json.dumps({"pid": os.getpid(), "doing": doing, "since": now().isoformat(timespec="seconds"),
                                  "until": until.isoformat(timespec="minutes") if until else None}) + "\n")


def run(*args: str, watch_morning: bool = False) -> tuple[int, str]:
    """Run local_run.py, logging its output; with watch_morning, ask it to yield
    (stop once the listing it is scoring is done) when the morning listing is due."""
    log(f"$ mh {' '.join(args)}")
    with open(log_path(), "a", encoding="utf-8") as out:
        proc = subprocess.Popen([*RUNNER, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        lines = []
        for line in proc.stdout:
            out.write(line)
            out.flush()
            lines.append(line)
            if watch_morning and morning_due() and not YIELD.exists():
                YIELD.touch()
                log("the morning listing is due: the backfill stops after this listing")
        code = proc.wait()
    YIELD.unlink(missing_ok=True)
    log(f"exit {code}")
    return code, "".join(lines)


def reset_time(output: str) -> dt.datetime | None:
    """The reset time in Claude Code's last limit message ("resets 7:40am
    (America/New_York)", or with a weekday or date for the weekly limit)."""
    found = RESET.findall(output)
    if not found:
        return None
    text, zone = found[-1]
    tz = ZoneInfo(zone) if zone else EASTERN
    current = dt.datetime.now(tz)
    text = text.strip().replace(" at ", " ")
    for fmt in ("%I:%M%p", "%I%p", "%a %I:%M%p", "%a %I%p", "%b %d, %I:%M%p", "%b %d, %I%p",
                "%b %d %I:%M%p", "%b %d %I%p"):
        try:
            t = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%a" in fmt:  # a weekday: the next one at that time
            wanted = text.split()[0][:3].title()  # strptime keeps no weekday of its own
            when = current.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            while when.strftime("%a") != wanted or when <= current:
                when += dt.timedelta(days=1)
            return when
        if "%b" in fmt:  # a date: this year, or next year if already past
            when = current.replace(month=t.month, day=t.day, hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return when if when > current else when.replace(year=when.year + 1)
        when = current.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return when if when > current else when + dt.timedelta(days=1)
    return None


def listing_done(day: dt.date) -> bool:
    return day.isoformat() in dtab.index_dates(ROOT / "tables")


def morning_due() -> bool:
    t = now()
    return t.weekday() < 5 and t.time() >= MORNING and not listing_done(t.date())


def sleep_until(when: dt.datetime, doing: str) -> None:
    """Sleep until `when`, waking early for a stop request or the morning listing."""
    status(doing, when)
    log(f"{doing} until {when:%a %Y-%m-%d %H:%M} ET")
    was_due = morning_due()
    while now() < when and not STOP.exists():
        if morning_due() and not was_due and "usage limit" not in doing:
            return
        time.sleep(TICK)


def waiting_commits() -> int:
    out = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", "@{upstream}..HEAD"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def review_and_push() -> None:
    """Give the maintainer REVIEW to look at the waiting commits, then push them."""
    if not waiting_commits():
        return
    deadline = now() + REVIEW
    HOLD.unlink(missing_ok=True)
    log(f"waiting for review until {deadline:%H:%M} ET: `mh -p` pushes now, `mh --hold` keeps the commits waiting")
    status("waiting for review (mh -p pushes, mh --hold keeps waiting)", deadline)
    while now() < deadline and not STOP.exists():
        if HOLD.exists():
            log("held: the commits wait for `mh -p`")
            HOLD.unlink(missing_ok=True)
            return
        if not waiting_commits():
            log("pushed by the maintainer")
            return
        time.sleep(TICK)
    if not STOP.exists():
        run("-p", "-y")


def after_stop(code: int, output: str, what: str, retry: bool = False) -> None:
    """Sleep after a run that stopped: until the usage limit resets, or a while
    (RETRY after a failure, or with retry=True; FALLBACK otherwise)."""
    if STOP.exists():
        return
    reset = reset_time(output)
    if reset:
        sleep_until(reset + MARGIN, f"usage limit reached during {what}; sleeping")
    elif code != 0 or retry:
        sleep_until(now() + RETRY, f"{what} not finished (exit {code}); retrying later")
    else:
        sleep_until(now() + FALLBACK, f"{what} stopped with no reset time given; sleeping")


def running() -> bool:
    """Whether a driver is running: its pid file names a live process that is the driver
    (a stale file after a reboot may name some other process)."""
    try:
        pid = int(PID.read_text())
        return "--drive" in Path(f"/proc/{pid}/cmdline").read_bytes().decode().replace("\0", " ") or \
            " -d" in " " + Path(f"/proc/{pid}/cmdline").read_bytes().decode().replace("\0", " ")
    except (OSError, ValueError):
        return False


def code_version() -> str:
    """The scorer's code as committed (the tree of scorer/), to notice a pulled change."""
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD:scorer"],
                          capture_output=True, text=True).stdout.strip()


def drive() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    if running() and PID.read_text().strip() != str(os.getpid()):  # not this driver restarting itself
        print(f"The driver is already running (pid {PID.read_text().strip()}); `mh --stop` stops it.")
        return 1
    PID.write_text(f"{os.getpid()}\n")
    for flag in (STOP, HOLD, YIELD):
        flag.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: STOP.touch())
    log(f"driver started (pid {os.getpid()})")
    started = code_version()
    try:
        while not STOP.exists():
            # Code pickup: each run pulls first, so code pushed from another computer reaches
            # the runs on its own; when the scorer's own code changed, the driver restarts in
            # place (between runs, so no listing is cut short) to run the new code too.
            if (current := code_version()) and started and current != started:
                log(f"the scorer's code changed ({started[:7]} -> {current[:7]}); restarting the driver")
                status("restarting with new code")
                os.execv(sys.executable, [sys.executable, *sys.argv])
            if morning_due():
                status("scoring the latest listing (mh -l)")
                code, output = run("-l")
                if listing_done(now().date()):
                    review_and_push()
                else:
                    after_stop(code, output, "the latest listing", retry=True)
                continue
            status("backfilling (mh -bb)")
            code, output = run("-bb", watch_morning=True)
            if morning_due():
                continue
            after_stop(code, output, "backfilling")
    except KeyboardInterrupt:
        pass
    finally:
        log("driver stopped")
        status("stopped")
        for path in (PID, STOP, HOLD, YIELD):
            path.unlink(missing_ok=True)
    return 0


def report() -> int:
    """mh --status"""
    if STATUS.exists():
        s = json.loads(STATUS.read_text())
        alive = running()
        print(f"Driver: {'running, pid ' + str(s['pid']) if alive else 'not running'}; "
              f"{s['doing']} (since {s['since']}{', until ' + s['until'] if s.get('until') else ''})")
    else:
        print("Driver: never started on this computer.")
    print(f"Commits waiting to be pushed: {waiting_commits()}")
    logs = sorted(STATE.glob("driver-*.log"))
    if logs:
        print(f"Last lines of {logs[-1].relative_to(ROOT)}:")
        print("".join(logs[-1].read_text(encoding="utf-8").splitlines(keepends=True)[-8:]), end="")
    return 0


def flag(path: Path, what: str) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    path.touch()
    print(what)
    return 0
