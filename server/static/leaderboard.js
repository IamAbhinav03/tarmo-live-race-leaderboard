let currentState = null;
let previousLeader = null;
let previousLeaderboardSignature = null;

const raceStrip = document.querySelector("#race-strip");
const raceLabel = document.querySelector("#race-label");
const raceName = document.querySelector("#race-name");
const liveTime = document.querySelector("#live-time");
const leaderGrid = document.querySelector("#leader-grid");

function classificationSignature(entries) {
  return JSON.stringify(entries.map((entry) => [
    entry.id, entry.player_name, entry.elapsed_us, entry.finished_at,
  ]));
}

function render(state) {
  currentState = state;
  document.querySelector("#connection-dot").classList.add("live");
  document.querySelector("#connection-label").textContent = "Timing live";
  document.querySelector("#entry-count").textContent = state.leaderboard.length;

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

  const signature = classificationSignature(state.leaderboard);
  if (signature === previousLeaderboardSignature) return;
  previousLeaderboardSignature = signature;

  if (!state.leaderboard.length) {
    leaderGrid.innerHTML = '<div class="empty">No classified drivers yet. The first completed lap sets the benchmark.</div>';
    previousLeader = null;
    return;
  }

  const newLeader = state.leaderboard[0]?.id;
  leaderGrid.innerHTML = state.leaderboard.map((entry, index) => `
    <article class="leader-row ${index < 3 ? "podium" : ""}" style="animation-delay:${Math.min(index * 45, 360)}ms">
      <div class="rank">${String(index + 1).padStart(2, "0")}</div>
      <div class="driver">${Tarmo.escapeHtml(entry.player_name)}<small>${index === 0 ? "Current benchmark" : `Gap +${Tarmo.formatTime(entry.elapsed_us - state.leaderboard[0].elapsed_us)}`}</small></div>
      <div class="lap-time">${Tarmo.formatTime(entry.elapsed_us)}</div>
    </article>`).join("");
  if (previousLeader && previousLeader !== newLeader) leaderGrid.classList.add("flash");
  setTimeout(() => leaderGrid.classList.remove("flash"), 700);
  previousLeader = newLeader;
}

function tick() {
  const active = currentState?.active_race;
  if (active?.status === "running" && active.started_at) {
    const elapsedUs = (Date.now() - new Date(active.started_at).getTime()) * 1000;
    liveTime.textContent = Tarmo.formatTime(elapsedUs);
  }
  requestAnimationFrame(tick);
}

Tarmo.connect(render);
requestAnimationFrame(tick);
