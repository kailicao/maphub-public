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
import codecs
import contextlib
import fcntl
import datetime as dt
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
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
STATE = ROOT / ".cache" / "driver"
SET_ASIDE = STATE / "set-aside.json"  # backfill listings that failed FAILS runs in a row (mh --status lists them)
WARNED = STATE / "warned.json"  # warnings an unattended run has already given, so that later runs do not repeat them
SAID = STATE / "said.json"  # notices an unattended run gives once a day (said_today)
PUSHED = STATE / "pushed.json"  # the last push: when, and its subject (mh --status)
FAILS = 3
RETRY_SET_ASIDE = dt.timedelta(days=7)  # a listing set aside is tried again after this long


def quiet_weekend() -> bool:
    """An unattended run (started by the driver, which sets MAPHUB_UNATTENDED) on a Saturday or
    Sunday, Eastern time: it pushes nothing, neither tables nor a merge of new code, which wait
    for Monday morning's run. A push started by hand (mh -p) goes out on any day."""
    return bool(os.environ.get("MAPHUB_UNATTENDED")) and dt.datetime.now(dtab.EASTERN).weekday() >= 5


class RunError(Exception):
    pass


class SurveyComplete(RunError):
    """The legacy survey reached the first listing of the surveyed categories (new: this run
    recorded it)."""

    def __init__(self, message: str, new: bool = False):
        super().__init__(message)
        self.new = new


class Incomplete(RunError):
    """The scorer stopped partway (exit 2), most likely at the usage limit; its saved
    requests let the next run finish the day. limit: stopped by a usage limit (the scorer gave
    no "Error:" line), which the driver reports itself."""

    def __init__(self, message: str, limit: bool = True):
        super().__init__(message)
        self.limit = limit


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


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(path: Path, data: dict) -> None:
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def warn_once(key: str, text: str | None) -> None:
    """Print a warning, but in unattended runs only when it is new: a problem that
    lasts is reported once, not by every run. text=None: the problem is gone."""
    warned = read_state(WARNED)
    if text is None:
        if key in warned:
            del warned[key]
            write_state(WARNED, warned)
        return
    if warned.get(key) != text or not os.environ.get("MAPHUB_UNATTENDED"):
        print(text, flush=True)
    warned[key] = text
    write_state(WARNED, warned)


def said_today(key: str) -> bool:
    """Whether an unattended run already gave the notice `key` today; records it if not.
    Runs started by hand always give it."""
    if not os.environ.get("MAPHUB_UNATTENDED"):
        return False
    said, today = read_state(SAID), dt.datetime.now(dtab.EASTERN).date().isoformat()
    if said.get(key) == today:
        return True
    said[key] = today
    write_state(SAID, said)
    return False


def unattended() -> bool:
    return bool(os.environ.get("MAPHUB_UNATTENDED"))


def say(text: str, *, full_only: bool = False) -> None:
    """A line of this run's output: for every log, or (full_only) for the full log and the terminal."""
    if full_only:
        dtab.detail(text)
    else:
        print(text, flush=True)


def changed_tables() -> list[str]:
    """Files under tables/ with uncommitted changes."""
    out = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no", "--", "tables"],
                         capture_output=True, text=True).stdout
    return [l[3:] for l in out.splitlines() if l.strip()]


