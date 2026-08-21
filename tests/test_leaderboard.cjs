const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let render;
let rowWrites = 0;
let cannonWrites = 0;
let rowHtml = "";
let cannonHtml = "";

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const value = {
      className: "",
      textContent: "",
      dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {},
    };
    if (id === "leader-grid") {
      Object.defineProperty(value, "innerHTML", {
        get: () => rowHtml,
        set: (html) => { rowHtml = html; rowWrites += 1; },
      });
    }
    if (id === "cannon-grid") {
      Object.defineProperty(value, "innerHTML", {
        get: () => cannonHtml,
        set: (html) => { cannonHtml = html; cannonWrites += 1; },
      });
    }
    elements.set(id, value);
  }
  return elements.get(id);
}

global.location = new URL("http://localhost/leaderboard");
global.history = { replaceState() {} };
global.document = {
  body: { classList: { add() {}, remove() {} } },
  documentElement: { requestFullscreen: async () => {} },
  fullscreenElement: null,
  querySelector: (selector) => element(selector.slice(1)),
  querySelectorAll: () => [],
  addEventListener() {},
  exitFullscreen: async () => {},
};
global.requestAnimationFrame = () => {};
global.setTimeout = (callback) => callback();
global.Tarmo = {
  connect: (callback) => { render = callback; },
  escapeHtml: (value) => String(value),
  formatTime: (value) => String(value),
  formatDistance: (value) => `${value} mm`,
  formatFeet: (value) => `${value} ft`,
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
  cannon_leaderboard: [{
    id: 10,
    player_name: "Launcher One",
    distance_mm: 4500,
    recorded_at: "2026-08-21T00:02:00.000Z",
  }],
};

render({ ...base, server_time: "first telemetry update" });
assert.equal(rowWrites, 1, "initial classification should render once");
assert.equal(cannonWrites, 1, "initial cannon classification should render once");

render({ ...base, server_time: "sensor noise changed server state envelope" });
assert.equal(rowWrites, 1, "unchanged classification must not rebuild rows");
assert.equal(cannonWrites, 1, "unchanged cannon classification must not rebuild rows");

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

render({
  ...base,
  cannon_leaderboard: [...base.cannon_leaderboard, {
    id: 11,
    player_name: "Launcher Two",
    distance_mm: 4200,
    recorded_at: "2026-08-21T00:03:00.000Z",
  }],
});
assert.equal(cannonWrites, 2, "a genuine cannon classification change must rebuild its rows");

console.log("leaderboard render guard passed");
