// MapHub viewer: one announcement day's scores as a heatmap, or one
// participant's top papers over a range of days.
//
// URLs: <base>/ shows the latest day, <base>/YYYY-MM-DD a given day, and
// <base>/top#p=<who>&from=<date>&to=<date>&n=<n> the top papers. The page
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

const TOP_N = 50;      // papers shown on the top-papers page unless the URL asks for more or fewer
const TOP_MAX = 500;   // its default range is the past calendar month, up to the latest table

const $ = (id) => document.getElementById(id);
// <base>/ is the latest day, <base>/YYYY-MM-DD a given day, <base>/top the top-papers page.
const match = location.pathname.match(/^(.*\/)(\d{4}-\d{2}-\d{2}|top)\/?$/);
const BASE = match ? match[1] : location.pathname.replace(/[^/]*$/, "");
const TABLES = BASE + "tables/";
const TOP = match?.[2] === "top";
const requested = match && !TOP ? match[2] : null;

let day = null;        // {date, papers: [{id, title, authors, n_authors, primary}], people: [...], score: Map}
let selection = null;  // {kind: "p" | "paper", key}
// From participants.json: who is active, and whose columns are hidden. The tables keep
// every column; hiding happens here. Without the lists, everyone counts as active.
let roster = { active: null, hidden: [] };
const isActive = (who) => !roster.active || roster.active.includes(who);
// From config-log.json: the arXiv listings each configuration follows ("astro-ph", "cs.AI").
let configs = [];
// From empty.json: listing dates whose table holds no paper (no announcement, a holiday).
let noAnnouncement = new Set();
function listingsOn(date) {
  const entry = configs.filter((c) => c.effective <= date).at(-1) ?? configs[0];
  const names = entry?.listings ?? [];
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names.join("");
}

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

async function readTable(file) {
  const resp = await fetch(TABLES + file);
  if (!resp.ok) throw new Error(`${file}: HTTP ${resp.status}`);
  // The folder (YYMM) supplies the part of the ID the rows leave out.
  const yymm = file.split("/").at(-2);
  return { file, yymm, rows: await parquetReadObjects({ file: await resp.arrayBuffer() }) };
}

// Titles and authors for the given papers, each {id, file, yymm}: from DataCite,
// or, for the rare paper DataCite lacks, from the scorer's fallback file.
async function describe(papers) {
  const info = await fetchPaperInfo(papers.map((p) => p.id));
  const gaps = new Map(papers.filter((p) => !info[p.id]).map((p) => [p.file, p.yymm]));
  for (const extra of await Promise.all([...gaps].map(([file, yymm]) => fetchFallback(file, yymm)))) {
    for (const [id, paper] of Object.entries(extra)) info[id] ??= paper;
  }
  return info;
}

const UNAVAILABLE = { title: "(title unavailable)", authors: [], n_authors: 0, primary: "" };

