# MapHub results

What a MapHub produces and how people use it: the daily tables, the back catalogs of past listings, and the viewer that shows them as heatmaps. The [README](README.md) shows how to read the viewer; [RUNNING.md](RUNNING.md) says how a MapHub is run, and [DEVELOPMENT.md](DEVELOPMENT.md) how the scores are made. The format does not depend on the field; the examples and dates here come from the prototype (this repository: astro-ph and cs.AI).

## Using the tables

One Parquet file per announcement date and arXiv ID month, with one row per paper: an `id` column, then one `uint8` column of scores per participant; each paper appears in exactly one file ([Table format](#table-format) has the layout).

The tables are on the repository's tables branch (its main branch holds the code alone): clone that branch (`git clone -b tables …`), or read the files from the viewer's address (`<base>/tables/…`). Parquet opens in Python, R, Julia, MATLAB and DuckDB, for example:

```python
import pandas as pd
df = pd.read_parquet("tables/2024-2026/2610/2026-10-06.parquet")
```

[`config-log.json`](config-log.json) records each change to the scoring setup (model, prompt version, listings, threshold and the like) with the date it takes effect, and each commit that adds or changes a table names the portfolios, the configuration entry and the columns behind it, so `git log -- <table>` gives the table's history ([RUNNING.md, Configuration log and provenance](RUNNING.md#configuration-log-and-provenance)).

## Table format

The format holds for any arXiv listing; the examples come from astro-ph. Each table is named by its listing date, Monday to Friday, as shown on arXiv's listing pages and RSS feed; arXiv announces each listing at 20:00 ET the evening before, and the submission deadline is 14:00 ET. Tuesday's table covers everything submitted from Friday afternoon to Monday afternoon, weekend included, in one list. Each row stores only the part of the arXiv ID that the file's folder does not already imply; everything else about a paper is on arXiv. Scores are stored as uint8, since 0–100 fits in one byte.

| Papers submitted | Full ID | Stored in the row |
| --- | --- | --- |
| From January 2015 | 2510.01234 | 01234 (5 digits) |
| April 2007 to December 2014 | 1410.1234 | 1234 (4 digits) |
| Before April 2007 | astro-ph/0601234 | astro-ph\_234: archive, underscore, 3 digits; the archive is needed because each one numbered its papers separately, and an underscore replaces the slash (likewise `hep-ph_234` for hep-ph/9901234) |

An ID's month (YYMM) follows the announcement evening, not the listing date. The listing dated Oct 1, 2026, announced the evening of Sep 30, holds 2609 IDs, while papers submitted after that day's 14:00 ET deadline already get 2610 IDs. Papers held for moderation take the month of their eventual announcement: 2610.02245 was submitted Sep 30 and listed Oct 5. So folders go by ID month and files by listing date. Each new submission lands in exactly one file, and the folder supplies the month the row leaves out. Folders are grouped in three-year spans aligned with arXiv's first year, 1991:

```text
tables/
  1991-1993/
  …
  2024-2026/
    2609/
      2026-09-30.parquet
      2026-10-01.parquet  announced the evening of Sep 30
    2610/
      2026-10-02.parquet
      2026-10-07.meta.json  papers DataCite lacks (rare)
  top/                    the top-paper summaries, for the viewer
    <pseudonym>/2025.json  one participant's summary of a complete year
  empty.json              the dates whose table is empty: no announcement (a
                          holiday)
  index.json              the dates with a table, by ID month
  participants.json       who is active, and whose columns the viewer hides
  survey.json             where the legacy survey ended, once it reaches the
                          surveyed categories' beginning
```

A listing normally holds one ID month, but the format allows two files per date, and the viewer merges them. A static website cannot list folders, so `index.json` lists, for each ID-month folder, the dates with a table in it: a day of the folder's own month as a number, and the days of another month in a nested list that starts with that month as YYMM (`{"2609": [29, 30, [2610, 1]], "2610": [[2609, 22, 23], 2, 5, 6]}`), about 3 bytes per date.

Two things give a date files in two ID-month folders: the first listing of a month, announced the evening before, holds the previous month's IDs (`"2609": [..., [2610, 1]]`, the Oct 1 listing); and papers held for moderation are numbered in the month they are announced but placed by a rebuilt listing on the date of their submission (`"2610": [[2609, 22, 23], ...]`, a few late-September papers with October IDs).

A past listing with no paper left to score gets an empty table. `participants.json` beside `index.json` holds the viewer's active and hidden lists, and `empty.json` the dates whose table holds no paper (no announcement), which the calendar colors; both are rebuilt from the tables whenever one is written. On the rare day when DataCite has no record of a paper's DOI, a `<date>.meta.json` beside the Parquet file gives that paper's title, first three authors, author count and primary category, keyed like the `id` column ([Web viewer](#web-viewer)).

## Back catalogs

A MapHub goes back through earlier listings, from the present backwards to the first listing of the categories it surveys (arXiv itself began in 1991; astro-ph in April 1992), and scores them against the portfolios of every participant with status active. The result is a back catalog of scored tables that participants go through on their own. Building does not wait for reading, and a portfolio update does not rescore existing tables: they change only in a deliberately launched second pass, whose timing is the community's decision ([RUNNING.md, Stages](RUNNING.md#stages)). Every participant is in the legacy survey unless they choose status no-legacy, which keeps them to new papers only (the prototype's rule since Oct 11, 2026; before that the survey was opt-in, and at the change every participant joined it).

**Past listings.** The RSS feed shows only the current listing, so a past listing is rebuilt by submission time: the window between the two 14:00 ET deadlines it covers (Tuesday's listing: Friday 14:00 to Monday 14:00), keeping papers whose primary category falls under a configured listing (how they are rebuilt and checked: [DEVELOPMENT.md, Past listings](DEVELOPMENT.md#past-listings)).

**Holidays.** They are not modeled. arXiv skips some announcements (Monday Sep 7, 2026 meant no listing dated Tuesday Sep 8), and the next listing then holds both days' submissions; a rebuilt holiday instead gets a table of its own, so its papers are dated one listing early. On a holiday morning the current listing is empty: the scorer writes an empty table for it, with "No announcement" in the commit message, and the viewer shows the day as one without an announcement. Recording holidays was considered and dropped (Oct 2026): arXiv's catch-up pages, which show whether a date had a listing, reach back only 90 days, and its holiday schedule is published only for recent years, so a record would cover a few recent dates and treat the rest differently. Consistency matters more, and what matters most holds either way: the windows tile time, so no paper is lost or scored twice.

**Where the survey ends.** When a backfill is about to enter a new month, it checks arXiv's monthly listing page for each surveyed listing; once every one shows no paper in that month and the eleven before, the surveyed categories had not yet begun, so the survey is complete. The earliest table, the first weekday of the month the surveyed categories began, is then recorded in `tables/survey.json` (that month's weekdays before their first paper have empty tables, shown as days without an announcement), `mh -bb` says that nothing is left to backfill, and the driver keeps only the morning runs. A page that cannot be read never ends the survey.

**Limits.** The deadline is verified only for 2026, and how complete OAI-PMH's records (and the API's date search, the fallback) are before 2007 is not yet known. Old-style IDs (before April 2007) are stored as `astro-ph_234` ([Table format](#table-format)).

## Web viewer

The viewer is a web page at a fixed URL (the prototype's: <https://kailicao.github.io/maphub-public/>), which people can bookmark or download. It shows one day's table as a heatmap. GitHub Pages publishes it together with the tables, and `viewer/serve.py` serves the same layout locally for testing.

- **Dates.** Like [APOD](https://science.nasa.gov/apod/), the base URL shows the latest day with an announcement, and each day has its own URL; with no title to add, the date alone names it (e.g. `<base>/2026-10-02`). Pages serves a copy of the viewer for these paths, and the viewer reads the date from the URL. A date without a table, such as a weekend or a day not yet announced, shows a message. Arrows step through the days that have tables; a typed date takes effect on Enter. A calendar of the viewer's own highlights the days with a table, and colors days without an announcement; other weekdays are muted and weekends faint, since a browser's date picker cannot mark days. Moving to another day keeps a selected participant.
- **Rows.** When a day loads, the viewer shows each row's title and first three authors. The arXiv API sends no CORS header, so a browser cannot query it; the viewer instead asks DataCite, which holds the metadata of arXiv's DOIs (10.48550/arXiv.<ID>), with one search per 100 papers. DataCite's search index can lag behind new DOIs, so papers it misses are fetched one by one. Rarely, arXiv never registers a paper's DOI (2610.07148, listed Oct 7, 2026, was the first). For these, the scorer checks DataCite after writing a table and saves each lacking paper's title, first three authors, author count and primary category in `<date>.meta.json` beside the Parquet file; the viewer reads that file only when DataCite leaves gaps. A file is written once per day and ID month with lacking papers and never changed, so git stores each just once; titles otherwise stay out of the repo. Hovering over an arXiv number for about 0.5 s pops up the paper's arXiv URL, built from the ID; clicking it opens the paper. Each of the first three author names links to arXiv's author search in the paper's archive, by family name and initials, as arXiv's own listings do.
- **Page.** In the prototype, the header spells out the full name on a Michigan Blue bar with a Maize rule; the heatmap shades run from near-white to Michigan Blue and selections are marked in Maize. A folded About panel explains the page, the score scale and the participants, credits the fellowship and the team, and says that MapHub is under development and its AI-generated scores may be inaccurate; the footer repeats that and gives a contact email. The table fits the window's width, and on narrow screens the arXiv number moves under the title.
- **Columns.** Columns carry pseudonyms rather than real names. The tables, scores included, are public, and a column can often be linked to the person or group behind it, since their own papers score near 100 ([README, What participants agree to](README.md#what-participants-agree-to)). Columns of participants who paused or left are grayed out, and hidden ones are left out ([RUNNING.md, Participants and portfolios](RUNNING.md#participants-and-portfolios)).

A day opens with papers ranked by their average score over the visible columns, so papers that matter to many participants come first, and participants ranked by the rank correlation (Spearman's ρ) of their scores with the average of the other participants', shown next to each column. A user can reorder the heatmap in two ways, usually in this order:

| Select | Papers sorted by | Columns sorted by |
| --- | --- | --- |
| A participant | Relevance to that participant | Similarity to that participant |
| A paper | Similarity to that paper | Relevance of that paper to each participant |

Similarity is the correlation (r) of that day's scores, shown next to each column or row: two participants are similar when their columns match, and two papers when their rows match. The selection is kept in the URL, so a view can be shared.

**Top papers.** A second page, `<base>/top#p=<who>&also=<others>&from=<date>&to=<date>&n=<n>`, lists one participant's n highest-scored papers over a range of listing dates: by default the past calendar month up to the latest table and the top 50, ties going to the newer listing, then the arXiv ID. Other participants can be shown beside, with their scores for the same papers, to compare. For each complete year (it is over, and every weekday in it has a table; the survey's first year counts from where the survey ended), it first reads a small summary, `tables/top/<pseudonym>/<year>.json`, with each day's number of scored papers and its scores of 30 or more, and then reads only the days that can hold the top n; the days of a year without a summary are read in full. It asks DataCite only for the papers shown, so a long range stays quick. Each paper's listing date opens that day's heatmap, sorted for the participant, and a participant selected in the heatmap links to their top papers up to that day. Other trends over time can be studied by downloading the tables ([Using the tables](#using-the-tables)).
