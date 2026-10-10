# maphub-public

MapHub, the MIDAS arXiv Preprint Hub: the scorer, the daily tables and the web viewer (see `README.md`). Kaili Cao (KC) runs it every day from a dedicated computer that holds only this repo, with the private portfolios repo `maphub-portfolios/` inside it. The design documents are in lsap-meth (*MIDAS arXiv Preprint Hub*), developed on KC's main computer.

This repo is public, and so is this file: nothing private goes here.

## Setting up a MapHub computer

KC clones this repo to `~/maphub-public` through VS Code (where he is signed in to GitHub), starts Claude Code in it from VS Code's terminal, and asks Claude to set up the computer. Claude Code and Anaconda are already installed. Claude then does the following, asking KC before installing anything:

1. **GitHub access.** VS Code passes KC's GitHub sign-in to git only in its own terminal. If a clone asks for credentials, stop and ask KC to start Claude Code from VS Code's terminal (or to sign in with `! gh auth login`).
2. **The portfolios.** Clone the private portfolios repo inside this one (`.gitignore` leaves it out):
   ```bash
   git clone -q https://github.com/kailicao/maphub-portfolios.git ~/maphub-public/maphub-portfolios
   ```
3. **Git identity.** If `git config --global user.name` or `user.email` is empty, ask KC for the values; never guess them.
4. **Python.** Install the scorer's packages (`anthropic`, `pyarrow`) for the Python that `python` runs, which should be Anaconda's: `python -m pip install -r ~/maphub-public/scorer/requirements.txt`.
5. **Claude Code.** Confirm with KC that the signed-in account is the Team seat meant for MapHub: the scorer runs `claude -p` on whichever seat is signed in.
6. **The `mh` command.** Create an executable `~/.local/bin/mh`, and check that `~/.local/bin` is on the search path both in a login shell and in Claude Code's `!` prompt:
   ```bash
   #!/usr/bin/env bash
   # MapHub local runs: mh l (latest), mh b (back one day), mh p (push pending backfills)
   exec python "$HOME/maphub-public/scorer/local_run.py" "$@"
   ```
7. **Report** what was done and what KC still has to do. No test runs are needed.

Only one computer runs MapHub: once this one does, `mh` is removed from any other.

## Running MapHub

- **Commands.** `mh l` scores the current listing and pushes it with any waiting backfills; `mh b` scores the weekday before the earliest table and commits locally; `mh p` pushes waiting backfills. Every active participant's column is scored through Claude Code. KC runs `mh l` each morning (any time after arXiv's 20:00 ET announcement) and `mh b` as his seat's usage allows, from VS Code's terminal, where his GitHub sign-in reaches git.
- **From Claude Code's `!` prompt.** `~/.bashrc` is not loaded there, which is why `mh` is a script and not a shell function. The confirmation question cannot be answered there: use `mh p --yes`, or run `mh p` in a terminal. Commands longer than 2 minutes move to the background.
- **Commits.** Table commits come only from `local_run.py`: one commit a day, folding waiting backfills with the latest listing, with the scorer's provenance message under each date; the script shows the message and asks before pushing. Code and documentation are changed on KC's main computer, and any commit there is drafted and shown to KC first.
- **Two computers, one repo.** The dedicated computer pushes tables; the main computer pushes code. `local_run.py` pulls before each run (and merges `tables/index.json` if needed); on the main computer, pull before editing (KC's sessions there start by pulling every repo).
- **Resuming.** A run that stops (usage limit, a failed request) keeps every saved request in `.work/<date>/`; rerunning the same command sends only the rest. Delete `.work/` only once its tables are pushed.
- **Papers held for moderation.** A rebuilt (past) listing places them by submission time, so a backfilled day can have a second file in the next ID month (`2610/2026-09-28.parquet`); a later RSS listing that announces them skips them as already scored.
- **Known arXiv and DataCite quirks.**
  - The arXiv API's index can lag behind the listing (2610.09954 on Oct 8, 2026); the scorer takes such papers from the RSS feed.
  - The arXiv API rate-limits busy clients (429, 503). The scorer waits up to about 5½ minutes; a limit on the computer's address can last an hour or more (Oct 8–9, 2026), so wait before rerunning.
  - DataCite registers each listing's DOIs some time after the 20:00 ET announcement (about 30 minutes for Oct 7, 2026), and occasionally never (2610.07148). The scorer then writes `<date>.meta.json` beside the table; the viewer reads it only when DataCite has no record.
  - Claude occasionally skips a paper in a reply; the scorer keeps the scores it got and asks again for the rest.
- **Viewer deploys.** Each push that changes `viewer/` or `tables/` deploys through "Publish viewer". Script and stylesheet links carry the commit (`?v=…`), so a reload shows a new viewer at once.
