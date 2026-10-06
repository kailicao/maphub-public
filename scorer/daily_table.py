#!/usr/bin/env python3
"""MapHub daily table: score one announcement day's new arXiv submissions.

Design: lsap-meth/midas-arxiv-preprint-hub.md (section "Daily table").

    python scorer/daily_table.py                # the current listing
    python scorer/daily_table.py 2026-10-05     # same, but check the date first
    python scorer/daily_table.py --test         # 5 astro-ph + 5 cs.AI papers, outputs in the work dir
    python scorer/daily_table.py --batch        # Message Batches API (half price, slower)
    python scorer/daily_table.py 2025-03-04     # a past date: Claude Code, listed columns only
    python scorer/daily_table.py 2026-10-05 --backfill   # a missed day, scored like a daily run

Steps:
  1. Fetch. The arXiv API cannot query by announcement date, so the current
     listing's new submissions (no cross-lists, no replacements) are read from
     arXiv's RSS feed, and their metadata then comes from the arXiv API in one
     query. A past listing is rebuilt from the API by submission time: the
     window between two 14:00 ET deadlines that the listing covers. Papers held for
     moderation are placed by submission, not announcement: they are missing
     from the listing that announced them (4 of 345 on Oct 6, 2026) and
     appear in their submission window's listing instead. The papers are saved in the work dir, so a rerun still
     has them.
  2. Score. One Claude request per participant per chunk of about 50 papers.
     The instructions and papers come first and are cached across participants.
  3. Write. One Parquet file per ID month, the viewer's index.json and each
     participant's report.
  4. Resume. Each (participant, chunk) is saved as it arrives; a rerun skips
     what is already saved. Parquet files are written only once all are done.

Participants are the folders of the portfolios repo that hold a portfolio.md;
the folder name is the participant's pseudonym and column name.

Two ways to send requests (--via):
  api          The Claude API, with ANTHROPIC_API_KEY from the environment (the
               GitHub Action passes the repo secret) or the repo root's .env
               file, which .gitignore keeps out of git. Default for the
               current listing.
  claude-code  Claude Code's print mode (claude -p), on KC's seat on the
               Avestruz Lab's Team plan. Only the participants named in
               MAPHUB_OWN_PARTICIPANTS (in .env, comma-separated) are scored:
               the columns that plan may be used for (KC's and the lab's for
               now). Default for past dates (the legacy survey).
               The API key is withheld from Claude Code, so it never bills
               the API account.

Exit codes: 0 done, 1 error, 2 incomplete (rerun to resume).
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

RSS_URL = "https://rss.arxiv.org/rss/astro-ph+cs.AI"
API_URL = "https://export.arxiv.org/api/query"
ARXIV_PAUSE = 3.0  # arXiv API terms: at most one request every 3 seconds
USER_AGENT = "MapHub/0.1 (MIDAS arXiv Preprint Hub)"

FIRST_YEAR = 1991  # table folders are 3-year spans aligned with arXiv's first year
EASTERN = ZoneInfo("America/New_York")
DEADLINE_HOUR = 14  # arXiv's daily submission deadline, 14:00 ET (unverified for older years)
LISTING_CATEGORIES = ["astro-ph", "astro-ph.CO", "astro-ph.EP", "astro-ph.GA", "astro-ph.HE",
                      "astro-ph.IM", "astro-ph.SR", "cs.AI"]
PAGE = 500  # results per arXiv API page
REPORT_DAYS = 5  # the report cache keeps the last 5 announcement days
REPORT_TOP = 5
REPORT_ABOVE = 90
MAX_TOKENS = 16000
SCORE_ATTEMPTS = 3  # direct mode: tries per chunk when the reply is unusable
BATCH_ROUNDS = 3  # batch mode: resubmissions of failed requests
BATCH_POLL = 60
CLAUDE_CODE_TIMEOUT = 900  # seconds per request through Claude Code
CLAUDE_CODE_SYSTEM = "You score arXiv papers for relevance to a research portfolio. Reply with JSON only."
REASONS_PROMPT = """\
You explain relevance scores in a daily reading report for the subject of a research portfolio: a researcher, a group or a collaboration. Each paper below was scored from 0 to 100 against the portfolio (100 = the subject wrote it or would write it; 80 = would coauthor it; 60 = has cited it or probably will; 30 = may broaden their view; 0 = outside their interests). The papers come first, then the portfolio.

