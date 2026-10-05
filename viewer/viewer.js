// MapHub viewer: one announcement day's scores as a heatmap.
//
// URLs: <base>/ shows the latest day, <base>/YYYY-MM-DD a given day. The page
// reads <base>/tables/index.json (which days exist and their Parquet files)
// and the day's Parquet files. Titles, authors and primary categories come from
// DataCite, which holds the metadata of arXiv's DOIs (10.48550/arXiv.<ID>);
// the arXiv API sends no CORS header, so a browser cannot ask it directly.
//
// Selecting a participant sorts papers by relevance to them and participants by
// similarity to them; selecting a paper sorts papers by similarity to it and
// participants by its relevance to them. Similarity is the Pearson correlation
// of that day's scores. The selection is kept in the URL hash (#p=… or #paper=…).

import { parquetReadObjects } from "https://cdn.jsdelivr.net/npm/hyparquet@1.31.2/+esm";

// Sequential blue ramp, steps 100–700 (light to dark).
const RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
  "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
const FIRST_DAY = "1991-08-14"; // arXiv's first day; earlier typed dates are ignored
const URL_DELAY = 500; // ms of hovering over an arXiv ID before its URL pops up
const DATACITE = "https://api.datacite.org/dois";
const DATACITE_FIELDS = "titles,creators,subjects";
const SEARCH_BATCH = 100; // DOIs per DataCite search, to keep URLs short

const $ = (id) => document.getElementById(id);
const match = location.pathname.match(/^(.*\/)(\d{4}-\d{2}-\d{2})\/?$/);
const BASE = match ? match[1] : location.pathname.replace(/[^/]*$/, "");
const TABLES = BASE + "tables/";
const requested = match ? match[2] : null;

let day = null;        // {date, papers: [{id, title, authors, n_authors, primary}], people: [...], score: Map}
let selection = null;  // {kind: "p" | "paper", key}

// ---------------------------------------------------------------- loading

// One DataCite record -> [arXiv ID, {title, authors (first three), n_authors, primary}].
function fromDataCite(record) {
  const a = record.attributes;
  const creators = a.creators ?? [];
  const name = (c) => (c.givenName && c.familyName ? `${c.givenName} ${c.familyName}` : c.name ?? "");
  // arXiv lists the primary category first, as "Name (archive.SUB)".
  const subject = (a.subjects ?? []).find((s) => s.subjectScheme === "arXiv");
  return [record.id.replace(/^10\.48550\/arxiv\./i, ""), {
    title: a.titles?.[0]?.title ?? "",
    authors: creators.slice(0, 3).map(name),
    n_authors: creators.length,
    primary: subject?.subject.match(/\(([^()]+)\)\s*$/)?.[1] ?? "",
  }];
}

async function fetchPaperInfo(ids) {
  const info = {};
  const keep = async (url, pick) => {
    try {
      const resp = await fetch(url);
      if (!resp.ok) return;
      for (const record of pick(await resp.json())) {
        const [id, paper] = fromDataCite(record);
        info[id] = paper;
      }
    } catch { /* missing titles are shown as unavailable */ }
  };
  const batches = [];
  for (let i = 0; i < ids.length; i += SEARCH_BATCH) batches.push(ids.slice(i, i + SEARCH_BATCH));
  await Promise.all(batches.map((part) => keep(`${DATACITE}?${new URLSearchParams({
    query: `doi:(${part.map((id) => `10.48550/arxiv.${id}`).join(" OR ")})`,
    "page[size]": String(part.length),
    "fields[dois]": DATACITE_FIELDS,
  })}`, (json) => json.data)));
  // DataCite's search index can lag behind new DOIs; ask for the rest one by one.
  await Promise.all(ids.filter((id) => !info[id]).map((id) => keep(
    `${DATACITE}/10.48550/arxiv.${id}?fields[dois]=${DATACITE_FIELDS}`, (json) => [json.data])));
  return info;
}

