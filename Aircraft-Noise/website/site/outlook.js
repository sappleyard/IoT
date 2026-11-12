// The Forecast page: the next 10 days, filled from data/outlook.json where it has them. The
// forecast job only has to write that file, as the website README describes.

const DAYS = 10;

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

async function start() {
  let data = { days: [] };
  try {
    const response = await fetch("data/outlook.json", { cache: "no-cache" });
    if (response.ok) data = await response.json();
  } catch (e) {
    // No forecast to show: the dashes stand.
  }
  draw(new Map(data.days.map((d) => [d.date, d])));
  if (data.days.length) {
    document.getElementById("status").hidden = true;
    const issued = new Date(data.generated + "T12:00:00Z").toLocaleDateString("en-GB",
      { timeZone: "UTC", day: "numeric", month: "long", year: "numeric" });
    document.getElementById("updated").textContent = `Forecast issued ${issued}.`;
  }
}

start();