async function loadDay(date, files) {
  const tables = await Promise.all(files.map(readTable));
  const info = await describe(tables.flatMap(({ file, yymm, rows }) => rows.map((r) => ({ id: fullId(yymm, r.id), file, yymm }))));
  // A month-boundary date has two files; merge them.
  const people = [...new Set(tables.flatMap((t) => t.rows.length ? Object.keys(t.rows[0]) : []))]
    .filter((k) => k !== "id" && !roster.hidden.includes(k)).sort();
  const score = new Map(); // `${paperId}|${person}` -> 0..100
  const papers = [];
  for (const { yymm, rows } of tables) {
    for (const row of rows) {
      const id = fullId(yymm, row.id);
      papers.push({ id, missing: !info[id], ...(info[id] ?? UNAVAILABLE) });
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

// Spearman's rank correlation: Pearson's r of the ranks, ties sharing their mean rank.
function spearman(xs, ys) {
  const keep = xs.map((x, i) => [x, ys[i]]).filter(([x, y]) => x != null && y != null);
  const ranks = (vs) => {
    const order = vs.map((v, i) => [v, i]).sort((a, b) => a[0] - b[0]);
    const r = new Array(vs.length);
    for (let i = 0; i < order.length;) {
      let j = i;
      while (j + 1 < order.length && order[j + 1][0] === order[i][0]) j++;
      for (let k = i; k <= j; k++) r[order[k][1]] = (i + j) / 2 + 1;
      i = j + 1;
    }
    return r;
  };
  return pearson(ranks(keep.map((p) => p[0])), ranks(keep.map((p) => p[1])));
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
  // No selection: papers by their average score over the visible columns, so
  // the papers that matter to most participants come first; participants by
  // the rank correlation of their scores with the average of the others', so
  // those most in line with the group come first.
  const mean = (scores) => {
    const present = scores.filter((s) => s != null);
    return present.length ? present.reduce((a, b) => a + b, 0) / present.length : null;
  };
  const others = (who) => day.papers.map((p) => mean(day.people.filter((w) => w !== who).map((w) => get(p.id, w))));
  const sim = new Map(day.people.map((who) => [who, spearman(column(who), others(who))]));
  return {
    papers: byDesc(day.papers, (p) => mean(row(p.id)), byId),
    people: byDesc(day.people, (w) => sim.get(w), byName),
    personSim: sim,
  };
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
    else if (k.startsWith("data-") || k.startsWith("aria-")) node.setAttribute(k, v);
    else node[k] = v;
  }
  node.append(...kids.filter((k) => k != null));
  return node;
}

// The first authors and the primary category; each name links to arXiv's author
// search, the rest is plain text.
function byline(p) {
  const parts = [];
  p.authors.forEach((a, i) => {
    if (i) parts.push(", ");
    parts.push(a.query ? el("a", { class: "author", href: authorUrl(a.query, p.primary), target: "_blank", rel: "noopener",
      title: `arXiv papers by ${a.query}` }, a.name) : a.name);
  });
  if (p.n_authors > p.authors.length) parts.push(" et al.");
  if (p.primary) parts.push(`${parts.length ? " · " : ""}${p.primary}`);
  return parts;
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
      personSim ? el("span", { class: "r", title: selection ? `Similarity to ${selection.key}`
        : "Rank correlation with the average of the other participants" }, fmt(personSim.get(who))) : null)));
  table.append(el("thead", {}, head));

  const body = el("tbody");
  for (const p of papers) {
    const tr = el("tr", { class: selection?.kind === "paper" && selection.key === p.id ? "selected" : "" },
      el("td", { class: "id" }, el("a", { href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener", "data-url": `https://arxiv.org/abs/${p.id}` }, p.id)),
      el("td", { class: "paper" },
        el("button", { type: "button", class: "title", "data-paper": p.id }, p.title),
        el("div", { class: "authors" },
          // On narrow screens the arXiv column is hidden and the ID shows here instead.
          el("a", { class: "id-inline", href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener", "data-url": `https://arxiv.org/abs/${p.id}` }, p.id),
          el("span", { class: "byline" }, ...byline(p)))),
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
    const when = longDate(day.date);
    const listings = listingsOn(day.date);
    status.textContent = `${when}: ${day.papers.length} new papers${listings ? ` in ${listings}` : ""}, ` +
      `scored for ${day.people.length} participants. Papers by average score; participants by rank correlation (ρ) ` +
      "with the average of the others. Select a participant or a paper title to reorder.";
  } else {
    status.textContent = selection.kind === "p"
      ? `Papers by relevance to ${selection.key}; participants by similarity (r) to ${selection.key}.`
      : `Papers by similarity (r) to ${selection.key}; participants by its relevance to them.`;
    status.append(el("button", { type: "button", class: "clear", onclick: () => select(null) }, "Clear"));
    if (selection.kind === "p") {
      status.append(el("a", { class: "top-link", href: `${BASE}top#${topHash({ who: selection.key, to: day.date })}` },
        `Top papers for ${selection.key} up to this day`));
    }
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
  // Moving to another day keeps a selected participant (papers belong to one day).
  // On a day without a table there is no selection, but the URL may still carry one.
  const keep = () => (selection?.kind === "p" ? `#p=${encodeURIComponent(selection.key)}`
    : selection ? "" : location.hash.match(/^#p=[^&]+$/)?.[0] ?? "");
  const go = (d) => { location.href = BASE + d + keep(); };
  const input = $("date");
  input.value = current ?? "";
  // A typed date takes effect on Enter or when the field loses focus.
  const valid = (v) => /^\d{4}-\d{2}-\d{2}$/.test(v) && !isNaN(Date.parse(`${v}T12:00:00Z`)) &&
    new Date(`${v}T12:00:00Z`).toISOString().startsWith(v);
  const commit = () => {
    const v = input.value.trim();
    if (v === current || !v) return;
    if (valid(v) && v >= FIRST_DAY) go(v);
    else input.value = current ?? "";
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); });
  input.addEventListener("blur", commit);
  wireCalendar(dates, current, go);
  // Prev/next step through the days that have tables.
  const prev = i > 0 ? dates[i - 1] : i < 0 ? dates.filter((d) => d < current).at(-1) : null;
  const next = i >= 0 ? dates[i + 1] : dates.find((d) => d > current);
  $("prev").disabled = !prev;
  $("next").disabled = !next;
  $("prev").onclick = () => prev && go(prev);
  $("next").onclick = () => next && go(next);
  $("home").href = $("latest").href = BASE;
  $("top").href = BASE + "top";
  // "Latest" keeps the selection too; the MapHub title starts afresh.
  // Set just before the link is followed, so new-tab clicks keep it as well.
  for (const ev of ["pointerdown", "focus"]) $("latest").addEventListener(ev, () => { $("latest").href = BASE + keep(); });
  // "Top papers" takes a selected participant along.
  for (const ev of ["pointerdown", "focus"]) {
    $("top").addEventListener(ev, () => {
      const who = selection?.kind === "p" ? selection.key : null;
      $("top").href = BASE + "top" + (who ? `#${topHash({ who })}` : "");
    });
  }
}

// The calendar shows one month: days with a table are buttons (sand-colored when the
// listing had no announcement), other days plain text (muted on weekdays, faint on
// weekends); the shown day is outlined.
function wireCalendar(dates, current, go) {
  const box = $("calendar"), button = $("cal");
  const have = new Set(dates);
  const months = (d) => d.slice(0, 7);
  const first = dates.length ? months(dates[0]) : null, last = dates.length ? months(dates.at(-1)) : null;
  let shown = months(current ?? dates.at(-1) ?? new Date().toISOString());
  const shift = (ym, n) => {
    const [y, m] = ym.split("-").map(Number);
    const d = new Date(Date.UTC(y, m - 1 + n, 1));
    return d.toISOString().slice(0, 7);
  };
  const draw = () => {
    const [y, m] = shown.split("-").map(Number);
    const title = new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
    const back = el("button", { type: "button", "aria-label": "Previous month", onclick: () => { shown = shift(shown, -1); draw(); } }, "‹");
    const fwd = el("button", { type: "button", "aria-label": "Next month", onclick: () => { shown = shift(shown, 1); draw(); } }, "›");
    back.disabled = !first || shown <= first;
    fwd.disabled = !last || shown >= last;
    const grid = el("div", { class: "cal-grid" }, ...["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].map((d) => el("span", { class: "cal-dow" }, d)));
    const lead = new Date(Date.UTC(y, m - 1, 1)).getUTCDay();
    for (let k = 0; k < lead; k++) grid.append(el("span"));
    const n = new Date(Date.UTC(y, m, 0)).getUTCDate();
    for (let dd = 1; dd <= n; dd++) {
      const iso = `${shown}-${String(dd).padStart(2, "0")}`;
      const weekend = [0, 6].includes(new Date(`${iso}T12:00:00Z`).getUTCDay());
      const cls = "cal-day" + (weekend ? " weekend" : "") + (iso === current ? " current" : "");
      const none = noAnnouncement.has(iso);
      grid.append(have.has(iso)
        ? el("button", { type: "button", class: cls + " has" + (none ? " none" : ""),
            title: none ? `${iso}: no announcement (a holiday)` : `Table for ${iso}`, onclick: () => go(iso) }, String(dd))
        : el("span", { class: cls, title: weekend ? "Weekend: no listing" : "No table" }, String(dd)));
    }
    box.replaceChildren(el("div", { class: "cal-head" }, back, el("span", {}, title), fwd), grid,
      el("div", { class: "cal-legend" }, "Highlighted days have a table; sand-colored days had no announcement (a holiday)."));
  };
  const open = (yes) => {
    box.hidden = !yes;
    button.setAttribute("aria-expanded", String(yes));
    if (yes) { shown = months(current ?? dates.at(-1) ?? shown); draw(); }
  };
  button.onclick = (e) => { e.stopPropagation(); open(box.hidden); };
  box.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => open(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
}

// ---------------------------------------------------------------- top papers

// One participant's highest-scored papers over a range of listing dates, with
// the scores of any other participants chosen for comparison. Only the papers
// shown get titles from DataCite, so a long range stays quick.

function topHash({ who, also, from, to, n }) {
  const params = new URLSearchParams();
  if (who) params.set("p", who);
  if (also?.length) params.set("also", also.join(","));
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (n) params.set("n", String(n));
  return params.toString();
}

function readTopHash(dates) {
  const params = new URLSearchParams(location.hash.slice(1));
  const isDate = (v) => /^\d{4}-\d{2}-\d{2}$/.test(v ?? "");
  const to = isDate(params.get("to")) ? params.get("to") : dates.at(-1);
  // By default, the past calendar month: from the same day a month before `to`
  // (the month's last day when that day does not exist, as March 31 -> February 28).
  const monthBefore = (d) => {
    const [y, m, dd] = d.split("-").map(Number);
    const last = new Date(Date.UTC(y, m - 1, 0)).getUTCDate();
    return new Date(Date.UTC(y, m - 2, Math.min(dd, last))).toISOString().slice(0, 10);
  };
  const from = isDate(params.get("from")) ? params.get("from") : monthBefore(to);
  const n = Math.min(TOP_MAX, Math.max(1, parseInt(params.get("n"), 10) || TOP_N));
  const also = (params.get("also") ?? "").split(",").filter(Boolean);
  return { who: params.get("p"), also, from, to, n };
}

// Every visible participant who has a column in the latest table, active ones first.
async function topPeople(files, dates) {
  const latest = await Promise.all(files[dates.at(-1)].map(readTable));
  const columns = [...new Set(latest.flatMap((t) => t.rows.length ? Object.keys(t.rows[0]) : []))]
    .filter((k) => k !== "id" && !roster.hidden.includes(k));
  return columns.sort((a, b) => (isActive(b) - isActive(a)) || a.localeCompare(b));
}

async function mainTop(files, dates) {
  document.title = "MapHub: top papers";
  let people;
  try {
    people = await topPeople(files, dates);
  } catch (e) {
    return fail(`Could not load the latest table (${e.message}).`);
  }
  const query = readTopHash(dates);
  if (!people.includes(query.who)) query.who = null;
  query.also = query.also.filter((w) => people.includes(w) && w !== query.who);
  selection = query.who ? { kind: "p", key: query.who } : null; // so the date arrows keep it

  // The form: participant, first and last listing dates, number of papers.
  const option = (who) => el("option", { value: who, selected: who === query.who },
    isActive(who) ? who : `${who} (inactive)`);
  const pick = el("select", { id: "top-who", "aria-label": "Participant" },
    el("option", { value: "", disabled: true, selected: !query.who }, "Choose a participant"), ...people.map(option));
  const dateBox = (id, value, label) => el("input", { id, type: "date", value, min: dates[0], max: dates.at(-1), "aria-label": label });
  const from = dateBox("top-from", query.from, "First listing date");
  const to = dateBox("top-to", query.to, "Last listing date");
  const count = el("input", { id: "top-n", type: "number", min: 1, max: TOP_MAX, value: query.n, "aria-label": "Number of papers" });
  // Other participants whose scores are shown beside, for the same papers.
  const others = people.map((who) => el("label", { class: "also-who" },
    el("input", { type: "checkbox", value: who, checked: query.also.includes(who) }), ` ${who}`));
  const alsoBox = el("fieldset", { class: "also" }, el("legend", {}, "Also show"), ...others);
  const syncAlso = () => others.forEach((l) => {
    const box = l.querySelector("input");
    box.disabled = box.value === pick.value;
    l.hidden = box.disabled;
  });
  pick.addEventListener("change", syncAlso);
  syncAlso();
  const form = el("form", { class: "top-form" },
    el("label", {}, "Participant ", pick),
    el("label", {}, "From ", from),
    el("label", {}, "to ", to),
    el("label", {}, "Top ", count),
    alsoBox,
    el("button", { type: "submit" }, "Show"));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    let [f, t] = [from.value, to.value];
    if (f && t && f > t) [f, t] = [t, f];
    const also = others.map((l) => l.querySelector("input")).filter((b) => b.checked && !b.disabled).map((b) => b.value);
    location.hash = topHash({ who: pick.value, also, from: f, to: t, n: count.value });
  });
  addEventListener("hashchange", () => location.reload());
  $("status").before(form);

  if (!query.who) {
    $("status").textContent = "Choose a participant to see their highest-scored papers over a range of listing dates.";
    return;
  }
  const range = dates.filter((d) => d >= query.from && d <= query.to);
  if (!range.length) return fail(`No tables between ${query.from} and ${query.to}.`);

  // Complete years have small summaries (tables/top/<participant>/<year>.json: each day's
  // count of scored papers and its scores of 30 or more). From them, the score of the Nth
  // paper among the summarized days is a floor for the cutoff, so only the days that have a
  // score at or above it are read, with every day of a year that has no summary yet.
  const summary = {};
  for (const year of new Set(range.map((d) => d.slice(0, 4)))) {
    // A summary that cannot be read only costs speed: its days are read in full.
    Object.assign(summary, await getOptionalJSON(`${TABLES}top/${encodeURIComponent(query.who)}/${year}.json`, {}).catch(() => ({})));
  }
  const known = range.filter((d) => summary[d]);
  const floor = known.flatMap((d) => summary[d].s).sort((a, b) => b - a)[query.n - 1];
  const toRead = range.filter((d) => !summary[d] || (floor !== undefined ? summary[d].s[0] >= floor : true));
  const reading = new Set(toRead);
  const countedElsewhere = known.filter((d) => !reading.has(d)).reduce((t, d) => t + summary[d].n, 0);

  // Read each day's table, a few days at a time, keeping the participant's
  // scores and those of the participants shown beside.
  const scored = []; // {id, file, yymm, date, score, others: {who: score}}
  let done = 0;
  const status = $("status");
  const progress = () => {
    status.textContent = `Reading ${done} of ${toRead.length} days` + (toRead.length < range.length ? ` (of ${range.length} in the range; the others cannot reach the top ${query.n})…` : "…");
  };
  progress();
  try {
    const queue = toRead.flatMap((date) => files[date].map((file) => ({ date, file })));
    const left = Object.fromEntries(toRead.map((date) => [date, files[date].length])); // files still to read, per date
    const worker = async () => {
      for (let job; (job = queue.shift());) {
        const { yymm, rows } = await readTable(job.file);
        for (const row of rows) {
          if (row[query.who] != null) {
            const others = Object.fromEntries(query.also.map((w) => [w, row[w] == null ? null : Number(row[w])]));
            scored.push({ id: fullId(yymm, row.id), file: job.file, yymm, date: job.date, score: Number(row[query.who]), others });
          }
        }
        if (--left[job.date] === 0) { done++; progress(); } // a date counts once, when its last file is read
      }
    };
    await Promise.all(Array.from({ length: 6 }, worker));
  } catch (e) {
    return fail(`Could not load the tables (${e.message}).`);
  }
  // Highest score first; ties: the newer listing, then the arXiv ID.
  scored.sort((a, b) => b.score - a.score || b.date.localeCompare(a.date) || a.id.localeCompare(b.id));
  const top = scored.slice(0, query.n);
  status.textContent = `Fetching titles for ${top.length} papers…`;
  const info = await describe(top);
  const shown = top.map((p) => ({ ...p, missing: !info[p.id], ...(info[p.id] ?? UNAVAILABLE) }));
  const total = scored.length + countedElsewhere;
  renderTop(query, range, total, shown);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderTop(query, range, total, shown));
}

function renderTop(query, range, total, papers) {
  const short = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
  const table = $("heatmap");
  table.classList.add("top");
  table.replaceChildren(el("thead", {}, el("tr", {},
    el("th", { scope: "col", class: "rank" }, "#"),
    el("th", { scope: "col", class: "id-head" }, "arXiv"),
    el("th", { scope: "col" }, "Paper"),
    el("th", { scope: "col", class: "when" }, "Listing"),
    el("th", { scope: "col", class: "who selected" }, el("span", {}, query.who)),
    ...query.also.map((w) => el("th", { scope: "col", class: "who" + (isActive(w) ? "" : " inactive"), title: `${w}'s scores for the same papers` }, el("span", {}, w))))));
  const body = el("tbody");
  papers.forEach((p, i) => {
    const c = scoreColor(p.score);
    body.append(el("tr", {},
      el("td", { class: "rank" }, String(i + 1)),
      el("td", { class: "id" }, el("a", { href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener", "data-url": `https://arxiv.org/abs/${p.id}` }, p.id)),
      el("td", { class: "paper" },
        el("span", { class: "title" }, p.title),
        el("div", { class: "authors" },
          el("a", { class: "id-inline", href: `https://arxiv.org/abs/${p.id}`, target: "_blank", rel: "noopener" }, p.id),
          el("span", { class: "byline" }, ...byline(p)))),
      // The listing date opens that day's heatmap, sorted for the participant.
      el("td", { class: "when" }, el("a", { href: `${BASE}${p.date}#p=${encodeURIComponent(query.who)}`, title: `The heatmap for ${p.date}` }, short(p.date))),
      el("td", { class: "cell", style: `background:${c.bg};color:${c.fg}` }, String(p.score)),
      ...query.also.map((w) => {
        const s = p.others[w];
        if (s == null) return el("td", { class: "cell missing", title: `${w}: no score` }, "·");
        const k = scoreColor(s);
        return el("td", { class: "cell", style: `background:${k.bg};color:${k.fg}`, title: `${w}: ${s}` }, String(s));
      })));
  });
  table.append(body);
  const status = $("status");
  status.classList.remove("error");
  status.textContent = `The ${papers.length} papers most relevant to ${query.who} among ${total} scored for them ` +
    `in ${range.length} listings, ${short(range[0])} to ${short(range.at(-1))}; ties go to the newer listing. ` +
    (query.also.length ? `Beside it, the scores of ${query.also.length > 1 ? query.also.slice(0, -1).join(", ") + " and " + query.also.at(-1) : query.also[0]} for the same papers. ` : "") +
    "A listing date opens that day's heatmap.";
  const missing = papers.filter((p) => p.missing).length;
  if (missing) status.append(` ${missing} titles could not be loaded; their arXiv links still work.`);
  paintLegend();
  $("legend").hidden = $("scroll").hidden = false;
}

function fail(message, error = true) {
  const status = $("status");
  status.textContent = message;
  status.classList.toggle("error", error);
  $("legend").hidden = $("scroll").hidden = true;
}

const longDate = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US",
  { weekday: "long", year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });

// index.json lists, for each ID-month folder, the listing dates with a table in
// it: a day of the folder's own month as a number, and days of another month in
// a nested list that starts with that month as YYMM (older indexes wrote such
// dates in full). {"2609": [29, 30, [2610, 1]], "2610": [[2609, 22, 23], 2, 5]}
// -> date -> table files.
function tableFiles(index) {
  const files = {};
  const yearOf = (yymm) => (Number(String(yymm).padStart(4, "0").slice(0, 2)) >= 91 ? 1900 : 2000) +
    Number(String(yymm).padStart(4, "0").slice(0, 2));
  const iso = (yymm, day) => `${yearOf(yymm)}-${String(yymm).padStart(4, "0").slice(2)}-${String(day).padStart(2, "0")}`;
  for (const [yymm, entries] of Object.entries(index)) {
    const year = yearOf(yymm);
    const start = 1991 + 3 * Math.floor((year - 1991) / 3);
    const dates = entries.flatMap((e) => typeof e === "number" ? [iso(yymm, e)]
      : Array.isArray(e) ? e.slice(1).map((d) => iso(e[0], d)) : [e]);
    for (const date of dates) {
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
    noAnnouncement = new Set(await getOptionalJSON(TABLES + "empty.json", []));
    configs = (await getOptionalJSON(BASE + "config-log.json", []))
      .sort((a, b) => a.effective.localeCompare(b.effective));
  } catch (e) {
    return fail(`Could not load the table index (${e.message}).`);
  }
  const dates = Object.keys(files).sort();
  roster = { active: people?.active ?? null, hidden: people?.hidden ?? [] };
  if (TOP) {
    wireDates(dates, null);
    if (!dates.length) return fail("No tables yet. The first one appears after the first daily run.");
    return mainTop(files, dates);
  }
  // The base URL opens on the latest day with an announcement, not on a holiday.
  const date = requested ?? [...dates].reverse().find((d) => !noAnnouncement.has(d)) ?? dates.at(-1);
  wireDates(dates, date);
  if (!date) return fail("No tables yet. The first one appears after the first daily run.");
  document.title = `MapHub ${date}`;
  if (!files[date]) {
    return fail(`No table for ${date}. arXiv makes no announcement on weekends and holidays, ` +
      "and a day's table appears only after that day's listing is scored.");
  }
  try {
    day = await loadDay(date, files[date]);
  } catch (e) {
    return fail(`Could not load the table for ${date} (${e.message}).`);
  }
  if (!day.papers.length) {
    // An empty table: arXiv made no announcement for this listing (a holiday), and its
    // submissions went out with the next one.
    const listings = listingsOn(date);
    return fail(`${longDate(date)}: no new papers${listings ? ` in ${listings}` : ""}. arXiv made no ` +
      "announcement for this listing (usually a holiday); its submissions appear in the next listing.", false);
  }
  selection = readHash();
  paintLegend();
  $("legend").hidden = $("scroll").hidden = false;
  wireTable();
  render();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { paintLegend(); render(); });
}

main();
