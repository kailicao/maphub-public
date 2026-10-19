#!/usr/bin/env python3
"""MapHub daily table: score one announcement day's new arXiv submissions.

Design: DEVELOPMENT.md (section "Daily table").

    python scorer/daily_table.py                # the current listing
    python scorer/daily_table.py 2026-10-05     # same, but check the date first
    python scorer/daily_table.py --test         # 5 papers of each listing, outputs in the work dir
    python scorer/daily_table.py --via api --batch   # Message Batches API (half price, slower)
    python scorer/daily_table.py 2025-03-04     # a past date: status-active columns only (the legacy survey)
    python scorer/daily_table.py 2026-10-05 --daily      # a past date scored as a daily run

Steps:
  1. Fetch. The arXiv API cannot query by announcement date, so the current
     listing's new submissions (no cross-lists, no replacements) are read from
     arXiv's RSS feed, and their metadata then comes from the arXiv API in one
     query. A past listing is rebuilt by submission time, the window between two
     14:00 ET deadlines that the listing covers, from a local cache of arXiv's
     OAI-PMH records (harvested once by set and topped up; the arXiv API, one query
     per category and listing, is the fallback). Papers held for
     moderation are placed by submission, not announcement: they are missing
     from the listing that announced them (4 of 345 on Oct 6, 2026) and
     appear in their submission window's listing instead. The papers are saved in the work dir, so a rerun still
     has them.
  2. Score. Prompt v1 takes two passes: one request per chunk of about 50 papers
     scores every participant against their outlines (the joint outline: each
     portfolio's title, Overview, and topics' Next and Expertise lines, cut at run
     time); then each participant's papers with an outline score of `threshold`
     or more are scored again against the full portfolio. Prompt v0 makes one
     request per participant per chunk, with the instructions and papers first,
     cached across participants.
  3. Write. One Parquet file per ID month, the viewer's index.json and
     empty.json (the dates without an announcement). The
     viewer takes titles and authors from DataCite; for the rare paper whose
     DOI DataCite lacks, a <date>.meta.json beside the Parquet file holds them.
  4. Resume. Each request's scores are saved as they arrive (outline chunks,
     then each participant's chunks); a rerun skips what is already saved. Parquet files are written only once all are done.

Participants are the folders of maphub-portfolios/ that hold a portfolio.md;
the folder name is the participant's pseudonym and column name.

Two ways to send requests (--via):
  claude-code  The default. Claude Code's print mode (claude -p), on whichever
               seat the claude command is signed in to. Only the participants
               named in MAPHUB_OWN_PARTICIPANTS (environment or .env,
               comma-separated) are scored; scorer/local_run.py sets it to every
               active participant.
  api          The Claude API, with ANTHROPIC_API_KEY from the environment or
               the repo root's .env file, which .gitignore keeps out of git.
               The API key is withheld from Claude Code, so it never bills
               the API account.

Exit codes: 0 done, 1 error, 2 incomplete (rerun to resume). A current listing
that arXiv did not announce (a holiday) gets an empty table, like a past listing
with nothing left to score.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import math
import os
import re
import sys
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import pyarrow as pa
import pyarrow.parquet as pq
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

ROOT = Path(__file__).resolve().parent.parent  # the maphub repo

RSS_BASE = "https://rss.arxiv.org/rss/"  # + the listings, joined by "+"
API_URL = "https://export.arxiv.org/api/query"
OAI_URL = "https://oaipmh.arxiv.org/oai"  # arXiv's interface for bulk metadata
OAI_CACHE = ROOT / ".cache" / "oai"  # harvested records, by set and submission month (not in git)
OAI_EXTEND_DAYS = 30  # a cache extended backwards grows by at least this many days
OAI_SETTLE_DAYS = 3  # a window counts as covered once harvested this many days after it ends
OAI_NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "raw": "http://arxiv.org/OAI/arXivRaw/"}
ARXIV_PAUSE = 3.0  # arXiv API terms: at most one request every 3 seconds
BUSY_WAITS = (30, 60, 120, 120)  # seconds before retrying a 429 or 503 reply: up to five tries
DATACITE_URL = "https://api.datacite.org/dois"  # where the viewer gets titles and authors
DATACITE_BATCH = 100  # DOIs per DataCite search, as in the viewer
# How the MapHub names itself to arXiv and DataCite; each MapHub sets its own in .env.
USER_AGENT_DEFAULT = "MapHub/0.1 (MIDAS arXiv Preprint Hub)"
user_agent = lambda: os.environ.get("MAPHUB_USER_AGENT") or USER_AGENT_DEFAULT

FIRST_YEAR = 1991  # table folders are 3-year spans aligned with arXiv's first year
EASTERN = ZoneInfo("America/New_York")
DEADLINE_HOUR = 14  # arXiv's daily submission deadline, 14:00 ET (unverified for older years)
# The arXiv listings MapHub follows, set from the configuration log (use_listings):
# "listings" are what arXiv's RSS feed and listing pages show (an archive such as
# astro-ph, or a category such as cs.AI), and "categories" are what past listings
# query in the arXiv API, one query each (an archive's subcategories listed too).
LISTINGS: list[str] = []
CATEGORIES: list[str] = []
PAGE = 500  # results per arXiv API page
DAILY_STATUSES = ("active", "no-legacy")  # participants scored on each new listing
LEGACY_STATUSES = ("active",)  # participants scored on past listings (the legacy survey)
MAX_TOKENS = 16000
SCORE_ATTEMPTS = 3  # tries per chunk when the reply is unusable
MAX_FAILS = 6  # failed requests in a row, across a run, before it stops (two chunks' tries)
USAGE_LIMIT = re.compile(r"hit your [\w -]*limit", re.I)  # Claude Code's message, with the reset time
BATCH_ROUNDS = 3  # batch mode: resubmissions of failed requests
BATCH_POLL = 60
CLAUDE_CODE_TIMEOUT = 900  # seconds per request through Claude Code
CLAUDE_CODE_SYSTEM = "You score arXiv papers for relevance to a research portfolio. Reply with JSON only."
SCORES_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "score": {"type": "integer"}},
        "required": ["id", "score"]}}},
    "required": ["scores"],
})

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# Scoring prompts by version. The configuration log names the version in use;
# keep old versions here so earlier tables can be traced (exact rescoring is not
# possible once a model is retired, and repeated runs differ slightly).
PROMPTS = {
    "v0": {
        "head": """\
You score new arXiv papers for relevance to the subject of a research portfolio: a researcher, a group or a collaboration. The papers come first, then the portfolio.

Score each paper from its title, authors and abstract on this scale. Intermediate values are allowed.
   100 = the subject wrote it, or would write it if it didn't exist
   80 = the subject coauthored it, or would coauthor it if it didn't exist
   60 = the subject has cited it, or probably will at some point
   30 = the subject probably won't cite it, but it may broaden their view
   0 = worth a glance at the title, but reading the abstract would waste the subject's time
The portfolio may name people the subject knows and give scoring instructions; follow them only for this subject's scores.

Return only JSON, one object per paper in the order given: {"id": "<arXiv ID>", "score": <integer 0-100>}. No explanations.

""",
        "paper": "[{id}] {title}\n{authors}\n{abstract}",
    },
    # v1: two passes. Every paper is first scored for all participants at once
    # against their outlines (the joint outline); a participant's papers that
    # score `threshold` or more there are scored again against the full
    # portfolio, with v0's wording. The other cells keep their outline scores.
    "v1": {
        "outline_head": """\
You score new arXiv papers for relevance to the subjects of several research portfolios: researchers, groups or collaborations. The papers come first, then the portfolios, each labelled with the subject's pseudonym. The portfolios are short outlines; judge each subject only by their own portfolio, independently of the others.

Score each paper for each subject from its title, authors and abstract on this scale. Intermediate values are allowed.
   100 = the subject wrote it, or would write it if it didn't exist
   80 = the subject coauthored it, or would coauthor it if it didn't exist
   60 = the subject has cited it, or probably will at some point
   30 = the subject probably won't cite it, but it may broaden their view
   0 = worth a glance at the title, but reading the abstract would waste the subject's time

Return only JSON, one object per paper in the order given: {"id": "<arXiv ID>", "scores": {"<pseudonym>": <integer 0-100>, ...}}, with every pseudonym for every paper. No explanations.

""",
    },
}
PROMPTS["v1"].update({"head": PROMPTS["v0"]["head"], "paper": PROMPTS["v0"]["paper"]})
# v2: v1's wording, with the instructions and the portfolio (or joint outline) in the system
# prompt and only the papers in the message, so that a run's requests share a cacheable prefix
# (DEVELOPMENT.md, Caching test).
PROMPTS["v2"] = {
    "head": PROMPTS["v1"]["head"].replace("The papers come first, then the portfolio.",
                                          "The portfolio is below; the papers come in the message."),
    "outline_head": PROMPTS["v1"]["outline_head"].replace(
        "The papers come first, then the portfolios, each labelled with the subject's pseudonym.",
        "The portfolios, each labelled with the subject's pseudonym, are below; the papers come in the message."),
    "paper": PROMPTS["v1"]["paper"],
    "system": True,
}
assert PROMPTS["v2"]["head"] != PROMPTS["v1"]["head"] and PROMPTS["v2"]["outline_head"] != PROMPTS["v1"]["outline_head"]


class MapHubError(Exception):
    pass


class ScoringStopped(MapHubError):
    """A usage limit, or MAX_FAILS failed requests in a row: the run stops, and the next
    run resumes from the saved chunks."""


_fails = 0  # failed requests in a row, across the run


def request_failed(error) -> None:
    """Count a failed request; stop the run at a usage limit or MAX_FAILS failures in a row."""
    global _fails
    _fails += 1
    if USAGE_LIMIT.search(str(error)):
        raise ScoringStopped("Claude Code reports a usage limit")
    if _fails >= MAX_FAILS:
        raise ScoringStopped(f"{_fails} requests failed in a row (a usage limit or an outage?)")


def request_ok() -> None:
    global _fails
    _fails = 0


class PartialScores(MapHubError):
    """A reply that scored only some of the papers; `scores` keeps those it did."""

    def __init__(self, message: str, scores: dict[str, int]):
        super().__init__(message)
        self.scores = scores


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- configuration


def load_env(path: Path) -> None:
    """Set KEY=VALUE lines from a .env file, without overriding the environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        # Skip empty values: an empty ANTHROPIC_API_KEY would still be used, and fail.
        if sep and value and not key.startswith("#"):
            os.environ.setdefault(key, value)


