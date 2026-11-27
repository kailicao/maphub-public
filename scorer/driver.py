#!/usr/bin/env python3
"""MapHub's driver: the daily routine, unattended, on the host computer.

    mh -d, --drive    start the driver (in the background: nohup mh -d >/dev/null 2>&1 &)
    mh --status       what the driver is doing, and the commits waiting to be pushed
    mh --hold         keep the listing under review from being pushed (push later with mh -p), when REVIEW is set
    mh --stop         stop the driver once the listing it is scoring is done, or at once if it is
                      sleeping or waiting for review; unpushed commits wait for mh -p

It pushes nothing on Saturdays and Sundays (Eastern time; local_run.py checks MAPHUB_UNATTENDED,
which it sets). Each weekday from MORNING (Eastern time) it runs `mh -l` until the day's listing
has a table: a usage limit or a busy arXiv only delays it. The new table is then
pushed, at once when REVIEW is 0 (the prototype's setting); otherwise it waits REVIEW for
the maintainer, `mh -p` pushing it at once and `mh --hold` keeping it waiting. The rest of
the time it runs `mh -bb`, backfilling until the usage limit stops it, and sleeps until a few
minutes after the reset time Claude Code reports (the maintainer stops and restarts the driver
when switching seats); while a weekly limit lasts, or when no reset time is given, no sleep is
longer than WAKE.
Listings missed while a weekly limit lasts are gaps that `mh -bb` fills first once it resets.
Code pushed from another computer is picked up on its own: every run pulls and merges main
first (on weekends, Monday morning's run, restarting with new scorer code before it scores), and
when the scorer's code has changed the driver restarts itself between runs. When the morning
comes during a backfill, the backfill stops after the listing it is scoring. The driver logs to
.cache/logs/mh-<date>.log, with the mh runs started by hand: one file a day, all kept (a run's
output stays in the file of the day it started). MAPHUB_LOG (environment or .env) sets what the
logs keep: brief (the default), one line per event that matters; full, every step; off, no logs.
MAPHUB_TIMEZONE sets the zone of the times they and mh --status show (default America/New_York;
the schedule itself is Eastern time). mh --status reads the state kept in .cache/driver/, so it
works with any MAPHUB_LOG.
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
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scorer"))
import daily_table as dtab  # noqa: E402
import local_run as lr  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
# The schedule, the prototype's choices; a MapHub sets its own (RUNNING.md, Setting up a copy, step 6).
MORNING = dt.time(2, 0)  # Eastern time; arXiv announces at 20:00 the evening before, and a listing
                         # scored from 02:00 is in the viewer by about 07:00 even after a usage-limit wait
REVIEW = dt.timedelta(0)  # how long a new listing waits for the maintainer before the driver pushes it
                          # (0: at once, as in the prototype since Oct 21, 2026)
SESSION = dt.timedelta(hours=5)  # Claude Code's session; a reset further away is the weekly limit
WAKE = dt.timedelta(hours=2.5)  # the longest sleep: while a weekly limit lasts, the driver tries again this often
RETRY = dt.timedelta(minutes=30)  # sleep after a failure that is not a usage limit
MARGIN = dt.timedelta(minutes=3)  # after a reported reset time
TICK = 60  # seconds between checks while sleeping or waiting

STATE = ROOT / ".cache" / "driver"
LOGS = ROOT / ".cache" / "logs"  # every mh run's output, the driver's and those started by hand
STATUS = STATE / "status.json"
PID = STATE / "driver.pid"
LIMIT = STATE / "limit.json"  # the usage limit in force, or the last one, with when it ended (mh --status)
PROBLEM = STATE / "problem.json"  # the last problem: a warning or error, or a wait for the maintainer (mh --status)
STOP, HOLD, YIELD = STATE / "stop", STATE / "hold", STATE / "yield"  # flags; local_run.py checks YIELD and STOP
PUSH_DUE = STATE / "push-due"  # a push failed: tried again every RETRY until it works (review_and_push)
RUNNER = [sys.executable, str(ROOT / "scorer" / "local_run.py")]
RESET = re.compile(r"resets ([^()\n]+?)(?:\s*\(([^)\n]+)\))?\s*$", re.M)
SCORED = re.compile(r"^\s+(?:\[\d+/\d+\] )?\S+ chunk \d+: \d+ scores|^\s+outline (?:chunk \d+/\d+|request): \d+ papers")
# a request that scored: the run is past any usage limit (a commit or a push alone scores nothing)
AT = re.compile(r"^(?:Backfilling(?: from)?|Finishing) (\d{4}-\d{2}-\d{2})")  # the listing a run is on
HOLD_LIMIT = dt.timedelta(minutes=20)  # a quiet check during a weekly limit is logged as it goes once it runs this long


def now() -> dt.datetime:
    return dt.datetime.now(EASTERN)


def log_path() -> Path:
    """Today's log (mh-2026-10-21.log), shared by the driver and the mh runs started by hand;
    a run's output goes to the file it started in. Every log is kept. With MAPHUB_LOG=brief
    (the default) a day of morning runs and backfills writes a few KB; with full, during the
    legacy survey a busy day wrote about 200 KB, the whole survey about 10-20 MB. With off,
    there are no logs. Before Oct 31, 2026 the driver's logs were .cache/driver/driver-<date>.log."""
    return LOGS / f"mh-{now():%Y-%m-%d}.log"


