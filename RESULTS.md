# MapHub results

What a MapHub produces and how people use it: the daily tables, the back catalogs of past listings, and the viewer that shows them as heatmaps. The [README](README.md) shows how to read the viewer; [RUNNING.md](RUNNING.md) says how a MapHub is run, and [DEVELOPMENT.md](DEVELOPMENT.md) how the scores are made. The format does not depend on the field; the examples and dates here come from the prototype (this repository: astro-ph and cs.AI, five participants).

## Using the tables

One Parquet file per announcement date and arXiv ID month, with one row per paper: an `id` column holding the part of the arXiv ID the folder does not supply (`02245` for 2610.02245), then one `uint8` column of scores per participant. Each paper is scored once and appears in exactly one file.

```text
tables/
  2024-2026/                 three-year spans aligned with 1991
    2610/                    arXiv ID month (YYMM)
      2026-10-06.parquet     papers with 2610 IDs announced on Oct 6, 2026
      2026-10-07.meta.json   metadata of papers DataCite lacks, only on days with any
  index.json                 the dates with a table, by ID month
  participants.json          who is active, and whose columns the viewer hides
  empty.json                 the dates whose table is empty: no announcement (a holiday)
```

Parquet opens in Python, R, Julia, MATLAB and DuckDB, for example:

```python
import pandas as pd
df = pd.read_parquet("tables/2024-2026/2610/2026-10-06.parquet")
```

The full format follows (Table format). [`config-log.json`](config-log.json) records each change to the scoring setup (model, prompt version, listings, threshold and the like) with the date it takes effect, and each commit that adds or changes a table names the portfolios, the configuration entry and the columns behind it, so `git log -- <table>` gives the table's history (RUNNING.md, Configuration log).

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
      2026-10-01.parquet    announced the evening of Sep 30
    2610/
      2026-10-02.parquet
      2026-10-07.meta.json  papers DataCite lacks (rare)
  index.json                the dates with a table, by ID month
