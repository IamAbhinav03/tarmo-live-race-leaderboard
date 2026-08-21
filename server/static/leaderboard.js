let currentState = null;
let previousRaceLeader = null;
let previousCannonLeader = null;
let previousRaceSignature = null;
let previousCannonSignature = null;
let selectedView = new URLSearchParams(location.search).get("view") || "both";

const raceStrip = document.querySelector("#race-strip");
const raceLabel = document.querySelector("#race-label");
const raceName = document.querySelector("#race-name");
const liveTime = document.querySelector("#live-time");
const leaderGrid = document.querySelector("#leader-grid");
const cannonGrid = document.querySelector("#cannon-grid");

function classificationSignature(entries, fields) {
  return JSON.stringify(entries.map((entry) => fields.map((field) => entry[field])));
}

function setView(view) {
  selectedView = ["both", "race", "cannon"].includes(view) ? view : "both";
  document.body.classList.remove("view-both", "view-race", "view-cannon");
  document.body.classList.add(`view-${selectedView}`);
  document.querySelectorAll("[data-view]").forEach((button) => {
    const selected = button.dataset.view === selectedView;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const url = new URL(location.href);
  url.searchParams.set("view", selectedView);
  history.replaceState(null, "", url);
}

function renderRace(state) {
  const races = state.leaderboard || [];
  document.querySelector("#entry-count").textContent = races.length;
  const active = state.active_race;
  raceStrip.className = `race-strip ${active?.status === "running" ? "running" : "waiting"}`;
  if (!active) {
    raceLabel.textContent = "Grid status";
    raceName.textContent = "Waiting for next driver";
    liveTime.textContent = "--:--.---";
  } else if (active.status === "armed") {
    raceLabel.textContent = "Armed · first crossing starts";
    raceName.textContent = active.player_name;
    liveTime.textContent = "00:00.000";
  } else {
    raceLabel.textContent = "Lap in progress";
    raceName.textContent = active.player_name;
  }

  const signature = classificationSignature(races, ["id", "player_name", "elapsed_us", "finished_at"]);
  if (signature === previousRaceSignature) return;
  previousRaceSignature = signature;
  if (!races.length) {
    leaderGrid.innerHTML = '<div class="empty">No classified drivers yet.</div>';
    previousRaceLeader = null;
    return;
  }
  const newLeader = races[0].id;
  leaderGrid.innerHTML = races.map((entry, index) => `
    <article class="leader-row ${index < 3 ? "podium" : ""}" style="animation-delay:${Math.min(index * 45, 360)}ms">
      <div class="rank">${String(index + 1).padStart(2, "0")}</div>
      <div class="driver">${Tarmo.escapeHtml(entry.player_name)}<small>${index === 0 ? "Current benchmark" : `Gap +${Tarmo.formatTime(entry.elapsed_us - races[0].elapsed_us)}`}</small></div>
      <div class="lap-time">${Tarmo.formatTime(entry.elapsed_us)}</div>
    </article>`).join("");
  if (previousRaceLeader && previousRaceLeader !== newLeader) leaderGrid.classList.add("flash");
  setTimeout(() => leaderGrid.classList.remove("flash"), 700);
  previousRaceLeader = newLeader;
}

function renderCannon(state) {
  const shots = state.cannon_leaderboard || [];
  document.querySelector("#cannon-entry-count").textContent = shots.length;
  const leader = shots[0];
  document.querySelector("#cannon-strip").classList.toggle("running", Boolean(leader));
  document.querySelector("#cannon-leader-name").textContent = leader ? leader.player_name : "Waiting for first launch";
  document.querySelector("#cannon-leading-distance").textContent = leader ? Tarmo.formatDistance(leader.distance_mm) : "--.-- m";

  const signature = classificationSignature(shots, ["id", "player_name", "distance_mm", "recorded_at"]);
  if (signature === previousCannonSignature) return;
  previousCannonSignature = signature;
  if (!shots.length) {
    cannonGrid.innerHTML = '<div class="empty">No cannon shots recorded yet.</div>';
    previousCannonLeader = null;
    return;
  }
  const newLeader = shots[0].id;
  cannonGrid.innerHTML = shots.map((entry, index) => `
    <article class="leader-row cannon-row ${index < 3 ? "podium" : ""}" style="animation-delay:${Math.min(index * 45, 360)}ms">
      <div class="rank">${String(index + 1).padStart(2, "0")}</div>
      <div class="driver">${Tarmo.escapeHtml(entry.player_name)}<small>${index === 0 ? "Distance to beat" : `${Tarmo.formatDistance(shots[0].distance_mm - entry.distance_mm)} behind`}</small></div>
      <div class="lap-time cannon-result">${Tarmo.formatDistance(entry.distance_mm)}<small>${Tarmo.formatFeet(entry.distance_mm)}</small></div>
    </article>`).join("");
  if (previousCannonLeader && previousCannonLeader !== newLeader) cannonGrid.classList.add("flash");
  setTimeout(() => cannonGrid.classList.remove("flash"), 700);
  previousCannonLeader = newLeader;
}

function render(state) {
  currentState = state;
  document.querySelector("#connection-dot").classList.add("live");
  document.querySelector("#connection-label").textContent = "Displays live";
  renderRace(state);
  renderCannon(state);
}

function tick() {
  const active = currentState?.active_race;
  if (active?.status === "running" && active.started_at) {
    liveTime.textContent = Tarmo.formatTime((Date.now() - new Date(active.started_at).getTime()) * 1000);
  }
  requestAnimationFrame(tick);
}

document.addEventListener("click", async (event) => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) setView(viewButton.dataset.view);
  if (event.target.matches("#fullscreen-button")) {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  }
});

document.addEventListener("fullscreenchange", () => {
  document.querySelector("#fullscreen-button").textContent = document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";
});

setView(selectedView);
Tarmo.connect(render);
requestAnimationFrame(tick);