def write(lines: list[str]) -> None:
    """Append lines to today's log, unless MAPHUB_LOG is off."""
    if not lines or dtab.log_mode() == "off":
        return
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(log_path(), "a", encoding="utf-8") as out:
        out.write("".join(line + "\n" for line in lines))


def stamp(message: str) -> str:
    return f"[{dtab.shown(now())}] {message}"


def log(message: str) -> None:
    write([stamp(message)])


def log_full(message: str) -> None:
    """A line of the driver's for the full log only."""
    if dtab.log_mode() == "full":
        log(message)


def output_lines(lines: list[str]) -> list[str]:
    """A run's lines as the log writes them (they are already those the log keeps): in the brief
    log, a line that is not indented gets the time, and blank lines are left out."""
    if dtab.log_mode() != "brief":
        return lines
    return [line if line[:1].isspace() else stamp(line) for line in lines if line.strip()]


def log_output(lines: list[str]) -> None:
    write(output_lines(lines))


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")  # written whole, then renamed: mh --status never reads half a file
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_status: dict = {}


def status(doing: str | None = None, until: dt.datetime | None = None, at: str | None = None) -> None:
    """What the driver is doing, for mh --status; at=..., alone, records only the listing a
    backfill is on."""
    if doing is not None:
        _status.update({"doing": doing, "since": now().isoformat(timespec="seconds"),
                        "until": until.isoformat(timespec="minutes") if until else None, "at": None})
    if at is not None:
        _status["at"] = at
    write_state(STATUS, {"pid": os.getpid(), "started": STARTED, **_status})


STARTED: str | None = None  # when this driver started (kept across its restarts for new code)


def record_problem(what: str, past: str | None = None) -> None:
    """The last problem, for mh --status; `past` is how it reads once cleared."""
    write_state(PROBLEM, {"when": now().isoformat(timespec="seconds"), "what": what, "past": past or what})


def clear_problem() -> None:
    problem = read_state(PROBLEM)
    if problem and not problem.get("cleared"):
        problem["cleared"] = now().isoformat(timespec="seconds")
        write_state(PROBLEM, problem)


def note_line(text: str, problems: bool = True) -> None:
    """What mh --status keeps from a line of a run: the listing it is on, its last warning or
    error (problems=False: a quiet run's, which its caller reports)."""
    if m := AT.match(text):
        status(at=m.group(1))
    if problems and text.startswith(("Error: ", "Warning: ")):
        record_problem(text.strip())


# ------------------------------------------------------------------ usage limits
# The rules (RUNNING.md, Running unattended, step 3), kept in .cache/driver/limit.json:
# R1. A limit is weekly when Claude Code's message says "weekly", or when its reset is more
#     than SESSION away; any other is the 5-hour limit.
# R2. A weekly limit ends only when a request scores (a SCORED line; a commit or a push alone
#     does not count), or when a run after its recorded reset meets no limit. While it lasts,
#     each run is a quiet check: its lines are dropped if it meets a limit again.
#     A 5-hour limit ends when a run meets no limit.
# R3. The reported reset is read as the occurrence of that time nearest to now (a weekday: the
#     next one, or today's up to an hour past), never rolled a day or a year ahead past a
#     reset just gone. The driver sleeps until MARGIN after it (a weekly sleep at most WAKE);
#     a reset already past (still limited after it) gives RETRY, never less.
# R4. A limit is said once per reset: limit.json keeps the last one said, and the same reset
#     reported again is not said again.


