let currentState = null;
const form = document.querySelector("#race-form");
const input = document.querySelector("#player-name");
const distanceInput = document.querySelector("#shot-distance");
const distanceUnit = document.querySelector("#distance-unit");
const armButton = document.querySelector("#arm-button");
const message = document.querySelector("#form-message");
const activeContent = document.querySelector("#active-content");
const logList = document.querySelector("#log-list");
const logFilter = document.querySelector("#log-filter");
const pauseLogs = document.querySelector("#pause-logs");
const mcpsForm = document.querySelector("#mcps-form");
const mcpsInput = document.querySelector("#mcps-input");
const mcpsApply = document.querySelector("#mcps-apply");
const mcpsState = document.querySelector("#mcps-state");
const mcpsMessage = document.querySelector("#mcps-message");
let auditLogs = [];
let logsPaused = false;
let selectedCompetition = "race";
let mcpsInputDirty = false;
let deviceConfig = null;
const sensorRows = {
  sensor_a: document.querySelector("#sensor-a-row"),
  sensor_b: document.querySelector("#sensor-b-row"),
};

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
    if (selectedCompetition === "race") {
      await post("/api/races", { player_name: input.value });
    } else {
      await post("/api/cannon-results", {
        player_name: input.value,
        distance: distanceInput.value,
        unit: distanceUnit.value,
      });
    }
    input.value = "";
    distanceInput.value = "";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    updateFormAvailability();
  }
});

mcpsInput.addEventListener("input", () => { mcpsInputDirty = true; });

mcpsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  mcpsMessage.textContent = "";
  mcpsMessage.classList.remove("success");
  const value = Number(mcpsInput.value);
  if (!Number.isFinite(value) || value < 0 || value > 20) {
    mcpsMessage.textContent = "Enter a value between 0.00 and 20.00 MCPS.";
    return;
  }
  mcpsApply.disabled = true;
  try {
    deviceConfig = await post("/api/device/config", { min_signal_rate_mcps: value });
    mcpsInputDirty = false;
    renderDeviceConfig(deviceConfig);
    mcpsMessage.textContent = deviceConfig.command_sent
      ? "Saved and sent to the ESP32."
      : "Saved. It will be applied when USB reconnects.";
    mcpsMessage.classList.add("success");
  } catch (error) {
    mcpsMessage.textContent = error.message;
  } finally {
    mcpsApply.disabled = false;
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
  const competitionButton = event.target.closest("[data-competition]");
  if (competitionButton) selectCompetition(competitionButton.dataset.competition);
});

