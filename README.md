# MapHub: MIDAS arXiv Preprint Hub

**Viewer: <https://kailicao.github.io/maphub-public/>**

Each arXiv announcement day, MapHub scores the new submissions to astrophysics (astro-ph) and artificial intelligence (cs.AI) for relevance to each participant, and shows the result as a heatmap: one row per paper, one column per participant, and in each cell a score from 0 to 100. A large language model assigns the scores by reading each paper's title, authors and abstract against a short research portfolio that every participant, individual or group, writes and keeps up to date. Selecting yourself shows which new papers matter most to you; selecting a paper shows who is most likely to answer questions about it.

MapHub is a carpentry of the [Eric and Wendy Schmidt AI in Science Postdoctoral Fellowship](https://midas.umich.edu/training/postdoctoral-programs/schmidt-ai-in-science-postdoctoral-fellowship/) at the Michigan Institute for Data and AI in Society (MIDAS), led by Kaili Cao with teammates Kyle Heyblom and Eva Albalghiti. Participants are researchers and research groups, shown by pseudonym; to join, email kailicao@umich.edu. Scores are public and stay in the tables after a participant pauses or leaves; the viewer can hide their column. This repository holds its code, configuration log, daily tables and web viewer, all public; portfolios are shared only among participants.

## Viewer

- The base URL shows the latest day; each day also has its own URL, such as `https://kailicao.github.io/maphub-public/2026-10-06`. Weekends and holidays have no arXiv listing and no table.
- Select a participant (column name) to sort papers by relevance to them and participants by similarity to them. Select a paper (title) to sort papers by similarity to it and participants by its relevance to them. Similarity is the correlation (r) of that day's scores.
- Hover over an arXiv ID for its URL, or over a cell for its score. The selection is kept in the URL, so a view can be shared.
- Columns carry pseudonyms rather than names.
- Titles and authors come from [DataCite](https://datacite.org/), which holds the metadata of arXiv's DOIs; the arXiv API cannot be queried from a browser. For the rare paper whose DOI DataCite lacks, the scorer saves them in a small file beside the day's table (Tables).

## Tables

One Parquet file per announcement date and arXiv ID month:

```text
tables/
  2024-2026/                 three-year spans aligned with 1991
    2610/                    arXiv ID month (YYMM)
      2026-10-06.parquet     papers with 2610 IDs announced on Oct 6, 2026
      2026-10-07.parquet
      2026-10-07.meta.json   metadata of papers DataCite lacks, only on days with any
  index.json                 the dates with a table, by ID month: {"2610": [5, 6]}
  participants.json          who is active, and whose columns the viewer hides
```

Each file has one row per paper: an `id` column holding the part of the arXiv ID the folder does not supply (`02245` for 2610.02245; `astro-ph_234` for the old-style astro-ph/0601234), then one `uint8` column of scores per participant. Each paper is scored once and appears in exactly one file. A date can have two files when its papers carry two ID months, as on the first listing of a month; `index.json` then lists that date in full under the other month, e.g. `"2609": [30, "2026-10-01"]`. On the rare day when DataCite has no record of a paper's DOI, a `<date>.meta.json` beside the Parquet file gives that paper's title, first three authors, author count and primary category, keyed like the `id` column; it is written once and never changed. Parquet opens in Python, R, Julia, MATLAB and DuckDB, for example:

```python
import pandas as pd
df = pd.read_parquet("tables/2024-2026/2610/2026-10-06.parquet")
```

## Configuration log

[`config-log.json`](config-log.json) records each change to the scoring setup with the date it takes effect: model, effort, prompt version, input (abstract only or more), listing (new submissions only or extended) and chunk size. Entries apply by the date a table is scored, not the listing it covers. Each commit that adds or changes a table names, in its message, the portfolios commit, the configuration entry and the columns behind it, so `git log -- <table>` gives the table's history. A rescored table replaces the old one, and git keeps the earlier version.

## Repository layout

| Path | Contents |
| --- | --- |
| `scorer/` | `daily_table.py`, which fetches a day's papers, scores them for each participant with the Claude API and writes the tables |
| `viewer/` | the web viewer (`index.html`, `viewer.js`, `viewer.css`) and `serve.py`, a local server for testing |
| `tables/` | the daily tables (written by the scorer) |
| `.github/workflows/` | publishing the viewer and tables on GitHub Pages |

## Running locally

Scoring needs the private portfolios repository checked out as `maphub-portfolios/` (one folder per pseudonym, each holding `portfolio.md`) and a Claude API key in `.env` (`ANTHROPIC_API_KEY=...`); both are kept out of git.

```bash
pip install -r scorer/requirements.txt
python scorer/daily_table.py --test      # 5 astro-ph and 5 cs.AI papers
python scorer/daily_table.py             # the current arXiv listing
python viewer/serve.py                   # viewer at http://localhost:8000/
```