def weekly_limit() -> dict | None:
    """The weekly limit in force, if any."""
    limit = read_state(LIMIT)
    return limit if limit.get("kind") == "weekly" and not limit.get("ended") else None


def end_limit() -> None:
    limit = read_state(LIMIT)
    if limit and not limit.get("ended"):
        limit["ended"] = now().isoformat(timespec="seconds")
        write_state(LIMIT, limit)


def run(*args: str, watch_morning: bool = False, quiet: bool = False) -> tuple[int, str]:
    """Run local_run.py, logging its output (as MAPHUB_LOG says); with watch_morning, ask it to
    yield (stop once the listing it is scoring is done) when the morning listing is due.
    While a weekly limit lasts, the run is a quiet check: its lines are held back, and dropped if
    the limit stops it again. A line of "Weekly limit over" comes before the first request that
    scores, or at the run's end once the reset time has passed; a run that goes on for
    HOLD_LIMIT without either has its lines logged as they come, the limit still in force."""
    mode = dtab.log_mode()
    limit_open = weekly_limit() is not None and not quiet
    held: list[str] | None = [] if limit_open or quiet else None  # quiet: the caller logs what matters
    began = now()

    def emit(lines: list[str]) -> None:
        if held is None:
            write(lines)
        else:
            held.extend(lines)

    def release() -> None:
        nonlocal held
        if held is not None:
            lines, held = held, None
            write(lines)

    def over() -> None:
        nonlocal limit_open
        end_limit()
        limit_open = False
        log("Weekly limit over")
        release()

    if mode == "full":
        emit([stamp(f"$ mh {' '.join(args)}")])
    proc = subprocess.Popen([*RUNNER, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
                            env={**os.environ, "MAPHUB_UNATTENDED": "1",  # no pushes on weekends (local_run.py)
                                 "MAPHUB_LOGGED": "1"})  # logged here, not again by local_run.py
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line)
            raw = line.rstrip("\n")
            text = dtab.unmark(raw)
            if limit_open and SCORED.search(text):
                over()  # before the line that shows it
            kept = dtab.log_line(raw, mode)
            if kept is not None:
                emit(output_lines([kept]))
            note_line(text, problems=not quiet)
            if held is not None and not quiet and now() - began > HOLD_LIMIT:
                release()  # a long run: its lines as they come, the limit not yet known to be over
            if watch_morning and not YIELD.exists() and (due_now() or push_due(safe=True)):
                YIELD.touch()  # the backfill stops after this listing
                if mode == "full":
                    emit([stamp("The morning listing, or a push to try again, is due: the backfill stops after this listing")])
        code = proc.wait()
    finally:
        if proc.poll() is None:  # the driver failed while the run went on: end the run, not leave it going
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    YIELD.unlink(missing_ok=True)
    output = "".join(lines)
    if quiet:
        return code, output
    if limit_open:  # R2
        if reported := reported_limit(output):
            if held is not None:
                held = None  # limited again: nothing to log (after_stop says it if the reset is new, R4)
                log_full(f"Checked: still at a usage limit (reset {dtab.shown(reported[1])})")
        elif now() >= dt.datetime.fromisoformat((weekly_limit() or {}).get("until") or "9999-01-01T00:00+00:00"):
            over()
    release()
    if mode == "full":
        log(lr.exit_words(code))
    return code, output


