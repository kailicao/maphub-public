# Running a MapHub

How a MapHub is set up, run and kept, for its maintainers: the repositories, setting up a copy step by step, participants and portfolios, the runs and their configuration, running unattended, and the stages. The step-by-step runbook for an AI assistant on the host computer is [`CLAUDE.md`](CLAUDE.md); this file gives the reasons behind it. [RESULTS.md](RESULTS.md) says what a MapHub produces, and [DEVELOPMENT.md](DEVELOPMENT.md) how the scoring works and why. The method does not depend on the field; the examples and dates here come from the prototype (this repository: astro-ph and cs.AI).

## Repositories

The main repository is public. Its main branch holds `scorer/` (the daily table script), `viewer/` (the web viewer), `config-log.json` and the GitHub workflow that publishes the viewer; its tables branch holds the same plus `tables/`, and is the one published. Code and documents are developed on main, which stays the code alone, for copies and code releases; the host computer works on the tables branch, merging main into it before each run ([Runs, Pulls and merges](#pulls-and-merges)), and its pushes publish the viewer and tables. A snapshot of the tables for a paper is a release of the tables branch.

The main repository ignores `.work/`, where runs save papers and scores for resuming, `.cache/` (the arXiv OAI-PMH records for rebuilding past listings, the driver's logs, and scoring trials kept by hand in `.cache/trials/`; [DEVELOPMENT.md, Past listings](DEVELOPMENT.md#past-listings)), `.env`, for local settings such as an API key for the `api` route, and, on main, `tables/` (the tables branch adds them with `git add -f`). The scorer needs Python with `anthropic` and `pyarrow`.

The portfolios sit in `maphub-portfolios/` inside it, in one of two ways ([Setting up a copy, step 3](#setting-up-a-copy), below):

```text
maphub-public/        public: code, configuration log and viewer (main); the
                      same with the daily tables (tables)
  maphub-portfolios/  private, shared with all participants, who have read
                      access
```

- **A repository of their own** (the default, and the prototype's): private, cloned inside the main one and left out by `.gitignore`, so participants can read the portfolios while everything else is public.
- **A folder of the main repository:** simpler, with one repository to keep, but the portfolios are then exactly as visible as the main repository, public if it is public, as GitHub Pages on a free plan requires.

The scorer handles both: it checks that the portfolios are committed and pushed (only that folder, in the second case) and names their commit in each table's provenance. `portfolios-template/` holds a minimal start for either: `participants.json` and one portfolio in the format.

## Setting up a copy

The README ([Run your own MapHub](README.md#run-your-own-maphub)) gives these steps in short; here they are in full.

1. **Make your copy.** To start with a clean history, use GitHub's "Use this template", leaving "Include all branches" unchecked (the prototype is a template repository; to make your copy one too, tick 'Template repository' in Settings > General), or a release's code: this repository's main branch holds the code alone, and the prototype's tables are on its tables branch ([Repositories](#repositories), above). A fork carries the prototype's history, and GitHub leaves a fork's workflows off until you enable them on its Actions tab. Then, in a clone of your copy, create the branch your tables will go on, `git switch -c tables` and `git push -u origin tables` (it starts as main, and the first run adds the tables), and note which version you started from ([DEVELOPMENT.md, Versions](DEVELOPMENT.md#versions)).
2. **Choose the listings** in `config-log.json`. Keep one entry and edit it:
   - `listings`: the listings as arXiv's RSS feeds name them, such as `["hep-ph", "hep-th"]`.
   - `categories`: the arXiv API categories that past listings are rebuilt from when OAI-PMH fails (the fallback), one query each. Leave it out when every listing is a single category; an archive split into subcategories, like astro-ph or cond-mat, needs each one listed (see the prototype's entry).
   - `effective`: on or before the day of your first run, even a test; entries take effect by the run date.
   - `model`, `effort`, `prompt` and `threshold`: the Claude model and effort level, the prompt version (in `scorer/daily_table.py`: `v2`, two passes with a cacheable prefix, the prototype's since Oct 19, 2026; `v1`, the same wording in an older order; `v0`, one pass) and, for two passes, the outline score from which a paper is scored again against the full portfolio (10 in the prototype, measured on astro-ph and cs.AI: [DEVELOPMENT.md, Two-pass test](DEVELOPMENT.md#two-passes-and-usage); worth checking on your own listings, for example with `--test`). `fallbacks` applies only to the API route. Replace `note`.
3. **Set up the portfolios** as `maphub-portfolios/` inside your copy, starting from `portfolios-template/`: one folder per participant, named by a pseudonym and holding `portfolio.md` ([README, Writing a portfolio](README.md#writing-a-portfolio)), and `participants.json`:
   ```json
   {"participants": {
     "1234": {"kind": "individual", "status": "active", "hidden": false, "since": "2027-01-04"},
     "5678": {"kind": "group", "status": "no-legacy", "hidden": false, "since": "2027-01-04"}}}
   ```
   - **Where they live.** In a private repository of their own, cloned inside your copy, or in a folder of your copy (remove the `maphub-portfolios/` line from `.gitignore`), where they are exactly as public as your copy ([Repositories](#repositories), above). Choose the folder only if your participants agree to public portfolios.
   - `status`: `active` is scored on each new listing and on backfills of past listings; `no-legacy`, new listings only; `paused` or `left`, not scored. `hidden: true` hides the column in the viewer without stopping its scoring. `kind` (individual, group or collaboration) and `since` (the date of the entry's latest change) are for your records, as is a dated log of changes beside the file, if you keep one (the prototype keeps `CHANGELOG.md`).
   - Pseudonyms are letters, digits, `_` or `-`, up to 40 characters; the prototype uses random numbers. Keep who stands behind each one where only participants can read it.
   - Commit and push every portfolio change before a run: the scorer refuses portfolios with uncommitted or unpushed changes, and the daily routine pulls them first. Portfolios kept as a folder of your copy are changed on main, like the code, and reach the host's tables branch with its next merge.
   - How you keep the portfolios, and how participants propose changes, is up to you; what the prototype does ([Participants and portfolios](#participants-and-portfolios), below) is a suggestion from its experience.
4. **Make the page yours:**
   - `viewer/index.html`: the title and description (lines 6–7), the full name in the header, the About panel (credits, contact, the GitHub link) and the footer's contact link; please keep the footer's "Built with MapHub" link, which notes the origin;
   - `viewer/viewer.css`: the colors at the top (the prototype's are Michigan Blue and Maize); `viewer/viewer.js`: the heatmap's color ramps, near the top;
   - the icons `viewer/favicon.svg`, `favicon-32.png` and `apple-touch-icon.png`;
   - the README, and `CLAUDE.md`, the operations file for an AI assistant, written so that it serves any MapHub, with the prototype's values as examples; replace those with yours;
   - `MAPHUB_USER_AGENT` in `.env`: how your MapHub names itself to arXiv and DataCite, with a contact, such as `MapHub/0.1 (+https://github.com/<you>/<repo>; you@example.edu)`.
5. **Publish:** in your repository's Settings > Pages, set the source to GitHub Actions, and in Settings > Environments > github-pages, let the tables branch deploy (Deployment branches; if the environment is not listed yet, GitHub creates it with the first deploy, which it refuses until the branch is added); each push of tables then deploys the viewer at your Pages address.
6. **Run it** each morning after arXiv's 20:00 ET announcement, with `python scorer/local_run.py -l` ([Local runs](#local-runs), below), on the tables branch of one host computer (on main, the script stops and says so).

   It needs either Claude Code (`claude` on the search path, recent enough for `--json-schema`), signed in to a Claude subscription whose terms allow this use (check them), whose usage limits then pace the runs; or the Claude API, with `ANTHROPIC_API_KEY=...` in `.env` and `--via api`. For scale, see [Stages, Cost](#stages) (below). `CLAUDE.md` sets up the host computer, with a short `mh` command for these runs. Once the routine works, the driver can run it unattended ([Running unattended](#running-unattended), below).

   The driver's schedule is yours to choose: four constants at the top of `scorer/driver.py`, `MORNING`, `REVIEW`, `WAKE` and `RETRY` (`SESSION` follows Claude Code's limits) ([Running unattended](#running-unattended), below).
7. **Optionally, go back in time:** `local_run.py -b` scores one earlier listing for participants with status `active`, and `-bb` keeps going until the usage limit stops it, back as far as you choose, or until the surveyed categories begin, where it stops by itself. For a long way back, seed the cache of arXiv's records from its metadata snapshot first, so that backfills need not harvest them: download the "arXiv Dataset" from Kaggle (`Cornell-University/arxiv`, a ZIP of about 1.9 GB) on any computer with the scorer's packages, run `python scorer/arxiv_snapshot.py convert <the ZIP> <folder>` there (and, to compare it with the tables so far, `check <folder> <a checkout of the tables branch>/tables`), and carry the folder (about 1 GB for astro-ph and cs.AI) to the host computer, into `.cache/arxiv-snapshot/`, with the driver stopped; the next backfill merges it into the cache, once (or run `python scorer/arxiv_snapshot.py merge <folder>` yourself). `arxiv_snapshot.py missing <folder> <tables>` lists the papers that earlier backfills missed, and `daily_table.py --late <that list>` scores them in one run and adds each to its listing's table ([DEVELOPMENT.md, Past listings](DEVELOPMENT.md#past-listings)).

## Participants and portfolios

Participants are individuals and groups or collaborations, each shown by a pseudonym. Each participant's portfolio is the only input that scores papers for them, in the format of the [README, Writing a portfolio](README.md#writing-a-portfolio). The relevance score doubles as a proxy for answering capability: the participants a paper is most relevant to are taken as the ones best able to answer questions about it. They are not necessarily the best, but relevance is the only measure available, and the proxy improves as participants revise their portfolios; how well the scores match participants' own judgments is tested by a blind rating study ([DEVELOPMENT.md, Measurements, Rating study](DEVELOPMENT.md#stability-and-quality)) and described in a research note in preparation.

- **First portfolios.** [README, The portfolio-drafting prompt](README.md#the-portfolio-drafting-prompt). A group's or collaboration's draft may be discussed with them before or after it goes into use.
- **Outline.** The scorer cuts each portfolio's outline at run time and never stores it ([README, Writing a portfolio](README.md#writing-a-portfolio)).
- **Names and instructions.** There is no separate author bonus. Participants who want one add names of people they know, and they may add scoring instructions; both count toward the portfolio's length. Since each full-pass request scores one participant, they affect only that participant's column; they stay out of the outline, so the outline pass, which serves everyone at once, never sees them. Instructions that inflate someone's scores would also make them look like a better person to ask, so the maintainer's review of portfolio changes watches for them.

How a MapHub keeps its portfolios is up to its maintainer; the prototype's practices below are suggestions from its experience.

- **Safeguards.** Each participant has a folder in the portfolios repository holding their portfolio. In the prototype, participants have read access to that repository only: they cannot push, and propose a change by forking the repository and sending a pull request, which the maintainer reviews (own folder only, length) and merges; emailing a new version works too.
- **Privacy.** Every participant can read every portfolio, since GitHub grants read access per repository, not per folder or branch. A participant who needs theirs private uses the own-repo option ([DEVELOPMENT.md, Extension directions](DEVELOPMENT.md#extension-directions)). Portfolios and the papers' abstracts are also sent to Anthropic, the provider of the language model, with every scoring request, under the terms of the account used ([README, What participants agree to](README.md#what-participants-agree-to)).
- **Participant changes.** The portfolios hold `participants.json`, each participant's current status and whether their column is hidden ([Setting up a copy, step 3](#setting-up-a-copy)); the prototype also keeps `CHANGELOG.md`, a dated log of joins, status changes and renames, in step with it. A portfolio without an entry is skipped with a warning. Data files are never changed when someone pauses or leaves: their past columns stay in the public tables, which participants are told when they join. Instead, the scorer publishes the active and hidden lists in `tables/participants.json`, and the viewer leaves out hidden columns (also from the similarity sorting) and grays out visible columns of participants who are no longer active.

## Runs

### Local runs

Scoring needs the portfolios in `maphub-portfolios/` ([Setting up a copy, step 3](#setting-up-a-copy)). The maintainer runs MapHub from one host computer, set up as `CLAUDE.md` describes. The daily routine runs through Claude Code on whichever seat `claude` is signed in to (it must be installed and signed in), with the column of every participant scored daily (status active or no-legacy; backfills: active):

```bash
python scorer/local_run.py -l    # --latest, in the morning: score the current listing, push it with any waiting backfills
python scorer/local_run.py -b    # --back: fill a missed day, else one listing further back; commit locally
python scorer/local_run.py -bb   # --back-all: keep backfilling until the usage limit stops it
python scorer/local_run.py -p    # --push: push waiting backfills when there is no new listing
# add -y (--yes) to push without confirming the commit message
```

Pushing a table starts "Publish viewer", which deploys the viewer and tables on GitHub Pages.

arXiv announces each listing at 20:00 ET, and a run any time after that scores it; `CLAUDE.md` names the commands above `mh`. A backfill takes first any gap, a weekday between the earliest and latest tables without a table, otherwise the weekday before the earliest table; `-bb` also spends the seat's weekly limit, so the maintainer chooses when to run it.

Every mode first finishes a day left unfinished in `.work/`, so a day stopped by the usage limit needs no manual step; a backfilled listing with nothing left to score gets an empty table, so that every weekday has one. In the prototype, backfills score every participant, all with status active, so the recent tables are complete.

### One commit a day

To keep one commit a day, a backfill is committed locally and waits: the next `--latest` (or `--push`, when there is no new listing) folds the waiting commits and the new table into one commit, whose message gives the provenance once for each group of tables that share code, portfolios, configuration and columns, under their dates and newest first, with the papers and full-pass counts summed and a line only one table has kept with its date; its subject names the dates as ranges of consecutive weekdays (`Tables for Oct 15, Sep 1–7, 2026`).

### Pulls and merges

If a table was pushed meanwhile from another computer, the pull rebuilds `index.json` (and the top-paper summaries) from the tables present. On the tables branch, each run then merges any new commits of main into the branch as last pushed, pushes the merge ("Merge main into tables: code pickup"), and replays the waiting backfills on top of it; main never touches `tables/`, and the tables branch changes nothing else, so the merge has no conflicts. Each table's provenance names the code that scored it (`Code: maphub-public <commit>`, the last commit that changed `scorer/`), so a waiting backfill replayed on top of newly pulled code still says which code it used.

### Configuration log and provenance

**Configuration log.** `config-log.json` records each change with the date it takes effect: model, effort, prompt version, input (abstract only or full text), listing (new submissions only or extended), the arXiv listings followed (`listings`, as named in arXiv's RSS feeds, such as astro-ph or cs.AI) and the categories past listings query in the arXiv API (`categories`: an archive split into subcategories, like astro-ph, needs each subcategory listed), chunk size, the two-pass threshold and similar settings. Entries apply by the date a run starts (Eastern time), not the listing it covers: an entry dated the next day leaves that evening's runs on the old configuration, so date an entry for the day it is committed unless a later start is meant. Changes should be rare. The prototype's first entry (Oct 5, 2026) is Claude Opus 5.5 at low effort with prompt v0. Trial runs of other models (`--model` and `--effort`, allowed only with `--test`) leave the log unchanged and write to their own folders.

**Provenance.** The tables carry no provenance themselves: each commit that adds or changes a table names, in its message, the portfolios commit, the configuration entry, the columns scored and the papers left out as already scored, so `git log -- <table>` gives the table's history even when a file holds columns from several runs or is corrected later. The scorer writes this message to `.work/commit-message.txt`, and `local_run.py` commits with it. A rescored table replaces the old one, and git keeps the earlier version.

### Running the scorer directly

The scorer can also be run directly, for example for a test; through Claude Code it scores only the columns named in `MAPHUB_OWN_PARTICIPANTS` (comma-separated pseudonyms; `local_run.py` sets it to every participant scored):

```bash
pip install -r scorer/requirements.txt
MAPHUB_OWN_PARTICIPANTS=<pseudonym> python scorer/daily_table.py --test   # 5 papers of each listing
python viewer/serve.py                   # viewer at http://localhost:8000/ (on main, add --tables <a checkout of the tables branch>/tables)
```

**Kinds of run.** Without a date, the scorer takes the current listing and stops at once if it is already in the tables, so repeated runs after a late announcement cost nothing. A past date is the legacy survey ([RESULTS.md, Back catalogs](RESULTS.md#back-catalogs)), scoring participants with status active; `daily_table.py DATE --daily` scores it as a daily run instead (no-legacy included), which `local_run.py` uses to finish a morning listing interrupted by the usage limit. `daily_table.py DATE --meta-only` checks an existing table against DataCite and writes its metadata fallback, without scoring. `--test` scores 5 papers of each listing into the work dir.

## Running unattended

The driver (`mh -d`, `scorer/driver.py`) runs the routine above on the host computer: `mh -l` each weekday morning, `mh -bb` the rest of the time. Once the legacy survey is complete, only the morning runs are left (and any gaps to fill). Its times are constants at the top of `scorer/driver.py`: the morning run from 02:00 ET, so that the listing is in the viewer by about 07:00 even after a usage-limit wait; no time for review (`REVIEW`, 0 in the prototype; a MapHub may give its maintainer an hour, say); a retry every 30 minutes after a failure; after a 5-hour limit, a wake at the reset (after switching seats, the maintainer stops and restarts the driver); and no other sleep longer than 2½ hours (`WAKE`), such as during a weekly limit (each try then stops at once) or when Claude Code gives no reset time.

1. **Start it** in the main repository's folder, on the tables branch, in the background:
   ```bash
   nohup mh -d >/dev/null 2>&1 &
   mh --status
   ```
   `mh --status` shows whether it runs, what it is doing and until when, the commits waiting to be pushed, and the last lines of its log. Only one driver runs at a time; a second `mh -d` says so and exits. Under WSL, keep the distribution running (a terminal left open, for one): when WSL shuts it down, the driver stops with it.
2. **Each morning.** From 02:00 ET the driver runs `mh -l` until the day's listing has a table, then pushes it with any backfills waiting, and "Publish viewer" updates the page. With a time for review (`REVIEW` above 0), the driver waits that long before pushing: to look at the commits first, run `mh -p` in a terminal, which shows the commit message (one group of dates per provenance: [One commit a day](#one-commit-a-day), above) and asks, `y` pushing now; to keep them waiting, run `mh --hold`, then `mh -p` whenever ready; backfills pause meanwhile. On Saturdays and Sundays (Eastern time) the driver pushes nothing: backfills, and a listing finished late, wait as local commits, and new code from main is merged on Monday morning's run; `mh -p` run by hand still pushes.
3. **Usage limits.** When the seat's 5-hour limit stops a run, the day's saved requests stay in `.work/`, and the driver sleeps until a few minutes after the reset time Claude Code reports (after switching to another seat, stop the driver and start it again); the next run finishes the day. While the weekly limit lasts, the driver tries again every 2½ hours and nothing is scored, not even the morning listing; once it resets, the first backfill fills the listings missed meanwhile, rebuilt by submission time like any past listing ([DEVELOPMENT.md, Past listings](DEVELOPMENT.md#past-listings)).
4. **Stop and restart.** `mh --stop` stops the driver once it finishes the listing it is scoring, or at once if it is sleeping or waiting for review (commits it has not pushed wait for `mh -p`, and a day left in `.work/` is finished by the next run); `mh --status` then says "not running". A driver killed or shut down partway (a reboot, WSL closing) loses nothing either: start it again, and the first run finishes the day left in `.work/`.
5. **Updating code, portfolios or the configuration.** Push to main from another computer: every run pulls and merges main first, so pushes reach the host on their own, at the latest when the next run starts (after a usage-limit sleep, a few hours; on weekends, at Monday morning's run, step 2); when the scorer's code (`scorer/`) has changed, the run that pulled it restarts with the new code before it scores anything, and the driver restarts itself between runs, so no listing is cut short. To edit on the host itself, stop the driver, wait until `mh --status` says it is not running, edit and commit, then start it again.
6. **When something goes wrong,** read the log, `.cache/driver/driver-<date>.log` (one file a day, all kept: about 4 MB a year of daily runs, plus about 2 KB a backfilled listing; `mh --status` shows the latest lines). Each run of `mh` appears as `$ mh …` with its output and exit code, followed by what the driver does next:
   - "usage limit reached …; sleeping until …": normal; it tries again a few minutes after the reset (step 3).
   - "… not finished (exit N); retrying later": a run failed for another reason (arXiv busy, a network error, a git conflict); it retries every 30 minutes. The output above it gives the cause; "GET failed … server is busy" lines are arXiv's rate limit, which the morning run outlasts by taking its metadata from the RSS feed.
   - A failed push (`$ mh -p -y` with a nonzero exit) leaves the commits waiting; `mh --status` counts them, and the next morning's push takes them along, or push by hand with `mh -p`.
   - "OAI-PMH failed …; falling back to the arXiv API": a backfill rebuilds its listing through the API instead; the harvested pieces stay in the cache, and OAI-PMH rests for two hours ("OAI-PMH rests after a failure until …"), then carries on from where it stopped.

## Stages

The prototype's stages; a new MapHub goes through the same ones.

1. **Build and tune.** Several months of tuning the method and recruiting participants. Started Oct 2026, with the script, the viewer and the first five portfolios; Opus 5.5 at low effort was chosen after the full-day check. Two-pass scoring (prompt v1) replaced one pass from Oct 15, after the two-pass test.
2. **Daily operation.** Each listing is scored every morning on a Claude Code seat, by the maintainer or by the driver ([Runs](#runs)), and the viewer updates on its own. Started with the Oct 7, 2026 listing, on the API until Oct 8 and on a Claude Code seat since; tuning goes on alongside.
3. **Legacy survey.** The MapHub goes back through earlier listings, from the present backwards to the first listing of the categories it surveys, where it ends by itself, building the back catalogs ([RESULTS.md, Back catalogs](RESULTS.md#back-catalogs)).
4. **Second pass.** The back catalogs keep the scores of the portfolios they were scored with. Once portfolios have changed enough, a second pass can rescore them; when to launch it is for the community to decide, the participants as much as the maintainer, since it costs seat usage on the scale of the legacy survey itself and changes what everyone's back catalog shows. The prototype has not reached it. A range of tables can be rescored in one run, for the columns served (the others are kept): `arxiv_snapshot.py listed <folder> <tables> <first> <last>`, then `daily_table.py --rescore` with its output; the prototype did so for all five columns of its tables of Sep 8 to Oct 14, 2026, scored before the portfolios were put in their standard format ([DEVELOPMENT.md, Two passes and usage](DEVELOPMENT.md#two-passes-and-usage), Prompt or portfolios).

**Cost.** Through Claude Code, each column runs on the signed-in seat, so the seat's usage limits pace the backfills (the prototype runs on a Standard seat of a Claude Team plan, which includes Claude Code, with 1.25 times the Pro plan's usage per session and a weekly limit, as of October 2026; an ordinary day of about 200 papers for five participants took about 3% of its weekly limit with prompt v0, about 36–45% of that with two passes, and about 30% less again per paper with prompt v2, [DEVELOPMENT.md, Caching test](DEVELOPMENT.md#two-passes-and-usage)); through the API, see [DEVELOPMENT.md, Budget](DEVELOPMENT.md#two-passes-and-usage). Subscription limits change over time, so test your own seat on a few listings before relying on these figures.

An estimate for the API route (Oct 2026): on Sonnet 5.5, one year of astro-ph listings (20,648 new submissions in the listings from Oct 7, 2025 to Oct 6, 2026; other listings scale with their size) would cost roughly $37 for the first column and $10 for each further one with prompt v0, since they share the cached papers; the two passes of prompt v1 cut this to about a third ([DEVELOPMENT.md, Two-pass test](DEVELOPMENT.md#two-passes-and-usage)), and chunks of 100 might cut more.

**Continuity.** A MapHub depends on its maintainer and one subscription. If the daily runs stop, the published tables and the viewer remain, and anyone can resume scoring from a copy ([README, Run your own MapHub](README.md#run-your-own-maphub)).
