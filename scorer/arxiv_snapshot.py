"""Past listings from arXiv's metadata snapshot, instead of harvesting OAI-PMH.

Cornell publishes the metadata of every arXiv paper as one file, updated regularly: the
"arXiv Dataset" on Kaggle (Cornell-University/arxiv; a ZIP of about 1.9 GB holding
arxiv-metadata-oai-snapshot.json, 5.6 GB, as of Oct 2026). This script turns it into the OAI-PMH cache's own
monthly files, so that the legacy survey rebuilds past listings without harvesting, back to the
first listing; OAI-PMH then only tops up the weeks after the snapshot. The steps (only `merge`
needs the host computer):

    python scorer/arxiv_snapshot.py convert archive.zip OUT     # the listings' sets, by month
    python scorer/arxiv_snapshot.py check OUT TABLES            # rebuild tabled listings, compare
    python scorer/arxiv_snapshot.py merge OUT                   # on the host, the driver stopped
    python scorer/arxiv_snapshot.py missing OUT TABLES > late.json   # papers backfills missed
    python scorer/arxiv_snapshot.py listed OUT TABLES FIRST LAST > rescore.json   # tables to rescore

Instead of `merge` by hand, OUT can be left in .cache/arxiv-snapshot/ on the host: the next
backfill merges it, once. `missing` lists the papers of tabled past listings that are in no
table, for `daily_table.py --late late.json`, which scores them and adds each to its table;
`listed` gives the papers of a range of tables, for `daily_table.py --rescore rescore.json`,
which scores them again for the columns served (after a portfolio changed, say).

`convert` keeps the papers of the OAI-PMH sets of the listings in the configuration log
(physics:astro-ph, cs:cs:AI), cross-lists included as in a harvest, each written as
`oai_record` writes it, into OUT/<set>/<YYYY-MM>.jsonl by submission month, with
OUT/snapshot.json describing the snapshot. OUT can be carried to the host (a flash drive will
do: about 1 GB for astro-ph and cs.AI). `check` rebuilds every listing that has a table, by submission time as a
backfill does, and compares its papers with the table's. `merge` adds OUT to .cache/oai/: for a
month the cache already holds, the snapshot's lines go first, so the cache's own harvests win;
the cache's span then reaches back to the snapshot's earliest paper, and its latest harvest is
the later of its own and the snapshot's.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import email.utils
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import daily_table as T

SNAPSHOT_NAME = "arxiv-metadata-oai-snapshot.json"


def lines(path: Path):
    """The snapshot's lines, from the ZIP as downloaded or from the unpacked file."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".json"))
            with z.open(name) as f:
                yield from io.TextIOWrapper(f, encoding="utf-8")
    else:
        with open(path, encoding="utf-8") as f:
            yield from f