def reset_time(output: str) -> dt.datetime | None:
    """The reset time in Claude Code's last limit message ("resets 7:40am (America/New_York)",
    or with a weekday or date for the weekly limit): the occurrence of that time nearest to now
    (R3), so a reset just past stays in the past instead of rolling a day or a year ahead."""
    found = RESET.findall(output)
    if not found:
        return None
    text, zone = found[-1]
    try:
        tz = ZoneInfo(zone) if zone else EASTERN
    except Exception:  # not a zone ("in 3 hours"): the text before it may still be a time
        tz = EASTERN
    current = now().astimezone(tz)
    text = text.strip().replace(" at ", " ")
    for fmt in ("%I:%M%p", "%I%p", "%a %I:%M%p", "%a %I%p", "%b %d, %I:%M%p", "%b %d, %I%p",
                "%b %d %I:%M%p", "%b %d %I%p"):
        try:
            t = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        when = current.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if "%a" in fmt:  # a weekday: the next one, or today's if at most an hour past
            wanted = text.split()[0][:3].title()  # strptime keeps no weekday of its own
            candidates = [c for c in (when + dt.timedelta(days=n) for n in range(-1, 8))
                          if c.strftime("%a") == wanted and c > current - dt.timedelta(hours=1)][:1]
        elif "%b" in fmt:  # a date: last year, this year or next
            base = when.replace(month=t.month, day=t.day)
            candidates = [base.replace(year=base.year + n) for n in (-1, 0, 1)]
        else:  # a time: yesterday, today or tomorrow
            candidates = [when + dt.timedelta(days=n) for n in (-1, 0, 1)]
        return min(candidates, key=lambda c: abs(c - current))
    return None


def reported_limit(output: str) -> tuple[str, dt.datetime] | None:
    """The last usage limit Claude Code reported in `output`: its kind (R1) and reset."""
    lines = [line for line in output.splitlines() if RESET.search(line)]
    reset = reset_time(lines[-1]) if lines else None
    if reset is None:
        return None
    weekly = "weekly" in lines[-1].lower() or reset - now() > SESSION
    return ("weekly" if weekly else "5-hour"), reset


def listing_done(day: dt.date) -> bool:
    return day.isoformat() in dtab.index_dates(ROOT / "tables")


def morning_due() -> bool:
    t = now()
    return t.weekday() < 5 and t.time() >= MORNING and not listing_done(t.date())


def due_now() -> bool:
    """morning_due() for the check after each line a run prints: a failed read of
    index.json counts as not due (the next line checks again) instead of ending the
    driver, which would cut the run off before it commits its table."""
    try:
        return morning_due()
    except (OSError, ValueError) as e:
        log_full(f"Could not check for the morning listing ({e}); checking again on the next line")
        return False


def next_morning() -> dt.datetime:
    """The next weekday's MORNING (Eastern time)."""
    t = now()
    day = t.date() + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, MORNING, tzinfo=t.tzinfo)


def next_listing() -> dt.datetime:
    """When the next morning listing is due: today's MORNING if it is still ahead on a weekday,
    else the next weekday's."""
    t = now()
    if t.weekday() < 5 and t.time() < MORNING:
        return dt.datetime.combine(t.date(), MORNING, tzinfo=t.tzinfo)
    return next_morning()


def wake_time(kind: str, reset: dt.datetime) -> dt.datetime:
    """When to wake after a usage limit (R3): MARGIN after its reset, a weekly one at most WAKE
    from now (a seat switched meanwhile is picked up when the maintainer restarts the driver);
    a reset already past, RETRY from now."""
    t = now()
    if reset + MARGIN <= t:
        return t + RETRY
    return min(reset + MARGIN, t + WAKE) if kind == "weekly" else reset + MARGIN


def sleep_until(when: dt.datetime, doing: str, cap: bool = True, morning: bool = True) -> None:
    """Sleep until `when`, but no longer than WAKE unless cap is False, waking early for a stop
    request or (morning=True) the morning listing. The caller has logged why."""
    if cap:
        when = min(when, now() + WAKE)
    status(doing, when)
    was_due = morning_due()
    while now() < when and not STOP.exists():
        if morning and morning_due() and not was_due:
            return
        time.sleep(TICK)


def waiting_commits() -> int:
    out = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", "@{upstream}..HEAD"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def review_and_push() -> tuple[int, str]:
    """Give the maintainer REVIEW to look at the waiting commits, then push them: the push's
    exit code and output (0 when nothing was left to push). A failed push leaves PUSH_DUE, so
    that the driver tries again (drive) rather than wait for the next morning."""
    if not waiting_commits():
        PUSH_DUE.unlink(missing_ok=True)
        return 0, ""
    if REVIEW > dt.timedelta(0) and not PUSH_DUE.exists():
        deadline = now() + REVIEW
        HOLD.unlink(missing_ok=True)
        log(f"Waiting for review until {dtab.shown(deadline, day=False)}: `mh -p` pushes now, `mh --hold` keeps the commits waiting")
        status("waiting for review (mh -p pushes, mh --hold keeps waiting)", deadline)
        while now() < deadline and not STOP.exists():
            if HOLD.exists():
                log("Held: the commits wait for `mh -p`")
                HOLD.unlink(missing_ok=True)
                return 0, ""
            if not waiting_commits():
                log("Pushed by the maintainer")
                return 0, ""
            time.sleep(TICK)
        if STOP.exists():
            return 0, ""
    return push()


