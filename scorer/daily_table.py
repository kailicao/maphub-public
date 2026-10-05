#!/usr/bin/env python3
"""MapHub daily table: score one announcement day's new arXiv submissions.

Design: lsap-meth/midas-arxiv-preprint-hub.md (section "Daily table").

    python scorer/daily_table.py                # the current listing
    python scorer/daily_table.py 2026-10-05     # same, but check the date first
    python scorer/daily_table.py --test         # 5 astro-ph + 5 cs.AI papers, outputs in the work dir
    python scorer/daily_table.py --batch        # Message Batches API (half price, slower)

Steps:
  1. Fetch. The arXiv API cannot query by announcement date, so the day's new
     submissions (no cross-lists, no replacements) are read from arXiv's RSS
     feed, which only shows the current listing; their metadata then comes from
     the arXiv API in one query. The papers are saved in the work dir, so a
     rerun on a later day still has them.
  2. Score. One Claude request per participant per chunk of about 50 papers.
     The instructions and papers come first and are cached across participants.
  3. Write. One Parquet file per ID month, the viewer's side files (index.json,
     papers/<date>.json) and each participant's report.
  4. Resume. Each (participant, chunk) is saved as it arrives; a rerun skips
     what is already saved. Parquet files are written only once all are done.

Participants are the folders of the portfolios repo that hold a portfolio.md;
the folder name is the participant's pseudonym and column name.

Credentials: ANTHROPIC_API_KEY from the environment (the GitHub Action passes
the repo secret) or, failing that, from the repo root's .env file, which
.gitignore keeps out of git. Exit codes: 0 done, 1 error, 2 incomplete (rerun to resume).
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
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

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
REPORT_DAYS = 5  # the report cache keeps the last 5 announcement days
REPORT_TOP = 5
REPORT_ABOVE = 90
MAX_TOKENS = 16000
SCORE_ATTEMPTS = 3  # direct mode: tries per chunk when the reply is unusable
BATCH_ROUNDS = 3  # batch mode: resubmissions of failed requests
BATCH_POLL = 60

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
    """'oai:arXiv.org:2610.02245v1' or 'http://arxiv.org/abs/2610.02245v1' -> '2610.02245'."""
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?$", raw.strip())
    if not m:
        raise MapHubError(f"unexpected arXiv ID {raw!r}")
    return m.group(1)


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
                pid = bare_id(entry.findtext("atom:id", "", NS))
                primary = entry.find("arxiv:primary_category", NS)
                papers[pid] = {
                    "id": pid,
                    "title": clean(entry.findtext("atom:title", "", NS)),
                    "authors": [
                        clean(a.findtext("atom:name", "", NS))
                        for a in entry.findall("atom:author", NS)
                    ],
                    "abstract": clean(entry.findtext("atom:summary", "", NS)),
                    "primary": primary.get("term", "") if primary is not None else "",
                }
        todo = [i for i in ids if i not in papers]
        if not todo:
            break
        log(f"  arXiv API missed {len(todo)} papers; asking again")
    if todo:
        raise MapHubError(f"arXiv API returned no metadata for {', '.join(todo)}")
    return papers


def listed_here(paper: dict) -> bool:
    return paper["primary"].startswith("astro-ph") or paper["primary"] == "cs.AI"


def get_papers(work: Path, date: dt.date | None) -> tuple[dt.date, list[dict]]:
    """Papers for `date` (default: the current listing), from the work dir if saved."""
    if date is not None:
        saved = work / date.isoformat() / "papers.json"
        if saved.exists():
            log(f"Papers for {date}: loaded from {saved}")
            return date, read_json(saved)
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


def load_participants(folder: Path) -> dict[str, str]:
    """Pseudonym -> portfolio text, for each participant folder with a portfolio.md."""
    if not folder.is_dir():
        raise MapHubError(f"portfolio folder {folder} not found")
    found = {}
    for sub in sorted(folder.iterdir()):
        portfolio = sub / "portfolio.md"
        if not portfolio.is_file():
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sub.name):
            raise MapHubError(f"pseudonym {sub.name!r} must match [A-Za-z0-9_-]{{1,40}}")
        found[sub.name] = portfolio.read_text(encoding="utf-8").strip()
    if not found:
        raise MapHubError(f"no <pseudonym>/portfolio.md under {folder}")
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
                    {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": f"<portfolio>\n{portfolio}\n</portfolio>"},
                ],
            }
        ],
    }
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
    text = "".join(b.text for b in message.content if b.type == "text")
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


def table_path(tables: Path, yymm: str, date: dt.date) -> Path:
    year = 2000 + int(yymm[:2])  # current-style IDs start in 2007
    span = FIRST_YEAR + 3 * ((year - FIRST_YEAR) // 3)
    return tables / f"{span}-{span + 2}" / yymm / f"{date.isoformat()}.parquet"


def write_tables(tables: Path, date: dt.date, papers, scores: dict[str, dict[str, int]]) -> list[Path]:
    """One Parquet file per ID month: an ID column (number after the dot), then uint8 scores."""
    by_month: dict[str, list[str]] = {}
    for p in papers:
        yymm, number = p["id"].split(".")
        by_month.setdefault(yymm, []).append(number)
    written = []
    for yymm, numbers in sorted(by_month.items()):
        order = sorted(numbers)
        columns = {"id": pa.array(order, pa.string())}
        for who in sorted(scores):
            columns[who] = pa.array([scores[who][f"{yymm}.{n}"] for n in order], pa.uint8())
        path = table_path(tables, yymm, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(pa.table(columns), tmp)
        os.replace(tmp, path)
        written.append(path)
    write_viewer_files(tables, date, papers, written)
    return written


def write_viewer_files(tables: Path, date: dt.date, papers, written: list[Path]) -> None:
    """Files the web viewer reads besides the tables.

    papers/<date>.json: titles and first three authors, since browsers cannot
    query the arXiv API (it sends no CORS header).
    index.json: each date's table files, so the viewer knows which days exist.
    """
    write_json(tables / "papers" / f"{date.isoformat()}.json", {
        p["id"]: {"title": p["title"], "authors": p["authors"][:3],
                  "n_authors": len(p["authors"]), "primary": p["primary"]}
        for p in papers
    })
    index_path = tables / "index.json"
    index = read_json(index_path) if index_path.exists() else {"dates": {}}
    index["dates"][date.isoformat()] = [path.relative_to(tables).as_posix() for path in written]
    index["dates"] = dict(sorted(index["dates"].items()))
    write_json(index_path, index)


def write_reports(reports: Path, date: dt.date, papers, scores, cfg) -> None:
    """Each participant's top 5 papers plus all above 90, ranked by score."""
    info = {p["id"]: p for p in papers}
    for who, mine in scores.items():
        ranked = sorted(mine.items(), key=lambda kv: (-kv[1], kv[0]))
        picked = [(pid, s) for i, (pid, s) in enumerate(ranked)
                  if i < REPORT_TOP or s > REPORT_ABOVE]
        lines = [f"# MapHub report for {who}, {date.isoformat()}", "",
                 f"{len(picked)} of {len(ranked)} new papers; scored by {cfg['model']}, "
                 f"prompt {cfg['prompt']}.", ""]
        for pid, s in picked:
            p = info[pid]
            authors = ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else "")
            lines += [f"## {s} · [{pid}](https://arxiv.org/abs/{pid}) · {p['primary']}", "",
                      f"**{p['title']}**", "", authors, "", p["abstract"], ""]
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
    for who in participants:
        for f in (scores_dir / who).glob("chunk*.json"):
            saved = read_json(f)
            requests += 1
            for key in total:
                total[key] += saved["usage"][key]
            seconds += saved["seconds"] or 0
    log(f"Usage: {requests} requests, {seconds:.0f} s of request time; tokens in "
        f"{total['input']} + cache write {total['cache_write']} + cache read "
        f"{total['cache_read']}, out {total['output']}")


