// The Passes page: everything is drawn in the browser from data/passes.json (see export.py).
// A row is [date, time, callsign, airline, origin, destination, aircraft, type].

const PAGE_SIZE = 100;
const FULL_DAY_HOURS = 23.5;     // a day with less logging than this is marked as partial
const COLUMNS = [                // heading, sort key
  ["Date", (r) => r[0] + r[1]],
  ["Hour", (r) => r[1]],
  ["Callsign", (r) => r[2]],
  ["Airline", (r) => r[3]],
  ["From", (r) => r[4]],
  ["To", (r) => r[5]],
  ["Aircraft", (r) => r[6]],
  ["Type", (r) => r[7]],
];

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

const dayName = (d, opts) =>
  new Date(d + "T12:00:00Z").toLocaleDateString("en-GB", { timeZone: "UTC", ...opts });
const shortDay = (d) => dayName(d, { weekday: "short", day: "numeric", month: "short" });
const number = (n) => n.toLocaleString("en-GB");

function tally(rows, keyOf) {
  const counts = new Map();
  for (const r of rows) {
    const key = keyOf(r);
    if (key) counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function drawStats(rows, days, counts) {
  const full = days.filter(([, hours]) => hours >= FULL_DAY_HOURS);
  const fullTotal = full.reduce((sum, [d]) => sum + (counts.get(d) || 0), 0);
  const [busyDay, busyCount] = days.reduce(
    (best, [d]) => ((counts.get(d) || 0) > best[1] ? [d, counts.get(d)] : best), ["", -1]);
  const helis = rows.filter((r) => r[7] === "Helicopter").length;
  const stats = [
    ["Passes logged", number(rows.length), `over ${days.length} days`],
    ["Average per full day", full.length ? number(Math.round(fullTotal / full.length)) : "–",
      `${full.length} full days`],
    ["Busiest day", number(busyCount), shortDay(busyDay)],
    ["Helicopters", number(helis), `${(100 * helis / rows.length).toFixed(1)}% of passes`],
  ];
  $("stats").replaceChildren(...stats.map(([label, value, note]) =>
    el("div", { className: "stat" },
      el("dt", { textContent: label }),
      el("dd", { textContent: value }),
      el("span", { className: "stat__note", textContent: note }))));
}

function drawDays(rows, days, counts) {
  const helis = new Map(tally(rows.filter((r) => r[7] === "Helicopter"), (r) => r[0]));
  const most = Math.max(...counts.values());
  $("days").replaceChildren(...days.slice().reverse().map(([d, hours]) => {
    const n = counts.get(d) || 0;
    const link = el("a", { href: "#flights", textContent: shortDay(d) });
    const name = el("td", {}, link);
    if (hours < FULL_DAY_HOURS) {
      name.append(el("span", { className: "tag", textContent: `${Math.round(hours)} h logged` }));
    }
    const row = el("tr", {},
      name,
      el("td", { className: "num", textContent: number(n) }),
      el("td", { className: "num", textContent: number(helis.get(d) || 0) }),
      el("td", { className: "bar-cell" },
        el("span", { className: "bar", style: `width:${100 * n / most}%` })));
    row.addEventListener("click", () => { chooseDay(d); $("flights").scrollIntoView(); });
    link.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); chooseDay(d); $("flights").scrollIntoView(); });
    return row;
  }));
}

function drawTop(id, entries) {
  $(id).replaceChildren(...entries.slice(0, 8).map(([name, n]) =>
    el("tr", {}, el("td", { textContent: name }),
      el("td", { className: "num", textContent: number(n) }))));
}

// A highlight is [hour, callsign, airline, origin, destination, aircraft, why].
function drawHighlights(h) {
  if (!h || !h.flights || !h.flights.length) return;
  $("highlights-note").textContent = `The most unusual flights the microphones heard on ${
    dayName(h.date, { weekday: "long", day: "numeric", month: "long" })}, judged against every flight heard before them. Most unusual first.`;
  $("highlight-rows").replaceChildren(...h.flights.map(([hour, callsign, airline, from, to, aircraft, why]) =>
    el("tr", {},
      cell(hour, "num nowrap"), cell(callsign, "callsign"), cell(airline),
      cell(from && to ? `${from} → ${to}` : ""), cell(aircraft),
      el("td", { className: "nowrap" }, el("span", { className: "tag tag--why", textContent: why })))));
  $("highlights").hidden = false;
}

// --- The flight table -----------------------------------------------------------------------

const state = { rows: [], shown: [], page: 0, sortBy: 0, descending: true };

function chooseDay(d) {
  $("day").value = d;
  $("search").value = "";
  $("kind").value = "";
  applyFilters();
}