_last_push_try: dt.datetime | None = None


def push(retrying: bool = False) -> tuple[int, str]:
    """Push the waiting commits (mh -p -y). A failure is said once (and kept as the last
    problem), again only when its error changes; PUSH_DUE then holds it, and the driver tries
    again between backfills, at most every RETRY, its tries logging nothing until one works."""
    global _last_push_try
    _last_push_try = now()
    status("pushing (mh -p -y)")
    code, output = run("-p", "-y", quiet=retrying)
    if code == 0 and not waiting_commits():
        if PUSH_DUE.exists():
            PUSH_DUE.unlink()
            clear_problem()
            pushed = [l for l in dtab.unmark_all(output).splitlines() if l.startswith("Pushed:")]
            if retrying and pushed:
                write(output_lines(pushed))
                log("The push works again")
            elif retrying:  # nothing left to push: someone pushed by hand meanwhile
                log("The waiting commits were pushed meanwhile (by hand?); no push is due")
        return code, output
    if code == 0:
        return code, output  # nothing pushed (a weekend): Monday morning's run pushes
    errors = [l for l in dtab.unmark_all(output).splitlines() if l.startswith("Error: ")]
    error = errors[-1][len("Error: "):].strip()[:300] if errors else lr.exit_words(code)
    if read_state(PUSH_DUE).get("error") != error:
        if retrying:
            log(f"Error: {error}")
        log(f"The push failed; the commits wait, and the driver tries again every {RETRY.seconds // 60} minutes "
            "between backfills (or push by hand: mh -p)")
        record_problem(f"the push failed: {error}")
    write_state(PUSH_DUE, {"error": error, "since": read_state(PUSH_DUE).get("since") or now().isoformat(timespec="seconds")})
    return code, output


def push_due(safe: bool = False) -> bool:
    """A failed push to try again now: today's listing is done (none on weekends), and RETRY has
    passed since the last try. safe: a failed read of index.json counts as not due (as due_now)."""
    if not PUSH_DUE.exists() or (_last_push_try is not None and now() - _last_push_try < RETRY):
        return False
    try:
        return listing_done(now().date())
    except (OSError, ValueError):
        if safe:
            return False
        raise


_not_announced_said: dt.date | None = None


def not_announced() -> None:
    """mh -l ran and wrote no table for today: arXiv has not announced the listing yet. Said
    once a morning; the driver checks again every RETRY."""
    global _not_announced_said
    wake = now() + RETRY
    if _not_announced_said != now().date():
        _not_announced_said = now().date()
        log(f"arXiv has not announced today's listing yet; checking every {RETRY.seconds // 60} minutes")
    sleep_until(wake, "waiting for arXiv to announce today's listing")


def stopped_in(output: str) -> str | None:
    """The listing a run stopped partway through (local_run.py's line), if it says."""
    found = re.findall(r"Stopped partway through (.+?); the next run", output)
    return found[-1] if found else None


