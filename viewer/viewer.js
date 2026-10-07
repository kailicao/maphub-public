// MapHub viewer: one announcement day's scores as a heatmap.
//
// URLs: <base>/ shows the latest day, <base>/YYYY-MM-DD a given day. The page
// reads <base>/tables/index.json (which days exist and their Parquet files)
// and the day's Parquet files. Titles, authors and primary categories come from
// DataCite, which holds the metadata of arXiv's DOIs (10.48550/arXiv.<ID>);
// the arXiv API sends no CORS header, so a browser cannot ask it directly. For
// the rare paper whose DOI DataCite lacks, the scorer writes <date>.meta.json
// beside the day's Parquet file, read only when DataCite leaves gaps.
//
// Selecting a participant sorts papers by relevance to them and participants by
// similarity to them; selecting a paper sorts papers by similarity to it and
// participants by its relevance to them. Similarity is the Pearson correlation
// of that day's scores. The selection is kept in the URL hash (#p=… or #paper=…).

import { parquetReadObjects } from "https://cdn.jsdelivr.net/npm/hyparquet@1.31.2/+esm";

// Sequential ramps ending in Michigan Blue (#00274c), from score 0 to 100. Low
// scores fade into the page, so the few high ones stand out. In dark mode the
// ramp runs from the dark page up to a pale blue instead.
const RAMP_LIGHT = ["#f3f6fa", "#4f7cb3", "#00274c"];
const RAMP_DARK = ["#1f2733", "#3f6ba3", "#d3e3f7"];
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
// From participants.json: who is active, and whose columns are hidden. The tables keep
// every column; hiding happens here. Without the lists, everyone counts as active.
let roster = { active: null, hidden: [] };
const isActive = (who) => !roster.active || roster.active.includes(who);

// ---------------------------------------------------------------- loading

// DataCite returns some characters as HTML entities ("&gt;"); decode them as
// plain text (a textarea never runs or renders markup).
const decoder = document.createElement("textarea");
const decode = (text) => { decoder.innerHTML = text; return decoder.value; };

// "Rønnow", "Henrik M." -> "Ronnow, H M", as arXiv's listings write author searches.
function authorQuery(family, given) {
  const plain = (t) => t.normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim();
  const initials = plain(given).split(/[\s.]+/).filter(Boolean).map((part) => part[0].toUpperCase());
  return [plain(family), initials.join(" ")].filter(Boolean).join(", ");
}

// arXiv's author search within the paper's archive (astro-ph, cs, ...).
function authorUrl(query, primary) {
  const archive = (primary || "").split(".")[0];
  const params = new URLSearchParams({ searchtype: "author", query });
  return `https://arxiv.org/search/${archive ? archive + "?" : "?"}${params}`;
}

