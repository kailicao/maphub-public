# MapHub: operations notes for an AI assistant

MapHub, the MIDAS arXiv Preprint Hub: the scorer, the daily tables and the web viewer (see `README.md`; running a MapHub is in `RUNNING.md`, the tables and viewer in `RESULTS.md`, how it works in `DEVELOPMENT.md`). This file tells an AI coding assistant how a MapHub is set up and run day to day; people can read it as a runbook too. Claude Code loads it automatically, since a file named `CLAUDE.md` is its convention. With another assistant (one that reads `AGENTS.md`, say), point it to this file. The scoring itself is separate from the assistant: by default it calls Claude Code (`claude -p`), and with `--via api` the Claude API.

The steps below hold for any MapHub; the values in them are the prototype's, this repository's, as examples: its maintainer is Kaili Cao, its main repository is `maphub-public` and its portfolios repository `maphub-portfolios`. A MapHub that copies this file replaces those values with its own (RUNNING.md, Setting up a copy).

- **The maintainer** runs the MapHub: the daily runs, the portfolios and the participants. Ask the maintainer whenever a step below says so, and before anything that cannot be undone.
- **The host computer** is the one computer that runs the scoring and pushes the tables. It holds the main repository, checked out on its tables branch, with the portfolios inside. Code and documents can be developed elsewhere (in the prototype, on the maintainer's main computer).
- **The seat** is the Claude subscription account that Claude Code is signed in to on the host computer, which pays for the scoring.

The main repository is public, and so is this file: nothing private goes here.

## Setting up the host computer

The maintainer clones the main repository to their home folder (in the prototype, `~/maphub-public`, through VS Code, where the maintainer is signed in to GitHub), starts Claude Code in it (in the prototype, from VS Code's terminal), and asks Claude to set up the computer. Claude Code and Python are installed (in the prototype, Anaconda's). Claude then does the following, asking the maintainer before installing anything:

1. **GitHub access.** Clones and pushes need the maintainer's GitHub sign-in. VS Code, for one, passes it to git only in its own terminal; if a clone asks for credentials, stop and ask the maintainer to start Claude Code where they are signed in, or to sign in with `! gh auth login`.
2. **The tables branch.** The host computer works on the tables branch (RUNNING.md, Repositories): `git switch tables`, or, in a new copy that has none yet, `git switch -c tables` and `git push -u origin tables`.
3. **The portfolios.** They live in `maphub-portfolios/` inside the main repository, in one of two ways (RUNNING.md, Setting up a copy, step 3):
   - **A private repository of their own** (the prototype's way): clone it inside the main one, where `.gitignore` leaves it out. In the prototype:
     ```bash
     git clone -q https://github.com/kailicao-lsap/maphub-portfolios.git ~/maphub-public/maphub-portfolios
     ```
   - **A folder of the main repository:** nothing to clone. The portfolios are then as public as the main repository; check with the maintainer that the participants agreed to that.

   If neither exists yet, the maintainer creates it first, from `portfolios-template/`: a new private repository, or the folder in the main repository with the `maphub-portfolios/` line removed from `.gitignore`.
4. **Git identity.** If `git config --global user.name` or `user.email` is empty, ask the maintainer for the values; never guess them.
5. **Python.** Install the scorer's packages (`anthropic`, `pyarrow`) for the Python that `python` runs: `python -m pip install -r scorer/requirements.txt`.
6. **Claude Code.** Confirm with the maintainer that the signed-in account is the seat meant for the MapHub: the scorer runs `claude -p` on whichever seat is signed in.
7. **The `mh` command.** Create an executable `~/.local/bin/mh` that runs `scorer/local_run.py` in the main repository, and check that `~/.local/bin` is on the search path both in a login shell and in Claude Code's `!` prompt. In the prototype:
   ```bash
   #!/usr/bin/env bash
   # MapHub local runs: mh -l (latest), mh -b (back one day), mh -bb (back until the usage limit), mh -p (push), mh -d (the driver)
   exec python "$HOME/maphub-public/scorer/local_run.py" "$@"
   ```
8. **Report** what was done and what the maintainer still has to do. No test runs are needed.

## Running MapHub

- **Commands.** `mh -l` (`--latest`) scores the current listing and pushes it with any waiting backfills; `mh -b` (`--back`) scores one listing and commits locally: first any weekday the morning runs missed, otherwise the weekday before the earliest table; `mh -bb` (`--back-all`) keeps backfilling until the usage limit stops it, or until the legacy survey is complete (RESULTS.md, Where the survey ends); `mh -p` (`--push`) pushes waiting backfills; `-y` (`--yes`) pushes without asking; `mh -d` (`--drive`) runs all of this unattended (RUNNING.md, Running unattended), with `mh --status`, `mh --hold` (keep the listing under review from being pushed) and `mh --stop` (stop once the listing being scored is done, or at once while sleeping or waiting for review). `--via api` would score through the Claude API instead of Claude Code. Run `mh` from a terminal where the maintainer's GitHub sign-in reaches git. Which columns each run scores, and why: RUNNING.md, Local runs.
- **The driver.** On a host computer that never sleeps, `mh -d` runs the routine unattended; how to start, watch, review, stop and update it is in RUNNING.md, Running unattended. While it runs, run no `mh` scoring command by hand (`mh -p`, `mh --hold`, `mh --status` and `mh --stop` are fine), and stop it before changing files on the host; code, portfolios and configuration pushed from another computer are picked up on their own (RUNNING.md, Running unattended, step 5).
- **From Claude Code's `!` prompt.** `~/.bashrc` is not loaded there, which is why `mh` is a script and not a shell function. The confirmation question cannot be answered there: use `mh -p -y`, or run `mh -p` in a terminal. Commands longer than 2 minutes move to the background.
- **Commits.** Table commits come only from `local_run.py`: one commit a day, folding waiting backfills with the latest listing, with the message `local_run.py` writes (RUNNING.md, One commit a day) and no statement of whose seat ran it, since the main repository is public; a fold made by hand follows the same form, and the script shows the message and asks before pushing. Commits go in the order things happened: tables scored before a code change are committed before it, with the message the code of their time writes. On the host, a backfill waiting when new code is merged from main is replayed on top of the merge; its `Code:` line still names the code that scored it, and that line is the record. Code and documentation commits are drafted and shown to the maintainer first. A release is tagged in git (`v0.9`, …) once the maintainer agrees, gets a row in `DEVELOPMENT.md`, Versions, and updates the version named in the README's "In depth" line.
- **Configuration changes.** An entry in `config-log.json` takes effect by the date a run starts (Eastern time). Date a new entry for the day it is committed unless the maintainer wants a later start, and tell the maintainer which runs it affects: an entry dated the next day leaves that evening's backfills on the old configuration.
- **New participants.** When a folder in `maphub-portfolios/` has a `portfolio.md` but no entry in `participants.json` (the scorer then warns and leaves it out), ask the maintainer whether the participant joins the legacy survey of past listings (status `active`) or only new listings (status `no-legacy`); never assume. Then add the entry to `participants.json`, and keep any records the MapHub keeps beside it in step (in the prototype, a line in `CHANGELOG.md` and the "Who is who" table in the portfolios repository's README).
- **Computers and maintainers.** Run a MapHub from one host computer: only it runs `mh` and pushes tables, so that no two runs score the same listing and table commits stay in order. Other computers, and other maintainers, change code and documents on main, and pull before editing; the host alone works on the tables branch. If the host computer changes, move `.work/` along with it or finish its days first, and remove `mh` from the old one. When several people maintain a MapHub, agree on who holds the host computer and who reviews portfolio changes.
- **Resuming.** A run that stops (usage limit, a failed request) keeps every saved request in `.work/<date>/`; the next run of any mode finishes that day first, sending only the rest. Delete `.work/` only once its tables are pushed.
- **Known arXiv and DataCite quirks.**
  - The arXiv API's index can lag behind the listing (2610.09954 on Oct 8, 2026); the scorer takes such papers from the RSS feed, and every paper when the API refuses altogether.
  - The arXiv API rate-limits busy clients (429, 503). The scorer waits up to about 5½ minutes; a limit on the computer's address can last an hour or more (Oct 8–9 and Oct 16, 2026), so wait before rerunning. Backfills do not need the API (DEVELOPMENT.md, Past listings), and the morning run falls back to the RSS feed's metadata. For a long way back, seed the cache from arXiv's metadata snapshot first (RUNNING.md, Setting up a copy, step 7).
  - DataCite registers each listing's DOIs some time after the 20:00 ET announcement (about 30 minutes for Oct 7, 2026), and occasionally never (2610.07148); the scorer then writes `<date>.meta.json` beside the table (RESULTS.md, Web viewer).
  - Claude occasionally skips a paper in a reply; the scorer keeps the scores it got and asks again for the rest.
  - arXiv holidays: the morning run writes an empty table, and a gap the morning run missed is filled first by `mh -b`, also with an empty table; backfills give older holidays the submissions they would have held (RESULTS.md, Holidays).
- **Viewer deploys.** Each push to the tables branch that changes `viewer/`, `tables/` or `config-log.json`, or the workflow itself (`.github/workflows/pages.yml`), deploys through "Publish viewer"; a push to main deploys nothing until the host's next run merges it. Script and stylesheet links carry the commit (`?v=…`), so a reload shows a new viewer at once.