For each paper, write one or two sentences saying which parts of the portfolio it connects to and why it earned its score. Be specific about the shared methods, data or questions; name the portfolio topic. If the paper is listed in the portfolio, say so. Do not restate the abstract or the score.

Return only JSON: {"reasons": [{"id": "<arXiv ID>", "reason": "<one or two sentences>"}]}.

"""
REASONS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"reasons": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["id", "reason"]}}},
    "required": ["reasons"],
})
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
# keep old versions here so earlier tables stay reproducible.
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
}


class MapHubError(Exception):
    pass


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
    """Return the configuration-log entry in effect on `date`."""
    entries = sorted(read_json(path), key=lambda e: e["effective"])
    active = [e for e in entries if dt.date.fromisoformat(e["effective"]) <= date]
    if not active:
        raise MapHubError(f"{path.name} has no entry in effect on {date}")
    cfg = active[-1]
    if cfg["prompt"] not in PROMPTS:
        raise MapHubError(f"unknown prompt version {cfg['prompt']!r}")
    if cfg.get("input", "abstract") != "abstract" or cfg.get("listing", "new") != "new":
        raise MapHubError("only input=abstract and listing=new are implemented")
    return cfg


# ------------------------------------------------------------------------ fetch


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise MapHubError(f"GET {url} failed: {e}") from e
            log(f"  GET failed ({e}); retrying")
            time.sleep(ARXIV_PAUSE * (attempt + 2))
    raise AssertionError


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


def fetch_listing() -> tuple[dt.date, list[str]]:
    """Read the current listing's date and its new submissions from the RSS feed."""
    root = ET.fromstring(http_get(RSS_URL))
    channel = root.find("channel")
    pub = channel.findtext("pubDate") if channel is not None else None
    if not pub:
        raise MapHubError("RSS feed has no pubDate")
    listing_date = email.utils.parsedate_to_datetime(pub).date()
    ids = [
        bare_id(item.findtext("guid", ""))
        for item in root.iter("item")
        if item.findtext("arxiv:announce_type", "", NS).strip() == "new"
    ]
    return listing_date, sorted(set(ids))


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


def fetch_metadata(ids: list[str]) -> dict[str, dict]:
    """Title, authors, abstract and primary category from the arXiv API."""
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
    if todo:
        raise MapHubError(f"arXiv API returned no metadata for {', '.join(todo)}")
    return papers


def listed_here(paper: dict) -> bool:
    return paper["primary"].startswith("astro-ph") or paper["primary"] == "cs.AI"


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


def fetch_past_listing(date: dt.date) -> list[dict]:
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
    for cat in LISTING_CATEGORIES:
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
    log(f"  {len(found)} papers in the window, {len(papers)} with astro-ph or cs.AI as primary")
    return papers


def get_papers(work: Path, date: dt.date | None) -> tuple[dt.date, list[dict]]:
    """Papers for `date` (default: the current listing), from the work dir if saved."""
    if date is not None:
        saved = work / date.isoformat() / "papers.json"
        if saved.exists():
            log(f"Papers for {date}: loaded from {saved}")
            return date, read_json(saved)
        if date < dt.datetime.now(EASTERN).date():
            papers = fetch_past_listing(date)
            if not papers:
                raise MapHubError(f"no new submissions found for the listing dated {date} "
                                  "(a holiday?)")
            start, end = listing_window(date)
            write_json(saved, papers)
            write_json(saved.with_name("window.json"),
                       {"source": "arXiv API by submission time", "from_utc": start.isoformat(),
                        "to_utc": end.isoformat()})
            return date, papers
    log(f"Fetching the current listing from {RSS_URL}")
    listing_date, ids = fetch_listing()
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
        raise MapHubError(f"the listing dated {listing_date} has no new submissions")
    log(f"Listing dated {listing_date}: {len(ids)} new submissions; fetching metadata")
    meta = fetch_metadata(ids)
    papers = [meta[i] for i in ids if listed_here(meta[i])]
    if len(papers) < len(ids):
        log(f"  dropped {len(ids) - len(papers)} papers whose primary category is elsewhere")
    write_json(saved, papers)
    return listing_date, papers


