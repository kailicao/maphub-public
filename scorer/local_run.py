#!/usr/bin/env python3
"""MapHub's local runs: the latest listing, and backfills.

    mh -l, --latest      score the current listing; fold it with any waiting backfills
                         into one commit, and push
    mh -b, --back        backfill one listing: a day the morning runs missed, else the
                         weekday before the earliest table; commit locally
    mh -bb, --back-all   backfill listing after listing until the usage limit stops it
    mh -p, --push        push waiting backfills without a new listing
    mh -y, --yes         push without asking to confirm the commit message
    mh -d, --drive       run the daily routine unattended (scorer/driver.py): mh -l each
                         weekday morning, mh -bb the rest of the time
    mh --status, --hold, --stop    the driver's state; keep a listing under review from
                         being pushed; stop the driver once the listing it is
                         scoring is done (at once if it is sleeping or waiting)
    --via api            score through the Claude API (ANTHROPIC_API_KEY in .env)
                         instead of Claude Code

(`mh` runs this script.) Every mode first finishes a day left unfinished in .work/,
say by the usage limit: a backfill day is completed and committed locally, the
latest listing is completed, committed and pushed. New listings get a column for
every participant with status active or no-legacy, backfills (the legacy survey)
for every participant with status active, scored through Claude Code by default.
One commit per day: backfills are committed locally and wait, and
the next --latest (or --push) folds them into one commit, keeping each table's
provenance from the scorer. The host works on the tables branch (the code of main
with the tables): each run first merges any new code from main and pushes the merge,
so that waiting backfills stay on top. Pushes to it start "Publish viewer".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # maphub-public
sys.path.insert(0, str(ROOT / "scorer"))
import daily_table as dtab  # noqa: E402

TABLES = ROOT / "tables"
PORTFOLIOS = ROOT / "maphub-portfolios"
WORK = ROOT / ".work"
VIA = "claude-code"  # how the scorer sends requests (--via)
CODE = "main"  # the branch the code is developed on; the host runs on a branch of tables merged from it
COMPLETE = "Legacy survey complete:"  # the subject of the commit that records the survey's start
SUBJECT = re.compile(r"^Table for (\d{4}-\d{2}-\d{2}) \(via ([\w-]+)\)$")


def quiet_weekend() -> bool:
    """An unattended run (started by the driver, which sets MAPHUB_UNATTENDED) on a Saturday or
    Sunday, Eastern time: it pushes nothing, neither tables nor a merge of new code, which wait
    for Monday morning's run. A push started by hand (mh -p) goes out on any day."""
    return bool(os.environ.get("MAPHUB_UNATTENDED")) and dt.datetime.now(dtab.EASTERN).weekday() >= 5


class RunError(Exception):
    pass


class SurveyComplete(RunError):
    """The legacy survey reached the first listing of the surveyed categories."""


class Incomplete(RunError):
    """The scorer stopped partway (exit 2), most likely at the usage limit; its saved
    requests let the next run finish the day."""


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            env={**os.environ, **(env or {})})
    if check and result.returncode != 0:
        raise RunError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def rebuild_index() -> None:
    """tables/index.json, empty.json and the top-paper summaries (top/) from the Parquet files present: settles a conflict between
    a pending backfill and a table pushed meanwhile from another computer."""
    (TABLES / "index.json").write_text("{}\n", encoding="utf-8")
    dates: dict[dt.date, list[str]] = {}
    for f in TABLES.glob("*/*/*.parquet"):
        dates.setdefault(dt.date.fromisoformat(f.stem), []).append(f.parent.name)
    for date, yymms in sorted(dates.items()):
        dtab.write_index(TABLES, date, sorted(yymms))
    dtab.write_empty_list(TABLES)
    dtab.rebuild_top(TABLES)