def sync() -> None:
    """Bring the repos up to date; pending backfill commits are replayed on top."""
    if dtab.portfolios_repo(PORTFOLIOS)[1]:  # portfolios in a repository of their own
        try:
            git(PORTFOLIOS, "pull", "-q", "--ff-only")
            warn_once("portfolios", None)
        except RunError as e:
            # Waiting cannot fix this (a lost GitHub sign-in, say): score with the local copy, whose
            # commit each table's message records, and say so once.
            local = git(PORTFOLIOS, "log", "-1", "--format=%h of %cs", check=False)
            text = str(e)
            if re.search(r"askpass|could not read Username|terminal prompts disabled|Authentication failed", text, re.I):
                why = "git cannot reach the GitHub sign-in (VS Code's sign-in helper, say, is gone)"
            else:
                why = text.splitlines()[0][:200]
            warn_once("portfolios", f"Warning: pulling maphub-portfolios failed: {why}; scoring with the local "
                      f"copy ({local}) until a pull works. If git cannot reach the sign-in, restart the driver "
                      "from a terminal where git signs in to GitHub.")
    if changed := changed_tables():
        shown = ", ".join(changed[:5]) + (", …" if len(changed) > 5 else "")
        raise RunError(f"tables/ has uncommitted changes ({shown}); commit or discard them first "
                       "(the driver waits, and resumes once they are gone)")
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
    if problem := dtab.tables_branch_problem():  # on main, which leaves tables/ out
        raise RunError(problem)
    if branch in ("", CODE):  # on main in a MapHub that keeps its tables there, or detached
        return
    if quiet_weekend():
        if not said_today("weekend"):
            say("Weekend: new code from main, if any, is merged on Monday morning's run.", full_only=True)
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


class Said(RunError):
    """A failure the scorer has already reported in its own words (its "Error:" line, the
    message): not printed again."""


SUMMARY = WORK / "summary.json"  # what the table just written took (daily_table.py, write_summary)


def score(date: dt.date | None, daily: bool = False) -> dict | None:
    """Run the scorer for all active columns; the table's summary (date, papers, requests,
    seconds, tokens) if a table was written, else None.
    A past date is the legacy survey (status active); daily=True scores it as a daily
    run instead (status no-legacy too), for a morning listing finished later.
    Raises Incomplete when it stopped partway, RunError on any other failure."""
    message = WORK / "commit-message.txt"
    message.unlink(missing_ok=True)
    SUMMARY.unlink(missing_ok=True)
    command = [sys.executable, str(ROOT / "scorer" / "daily_table.py"), "--via", VIA]
    if date is not None:
        command += [date.isoformat(), *(["--daily"] if daily else [])]
    env = {**os.environ, "MAPHUB_OWN_PARTICIPANTS": ",".join(active_participants()), "MAPHUB_SUMMARY": str(SUMMARY)}
    # The scorer's output passes through, its marks for the logs kept; its last "Error:" line is
    # kept too, as the failure's own words (for a listing set aside, say).
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)
    error = None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if dtab.unmark(line).startswith("Error: "):
            error = dtab.unmark(line)[len("Error: "):].strip()
    code = proc.wait()
    if code == 2:
        raise Incomplete(f"Stopped partway through {date or 'the latest listing'}; the next run finishes it.",
                         limit=error is None)
    if code != 0:
        raise Said(error) if error else RunError(f"the scorer stopped with {exit_words(code)}")
    if not message.exists():
        return None
    try:
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


EXITS = {0: "done", 1: "an error", 2: "stopped partway", 3: "survey complete"}  # this script's exit codes


def exit_words(code: int) -> str:
    """'exit 3 (survey complete)'; a negative code is the signal that ended the run."""
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"exit {code} (ended by a signal, {name})"
    return f"exit {code} ({EXITS.get(code, 'an error')})"


def summary_text(summary: dict, usage: bool = True) -> str:
    """'162 papers scored, committed (9 requests, 85 s; tokens in 20 + cache write 203,600 +
    cache read 44,774, out 9,122)', for the brief log's line of a listing."""
    if summary.get("empty") or not summary.get("papers"):
        return "nothing to score; an empty table committed"
    text = f"{summary['papers']} papers scored, committed"
    t = summary.get("tokens")
    if usage and t:
        text += (f" ({summary.get('requests', 0)} requests, {summary.get('seconds', 0)} s; tokens in {t['input']:,} "
                 f"+ cache write {t['cache_write']:,} + cache read {t['cache_read']:,}, out {t['output']:,})")
    return text