def record(item: dict) -> dict | None:
    """One snapshot entry -> the cache's record, as T.oai_record makes it from arXivRaw."""
    v1 = next((v for v in item.get("versions") or [] if v.get("version") == "v1"), None)
    if v1 is None:
        return None
    submitted = email.utils.parsedate_to_datetime(v1["created"]).astimezone(dt.timezone.utc)
    authors = re.split(r",\s*|\s+and\s+", T.clean(item.get("authors") or ""))
    return {
        "id": item["id"],
        "title": T.clean(item.get("title")),
        "authors": [a for a in authors if a],
        "abstract": T.clean(item.get("abstract")),
        "primary": ((item.get("categories") or "").split() or [""])[0],
        "submitted": submitted.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def follow_config() -> None:
    T.use_listings(T.load_config(T.ROOT / "config-log.json", dt.date.today()))


def convert(snapshot: Path, out: Path) -> None:
    follow_config()
    sets = {listing: T.oai_set(listing) for listing in T.LISTINGS}
    found: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    latest, seen = "", 0
    for line in lines(snapshot):
        item = json.loads(line)
        seen += 1
        latest = max(latest, item.get("update_date") or "")
        cats = (item.get("categories") or "").split()
        hit = [s for listing, s in sets.items() if any(T.under(c, listing) for c in cats)]
        if not hit:
            continue
        paper = record(item)
        if paper is None:
            continue
        text = json.dumps(paper, ensure_ascii=False)
        for s in hit:
            found[(s, paper["submitted"][:7])].append(text)
        if seen % 500000 == 0:
            T.log(f"  {seen} entries read")
    counts: dict[str, int] = collections.Counter()
    earliest: dict[str, str] = {}
    for (s, month), texts in sorted(found.items()):
        folder = out / s.replace(":", "_")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{month}.jsonl").write_text("\n".join(texts) + "\n", encoding="utf-8")
        counts[s] += len(texts)
        earliest[s] = min(earliest.get(s, month), month)
    T.write_json(out / "snapshot.json", {
        "source": f"{snapshot.name} (Kaggle, Cornell-University/arxiv)",
        "latest_change": latest, "entries": seen,
        "sets": {s: {"papers": counts[s], "earliest_month": earliest[s]} for s in sorted(counts)}})
    T.log(f"{seen} entries, latest change {latest}; " +
          ", ".join(f"{s}: {counts[s]} papers from {earliest[s]}" for s in sorted(counts)))


def rebuild(out: Path, date: dt.date, records: bool = False):
    """A listing's papers from OUT, as T.fetch_past_listing_oai rebuilds them from the cache."""
    start, end = T.listing_window(date)
    months = sorted({start.strftime("%Y-%m"), end.strftime("%Y-%m")})
    found: dict[str, dict] = {}
    for listing in T.LISTINGS:
        for month in months:
            path = out / T.oai_set(listing).replace(":", "_") / f"{month}.jsonl"
            if path.exists():
                for paper in T.cache_records(path):
                    found[paper["id"]] = paper
    when = lambda p: dt.datetime.fromisoformat(p["submitted"].replace("Z", "+00:00"))
    papers = [p for p in found.values() if T.listed_here(p) and start <= when(p) < end]
    return papers if records else {p["id"] for p in papers}


def tabled(tables: Path) -> dict[str, set[str]]:
    """Each tabled listing's papers, by date, with their full IDs."""
    import pyarrow.parquet as pq
    out: dict[str, set[str]] = collections.defaultdict(set)
    for f in tables.glob("*/*/*.parquet"):
        for i in pq.read_table(f, columns=["id"]).column(0).to_pylist():
            out[f.name[:10]].add(T.full_id(f.parent.name, i))
    return out


def check(out: Path, tables: Path) -> None:
    follow_config()
    latest = T.read_json(out / "snapshot.json")["latest_change"]
    same = differ = 0
    worst = []
    for date, ids in sorted(tabled(tables).items()):
        if not ids or date > latest:
            continue
        mine = rebuild(out, dt.date.fromisoformat(date))
        if mine == ids:
            same += 1
        else:
            differ += 1
            worst.append((len(mine ^ ids), date, len(ids), len(mine - ids), len(ids - mine)))
    print(f"{same + differ} listings with tables, up to the snapshot's {latest}: {same} identical, {differ} differ")
    for n, date, size, extra, missing in sorted(worst, reverse=True)[:15]:
        print(f"  {date}: {size} papers in the table; the snapshot adds {extra} and lacks {missing}")


def missing(out: Path, tables: Path) -> list[dict]:
    """Papers of the tabled past listings that are in no table at all, each with its listing
    date: what backfills missed before Oct 30, 2026, when a paper revised after the cache's
    latest harvest fell outside every harvested piece. Listings scored by the morning runs (from
    the RSS feed, not by submission time) are left out."""
    follow_config()
    by_date = tabled(tables)
    everywhere = set().union(*by_date.values())
    latest = T.read_json(out / "snapshot.json")["latest_change"]
    found = []
    for date, ids in sorted(by_date.items()):
        if not ids or date > latest or date in morning_runs(tables):
            continue
        papers = rebuild(out, dt.date.fromisoformat(date), records=True)
        found += [{**p, "listing": date} for p in papers if p["id"] not in everywhere]
    return found


def listed(out: Path, tables: Path, first: str, last: str) -> list[dict]:
    """The papers of the tables dated first to last, as they stand (a morning run's list came
    from the RSS feed, which a rebuild would not reproduce), each with its listing date and
    its metadata from OUT, for `daily_table.py --rescore`."""
    import pyarrow.parquet as pq
    wanted: dict[str, str] = {}
    for f in tables.glob("*/*/*.parquet"):
        if first <= f.name[:10] <= last:
            for i in pq.read_table(f, columns=["id"]).column(0).to_pylist():
                wanted[T.full_id(f.parent.name, i)] = f.name[:10]
    found: dict[str, dict] = {}
    for f in sorted(out.glob("*/*.jsonl")):
        for paper in T.cache_records(f):
            if paper["id"] in wanted:
                found[paper["id"]] = {**paper, "listing": wanted[paper["id"]]}
    lacking = sorted(set(wanted) - set(found))
    if lacking:
        sys.exit(f"{len(lacking)} papers are not in the snapshot: {', '.join(lacking[:10])}")
    return sorted(found.values(), key=lambda p: (p["listing"], p["id"]))


def morning_runs(tables: Path) -> set[str]:
    """Dates whose tables the morning runs wrote: from the first one on (Oct 6, 2026, the start of
    daily operation in the prototype), read from the configuration log's earliest entry."""
    first = min(e["effective"] for e in T.read_json(T.ROOT / "config-log.json"))
    return {f.name[:10] for f in tables.glob("*/*/*.parquet") if f.name[:10] >= first}


def merge(out: Path, in_run: bool = False) -> None:
    """Add OUT to the cache. By hand, the driver must be stopped; from a run (in_run, a snapshot
    left in .cache/arxiv-snapshot/), the run itself is the only one using the cache."""
    def stop(message: str) -> None:
        if in_run:
            raise T.MapHubError(message)
        sys.exit(message)

    if not in_run:
        import driver
        if driver.running():
            stop("The driver is running: stop it (`mh --stop`), wait for `mh --status` to say so, and merge again.")
    info = T.read_json(out / "snapshot.json")
    state_path = T.OAI_CACHE / "state.json"
    state = T.read_json(state_path) if state_path.exists() else {}
    if state.get("snapshot") == info["latest_change"]:
        if not in_run:
            print(f"This snapshot ({info['latest_change']}) is already merged.")
        return
    for s, about in info["sets"].items():
        span = state.get(s)
        if span and dt.date.fromisoformat(span["from"]) > dt.date.fromisoformat(info["latest_change"]) + dt.timedelta(days=1):
            stop(f"{s}: the cache begins on {span['from']}, after the snapshot's latest change "
                 f"({info['latest_change']}); a gap would remain. Use a newer snapshot.")
    T.log(f"Merging arXiv's metadata snapshot ({info['latest_change']}) into the cache")
    for s, about in info["sets"].items():
        src, dst = out / s.replace(":", "_"), T.OAI_CACHE / s.replace(":", "_")
        dst.mkdir(parents=True, exist_ok=True)
        checked = state.setdefault("checked", {}).setdefault(s, {})
        for f in sorted(src.glob("*.jsonl")):
            target = dst / f.name
            papers = {p["id"]: p for p in T.cache_records(f)}
            if target.exists():  # one line a paper; the cache's own harvests win
                papers.update((p["id"], p) for p in T.cache_records(target))
            tmp = target.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in papers.values()), encoding="utf-8")
            tmp.replace(target)
            checked.setdefault(f.stem, "snapshot")  # the monthly count check stays at the backfill's front
        span = state.get(s) or {"harvested": info["latest_change"]}
        span["from"] = min(span.get("from", "9999"), f"{about['earliest_month']}-01")
        span["harvested"] = max(span["harvested"], info["latest_change"])
        state[s] = span
        T.log(f"  {s}: {about['papers']} papers merged; the cache now covers {span['from']} to {span['harvested']}")
    state["snapshot"] = info["latest_change"]
    T.write_json(state_path, state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="step", required=True)
    c = sub.add_parser("convert", help="the snapshot (ZIP or JSON) -> the listings' sets, by month")
    c.add_argument("snapshot", type=Path)
    c.add_argument("out", type=Path)
    k = sub.add_parser("check", help="rebuild the tabled listings from OUT and compare")
    k.add_argument("out", type=Path)
    k.add_argument("tables", type=Path)
    m = sub.add_parser("merge", help="add OUT to .cache/oai/ (on the host, the driver stopped)")
    m.add_argument("out", type=Path)
    g = sub.add_parser("missing", help="papers of tabled past listings in no table, as JSON for daily_table.py --late")
    g.add_argument("out", type=Path)
    g.add_argument("tables", type=Path)
    r = sub.add_parser("listed", help="the papers of the tables dated FIRST to LAST, as JSON for daily_table.py --rescore")
    r.add_argument("out", type=Path)
    r.add_argument("tables", type=Path)
    r.add_argument("first")
    r.add_argument("last")
    a = ap.parse_args()
    if a.step == "convert":
        convert(a.snapshot, a.out)
    elif a.step == "check":
        check(a.out, a.tables)
    elif a.step == "listed":
        print(json.dumps(listed(a.out, a.tables, a.first, a.last), ensure_ascii=False, indent=1))
    elif a.step == "missing":
        print(json.dumps(missing(a.out, a.tables), ensure_ascii=False, indent=1))
    else:
        merge(a.out)


if __name__ == "__main__":
    main()