async function loadDay(date, files) {
  const tables = await Promise.all(files.map(async (file) => {
    const resp = await fetch(TABLES + file);
    if (!resp.ok) throw new Error(`${file}: HTTP ${resp.status}`);
    // The folder (YYMM) supplies the part of the ID the rows leave out.
    const yymm = file.split("/").at(-2);
    return { yymm, rows: await parquetReadObjects({ file: await resp.arrayBuffer() }) };
  }));
  const info = await fetchPaperInfo(tables.flatMap(({ yymm, rows }) => rows.map((r) => `${yymm}.${r.id}`)));
  // A month-boundary date has two files; merge them.
  const people = [...new Set(tables.flatMap((t) => t.rows.length ? Object.keys(t.rows[0]) : []))]
    .filter((k) => k !== "id").sort();
  const score = new Map(); // `${paperId}|${person}` -> 0..100
  const papers = [];
  for (const { yymm, rows } of tables) {
    for (const row of rows) {
      const id = `${yymm}.${row.id}`;
      papers.push({ id, missing: !info[id], ...(info[id] ?? { title: "(title unavailable)", authors: [], n_authors: 0, primary: "" }) });
      for (const who of people) {
        if (row[who] != null) score.set(`${id}|${who}`, Number(row[who]));
      }
    }
  }
  papers.sort((a, b) => a.id.localeCompare(b.id));
  return { date, papers, people, score };
}

// ---------------------------------------------------------------- similarity and order

function pearson(xs, ys) {
  const pairs = xs.map((x, i) => [x, ys[i]]).filter(([x, y]) => x != null && y != null);
  if (pairs.length < 2) return null;
  const mean = (k) => pairs.reduce((s, p) => s + p[k], 0) / pairs.length;
  const mx = mean(0), my = mean(1);
  let sxy = 0, sxx = 0, syy = 0;
  for (const [x, y] of pairs) { sxy += (x - mx) * (y - my); sxx += (x - mx) ** 2; syy += (y - my) ** 2; }
  return sxx && syy ? sxy / Math.sqrt(sxx * syy) : null;
}

const get = (paper, who) => day.score.get(`${paper}|${who}`) ?? null;
const column = (who) => day.papers.map((p) => get(p.id, who));
const row = (paper) => day.people.map((who) => get(paper, who));

// Sort descending by value; missing values last; ties by the default order.
function byDesc(items, value, tie) {
  return [...items].sort((a, b) => {
    const va = value(a), vb = value(b);
    if (va == null || vb == null) return (va == null) - (vb == null) || tie(a, b);
    return vb - va || tie(a, b);
  });
}

function arrange() {
  const byId = (a, b) => a.id.localeCompare(b.id);
  const byName = (a, b) => a.localeCompare(b);
  if (selection?.kind === "p") {
    const me = column(selection.key);
    const sim = new Map(day.people.map((who) => [who, who === selection.key ? 1 : pearson(me, column(who))]));
    return {
      papers: byDesc(day.papers, (p) => get(p.id, selection.key), byId),
      people: byDesc(day.people, (w) => (w === selection.key ? Infinity : sim.get(w)), byName),
      personSim: sim,
    };
  }
  if (selection?.kind === "paper") {
    const me = row(selection.key);
    const sim = new Map(day.papers.map((p) => [p.id, p.id === selection.key ? 1 : pearson(me, row(p.id))]));
    return {
      papers: byDesc(day.papers, (p) => (p.id === selection.key ? Infinity : sim.get(p.id)), byId),
      people: byDesc(day.people, (w) => get(selection.key, w), byName),
      paperSim: sim,
    };
  }
  return { papers: day.papers, people: day.people };
}

// ---------------------------------------------------------------- color

const isDark = () => getComputedStyle(document.documentElement).colorScheme.includes("dark");

function rgb(hex) { return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)); }

function scoreColor(s) {
  // Light mode: low scores recede toward the light surface. Dark mode: the same
  // ramp reversed, so low scores recede toward the dark surface.
  const ramp = isDark() ? [...RAMP].reverse() : RAMP;
  const t = (Math.max(0, Math.min(100, s)) / 100) * (ramp.length - 1);
  const i = Math.min(Math.floor(t), ramp.length - 2);
  const [a, b] = [rgb(ramp[i]), rgb(ramp[i + 1])];
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * (t - i)));
  const lum = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  const L = 0.2126 * lum[0] + 0.7152 * lum[1] + 0.0722 * lum[2];
  return { bg: `rgb(${c.join(",")})`, fg: L > 0.3 ? "#0b0b0b" : "#ffffff" };
}

