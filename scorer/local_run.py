#!/usr/bin/env python3
"""MapHub's local runs through Claude Code: the latest listing, and backfills one day at a time.

    python scorer/local_run.py latest   # (or l) score the current listing; commit it with
                                        # any pending backfills as one commit, and push
    python scorer/local_run.py back     # (or b) score one listing further back; commit locally
    python scorer/local_run.py push     # (or p) push pending backfills without a new listing

Every table gets all active participants' columns, scored through Claude Code
(daily_table.py --via claude-code). One commit per day: a backfill is committed
locally and waits, and the next `latest` (or `push`) folds the waiting commits
and the new table into one commit, keeping each table's provenance from the
scorer. The push starts "Publish viewer". Run `back` as often as usage allows;
each run scores a single day, so usage can be checked between runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
SUBJECT = re.compile(r"^Table for (\d{4}-\d{2}-\d{2}) \(via ([\w-]+)\)$")


class RunError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            env={**os.environ, **(env or {})})
    if check and result.returncode != 0:
        raise RunError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def rebuild_index() -> None:
    """tables/index.json from the Parquet files present: settles a conflict between
    a pending backfill and a table pushed meanwhile from another computer."""
    (TABLES / "index.json").write_text("{}\n", encoding="utf-8")
    dates: dict[dt.date, list[str]] = {}
    for f in TABLES.glob("*/*/*.parquet"):
        dates.setdefault(dt.date.fromisoformat(f.stem), []).append(f.parent.name)
    for date, yymms in sorted(dates.items()):
        dtab.write_index(TABLES, date, sorted(yymms))


def sync() -> None:
    """Bring both repos up to date; pending backfill commits are replayed on top."""
    git(PORTFOLIOS, "pull", "-q", "--ff-only")
    if git(ROOT, "status", "--porcelain", "--untracked-files=no", "--", "tables"):
        raise RunError("tables/ has uncommitted changes; commit or discard them first")
    pulled = subprocess.run(["git", "-C", str(ROOT), "pull", "-q", "--rebase", "--autostash"], capture_output=True, text=True)
    rebasing = lambda: (ROOT / ".git" / "rebase-merge").exists() or (ROOT / ".git" / "rebase-apply").exists()
    if pulled.returncode != 0 and not rebasing():
        raise RunError(f"git pull: {pulled.stderr.strip()}")
    while rebasing():
        conflicts = git(ROOT, "diff", "--name-only", "--diff-filter=U").split()
        if conflicts != ["tables/index.json"]:
            git(ROOT, "rebase", "--abort", check=False)
            raise RunError(f"pull conflicts in {', '.join(conflicts) or 'unknown files'}; resolve by hand")
        rebuild_index()
        git(ROOT, "add", "tables/index.json")
        git(ROOT, "rebase", "--continue", env={"GIT_EDITOR": "true"}, check=False)


def active_participants() -> list[str]:
    """Every active participant with a portfolio: the columns each table gets."""
    return sorted(dtab.load_participants(PORTFOLIOS))


def score(date: dt.date | None) -> bool:
    """Run the scorer through Claude Code for all active columns; True if a table was written."""
    message = WORK / "commit-message.txt"
    message.unlink(missing_ok=True)
    command = [sys.executable, str(ROOT / "scorer" / "daily_table.py"), "--via", "claude-code"]
    if date is not None:
        command += [date.isoformat(), "--backfill"]
    env = {**os.environ, "MAPHUB_OWN_PARTICIPANTS": ",".join(active_participants())}
    result = subprocess.run(command, env=env)
    if result.returncode != 0:
        raise RunError(f"the scorer stopped (exit {result.returncode}); rerun to resume")
    return message.exists()


def commit_locally() -> str:
    """Commit the table just written with the scorer's provenance message."""
    git(ROOT, "add", "tables")
    git(ROOT, "commit", "-q", "-F", str(WORK / "commit-message.txt"))
    return git(ROOT, "log", "-1", "--format=%s")


def pending() -> list[str]:
    """Messages of the table commits not yet pushed, oldest first."""
    log = git(ROOT, "log", "--reverse", "--format=%B%x00", "@{upstream}..HEAD")
    return [m.strip() for m in log.split("\x00") if m.strip()]


def combined(messages: list[str]) -> str:
    """One message for several tables: a subject naming them, then each table's
    provenance under its date. A single table keeps the scorer's message."""
    if len(messages) == 1:
        return messages[0] + "\n"
    heads = [SUBJECT.match(m.splitlines()[0]) for m in messages]
    if not all(heads):
        raise RunError("an unpushed commit is not a table commit; push or fold it by hand")
    dates = [h.group(1) for h in heads]
    routes = sorted({h.group(2) for h in heads})
    lines = [f"Tables for {', '.join(dates)} (via {', '.join(routes)})"]
    for date, message in zip(dates, messages):
        body = [l for l in message.splitlines()[1:] if l.strip()]
        lines += ["", date, *body]
    return "\n".join(lines) + "\n"


def publish(confirm: bool) -> None:
    """Fold the unpushed table commits into one and push it."""
    messages = pending()
    if not messages:
        print("Nothing to push.")
        return
    message = combined(messages)
    print("--- commit message ---\n" + message + "----------------------")
    if confirm and not sys.stdin.isatty():
        # No terminal to answer in (e.g. a command run from a chat): leave the commits waiting.
        print("Not pushed: no terminal to confirm in. Run `mh p` in a terminal, or `mh p --yes`.")
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


def back_date() -> dt.date:
    """The weekday before the earliest table: the next listing back."""
    dates = sorted(dt.date.fromisoformat(d) for d in dtab.index_dates(TABLES))
    if not dates:
        raise RunError("no tables yet; run `latest` first")
    day = dates[0] - dt.timedelta(days=1)
    while day.weekday() >= 5:  # no listings on weekends
        day -= dt.timedelta(days=1)
    return day


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=["latest", "back", "push", "l", "b", "p"],
                    help="latest (l), back (b) or push (p)")
    ap.add_argument("--yes", action="store_true", help="push without asking to confirm the message")
    args = ap.parse_args()
    args.mode = {"l": "latest", "b": "back", "p": "push"}.get(args.mode, args.mode)
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with the scorer's
    dtab.load_env(ROOT / ".env")
    try:
        sync()
        if args.mode == "back":
            date = back_date()
            print(f"Backfilling {date}, all active columns: {', '.join(active_participants())}")
            if score(date):
                print(f"Committed locally: {commit_locally()}")
                print(f"{len(pending())} table commit(s) wait for the next `latest` or `push`.")
            return 0
        if args.mode == "latest" and score(None):
            print(f"Committed locally: {commit_locally()}")
        publish(confirm=not args.yes)
        return 0
    except RunError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