# ------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("date", nargs="?", type=dt.date.fromisoformat,
                    help="announcement date YYYY-MM-DD (default: the current listing)")
    ap.add_argument("--test", action="store_true",
                    help="score 5 astro-ph and 5 cs.AI papers; outputs go to the work dir")
    ap.add_argument("--batch", action="store_true", help="use the Message Batches API")
    ap.add_argument("--portfolios", type=Path, default=ROOT / "maphub-portfolios")
    ap.add_argument("--tables", type=Path, default=ROOT / "tables")
    ap.add_argument("--reports", type=Path, default=ROOT / ".work" / "reports",
                    help="report cache (point at the cache branch's checkout)")
    ap.add_argument("--work", type=Path, default=ROOT / ".work",
                    help="saved papers and scores for resuming")
    ap.add_argument("--config", type=Path, default=ROOT / "config-log.json")
    args = ap.parse_args()
    load_env(ROOT / ".env")

    try:
        date, papers = get_papers(args.work, args.date)
        day_dir = args.work / date.isoformat()
        tables, reports = args.tables, args.reports
        if args.test:
            papers = test_sample(papers)
            day_dir = day_dir / "test"
            tables, reports = day_dir / "tables", day_dir / "reports"
        cfg = load_config(args.config, date)
        portfolios = load_participants(args.portfolios)
        prompt = PROMPTS[cfg["prompt"]]
        chunks = make_chunks(papers, cfg["chunk_size"])
        prefixes = [papers_prefix(prompt, c) for c in chunks]
        scores_dir = day_dir / "scores"
        log(f"{date}: {len(papers)} papers in {len(chunks)} chunks, {len(portfolios)} "
            f"participants; {cfg['model']}, effort {cfg.get('effort')}, prompt {cfg['prompt']}")

        def done(who: str, k: int) -> bool:
            f = scores_dir / who / f"chunk{k}.json"
            return f.exists() and read_json(f)["ids"] == [p["id"] for p in chunks[k]]

        # Chunk-major order: every participant reads the cache the first one wrote.
        jobs = [(who, k, chunk) for k, chunk in enumerate(chunks)
                for who in portfolios if not done(who, k)]
        skipped = len(chunks) * len(portfolios) - len(jobs)
        if skipped:
            log(f"Resuming: {skipped} requests already saved, {len(jobs)} to go")
        if jobs:
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
        write_reports(reports, date, papers, scores, cfg)
        log(f"Wrote reports to {reports}")
        return 0
    except MapHubError as e:
        log(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
