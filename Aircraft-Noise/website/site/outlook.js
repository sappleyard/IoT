// The Forecast page: the next 10 days, filled from data/outlook.json where it has them. The
// forecast job only has to write that file, as the website README describes. Past days pair
// outlook.json's history (each day's forecast from the day before) with the passes that
// happened, counted from data/passes.json.

const DAYS = 10;
const FULL_DAY_HOURS = 23.5;     // as on the Heard page: less logging than this isn't scored
const PAST_SHOWN = 10;           // past days listed before "Show all"
const CORRECT = 75;              // a day is correct at this accuracy (%) or better
const KINDS = { west: "Westerly", east: "Easterly", switch: "Runway change" };

function isoDate(d) {
  return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"),
    String(d.getDate()).padStart(2, "0")].join("-");
}

function dash(value) {
  return value === null || value === undefined || value === "" ? "–" : String(value);
}

function draw(forecast) {
  const body = document.getElementById("outlook");
  const today = new Date();
  body.replaceChildren(...Array.from({ length: DAYS }, (_, i) => {
    const day = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
    const f = forecast.get(isoDate(day)) || {};
    const cells = [
      [day.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }), "nowrap"],
      [dash(f.passes), "num"],
      [f.confidence != null ? `${f.confidence}%` : dash(null), "num"],
      [dash(f.reason), ""],
      [dash(f.wind), "nowrap"],
      [dash(f.weather), ""],
    ];
    const row = document.createElement("tr");
    for (const [text, className] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      td.className = text === "–" ? `${className} empty` : className;
      row.append(td);
    }
    return row;
  }));
}

const shortDay = (d) => new Date(d + "T12:00:00Z").toLocaleDateString("en-GB",
  { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" });

// Accuracy (%): the smaller count as a share of the larger, 100 when both are 0.
const accuracy = (expected, actual) =>
  Math.round(100 * (Math.min(expected, actual) / Math.max(expected, actual, 1)));

async function getJSON(url) {
  try {
    const response = await fetch(url, { cache: "no-cache" });
    if (response.ok) return await response.json();
  } catch (e) {
    // missing: the caller's default stands
  }
  return null;
}

function drawPast(history, passes) {
  const full = new Set(passes.days.filter(([, hours]) => hours >= FULL_DAY_HOURS).map(([d]) => d));
  const actual = new Map();
  for (const r of passes.passes) actual.set(r[0], (actual.get(r[0]) || 0) + 1);
  const scored = history.filter((h) => full.has(h.date) && h.passes != null)
    .map((h) => ({ ...h, actual: actual.get(h.date) || 0 }))
    .map((h) => ({ ...h, accuracy: accuracy(h.passes, h.actual) }));
  if (!scored.length) return;

  document.getElementById("past-rows").replaceChildren(...scored.reverse().map((h) => {
    const row = document.createElement("tr");
    for (const [text, className] of [
      [shortDay(h.date), "nowrap"],
      [String(h.passes), "num"],
      [String(h.actual), "num"],
      [`${h.accuracy}%`, `num ${h.accuracy >= CORRECT ? "good" : "bad"}`],
      [h.confidence != null ? `${h.confidence}%` : "–", "num"],
      [KINDS[h.kind] || "–", ""],
    ]) {
      const td = document.createElement("td");
      td.textContent = text;
      td.className = className;
      row.append(td);
    }
    return row;
  }));

  // The latest PAST_SHOWN days, the rest behind a button (the averages use every day)
  const rows = [...document.getElementById("past-rows").children];
  const more = document.getElementById("past-more");
  more.hidden = rows.length <= PAST_SHOWN;
  const collapse = (collapsed) => {
    rows.forEach((row, i) => { row.hidden = collapsed && i >= PAST_SHOWN; });
    more.textContent = collapsed ? `Show all ${rows.length} days` : "Show fewer";
    more.setAttribute("aria-expanded", String(!collapsed));
  };
  more.addEventListener("click", () => collapse(more.getAttribute("aria-expanded") === "true"));
  collapse(true);

  const correct = scored.filter((h) => h.accuracy >= CORRECT).length;
  const mean = Math.round(scored.reduce((sum, h) => sum + h.accuracy, 0) / scored.length);
  const stats = [
    ["Forecast correct", `${Math.round(100 * correct / scored.length)}%`,
      `${correct} of ${scored.length} days at least ${CORRECT}% accurate`],
    ["Average accuracy", `${mean}%`, `how close it was, averaged over ${scored.length} days`],
  ];
  const box = document.getElementById("accuracy");
  box.replaceChildren(...stats.map(([label, value, note]) => {
    const div = document.createElement("div");
    div.className = "stat";
    for (const [tag, text, className] of [["dt", label, ""], ["dd", value, ""], ["dd", note, "note"]]) {
      const node = document.createElement(tag);
      node.textContent = text;
      node.className = className;
      div.append(node);
    }
    return div;
  }));
  box.hidden = false;

  const button = document.getElementById("toggle-past");
  const past = document.getElementById("past");
  button.hidden = false;
  button.addEventListener("click", () => {
    past.hidden = !past.hidden;
    button.setAttribute("aria-expanded", String(!past.hidden));
    button.textContent = past.hidden ? "Show past days" : "Hide past days";
  });
}

async function start() {
  const data = (await getJSON("data/outlook.json")) || { days: [] };
  draw(new Map(data.days.map((d) => [d.date, d])));
  if (data.days.length) {
    document.getElementById("status").hidden = true;
    const issued = new Date(data.generated + "T12:00:00Z").toLocaleDateString("en-GB",
      { timeZone: "UTC", day: "numeric", month: "long", year: "numeric" });
    document.getElementById("updated").textContent = `Forecast issued ${issued}.`;
  }
  if (data.history && data.history.length) {
    const passes = await getJSON("data/passes.json");
    if (passes) drawPast(data.history, passes);
  }
}

start();