def sync() -> None:
    """Bring the repos up to date; pending backfill commits are replayed on top."""
    if dtab.portfolios_repo(PORTFOLIOS)[1]:  # portfolios in a repository of their own
        git(PORTFOLIOS, "pull", "-q", "--ff-only")
    if git(ROOT, "status", "--porcelain", "--untracked-files=no", "--", "tables"):
        raise RunError("tables/ has uncommitted changes; commit or discard them first")
    before = git(ROOT, "rev-parse", "HEAD:scorer", check=False)
    pulled = subprocess.run(["git", "-C", str(ROOT), "pull", "-q", "--rebase", "--autostash"], capture_output=True, text=True)
    rebasing = lambda: (ROOT / ".git" / "rebase-merge").exists() or (ROOT / ".git" / "rebase-apply").exists()
    if pulled.returncode != 0 and not rebasing():
        raise RunError(f"git pull: {pulled.stderr.strip()}")
    while rebasing():
        conflicts = git(ROOT, "diff", "--name-only", "--diff-filter=U").split()
        if not conflicts or not all(c in ("tables/index.json", "tables/empty.json") or c.startswith("tables/top/") for c in conflicts):
            git(ROOT, "rebase", "--abort", check=False)
            raise RunError(f"pull conflicts in {', '.join(conflicts) or 'unknown files'}; resolve by hand")
        rebuild_index()
        git(ROOT, "add", "-A", "-f", "tables/index.json", "tables/empty.json", "tables/top")
        git(ROOT, "rebase", "--continue", env={"GIT_EDITOR": "true"}, check=False)
    merge_code()
    # A pull that brought new scorer code restarts this run, so that the run uses it throughout
    # (the scoring, which runs daily_table.py afresh, would; the folding here would not).
    if before and git(ROOT, "rev-parse", "HEAD:scorer", check=False) != before:
        print("The scorer's code changed with the pull; restarting with it.", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])


def merge_code() -> None:
    """Code pickup on the tables branch: merge new commits of main into the branch as last
    pushed, push the merge, and replay any waiting table commits on top. main never
    touches tables/ and the tables branch nothing else, so the merge has no conflicts."""
    branch = git(ROOT, "branch", "--show-current")
    if branch == CODE and subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", "tables/index.json"]).returncode == 0:
        raise RunError(f"this is {CODE}, which leaves tables/ out; run on the tables branch "
                       "(`git switch tables`, or `git switch -c tables` in a new copy: RUNNING.md, Setting up a copy, step 1)")
    if branch in ("", CODE):  # on main in a MapHub that keeps its tables there, or detached
        return
    if quiet_weekend():
        print("Weekend: new code from main, if any, is merged on Monday morning's run.", flush=True)
        return
    git(ROOT, "fetch", "-q", "origin", CODE)
    if subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", f"origin/{CODE}", "@{upstream}"]).returncode == 0:
        return
    upstream = git(ROOT, "rev-parse", "--abbrev-ref", "@{upstream}")  # origin/tables
    git(ROOT, "checkout", "-q", "--detach", upstream)
    try:
        merged = subprocess.run(["git", "-C", str(ROOT), "merge", "-q", "--no-edit", "-m",
                                 f"Merge {CODE} into {branch}: code pickup", f"origin/{CODE}"], capture_output=True, text=True)
        if merged.returncode != 0:
            git(ROOT, "merge", "--abort", check=False)
            raise RunError(f"merging {CODE} into {branch}: {merged.stderr.strip() or merged.stdout.strip()}; resolve by hand")
        git(ROOT, "push", "-q", "origin", f"HEAD:{upstream.split('/', 1)[1]}")
    finally:
        git(ROOT, "checkout", "-q", branch)
    git(ROOT, "rebase", "-q", upstream)  # the push moved it to the merge
    print(f"Merged new code from {CODE} ({git(ROOT, 'log', '-1', '--format=%h', f'origin/{CODE}')}); "
          f"{git(ROOT, 'rev-list', '--count', '@{upstream}..HEAD')} table commit(s) wait on top.", flush=True)


def active_participants(legacy: bool = False) -> list[str]:
    """The participants scored: on a new listing everyone scored daily (status active
    or no-legacy); on a past listing, the legacy survey (status active)."""
    return sorted(dtab.load_participants(PORTFOLIOS, legacy=legacy))


def score(date: dt.date | None, daily: bool = False) -> bool:
    """Run the scorer for all active columns; True if a table was written.
    A past date is the legacy survey (status active); daily=True scores it as a daily
    run instead (status no-legacy too), for a morning listing finished later.
    Raises Incomplete when it stopped partway, RunError on any other failure."""
    message = WORK / "commit-message.txt"
    message.unlink(missing_ok=True)
    command = [sys.executable, str(ROOT / "scorer" / "daily_table.py"), "--via", VIA]
    if date is not None:
        command += [date.isoformat(), *(["--daily"] if daily else [])]
    env = {**os.environ, "MAPHUB_OWN_PARTICIPANTS": ",".join(active_participants())}
    result = subprocess.run(command, env=env)
    if result.returncode == 2:
        raise Incomplete(f"the scorer stopped partway through {date or 'the latest listing'} "
                         "(the usage limit?); the next run finishes it")
    if result.returncode != 0:
        raise RunError(f"the scorer stopped (exit {result.returncode}); rerun to resume")
    return message.exists()