class Briefly:
    """The brief log's lines for the listings a run scores: one line each, indented under the
    run's heading when there is one; once a backfill is long (FIRST_EACH listings), one line
    for every EVERY listings."""
    FIRST_EACH, EVERY = 5, 10

    def __init__(self, headed: bool):
        self.headed, self.count, self.group, self.waiting = headed, 0, [], None

    def heading(self, text: str, lazy: bool = False) -> None:
        """The run's heading; lazy: only once a listing is scored (a morning run that finds no
        listing yet, every 30 minutes, says nothing)."""
        self.headed = True
        if lazy:
            self.waiting = text
        else:
            dtab.in_brief(text)

    def say(self, text: str) -> None:
        if self.waiting:
            dtab.in_brief(self.waiting)
            self.waiting = None
        dtab.in_brief(text)

    def listing(self, date, summary: dict | None, note: str = "", own_line: bool = False) -> None:
        """One listing scored; own_line (the morning listing) always gets a line of its own."""
        date, summary = str(date), summary or {}
        self.count += 1
        if own_line:
            self.flush()
        if self.count <= self.FIRST_EACH or own_line:
            self.say(("  " if self.headed else "") + f"{date}{note}: {summary_text(summary)}")
            return
        self.group.append((date, summary))
        if len(self.group) >= self.EVERY:
            self.flush()

    def flush(self) -> None:
        if not self.group:
            return
        dates = [d for d, _ in self.group]
        papers = sum(s.get("papers", 0) for _, s in self.group)
        requests = sum(s.get("requests", 0) for _, s in self.group)
        self.say(f"Backfilled {max(dates)} to {min(dates)}: {len(dates)} listings, {papers} papers scored, "
                 f"committed ({requests} requests)")
        self.group = []


def commit_locally() -> str:
    """Commit the table just written with the scorer's provenance message (under git_lock)."""
    with git_lock():
        return _commit_locally()


def _commit_locally() -> str:
    # -f: main's .gitignore leaves tables/ out, so that only the tables branch holds them
    git(ROOT, "add", "-A", "-f", "--", "tables", ":(exclude,glob)tables/**/*.tmp")
    # Only tables/: anything else staged (by hand, say) stays out of the table's commit
    git(ROOT, "commit", "-q", "-F", str(WORK / "commit-message.txt"), "--", "tables")
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


FOLDED = "Tables for "  # the subject of a commit that folds several tables (combined)
FOLD = re.compile(r"^Tables for (.+) \(via ([\w, -]+)\)$")


