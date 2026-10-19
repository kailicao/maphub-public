# MapHub: MIDAS arXiv Preprint Hub

**The prototype's viewer: <https://kailicao.github.io/maphub-public/>**

Each arXiv announcement day brings hundreds of new papers, and the colleague best placed to explain one is often close by but unknown. A MapHub serves a community of researchers who follow the same arXiv listings. Each participant, an individual or a group, writes a short research portfolio and keeps it up to date. On each announcement day, a large language model reads every new paper's title, authors and abstract against these portfolios and gives each paper a relevance score from 0 to 100 for each participant. The scores appear as a heatmap: one row per paper, one column per participant. Selecting yourself shows which new papers matter most to you; selecting a paper shows whose portfolios it matches best, a starting point for finding someone to ask about it. Papers come with people: a paper is worth more when someone nearby can discuss it. AI does the first pass of reading, while understanding still comes from reading and conversation.

Every MapHub works the same way. Its code, daily tables and viewer are public, and participants appear only by pseudonym; portfolios are shared only among participants. To spare usage, the model first scores each paper for all participants at once against short outlines of their portfolios, and reads a full portfolio only for the papers that pass this first look. A MapHub can also go back through earlier listings, building a back catalog of scored tables: the legacy survey.

**What is different.** Most tools that help with arXiv listings rank papers for one reader, privately, from keywords or from what that reader has clicked or rated. MapHub instead:

- scores every paper for every participant and shows the scores together, so that a paper also shows whose work it touches, and whom to ask;
- builds each profile from a portfolio the participant writes and corrects, in a common format, rather than learning it from clicks;
- publishes its scores, labeled as AI-generated, with each table's commit recording the portfolio version, configuration and code that produced it (the portfolios themselves stay among participants).