function paintLegend() {
  const stops = Array.from({ length: 11 }, (_, i) => scoreColor(i * 10).bg);
  $("legend-bar").style.background = `linear-gradient(to right, ${stops.join(",")})`;
}

// ---------------------------------------------------------------- rendering

const fmt = (r) => (r == null ? "–" : r.toFixed(2).replace("-", "−"));

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("data-")) node.setAttribute(k, v);
    else node[k] = v;
  }
  node.append(...kids.filter((k) => k != null));
  return node;
}

function render() {
  const { papers, people, personSim, paperSim } = arrange();
  const table = $("heatmap");
  table.replaceChildren();

  const head = el("tr", {},
    el("th", { scope: "col" }, "arXiv"),
    el("th", { scope: "col" }, "Paper"),
    paperSim ? el("th", { scope: "col", class: "sim-head", title: "Similarity to the selected paper" }, "r") : null,
    ...people.map((who) => el("th", { scope: "col", class: "who" + (selection?.kind === "p" && selection.key === who ? " selected" : "") },
      el("button", { type: "button", "data-person": who, title: `Select ${who}` }, who),
      personSim ? el("span", { class: "r", title: `Similarity to ${selection.key}` }, fmt(personSim.get(who))) : null)));
  table.append(el("thead", {}, head));

  const body = el("tbody");
  for (const p of papers) {
    const authors = p.authors.join(", ") + (p.n_authors > p.authors.length ? " et al." : "");
    const tr = el("tr", { class: selection?.kind === "paper" && selection.key === p.id ? "selected" : "" },
      el("td", { class: "id" }, el("a", { href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener", "data-url": `https://arxiv.org/abs/${p.id}` }, p.id)),
      el("td", { class: "paper" },
        el("button", { type: "button", class: "title", "data-paper": p.id }, p.title),
        el("div", { class: "authors" }, [authors, p.primary].filter(Boolean).join(" · "))),
      paperSim ? el("td", { class: "sim" }, fmt(paperSim.get(p.id))) : null);
    for (const who of people) {
      const s = get(p.id, who);
      const td = el("td", { class: "cell", "data-paper": p.id, "data-who": who });
      if (s == null) { td.classList.add("missing"); td.textContent = "·"; }
      else { const c = scoreColor(s); td.style.background = c.bg; td.style.color = c.fg; td.textContent = s; }
      if (selection?.kind === "p" && selection.key === who) td.classList.add("selected-col");
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(body);

  const status = $("status");
  status.classList.remove("error");
  if (!selection) {
    status.textContent = `${day.papers.length} new papers announced ${day.date}, ${day.people.length} participants. ` +
      "Select a participant or a paper title to reorder.";
  } else {
    status.textContent = selection.kind === "p"
      ? `Papers by relevance to ${selection.key}; participants by similarity (r) to ${selection.key}.`
      : `Papers by similarity (r) to ${selection.key}; participants by its relevance to them.`;
    status.append(el("button", { type: "button", class: "clear", onclick: () => select(null) }, "Clear"));
  }
  const missing = day.papers.filter((p) => p.missing).length;
  if (missing) status.append(` ${missing} titles could not be loaded from DataCite; their arXiv links still work.`);
}

function select(next) {
  selection = next && !(selection && selection.kind === next.kind && selection.key === next.key) ? next : null;
  history.replaceState(null, "", selection ? `#${selection.kind}=${encodeURIComponent(selection.key)}` : location.pathname);
  render();
}

function readHash() {
  const m = location.hash.match(/^#(p|paper)=(.+)$/);
  if (!m) return null;
  const key = decodeURIComponent(m[2]);
  const ok = m[1] === "p" ? day.people.includes(key) : day.papers.some((p) => p.id === key);
  return ok ? { kind: m[1], key } : null;
}

// ---------------------------------------------------------------- tooltip

const tip = $("tip");
let tipTimer = 0;

function showTip(target, content) {
  tip.replaceChildren(...content);
  tip.hidden = false;
  const box = target.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const x = Math.min(Math.max(8, box.left + box.width / 2 - w / 2), innerWidth - w - 8);
  const y = box.top - h - 6 >= 8 ? box.top - h - 6 : box.bottom + 6;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

function hideTip() { clearTimeout(tipTimer); tip.hidden = true; }

function wireTable() {
  const table = $("heatmap");
  table.addEventListener("click", (e) => {
    const person = e.target.closest("[data-person]");
    const title = e.target.closest("button[data-paper]");
    if (person) select({ kind: "p", key: person.dataset.person });
    else if (title) select({ kind: "paper", key: title.dataset.paper });
  });
  table.addEventListener("mouseover", (e) => {
    const link = e.target.closest("a[data-url]");
    const cell = e.target.closest("td.cell");
    hideTip();
    if (link) {
      tipTimer = setTimeout(() => showTip(link, [link.dataset.url]), URL_DELAY);
    } else if (cell) {
      const s = get(cell.dataset.paper, cell.dataset.who);
      const paper = day.papers.find((p) => p.id === cell.dataset.paper);
      showTip(cell, [el("strong", {}, `${cell.dataset.who}: ${s ?? "no score"}`), el("br"), paper.title]);
    }
  });
  table.addEventListener("mouseleave", hideTip);
  addEventListener("scroll", hideTip, true);
}

// ---------------------------------------------------------------- dates and start-up

function wireDates(dates, current) {
  const i = dates.indexOf(current);
  const go = (d) => { location.href = BASE + d; };
  const input = $("date");
  input.min = dates[0];
  input.max = dates.at(-1);
  input.value = current ?? "";
  // The browser fires "change" on every keystroke once the date is valid (typing
  // a year passes through 0002, 0020, ...), so typed dates take effect on Enter
  // or when the field loses focus; dates picked from the calendar at once.
  let typing = false;
  const commit = () => {
    typing = false;
    const v = input.value;
    if (v && v !== current && v >= FIRST_DAY) go(v);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); else typing = true; });
  input.addEventListener("pointerdown", () => { typing = false; });
  input.addEventListener("change", () => { if (!typing) commit(); });
  input.addEventListener("blur", () => { if (typing) commit(); });
  // Prev/next step through the days that have tables.
  const prev = i > 0 ? dates[i - 1] : i < 0 ? dates.filter((d) => d < current).at(-1) : null;
  const next = i >= 0 ? dates[i + 1] : dates.find((d) => d > current);
  $("prev").disabled = !prev;
  $("next").disabled = !next;
  $("prev").onclick = () => prev && go(prev);
  $("next").onclick = () => next && go(next);
  $("home").href = $("latest").href = BASE;
}

function fail(message) {
  const status = $("status");
  status.textContent = message;
  status.classList.add("error");
  $("legend").hidden = $("scroll").hidden = true;
}

async function main() {
  let index;
  try {
    const resp = await fetch(TABLES + "index.json", { cache: "no-cache" });
    // No index means no table has been written yet.
    if (resp.status === 404) index = { dates: {} };
    else if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    else index = await resp.json();
  } catch (e) {
    return fail(`Could not load the table index (${e.message}).`);
  }
  const dates = Object.keys(index.dates).sort();
  const date = requested ?? dates.at(-1);
  wireDates(dates, date);
  if (!date) return fail("No tables yet. The first one appears after the first daily run.");
  document.title = `MapHub ${date}`;
  if (!index.dates[date]) {
    return fail(`No table for ${date}. arXiv makes no announcement on weekends and holidays, ` +
      "and a day's table appears only after that day's listing is scored.");
  }
  try {
    day = await loadDay(date, index.dates[date]);
  } catch (e) {
    return fail(`Could not load the table for ${date} (${e.message}).`);
  }
  selection = readHash();
  paintLegend();
  $("legend").hidden = $("scroll").hidden = false;
  wireTable();
  render();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { paintLegend(); render(); });
}

main();