```

A listing normally holds one ID month, but the format allows two files per date, and the viewer merges them. A static website cannot list folders, so `index.json` lists, for each ID-month folder, the dates with a table in it: a day of the folder's own month as a number, and the days of another month in a nested list that starts with that month as YYMM (`{"2609": [29, 30, [2610, 1]], "2610": [[2609, 22, 23], 2, 5, 6]}`), about 3 bytes per date. Two things give a date files in two ID-month folders: the first listing of a month, announced the evening before, holds the previous month's IDs (`"2609": [..., [2610, 1]]`, the Oct 1 listing); and papers held for moderation are numbered in the month they are announced but placed by a rebuilt listing on the date of their submission (`"2610": [[2609, 22, 23], ...]`, a few late-September papers with October IDs). A past listing with no paper left to score gets an empty table. `participants.json` beside `index.json` holds the viewer's active and hidden lists, and `empty.json` the dates whose table holds no paper (no announcement), rebuilt from the tables whenever one is written, which the calendar colors. On the rare day when DataCite has no record of a paper's DOI, a `<date>.meta.json` beside the Parquet file gives that paper's title, first three authors, author count and primary category, keyed like the `id` column (Web viewer). Files are Parquet: one row per paper, an ID column, then one uint8 column per participant pseudonym. Next to the tables, the daily job will keep the long-term correlation table (Web viewer); it is not built yet.

## Back catalogs

A MapHub goes back through earlier listings, from the present backwards to arXiv's first listing in 1991 (a goal; the prototype reached May 29, 2026 by Oct 19), and scores them against the portfolios of every participant with status active. The result is a back catalog of scored tables that participants go through on their own. Building does not wait for reading, and a portfolio update does not rescore existing tables: they change only in a deliberately launched second pass. Cross-lists are to be settled before the survey goes far back. Every participant is in the legacy survey unless they choose status no-legacy, which keeps them to new papers only (in the prototype since Oct 11, 2026; until then the survey was opt-in, and every participant was then included).

**Past listings.** The RSS feed shows only the current listing, so a past listing is rebuilt by submission time: the window between the two 14:00 ET deadlines it covers (Tuesday's listing: Friday 14:00 to Monday 14:00), keeping papers whose primary category falls under a configured listing. The papers come from a local cache of arXiv's OAI-PMH records (`.cache/oai/`, outside git), harvested by set (`physics:astro-ph`, `cs:cs:AI`) with each paper's first submission time, primary category, title, authors and abstract. OAI-PMH selects records by the date they last changed, so a harvest from a given day to today holds every paper submitted since that day; the cache keeps one such span per set, extends it backwards in steps of at least 30 days, and tops it up from its last harvest. A listing counts as covered once harvested three days after its window ends. Once the cache covers a month whole (six months back, since papers held for moderation keep their earlier submission dates), the backfill compares the month's entries in the cache with the total on arXiv's monthly listing page, one request per month and at most two months per backfill, and warns if they differ; every paper is still scored exactly once, since the windows tile time and a paper already in a table is never scored again. The arXiv API, one query per configured category (`categories` in the configuration log) and listing, is the fallback when OAI-PMH fails; how the two compare, in requests and in the papers found, is in DEVELOPMENT.md, OAI-PMH test. Against arXiv's own lists, 341 of 345 papers matched on Oct 6, 2026, 256 of 261 on Sep 15 and 304 of 326 on Aug 4; every difference was a paper held for moderation, which a rebuilt listing dates by submission rather than announcement. Since the windows tile time, every paper is scored at least once, at worst on a neighbouring date; and since a run leaves out papers already in another date's table, at most once. A held paper claimed by both a rebuilt and an RSS listing stays with whichever table was written first (two such papers, 2610.03840 and 2610.03850, were in both the Oct 5 and Oct 6 tables and were removed from Oct 5, a one-time correction). When the arXiv API or OAI-PMH is busy (429 Too Many Requests or 503 Service Unavailable, as on Oct 8 and Oct 16, 2026), the scorer waits 30 s, 60 s and then 2 min between tries, or as long as the reply's Retry-After asks, up to five tries; other failures get three quick tries. Holidays are not modelled. arXiv skips some announcements (Monday Sep 7, 2026 meant no listing dated Tuesday Sep 8), and the next listing then holds both days' submissions; a rebuilt holiday instead gets a table of its own, so its papers are dated one listing early. On a holiday morning the current listing is empty: the scorer writes an empty table for it, with "No announcement" in the commit message, and the viewer shows the day as one without an announcement. Recording holidays was considered and dropped (Oct 2026): arXiv's catch-up pages, which show whether a date had a listing, reach back only 90 days, and its holiday schedule is published only for recent years, so a record would cover a few recent dates and treat the rest differently. Consistency matters more, and what matters most holds either way: the windows tile time, so no paper is lost or scored twice. The deadline is verified only for 2026. Old-style IDs (before April 2007) are stored as `astro-ph_234` (Table format).

## Web viewer

The viewer is a web page at a fixed URL (the prototype's: <https://kailicao.github.io/maphub-public/>), that people can bookmark, and they can also download it. It shows one day's table as a heatmap. GitHub Pages publishes it together with the tables, and `viewer/serve.py` serves the same layout locally for testing.

- **Dates.** Like [APOD](https://science.nasa.gov/apod/), the base URL shows the latest day with an announcement, and each day has its own URL; with no title to add, the date alone names it (e.g. `<base>/2026-10-02`). Pages serves a copy of the viewer for these paths, and the viewer reads the date from the URL. A date without a table, such as a weekend or a day not yet announced, shows a message. Arrows step through the days that have tables; a typed date takes effect on Enter. A calendar of the viewer's own highlights the days with a table, and colors days without an announcement; other weekdays are muted and weekends faint, since a browser's date picker cannot mark days. Moving to another day keeps a selected participant.
- **Rows.** When a day loads, the viewer shows each row's title and first three authors. The arXiv API sends no CORS header, so a browser cannot query it; the viewer instead asks DataCite, which holds the metadata of arXiv's DOIs (10.48550/arXiv.<ID>), with one search per 100 papers. DataCite's search index can lag behind new DOIs, so papers it misses are fetched one by one. Rarely, arXiv never registers a paper's DOI (2610.07148, listed Oct 7, 2026, was the first). For these, the scorer checks DataCite after writing a table and saves each lacking paper's title, first three authors, author count and primary category in `<date>.meta.json` beside the Parquet file; the viewer reads that file only when DataCite leaves gaps. A file is written once per day and ID month with lacking papers and never changed, so git stores each just once; titles otherwise stay out of the repo. Hovering over an arXiv number for about 0.5 s pops up the paper's arXiv URL, built from the ID; clicking it opens the paper.
- **Columns.** Columns carry pseudonyms rather than real names. The tables, scores included, are public; participants know each other and can tell who is who. Columns of participants who paused or left are greyed out, and hidden ones are left out (RUNNING.md, Participants and portfolios).
- **Authors.** Each of the first three author names links to arXiv's author search in the paper's archive, by family name and initials, as arXiv's own listings do.
- **Page.** In the prototype, the header spells out the full name on a Michigan Blue bar with a Maize rule; the heatmap shades run from near-white to Michigan Blue and selections are marked in Maize. A folded About panel explains the page, the score scale and the participants, credits the fellowship and the team, and says that MapHub is under development and its AI-generated scores may be inaccurate; the footer repeats that and gives a contact email. The table fits the window's width, and on narrow screens the arXiv number moves under the title.

A day opens with papers ranked by their average score over the visible columns, so papers that matter to many participants come first, and participants ranked by the rank correlation (Spearman's ρ) of their scores with the average of the other participants', shown next to each column. A user can reorder the heatmap in two ways, usually in this order:

| Select | Papers sorted by | Columns sorted by |
| --- | --- | --- |
| A participant | Relevance to that participant | Similarity to that participant |
| A paper | Similarity to that paper | Relevance of that paper to each participant |

Similarity is the correlation (r) of that day's scores, shown next to each column or row: two participants are similar when their columns match, and two papers when their rows match. The selection is kept in the URL, so a view can be shared.

**Top papers.** A second page, `<base>/top#p=<who>&also=<others>&from=<date>&to=<date>&n=<n>`, lists one participant's n highest-scored papers over a range of listing dates: by default the past calendar month up to the latest table and the top 50, ties going to the newer listing, then the arXiv ID. Other participants can be shown beside, with their scores for the same papers, to compare. It asks DataCite only for the papers shown, so a long range stays quick. Each paper's listing date opens that day's heatmap, sorted for the participant, and a participant selected in the heatmap links to their top papers up to that day. Ranking by a set of participants (their average score) may follow. Anyone who wants other trends over time downloads the tables and analyzes them on their own.