def table_count(message: str) -> int:
    """The tables a commit message records: 1 for a table's own commit; for a folded one, its
    dates on lines of their own and its groups' "(N tables)"; 0 for any other commit."""
    lines = message.splitlines()
    if not lines:
        return 0
    if SUBJECT.match(lines[0]):
        return 1
    if not lines[0].startswith(FOLDED):
        return 0
    return sum(1 for l in lines[1:] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", l.strip())) + \
        sum(int(m.group(1)) for l in lines[1:] if (m := re.search(r"\((\d+) tables\)$", l.strip())))


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
    # The commit recording the survey's end, and those of metadata fallbacks written later
    # (daily_table.py --meta-only), travel with the tables, as lines of their own.
    own = lambda m: m.startswith((COMPLETE, dtab.META_SUBJECT, FOLDED))  # FOLDED: a fold left unpushed
    ends = [m for m in messages if own(m)]
    messages = [m for m in messages if not own(m)]
    if ends and not messages:
        return "\n\n".join(ends) + "\n"
    if ends:
        body = combined(messages).rstrip("\n")
        folds = [FOLD.match(m.splitlines()[0]) for m in ends if m.startswith(FOLDED)]
        if folds := [f for f in folds if f]:
            # A fold left unpushed (a push that failed, say) goes out with the new tables: the
            # subject names its dates too, after the new ones.
            heads = [SUBJECT.match(m.splitlines()[0]) for m in messages]
            if not all(heads):
                raise RunError("an unpushed commit is not a table commit; push or fold it by hand")
            routes = sorted({h.group(2) for h in heads} | {r.strip() for f in folds for r in f.group(2).split(",")})
            subject = (f"Tables for {date_span([h.group(1) for h in heads])}, "
                       f"{', '.join(f.group(1) for f in folds)} (via {', '.join(routes)})")
            body = (subject + body[body.index("\n"):]) if body.startswith(FOLDED) else f"{subject}\n\n{body}"
        return body + "\n\n" + "\n\n".join(ends) + "\n"
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
        say("Nothing to push.", full_only=unattended())
        return
    if quiet_weekend():
        say(f"Weekend: {len(messages)} commit(s) wait for Monday morning's push.", full_only=True)
        return
    if confirm and unattended():
        # The driver's own run: it pushes next (mh -p -y), after any time for review.
        say("Left for the driver to push.", full_only=True)
        return
    message = combined(messages)
    shown = "--- commit message ---\n" + message + "----------------------"
    sys.stdout.flush()
    for line in shown.splitlines():  # the terminal and the full log show it; the brief log has it in git
        say(line, full_only=True)
    if confirm and not sys.stdin.isatty():
        # No terminal to answer in (e.g. a command run from a chat): leave the commits waiting.
        print("Not pushed: no terminal to confirm in. Run `mh -p` in a terminal, or `mh -p -y`.")
        return
    # The question goes to the full log only (the log writer ends its line, since the answer
    # is typed at the terminal, not written to the output).
    question = (dtab.FULL_ONLY if dtab.marked() else "") + "Commit and push? [Y/n] "
    if confirm and input(question).strip().lower() not in ("", "y", "yes"):
        print("Not pushed; the commits wait locally.")
        return
    with git_lock():  # a run committing a table meanwhile (the driver's, say) waits, and is taken along
        if (now_waiting := pending()) != messages:
            messages, message = now_waiting, combined(now_waiting)
            print("A table was committed meanwhile; the pushed message names it too.", flush=True)
        before = git(ROOT, "rev-parse", "HEAD")
        if len(messages) > 1:
            fold(message)
        try:
            git(ROOT, "push", "-q")
        except RunError:
            # Unfold, so that the waiting commits stay as they were and the next push folds them again.
            git(ROOT, "reset", "-q", "--soft", before)
            raise
        tables = sum(table_count(m) for m in messages)
        subject = git(ROOT, "log", "-1", "--format=%s")
        print(f"Pushed: {subject}" + (f" ({tables} table{'s' if tables != 1 else ''})" if tables else ""), flush=True)
        write_state(PUSHED, {"when": dt.datetime.now(dtab.EASTERN).isoformat(timespec="seconds"),
                             "subject": subject, "tables": tables})
        clean_work()


@contextlib.contextmanager
def git_lock():
    """One git step on the tables at a time across mh runs (a by-hand mh -p and the driver's
    backfill, say): committing a table, folding and pushing, cleaning .work/. Held with
    fcntl on .cache/driver/git.lock, released when the run ends, even if it is killed. Reentrant
    within a run: flock on a second open file in the same process would wait for itself forever."""
    depth = getattr(_lock_held, "depth", 0)
    if depth:  # already held by this run (and thread)
        _lock_held.depth = depth + 1
        try:
            yield
        finally:
            _lock_held.depth = depth
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "git.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        _lock_held.depth = 1
        try:
            yield
        finally:
            _lock_held.depth = 0
            fcntl.flock(f, fcntl.LOCK_UN)


_lock_held = threading.local()


def clean_work() -> None:
    """After a push (under git_lock): remove the work folders of days whose table is committed,
    and the fold's message; days unfinished or set aside keep their saved requests. A run may be
    scoring meanwhile, so its commit message and summary stay (it replaces them itself), and
    nothing is removed while tables/ has uncommitted changes (a table written, not yet committed)."""
    if changed_tables():
        return
    have = dtab.index_dates(TABLES)
    for d in WORK.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"):
        if d.is_dir() and d.name in have:
            shutil.rmtree(d, ignore_errors=True)
    (WORK / "combined-message.txt").unlink(missing_ok=True)


def fold(message: str) -> None:
    """Fold the waiting commits into one with `message`: the folded commit has exactly the tree
    of the last waiting commit, on top of the branch as last pushed, so that nothing else staged
    in the index (an unrelated file, say) is swept into it; the index and working tree are left
    as they are."""
    path = WORK / "combined-message.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message, encoding="utf-8")
    folded = git(ROOT, "commit-tree", "HEAD^{tree}", "-p", "@{upstream}", "-F", str(path))
    git(ROOT, "reset", "-q", "--soft", folded)


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


def finish_unfinished(confirm: bool, brief: Briefly) -> None:
    """Complete any day left unfinished: a backfill day is committed locally; the
    latest listing is committed and pushed (with any waiting backfills)."""
    latest = False
    aside = resting()
    for date, kind in unfinished():
        if kind == "back" and date.isoformat() in aside:
            continue  # set aside: tried again when its rest is over
        what = "latest listing" if kind == "latest" else "backfill"
        say(f"Finishing {date}, left unfinished ({what})", full_only=True)
        try:
            wrote = score(date, daily=kind == "latest")
        except Incomplete:
            raise
        except RunError as e:
            if kind == "latest" or not record_failure(date, e):
                raise
            set_aside_note(date, e)
            continue
        if kind == "back":
            clear_failure(date)
        if wrote is not None:
            say(f"Committed locally: {commit_locally()}", full_only=True)
            brief.listing(date, wrote, f" (left over from the {'morning run' if kind == 'latest' else 'backfill'})")
            latest |= kind == "latest"
    if latest:
        publish(confirm)


def set_aside_note(date: dt.date, error: Exception) -> None:
    print(f"Set aside {date}: it failed {FAILS} runs in a row (last: {error}); the backfill goes on "
          f"without it and tries it again in {RETRY_SET_ASIDE.days} days (mh --status lists it).", flush=True)


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
    aside = resting()
    day = dates[-1] - dt.timedelta(days=1)
    while day > dates[0]:
        if day.weekday() < 5 and day.isoformat() not in done and day.isoformat() not in aside:
            return day
        day -= dt.timedelta(days=1)
    day = dates[0] - dt.timedelta(days=1)
    while day.weekday() >= 5 or day.isoformat() in aside:  # no listings on weekends; set-aside ones wait
        day -= dt.timedelta(days=1)
    start = dtab.survey_start(TABLES)
    if start and day < start:
        raise SurveyComplete(f"The legacy survey is complete: it reached the first listing of the surveyed categories, {start}")
    if (day.year, day.month) != (dates[0].year, dates[0].month) and not categories_begun(day):
        (TABLES / "survey.json").write_text(json.dumps({"start": dates[0].isoformat()}) + "\n", encoding="utf-8")
        dtab.write_top(TABLES, dates[0])  # the first year is now complete, if every weekday since has a table
        lines = [f"{COMPLETE} the surveyed categories begin with the listing of {dates[0]}"]
        if dtab.TOP_WRITTEN:
            lines += ["", f"Top-paper summaries written for {dates[0].year}, now complete (tables/top/)"]
        git(ROOT, "add", "-A", "-f", "tables/survey.json", "tables/top")
        git(ROOT, "commit", "-q", "-m", "\n".join(lines))
        raise SurveyComplete(f"The legacy survey is complete: the surveyed categories begin with the listing of {dates[0]}", new=True)
    return day


def resting() -> set[str]:
    """Backfill listings set aside and not yet due to be tried again."""
    t = dt.datetime.now(dtab.EASTERN)
    return {d for d, e in read_state(SET_ASIDE).items() if e.get("until") and t < dt.datetime.fromisoformat(e["until"])}


def record_failure(date: dt.date, error: Exception) -> bool:
    """Count a failed backfill of `date`; True when it is (again) set aside: FAILS runs in a
    row, or one more failure when it is tried again after RETRY_SET_ASIDE."""
    entries = read_state(SET_ASIDE)
    entry = entries.setdefault(date.isoformat(), {"failures": 0})
    entry["failures"] += 1
    entry["error"] = str(error)[:300]
    aside = entry["failures"] >= FAILS
    if aside:
        entry["until"] = (dt.datetime.now(dtab.EASTERN) + RETRY_SET_ASIDE).isoformat(timespec="minutes")
    write_state(SET_ASIDE, entries)
    return aside


def clear_failure(date: dt.date) -> None:
    entries = read_state(SET_ASIDE)
    if entries.pop(date.isoformat(), None) is not None:
        write_state(SET_ASIDE, entries)
        print(f"{date}: scored after earlier failures; no longer set aside.")


SILENT_MONTHS = 12  # months without a paper in any surveyed listing that mark their beginning


def categories_begun(day: dt.date) -> bool:
    """Whether the surveyed listings had begun by the month of `day`, from arXiv's monthly
    listing pages (one request per listing): yes as soon as one shows a paper in that month or
    in the SILENT_MONTHS - 1 before it; no only when every listing shows none in all of them.
    When a page cannot be read and none shows a paper, the answer is unknown: raise RunError,
    so that this run stops before scoring `day` and the next run asks again. Answering yes
    would never end the survey on a failure, but would backfill a month of empty listings
    before the categories began and record that month as the survey's start."""
    listings = dtab.load_config(ROOT / "config-log.json", dt.datetime.now(dtab.EASTERN).date())["listings"]
    month = day.replace(day=1)
    unread = []
    for _ in range(SILENT_MONTHS):
        for listing in listings:
            total = dtab.listing_month_total(listing, month)
            if total is None:
                unread.append(f"{listing} {month:%Y-%m}")
            elif total > 0:
                return True
        month = (month - dt.timedelta(days=1)).replace(day=1)
    if unread:
        raise RunError(f"could not read arXiv's monthly listing pages ({', '.join(unread)}) to check whether "
                       f"the surveyed categories had begun by {day:%Y-%m}; the next run checks again")
    return False


def logged_by_hand(args: list[str]) -> int:
    """Run this command again with its output both on the terminal and in the day's log, which
    the driver writes too (MAPHUB_LOG: in the brief log, `mh … (by hand)` and the run's own
    lines; in the full one, `$ mh … (by hand)`, the output and the exit code; with off, no log).
    The terminal shows the full detail whatever the log keeps, and stays the run's input, so
    `mh -p` can still ask before pushing. Its last warning or error is kept for mh --status."""
    import driver
    mode = dtab.log_mode()
    driver.log(f"$ mh {' '.join(args)} (by hand)" if mode == "full" else f"mh {' '.join(args)} (by hand)")
    split, every = dtab.OutputSplitter(mode), dtab.OutputSplitter("full")  # every: for the last problem, in any mode

    def note(lines: list[str]) -> None:
        for line in lines:
            if line.startswith(("Error: ", "Warning: ")):
                driver.record_problem(line.strip())

    proc = subprocess.Popen([sys.executable, __file__, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env={**os.environ, "MAPHUB_LOGGED": "1"})
    decode = codecs.getincrementaldecoder("utf-8")("replace")
    fd = proc.stdout.fileno()
    while True:
        if not select.select([fd], [], [], 0.5)[0]:  # quiet: a question may be waiting for an answer
            driver.log_output(split.cut())
            every.cut()
            continue
        if not (chunk := os.read(fd, 4096)):
            break
        text = decode.decode(chunk)
        shown, lines = split.feed(text)
        sys.stdout.write(shown)
        sys.stdout.flush()
        driver.log_output(lines)
        note(every.feed(text)[1])
    driver.log_output(split.end())
    note(every.end())
    code = proc.wait()
    if mode == "full" or code != 0:
        driver.log(f"{exit_words(code)} (by hand)" if mode == "full" else f"mh {' '.join(args)} (by hand): {exit_words(code)}")
    return code


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
    dtab.load_env(ROOT / ".env")  # MAPHUB_LOG and MAPHUB_TIMEZONE, for the driver and the logs too
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
    if not os.environ.get("MAPHUB_LOGGED"):  # started by hand: into the day's log as well
        return logged_by_hand(sys.argv[1:])
    global VIA
    VIA = args.via
    if args.back_all:
        args.back = 2
    confirm = not args.yes
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with the scorer's
    os.environ.setdefault("MAPHUB_RUN", f"{os.getpid()}-{dt.datetime.now().timestamp():.0f}")  # one run, for the scorer's once-a-run notes
    brief = Briefly(headed=False)
    try:
        try:
            sync()  # may restart this run with new code, so the heading comes after it
            if args.latest and unattended():
                brief.heading("Morning run", lazy=True)
            finish_unfinished(confirm, brief)
            if args.latest:
                if (summary := score(None)) is not None:
                    subject = commit_locally()
                    say(f"Committed locally: {subject}", full_only=True)
                    head = SUBJECT.match(subject)
                    brief.listing(summary.get("date") or (head.group(1) if head else "The latest listing"), summary, own_line=True)
                publish(confirm)
            elif args.back:
                try:
                    backfill(args.back == 1, brief)
                finally:
                    brief.flush()
                say(waiting_text(), full_only=unattended())
            elif args.push:
                publish(confirm)
            return 0
        except SurveyComplete as e:
            # Said in every log the first time; afterwards the driver's own line says it.
            say(f"{e}; nothing is left to backfill.", full_only=unattended() and not e.new)
            return 3  # the driver then waits for the morning listings
        except Incomplete as e:
            say(str(e), full_only=unattended() and e.limit)  # a usage limit: the driver says why, with the reset time
            if args.back:
                say(waiting_text(), full_only=unattended())
            return 0 if args.back > 1 else 2
        except Said:
            return 1  # the scorer's own "Error:" line said it
        except RunError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    finally:
        brief.flush()  # listings grouped for the brief log and not yet said


def waiting_text() -> str:
    n = len(pending())
    if quiet_weekend():
        return f"{n} commit(s) wait for Monday morning's push."
    return f"{n} commit(s) wait for the next mh -l or mh -p."


def backfill(once: bool, brief: Briefly) -> None:
    """Backfill listing after listing (once: one listing) until the usage limit stops it, the
    survey is complete, or the driver needs the seat."""
    first = True
    while True:
        date = back_date()
        if first:
            brief.heading(f"Backfilling from {date}")
            say(f"Columns: {', '.join(active_participants(legacy=True))}", full_only=True)
            first = False
        say(f"Backfilling {date}", full_only=True)
        try:
            summary = score(date)
            if summary is None:
                raise RunError(f"no table was written for {date}; the scorer's output above says why")
        except Incomplete:
            raise  # a usage limit: the next run finishes the day
        except RunError as e:
            if not record_failure(date, e):
                raise
            # A listing that keeps failing does not hold up the rest of the survey.
            set_aside_note(date, e)
            if once:
                break
            continue
        clear_failure(date)
        say(f"Committed locally: {commit_locally()}", full_only=True)
        brief.listing(date, summary)
        if once:
            break
        if unattended() and ((STATE / "yield").exists() or (STATE / "stop").exists()):  # the driver's flags
            say("Stopping the backfill: the driver needs the seat (morning listing, or a stop).", full_only=True)
            break


if __name__ == "__main__":
    sys.exit(main())
