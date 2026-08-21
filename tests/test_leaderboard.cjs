const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let render;
let rowWrites = 0;
let rowHtml = "";

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const value = {
      className: "",
      textContent: "",
      classList: { add() {}, remove() {} },
    };
    if (id === "leader-grid") {
      Object.defineProperty(value, "innerHTML", {
        get: () => rowHtml,
        set: (html) => { rowHtml = html; rowWrites += 1; },
      });
    }
    elements.set(id, value);
  }
  return elements.get(id);
}

global.document = { querySelector: (selector) => element(selector.slice(1)) };
global.requestAnimationFrame = () => {};
global.setTimeout = (callback) => callback();
global.Tarmo = {
  connect: (callback) => { render = callback; },
  escapeHtml: (value) => String(value),
  formatTime: (value) => String(value),
};

vm.runInThisContext(fs.readFileSync("server/static/leaderboard.js", "utf8"));

const base = {
  active_race: null,
  leaderboard: [{
    id: 1,
    player_name: "Driver One",
    elapsed_us: 12_000_000,
    finished_at: "2026-08-21T00:00:00.000Z",
  }],
};

render({ ...base, server_time: "first telemetry update" });
assert.equal(rowWrites, 1, "initial classification should render once");

render({ ...base, server_time: "sensor noise changed server state envelope" });
assert.equal(rowWrites, 1, "unchanged classification must not rebuild rows");

render({ ...base, active_race: { status: "armed", player_name: "Next Driver" } });
assert.equal(rowWrites, 1, "arming a race must not rebuild existing classification rows");

render({
  ...base,
  leaderboard: [...base.leaderboard, {
    id: 2,
    player_name: "Driver Two",
    elapsed_us: 13_000_000,
    finished_at: "2026-08-21T00:01:00.000Z",
  }],
});
assert.equal(rowWrites, 2, "a genuine classification change must rebuild rows");

console.log("leaderboard render guard passed");