def after_stop(code: int, output: str, what: str, retry: bool = False) -> None:
    """Log why a run stopped and sleep: until the usage limit resets (a weekly limit is said
    once, then checked quietly every WAKE), or a while (RETRY after a failure, or with
    retry=True; WAKE otherwise)."""
    if STOP.exists():
        return
    if reported := reported_limit(output):
        kind, reset = reported
        listing = stopped_in(output)
        where = f" in {listing} (its scores so far are saved)" if listing else ""
        seat = "To use another seat: mh --stop, sign in, mh -d."
        wake = wake_time(kind, reset)
        known, key = read_state(LIMIT), reset.isoformat(timespec="minutes")
        if (known.get("kind"), known.get("until")) != (kind, key):  # R4: a reset not yet said
            if kind == "weekly":
                log(f"Weekly limit reached{where}. No scoring until {dtab.shown(reset)}; the morning listings "
                    f"wait too. {seat}")
            else:
                zone = dtab.display_zone()
                other_day = wake.astimezone(zone).date() != now().astimezone(zone).date()
                log(f"Usage limit reached{where}; scoring resumes at {dtab.shown(wake, day=other_day)}.")
            write_state(LIMIT, {"kind": kind, "until": key, "since": now().isoformat(timespec="seconds")})
        elif known.get("ended"):  # the same reset met again after a run passed it: in force again, quietly
            known.pop("ended")
            write_state(LIMIT, known)
        doing = (f"weekly limit until {dtab.shown(reset)}, checked every 2½ hours ({seat[:-1]})" if kind == "weekly"
                 else "5-hour usage limit; sleeping")
        sleep_until(wake, doing, cap=False, morning=False)
        return
    if code != 0 or retry:
        wake = now() + RETRY
        log(f"{what[:1].upper()}{what[1:]} not finished, {lr.exit_words(code)}; trying again at {dtab.shown(wake, day=False)}")
        sleep_until(wake, f"{what} not finished, {lr.exit_words(code)}; trying again")
    else:
        wake = now() + WAKE
        log(f"{what[:1].upper()}{what[1:]} stopped with no reset time given; trying again at {dtab.shown(wake, day=False)}")
        sleep_until(wake, f"{what} stopped with no reset time given; sleeping")


def is_driver(pid: int) -> bool:
    """Whether a live process `pid` is the driver (after a reboot, a pid may name another process)."""
    try:
        command = " " + Path(f"/proc/{pid}/cmdline").read_bytes().decode().replace("\0", " ")
    except OSError:
        return False
    return "--drive" in command or " -d" in command


def running() -> bool:
    """Whether a driver is running: its pid file names a live process that is the driver."""
    try:
        return is_driver(int(PID.read_text()))
    except (OSError, ValueError):
        return False


def code_version() -> str:
    """The scorer's code as committed (the tree of scorer/), to notice a pulled change."""
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD:scorer"],
                          capture_output=True, text=True).stdout.strip()


def changed_tables() -> list[str]:
    """Files under tables/ with uncommitted changes, which stop every run (local_run.py, sync)."""
    out = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no", "--", "tables"],
                         capture_output=True, text=True).stdout
    return [l[3:] for l in out.splitlines() if l.strip()]


def wait_for_maintainer(changed: list[str]) -> None:
    """Uncommitted changes in tables/ may be the maintainer's own work, so the driver never
    discards them: it says so once and waits quietly, resuming as soon as they are gone."""
    shown = ", ".join(changed[:5]) + (", …" if len(changed) > 5 else "")
    likely = ("most likely a run cut off between writing a table and committing it (a reboot, say): "
              "`git checkout -- tables` discards them, and the next run finishes the day from .work/")
    log(f"Waiting for you: tables/ has uncommitted changes ({shown}). Commit or discard them; "
        f"the driver resumes by itself once they are gone. Unless they are your own, it is {likely}.")
    status(f"waiting for you: tables/ has uncommitted changes ({shown}); commit or discard them ({likely})")
    record_problem(f"tables/ has uncommitted changes ({shown}); {likely}", f"tables/ had uncommitted changes ({shown})")
    while changed_tables() and not STOP.exists():
        time.sleep(TICK)
    if not STOP.exists():
        clear_problem()
        log("tables/ has no uncommitted changes now; resuming")


RESTART = "MAPHUB_DRIVER_RESTART"  # set to the driver's pid just before it restarts itself with new code


def ended_unstopped(previous: dict, restarting: bool = False) -> str | None:
    """For a driver that ended without stopping (a reboot, WSL closing, a kill), its pid and what
    it was doing since when, from the status it left; None if it stopped or crashed (said as it
    happened), is running, or is this one restarting itself."""
    pid = previous.get("pid")
    doing = previous.get("doing", "")
    if restarting or not pid or doing == "stopped" or doing.startswith("crashed") or is_driver(pid):
        return None
    since = dt.datetime.fromisoformat(previous["since"]) if previous.get("since") else None
    doing = re.sub(r" \(mh [^)]*\)", "", doing or "running")
    return f"pid {pid}, {doing}{' since ' + dtab.shown(since) if since else ''}"