function selectCompetition(competition) {
  selectedCompetition = competition;
  document.querySelectorAll("[data-competition]").forEach((button) => {
    const selected = button.dataset.competition === competition;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  const cannon = competition === "cannon";
  document.body.classList.toggle("operator-cannon", cannon);
  document.querySelector("#operator-brand-mark").textContent = cannon ? "C" : "R";
  document.querySelector("#operator-brand-name").textContent = cannon ? "Cannon Control" : "Race Control";
  document.querySelector("#entry-kicker").textContent = cannon ? "Distance registration" : "Driver registration";
  document.querySelector("#entry-title").textContent = cannon ? "Launch for glory." : "Ready the grid.";
  document.querySelector("#entry-copy").textContent = cannon
    ? "Record each measured shot. Every attempt is saved; each participant is ranked by their personal best."
    : "Register one driver and arm the timing gate. The first validated crossing starts their lap; the next finishes it.";
  document.querySelector("#player-label").textContent = cannon ? "Participant name" : "Driver name";
  input.placeholder = cannon ? "Enter participant name" : "Enter player name";
  document.querySelector("#distance-fields").hidden = !cannon;
  distanceInput.required = cannon;
  document.querySelector("#device-card").hidden = cannon;
  armButton.textContent = cannon ? "Record cannon shot" : "Register & arm race";
  document.querySelector("#recent-heading").textContent = cannon ? "Recent shots" : "Recent runs";
  message.textContent = "";
  updateFormAvailability();
  if (currentState) renderCompetition(currentState);
}

function updateFormAvailability() {
  const raceBlocked = selectedCompetition === "race" && Boolean(currentState?.active_race);
  armButton.disabled = raceBlocked;
  input.disabled = raceBlocked;
  distanceInput.disabled = selectedCompetition !== "cannon";
  distanceUnit.disabled = selectedCompetition !== "cannon";
}

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

function renderSensor(channel, sensor, receivedAt) {
  const suffix = channel === "sensor_a" ? "a" : "b";
  const row = sensorRows[channel];
  const stale = !receivedAt || Date.now() - new Date(receivedAt).getTime() > 1500;
  const detected = Boolean(sensor?.near) && !stale;
  const candidate = Boolean(sensor?.candidate) && !detected && !stale;
  row.classList.toggle("detected", detected);
  row.classList.toggle("candidate", candidate);
  row.classList.toggle("stale", stale);

  const distance = Number(sensor?.distance_mm);
  const validDistance = Number.isFinite(distance) && distance < 8000;
  document.querySelector(`#sensor-${suffix}-distance`).textContent = validDistance
    ? `${distance} mm`
    : "Out of range";
  document.querySelector(`#sensor-${suffix}-state`).textContent = stale
    ? "Stale"
    : detected ? "Detected" : candidate ? "Candidate" : "Clear";
  document.querySelector(`#sensor-${suffix}-meta`).textContent = sensor
    ? `Range status ${sensor.range_status} · ${sensor.near ? "stable-near" : "stable-clear"} · raw ${distance} mm · signal ${Number(sensor.signal_rate_mcps || 0).toFixed(3)} MCPS`
    : "Waiting for USB telemetry";
}

function renderSensors(snapshot) {
  renderSensor("sensor_a", snapshot?.sensor_a, snapshot?.received_at);
  renderSensor("sensor_b", snapshot?.sensor_b, snapshot?.received_at);
  const gateChip = document.querySelector("#gate-chip");
  gateChip.textContent = snapshot?.gate_locked ? "Gate locked" : snapshot ? "Gate ready" : "Gate waiting";
  gateChip.classList.toggle("live", Boolean(snapshot) && !snapshot.gate_locked);
  if (snapshot && deviceConfig) {
    deviceConfig.applied_min_signal_rate_mcps = snapshot.min_signal_rate_mcps;
    renderDeviceConfig(deviceConfig);
  }
}

function renderDeviceConfig(config) {
  if (!config) return;
  const configured = Number(config.min_signal_rate_mcps || 0);
  const applied = Number(config.applied_min_signal_rate_mcps);
  if (!mcpsInputDirty && document.activeElement !== mcpsInput) {
    mcpsInput.value = configured.toFixed(2);
  }
  mcpsState.textContent = `Configured ${configured.toFixed(3)} MCPS · ESP applied ${Number.isFinite(applied) ? `${applied.toFixed(3)} MCPS` : "unknown"} · USB ${config.usb_connected ? "live" : "offline"}`;
}

async function refreshDeviceConfig() {
  try {
    const response = await fetch("/api/device/config", { cache: "no-store" });
    if (!response.ok) throw new Error("Configuration request failed");
    deviceConfig = await response.json();
    renderDeviceConfig(deviceConfig);
  } catch (_error) {
    mcpsState.textContent = "Configuration unavailable · USB state unknown";
  }
}

async function refreshSensors() {
  try {
    const response = await fetch("/api/sensors", { cache: "no-store" });
    if (!response.ok) throw new Error("Sensor request failed");
    renderSensors((await response.json()).sensors);
  } catch (_error) {
    renderSensors(null);
  }
}

function statusLabel(status) {
  return ({ complete: "Classified", cancelled: "Cancelled", error: "Timing error", armed: "Armed", running: "On track" })[status] || status;
}

function render(state) {
  currentState = state;
  document.querySelector("#server-dot").classList.add("live");
  document.querySelector("#server-status").textContent = "Server live";
  updateFormAvailability();
  renderCompetition(state);

  const device = state.device;
  document.querySelector("#device-name").textContent = device ? `${device.device_id} · ${device.transport}` : "Not seen yet";
  document.querySelector("#device-last").textContent = device ? Tarmo.shortDate(device.received_at) : "—";
}

function renderCompetition(state) {
  const active = state.active_race;
  if (selectedCompetition === "cannon") {
    const leader = state.cannon_leaderboard?.[0];
    activeContent.innerHTML = leader
      ? `<div class="active-status">Cannon Clash leader</div><div class="active-name">${Tarmo.escapeHtml(leader.player_name)}</div><div class="active-clock">${Tarmo.formatDistance(leader.distance_mm)}</div><div class="active-hint">${Tarmo.formatFeet(leader.distance_mm)} · personal-best ranking</div>`
      : '<div class="active-status">Range clear</div><div class="active-name">No shots recorded</div><div class="active-hint">Measure the first launch and enter its distance.</div>';
    const recent = state.recent_cannon || [];
    document.querySelector("#recent-list").innerHTML = recent.length ? recent.map((shot) => `
      <div class="recent-row">
        <div><div class="recent-name">${Tarmo.escapeHtml(shot.player_name)}</div><div class="recent-meta">Cannon Clash · ${Tarmo.shortDate(shot.recorded_at)}</div></div>
        <div class="recent-value">${Tarmo.formatDistance(shot.distance_mm)}<br><span>${Tarmo.formatFeet(shot.distance_mm)}</span></div>
      </div>`).join("") : '<div class="empty">No cannon shots recorded yet.</div>';
    return;
  }

  if (!active) {
    activeContent.innerHTML = '<div class="active-status">Grid open</div><div class="active-name">No active race</div><div class="active-hint">Register the next driver to arm the timing gate.</div>';
  } else if (active.status === "armed") {
    activeContent.innerHTML = `<div class="active-status">Timing armed</div><div class="active-name">${Tarmo.escapeHtml(active.player_name)}</div><div class="active-hint">Waiting for the first two-sensor crossing to start the lap.</div><div class="button-row" style="justify-content:center"><button class="secondary danger" id="cancel-race">Cancel race</button></div>`;
  } else {
    activeContent.innerHTML = `<div class="active-status">Lap in progress</div><div class="active-name">${Tarmo.escapeHtml(active.player_name)}</div><div class="active-clock" id="operator-clock">00:00.000</div><div class="active-hint">The next validated crossing records the official time.</div><div class="button-row" style="justify-content:center"><button class="secondary danger" id="cancel-race">Cancel race</button></div>`;
  }

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
refreshSensors();
setInterval(refreshSensors, 250);
refreshDeviceConfig();
setInterval(refreshDeviceConfig, 5000);
