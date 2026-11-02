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

How to run a MapHub is in `RUNNING.md`; what follows is what an assistant on the host must not miss.

- **Commands** (`mh`, which runs `scorer/local_run.py`): `-l` the current listing, pushed with any waiting backfills; `-b` one listing back (a missed weekday first), committed locally; `-bb` backfills until the usage limit stops it or the legacy survey is complete; `-p` pushes waiting backfills (`-y` without asking); `-d` the driver, with `--status`, `--hold` (keep a listing under review from being pushed) and `--stop` (RUNNING.md, Running unattended; which columns each run scores, and why: RUNNING.md, Local runs); `--via api` scores through the Claude API. Run `mh` where the maintainer's GitHub sign-in reaches git. Every run, the driver's and those started by hand, logs into `.cache/logs/mh-<date>.log`.
- **While the driver runs,** run no scoring command by hand (`mh -p`, `--hold`, `--status` and `--stop` are fine), and stop it before changing files on the host; code, portfolios and configuration pushed from another computer arrive on their own.
- **From Claude Code's `!` prompt,** `~/.bashrc` is not loaded (hence `mh` is a script), the confirmation question cannot be answered (use `mh -p -y`, or a terminal), and commands longer than 2 minutes move to the background.
- **Commits.** Table commits, including those made away from the host, follow RUNNING.md, One commit a day, with no statement of whose seat ran them. Code and documentation commits are drafted and shown to the maintainer first; a release is tagged once the maintainer agrees (in the prototype, the portfolios are tagged too, but only at their major updates, with maphub-public's latest version number), with a row in `DEVELOPMENT.md`, Versions, and the version in the README's "In depth" line.
- **Configuration changes:** date an entry as RUNNING.md, Configuration log says, and tell the maintainer which runs it affects.
- **Participant changes.** A folder in `maphub-portfolios/` with a `portfolio.md` but no entry in `participants.json` is left out with a warning: ask the maintainer whether the participant joins the legacy survey (`active`) or only new listings (`no-legacy`), never assume (statuses and `hidden`: `RUNNING.md`, Setting up a copy, step 3). Every join, status change and rename gets a dated line in the MapHub's log first, if it keeps one (in the prototype, `CHANGELOG.md` in the portfolios), then the entry in `participants.json` (`since`: the change's date) and the MapHub's other records follow (in the prototype, the "Who is who" table in the portfolios' README).
- **One host computer** runs `mh` and pushes tables, so that no two runs score the same listing and table commits stay in order; it alone works on the tables branch, and other computers and maintainers change code and documents on main, pulling first. If the host changes, move `.work/` with it or finish its days first, and remove `mh` from the old one; with several maintainers, agree who holds the host and who reviews portfolio changes.
- **Resuming.** A stopped run keeps every saved request in `.work/<date>/`, and the next run of any mode finishes that day first; delete `.work/` only once its tables are pushed.
- **Known arXiv and DataCite quirks.**
  - The arXiv API rate-limits busy clients (429, 503): the scorer waits up to about 5½ minutes, and a limit on the computer's address can last an hour or more, so wait before rerunning. Backfills do not need the API (DEVELOPMENT.md, Past listings), and the morning run falls back to the RSS feed's metadata; for a long way back, seed the cache from arXiv's metadata snapshot first (RUNNING.md, Setting up a copy, step 7).
  - The scorer handles the rest itself: papers the API's index lacks (from the RSS feed), DOIs DataCite lacks (`<date>.meta.json`), replies that skip papers, and holidays (empty tables); see DEVELOPMENT.md, Daily table, and RESULTS.md.
- **Viewer deploys.** A push to the tables branch that changes `viewer/`, `tables/`, `config-log.json` or the workflow deploys through "Publish viewer"; a push to main deploys nothing until the host's next run merges it. Script and stylesheet links carry the commit (`?v=…`), so a reload shows a new viewer at once.
