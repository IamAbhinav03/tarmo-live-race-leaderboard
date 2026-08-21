let currentState = null;
const form = document.querySelector("#race-form");
const input = document.querySelector("#player-name");
const armButton = document.querySelector("#arm-button");
const message = document.querySelector("#form-message");
const activeContent = document.querySelector("#active-content");
const logList = document.querySelector("#log-list");
const logFilter = document.querySelector("#log-filter");
const pauseLogs = document.querySelector("#pause-logs");
let auditLogs = [];
let logsPaused = false;

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  message.textContent = "";
  armButton.disabled = true;
  try {
    await post("/api/races", { player_name: input.value });
    input.value = "";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    armButton.disabled = Boolean(currentState?.active_race);
  }
});

document.addEventListener("click", async (event) => {
  if (event.target.matches("#cancel-race")) {
    event.target.disabled = true;
    try { await post("/api/races/cancel"); }
    catch (error) { message.textContent = error.message; }
  }
  if (event.target.matches("#pause-logs")) {
    logsPaused = !logsPaused;
    pauseLogs.textContent = logsPaused ? "Resume" : "Pause";
    pauseLogs.classList.toggle("paused", logsPaused);
  }
});

logFilter.addEventListener("change", renderLogs);

function logMatches(entry, filter) {
  if (filter === "all") return true;
  if (filter === "sensor") return entry.source === "firmware" && entry.code.startsWith("sensor");
  if (filter === "usb" || filter === "wifi") return entry.transport?.includes(filter);
  if (filter === "race") return entry.source === "race" || entry.source === "operator";
  return entry.level === filter;
}

function logTime(iso) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3,
  }).format(new Date(iso));
}

function renderLogs() {
  const entries = auditLogs.filter((entry) => logMatches(entry, logFilter.value));
  logList.innerHTML = entries.length ? entries.map((entry) => {
    const details = Object.entries(entry.details || {})
      .map(([key, value]) => `${Tarmo.escapeHtml(key)}=${Tarmo.escapeHtml(value)}`).join(" · ");
    const meta = [entry.source, entry.transport, entry.event_id ? `event ${entry.event_id}` : null,
      entry.race_id ? `race ${entry.race_id}` : null].filter(Boolean).join(" · ");
    return `<article class="log-row level-${entry.level}">
      <time datetime="${Tarmo.escapeHtml(entry.created_at)}">${logTime(entry.created_at)}</time>
      <div class="log-body"><div class="log-code">${Tarmo.escapeHtml(entry.code)}</div>
        <div class="log-message">${Tarmo.escapeHtml(entry.message)}</div>
        <div class="log-meta">${Tarmo.escapeHtml(meta)}${details ? `<br>${details}` : ""}</div></div>
      <span class="log-level">${Tarmo.escapeHtml(entry.level)}</span>
    </article>`;
  }).join("") : '<div class="empty">No matching log entries.</div>';
}

async function refreshLogs() {
  if (logsPaused) return;
  try {
    const response = await fetch("/api/logs?limit=250", { cache: "no-store" });
    if (!response.ok) throw new Error("Log request failed");
    auditLogs = (await response.json()).logs;
    renderLogs();
  } catch (error) {
    logList.innerHTML = `<div class="empty">${Tarmo.escapeHtml(error.message)}</div>`;
  }
}

function statusLabel(status) {
  return ({ complete: "Classified", cancelled: "Cancelled", error: "Timing error", armed: "Armed", running: "On track" })[status] || status;
}

function render(state) {
  currentState = state;
  document.querySelector("#server-dot").classList.add("live");
  document.querySelector("#server-status").textContent = "Server live";
  const active = state.active_race;
  armButton.disabled = Boolean(active);
  input.disabled = Boolean(active);

  if (!active) {
    activeContent.innerHTML = '<div class="active-status">Grid open</div><div class="active-name">No active race</div><div class="active-hint">Register the next driver to arm the timing gate.</div>';
  } else if (active.status === "armed") {
    activeContent.innerHTML = `<div class="active-status">Timing armed</div><div class="active-name">${Tarmo.escapeHtml(active.player_name)}</div><div class="active-hint">Waiting for the first two-sensor crossing to start the lap.</div><div class="button-row" style="justify-content:center"><button class="secondary danger" id="cancel-race">Cancel race</button></div>`;
  } else {
    activeContent.innerHTML = `<div class="active-status">Lap in progress</div><div class="active-name">${Tarmo.escapeHtml(active.player_name)}</div><div class="active-clock" id="operator-clock">00:00.000</div><div class="active-hint">The next validated crossing records the official time.</div><div class="button-row" style="justify-content:center"><button class="secondary danger" id="cancel-race">Cancel race</button></div>`;
  }

  const device = state.device;
  document.querySelector("#device-name").textContent = device ? `${device.device_id} · ${device.transport}` : "Not seen yet";
  document.querySelector("#device-last").textContent = device ? Tarmo.shortDate(device.received_at) : "—";

  const recent = state.recent_races;
  document.querySelector("#recent-list").innerHTML = recent.length ? recent.map((race) => `
    <div class="recent-row">
      <div><div class="recent-name">${Tarmo.escapeHtml(race.player_name)}</div><div class="recent-meta">${statusLabel(race.status)} · ${Tarmo.shortDate(race.finished_at || race.armed_at)}</div></div>
      <div class="recent-value">${race.status === "complete" ? Tarmo.formatTime(race.elapsed_us) : (race.error_message ? Tarmo.escapeHtml(race.error_message) : "—")}</div>
    </div>`).join("") : '<div class="empty">No races recorded yet.</div>';
}

function tick() {
  const active = currentState?.active_race;
  const clock = document.querySelector("#operator-clock");
  if (clock && active?.started_at) {
    clock.textContent = Tarmo.formatTime((Date.now() - new Date(active.started_at).getTime()) * 1000);
  }
  requestAnimationFrame(tick);
}

Tarmo.connect(render);
requestAnimationFrame(tick);
refreshLogs();
setInterval(refreshLogs, 1000);
