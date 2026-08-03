"use strict";

const state = { range: "30", from: null, to: null, cursor: null };
const errorBox = document.querySelector("#error");

function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function bounds() {
  const now = new Date();
  let from = new Date(now);
  if (state.range === "today") from.setHours(0, 0, 0, 0);
  else if (state.range === "all") from = new Date(now.getTime() - 365 * 86400000);
  else if (state.range === "custom") return { from: state.from, to: state.to };
  else from = new Date(now.getTime() - Number(state.range) * 86400000);
  return { from, to: now };
}

function query(cursor = null) {
  const range = bounds();
  const params = new URLSearchParams({ from: range.from.toISOString(), to: range.to.toISOString() });
  const client = document.querySelector("#client-filter").value;
  if (client) params.set("client_type", client);
  if (cursor) params.set("cursor", cursor);
  return params;
}

async function api(path, params) {
  const response = await fetch(`${path}?${params}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function ranking(target, items, name) {
  target.replaceChildren(...items.slice(0, 10).map((item) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = name(item);
    const value = document.createElement("strong");
    value.textContent = formatDuration(item.watch_seconds);
    const detail = document.createElement("small");
    detail.textContent = `${item.session_count} session${item.session_count === 1 ? "" : "s"}`;
    const meter = document.createElement("span");
    meter.className = "meter";
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(1, item.percentage)}%`;
    meter.append(fill);
    li.append(label, value, detail, meter);
    return li;
  }));
}

function renderSummary(data) {
  document.querySelector("#watch-time").textContent = formatDuration(data.total_watch_seconds);
  document.querySelector("#session-count").textContent = data.session_count.toLocaleString();
  document.querySelector("#top-channel").textContent = data.channels[0]?.channel_name || "—";
  document.querySelector("#top-show").textContent = data.shows[0]?.show_name || "—";
  ranking(document.querySelector("#channels"), data.channels, (item) => `CH ${item.channel_number} · ${item.channel_name}`);
  ranking(document.querySelector("#shows"), data.shows, (item) => item.show_name);
  ranking(document.querySelector("#clients"), data.clients, (item) => item.client_type === "fire_tv" ? "Fire TV" : "Browser");
  const max = Math.max(1, ...data.daily.map((day) => day.watch_seconds));
  document.querySelector("#daily-chart").replaceChildren(...data.daily.map((day) => {
    const bar = document.createElement("span");
    bar.className = "bar";
    bar.title = `${day.date}: ${formatDuration(day.watch_seconds)}`;
    const fill = document.createElement("i");
    fill.style.height = `${Math.max(1, day.watch_seconds / max * 100)}%`;
    const label = document.createElement("span");
    label.textContent = day.date.slice(5);
    bar.append(fill, label);
    return bar;
  }));
}

function appendHistory(data, reset) {
  const body = document.querySelector("#history");
  if (reset) body.replaceChildren();
  for (const item of data.items) {
    const row = document.createElement("tr");
    const values = [new Date(item.started_at * 1000).toLocaleString(), `CH ${item.channel_number} · ${item.channel_name}`, item.show_name, item.episode_title, item.client_type === "fire_tv" ? "Fire TV" : "Browser", formatDuration(item.watch_seconds)];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    body.append(row);
  }
  state.cursor = data.next_cursor;
  document.querySelector("#load-more").hidden = !state.cursor;
  document.querySelector("#empty-history").hidden = body.children.length > 0;
}

async function load(reset = true) {
  errorBox.hidden = true;
  try {
    if (reset) state.cursor = null;
    const params = query(reset ? null : state.cursor);
    const [summary, history] = await Promise.all([
      reset ? api("/api/v1/analytics/summary", params) : Promise.resolve(null),
      api("/api/v1/analytics/history", params),
    ]);
    if (summary) renderSummary(summary);
    appendHistory(history, reset);
  } catch (error) {
    errorBox.textContent = `Unable to load analytics: ${error.message}`;
    errorBox.hidden = false;
  }
}

document.querySelectorAll("[data-range]").forEach((button) => button.addEventListener("click", () => {
  state.range = button.dataset.range;
  document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("active", item === button));
  void load();
}));
document.querySelector("#custom-range").addEventListener("submit", (event) => {
  event.preventDefault();
  const from = new Date(`${document.querySelector("#from-date").value}T00:00:00`);
  const to = new Date(`${document.querySelector("#to-date").value}T23:59:59.999`);
  if (from >= to) {
    errorBox.textContent = "From must be before To.";
    errorBox.hidden = false;
    return;
  }
  state.range = "custom";
  state.from = from;
  state.to = to;
  document.querySelectorAll("[data-range]").forEach((item) => item.classList.remove("active"));
  void load();
});
document.querySelector("#client-filter").addEventListener("change", () => void load());
document.querySelector("#load-more").addEventListener("click", () => void load(false));

void load();