def drive() -> int:
    global STARTED
    STATE.mkdir(parents=True, exist_ok=True)
    # This driver restarting itself with new code (os.execv keeps the pid), not a new start:
    restarting = os.environ.pop(RESTART, None) == str(os.getpid())
    if running() and not restarting and PID.read_text().strip() != str(os.getpid()):  # a stale file may name this pid
        print(f"The driver is already running (pid {PID.read_text().strip()}); `mh --stop` stops it.")
        return 1
    previous = read_state(STATUS)
    PID.write_text(f"{os.getpid()}\n")
    for flag_path in (STOP, HOLD, YIELD):
        flag_path.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: STOP.touch())
    STARTED = previous.get("started") if restarting and previous.get("started") else now().isoformat(timespec="seconds")
    if not restarting:
        log(f"Driver started (pid {os.getpid()})")
    if dead := ended_unstopped(previous, restarting):  # said once: the status is overwritten next
        log(f"The previous driver ({dead}) ended without stopping (a reboot, or WSL closing?)")
        record_problem(f"the driver ({dead}) ended without stopping")
    started = code_version()
    crashed = False
    survey_wait: dt.datetime | None = None  # the survey is complete: no backfill until then
    survey_said: dt.date | None = None
    try:
        while not STOP.exists():
            # Code pickup: each run pulls and merges main first, so code pushed from another
            # computer reaches the runs on its own; when the scorer's own code changed, the driver
            # restarts in place (between runs, so no listing is cut short) to run the new code too.
            if (current := code_version()) and started and current != started:
                log(f"The scorer's code changed ({started[:7]} -> {current[:7]}); restarting the driver")
                status("restarting with new code")
                os.environ[RESTART] = str(os.getpid())
                os.execv(sys.executable, [sys.executable, *sys.argv])
            if changed := changed_tables():
                wait_for_maintainer(changed)
                continue
            if push_due():  # the push after the morning run failed: tried again between backfills
                push(retrying=True)
            if survey_wait and not morning_due():
                if now() < survey_wait:  # the survey is complete: nothing to backfill until the next morning
                    weekday_push = PUSH_DUE.exists() and now().weekday() < 5
                    sleep_until(min(survey_wait, now() + RETRY) if weekday_push else survey_wait,
                                "legacy survey complete; waiting for the next morning listing", cap=False)
                    continue
                survey_wait = None
            if morning_due():
                survey_wait = None  # after the morning run, one backfill fills any gap
                status("scoring the morning listing (mh -l)")
                code, output = run("-l")
                note_limit(output)
                if listing_done(now().date()):
                    review_and_push()  # a failed push does not hold up the backfills
                elif code == 0 and not reported_limit(output):
                    not_announced()
                else:
                    after_stop(code, output, "the morning listing", retry=True)
                continue
            status("backfilling (mh -bb)")
            code, output = run("-bb", watch_morning=True)
            note_limit(output)
            if morning_due():
                continue
            if code == 3:  # the legacy survey is complete: only the morning listings are left
                survey_wait = next_listing()
                if survey_said != now().date():  # said once a day
                    survey_said = now().date()
                    log(f"Legacy survey complete; waiting for the next morning listing, {dtab.shown(survey_wait)}")
                continue
            after_stop(code, output, "backfilling")
    except KeyboardInterrupt:
        pass
    except Exception as e:  # a fault in the driver itself: said, kept, and shown by mh --status
        crashed = True  # set first: should the writes below fail, finally still does not say "stopped"
        what = f"{type(e).__name__}: {e}"[:300]
        write([stamp(f"Driver crashed: {what}"), *traceback.format_exc().rstrip().splitlines()])
        record_problem(f"the driver crashed: {what}")
        status(f"crashed: {what}")
        return 1
    finally:
        if not crashed:
            log("Driver stopped")
            status("stopped")
        for path in (PID, STOP, HOLD, YIELD):
            path.unlink(missing_ok=True)
    return 0


def note_limit(output: str) -> None:
    """A 5-hour limit is over once a run meets no limit (a weekly one ends in run, with its line)."""
    limit = read_state(LIMIT)
    if limit.get("kind") == "5-hour" and not limit.get("ended") and not reported_limit(output):
        end_limit()