// One DataCite record -> [arXiv ID, {title, authors (first three), n_authors, primary}].
function fromDataCite(record) {
  const a = record.attributes;
  const creators = a.creators ?? [];
  const name = (c) => decode(c.givenName && c.familyName ? `${c.givenName} ${c.familyName}` : c.name ?? "");
  // The search arXiv's own listings link to: family name and initials.
  const family = (c) => decode(c.familyName ?? (c.name?.includes(",") ? c.name.split(",")[0] : ""));
  const given = (c) => decode(c.givenName ?? (c.name?.includes(",") ? c.name.split(",")[1] : ""));
  const author = (c) => ({ name: name(c), query: c.nameType === "Organizational" || !family(c) ? null : authorQuery(family(c), given(c)) });
  // arXiv lists the primary category first, as "Name (archive.SUB)".
  const subject = (a.subjects ?? []).find((s) => s.subjectScheme === "arXiv");
  return [record.id.replace(/^10\.48550\/arxiv\./i, ""), {
    title: decode(a.titles?.[0]?.title ?? ""),
    authors: creators.slice(0, 3).map(author),
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
    // Old IDs (astro-ph/0601234) contain a slash, which the search syntax needs escaped.
    query: `doi:(${part.map((id) => `10.48550/arxiv.${id.replace("/", "\\/")}`).join(" OR ")})`,
    "page[size]": String(part.length),
    "fields[dois]": DATACITE_FIELDS,
  })}`, (json) => json.data)));
  // DataCite's search index can lag behind new DOIs; ask for the rest one by one.
  await Promise.all(ids.filter((id) => !info[id]).map((id) => keep(
    `${DATACITE}/10.48550/arxiv.${id}?fields[dois]=${DATACITE_FIELDS}`, (json) => [json.data])));
  return info;
}

// Papers DataCite lacks, from the scorer's <date>.meta.json beside the table:
// {row ID: {title, authors (first three, as full names), n_authors, primary}}.
async function fetchFallback(file, yymm) {
  const info = {};
  try {
    const meta = await getOptionalJSON(TABLES + file.replace(/\.parquet$/, ".meta.json"), {});
    for (const [row, p] of Object.entries(meta)) {
      // arXiv gives full names; take the last word as the family name.
      const authors = p.authors.map((name) => {
        const words = name.trim().split(/\s+/);
        return { name, query: authorQuery(words.at(-1), words.slice(0, -1).join(" ")) };
      });
      info[fullId(yymm, row)] = { title: p.title, authors, n_authors: p.n_authors, primary: p.primary };
    }
  } catch { /* still unavailable */ }
  return info;
}

// The folder supplies the ID month: "02245" -> "2610.02245", and old IDs
// stored as archive_number: "astro-ph_234" -> "astro-ph/0601234".
function fullId(yymm, row) {
  const old = row.split("_");
  return old.length === 2 ? `${old[0]}/${yymm}${old[1]}` : `${yymm}.${row}`;
}

async function loadDay(date, files) {
  const tables = await Promise.all(files.map(async (file) => {
    const resp = await fetch(TABLES + file);
    if (!resp.ok) throw new Error(`${file}: HTTP ${resp.status}`);
    // The folder (YYMM) supplies the part of the ID the rows leave out.
    const yymm = file.split("/").at(-2);
    return { file, yymm, rows: await parquetReadObjects({ file: await resp.arrayBuffer() }) };
  }));
  const info = await fetchPaperInfo(tables.flatMap(({ yymm, rows }) => rows.map((r) => fullId(yymm, r.id))));
  // Rarely, DataCite lacks a paper; then the scorer's fallback file has it.
  const gaps = tables.filter(({ yymm, rows }) => rows.some((r) => !info[fullId(yymm, r.id)]));
  for (const extra of await Promise.all(gaps.map(({ file, yymm }) => fetchFallback(file, yymm)))) {
    for (const [id, paper] of Object.entries(extra)) info[id] ??= paper;
  }
  // A month-boundary date has two files; merge them.
  const people = [...new Set(tables.flatMap((t) => t.rows.length ? Object.keys(t.rows[0]) : []))]
    .filter((k) => k !== "id" && !roster.hidden.includes(k)).sort();
  const score = new Map(); // `${paperId}|${person}` -> 0..100
  const papers = [];
  for (const { yymm, rows } of tables) {
    for (const row of rows) {
      const id = fullId(yymm, row.id);
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

const toLinear = (v) => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
const toByte = (v) => Math.round(255 * Math.min(1, Math.max(0,
  v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055)));

// OKLab, a perceptually even color space, so equal score steps look equal.
function toOklab(hex) {
  const [r, g, b] = rgb(hex).map(toLinear);
  const [l, m, s] = [0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
    0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
    0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b].map(Math.cbrt);
  return [0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s];
}

function fromOklab([L, A, B]) {
  const [l, m, s] = [L + 0.3963377774 * A + 0.2158037573 * B,
    L - 0.1055613458 * A - 0.0638541728 * B,
    L - 0.0894841775 * A - 1.2914855480 * B].map((v) => v ** 3);
  return [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s];
}

function scoreColor(s) {
  const ramp = (isDark() ? RAMP_DARK : RAMP_LIGHT).map(toOklab);
  const t = (Math.max(0, Math.min(100, s)) / 100) * (ramp.length - 1);
  const i = Math.min(Math.floor(t), ramp.length - 2);
  const linear = fromOklab(ramp[i].map((v, k) => v + (ramp[i + 1][k] - v) * (t - i)));
  const L = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  // Dark or white text, whichever contrasts more (they tie near luminance 0.18).
  return { bg: `rgb(${linear.map(toByte).join(",")})`, fg: L > 0.179 ? "#000000" : "#ffffff" };
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
    el("th", { scope: "col", class: "id-head" }, "arXiv"),
    el("th", { scope: "col" }, "Paper"),
    paperSim ? el("th", { scope: "col", class: "sim-head", title: "Similarity to the selected paper" }, "r") : null,
    ...people.map((who) => el("th", { scope: "col", class: "who" + (selection?.kind === "p" && selection.key === who ? " selected" : "") + (isActive(who) ? "" : " inactive") },
      el("button", { type: "button", "data-person": who,
        title: isActive(who) ? `Select ${who}` : `Select ${who} (no longer active; scores up to when they paused or left)` }, who),
      personSim ? el("span", { class: "r", title: `Similarity to ${selection.key}` }, fmt(personSim.get(who))) : null)));
  table.append(el("thead", {}, head));

  const body = el("tbody");
  for (const p of papers) {
    // Each name links to arXiv's author search; the rest is plain text.
    const byline = [];
    p.authors.forEach((a, i) => {
      if (i) byline.push(", ");
      byline.push(a.query ? el("a", { class: "author", href: authorUrl(a.query, p.primary), target: "_blank", rel: "noopener",
        title: `arXiv papers by ${a.query}` }, a.name) : a.name);
    });
    if (p.n_authors > p.authors.length) byline.push(" et al.");
    if (p.primary) byline.push(`${byline.length ? " · " : ""}${p.primary}`);
    const tr = el("tr", { class: selection?.kind === "paper" && selection.key === p.id ? "selected" : "" },
      el("td", { class: "id" }, el("a", { href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener", "data-url": `https://arxiv.org/abs/${p.id}` }, p.id)),
      el("td", { class: "paper" },
        el("button", { type: "button", class: "title", "data-paper": p.id }, p.title),
        el("div", { class: "authors" },
          // On narrow screens the arXiv column is hidden and the ID shows here instead.
          el("a", { class: "id-inline", href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener", "data-url": `https://arxiv.org/abs/${p.id}` }, p.id),
          el("span", { class: "byline" }, ...byline))),
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
    const when = new Date(`${day.date}T12:00:00Z`).toLocaleDateString("en-US",
      { weekday: "long", year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });
    status.textContent = `${when}: ${day.papers.length} new papers in astro-ph and cs.AI, ` +
      `scored for ${day.people.length} participants. Select a participant or a paper title to reorder.`;
  } else {
    status.textContent = selection.kind === "p"
      ? `Papers by relevance to ${selection.key}; participants by similarity (r) to ${selection.key}.`
      : `Papers by similarity (r) to ${selection.key}; participants by its relevance to them.`;
    status.append(el("button", { type: "button", class: "clear", onclick: () => select(null) }, "Clear"));
  }
  const missing = day.papers.filter((p) => p.missing).length;
  if (missing) status.append(` ${missing} titles could not be loaded; their arXiv links still work.`);
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

// index.json lists, for each ID-month folder, the listing dates with a table in
// it: a day of the folder's own month as a number, any other date in full.
// {"2609": [29, 30, "2026-10-01"], "2610": [2, 5, 6]} -> date -> table files.
function tableFiles(index) {
  const files = {};
  for (const [yymm, entries] of Object.entries(index)) {
    const year = (Number(yymm.slice(0, 2)) >= 91 ? 1900 : 2000) + Number(yymm.slice(0, 2));
    const start = 1991 + 3 * Math.floor((year - 1991) / 3);
    for (const e of entries) {
      const date = typeof e === "number" ? `${year}-${yymm.slice(2)}-${String(e).padStart(2, "0")}` : e;
      (files[date] ??= []).push(`${start}-${start + 2}/${yymm}/${date}.parquet`);
    }
  }
  return files;
}

async function getOptionalJSON(url, fallback) {
  const resp = await fetch(url, { cache: "no-cache" });
  if (resp.status === 404) return fallback;
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function main() {
  let files, people;
  try {
    // No index means no table has been written yet.
    files = tableFiles(await getOptionalJSON(TABLES + "index.json", {}));
    people = await getOptionalJSON(TABLES + "participants.json", null);
  } catch (e) {
    return fail(`Could not load the table index (${e.message}).`);
  }
  const dates = Object.keys(files).sort();
  const date = requested ?? dates.at(-1);
  wireDates(dates, date);
  if (!date) return fail("No tables yet. The first one appears after the first daily run.");
  document.title = `MapHub ${date}`;
  if (!files[date]) {
    return fail(`No table for ${date}. arXiv makes no announcement on weekends and holidays, ` +
      "and a day's table appears only after that day's listing is scored.");
  }
  try {
    roster = { active: people?.active ?? null, hidden: people?.hidden ?? [] };
    day = await loadDay(date, files[date]);
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