function applyFilters() {
  const words = $("search").value.toLowerCase().split(/\s+/).filter(Boolean);
  const day = $("day").value;
  const kind = $("kind").value;
  state.shown = state.rows.filter((r) => {
    if ((day && r[0] !== day) || (kind && r[7] !== kind)) return false;
    const text = r.slice(2).join(" ").toLowerCase();
    return words.every((w) => text.includes(w));
  });
  const key = COLUMNS[state.sortBy][1];
  const sign = state.descending ? -1 : 1;
  state.shown.sort((a, b) => {
    const [x, y] = [key(a), key(b)];
    if (!x !== !y) return x ? -1 : 1;    // blanks last, whichever way it's sorted
    return sign * x.localeCompare(y) || b[0].localeCompare(a[0]) || b[1].localeCompare(a[1]);
  });
  state.page = 0;
  drawFlights();
}

function cell(text, className = "") {
  return text ? el("td", { className, textContent: text })
    : el("td", { className: `${className} empty`, textContent: "–" });
}

function drawHead() {
  $("flight-head").replaceChildren(el("tr", {}, ...COLUMNS.map(([name], i) => {
    const button = el("button", { type: "button", textContent: name });
    button.addEventListener("click", () => {
      state.descending = state.sortBy === i ? !state.descending : i === 0;
      state.sortBy = i;
      drawHead();
      applyFilters();
    });
    const th = el("th", { scope: "col" }, button);
    if (state.sortBy === i) th.setAttribute("aria-sort", state.descending ? "descending" : "ascending");
    return th;
  })));
}

function drawFlights() {
  const pages = Math.max(1, Math.ceil(state.shown.length / PAGE_SIZE));
  const start = state.page * PAGE_SIZE;
  $("flight-rows").replaceChildren(...state.shown.slice(start, start + PAGE_SIZE).map((r) =>
    el("tr", {},
      cell(shortDay(r[0]), "nowrap"), cell(r[1], "num nowrap"), cell(r[2], "callsign"),
      cell(r[3]), cell(r[4]), cell(r[5]), cell(r[6]), cell(r[7]))));
  if (!state.shown.length) {
    $("flight-rows").replaceChildren(el("tr", {}, el("td", {
      colSpan: COLUMNS.length, className: "empty", textContent: "No flights match.",
    })));
  }
  $("count").textContent = `${number(state.shown.length)} of ${number(state.rows.length)} passes`;
  $("at").textContent = `Page ${state.page + 1} of ${pages}`;
  $("prev").disabled = state.page === 0;
  $("next").disabled = state.page >= pages - 1;
}

function turn(by) {
  state.page += by;
  drawFlights();
  $("flight-head").scrollIntoView({ block: "center" });
}

async function start() {
  let data;
  try {
    const response = await fetch("data/passes.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(response.status);
    data = await response.json();
  } catch (e) {
    $("lede").textContent = "The log couldn't be loaded. Try refreshing the page.";
    $("lede").className = "head__lede error";
    return;
  }
  const rows = data.passes;
  const days = data.days;
  const counts = new Map(tally(rows, (r) => r[0]));

  const last = days[days.length - 1][0];
  $("lede").textContent = `Every aircraft that has come within 5 km, from ${
    dayName(days[0][0], { day: "numeric", month: "long" })} to ${
    dayName(last, { day: "numeric", month: "long", year: "numeric" })}.`;
  $("updated").textContent = `Updated daily. Today's flights appear tomorrow. Last updated ${
    dayName(data.generated, { day: "numeric", month: "long", year: "numeric" })}.`;

  drawStats(rows, days, counts);
  drawDays(rows, days, counts);
  drawHighlights(data.highlights);
  drawTop("top-airlines", tally(rows, (r) => r[3]));
  drawTop("top-aircraft", tally(rows, (r) => r[6]));
  drawTop("top-routes", tally(rows, (r) => (r[4] && r[5] ? `${r[4]} → ${r[5]}` : "")));

  for (const [d] of days.slice().reverse()) {
    $("day").append(el("option", { value: d, textContent: shortDay(d) }));
  }
  for (const [kind] of tally(rows, (r) => r[7])) {
    $("kind").append(el("option", { value: kind, textContent: kind }));
  }

  state.rows = rows;
  drawHead();
  applyFilters();
  $("search").addEventListener("input", applyFilters);
  $("day").addEventListener("change", applyFilters);
  $("kind").addEventListener("change", applyFilters);
  $("prev").addEventListener("click", () => turn(-1));
  $("next").addEventListener("click", () => turn(1));
}

start();