def test_sample(papers: list[dict], per_listing: int = 5) -> list[dict]:
    astro = [p for p in papers if p["primary"].startswith("astro-ph")][:per_listing]
    ai = [p for p in papers if p["primary"] == "cs.AI"][:per_listing]
    return sorted(astro + ai, key=lambda p: p["id"])


# ------------------------------------------------------------------------ score


def load_participants(folder: Path, legacy: bool = False) -> dict[str, str]:
    """Pseudonym -> portfolio text, for the participants to score.

    participants.json in the portfolios repo gives each participant's status:
    only "active" ones are scored, and for a past date (legacy) only those who
    opted in to the legacy survey. A portfolio folder with no entry there is
    skipped with a warning, so a new portfolio never breaks the daily run.
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
        if entry.get("status") != "active" or (legacy and not entry.get("legacy")):
            continue
        found[sub.name] = portfolio.read_text(encoding="utf-8").strip()
    for who in status:
        if not (folder / who / "portfolio.md").is_file() and status[who].get("status") == "active":
            log(f"Warning: {who} is active in participants.json but has no portfolio.md")
    if not found:
        raise MapHubError(f"no participants to score under {folder}"
                          + (" (none opted in to the legacy survey)" if legacy else ""))
    return found


def make_chunks(papers: list[dict], size: int) -> list[list[dict]]:
    """Split into the fewest chunks of at most `size` papers, as even as possible."""
    n = len(papers)
    k = max(1, math.ceil(n / size))
    return [papers[i * n // k : (i + 1) * n // k] for i in range(k)]


def papers_prefix(prompt: dict, chunk: list[dict]) -> str:
    """The cached part of the request: instructions plus papers, identical for everyone."""
    entries = [
        prompt["paper"].format(
            id=p["id"], title=p["title"], authors=", ".join(p["authors"]), abstract=p["abstract"]
        )
        for p in chunk
    ]
    return prompt["head"] + "<papers>\n" + "\n\n".join(entries) + "\n</papers>\n\n"


def request_params(cfg: dict, prefix: str, portfolio: str) -> dict:
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
        raise MapHubError(f"no score for {len(missing)} of {len(wanted)} papers")
    return scores


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
    for n, (who, k, chunk) in enumerate(jobs, 1):
        params = request_params(cfg, prefixes[k], portfolios[who])
        if cfg.get("fallbacks"):
            params["fallbacks"] = cfg["fallbacks"]
            params["betas"] = ["server-side-fallback-2026-07-01"]
            create = client.beta.messages.create
        else:
            create = client.messages.create
        for attempt in range(1, SCORE_ATTEMPTS + 1):
            start = time.monotonic()
            try:
                message = create(**params)
                scores = parse_scores(message, chunk)
            except (MapHubError, anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                log(f"  [{n}/{len(jobs)}] {who} chunk {k}: attempt {attempt} failed: {e}")
                if isinstance(e, anthropic.APIStatusError) and e.status_code < 500 and e.status_code != 429:
                    raise  # a bad request or credentials problem will not fix itself
                continue
            seconds = time.monotonic() - start
            usage = usage_dict(message)
            write_json(
                scores_dir / who / f"chunk{k}.json",
                {"ids": [p["id"] for p in chunk], "scores": scores, "usage": usage,
                 "seconds": round(seconds, 1), "model": message.model},
            )
            log(f"  [{n}/{len(jobs)}] {who} chunk {k}: {len(scores)} scores, {seconds:.0f} s, "
                f"tokens in {usage['input']} + cache write {usage['cache_write']} "
                f"+ cache read {usage['cache_read']}, out {usage['output']}")
            break


def score_claude_code(cfg, jobs, prefixes, portfolios, scores_dir) -> None:
    """One request at a time through Claude Code's print mode (KC's Team-plan seat).

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
            text = prefixes[k] + f"<portfolio>\n{portfolios[who]}\n</portfolio>"
            for attempt in range(1, SCORE_ATTEMPTS + 1):
                start = time.monotonic()
                try:
                    run = subprocess.run(command, input=text, capture_output=True, text=True,
                                         cwd=empty, env=env, timeout=CLAUDE_CODE_TIMEOUT)
                    out = json.loads(run.stdout) if run.stdout.strip() else {}
                    if run.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
                        raise MapHubError(str(out.get("result") or run.stderr.strip() or
                                              f"exit {run.returncode}")[:300])
                    reply = (json.dumps(out["structured_output"]) if out.get("structured_output")
                             else out.get("result", ""))
                    scores = scores_from_text(reply, chunk)
                except FileNotFoundError:
                    raise MapHubError("the claude command was not found; install Claude Code")
                except (MapHubError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
                    log(f"  [{n}/{len(jobs)}] {who} chunk {k}: attempt {attempt} failed: {e}")
                    continue
                seconds = time.monotonic() - start
                u = out.get("usage", {})
                usage = {"input": u.get("input_tokens", 0),
                         "cache_write": u.get("cache_creation_input_tokens", 0),
                         "cache_read": u.get("cache_read_input_tokens", 0),
                         "output": u.get("output_tokens", 0)}
                write_json(
                    scores_dir / who / f"chunk{k}.json",
                    {"ids": [p["id"] for p in chunk], "scores": scores, "usage": usage,
                     "seconds": round(seconds, 1), "model": cfg["model"], "via": "claude-code",
                     "api_equivalent_usd": out.get("total_cost_usd")},
                )
                log(f"  [{n}/{len(jobs)}] {who} chunk {k}: {len(scores)} scores, {seconds:.0f} s, "
                    f"tokens in {usage['input']} + cache write {usage['cache_write']} "
                    f"+ cache read {usage['cache_read']}, out {usage['output']}")
                break
            else:
                log("  Stopping: Claude Code keeps failing (perhaps a usage limit); "
                    "rerun later to resume.")
                return


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


# ---------------------------------------------------------------------- reasons


def report_picks(mine: dict[str, int]) -> list[tuple[str, int]]:
    """A participant's report: their top 5 papers plus all above 90, ranked by score."""
    ranked = sorted(mine.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(pid, s) for i, (pid, s) in enumerate(ranked) if i < REPORT_TOP or s > REPORT_ABOVE]


def ask_json(cfg: dict, via: str, text: str, schema: str) -> tuple[dict, dict]:
    """One request expecting a JSON object; returns (object, usage) by either route."""
    if via == "claude-code":
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        command = ["claude", "-p", "--model", cfg["model"], "--tools", "",
                   "--system-prompt", "Reply with JSON only.", "--output-format", "json",
                   "--json-schema", schema, "--no-session-persistence", "--setting-sources", ""]
        if cfg.get("effort"):
            command += ["--effort", cfg["effort"]]
        with tempfile.TemporaryDirectory() as empty:
            run = subprocess.run(command, input=text, capture_output=True, text=True,
                                 cwd=empty, env=env, timeout=CLAUDE_CODE_TIMEOUT)
        out = json.loads(run.stdout) if run.stdout.strip() else {}
        if run.returncode != 0 or out.get("is_error") or not out.get("structured_output"):
            raise MapHubError(str(out.get("result") or run.stderr.strip() or f"exit {run.returncode}")[:300])
        u = out.get("usage", {})
        return out["structured_output"], {"input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                          + u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0)}
    params = {"model": cfg["model"], "max_tokens": MAX_TOKENS,
              "messages": [{"role": "user", "content": text}]}
    if cfg.get("effort"):
        params["output_config"] = {"effort": cfg["effort"]}
    client = anthropic.Anthropic()
    if cfg.get("fallbacks"):
        message = client.beta.messages.create(**params, fallbacks=cfg["fallbacks"],
                                              betas=["server-side-fallback-2026-07-01"])
    else:
        message = client.messages.create(**params)
    if message.stop_reason in ("refusal", "max_tokens"):
        raise MapHubError(f"reply stopped: {message.stop_reason}")
    reply = "".join(b.text for b in message.content if b.type == "text")
    try:
        obj = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
    except ValueError as e:
        raise MapHubError("reply is not JSON") from e
    return obj, {"input": message.usage.input_tokens, "output": message.usage.output_tokens}


def get_reasons(cfg, via, date, info, scores, portfolios, work_dir: Path) -> dict[str, dict[str, str]]:
    """One request per participant: why each paper in their report scored as it did.

    Reasons can echo the portfolio, so they go only into the private reports,
    never into the public tables or the run log. They are saved in the work dir
    so a rerun does not ask again.
    """
    reasons = {}
    for who, mine in scores.items():
        saved = work_dir / "reasons" / f"{who}.json"
        picks = report_picks(mine)
        if saved.exists() and set(read_json(saved)) >= {pid for pid, _ in picks}:
            reasons[who] = read_json(saved)
            continue
        entries = [f"[{pid}] {info[pid]['title']} (score {s})\n{', '.join(info[pid]['authors'])}\n"
                   f"{info[pid]['abstract']}" for pid, s in picks]
        text = (REASONS_PROMPT + "<papers>\n" + "\n\n".join(entries) + "\n</papers>\n\n"
                + f"<portfolio>\n{portfolios[who]}\n</portfolio>")
        for attempt in range(1, SCORE_ATTEMPTS + 1):
            try:
                obj, usage = ask_json(cfg, via, text, REASONS_SCHEMA)
                got = {bare_id(str(r["id"])): str(r["reason"]).strip() for r in obj["reasons"]}
                break
            except (MapHubError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired,
                    anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                log(f"  reasons for {who}: attempt {attempt} failed: {str(e)[:120]}")
        else:
            log(f"  reasons for {who}: giving up; the report will have no reasons")
            continue
        write_json(saved, got)
        reasons[who] = got
        log(f"  reasons for {who}: {len(got)} papers, tokens in {usage['input']}, out {usage['output']}")
    return reasons


# ------------------------------------------------------------------------ write


def table_path(tables: Path, yymm: str, date: dt.date) -> Path:
    year = (1900 if int(yymm[:2]) >= 91 else 2000) + int(yymm[:2])  # arXiv began in 1991
    span = FIRST_YEAR + 3 * ((year - FIRST_YEAR) // 3)
    return tables / f"{span}-{span + 2}" / yymm / f"{date.isoformat()}.parquet"


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
            # daily job's columns when KC's own column is rescored for a past date.
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
    write_index(tables, date, written)
    return written


def publish_roster(tables: Path, portfolios: Path, date: dt.date) -> None:
    """Copy who is active and who is hidden into index.json for the public viewer,
    and record which maphub-portfolios commit scored this date.

    The tables are never changed when someone pauses or leaves; the viewer
    hides the columns listed as hidden and marks visible ones not listed as active.
    """
    status = read_json(portfolios / "participants.json")["participants"]
    index_path = tables / "index.json"
    index = read_json(index_path) if index_path.exists() else {"dates": {}}
    commit = subprocess.run(["git", "-C", str(portfolios), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    if commit.returncode == 0:
        # Any score can be traced to the portfolio version behind it.
        versions = index.setdefault("portfolio_versions", {})
        versions[date.isoformat()] = commit.stdout.strip()
        index["portfolio_versions"] = dict(sorted(versions.items()))
    index["participants"] = {
        "active": sorted(w for w, e in status.items() if e.get("status") == "active" and not e.get("hidden")),
        "hidden": sorted(w for w, e in status.items() if e.get("hidden")),
    }
    write_json(index_path, index)


def write_index(tables: Path, date: dt.date, written: list[Path]) -> None:
    """index.json: each date's table files, so the viewer knows which days exist."""
    index_path = tables / "index.json"
    index = read_json(index_path) if index_path.exists() else {"dates": {}}
    index["dates"][date.isoformat()] = [path.relative_to(tables).as_posix() for path in written]
    index["dates"] = dict(sorted(index["dates"].items()))
    write_json(index_path, index)


def write_reports(reports: Path, date: dt.date, papers, scores, cfg, reasons=None) -> None:
    """Each participant's top 5 papers plus all above 90, ranked by score, with the
    reason for each score when there is one."""
    info = {p["id"]: p for p in papers}
    for who, mine in scores.items():
        picked = report_picks(mine)
        why = (reasons or {}).get(who, {})
        lines = [f"# MapHub report for {who}, {date.isoformat()}", "",
                 f"{len(picked)} of {len(mine)} new papers; scored by {cfg['model']}, "
                 f"prompt {cfg['prompt']}.", ""]
        for pid, s in picked:
            p = info[pid]
            authors = ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else "")
            lines += [f"## {s} · [{pid}](https://arxiv.org/abs/{pid}) · {p['primary']}", "",
                      f"**{p['title']}**", "", authors, ""]
            if pid in why:
                lines += [f"*Why:* {why[pid]}", ""]
            lines += [p["abstract"], ""]
        path = reports / who / f"{date.isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    # Keep the last REPORT_DAYS announcement days across all participants.
    days = sorted({f.stem for f in reports.glob("*/*.md")})
    for old in days[:-REPORT_DAYS]:
        for f in reports.glob(f"*/{old}.md"):
            f.unlink()


def summarize(scores_dir: Path, participants) -> None:
    total = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    seconds = 0.0
    requests = 0
    equivalent = None
    for who in participants:
        for f in (scores_dir / who).glob("chunk*.json"):
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


def rebuild_reports(args, own_only) -> int:
    """Rebuild one date's reports from its table: no scoring, so a backfilled or
    locally scored day can join the report cache. Reasons are added with --reasons."""
    date, via = args.date, args.via or "api"
    files = read_json(args.tables / "index.json")["dates"].get(date.isoformat())
    if not files:
        raise MapHubError(f"no table for {date}")
    scores: dict[str, dict[str, int]] = {}
    for file in files:
        yymm = Path(file).parent.name
        for row in pq.read_table(args.tables / file).to_pylist():
            pid = full_id(yymm, row["id"])
            for who, value in row.items():
                if who != "id" and value is not None:
                    scores.setdefault(who, {})[pid] = value
    portfolios = load_participants(args.portfolios)
    if own_only:
        portfolios = own_only(portfolios)
    scores = {w: v for w, v in scores.items() if w in portfolios}
    saved = args.work / date.isoformat() / "papers.json"
    info = {p["id"]: p for p in read_json(saved)} if saved.exists() else {}
    missing = sorted({pid for mine in scores.values() for pid in mine} - info.keys())
    if missing:
        info.update(fetch_metadata(missing))
    cfg = load_config(args.config, date)
    log(f"{date}: rebuilding reports for {len(scores)} participants from the table"
        + (f"; reasons via {via}" if args.reasons else ""))
    reasons = (get_reasons(cfg, via, date, info, scores, portfolios, args.work / date.isoformat())
               if args.reasons else None)
    write_reports(args.reports, date, list(info.values()), scores, cfg, reasons)
    log(f"Wrote reports to {args.reports}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("date", nargs="?", type=dt.date.fromisoformat,
                    help="announcement date YYYY-MM-DD (default: the current listing)")
    ap.add_argument("--test", action="store_true",
                    help="score 5 astro-ph and 5 cs.AI papers; outputs go to the work dir")
    ap.add_argument("--batch", action="store_true", help="use the Message Batches API")
    ap.add_argument("--backfill", action="store_true",
                    help="score a past date like a daily run: all active participants, via the API, "
                         "with reports (default for past dates: the legacy survey)")
    ap.add_argument("--reasons", action="store_true",
                    help="add a reason for each paper in the reports (one extra request per participant)")
    ap.add_argument("--reports-only", action="store_true",
                    help="rebuild a date's reports from its table, without scoring")
    ap.add_argument("--via", choices=["api", "claude-code"],
                    help="how to send requests (default: claude-code for a past date, else api)")
    ap.add_argument("--model", help="with --test: try this model instead of the configured one")
    ap.add_argument("--effort", help="with --test: try this effort level ('none' to omit it)")
    ap.add_argument("--portfolios", type=Path, default=ROOT / "maphub-portfolios")
    ap.add_argument("--tables", type=Path, default=ROOT / "tables")
    ap.add_argument("--reports", type=Path, default=ROOT / ".work" / "reports",
                    help="report cache (point at the cache branch's checkout)")
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
    legacy = past and not args.backfill  # a past date is the legacy survey unless backfilled
    via = args.via or ("claude-code" if legacy else "api")
    if via == "claude-code" and args.batch:
        ap.error("--batch works only with --via api")

    if args.reports_only and args.date is None:
        ap.error("--reports-only needs a date")

    def own_only(portfolios: dict) -> dict:
        """On the Claude Code route, only the columns listed in MAPHUB_OWN_PARTICIPANTS."""
        own = [w.strip() for w in os.environ.get("MAPHUB_OWN_PARTICIPANTS", "").split(",") if w.strip()]
        if not own:
            raise MapHubError("set MAPHUB_OWN_PARTICIPANTS in .env (comma-separated pseudonyms): "
                              "Claude Code serves only the columns listed there")
        skipped = [w for w in own if w not in portfolios]
        if skipped:
            log(f"Not serving {', '.join(skipped)}: inactive, not opted in, or no portfolio")
        kept = {w: portfolios[w] for w in own if w in portfolios}
        if not kept:
            raise MapHubError("none of MAPHUB_OWN_PARTICIPANTS can be served")
        return kept

    try:
        if args.reports_only:
            return rebuild_reports(args, own_only if (args.via or "api") == "claude-code" else None)
        date, papers = get_papers(args.work, args.date)
        if args.date is None and not args.test:
            # Runs are repeated in case arXiv announces late; a listing already in
            # the tables is not scored again (an explicit date forces a rescore).
            index_path = args.tables / "index.json"
            if index_path.exists() and date.isoformat() in read_json(index_path)["dates"]:
                log(f"The current listing ({date}) is already scored; nothing to do.")
                return 0
        cfg = load_config(args.config, date)
        day_dir = args.work / date.isoformat()
        tables, reports = args.tables, args.reports
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
            tables, reports = day_dir / "tables", day_dir / "reports"
        portfolios = load_participants(args.portfolios, legacy=legacy and not args.test)
        if via == "claude-code":
            portfolios = own_only(portfolios)
        cfg = {**cfg, "cache": len(portfolios) > 1}
        prompt = PROMPTS[cfg["prompt"]]
        chunks = make_chunks(papers, cfg["chunk_size"])
        prefixes = [papers_prefix(prompt, c) for c in chunks]
        scores_dir = day_dir / "scores"
        log(f"{date}: {len(papers)} papers in {len(chunks)} chunks, {len(portfolios)} "
            f"participants; {cfg['model']}, effort {cfg.get('effort')}, prompt {cfg['prompt']}; "
            f"via {via}" + ("" if cfg["cache"] or via != "api" else "; no caching (one participant)"))

        def done(who: str, k: int) -> bool:
            f = scores_dir / who / f"chunk{k}.json"
            return f.exists() and read_json(f)["ids"] == [p["id"] for p in chunks[k]]

        # Chunk-major order: every participant reads the cache the first one wrote.
        jobs = [(who, k, chunk) for k, chunk in enumerate(chunks)
                for who in portfolios if not done(who, k)]
        skipped = len(chunks) * len(portfolios) - len(jobs)
        if skipped:
            log(f"Resuming: {skipped} requests already saved, {len(jobs)} to go")
        if jobs and via == "claude-code":
            score_claude_code(cfg, jobs, prefixes, portfolios, scores_dir)
        elif jobs:
            client = anthropic.Anthropic()
            if args.batch:
                score_batch(client, cfg, jobs, prefixes, portfolios, scores_dir,
                            day_dir / "batch.json")
            else:
                score_direct(client, cfg, jobs, prefixes, portfolios, scores_dir)

        summarize(scores_dir, portfolios)
        left = [(who, k) for k in range(len(chunks)) for who in portfolios if not done(who, k)]
        if left:
            log(f"Incomplete: {len(left)} requests failed; rerun to resume. No table written.")
            return 2

        scores = {who: {} for who in portfolios}
        for who in portfolios:
            for k in range(len(chunks)):
                scores[who].update(read_json(scores_dir / who / f"chunk{k}.json")["scores"])
        for path in write_tables(tables, date, papers, scores):
            log(f"Wrote {path}")
        publish_roster(tables, args.portfolios, date)
        if legacy and not args.test:
            log("No reports for a past date; the report cache holds recent days only.")
        else:
            info = {p["id"]: p for p in papers}
            reasons = get_reasons(cfg, via, date, info, scores, portfolios, day_dir) if args.reasons else None
            write_reports(reports, date, papers, scores, cfg, reasons)
            log(f"Wrote reports to {reports}")
        return 0
    except MapHubError as e:
        log(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