def commit_locally() -> str:
    """Commit the table just written with the scorer's provenance message."""
    # -f: main's .gitignore leaves tables/ out, so that only the tables branch holds them
    git(ROOT, "add", "-A", "-f", "--", "tables", ":(exclude,glob)tables/**/*.tmp")
    git(ROOT, "commit", "-q", "-F", str(WORK / "commit-message.txt"))
    return git(ROOT, "log", "-1", "--format=%s")


def pending() -> list[str]:
    """Messages of the table commits not yet pushed, oldest first."""
    log = git(ROOT, "log", "--reverse", "--format=%B%x00", "@{upstream}..HEAD")
    return [m.strip() for m in log.split("\x00") if m.strip()]


def date_span(dates: list[str]) -> str:
    """Listing dates for a commit subject, newest first: each run of consecutive
    weekdays as a range, the year once at the end (or after each run when the
    years differ). ["2026-10-15", "2026-09-01", ..., "2026-09-07"] gives
    "Oct 15, Sep 1–7, 2026"; weekends never have a listing, so they do not
    break a run."""
    days = sorted({dt.date.fromisoformat(d) for d in dates})
    runs: list[list[dt.date]] = []
    for day in days:
        previous = runs[-1][-1] if runs else None
        step = 3 if previous and previous.weekday() == 4 else 1  # Friday to Monday
        if previous and (day - previous).days == step:
            runs[-1].append(day)
        else:
            runs.append([day])
    one_year = len({d.year for d in days}) == 1
    year = lambda d: "" if one_year else f", {d.year}"
    parts = []
    for run in reversed(runs):
        a, b = run[0], run[-1]
        if a == b:
            parts.append(f"{a:%b} {a.day}{year(a)}")
        elif a.year != b.year:
            parts.append(f"{a:%b} {a.day}, {a.year}–{b:%b} {b.day}, {b.year}")
        elif a.month == b.month:
            parts.append(f"{a:%b} {a.day}–{b.day}{year(b)}")
        else:
            parts.append(f"{a:%b} {a.day}–{b:%b} {b.day}{year(b)}")
    return ", ".join(parts) + (f", {days[0].year}" if one_year else "")


SHARED = ("Code:", "Portfolios:", "Configuration:", "Columns:")  # provenance lines a group of tables shares
PAPERS = re.compile(r"^Papers: (\d+)(?: \((\d+) already scored on another date, left out\))?$")
PROMOTED = re.compile(r"^Full portfolio \(outline score (\d+) or more\): (.*)$")


def combined(messages: list[str]) -> str:
    """One message for several tables: a subject naming them (date_span), then their
    provenance by group, newest first. Tables with the same portfolios, configuration
    and columns form one group under its dates (date_span), with the papers and the
    full-pass counts summed; a line only one table has (papers left out as already
    scored, a note, a metadata fallback) keeps its date. A single table keeps the
    scorer's message."""
    if len(messages) == 1:
        return messages[0] + "\n"
    # The commit recording the survey's end travels with the tables, as lines of its own.
    ends = [m for m in messages if m.startswith(COMPLETE)]
    messages = [m for m in messages if not m.startswith(COMPLETE)]
    if ends and not messages:
        return "\n\n".join(ends) + "\n"
    if ends:
        return combined(messages).rstrip("\n") + "\n\n" + "\n\n".join(ends) + "\n"
    heads = [SUBJECT.match(m.splitlines()[0]) for m in messages]
    if not all(heads):
        raise RunError("an unpushed commit is not a table commit; push or fold it by hand")
    dates = [h.group(1) for h in heads]
    routes = sorted({h.group(2) for h in heads})
    groups: dict[tuple, list[tuple[str, list[str]]]] = {}
    for head, message in zip(heads, messages):
        body = [l for l in message.splitlines()[1:] if l.strip()]
        key = (head.group(2), *(l for l in body if l.startswith(SHARED)))
        groups.setdefault(key, []).append((head.group(1), body))
    lines = [f"Tables for {date_span(dates)} (via {', '.join(routes)})"]
    for key, tables in sorted(groups.items(), key=lambda g: max(d for d, _ in g[1]), reverse=True):
        tables.sort(reverse=True)
        if len(tables) == 1:
            date, body = tables[0]
            lines += ["", date, *body]
            continue
        papers = skipped = 0
        threshold, promoted, own = None, {}, []
        for date, body in tables:
            for l in body:
                if l.startswith(SHARED):
                    continue
                if m := PAPERS.match(l):
                    papers += int(m.group(1))
                    if m.group(2):
                        skipped += int(m.group(2))
                        own.append(f"{date}: {m.group(2)} papers already scored on another date, left out")
                elif m := PROMOTED.match(l):
                    threshold = m.group(1)
                    for item in m.group(2).split(", "):
                        who, n = item.rsplit(" ", 1)
                        promoted[who] = promoted.get(who, 0) + int(n)
                else:
                    own.append(f"{date}: {l}")
        lines += ["", f"{date_span([d for d, _ in tables])} ({len(tables)} tables)", *key[1:],
                  f"Papers: {papers}" + (f" ({skipped} already scored on another date, left out)" if skipped else "")]
        if promoted:
            lines.append(f"Full portfolio (outline score {threshold} or more): "
                         + ", ".join(f"{w} {promoted[w]}" for w in sorted(promoted)))
        lines += own
    return "\n".join(lines) + "\n"