def load_config(path: Path, date: dt.date) -> dict:
    """Return the configuration-log entry in effect on `date` (the run date)."""
    entries = sorted(read_json(path), key=lambda e: e["effective"])
    active = [e for e in entries if dt.date.fromisoformat(e["effective"]) <= date]
    if not active:
        raise MapHubError(f"{path.name} has no entry in effect on {date}")
    cfg = active[-1]
    if cfg["prompt"] not in PROMPTS:
        raise MapHubError(f"unknown prompt version {cfg['prompt']!r}")
    if cfg.get("input", "abstract") != "abstract" or cfg.get("listing", "new") != "new":
        raise MapHubError("only input=abstract and listing=new are implemented")
    if not cfg.get("listings"):
        raise MapHubError(f"{path.name}: the entry in effect names no listings")
    return cfg


def use_listings(cfg: dict) -> None:
    """Follow the listings (and API categories) of a configuration-log entry."""
    LISTINGS[:] = cfg["listings"]
    CATEGORIES[:] = cfg.get("categories") or cfg["listings"]


def listings_text() -> str:
    """'astro-ph or cs.AI', for messages."""
    return " or ".join(LISTINGS)


# ------------------------------------------------------------------------ fetch


def http_get(url: str) -> bytes:
    """GET with retries. A busy server (429 Too Many Requests, 503 Service
    Unavailable) gets long waits, or the wait its Retry-After asks for; other
    failures get three quick tries."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    quick, busy = 0, 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503):
                error = e
            elif busy < len(BUSY_WAITS):
                after = (e.headers.get("Retry-After") or "").strip()
                wait = min(int(after), 300) if after.isdigit() else BUSY_WAITS[busy]
                busy += 1
                log(f"  GET failed ({e}); the server is busy, retrying in {wait} s")
                time.sleep(wait)
                continue
            else:
                raise MapHubError(f"GET {url} failed: {e} (still busy after {busy + 1} tries)") from e
        except (urllib.error.URLError, TimeoutError) as e:
            error = e
        quick += 1
        if quick == 3:
            raise MapHubError(f"GET {url} failed: {error}") from error
        log(f"  GET failed ({error}); retrying")
        time.sleep(ARXIV_PAUSE * (quick + 1))


def bare_id(raw: str) -> str:
    """An arXiv ID without version or prefix.

    'http://arxiv.org/abs/2610.02245v1' -> '2610.02245'; old-style IDs (before
    April 2007) keep their archive, without a subject class:
    'http://arxiv.org/abs/astro-ph/0601234v2' -> 'astro-ph/0601234',
    'cs.AI/0601001' -> 'cs/0601001'.
    """
    raw = raw.strip()
    if m := re.search(r"(\d{4}\.\d{4,5})(v\d+)?$", raw):
        return m.group(1)
    if m := re.search(r"([a-z-]+)(?:\.[A-Za-z-]+)?/(\d{7})(v\d+)?$", raw):
        return f"{m.group(1)}/{m.group(2)}"
    raise MapHubError(f"unexpected arXiv ID {raw!r}")


def id_parts(pid: str) -> tuple[str, str]:
    """(ID month YYMM, the row's ID): the folder supplies the month.

    '2610.02245' -> ('2610', '02245'); 'astro-ph/0601234' -> ('0601',
    'astro-ph_234'). Old IDs keep their archive, since each archive numbered
    its papers separately, joined with '_' rather than '/'.
    """
    if "/" in pid:
        archive, number = pid.split("/")
        return number[:4], f"{archive}_{number[4:]}"
    yymm, number = pid.split(".")
    return yymm, number


def full_id(yymm: str, row: str) -> str:
    """The inverse of id_parts."""
    if "_" in row:
        archive, number = row.split("_")
        return f"{archive}/{yymm}{number}"
    return f"{yymm}.{row}"


def clean(text: str | None) -> str:
    return " ".join((text or "").split())


def from_rss(item) -> dict:
    """One RSS item -> {id, title, authors, abstract, primary}, as from the API.

    For the rare paper the API's index lacks on the day it is listed. The feed
    lists the primary category first and the authors in one comma-separated line."""
    abstract = (item.findtext("description") or "").split("Abstract:", 1)[-1]
    creators = item.findtext("dc:creator", "", NS)
    return {
        "id": bare_id(item.findtext("guid", "")),
        "title": clean(item.findtext("title")),
        "authors": [clean(a) for a in creators.split(",") if a.strip()],
        "abstract": clean(abstract),
        "primary": clean(item.findtext("category")),
        "submitted": "",
    }


def fetch_listing() -> tuple[dt.date, dict[str, dict]]:
    """Read the current listing's date and its new submissions from the RSS feed,
    with each item's metadata as a fallback for the API."""
    root = ET.fromstring(http_get(RSS_BASE + "+".join(LISTINGS)))
    channel = root.find("channel")
    pub = channel.findtext("pubDate") if channel is not None else None
    if not pub:
        raise MapHubError("RSS feed has no pubDate")
    listing_date = email.utils.parsedate_to_datetime(pub).date()
    items = {}
    for item in root.iter("item"):
        if item.findtext("arxiv:announce_type", "", NS).strip() == "new":
            paper = from_rss(item)
            items[paper["id"]] = paper
    return listing_date, dict(sorted(items.items()))


def parse_entry(entry) -> dict:
    """One arXiv API entry -> {id, title, authors, abstract, primary}."""
    primary = entry.find("arxiv:primary_category", NS)
    return {
        "id": bare_id(entry.findtext("atom:id", "", NS)),
        "title": clean(entry.findtext("atom:title", "", NS)),
        "authors": [clean(a.findtext("atom:name", "", NS)) for a in entry.findall("atom:author", NS)],
        "abstract": clean(entry.findtext("atom:summary", "", NS)),
        "primary": primary.get("term", "") if primary is not None else "",
        "submitted": entry.findtext("atom:published", "", NS),
    }


def fetch_metadata(ids: list[str], required: bool = True) -> dict[str, dict]:
    """Title, authors, abstract and primary category from the arXiv API. With
    required=False, papers the API still lacks after a retry are left out."""
    papers: dict[str, dict] = {}
    todo = list(ids)
    for attempt in range(2):  # the API occasionally drops entries; ask once more
        for start in range(0, len(todo), 200):
            part = todo[start : start + 200]
            query = urllib.parse.urlencode(
                {"id_list": ",".join(part), "max_results": len(part)}
            )
            time.sleep(ARXIV_PAUSE)
            root = ET.fromstring(http_get(f"{API_URL}?{query}"))
            for entry in root.findall("atom:entry", NS):
                paper = parse_entry(entry)
                papers[paper["id"]] = paper
        todo = [i for i in ids if i not in papers]
        if not todo:
            break
        log(f"  arXiv API missed {len(todo)} papers; asking again")
    if todo and required:
        raise MapHubError(f"arXiv API returned no metadata for {', '.join(todo)}")
    return papers


def under(category: str, listing: str) -> bool:
    """Whether a category belongs to a listing: the listing itself, or a subcategory
    of an archive (astro-ph.CO under astro-ph)."""
    return category == listing or ("." not in listing and category.startswith(listing + "."))


def listed_here(paper: dict) -> bool:
    return any(under(paper["primary"], listing) for listing in LISTINGS)


def listing_window(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The submission window (UTC) of the listing dated `date`.

    A listing dated D (Monday to Friday) is announced the evening before and
    holds the submissions received between the 14:00 ET deadlines of the two
    weekdays before D: Tuesday's listing covers Friday 14:00 to Monday 14:00.
    Holidays, when arXiv skips an announcement and merges windows, are not
    modelled yet, and the 14:00 deadline is checked only for 2026. Either way
    the windows tile time, so every paper is scored once; an error only moves
    some papers to a neighbouring date. Checked against arXiv's catch-up pages:
    256 of 261 papers on Sep 15, 2026 and 304 of 326 on Aug 4, 2026, every
    difference a paper held for moderation.
    """
    if date.weekday() >= 5:
        raise MapHubError(f"{date} is a weekend day; arXiv has no listing then")

    def weekday_before(d: dt.date) -> dt.date:
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d

    end_day = weekday_before(date)
    start_day = weekday_before(end_day)
    at = lambda d: dt.datetime(d.year, d.month, d.day, DEADLINE_HOUR, tzinfo=EASTERN).astimezone(dt.timezone.utc)
    return at(start_day), at(end_day)


def fetch_past_listing_api(date: dt.date) -> list[dict]:
    """Rebuild a past listing's new submissions from the arXiv API by submission time."""
    start, end = listing_window(date)
    stamp = lambda t: t.strftime("%Y%m%d%H%M")
    # The API matches whole minutes; a paper at exactly 14:00 can land in either
    # listing, and is filtered below by its exact time.
    log(f"Rebuilding the listing dated {date} from the arXiv API: submissions "
        f"{start.astimezone(EASTERN):%a %Y-%m-%d %H:%M} to {end.astimezone(EASTERN):%a %Y-%m-%d %H:%M} ET")
    found: dict[str, dict] = {}
    # One query per category: an OR of several categories loses results on
    # older dates (128 instead of several hundred for a 2025 listing).
    for cat in CATEGORIES:
        query = f"cat:{cat} AND submittedDate:[{stamp(start)} TO {stamp(end)}]"
        for first in range(0, 30000, PAGE):
            params = urllib.parse.urlencode({"search_query": query, "start": first, "max_results": PAGE,
                                             "sortBy": "submittedDate", "sortOrder": "ascending"})
            time.sleep(ARXIV_PAUSE)
            root = ET.fromstring(http_get(f"{API_URL}?{params}"))
            entries = root.findall("atom:entry", NS)
            for entry in entries:
                paper = parse_entry(entry)
                found[paper["id"]] = paper
            total = int(root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults", "0"))
            if first + PAGE >= total or not entries:
                break
    when = lambda p: dt.datetime.fromisoformat(p["submitted"].replace("Z", "+00:00"))
    papers = sorted((p for p in found.values() if listed_here(p) and start <= when(p) < end),
                    key=lambda p: p["id"])
    log(f"  {len(found)} papers in the window, {len(papers)} with {listings_text()} as primary")
    return papers


# Past listings from OAI-PMH. A harvest selects records by the date they last
# changed, so a harvest from day F to day T holds every paper submitted on or
# after F (as of T): a later replacement only moves a record's datestamp forward.
# The cache keeps one such span per set, extended backwards with a harvest that
# ends where the span begins, and forwards with one from its last harvest day.
OAI_GROUPS = {"astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph",
              "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph"}  # archives in OAI's physics set


def oai_set(listing: str) -> str:
    """The OAI-PMH set of a listing: astro-ph -> physics:astro-ph, cs.AI -> cs:cs:AI."""
    archive, _, sub = listing.partition(".")
    group = "physics" if archive in OAI_GROUPS else archive
    if group == "physics":
        return f"physics:{archive}" + (f":{sub}" if sub else "")
    return group + (f":{archive}:{sub}" if sub else "")


def oai_record(rec) -> dict | None:
    """One arXivRaw record -> {id, title, authors, abstract, primary, submitted}."""
    raw = rec.find("oai:metadata/raw:arXivRaw", OAI_NS)
    if raw is None:  # a deleted record
        return None
    v1 = raw.find("raw:version[@version='v1']/raw:date", OAI_NS)
    if v1 is None:
        return None
    submitted = email.utils.parsedate_to_datetime(v1.text).astimezone(dt.timezone.utc)
    authors = re.split(r",\s*|\s+and\s+", clean(raw.findtext("raw:authors", "", OAI_NS)))
    return {
        "id": clean(raw.findtext("raw:id", "", OAI_NS)),
        "title": clean(raw.findtext("raw:title", "", OAI_NS)),
        "authors": [a for a in authors if a],
        "abstract": clean(raw.findtext("raw:abstract", "", OAI_NS)),
        "primary": (raw.findtext("raw:categories", "", OAI_NS).split() or [""])[0],
        "submitted": submitted.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def oai_harvest(setspec: str, frm: dt.date, until: dt.date | None) -> int:
    """Harvest a set's records changed from `frm` (to `until`, or today) into the cache."""
    folder = OAI_CACHE / setspec.replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    params = {"verb": "ListRecords", "metadataPrefix": "arXivRaw", "set": setspec, "from": frm.isoformat()}
    if until:
        params["until"] = until.isoformat()
    log(f"  harvesting {setspec} records changed {frm}–{until or 'today'} from arXiv OAI-PMH")
    calls = count = 0
    while True:
        time.sleep(ARXIV_PAUSE)
        root = ET.fromstring(http_get(f"{OAI_URL}?{urllib.parse.urlencode(params)}"))
        calls += 1
        error = root.find("oai:error", OAI_NS)
        if error is not None:
            if error.get("code") == "noRecordsMatch":
                break
            raise MapHubError(f"OAI-PMH error for {setspec}: {error.get('code')}: {clean(error.text)}")
        by_month: dict[str, list[str]] = {}
        for rec in root.iterfind("oai:ListRecords/oai:record", OAI_NS):
            paper = oai_record(rec)
            if paper:
                by_month.setdefault(paper["submitted"][:7], []).append(json.dumps(paper, ensure_ascii=False))
                count += 1
        for month, lines in by_month.items():
            with open(folder / f"{month}.jsonl", "a", encoding="utf-8") as out:
                out.write("\n".join(lines) + "\n")
        token = clean(root.findtext("oai:ListRecords/oai:resumptionToken", "", OAI_NS))
        if not token:
            break
        params = {"verb": "ListRecords", "resumptionToken": token}
    log(f"    {count} records in {calls} requests")
    return calls


def oai_cover(setspec: str, start: dt.datetime, end: dt.datetime) -> None:
    """Make sure the cache holds every paper of `setspec` submitted in [start, end)."""
    state_path = OAI_CACHE / "state.json"
    state = read_json(state_path) if state_path.exists() else {}
    span = state.get(setspec)
    today = dt.datetime.now(dt.timezone.utc).date()
    need_from = start.date() - dt.timedelta(days=1)
    if span is None:
        oai_harvest(setspec, need_from, None)
        span = {"from": need_from.isoformat(), "harvested": today.isoformat()}
    else:
        frm, harvested = dt.date.fromisoformat(span["from"]), dt.date.fromisoformat(span["harvested"])
        if need_from < frm:
            new_from = min(need_from, frm - dt.timedelta(days=OAI_EXTEND_DAYS))
            oai_harvest(setspec, new_from, frm - dt.timedelta(days=1))
            span["from"] = new_from.isoformat()
        if harvested < end.date() + dt.timedelta(days=OAI_SETTLE_DAYS) and harvested < today:
            oai_harvest(setspec, harvested, None)
            span["harvested"] = today.isoformat()
    state[setspec] = span
    write_json(state_path, state)


def id_month(arxiv_id: str) -> str:
    """The YYMM of an arXiv ID: '2608.01234' -> '2608', 'astro-ph/0501123' -> '0501'."""
    m = re.search(r"(?:^|/)(\d{4})(?:\.\d+|\d{3})(?:v\d+)?$", arxiv_id)
    return m.group(1) if m else ""


CHECK_TRIES = 3  # tries for a month whose listing page cannot be read


def oai_month_check(listing: str, setspec: str) -> None:
    """Compare the OAI-PMH cache with arXiv's monthly listing page and warn when they differ.

    A month (its IDs, that is its announcements) is checked once the cache starts by its first
    day (a harvest keeps every record changed since, and a paper announced in the month changed
    then at the latest) and was harvested a few days after it ends. Its papers are read from the
    cache files of the month and the six before, by submission month, since papers held for
    moderation keep their earlier submission dates (of August 2026's cs.AI IDs, 544 were submitted
    in April to July). arXiv's page counts every entry, cross-lists included, as the set does
    (equal for astro-ph in Jul and Aug 2026 and cs.AI in Aug 2026). Advisory: one request per
    month, at most two months per call; a month whose page cannot be read is tried again on later
    calls, up to CHECK_TRIES times. state.json keeps, per month, [cache count, arXiv's total] or
    {"tries": n}."""
    state_path = OAI_CACHE / "state.json"
    state = read_json(state_path) if state_path.exists() else {}
    span = state.get(setspec)
    if not span:
        return
    checked = state.setdefault("checked", {}).setdefault(setspec, {})
    lookback = 6  # months of submission files read for each month's IDs
    start = dt.date.fromisoformat(span["from"])
    month = start if start.day == 1 else (start.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    last = dt.date.fromisoformat(span["harvested"]) - dt.timedelta(days=OAI_SETTLE_DAYS)
    todo = []
    while (month + dt.timedelta(days=32)).replace(day=1) <= last:
        result = checked.get(month.strftime("%Y-%m"))
        if result is None or (isinstance(result, dict) and result.get("tries", 0) < CHECK_TRIES):
            todo.append(month)
        month = (month + dt.timedelta(days=32)).replace(day=1)
    folder = OAI_CACHE / setspec.replace(":", "_")
    for month in todo[:2]:  # the earliest first: the backfill's front
        yymm, key = month.strftime("%y%m"), month.strftime("%Y-%m")
        ids = set()
        for back in range(lookback + 1):
            path = folder / f"{(month.replace(day=15) - dt.timedelta(days=30 * back)).strftime('%Y-%m')}.jsonl"
            if path.exists():
                for line in open(path, encoding="utf-8"):
                    paper_id = json.loads(line)["id"]
                    if id_month(paper_id) == yymm:
                        ids.add(paper_id)
        time.sleep(ARXIV_PAUSE)
        try:
            page = http_get(f"https://arxiv.org/list/{listing}/{key}?skip=0&show=25").decode("utf-8", "replace")
            found = re.search(r"Total of ([\d,]+) entries", page)
            if not found:
                raise MapHubError("the page gave no total")
        except Exception as e:  # advisory: never stop a backfill; try the month again later
            tries = (checked.get(key) or {}).get("tries", 0) + 1
            checked[key] = {"tries": tries}
            log(f"  OAI-PMH check for {listing} {key} skipped ({e})" +
                (f"; not tried again after {tries} tries" if tries >= CHECK_TRIES else "; tried again later"))
            continue
        total = int(found.group(1).replace(",", ""))
        checked[key] = [len(ids), total]
        if len(ids) != total:
            log(f"Warning: the OAI-PMH cache holds {len(ids)} of arXiv's {total} {listing} entries for {key}; "
                f"its listings may be incomplete")
    write_json(state_path, state)


def fetch_past_listing_oai(date: dt.date) -> list[dict]:
    """Rebuild a past listing's new submissions from the OAI-PMH cache by submission time."""
    start, end = listing_window(date)
    log(f"Rebuilding the listing dated {date} from the arXiv OAI-PMH cache: submissions "
        f"{start.astimezone(EASTERN):%a %Y-%m-%d %H:%M} to {end.astimezone(EASTERN):%a %Y-%m-%d %H:%M} ET")
    found: dict[str, dict] = {}
    months = sorted({start.strftime("%Y-%m"), end.strftime("%Y-%m")})
    for listing in LISTINGS:
        setspec = oai_set(listing)
        oai_cover(setspec, start, end)
        oai_month_check(listing, setspec)
        for month in months:
            path = OAI_CACHE / setspec.replace(":", "_") / f"{month}.jsonl"
            if path.exists():
                for line in open(path, encoding="utf-8"):
                    paper = json.loads(line)
                    found[paper["id"]] = paper  # later harvests win
    when = lambda p: dt.datetime.fromisoformat(p["submitted"].replace("Z", "+00:00"))
    papers = sorted((p for p in found.values() if listed_here(p) and start <= when(p) < end),
                    key=lambda p: p["id"])
    log(f"  {len(papers)} papers with {listings_text()} as primary")
    return papers


def fetch_past_listing(date: dt.date) -> tuple[list[dict], str]:
    """A past listing from the OAI-PMH cache, or from the arXiv API if OAI-PMH fails."""
    try:
        return fetch_past_listing_oai(date), "arXiv OAI-PMH by submission time"
    except (MapHubError, ET.ParseError) as e:
        log(f"  OAI-PMH failed ({e}); falling back to the arXiv API")
        return fetch_past_listing_api(date), "arXiv API by submission time"


def get_papers(work: Path, date: dt.date | None) -> tuple[dt.date, list[dict]]:
    """Papers for `date` (default: the current listing), from the work dir if saved."""
    if date is not None:
        saved = work / date.isoformat() / "papers.json"
        if saved.exists():
            log(f"Papers for {date}: loaded from {saved}")
            return date, read_json(saved)
        if date < dt.datetime.now(EASTERN).date():
            papers, source = fetch_past_listing(date)
            if not papers:
                log(f"  no new submissions in {listings_text()} for the listing dated {date}")
            start, end = listing_window(date)
            write_json(saved, papers)
            write_json(saved.with_name("window.json"),
                       {"source": source, "from_utc": start.isoformat(),
                        "to_utc": end.isoformat()})
            return date, papers
    log(f"Fetching the current listing from {RSS_BASE + '+'.join(LISTINGS)}")
    listing_date, rss = fetch_listing()
    ids = list(rss)
    if date is not None and date != listing_date:
        raise MapHubError(
            f"no saved papers for {date}, and the current arXiv listing is dated "
            f"{listing_date}; only the current listing can be fetched"
        )
    saved = work / listing_date.isoformat() / "papers.json"
    if saved.exists():  # the default date's papers were fetched by an earlier run
        log(f"Papers for {listing_date}: loaded from {saved}")
        return listing_date, read_json(saved)
    if not ids:
        # arXiv announced nothing for this date (a holiday): the empty listing gets an
        # empty table, which the viewer shows as a day without an announcement.
        log(f"Listing dated {listing_date}: no new submissions (no announcement: an arXiv holiday?)")
        write_json(saved, [])
        return listing_date, []
    log(f"Listing dated {listing_date}: {len(ids)} new submissions; fetching metadata")
    try:
        meta = fetch_metadata(ids, required=False)
    except MapHubError as e:
        # The API refuses (a rate limit on this computer can last hours): the feed has
        # every paper's title, authors, abstract and primary category too.
        log(f"  the arXiv API failed ({e}); taking all metadata from the RSS feed")
        meta = {}
    if lacking := [i for i in ids if i not in meta]:
        # The API's index occasionally lags behind the listing (2610.09954 on Oct 8, 2026).
        if meta:
            log(f"  taking {len(lacking)} papers from the RSS feed, which the API lacks: {', '.join(lacking)}")
        meta.update({i: rss[i] for i in lacking})
    papers = [meta[i] for i in ids if listed_here(meta[i])]
    if len(papers) < len(ids):
        log(f"  dropped {len(ids) - len(papers)} papers whose primary category is elsewhere")
    write_json(saved, papers)
    return listing_date, papers


def test_sample(papers: list[dict], per_listing: int = 5) -> list[dict]:
    """The first few papers of each listing."""
    sample = [p for listing in LISTINGS for p in [q for q in papers if under(q["primary"], listing)][:per_listing]]
    return sorted({p["id"]: p for p in sample}.values(), key=lambda p: p["id"])


# ------------------------------------------------------------------------ score


def load_participants(folder: Path, legacy: bool = False) -> dict[str, str]:
    """Pseudonym -> portfolio text, for the participants to score.

    participants.json in maphub-portfolios/ gives each participant's status:
    "active" (scored daily and in the legacy survey, past listings), "no-legacy"
    (scored daily only), "paused" or "left" (not scored). With legacy=True (a past
    listing), only "active" participants are scored. A portfolio folder with no
    entry there is skipped with a warning, so a new portfolio never breaks the run.
    """
    if not folder.is_dir():
        raise MapHubError(f"portfolio folder {folder} not found")
    status_file = folder / "participants.json"
    if not status_file.is_file():
        raise MapHubError(f"{status_file} not found; it lists each participant's status")
    status = read_json(status_file)["participants"]
    found = {}
    for sub in sorted(folder.iterdir()):
        portfolio = sub / "portfolio.md"
        if not portfolio.is_file():
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sub.name):
            raise MapHubError(f"pseudonym {sub.name!r} must match [A-Za-z0-9_-]{{1,40}}")
        entry = status.get(sub.name)
        if entry is None:
            log(f"Warning: {sub.name} has a portfolio but no entry in participants.json; not scored")
            continue
        if entry.get("status") not in (LEGACY_STATUSES if legacy else DAILY_STATUSES):
            continue
        found[sub.name] = portfolio.read_text(encoding="utf-8").strip()
    for who in status:
        if not (folder / who / "portfolio.md").is_file() and status[who].get("status") in DAILY_STATUSES:
            log(f"Warning: {who} is active in participants.json but has no portfolio.md")
    if not found:
        raise MapHubError(f"no participants to score under {folder}"
                          + (" (none with status active, for the legacy survey)" if legacy else ""))
    return found


def outline(portfolio: str) -> str:
    """A portfolio's outline: its title, its Overview, and each topic's heading with
    its Next and Expertise lines (README.md, Writing a portfolio). It is cut at run time and never stored."""
    lines = portfolio.splitlines()
    out = [lines[0].strip()] if lines and lines[0].startswith("# ") else []
    overview = re.search(r"^## Overview[ \t]*\n(.*?)(?=^## |\Z)", portfolio, re.S | re.M)
    if overview:
        out += ["", "## Overview", overview.group(1).strip()]
    for m in re.finditer(r"^## (.+)$", portfolio, re.M):
        if m.group(1).strip() == "Overview":
            continue
        section = re.split(r"^## ", portfolio[m.end():], maxsplit=1, flags=re.M)[0]
        out += ["", f"## {m.group(1).strip()}"]
        out += [x.group(0).strip() for x in re.finditer(r"^(?:Next|Expertise):.*$", section, re.M)]
    return "\n".join(out)


def joint_outline(portfolios: dict[str, str]) -> str:
    """Every participant's outline, labelled by pseudonym, for the outline pass."""
    parts = []
    for who, text in portfolios.items():
        cut = outline(text)
        if "Expertise:" not in cut:
            log(f"Warning: {who}'s portfolio has no Expertise lines; its outline is thin (README.md, Writing a portfolio)")
        parts.append(f'<portfolio subject="{who}">\n{cut}\n</portfolio>')
    return "\n\n".join(parts) + "\n"


def make_chunks(papers: list[dict], size: int) -> list[list[dict]]:
    """Split into the fewest chunks of at most `size` papers, as even as possible."""
    n = len(papers)
    k = max(1, math.ceil(n / size))
    return [papers[i * n // k : (i + 1) * n // k] for i in range(k)]


def papers_prefix(prompt: dict, chunk: list[dict], head: str = "head") -> str:
    """The cached part of the request: instructions plus papers, identical for everyone."""
    entries = [
        prompt["paper"].format(
            id=p["id"], title=p["title"], authors=", ".join(p["authors"]), abstract=p["abstract"]
        )
        for p in chunk
    ]
    return prompt[head] + "<papers>\n" + "\n\n".join(entries) + "\n</papers>\n\n"


def request_params(cfg: dict, prefix: str, portfolio: str) -> dict:
    prompt = PROMPTS[cfg["prompt"]]
    if prompt.get("system"):  # v2: instructions and portfolio as the (cached) system prompt
        params = {"model": cfg["model"], "max_tokens": MAX_TOKENS,
                  "system": [{"type": "text", "text": prompt["head"] + f"<portfolio>\n{portfolio}\n</portfolio>",
                              "cache_control": {"type": "ephemeral"}}],
                  "messages": [{"role": "user", "content": prefix.removeprefix(prompt["head"])}]}
        if cfg.get("effort"):
            params["output_config"] = {"effort": cfg["effort"]}
        return params
    params = {
        "model": cfg["model"],
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prefix},
                    {"type": "text", "text": f"<portfolio>\n{portfolio}\n</portfolio>"},
                ],
            }
        ],
    }
    # Caching the papers block costs 25% extra to write and pays off only when
    # other participants read it, so a lone participant skips it.
    if cfg.get("cache", True):
        params["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    if cfg.get("effort"):
        params["output_config"] = {"effort": cfg["effort"]}
    return params


def parse_scores(message, chunk: list[dict]) -> dict[str, int]:
    """Read {"id", "score"} objects from the reply; every paper in the chunk must have one."""
    if message.stop_reason == "refusal":
        category = getattr(message.stop_details, "category", None)
        raise MapHubError(f"refused (category: {category})")
    if message.stop_reason == "max_tokens":
        raise MapHubError("reply cut off at max_tokens")
    return scores_from_text("".join(b.text for b in message.content if b.type == "text"), chunk)


def scores_from_text(text: str, chunk: list[dict]) -> dict[str, int]:
    wanted = {p["id"] for p in chunk}
    scores: dict[str, int] = {}
    for obj in re.findall(r"\{[^{}]*\}", text):
        try:
            item = json.loads(obj)
            pid = bare_id(str(item["id"]))
            score = round(float(item["score"]))
        except (ValueError, KeyError, TypeError, MapHubError):
            continue
        if pid in wanted:
            scores[pid] = min(100, max(0, score))
    missing = wanted - scores.keys()
    if missing:
        raise PartialScores(f"no score for {len(missing)} of {len(wanted)} papers", scores)
    return scores


def add_usage(total: dict | None, usage: dict) -> dict:
    return usage if total is None else {k: total[k] + usage[k] for k in total}


def still_to_score(cfg: dict, chunk: list[dict], prefix: str, got: dict) -> tuple[list[dict], str]:
    """The papers a retry asks for: the whole chunk, or after a partial reply only the
    papers it skipped, in a request of their own (too small to be worth caching)."""
    todo = [p for p in chunk if p["id"] not in got]
    return todo, prefix if len(todo) == len(chunk) else papers_prefix(PROMPTS[cfg["prompt"]], todo)


def usage_dict(message) -> dict:
    u = message.usage
    return {
        "input": u.input_tokens,
        "cache_write": u.cache_creation_input_tokens or 0,
        "cache_read": u.cache_read_input_tokens or 0,
        "output": u.output_tokens,
    }


def score_direct(client, cfg, jobs, prefixes, portfolios, scores_dir) -> None:
    """One request at a time, chunk by chunk, so participants reuse the cached papers block."""
    create = client.beta.messages.create if cfg.get("fallbacks") else client.messages.create
    for n, (who, k, chunk) in enumerate(jobs, 1):
        got: dict[str, int] = {}  # scores kept from partial replies
        usage, seconds = None, 0.0
        for attempt in range(1, SCORE_ATTEMPTS + 1):
            todo, prefix = still_to_score(cfg, chunk, prefixes[k], got)
            params = request_params(cfg if len(todo) == len(chunk) else {**cfg, "cache": False},
                                    prefix, portfolios[who])
            if cfg.get("fallbacks"):
                params["fallbacks"] = cfg["fallbacks"]
                params["betas"] = ["server-side-fallback-2026-07-01"]
            start = time.monotonic()
            try:
                message = create(**params)
                usage, seconds = add_usage(usage, usage_dict(message)), seconds + time.monotonic() - start
                scores = {**got, **parse_scores(message, todo)}
            except PartialScores as e:
                got.update(e.scores)
                log(f"  [{n}/{len(jobs)}] {who} chunk {k}: attempt {attempt}: {e}; asking again for those")
                continue
            except (MapHubError, anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                log(f"  [{n}/{len(jobs)}] {who} chunk {k}: attempt {attempt} failed: {e}")
                if isinstance(e, anthropic.APIStatusError) and e.status_code < 500 and e.status_code != 429:
                    raise  # a bad request or credentials problem will not fix itself
                request_failed(e)
                continue
            request_ok()
            write_json(
                scores_dir / who / f"chunk{k}.json",
                {"ids": [p["id"] for p in chunk], "scores": scores, "usage": usage,
                 "seconds": round(seconds, 1), "model": message.model, "prompt": cfg["prompt"]},
            )
            log(f"  [{n}/{len(jobs)}] {who} chunk {k}: {len(scores)} scores, {seconds:.0f} s, "
                f"tokens in {usage['input']} + cache write {usage['cache_write']} "
                f"+ cache read {usage['cache_read']}, out {usage['output']}")
            break


def score_claude_code(cfg, jobs, prefixes, portfolios, scores_dir) -> None:
    """One request at a time through Claude Code's print mode, on the signed-in seat.

    Tools are off, settings and CLAUDE.md files are not loaded (it runs in an
    empty folder), and the API key is withheld, so the request is the scoring
    prompt alone, counted against the seat's usage limits. On a failure that persists (for
    example a usage limit), it stops; a rerun resumes from the saved chunks.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    command = ["claude", "-p", "--model", cfg["model"], "--tools", "",
               "--system-prompt", CLAUDE_CODE_SYSTEM, "--output-format", "json",
               "--json-schema", SCORES_SCHEMA, "--no-session-persistence", "--setting-sources", ""]
    if cfg.get("effort"):
        command += ["--effort", cfg["effort"]]
    with tempfile.TemporaryDirectory() as empty:
        for n, (who, k, chunk) in enumerate(jobs, 1):
            got: dict[str, int] = {}  # scores kept from partial replies
            usage, seconds, cost = None, 0.0, 0.0
            for attempt in range(1, SCORE_ATTEMPTS + 1):
                todo, prefix = still_to_score(cfg, chunk, prefixes[k], got)
                prompt = PROMPTS[cfg["prompt"]]
                if prompt.get("system"):  # v2: instructions and portfolio in the system prompt
                    system = CLAUDE_CODE_SYSTEM + "\n\n" + prompt["head"] + f"<portfolio>\n{portfolios[who]}\n</portfolio>"
                    if len(system.encode()) > 120_000:  # one command-line argument is limited to 128 KiB
                        raise MapHubError(f"{who}'s portfolio is too long for prompt v2's system prompt")
                    command[command.index("--system-prompt") + 1] = system
                    text = prefix.removeprefix(prompt["head"])
                else:
                    text = prefix + f"<portfolio>\n{portfolios[who]}\n</portfolio>"
                start = time.monotonic()
                try:
                    run = subprocess.run(command, input=text, capture_output=True, text=True,
                                         cwd=empty, env=env, timeout=CLAUDE_CODE_TIMEOUT)
                    out = json.loads(run.stdout) if run.stdout.strip() else {}
                    if run.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
                        raise MapHubError(str(out.get("result") or run.stderr.strip() or
                                              f"exit {run.returncode}")[:300])
                    u = out.get("usage", {})
                    usage = add_usage(usage, {"input": u.get("input_tokens", 0),
                                              "cache_write": u.get("cache_creation_input_tokens", 0),
                                              "cache_read": u.get("cache_read_input_tokens", 0),
                                              "output": u.get("output_tokens", 0)})
                    seconds += time.monotonic() - start
                    cost += out.get("total_cost_usd") or 0
                    reply = (json.dumps(out["structured_output"]) if out.get("structured_output")
                             else out.get("result", ""))
                    scores = {**got, **scores_from_text(reply, todo)}
                except FileNotFoundError:
                    raise MapHubError("the claude command was not found; install Claude Code")
                except PartialScores as e:
                    got.update(e.scores)
                    log(f"  [{n}/{len(jobs)}] {who} chunk {k}: attempt {attempt}: {e}; asking again for those")
                    continue
                except (MapHubError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
                    log(f"  [{n}/{len(jobs)}] {who} chunk {k}: attempt {attempt} failed: {e}")
                    request_failed(e)
                    continue
                request_ok()
                write_json(
                    scores_dir / who / f"chunk{k}.json",
                    {"ids": [p["id"] for p in chunk], "scores": scores, "usage": usage,
                     "seconds": round(seconds, 1), "model": cfg["model"], "prompt": cfg["prompt"], "via": "claude-code",
                     "api_equivalent_usd": round(cost, 6) if cost else None},
                )
                log(f"  [{n}/{len(jobs)}] {who} chunk {k}: {len(scores)} scores, {seconds:.0f} s, "
                    f"tokens in {usage['input']} + cache write {usage['cache_write']} "
                    f"+ cache read {usage['cache_read']}, out {usage['output']}")
                break
            else:
                log(f"  [{n}/{len(jobs)}] {who} chunk {k}: no usable reply after {SCORE_ATTEMPTS} tries; "
                    "left for the next run")


def joint_scores_from_text(text: str, chunk: list[dict], who: list[str]) -> dict[str, dict[str, int]]:
    """Read {"id", "scores": {pseudonym: score}} objects; every paper needs every pseudonym."""
    wanted = {p["id"] for p in chunk}
    scores: dict[str, dict[str, int]] = {}
    for m in re.finditer(r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"scores"\s*:\s*(\{[^{}]*\})\s*\}', text):
        try:
            pid, row = bare_id(m.group(1)), json.loads(m.group(2))
            row = {w: min(100, max(0, round(float(row[w])))) for w in who}
        except (ValueError, KeyError, TypeError, MapHubError):
            continue
        if pid in wanted:
            scores[pid] = row
    missing = wanted - scores.keys()
    if missing:
        raise PartialScores(f"no complete scores for {len(missing)} of {len(wanted)} papers", scores)
    return scores


def score_outlines(cfg, via, chunks, portfolios, out_dir) -> None:
    """The outline pass: one request per chunk scores every participant against the
    joint outline. Saved per chunk as outline/chunk<k>.json; a rerun skips saved chunks."""
    prompt = PROMPTS[cfg["prompt"]]
    who = list(portfolios)
    joint = joint_outline(portfolios)
    schema = json.dumps({"type": "object", "properties": {"papers": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "scores": {
            "type": "object", "properties": {w: {"type": "integer"} for w in who}, "required": who}},
        "required": ["id", "scores"]}}}, "required": ["papers"]})
    client = anthropic.Anthropic() if via == "api" else None
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    system = CLAUDE_CODE_SYSTEM + ("\n\n" + prompt["outline_head"] + joint if prompt.get("system") else "")
    if len(system.encode()) > 120_000:  # one command-line argument is limited to 128 KiB
        raise MapHubError("the joint outline is too long for prompt v2's system prompt")
    command = ["claude", "-p", "--model", cfg["model"], "--tools", "",
               "--system-prompt", system, "--output-format", "json",
               "--json-schema", schema, "--no-session-persistence", "--setting-sources", ""]
    if cfg.get("effort"):
        command += ["--effort", cfg["effort"]]
    with tempfile.TemporaryDirectory() as empty:
        for k, chunk in enumerate(chunks):
            if outline_done(out_dir, k, chunk, who, cfg["prompt"]):
                continue
            got: dict[str, dict[str, int]] = {}
            usage, seconds, cost = None, 0.0, 0.0
            for attempt in range(1, SCORE_ATTEMPTS + 1):
                todo = [p for p in chunk if p["id"] not in got]
                text = (papers_prefix(prompt, todo, "outline_head").removeprefix(prompt["outline_head"])
                        if prompt.get("system") else papers_prefix(prompt, todo, "outline_head") + joint)
                start = time.monotonic()
                try:
                    if via == "api":
                        params = {"model": cfg["model"], "max_tokens": MAX_TOKENS,
                                  "messages": [{"role": "user", "content": text}]}
                        if prompt.get("system"):  # v2: the outline head and joint outline, cached
                            params["system"] = [{"type": "text", "text": prompt["outline_head"] + joint,
                                                 "cache_control": {"type": "ephemeral"}}]
                        if cfg.get("effort"):
                            params["output_config"] = {"effort": cfg["effort"]}
                        message = client.messages.create(**params)
                        usage = add_usage(usage, usage_dict(message))
                        reply = "".join(b.text for b in message.content if b.type == "text")
                    else:
                        run = subprocess.run(command, input=text, capture_output=True, text=True,
                                             cwd=empty, env=env, timeout=CLAUDE_CODE_TIMEOUT)
                        out = json.loads(run.stdout) if run.stdout.strip() else {}
                        if run.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
                            raise MapHubError(str(out.get("result") or run.stderr.strip() or
                                                  f"exit {run.returncode}")[:300])
                        u = out.get("usage", {})
                        usage = add_usage(usage, {"input": u.get("input_tokens", 0),
                                                  "cache_write": u.get("cache_creation_input_tokens", 0),
                                                  "cache_read": u.get("cache_read_input_tokens", 0),
                                                  "output": u.get("output_tokens", 0)})
                        cost += out.get("total_cost_usd") or 0
                        reply = (json.dumps(out["structured_output"]) if out.get("structured_output")
                                 else out.get("result", ""))
                    seconds += time.monotonic() - start
                    scores = {**got, **joint_scores_from_text(reply, todo, who)}
                except FileNotFoundError:
                    raise MapHubError("the claude command was not found; install Claude Code")
                except PartialScores as e:
                    got.update(e.scores)
                    log(f"  outline chunk {k}: attempt {attempt}: {e}; asking again for those")
                    continue
                except (MapHubError, anthropic.APIError, json.JSONDecodeError,
                        subprocess.TimeoutExpired) as e:
                    log(f"  outline chunk {k}: attempt {attempt} failed: {e}")
                    request_failed(e)
                    continue
                request_ok()
                write_json(out_dir / f"chunk{k}.json",
                           {"ids": [p["id"] for p in chunk], "participants": who, "scores": scores,
                            "usage": usage, "seconds": round(seconds, 1), "model": cfg["model"],
                            "prompt": cfg["prompt"], "via": via, "api_equivalent_usd": round(cost, 6) if cost else None})
                log(f"  outline chunk {k + 1}/{len(chunks)}: {len(scores)} papers x {len(who)} "
                    f"participants, {seconds:.0f} s, tokens in {usage['input']} + cache write "
                    f"{usage['cache_write']} + cache read {usage['cache_read']}, out {usage['output']}")
                break
            else:
                log(f"  outline chunk {k}: no usable reply after {SCORE_ATTEMPTS} tries; left for the next run")


def outline_done(out_dir: Path, k: int, chunk: list[dict], who: list[str], prompt: str | None = None) -> bool:
    """A saved outline chunk counts as done for the same papers and participants and, when
    `prompt` is given, the same prompt version (chunks saved before Oct 19, 2026 carry none: v1)."""
    f = out_dir / f"chunk{k}.json"
    if not f.exists():
        return False
    saved = read_json(f)
    return (saved["ids"] == [p["id"] for p in chunk] and set(saved["participants"]) >= set(who)
            and (prompt is None or saved.get("prompt", "v1") == prompt))


def score_batch(client, cfg, jobs, prefixes, portfolios, scores_dir, state: Path) -> None:
    """Submit the remaining jobs as one batch; a rerun picks up a batch already submitted."""
    by_id = {f"{who}__c{k}": (who, k, chunk) for who, k, chunk in jobs}
    for round_ in range(1, BATCH_ROUNDS + 1):
        if state.exists():
            batch_id = read_json(state)["batch_id"]
            log(f"Resuming batch {batch_id}")
        else:
            pending = [cid for cid, (who, k, _) in by_id.items()
                       if not (scores_dir / who / f"chunk{k}.json").exists()]
            if not pending:
                return
            requests = [
                Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
                    **request_params(cfg, prefixes[by_id[cid][1]], portfolios[by_id[cid][0]])))
                for cid in pending
            ]
            batch_id = client.messages.batches.create(requests=requests).id
            write_json(state, {"batch_id": batch_id})
            log(f"Round {round_}: submitted batch {batch_id} with {len(requests)} requests")
        while (batch := client.messages.batches.retrieve(batch_id)).processing_status != "ended":
            c = batch.request_counts
            log(f"  batch {batch_id}: {c.processing} processing, {c.succeeded} succeeded, "
                f"{c.errored} errored")
            time.sleep(BATCH_POLL)
        for result in client.messages.batches.results(batch_id):
            if result.custom_id not in by_id:
                continue
            who, k, chunk = by_id[result.custom_id]
            if result.result.type != "succeeded":
                log(f"  {who} chunk {k}: {result.result.type}")
                continue
            message = result.result.message
            try:
                scores = parse_scores(message, chunk)
            except MapHubError as e:
                log(f"  {who} chunk {k}: {e}")
                continue
            write_json(
                scores_dir / who / f"chunk{k}.json",
                {"ids": [p["id"] for p in chunk], "scores": scores,
                 "usage": usage_dict(message), "seconds": None, "model": message.model},
            )
        state.unlink()


# ------------------------------------------------------------------------ write


def yymm_year(yymm: str) -> int:
    return (1900 if int(yymm[:2]) >= 91 else 2000) + int(yymm[:2])  # arXiv began in 1991


def table_path(tables: Path, yymm: str, date: dt.date) -> Path:
    year = yymm_year(yymm)
    span = FIRST_YEAR + 3 * ((year - FIRST_YEAR) // 3)
    return tables / f"{span}-{span + 2}" / yymm / f"{date.isoformat()}.parquet"


def read_index(tables: Path) -> dict[str, list]:
    """index.json: for each ID-month folder, the listing dates that have a table in it.

    A day in the folder's own month is a number; days of another month are grouped
    in a nested list that starts with that month as YYMM:
    {"2609": [29, 30, [2610, 1]], "2610": [[2609, 22, 23], 2, 5, 6]}. A static
    website cannot list folders, so the viewer reads this to find the days and files.
    (Older indexes wrote other-month dates in full, "2026-10-01"; both are read.)
    """
    path = tables / "index.json"
    return read_json(path) if path.exists() else {}


def yymm_date(yymm: str | int, day: int) -> str:
    yymm = f"{int(yymm):04d}"
    return f"{yymm_year(yymm)}-{yymm[2:]}-{day:02d}"


def index_dates(tables: Path) -> dict[str, list[str]]:
    """Listing date (ISO) -> the ID-month folders holding its tables."""
    dates: dict[str, list[str]] = {}
    for yymm, entries in read_index(tables).items():
        for e in entries:
            if isinstance(e, int):
                days = [yymm_date(yymm, e)]
            elif isinstance(e, list):
                days = [yymm_date(e[0], d) for d in e[1:]]
            else:
                days = [e]
            for day in days:
                dates.setdefault(day, []).append(yymm)
    return dict(sorted(dates.items()))


def write_index(tables: Path, date: dt.date | None, yymms: list[str]) -> None:
    """Record that `date` has tables in the folders `yymms` (and in no others), and
    write index.json in its compact form. With date None, only rewrite the file."""
    folders: dict[str, set[str]] = {}
    for day, held in index_dates(tables).items():
        for yymm in held:
            folders.setdefault(yymm, set()).add(day)
    if date is not None:
        for days in folders.values():
            days.discard(date.isoformat())
        for yymm in yymms:
            folders.setdefault(yymm, set()).add(date.isoformat())
    index = {}
    for yymm in sorted((y for y in folders if folders[y]), key=lambda y: (yymm_year(y), y[2:])):
        entries, groups = [], {}
        for day in sorted(folders[yymm]):
            d = dt.date.fromisoformat(day)
            own = f"{d.year % 100:02d}{d.month:02d}"
            if own == yymm:
                entries.append((day, d.day))
            else:
                groups.setdefault(own, []).append(d.day)
        for own, days in groups.items():  # a nested list sorts by its first day
            entries.append((yymm_date(own, days[0]), [int(own), *days]))
        index[yymm] = [e for _, e in sorted(entries, key=lambda pair: pair[0])]
    # One folder per line keeps the file readable and its diffs small.
    lines = [f" {json.dumps(k)}: {json.dumps(v)}" for k, v in index.items()]
    (tables / "index.json").write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")


def scored_elsewhere(tables: Path, date: dt.date, papers) -> set[str]:
    """Papers already in another date's table: each paper is scored once only.

    A paper can only be in its own ID-month folder, so only those few files are
    read. A rebuilt (legacy or backfilled) listing places a paper held for
    moderation by submission, while the RSS feed places it by announcement, so
    the two can claim the same paper; the date written first keeps it.
    """
    months = {id_parts(p["id"])[0] for p in papers}
    seen: set[str] = set()
    for yymm in months:
        folder = table_path(tables, yymm, date).parent
        for f in folder.glob("*.parquet"):
            if f.stem != date.isoformat():
                seen.update(full_id(yymm, r) for r in pq.read_table(f, columns=["id"]).column("id").to_pylist())
    return {p["id"] for p in papers} & seen


def portfolios_repo(portfolios: Path) -> tuple[Path, bool]:
    """The git repository holding the portfolios, and whether it is their own.

    The portfolios usually live in a private repository of their own, checked out
    inside this one (and left out by .gitignore). A MapHub may instead keep them as a
    folder of the main repository, if it is fine for them to be as visible as it is."""
    top = subprocess.run(["git", "-C", str(portfolios), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        raise MapHubError(f"{portfolios} is not in a git repository")
    top_path = Path(top.stdout.strip()).resolve()
    return top_path, top_path == portfolios.resolve()


def check_portfolios(portfolios: Path) -> None:
    """A table's commit time identifies the portfolios behind it, so a run that
    writes tables must use portfolios that are committed and pushed."""
    top, own = portfolios_repo(portfolios)
    where = [] if own else ["--", str(portfolios.resolve().relative_to(top))]
    git = lambda *a: subprocess.run(["git", "-C", str(top), *a], capture_output=True, text=True)
    if git("status", "--porcelain", *where).stdout.strip():
        raise MapHubError(f"{portfolios.name} has uncommitted changes; commit and push them first")
    ahead = git("rev-list", "--count", "@{u}..HEAD", *where)
    if ahead.returncode == 0 and ahead.stdout.strip() != "0":
        raise MapHubError(f"{portfolios.name} has unpushed commits; push them first")


def write_tables(tables: Path, date: dt.date, papers, scores: dict[str, dict[str, int]]) -> list[Path]:
    """One Parquet file per ID month: an ID column (the part the folder does not
    imply: '02245', or 'astro-ph_234' for old IDs), then uint8 scores."""
    by_month: dict[str, list[str]] = {}
    for p in papers:
        yymm, row = id_parts(p["id"])
        by_month.setdefault(yymm, []).append(row)
    written = []
    for yymm, rows in sorted(by_month.items()):
        order = sorted(rows)
        columns = {"id": pa.array(order, pa.string())}
        for who in sorted(scores):
            columns[who] = pa.array([scores[who][full_id(yymm, n)] for n in order], pa.uint8())
        path = table_path(tables, yymm, date)
        if path.exists():
            # Keep the columns of participants not scored in this run, e.g. the
            # daily job's columns when the designer's own column is rescored for a past date.
            old = pq.read_table(path)
            if sorted(old.column("id").to_pylist()) != order:
                raise MapHubError(f"{path} lists different papers; not overwriting it")
            old_rows = dict(zip(old.column("id").to_pylist(), range(old.num_rows)))
            for who in old.column_names:
                if who != "id" and who not in columns:
                    values = old.column(who).to_pylist()
                    columns[who] = pa.array([values[old_rows[n]] for n in order], pa.uint8())
            columns = {"id": columns["id"], **{w: columns[w] for w in sorted(columns) if w != "id"}}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(pa.table(columns), tmp)
        os.replace(tmp, path)
        written.append(path)
    write_index(tables, date, sorted(by_month))
    write_empty_list(tables)
    return written


def datacite_lacks(ids: list[str]) -> list[str]:
    """The IDs whose arXiv DOI (10.48550/arXiv.<ID>) DataCite has no record of.

    Searched in batches like the viewer, then one by one, since DataCite's
    search index can lag behind new DOIs."""
    found: set[str] = set()
    for start in range(0, len(ids), DATACITE_BATCH):
        part = ids[start : start + DATACITE_BATCH]
        query = urllib.parse.urlencode({
            # Old IDs (astro-ph/0601234) contain a slash, which the search syntax needs escaped.
            "query": "doi:(" + " OR ".join("10.48550/arxiv." + i.replace("/", "\\/") for i in part) + ")",
            "page[size]": len(part), "fields[dois]": "titles"})
        for record in json.loads(http_get(f"{DATACITE_URL}?{query}"))["data"]:
            found.add(re.sub(r"^10\.48550/arxiv\.", "", record["id"], flags=re.I))
    lacking = []
    for pid in ids:
        if pid in found:
            continue
        req = urllib.request.Request(f"{DATACITE_URL}/10.48550/arxiv.{pid}?fields[dois]=titles",
                                     headers={"User-Agent": user_agent()})
        try:
            with urllib.request.urlopen(req, timeout=60):
                pass
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise MapHubError(f"DataCite lookup of {pid} failed: {e}") from e
            lacking.append(pid)
        except (urllib.error.URLError, TimeoutError) as e:
            raise MapHubError(f"DataCite lookup of {pid} failed: {e}") from e
    return lacking


def write_meta(tables: Path, date: dt.date, ids: list[str], lookup) -> list[Path]:
    """<date>.meta.json beside each Parquet file, for the papers DataCite lacks:
    title, first three authors, author count and primary category, keyed by the
    row's ID. `lookup` maps a list of IDs to their papers, so metadata is needed
    only for those. Written once and never changed; usually there is none. A
    DataCite failure only warns, since the table is complete without it."""
    by_month: dict[str, list[str]] = {}
    for pid in ids:
        by_month.setdefault(id_parts(pid)[0], []).append(pid)
    todo = {yymm: month for yymm, month in by_month.items()
            if not table_path(tables, yymm, date).with_suffix(".meta.json").exists()}
    if not todo:
        return []
    try:
        lacking = datacite_lacks([pid for month in todo.values() for pid in month])
    except MapHubError as e:
        log(f"Warning: {e}; no metadata fallback written (retry with --meta-only {date})")
        return []
    if not lacking:
        return []
    info = lookup(lacking)
    written = []
    for yymm, month in sorted(todo.items()):
        meta = {id_parts(pid)[1]: {"title": info[pid]["title"], "authors": info[pid]["authors"][:3],
                                   "n_authors": len(info[pid]["authors"]), "primary": info[pid]["primary"]}
                for pid in sorted(month) if pid in lacking}
        if meta:
            path = table_path(tables, yymm, date).with_suffix(".meta.json")
            write_json(path, meta)
            written.append(path)
    return written


def meta_only(args) -> int:
    """Write a date's metadata fallback from its existing table, without scoring:
    for tables written before the fallback existed, or when DataCite was down."""
    date = args.date
    yymms = index_dates(args.tables).get(date.isoformat())
    if not yymms:
        raise MapHubError(f"no table for {date}")
    ids = [full_id(yymm, row) for yymm in yymms
           for row in pq.read_table(table_path(args.tables, yymm, date)).column("id").to_pylist()]
    saved = args.work / date.isoformat() / "papers.json"
    known = {p["id"]: p for p in read_json(saved)} if saved.exists() else {}

    def lookup(wanted: list[str]) -> dict[str, dict]:
        missing = [i for i in wanted if i not in known]
        return {**known, **(fetch_metadata(missing) if missing else {})}

    paths = write_meta(args.tables, date, ids, lookup)
    log(f"{date}: " + (", ".join(f"wrote {p}" for p in paths)
                       or "nothing to write (DataCite has every paper, or the fallback exists)"))
    return 0


def write_empty_table(tables: Path, date: dt.date, columns: list[str]) -> Path:
    """A table with no rows, in the date's own ID-month folder: a past listing with
    no paper left to score (none submitted, or all scored on another date)."""
    yymm = f"{date.year % 100:02d}{date.month:02d}"
    path = table_path(tables, yymm, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    empty = {"id": pa.array([], pa.string()), **{w: pa.array([], pa.uint8()) for w in columns}}
    pq.write_table(pa.table(empty), path)
    write_index(tables, date, [yymm])
    write_empty_list(tables)
    return path


def write_empty_list(tables: Path) -> None:
    """empty.json: the listing dates whose tables hold no paper, rebuilt from the tables.
    Such a date had no announcement (a holiday), met by the morning run or filled in
    by a backfill; the viewer's calendar marks it."""
    rows: dict[str, int] = {}
    for f in tables.glob("*/*/*.parquet"):
        rows[f.stem] = rows.get(f.stem, 0) + pq.read_metadata(f).num_rows
    empty = sorted(day for day, n in rows.items() if n == 0)
    (tables / "empty.json").write_text(json.dumps(empty) + "\n", encoding="utf-8")


def publish_roster(tables: Path, portfolios: Path) -> None:
    """tables/participants.json: who is active and whose column is hidden, for the
    public viewer. The tables never change when someone pauses or leaves; the
    viewer hides the hidden columns and greys out visible ones not active."""
    status = read_json(portfolios / "participants.json")["participants"]
    write_json(tables / "participants.json", {
        "active": sorted(w for w, e in status.items() if e.get("status") in DAILY_STATUSES and not e.get("hidden")),
        "hidden": sorted(w for w, e in status.items() if e.get("hidden")),
    })


def summarize(files: list[Path]) -> None:
    """Usage of the saved requests behind this run's table (both passes, when there are
    two). Files left in the work dir by a run under another prompt version are not counted."""
    total = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    seconds = 0.0
    requests = 0
    equivalent = None
    for f in files:
        saved = read_json(f)
        requests += 1
        for key in total:
            total[key] += saved["usage"][key]
        seconds += saved["seconds"] or 0
        if saved.get("api_equivalent_usd") is not None:
            equivalent = (equivalent or 0) + saved["api_equivalent_usd"]
    log(f"Usage: {requests} requests, {seconds:.0f} s of request time; tokens in "
        f"{total['input']} + cache write {total['cache_write']} + cache read "
        f"{total['cache_read']}, out {total['output']}"
        + ("" if equivalent is None else
           f"; Claude Code requests would cost ${equivalent:.2f} on the API"))


# ------------------------------------------------------------------------- main


def write_commit_message(args, date, cfg, via, columns, n_papers, n_skipped, n_meta=0,
                         note: str | None = None, promoted: dict[str, int] | None = None) -> None:
    """Write the commit message for the table just written, to .work/commit-message.txt.

    The tables carry no provenance themselves; the commit that adds or changes a
    table records which portfolios, configuration and columns produced it, so
    `git log -- <table>` gives each table's full history. scorer/local_run.py
    commits with this message; by hand, use `git commit -F .work/commit-message.txt`.
    """
    head = subprocess.run(["git", "-C", str(args.portfolios), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip() or "unknown"
    top, own = portfolios_repo(args.portfolios)
    source = f"{args.portfolios.name} {head}" if own else f"{args.portfolios.name}/ in {top.name} {head}"
    # The code that scored the table: a table scored before a code change may be committed
    # after it (pending backfills are replayed on top of pulled commits), so it names its own.
    code = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip() or "unknown"
    edited = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "scorer", "config-log.json"],
                            capture_output=True, text=True).stdout.strip()
    lines = [f"Table for {date.isoformat()} (via {via})", "",
             f"Code: {ROOT.name} {code}" + (" with local changes" if edited else ""),
             f"Portfolios: {source}",
             f"Configuration: entry effective {cfg['effective']} ({cfg['model']}, "
             f"effort {cfg.get('effort') or 'default'}, prompt {cfg['prompt']})",
             f"Columns: {', '.join(columns)}",
             f"Papers: {n_papers}" + (f" ({n_skipped} already scored on another date, left out)"
                                      if n_skipped else "")]
    if promoted:
        lines.append(f"Full portfolio (outline score {cfg['threshold']} or more): "
                     + ", ".join(f"{w} {promoted[w]}" for w in sorted(promoted)))
    if note:
        lines.append(note)
    if n_meta:
        lines.append(f"Metadata fallback: {n_meta} papers DataCite lacks, in {date.isoformat()}.meta.json")
    path = args.work / "commit-message.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Commit message written to {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("date", nargs="?", type=dt.date.fromisoformat,
                    help="announcement date YYYY-MM-DD (default: the current listing)")
    ap.add_argument("--test", action="store_true",
                    help="score 5 papers of each listing; outputs go to the work dir")
    ap.add_argument("--batch", action="store_true", help="use the Message Batches API")
    ap.add_argument("--daily", action="store_true",
                    help="score a past date as a daily run: every participant scored daily, "
                         "status no-legacy included (default for past dates: the legacy survey, "
                         "status active only)")
    ap.add_argument("--meta-only", action="store_true",
                    help="write a date's metadata fallback (papers DataCite lacks) from its table, without scoring")
    ap.add_argument("--via", choices=["api", "claude-code"],
                    help="how to send requests (default: claude-code)")
    ap.add_argument("--model", help="with --test: try this model instead of the configured one")
    ap.add_argument("--effort", help="with --test: try this effort level ('none' to omit it)")
    ap.add_argument("--portfolios", type=Path, default=ROOT / "maphub-portfolios")
    ap.add_argument("--tables", type=Path, default=ROOT / "tables")
    ap.add_argument("--work", type=Path, default=ROOT / ".work",
                    help="saved papers and scores for resuming")
    ap.add_argument("--config", type=Path, default=ROOT / "config-log.json")
    args = ap.parse_args()
    load_env(ROOT / ".env")
    if (args.model or args.effort) and not args.test:
        ap.error("--model and --effort are only for trials with --test; "
                 "real runs follow the configuration log")
    # A listing dated before today (Eastern time) is a past date.
    past = args.date is not None and args.date < dt.datetime.now(EASTERN).date()
    legacy = past and not args.daily  # a past date is the legacy survey unless scored as a daily run
    via = args.via or "claude-code"
    if via == "claude-code" and args.batch:
        ap.error("--batch works only with --via api")

    def own_only(portfolios: dict) -> dict:
        """On the Claude Code route, only the columns listed in MAPHUB_OWN_PARTICIPANTS."""
        own = [w.strip() for w in os.environ.get("MAPHUB_OWN_PARTICIPANTS", "").split(",") if w.strip()]
        if not own:
            raise MapHubError("set MAPHUB_OWN_PARTICIPANTS in .env (comma-separated pseudonyms): "
                              "Claude Code serves only the columns listed there")
        skipped = [w for w in own if w not in portfolios]
        if skipped:
            log(f"Not serving {', '.join(skipped)}: not scored on this listing (status), or no portfolio")
        kept = {w: portfolios[w] for w in own if w in portfolios}
        if not kept:
            raise MapHubError("none of MAPHUB_OWN_PARTICIPANTS can be served")
        return kept

    if args.meta_only and args.date is None:
        ap.error("--meta-only needs a date")

    try:
        if args.meta_only:
            return meta_only(args)
        # The configuration log takes effect by run date, so a rerun of an old
        # listing uses, and is attributed to, the configuration of its run.
        cfg = load_config(args.config, dt.datetime.now(EASTERN).date())
        use_listings(cfg)
        date, papers = get_papers(args.work, args.date)
        announced = bool(papers)  # an empty current listing means no announcement
        if args.date is None and not args.test:
            # Runs are repeated in case arXiv announces late; a listing already in
            # the tables is not scored again (an explicit date forces a rescore).
            if date.isoformat() in index_dates(args.tables):
                log(f"The current listing ({date}) is already scored; nothing to do.")
                return 0
        day_dir = args.work / date.isoformat()
        dupes: set[str] = set()
        if not args.test:
            check_portfolios(args.portfolios)
            dupes = scored_elsewhere(args.tables, date, papers)
            if dupes:
                log(f"Skipping {len(dupes)} papers already scored on another date: {', '.join(sorted(dupes)[:5])}"
                    + (" ..." if len(dupes) > 5 else ""))
                papers = [p for p in papers if p["id"] not in dupes]
            if not papers and announced and not past:
                log(f"Every paper of {date} is already scored on another date; nothing to do.")
                return 0
            if not papers:
                # A listing with nothing to score still gets a table, an empty one, so that
                # every weekday has a table and backfills move on: a past listing with no
                # paper left, or a current listing arXiv did not announce (a holiday).
                columns = sorted(load_participants(args.portfolios, legacy=legacy))
                path = write_empty_table(args.tables, date, columns)
                log(f"{date}: nothing to score; wrote an empty table, {path}")
                publish_roster(args.tables, args.portfolios)
                write_commit_message(args, date, cfg, via, columns, 0, len(dupes),
                                     note=None if announced or past else
                                     "No announcement: arXiv listed no new submissions (a holiday)")
                return 0
        tables = args.tables
        if args.test:
            papers = test_sample(papers)
            trial = ""
            if args.model or args.effort:
                # A trial setting gets its own folder, so results are not mixed.
                cfg = {**cfg, "model": args.model or cfg["model"],
                       "effort": cfg.get("effort") if args.effort is None
                       else None if args.effort == "none" else args.effort}
                if args.model:
                    cfg["fallbacks"] = None  # not every model accepts them
                trial = f"-{cfg['model']}-{cfg.get('effort') or 'default'}"
            day_dir = day_dir / f"test{trial}{'-claude-code' if via == 'claude-code' else ''}"
            tables = day_dir / "tables"
        portfolios = load_participants(args.portfolios, legacy=legacy and not args.test)
        if via == "claude-code":
            portfolios = own_only(portfolios)
        cfg = {**cfg, "cache": len(portfolios) > 1}
        prompt = PROMPTS[cfg["prompt"]]
        two_pass = "outline_head" in prompt
        if two_pass and args.batch:
            raise MapHubError(f"prompt {cfg['prompt']} scores in two passes; --batch is not supported")
        chunks = make_chunks(papers, cfg["chunk_size"])
        scores_dir = day_dir / "scores"
        log(f"{date}: {len(papers)} papers in {len(chunks)} chunks, {len(portfolios)} "
            f"participants; {cfg['model']}, effort {cfg.get('effort')}, prompt {cfg['prompt']}; "
            f"via {via}" + ("" if cfg["cache"] or via != "api" or two_pass else "; no caching (one participant)"))

        def done(folder: Path, who: str, k: int, chunk: list[dict]) -> bool:
            f = folder / who / f"chunk{k}.json"  # same papers and prompt version (none saved before Oct 19: v1)
            return (f.exists() and read_json(f)["ids"] == [p["id"] for p in chunk]
                    and read_json(f).get("prompt", "v1") == cfg["prompt"])

        def run_jobs(folder: Path, jobs, prefixes, serve: dict, job_cfg: dict) -> None:
            if not jobs:
                return
            if via == "claude-code":
                score_claude_code(job_cfg, jobs, prefixes, serve, folder)
            else:
                client = anthropic.Anthropic()
                if args.batch:
                    score_batch(client, job_cfg, jobs, prefixes, serve, folder, day_dir / "batch.json")
                else:
                    score_direct(client, job_cfg, jobs, prefixes, serve, folder)

        scores: dict[str, dict[str, int]] = {who: {} for who in portfolios}
        promoted: dict[str, int] = {}
        if two_pass:
            # Pass 1: every paper against the joint outline, one request per chunk.
            outline_dir = scores_dir / "outline"
            log(f"Outline pass: {len(chunks)} requests, all participants at once")
            score_outlines(cfg, via, chunks, portfolios, outline_dir)
            left = [k for k, c in enumerate(chunks) if not outline_done(outline_dir, k, c, list(portfolios), cfg["prompt"])]
            if left:
                summarize(sorted(outline_dir.glob("chunk*.json")))
                log(f"Incomplete: {len(left)} outline requests failed; rerun to resume. No table written.")
                return 2
            coarse: dict[str, dict[str, int]] = {}
            for k in range(len(chunks)):
                coarse.update(read_json(outline_dir / f"chunk{k}.json")["scores"])
            # Pass 2: each participant's papers that scored `threshold` or more, against
            # the full portfolio. No other participant shares these papers, so no caching.
            full_dir = scores_dir / "full"
            threshold = cfg["threshold"]
            full_chunks = {}
            for who in portfolios:
                mine = [p for p in papers if coarse[p["id"]][who] >= threshold]
                promoted[who] = len(mine)
                full_chunks[who] = make_chunks(mine, cfg["chunk_size"]) if mine else []
            log(f"Full pass: outline scores of {threshold} or more, "
                + ", ".join(f"{w} {n}" for w, n in promoted.items()) + " papers")
            for who in portfolios:
                jobs = [(who, k, c) for k, c in enumerate(full_chunks[who]) if not done(full_dir, who, k, c)]
                run_jobs(full_dir, jobs, [papers_prefix(prompt, c) for c in full_chunks[who]],
                         {who: portfolios[who]}, {**cfg, "cache": False})
            summarize(sorted(outline_dir.glob("chunk*.json")) + sorted(full_dir.glob("*/chunk*.json")))
            left = [(w, k) for w in portfolios for k, c in enumerate(full_chunks[w]) if not done(full_dir, w, k, c)]
            if left:
                log(f"Incomplete: {len(left)} full-portfolio requests failed; rerun to resume. No table written.")
                return 2
            for who in portfolios:
                scores[who] = {pid: row[who] for pid, row in coarse.items()}
                for k in range(len(full_chunks[who])):
                    scores[who].update(read_json(full_dir / who / f"chunk{k}.json")["scores"])
        else:
            prefixes = [papers_prefix(prompt, c) for c in chunks]
            # Chunk-major order: every participant reads the cache the first one wrote.
            jobs = [(who, k, chunk) for k, chunk in enumerate(chunks)
                    for who in portfolios if not done(scores_dir, who, k, chunk)]
            skipped = len(chunks) * len(portfolios) - len(jobs)
            if skipped:
                log(f"Resuming: {skipped} requests already saved, {len(jobs)} to go")
            run_jobs(scores_dir, jobs, prefixes, portfolios, cfg)
            summarize([f for who in portfolios for f in sorted((scores_dir / who).glob("chunk*.json"))])
            left = [(who, k) for k, c in enumerate(chunks) for who in portfolios if not done(scores_dir, who, k, c)]
            if left:
                log(f"Incomplete: {len(left)} requests failed; rerun to resume. No table written.")
                return 2
            for who in portfolios:
                for k in range(len(chunks)):
                    scores[who].update(read_json(scores_dir / who / f"chunk{k}.json")["scores"])
        for path in write_tables(tables, date, papers, scores):
            log(f"Wrote {path}")
        meta = write_meta(tables, date, [p["id"] for p in papers],
                          lambda wanted: {p["id"]: p for p in papers})
        for path in meta:
            log(f"Wrote {path} (papers DataCite lacks)")
        publish_roster(tables, args.portfolios)
        if not args.test:
            write_commit_message(args, date, cfg, via, sorted(scores), len(papers), len(dupes),
                                 sum(len(read_json(m)) for m in meta), promoted=promoted)
        return 0
    except ScoringStopped as e:
        log(f"Stopping: {e}; the requests saved so far stay in the work dir, and a rerun resumes. No table written.")
        return 2
    except MapHubError as e:
        log(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