def table_range() -> str:
    dates = list(dtab.index_dates(ROOT / "tables"))
    if not dates:
        return "none yet"
    return f"{dates[0]} to {dates[-1]}, {len(dates)} listings"


def last_push() -> str:
    pushed = read_state(STATE / "pushed.json")
    if pushed.get("when"):
        return dtab.shown(dt.datetime.fromisoformat(pushed["when"]))
    out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cI", "@{upstream}"],
                         capture_output=True, text=True).stdout.strip()
    return f"{dtab.shown(dt.datetime.fromisoformat(out))} (the tables branch's last commit)" if out else "none"


def limit_line() -> str:
    limit = read_state(LIMIT)
    if not limit:
        return "none"
    until = dtab.shown(dt.datetime.fromisoformat(limit["until"]))
    if not limit.get("ended") and dt.datetime.fromisoformat(limit["until"]) <= now():
        return f"none known (the last, {limit['kind']}, was to reset {until}; no run has scored since)"
    if not limit.get("ended"):
        return f"{limit['kind']}, until {until}" + ("; checked every 2½ hours (to use another seat: mh --stop, sign in, mh -d)"
                                                    if limit["kind"] == "weekly" else "")
    return f"none (the last, {limit['kind']}, ended {dtab.shown(dt.datetime.fromisoformat(limit['ended']))})"


def problem_line() -> str | None:
    problem = read_state(PROBLEM)
    if not problem:
        return None
    when = dtab.shown(dt.datetime.fromisoformat(problem["when"]))
    if problem.get("cleared"):
        cleared = dt.datetime.fromisoformat(problem["cleared"])
        same_day = cleared.astimezone(dtab.display_zone()).date() == \
            dt.datetime.fromisoformat(problem["when"]).astimezone(dtab.display_zone()).date()
        return f"{when}, {problem['past']} (cleared {dtab.shown(cleared, day=not same_day).rsplit(' ', 1)[0]})"
    return f"{when}, {problem['what']}"


def report() -> int:
    """mh --status: the driver, the tables, the last push, the usage limit, the last problem;
    then the commits waiting, listings set aside, and warnings still standing."""
    s = read_state(STATUS)
    if not s:
        print("Driver: never started on this computer.")
    elif running():
        doing = s.get("doing", "")
        if s.get("at") and doing.startswith("backfilling"):
            doing = f"backfilling (at {s['at']})"
        until = f", until {dtab.shown(dt.datetime.fromisoformat(s['until']))}" if s.get("until") else ""
        since = s.get("started") or s.get("since")
        print(f"Driver: running since {dtab.shown(dt.datetime.fromisoformat(since))}; {doing}{until}")
    elif s.get("doing", "").startswith("crashed"):
        print(f"Driver: not running; it {s['doing']} ({dtab.shown(dt.datetime.fromisoformat(s['since']))}; "
              f"{'the log has the traceback' if dtab.log_mode() != 'off' else 'no log kept (MAPHUB_LOG=off)'})")
    elif dead := ended_unstopped(s):
        print(f"Driver: not running; it ended without stopping ({dead})")
    else:
        print(f"Driver: not running; stopped {dtab.shown(dt.datetime.fromisoformat(s['since']))}"
              if s.get("since") else "Driver: not running")
    print(f"Tables: {table_range()}; last push {last_push()}")
    print(f"Limit: {limit_line()}")
    if problem := problem_line():
        print(f"Last problem: {problem}")
    if n := waiting_commits():
        print(f"Commits waiting to be pushed: {n}")
    for date, e in sorted(read_state(STATE / "set-aside.json").items()):
        if e.get("until"):
            again = dt.datetime.fromisoformat(e["until"])
            print(f"Set aside: {date}, after {e['failures']} failed runs (last: {e['error']}); "
                  f"tried again from {dtab.shown(again)}")
    for text in read_state(STATE / "warned.json").values():
        print(text)
    return 0


def flag(path: Path, what: str) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    if path == STOP and not running():
        # No driver to stop: a flag left behind would stop the next run started by hand, or the
        # next driver, at once.
        STOP.unlink(missing_ok=True)
        print("The driver is not running; nothing to stop.")
        return 0
    path.touch()
    print(what)
    return 0