In the prototype's tables scored with the current portfolios (70 listings from May 29 to Oct 16, 2026, most of them backfilled), about 6% of a day's new astro-ph papers score 30 or more for each participant, on average: a handful worth reading out of some ninety. For a department, the heatmap can also seed a daily journal club like the astro-ph coffee discussions held in many astronomy departments, across subfields rather than one topic: papers that touch several columns make natural topics, and the participants they score highest for are natural people to invite to lead the discussion, if they wish. Scores come without explanations; each morning's table appears in the viewer after arXiv's 20:00 ET announcement is scored. To run a MapHub of your own, see [Run your own MapHub](#run-your-own-maphub).

**This repository is the prototype.** It is run by [Kaili Cao](https://kailicao.github.io/), its maintainer, and follows astrophysics (astro-ph) and artificial intelligence (cs.AI) (DEVELOPMENT.md, Listings). Its daily scoring began with the listing of Oct 7, 2026, for five participants, and it is under development; the viewer shows its current participants (the columns) and the days scored so far (the calendar). MapHub, and this prototype, were developed as a team project (a "carpentry") of the [Eric and Wendy Schmidt AI in Science Postdoctoral Fellowship](https://midas.umich.edu/training/postdoctoral-programs/schmidt-ai-in-science-postdoctoral-fellowship/) at the Michigan Institute for Data and AI in Society (MIDAS), led by Kaili Cao with teammates Kyle Heyblom, Eva Albalghiti and Dimitrios Simatos; it grew out of Kaili Cao's own literature workflow and extends it from one person to a community. To join the prototype, read What participants agree to (below) and email kailicao@umich.edu.

This README is for first readers: how to run a MapHub of your own, what participants agree to, how to write a portfolio and how to read the results. The portfolio-drafting prompt and the repository's layout follow below a marked line, and three documents go deeper: [RUNNING.md](RUNNING.md) (setting up and running a MapHub), [RESULTS.md](RESULTS.md) (the tables, the back catalogs and the viewer) and [DEVELOPMENT.md](DEVELOPMENT.md) (how MapHub works and why, and where it is going).

## Run your own MapHub

MapHub is meant to be copied: a high-energy physics department, say, can run one for hep-ph and hep-th without joining the prototype. Adapt anything you need, from the listings and the page to the code: the code and documents are under the MIT License, which lets you use, change and share them as long as the copyright and license notice stay with them, and the tables under CC BY 4.0 (License, below). The scoring code is general; the settings, the page's wording and colors, the user agent and the examples in `CLAUDE.md` are the prototype's own.

**The name.** MapHub, the MIDAS arXiv Preprint Hub, names the prototype and the copies run within MIDAS, for example by members of the carpentry team for their own disciplines. A copy elsewhere may take whatever name suits it; we would appreciate a note of where it comes from, such as a line in its README and on its page linking to this repository.

In short (the details are in [RUNNING.md](RUNNING.md), Setting up a copy):

1. **Make your copy** of this repository, and delete `tables/`.
2. **Choose the listings** in `config-log.json`.
3. **Set up the portfolios** from `portfolios-template/`: a private repository of their own, or, if your participants agree to public portfolios, a folder of your copy.
4. **Make the page yours:** the viewer's wording, colors and icons, this README and `CLAUDE.md`.
5. **Publish** the viewer on GitHub Pages.
6. **Run it** each morning after arXiv's 20:00 ET announcement, from one host computer, through Claude Code on a Claude subscription whose terms allow this use, or through the Claude API. `CLAUDE.md` sets up the host computer. Once it works, `mh -d` can run the routine unattended on a computer that never sleeps.
7. **Optionally, go back in time,** building a back catalog of earlier listings.

Questions are welcome: kailicao@umich.edu.

## What participants agree to

- **Public scores.** Every score is public, in the tables and the viewer, and stays public: after a participant pauses or leaves, their column can be greyed out or hidden in the viewer, but the tables, and their git history, keep it.
- **Pseudonyms are not anonymity.** Pseudonyms keep names off the page, but a participant's own papers score near 100, so a column can be linked to the person or group behind it. Join only if you are comfortable with your relevance scores being public.
- **Portfolios.** A portfolio is visible to every participant, and is sent with every scoring request to Anthropic, the provider of the language model (Claude), under the terms of the account used. Put nothing in it that you would not share that way.

## Writing a portfolio

A portfolio is the only thing a MapHub knows about a participant: every score comes from it. It works for any subject, a researcher, a group or a collaboration, in any field. The examples come from astronomy, where papers are also identified by NASA ADS bibcodes; other fields use their own identifiers, such as INSPIRE-HEP records, or DOIs. A first portfolio can be drafted from a publication record with the portfolio-drafting prompt (below the line).

The portfolio is built from papers, never from stated interests:

- **Bottom-up from papers.** Topics come from the papers the subject has, starting with their own. No topic is decided in advance.
- **Interests enter only through reading; plans are welcome.** A topic the subject hasn't published on appears only once their reading on it builds up. Interest without reading doesn't count. The subject's own plans do belong in the portfolio, in each topic's Next line or as a topic of their own (Basis: plans): portfolios are used to find papers of interest, and papers on planned work are among them. To improve a column's scores, add the papers the subject has read on a topic, and their plans: a portfolio drawn from a publication record shows what was published, not everything the subject follows.
- **The subject's own work, AI-drafted and corrected by the subject.**
- **Names and instructions.** A portfolio may name people the subject knows and add scoring instructions, within the length; both affect only that participant's column, and only for papers that pass the outline (below).
- **One paper can sit under several topics.** Those overlaps are often the most thought-provoking.
- **Versions live in git.** There's no changelog in the file, and each scoring run can be traced to a commit of the portfolios.

```markdown
# Research portfolio: <subject>

## Overview
<about 100–150 words of prose: the subject's main research thread and current focus>

## <Topic name>
Basis: <own papers | own work | reading | plans, or a combination>

Summary: <about 2–4 sentences: what the subject has done or learned here, and their position>

Next: <one line: the subject's plans in this topic; left out if there are none>

Expertise: <one line of phrases separated by semicolons, about 30–60 words: the methods, concepts, instruments, codes and data sets that name the topic, plans included>

Papers: <arXiv ID, bibcode or DOI, role (first author / coauthor)>, …

## <Next topic>
…
```

**The outline.** Scoring takes two passes. The first reads not the whole portfolio but its outline, which the scorer cuts from it at run time: the title, the Overview, and each topic's heading with its Next and Expertise lines, about 500 words for a person and 700 for a group. Only papers that pass this first look (in the prototype, an outline score of 10 or more) are scored again against the full portfolio. So everything a paper of interest could be about should show in the outline: the Overview names the main threads, and each topic's Expertise and Next lines name its methods, data and plans in the portfolio's own words. A topic without papers, such as a planned direction, still gets a heading, a Basis and these lines.

The lengths are guides. The Summary may run longer, with short bullet lists, when the extra detail comes from the papers, as in a group's portfolio that follows a series of papers; the Overview, Next and Expertise lines are best kept short, since the first pass reads them for every paper. A group's portfolio may end with a line defining the marks in its Papers lists (the portfolio-drafting prompt). The whole portfolio runs to about 1,500 words for a person and about 3,000 for a group or collaboration.

The portfolio must stand on its own: it contains no scores, and everything in it must make sense to a reader with no other context, such as the scorer.

## Understanding the results

### The viewer

- The base URL shows the latest day with an announcement; each day also has its own URL, such as `https://kailicao.github.io/maphub-public/2026-10-06`. Weekends have no arXiv listing and no table, and a day without an announcement (an arXiv holiday) is marked in the calendar (sand-colored in the prototype). The arrows, the date box and the calendar move between days and keep a selected participant.
- By default, papers are ranked by their average score over the participants shown, and participants by the rank correlation (ρ) of their scores with the average of the others'. Select a participant (column name) to sort papers by relevance to them and participants by similarity to them. Select a paper (title) to sort papers by similarity to it and participants by its relevance to them. Similarity is the correlation (r) of that day's scores.
- **Top papers** (`<base>/top`) lists one participant's highest-scored papers over a range of listing dates (by default the past calendar month up to the latest table, top 50); other participants can be shown beside, with their scores for the same papers, and each listing date opens that day's heatmap. Its URL keeps the choice, as in `<base>/top#p=<pseudonym>&also=<others>&from=2026-09-12&to=2026-10-12&n=50`. Selecting a participant in the heatmap offers a link to their top papers up to that day.
- Hover over an arXiv ID for its URL, or over a cell for its score. The selection is kept in the URL, so a view can be shared. Columns carry pseudonyms rather than names.
- The tables behind the viewer are public Parquet files, which you can download and analyze with your own tools ([RESULTS.md](RESULTS.md), Using the tables).

### Scoring rubric

Each paper is scored from its title, authors and abstract against a participant's portfolio, by a language model (Claude), on this scale. Intermediate values are allowed.

| Score | Meaning |
| --- | --- |
| 100 | The subject wrote it, or would write it if it didn't exist |
| 80 | The subject coauthored it, or would coauthor it if it didn't exist |
| 60 | The subject has cited it, or probably will at some point |
| 30 | The subject probably won't cite it, but it may broaden their view |
| 0 | Worth a glance at the title, but reading the abstract would waste the subject's time |

The anchors guide the model; they are not calibrated probabilities, so scores rank papers within a column and a day best. Most papers score low for most participants: in the 70 listings scored with the current portfolios (May 29–Oct 16, 2026), 97% of the scores are below 30, and 0.2% of all scores are 60 or above. In the first ratings of a blind study in progress (one rater so far; DEVELOPMENT.md, Measurements), most papers rated 50 or more (10 papers) were found within the top tenth of a listing read in score order, sooner than with a word-matching baseline; for papers rated 30 or more, MapHub finds about twice as many in the top tenth, and the two meet once about a third of a listing is read. A research note on MapHub and this study is in preparation. Low scores come from the first pass, against the outline, and are coarse; scores that matter come from the full portfolio. Scores are AI-generated and can be wrong: they are pointers to papers and people, not judgments of anyone's work.

### Terms

- **Listing:** the new submissions arXiv announces for a date, Monday to Friday, at 20:00 ET the evening before; a table is named by its listing date.
- **Backfill:** scoring a past listing, rebuilt from arXiv's records by submission time; the back catalog of past listings is the legacy survey.
- **Status:** active (scored on new listings and backfills), no-legacy (new listings only), paused or left (not scored).
- **Maintainer:** whoever runs a MapHub: the daily runs, the portfolios and the participants.
- **Host computer:** the one computer that runs a MapHub's scoring and pushes its tables.
- **Seat:** a Claude subscription account that Claude Code is signed in to, whose usage limits pace the runs.

---

**In depth.** The sections below are for participants drafting a portfolio and for anyone who wants the details. This is version 0.8 (DEVELOPMENT.md, Versions). [RUNNING.md](RUNNING.md) covers setting up a copy, participants and portfolios, the repositories, the runs and the configuration log, running unattended, and the stages; [RESULTS.md](RESULTS.md) the tables, their format, the back catalogs and the viewer; [DEVELOPMENT.md](DEVELOPMENT.md) how the scoring works and why: the daily table, the prompts, the measurements and budget behind its choices, versions, plans and open questions.

## The portfolio-drafting prompt

Run this once per participant, in a chatbot's Deep Research mode. Attach or paste at least one of the subject's profile page, publication list or CV. The result is a preliminary portfolio; save it as `portfolio.md`, keeping its Markdown headings (copying from a chat window can drop the `#` marks). The prompt names NASA ADS, INSPIRE-HEP and arXiv, and asks for whatever databases suit the subject's field. In the prototype, individuals either run it themselves or send the maintainer their CV to run it, and the maintainer drafts the portfolios of groups and collaborations and discusses them with them.

```text
Build a preliminary research portfolio for the researcher, group or collaboration that the attached documents describe (the subject). I have attached at least one of: a profile page, a publication list, a CV. Work out who the subject is from them. Treat them as the starting point, and use NASA ADS, INSPIRE-HEP, arXiv or other databases suited to the field to find, check and complete the subject's publication record.

Research steps:
1. Compile the subject's publications: title, arXiv ID (or ADS bibcode or DOI if not on arXiv), year, and role (first author, or coauthor with position in the author list). For a group or collaboration, see the note after these steps. Flag anything you are unsure belongs to the subject.
2. Read each paper's abstract, and for the most central papers (first-author papers, for a person) also the introduction and conclusions. arXiv's HTML full texts (arxiv.org/html/<ID>) are usually the most reliable way to read a paper; arXiv abstract pages and ADS pages often fail, and repeated requests get rate-limited. Keep track of papers you could read only at title level.
3. Group the papers into topics from the bottom up: let the topics emerge from what the papers actually do, not from a predefined list. A paper may belong to several topics. Do not add any topic that none of the papers support, even if a profile mentions it as an interest. A topic with only one or two papers may be merged into a close neighbour; ask me when unsure.

For a group or collaboration, decide case by case which papers count as the group's, from the documents and the papers themselves (author lists, acknowledgments, the group's stated aims), and ask me about every borderline case. Papers that may belong but are not confirmed can be included with less weight: mark them in the Papers lists, define the marks in one line at the end of the portfolio, and end with a line saying which expertise rests on confirmed papers and which on marked ones.

Write the portfolio in exactly this format:

# Research portfolio: <subject>

## Overview
<about 100–150 words of prose: the subject's main research thread and current focus>

## <Topic name>
Basis: own papers

Summary: <2–4 sentences: what the subject has done here and their position>

Next: <one line: the future work the papers state for this topic; leave the line out if there is none>

Expertise: <one line of phrases separated by semicolons, about 30–60 words: methods, concepts and data sets the papers show the subject commands; weigh central papers more than papers where the subject's role was small>

Papers: <arXiv ID, bibcode or DOI, role>, …

Rules: base every statement on the papers or the documents provided, not on guesses; write in plain language that a reader with no other context can follow; make this first draft comprehensive, including every topic the papers support, but write it with an eventual length of about 1,500 words (about 3,000 for a group or collaboration) in mind. The lengths are guides: the Summary may run longer, with short bullet lists if that helps, when the detail comes from the papers; keep the Overview, Next and Expertise lines short, since MapHub's first pass reads them for every paper. Include the limits the papers state in the Summary and the future work in the Next lines. If the draft falls short of the target length, expand from the main papers' introductions and conclusions; never pad. Where the documents and the papers disagree, follow the papers.

Output only the portfolio, not your working publication list, and then list the papers you could read only at title level.

Then talk the draft through with me. Ask, one question at a time, about (a) papers you were unsure belong to the subject, (b) topic boundaries you were unsure about, and (c) anything in the documents that contradicts the publication record. Then help me condense the portfolio to about 1,500 words (3,000 for a group or collaboration) by proposing what to merge, shorten or cut. Revise until I say it is final.
```

## Repository layout

| Path | Contents |
| --- | --- |
| `RUNNING.md` | setting up and running a MapHub: a copy step by step, participants and portfolios, repositories, runs and the configuration log, running unattended, stages |
| `RESULTS.md` | what a MapHub produces: using the tables, their format, the back catalogs (legacy survey), the web viewer |
| `DEVELOPMENT.md` | how MapHub works and why: the daily table, the prompts, the measurements and budget, versions, plans and open questions |
| `CLAUDE.md` | the operations file for an AI assistant: setting up the host computer and running MapHub day to day |
| `config-log.json` | every scoring configuration, with the date it took effect |
| `scorer/` | `daily_table.py`, which fetches a day's papers, scores them with Claude (through Claude Code or the API) and writes the tables; `local_run.py`, the daily routine around it (`mh`); `driver.py`, which runs that routine unattended (`mh -d`) |
| `viewer/` | the web viewer (`index.html`, `viewer.js`, `viewer.css`, and its icon: `favicon.svg` with PNG versions) and `serve.py`, a local server for testing |
| `tables/` | the daily tables (written by the scorer; CC BY 4.0) |
| `.github/workflows/` | publishing the viewer and tables on GitHub Pages |
| `portfolios-template/` | a minimal start for a MapHub's portfolios: `participants.json` and one portfolio in the format |
| `maphub-portfolios/` | the portfolios, in the prototype a private repository of its own (left out by `.gitignore`) |

## License

The code and documents are under the MIT License ([LICENSE](LICENSE)); the tables (`tables/`) are under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0).