def publish(confirm: bool) -> None:
    """Fold the unpushed table commits into one and push it."""
    messages = pending()
    if not messages:
        print("Nothing to push.")
        return
    if quiet_weekend():
        print(f"Weekend: {len(messages)} commit(s) wait for Monday morning's push.")
        return
    message = combined(messages)
    print("--- commit message ---\n" + message + "----------------------")
    if confirm and not sys.stdin.isatty():
        # No terminal to answer in (e.g. a command run from a chat): leave the commits waiting.
        print("Not pushed: no terminal to confirm in. Run `mh -p` in a terminal, or `mh -p -y`.")
        return
    if confirm and input("Commit and push? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        print("Not pushed; the commits wait locally.")
        return
    if len(messages) > 1:
        git(ROOT, "reset", "-q", "--soft", "@{upstream}")
        path = WORK / "combined-message.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(message, encoding="utf-8")
        git(ROOT, "commit", "-q", "-F", str(path))
    git(ROOT, "push", "-q")
    print(f"Pushed: {git(ROOT, 'log', '-1', '--format=%h %s')}")
    shutil.rmtree(WORK, ignore_errors=True)


def unfinished() -> list[tuple[dt.date, str]]:
    """Days started in .work/ but without a table yet, oldest first, each with its
    kind: "back" for a rebuilt past listing (it leaves window.json beside its saved
    papers), "latest" for a listing read from the RSS feed. Test runs, which save
    their scores in a subfolder, do not count."""
    have = dtab.index_dates(TABLES)
    days = []
    for d in sorted(WORK.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")):
        if d.name not in have and any((d / "scores").glob("*/chunk*.json")):
            days.append((dt.date.fromisoformat(d.name), "back" if (d / "window.json").exists() else "latest"))
    return days


def finish_unfinished(confirm: bool) -> None:
    """Complete any day left unfinished: a backfill day is committed locally; the
    latest listing is committed and pushed (with any waiting backfills)."""
    latest = False
    for date, kind in unfinished():
        print(f"Finishing {date}, left unfinished ({'latest listing' if kind == 'latest' else 'backfill'})")
        if score(date, daily=kind == "latest"):
            print(f"Committed locally: {commit_locally()}")
            latest |= kind == "latest"
    if latest:
        publish(confirm)


def back_date() -> dt.date:
    """The next listing to backfill. Gaps first: the most recent weekday between the
    earliest and latest tables without a table (a day the morning run missed).
    With no gaps, the weekday before the earliest table: the next listing back.
    A recent holiday the morning run did not meet is such a gap: rebuilt, its papers
    are already in the next listing's table, so it gets an empty table, as a holiday
    met by the morning run does. Older holidays get a table of the submissions they
    would have held, so no paper is lost or scored twice."""
    dates = sorted(dt.date.fromisoformat(d) for d in dtab.index_dates(TABLES))
    if not dates:
        raise RunError("no tables yet; run `mh -l` (--latest) first")
    done = {d.isoformat() for d in dates}
    day = dates[-1] - dt.timedelta(days=1)
    while day > dates[0]:
        if day.weekday() < 5 and day.isoformat() not in done:
            return day
        day -= dt.timedelta(days=1)
    day = dates[0] - dt.timedelta(days=1)
    while day.weekday() >= 5:  # no listings on weekends
        day -= dt.timedelta(days=1)
    start = dtab.survey_start(TABLES)
    if start and day < start:
        raise SurveyComplete(f"the legacy survey is complete: it reached the first listing of the surveyed categories, {start}")
    if (day.year, day.month) != (dates[0].year, dates[0].month) and not categories_begun(day):
        (TABLES / "survey.json").write_text(json.dumps({"start": dates[0].isoformat()}) + "\n", encoding="utf-8")
        dtab.write_top(TABLES, dates[0])  # the first year is now complete, if every weekday since has a table
        lines = [f"{COMPLETE} the surveyed categories begin with the listing of {dates[0]}"]
        if dtab.TOP_WRITTEN:
            lines += ["", f"Top-paper summaries written for {dates[0].year}, now complete (tables/top/)"]
        git(ROOT, "add", "-A", "-f", "tables/survey.json", "tables/top")
        git(ROOT, "commit", "-q", "-m", "\n".join(lines))
        raise SurveyComplete(f"the legacy survey is complete: the surveyed categories begin with the listing of {dates[0]}")
    return day


SILENT_MONTHS = 12  # months without a paper in any surveyed listing that mark their beginning


def categories_begun(day: dt.date) -> bool:
    """Whether the surveyed listings had begun by the month of `day`, from arXiv's monthly
    listing pages (one request per listing): yes as soon as one shows a paper in that month or
    in the SILENT_MONTHS - 1 before it, or when a page cannot be read (never end the survey
    on a failure); no only when every listing shows none in all of them."""
    listings = dtab.load_config(ROOT / "config-log.json", dt.datetime.now(dtab.EASTERN).date())["listings"]
    month = day.replace(day=1)
    for _ in range(SILENT_MONTHS):
        for listing in listings:
            total = dtab.listing_month_total(listing, month)
            if total is None or total > 0:
                return True
        month = (month - dt.timedelta(days=1)).replace(day=1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("-l", "--latest", action="store_true", help="score the current listing, then push")
    mode.add_argument("-b", "--back", action="count", default=0,
                      help="backfill one listing; -bb: until the usage limit stops it")
    mode.add_argument("--back-all", action="store_true", help="the same as -bb")
    mode.add_argument("-p", "--push", action="store_true", help="push waiting backfills")
    mode.add_argument("-d", "--drive", action="store_true", help="run the daily routine unattended (scorer/driver.py)")
    mode.add_argument("--status", action="store_true", help="what the driver is doing")
    mode.add_argument("--hold", action="store_true", help="keep the listing under review from being pushed")
    mode.add_argument("--stop", action="store_true", help="stop the driver once the listing it is scoring is done (at once if it is sleeping or waiting for review)")
    ap.add_argument("-y", "--yes", action="store_true", help="push without asking to confirm the message")
    ap.add_argument("--via", choices=["claude-code", "api"], default="claude-code",
                    help="score through Claude Code (default) or the Claude API (ANTHROPIC_API_KEY in .env)")
    args = ap.parse_args()
    if args.drive or args.status or args.hold or args.stop:
        import driver
        if args.drive:
            return driver.drive()
        if args.status:
            return driver.report()
        if args.hold:
            if driver.REVIEW <= dt.timedelta(0):
                print("Nothing to hold: the driver pushes a new listing at once (REVIEW is 0 in scorer/driver.py).")
                return 0
            return driver.flag(driver.HOLD, "Held: the driver will not push the listing under review; push it with `mh -p`.")
        return driver.flag(driver.STOP, "The driver stops once it finishes the listing it is scoring, or at once if it is sleeping "
                           "or waiting for review; commits it has not pushed wait for `mh -p`. "
                           "`mh --status` shows when it has stopped.")
    global VIA
    VIA = args.via
    if args.back_all:
        args.back = 2
    confirm = not args.yes
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with the scorer's
    dtab.load_env(ROOT / ".env")
    try:
        sync()
        finish_unfinished(confirm)
        if args.latest:
            if score(None):
                print(f"Committed locally: {commit_locally()}")
            publish(confirm)
        elif args.back:
            done = 0
            while True:
                date = back_date()
                print(f"Backfilling {date}, columns: {', '.join(active_participants(legacy=True))}")
                if not score(date):
                    raise RunError(f"no table was written for {date}; check the scorer's output")
                print(f"Committed locally: {commit_locally()}")
                done += 1
                if args.back == 1:
                    break
                if (ROOT / ".cache" / "driver" / "yield").exists() or (ROOT / ".cache" / "driver" / "stop").exists():
                    print("Stopping the backfill: the driver needs the seat (morning listing, or a stop).")
                    break
            print(f"{len(pending())} commit(s) wait for the next --latest or --push.")
        elif args.push:
            publish(confirm)
        return 0
    except SurveyComplete as e:
        print(f"{e}; nothing is left to backfill.")
        return 3  # the driver then waits for the morning listings
    except Incomplete as e:
        print(f"Stopped: {e}.")
        if args.back:
            print(f"{len(pending())} commit(s) wait for the next --latest or --push.")
        return 0 if args.back > 1 else 2
    except RunError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
