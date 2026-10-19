"use strict";

// GazeVoice control center. Talks to the local server (see gazevoice/server/app.py):
// GET /api/state, /api/jobs, /api/events (SSE); POST actions; PUT /api/settings.

const NAMES = {
  microphone: "Microphone",
  speech: "Speech-to-text",
  claude: "Claude Code",
  codex: "Codex",
  blender: "Blender",
  mcp_addon: "MCP add-on",
  mcp_server: "MCP server",
  bridge: "Bridge",
  brain: "no brain logged in",
  workflow: "Workflow check",
};

const GROUPS = [
  { label: "Input", hint: "Hears you and writes down what you said", ids: ["microphone", "speech"] },
  { label: "Brain", hint: "Cleans up what you said, then edits the scene", ids: ["claude", "codex"] },
  { label: "Blender", hint: "Where the edits land", ids: ["blender", "mcp_addon", "mcp_server", "bridge"] },
];

const HEALTH = {
  ready: { word: "Ready", led: "live" },
  installed: { word: "Installed", led: "standby" },
  missing: { word: "Not installed", led: "off" },
  error: { word: "Problem", led: "fault" },
  checking: { word: "Checking…", led: "busy" },
};

// Which signal-path stages a workflow-check step proves.
const STEP_STAGES = {
  microphone: ["listen"],
  speech: ["transcribe"],
  claude: ["refine", "edit"],
  codex: ["refine", "edit"],
  mcp_server: ["edit"],
  bridge: ["blender"],
};

const ARM_MS = 8000;
const REFRESH_MS = 45000;
const MAX_LINES = 400;
const HIDDEN_DETAILS = new Set(["Devices"]);
// `?snapshot` renders once with no live stream or timers (headless screenshots, a static view).
const SNAPSHOT = new URLSearchParams(location.search).has("snapshot");

const S = {
  state: null,
  loading: true,
  offline: false,
  jobs: new Map(),
  open: new Set(),
  armed: null, // {cid, aid, until}
  flash: new Map(), // cid -> {text, tone}
  link: "connecting",
  stageTests: new Map(), // stage id -> testing | passed | failed
  workflowStep: null, // component id "Check everything" is testing right now
  spotlight: null,
  voice: null, // the latest voice snapshot (from /api/state, then live events)
  voiceError: null,
  typed: "", // the unsent "type a command" draft survives re-renders
  talkHeld: false,
  talkListeners: null, // ends the listeners a dashboard hold-to-talk added
};

const VOICE_WORDS = {
  off: ["off", "Voice is off"],
  starting: ["busy", "Starting…"],
  ready: ["live", "Ready"],
  listening: ["rec", "Listening…"],
  transcribing: ["busy", "Transcribing…"],
  refining: ["busy", "Understanding…"],
  editing: ["busy", "Working in Blender…"],
  error: ["fault", "Voice stopped"],
};
const STAGE_WORDS = {
  queued: "Waiting its turn…",
  transcribing: "Transcribing…",
  refining: "Understanding…",
  editing: "Working in Blender…",
};
const KIND_WORDS = { undo: "Undo", redo: "Redo", stop: "Stop", ignore: "Not a Blender command" };
// Claude Code models by full name, and how much they think (`claude --effort`); the server checks the same lists.
const CLAUDE_MODELS = [
  ["default", "Default (Claude Code picks)"],
  ["claude-fable-5-1", "Fable 5.1"],
  ["claude-opus-5-5", "Opus 5.5"],
  ["claude-sonnet-5-5", "Sonnet 5.5"],
  ["claude-haiku-5-5", "Haiku 5.5"],
];
const EFFORTS = [
  ["default", "Default thinking"],
  ["low", "Low thinking"],
  ["medium", "Medium thinking"],
  ["high", "High thinking"],
  ["xhigh", "Extra high thinking"],
  ["max", "Max thinking"],
];

function modelLabel(id) {
  const known = CLAUDE_MODELS.find(([value]) => value !== "default" && id.startsWith(value));
  return known ? known[1] : id;
}

function effortLabel(effort) {
  const known = EFFORTS.find(([value]) => value === effort);
  return known && effort !== "default" ? known[1].toLowerCase() : "";
}

// ---------- small helpers ----------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);
const nameOf = (cid) => {
  const comp = S.state && S.state.components.find((c) => c.id === cid);
  return (comp && comp.name) || NAMES[cid] || cid;
};
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function ago(seconds) {
  const delta = Math.max(0, Date.now() / 1000 - seconds);
  if (delta < 10) return "just now";
  if (delta < 60) return `${Math.round(delta)} s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
  return `${Math.round(delta / 3600)} h ago`;
}

function clock(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function duration(job) {
  if (!job.finished_at) return "";
  const s = job.finished_at - job.started_at;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.round(s / 60)} min`;
}

function announce(text) {
  $("announcer").textContent = text;
}

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function send(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "X-GazeVoice": "1", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  return { ok: response.ok, status: response.status, data };
}

// Keep keyboard focus across re-renders.
function preservingFocus(render) {
  const key = document.activeElement && document.activeElement.dataset
    ? document.activeElement.dataset.focusKey
    : null;
  render();
  if (key) {
    const again = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
    if (again && again !== document.activeElement) again.focus({ preventScroll: true });
  }
}

// ---------- derived state ----------

function components() {
  return S.state ? S.state.components : [];
}

function component(cid) {
  return components().find((c) => c.id === cid) || null;
}

function runningJob(cid) {
  for (const job of S.jobs.values()) {
    if (job.component_id === cid && job.state === "running") return job;
  }
  return null;
}

function lastFinishedJob(cid) {
  let best = null;
  for (const job of S.jobs.values()) {
    if (job.component_id !== cid || job.state === "running") continue;
    if (!best || job.started_at > best.started_at) best = job;
  }
  return best;
}

function jobRunning(cid) {
  return Boolean(runningJob(cid) || (S.state && S.state.running && S.state.running[cid]));
}

function anyUnitRunning() {
  const fromJobs = [...S.jobs.values()].some((j) => j.state === "running" && j.component_id !== "workflow");
  const fromState = Object.keys((S.state && S.state.running) || {}).some((cid) => cid !== "workflow");
  return fromJobs || fromState;
}

// "Check everything" and unit jobs exclude each other (the server enforces it too).
function isBusy(cid) {
  if (cid === "workflow") return jobRunning("workflow") || anyUnitRunning();
  return jobRunning(cid) || jobRunning("workflow");
}

// The one unit to blink: its own job, or the step "Check everything" is on right now.
function isWorking(cid) {
  return jobRunning(cid) || S.workflowStep === cid;
}

function micLevel() {
  for (const job of [...S.jobs.values()].sort((a, b) => b.started_at - a.started_at)) {
    if (job.component_id === "microphone" && job.state === "succeeded" && job.result
        && typeof job.result.level_dbfs === "number") {
      return job.result.level_dbfs;
    }
  }
  return null;
}

function resultLine(job) {
  const r = job.result || {};
  if (typeof r.reply === "string") return `Replied “${r.reply}” in ${r.seconds} s`;
  if (typeof r.text === "string") return `Heard “${r.text}” in ${r.seconds} s`;
  if (typeof r.level_dbfs === "number") return `Input level ${r.level_dbfs.toFixed(1)} dBFS`;
  if (typeof r.tools === "number") return `${plural(r.tools, "tool")} · Blender ${r.blender}`;
  if (r.selection && typeof r.selection === "object") {
    const selected = r.selection.selected || [];
    return selected.length ? `Connected · selected: ${selected.join(", ")}` : "Connected · nothing selected";
  }
  if (r.launched) return "Launched";
  if (r.installed) return "Installed";
  return "Done";
}

function actionLabel(cid, aid) {
  const comp = component(cid);
  const spec = comp && comp.actions.find((a) => a.id === aid);
  return spec ? spec.label : aid;
}

// ---------- header ----------

function renderHeader() {
  const led = $("chain-led");
  const title = $("chain-title");
  const sub = $("chain-sub");
  if (S.offline) {
    led.dataset.led = "fault";
    title.textContent = "Can't reach the control center";
    sub.textContent = "Start it again with python3 -m gazevoice, then refresh";
  } else if (!S.state) {
    led.dataset.led = "off";
    title.textContent = "Checking your setup…";
    sub.textContent = "Reading each component on this Mac";
  } else {
    const stages = S.state.workflow;
    const ready = stages.filter((s) => s.health === "ready").length;
    const fault = stages.some((s) => s.health === "error");
    const checking = stages.some((s) => s.health === "checking");
    const brain = S.state.brain
      ? `Brain: ${nameOf(S.state.brain)}`
      : S.state.brain_pending ? "Brain: checking…" : "No brain logged in yet";
    if (checking) {
      led.dataset.led = "busy";
      title.textContent = "Still checking your setup…";
      sub.textContent = `${brain} · ${ready} of ${stages.length} stages ready so far`;
    } else {
      led.dataset.led = ready === stages.length ? "live" : fault ? "fault" : "standby";
      title.textContent = ready === stages.length
        ? "Workflow ready"
        : `${plural(stages.length - ready, "stage")} need${stages.length - ready === 1 ? "s" : ""} attention`;
      sub.textContent = `${brain} · ${ready} of ${stages.length} stages ready`;
    }
  }

  const check = $("check-all");
  const workflow = component("workflow");
  const offered = workflow && workflow.actions.find((a) => a.id === "check");
  check.hidden = !offered;
  if (offered) {
    const running = jobRunning("workflow");
    const armed = S.armed && S.armed.cid === "workflow" && S.armed.until > Date.now();
    check.disabled = isBusy("workflow");
    check.title = anyUnitRunning() ? "Wait for the running unit to finish" : "";
    check.toggleAttribute("data-armed", Boolean(armed));
    check.firstChild.textContent = running ? "Checking… " : armed ? "Confirm check " : "Check everything ";
    if (armed && offered.confirm) sub.textContent = offered.confirm;
  }
  renderLink();
}

function renderLink() {
  const map = { live: ["live", "Live"], connecting: ["standby", "Connecting"], reconnecting: ["standby", "Reconnecting"] };
  const [led, word] = map[S.link] || map.connecting;
  $("link-led").dataset.led = led;
  $("link-word").textContent = word;
}

// ---------- signal path ----------

function renderChain() {
  const chain = $("chain");
  chain.replaceChildren();
  if (!S.state) {
    for (const label of ["Microphone", "Speech-to-text", "Refiner", "Editor", "Blender"]) {
      chain.append(h("li", { class: "stage skeleton", "aria-busy": "true" },
        h("div", { class: "stage-top" }, h("span", { class: "led", "data-led": "off" }),
          h("span", { class: "stage-word", text: "Checking…" })),
        h("span", { class: "stage-name", text: label })));
    }
    return;
  }
  for (const stage of S.state.workflow) {
    const health = HEALTH[stage.health] || HEALTH.missing;
    const test = S.stageTests.get(stage.id);
    const parts = stage.parts.map((p) => (p === "brain" ? nameOf("brain") : nameOf(p)));
    const li = h("li", {
      class: "stage",
      tabindex: "0",
      role: "button",
      "data-health": stage.health,
      "data-test": test || null,
      "data-focus-key": `stage-${stage.id}`,
      "aria-label": `${stage.label}: ${health.word}. Uses ${parts.join(", ")}. Show in rack.`,
      onclick: () => spotlight(stage.parts),
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          spotlight(stage.parts);
        }
      },
    },
      h("div", { class: "stage-top" },
        h("span", { class: "led", "data-led": health.led, "aria-hidden": "true" }),
        h("span", { class: "stage-word", text: test === "testing" ? "Testing…" : health.word })),
      h("span", { class: "stage-name", text: stage.label }),
      parts.length === 1 && parts[0] === stage.label ? null : h("span", { class: "stage-parts", text: parts.join(" + ") }));
    if (stage.id === "listen") {
      const level = micLevel();
      if (level !== null) li.append(meter(level));
    }
    chain.append(li);
  }
}

function meter(dbfs) {
  // 12 segments from -60 dBFS to 0 dBFS; the top two warn, the last clips.
  const lit = Math.max(0, Math.min(12, Math.round(((dbfs + 60) / 60) * 12)));
  const bars = Array.from({ length: 12 }, (_, i) =>
    h("i", { "data-on": i < lit || null, "data-hot": i >= 9 || null, "data-clip": i === 11 || null }));
  return h("div", {},
    h("div", { class: "meter", "aria-hidden": "true" }, bars),
    h("span", { class: "meter-label", text: `Last test ${dbfs.toFixed(1)} dBFS` }));
}

function spotlight(ids) {
  const targets = ids.map((id) => document.querySelector(`.unit[data-cid="${CSS.escape(id)}"]`)).filter(Boolean);
  if (!targets.length) return;
  S.spotlight = new Set(ids);
  for (const el of document.querySelectorAll(".unit[data-spotlight]")) el.removeAttribute("data-spotlight");
  for (const el of targets) el.setAttribute("data-spotlight", "");
  targets[0].scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
  clearTimeout(spotlight.timer);
  spotlight.timer = setTimeout(() => {
    S.spotlight = null;
    for (const el of document.querySelectorAll(".unit[data-spotlight]")) el.removeAttribute("data-spotlight");
  }, 2400);
}

// ---------- rack ----------

function renderRack() {
  const rack = $("rack");
  rack.replaceChildren();
  if (S.offline && !S.state) {
    rack.append(h("div", { class: "offline" },
      "The control center isn't answering. In Terminal, run ",
      h("code", { text: "python3 -m gazevoice" }), " in the project folder, then refresh this page."));
    return;
  }
  if (!S.state) {
    for (let i = 0; i < 6; i += 1) {
      rack.append(h("div", { class: "unit skeleton", "aria-hidden": "true" },
        h("div", { class: "unit-id" }, h("span", { class: "bar" }), h("span", { class: "bar short" })),
        h("span", { class: "bar short" }),
        h("div", { class: "readout" }, h("span", { class: "bar" }), h("span", { class: "bar short" })),
        h("span", {})));
    }
    return;
  }
  const placed = new Set();
  for (const group of GROUPS) {
    const units = group.ids.map(component).filter(Boolean);
    if (!units.length) continue;
    units.forEach((c) => placed.add(c.id));
    rack.append(shelf(group.label, group.hint, units));
  }
  const others = components().filter((c) => !placed.has(c.id) && c.id !== "workflow");
  if (others.length) rack.append(shelf("Other", "", others));
}

function shelf(label, hint, units) {
  return h("section", { class: "shelf", "aria-label": label },
    h("div", { class: "shelf-head" }, h("h3", { text: label }), hint ? h("span", { text: hint }) : null),
    units.map(unit));
}

function unit(comp) {
  const health = HEALTH[comp.health] || HEALTH.missing;
  const busy = isBusy(comp.id);
  const working = isWorking(comp.id);
  const armed = S.armed && S.armed.cid === comp.id && S.armed.until > Date.now() ? S.armed : null;
  const flash = S.flash.get(comp.id);

  const specs = comp.details.filter(([k]) => !HIDDEN_DETAILS.has(k));
  const readout = h("div", { class: "readout" },
    armed
      ? h("p", { class: "confirm-text", text: confirmText(comp, armed.aid) })
      : h("p", { class: "summary", text: working ? `${runningLabel(comp.id)}…` : comp.summary }),
    specs.length
      ? h("dl", { class: "specs" }, specs.map(([k, v]) => h("div", {}, h("dt", { text: k }), h("dd", { text: v, title: v }))))
      : null,
    flash ? h("p", { class: "last", "data-tone": flash.tone, text: flash.text }) : lastLine(comp.id));

  return h("article", {
    class: "unit",
    "data-cid": comp.id,
    "data-health": comp.health,
    "data-spotlight": S.spotlight && S.spotlight.has(comp.id) ? "" : null,
    "aria-label": `${comp.name}: ${health.word}`,
  },
    h("div", { class: "unit-id" },
      h("span", { class: "tape", text: comp.name }),
      h("span", { class: "role", text: comp.role })),
    h("div", { class: "unit-status" },
      h("span", { class: "led", "data-led": working ? "busy" : health.led, "aria-hidden": "true" }),
      h("span", { text: working ? "Working" : health.word })),
    readout,
    keys(comp, busy, armed));
}

function runningLabel(cid) {
  const job = runningJob(cid);
  if (job) return `${actionLabel(cid, job.action)} running`;
  return S.workflowStep === cid ? "Being tested by Check everything" : "Working";
}

function confirmText(comp, aid) {
  const spec = comp.actions.find((a) => a.id === aid);
  return spec && spec.confirm ? spec.confirm : `Run “${aid}”?`;
}

function lastLine(cid) {
  const job = lastFinishedJob(cid);
  if (!job) return null;
  const when = ago(job.finished_at || job.started_at);
  const label = actionLabel(cid, job.action);
  if (job.state === "failed") {
    return h("p", { class: "last", "data-tone": "bad", text: `${label} failed ${when}: ${job.error}` });
  }
  return h("p", { class: "last", "data-tone": "ok", text: `${label} · ${resultLine(job)} · ${when}` });
}

function keys(comp, busy, armed) {
  const wrap = h("div", { class: "unit-keys" });
  if (armed) {
    wrap.append(
      h("button", {
        class: "key key-primary",
        type: "button",
        "data-armed": "",
        "data-focus-key": `${comp.id}-confirm`,
        onclick: () => runAction(comp.id, armed.aid),
      }, `Confirm ${actionLabel(comp.id, armed.aid).toLowerCase()}`),
      h("button", {
        class: "key key-quiet",
        type: "button",
        "data-focus-key": `${comp.id}-cancel`,
        onclick: () => disarm(),
      }, "Cancel"));
    return wrap;
  }
  let usesPlan = false;
  const running = runningJob(comp.id);
  for (const action of comp.actions) {
    usesPlan = usesPlan || action.uses_plan;
    const primary = action.id === "install";
    wrap.append(h("button", {
      class: primary ? "key key-primary" : "key",
      type: "button",
      disabled: busy || null,
      "data-focus-key": `${comp.id}-${action.id}`,
      "aria-describedby": action.uses_plan ? `${comp.id}-plan` : null,
      onclick: () => pressKey(comp, action),
    }, running && running.action === action.id ? "Working…" : action.label,
      action.uses_plan ? h("span", { class: "plan-ring", "aria-hidden": "true" }) : null));
  }
  if (usesPlan) {
    wrap.append(h("span", { class: "plan-note", id: `${comp.id}-plan`, text: "uses your plan" }));
  }
  return wrap;
}

function pressKey(comp, action) {
  if (action.confirm) {
    S.armed = { cid: comp.id, aid: action.id, until: Date.now() + ARM_MS };
    clearTimeout(pressKey.timer);
    pressKey.timer = setTimeout(disarm, ARM_MS);
    preservingFocus(renderRack);
    const confirm = document.querySelector(`[data-focus-key="${CSS.escape(`${comp.id}-confirm`)}"]`);
    if (confirm) confirm.focus();
    return;
  }
  runAction(comp.id, action.id);
}

function disarm() {
  if (!S.armed) return;
  const cid = S.armed.cid;
  S.armed = null;
  preservingFocus(renderRack);
  const first = document.querySelector(`.unit[data-cid="${CSS.escape(cid)}"] .key`);
  if (first && document.activeElement === document.body) first.focus();
}

function setFlash(cid, text, tone) {
  S.flash.set(cid, { text, tone });
  setTimeout(() => {
    S.flash.delete(cid);
    preservingFocus(renderRack);
  }, 7000);
}

async function runAction(cid, aid) {
  S.armed = null;
  let response;
  try {
    response = await send("POST", `/api/components/${encodeURIComponent(cid)}/actions/${encodeURIComponent(aid)}`);
  } catch {
    setFlash(cid, "Couldn't reach the control center.", "bad");
    preservingFocus(renderRack);
    return;
  }
  if (response.status === 202 && response.data && response.data.job) {
    const job = response.data.job;
    S.jobs.set(job.id, { ...S.jobs.get(job.id), ...job });
    S.open.add(job.id);
  } else if (response.status === 409) {
    setFlash(cid, "Already working on it. Watch the activity tape.", "bad");
  } else {
    setFlash(cid, (response.data && response.data.error) || `Didn't start (HTTP ${response.status}).`, "bad");
    refresh();
  }
  renderAll();
}

// ---------- voice ----------

function voice() {
  return S.voice || (S.state && S.state.voice) || { state: "off", reason: "", recent: [], missing: [] };
}

function voiceOn(v = voice()) {
  return !["off", "error", "starting"].includes(v.state);
}

function renderVoice() {
  const box = $("voice");
  if (!box) return;
  preservingFocus(() => {
    box.replaceChildren();
    const v = voice();
    const on = voiceOn(v);
    const startingJob = jobRunning("voice");
    const [led, word] = VOICE_WORDS[v.state] || VOICE_WORDS.off;
    const key = v.key || "⌥ Space";
    $("voice-note").textContent = `In Blender, hold ${key}, say what you want, and let go. Your selection is what “this” means.`;
    let reason = v.reason;
    if (!reason && v.state === "ready") reason = `Hold ${key} in Blender, speak, and let go.`;
    if (!reason && v.state === "off") reason = "Turn voice on to start talking to Blender.";
    if (v.state === "ready" && v.blender_connected === false) reason = "Blender isn't connected. Voice picks up again when it's back.";

    const keys = h("div", { class: "voice-keys" });
    if (!on) {
      const missing = v.missing || [];
      const starting = startingJob || v.state === "starting";
      keys.append(h("button", {
        class: "key key-primary",
        type: "button",
        "data-focus-key": "voice-start",
        disabled: starting || missing.length > 0 || null,
        title: missing.length ? "Fix the items listed below first" : "Uses a little of your plan to warm up the brain",
        onclick: () => voicePost("start"),
      }, starting ? "Starting…" : "Start voice", h("span", { class: "plan-ring", "aria-hidden": "true" })));
      if (v.state === "starting") {
        keys.append(h("button", {
          class: "key", type: "button", "data-focus-key": "voice-cancel", onclick: () => voicePost("stop"),
        }, "Cancel"));
      }
    } else {
      keys.append(
        talkKey(),
        h("button", { class: "key", type: "button", "data-focus-key": "voice-undo", onclick: () => voicePost("undo") }, "Undo last"),
        h("button", { class: "key", type: "button", "data-focus-key": "voice-stop", onclick: () => voicePost("stop") }, "Stop voice"));
    }
    box.append(h("div", { class: "transport-top" },
      h("div", { class: "voice-readout", role: "status" },
        h("span", { class: "led", "data-led": led, "aria-hidden": "true" }),
        h("div", { class: "voice-text" },
          h("span", { class: "voice-word", text: word }),
          reason ? h("span", { class: "voice-reason", text: reason }) : null)),
      keys));

    const missing = v.missing || [];
    if (!on && missing.length) {
      box.append(h("ul", { class: "voice-missing", "aria-label": "Before voice can start" },
        h("li", {}, h("strong", { text: "Before voice can start:" })),
        missing.map((m) => h("li", { text: `○ ${m}` }))));
    }
    if (S.voiceError) box.append(h("p", { class: "voice-note", "data-tone": "warn", text: S.voiceError }));
    if (v.needs_restart) {
      box.append(h("p", { class: "voice-note", "data-tone": "warn", text: "Settings changed. Stop and start voice to use them." }));
    }

    if (on) {
      const input = h("input", {
        type: "text",
        value: S.typed,
        placeholder: "Or type a command: make the selected object twice as tall",
        "aria-label": "Type a command",
        "data-focus-key": "voice-type",
        oninput: (e) => { S.typed = e.target.value; },
      });
      const form = h("form", {
        class: "voice-type",
        onsubmit: (e) => {
          e.preventDefault();
          sendTyped();
        },
      }, input, h("button", { class: "key", type: "submit", "data-focus-key": "voice-send" }, "Send"));
      box.append(h("div", { class: "transport-mid" }, form,
        h("div", { class: "voice-aside" },
          h("button", {
            class: "key key-quiet",
            type: "button",
            "data-focus-key": "voice-reset",
            title: "Forget earlier commands, so “it” starts fresh",
            onclick: () => voicePost("reset"),
          }, "New conversation"))));
      box.append(h("p", {
        class: "voice-note",
        text: v.brain === "codex"
          ? "The mic is open while voice is on. Audio stays in memory; the commands you speak are saved in the app's logs folder, and Codex keeps its own history of them."
          : "The mic is open while voice is on. Audio stays in memory; only the commands you speak are saved, in the app's logs folder.",
      }));
    }

    const recent = v.recent || [];
    box.append(recent.length
      ? h("ol", { class: "cmd-list", "aria-label": "Recent commands" }, recent.slice(0, 12).map(commandRow))
      : h("p", {
        class: "cmd-empty",
        text: on
          ? `Nothing yet. Hold ${key} in Blender and say something like “make this twice as tall”.`
          : "Commands you speak show up here: what was heard, what was understood, and what changed.",
      }));
  });
}

function commandRow(r) {
  const led = { done: "live", failed: "fault", stopped: "off" }[r.stage] || "busy";
  let understood = KIND_WORDS[r.kind] || r.refined || r.heard || STAGE_WORDS[r.stage] || "…";
  if (r.kind === "command" && r.refined) understood = r.refined;
  const heard = r.heard ? `${r.source === "typed" ? "Typed" : "Heard"}: “${r.heard}”` : "";
  let tone = null;
  let result = STAGE_WORDS[r.stage] || "";
  if (r.stage === "failed") {
    tone = "bad";
    result = `✗ ${r.error}`;
  } else if (r.stage === "stopped") {
    result = `■ ${r.reply || "Stopped."}`;
  } else if (r.stage === "done") {
    tone = "ok";
    result = r.reply ? `✓ ${r.reply}` : "✓ Done";
  } else if (r.tools && r.tools.length) {
    result += ` · ${r.tools[r.tools.length - 1].replaceAll("_", " ")}`;
  }
  const t = r.timings || {};
  const meta = [
    clock(r.created),
    t.stt != null && `heard ${t.stt}s`,
    t.refine != null && `understood ${t.refine}s`,
    t.edit != null && `edited ${t.edit}s`,
    t.total != null && `total ${t.total}s`,
    r.fallback && "used what was heard",
  ].filter(Boolean).join(" · ");
  const editedBy = r.model ? [modelLabel(r.model), effortLabel(r.effort)].filter(Boolean).join(" · ") : "";
  return h("li", { class: "cmd", "data-stage": r.stage },
    h("span", { class: "led", "data-led": led, "aria-hidden": "true" }),
    h("div", { class: "cmd-main" },
      h("p", { class: "cmd-understood", text: understood }),
      heard ? h("p", { class: "cmd-heard", text: heard }) : null,
      result ? h("p", { class: "cmd-result", "data-tone": tone, text: result }) : null),
    h("div", { class: "cmd-meta" },
      h("span", { text: meta }),
      editedBy ? h("span", { class: "cmd-model", text: editedBy }) : null));
}

function talkKey() {
  return h("button", {
    class: "key key-talk",
    type: "button",
    "data-focus-key": "voice-talk",
    "data-held": S.talkHeld || null,
    "aria-pressed": String(S.talkHeld),
    title: "Press and hold while you speak (or hold Space on this key)",
    onpointerdown: (e) => {
      e.preventDefault();
      pressTalk();
    },
    onkeydown: (e) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        pressTalk();
      }
    },
  }, h("span", { class: "led", "data-led": S.talkHeld ? "rec" : "off", "aria-hidden": "true" }),
  S.talkHeld ? "Listening… release to send" : "Hold to talk");
}

function pressTalk() {
  if (S.talkHeld) return;
  S.talkHeld = true;
  voicePost("ptt", { state: "down" });
  // Releasing sends; anything that ends the hold some other way (the window losing focus,
  // the browser taking the pointer) cancels, so a stuck hold never sends room audio.
  const listening = new AbortController();
  S.talkListeners = listening;
  window.addEventListener("pointerup", releaseTalk, { signal: listening.signal });
  window.addEventListener("pointercancel", cancelTalk, { signal: listening.signal });
  window.addEventListener("blur", cancelTalk, { signal: listening.signal });
  renderVoice();
}

function endTalk(state) {
  if (!S.talkHeld) return;
  S.talkHeld = false;
  if (S.talkListeners) S.talkListeners.abort();
  S.talkListeners = null;
  voicePost("ptt", { state });
  renderVoice();
}

function releaseTalk() {
  endTalk("up");
}

function cancelTalk() {
  endTalk("cancel");
}

async function sendTyped() {
  const text = S.typed.trim();
  if (!text) return;
  S.typed = "";
  const response = await voicePost("say", { text });
  if (!response.ok) {
    S.typed = text;
    renderVoice();
  }
}

async function voicePost(name, body) {
  S.voiceError = null;
  let response;
  try {
    response = await send("POST", `/api/voice/${name}`, body || {});
  } catch {
    S.voiceError = "Couldn't reach the control center.";
    renderVoice();
    return { ok: false };
  }
  if (response.status === 409 && response.data && response.data.missing) {
    S.voice = { ...voice(), missing: response.data.missing };
  } else if (!response.ok) {
    S.voiceError = (response.data && response.data.error) || `That didn't work (HTTP ${response.status}).`;
  } else if (name === "stop" && response.data) {
    S.voice = { ...response.data, missing: [] };
    refresh();
  } else if (name === "start" && response.data && response.data.job) {
    const job = response.data.job;
    S.jobs.set(job.id, job);
    S.open.add(job.id);
    renderLog();
  }
  renderVoice();
  return response;
}

function upsertCommand(record) {
  const v = voice();
  const recent = (v.recent || []).filter((r) => r.id !== record.id);
  recent.unshift(record);
  recent.sort((a, b) => b.created - a.created);
  S.voice = { ...v, recent: recent.slice(0, 20) };
}

// ---------- routing (settings) ----------

function renderRouting() {
  const box = $("routing");
  box.replaceChildren();
  const settings = S.state ? S.state.settings : null;
  if (!settings) {
    box.append(h("p", { class: "route-note", text: "Loading settings…" }));
    return;
  }
  const brainNote = {
    auto: "Auto uses Claude Code when it's logged in, otherwise Codex.",
    claude: "Always use Claude Code.",
    codex: "Always use Codex.",
  }[settings.brain];

  box.append(
    routeRow("Brain", "brain", settings.brain, [["auto", "Auto"], ["claude", "Claude Code"], ["codex", "Codex"]]),
    routeRow("Speech engine", "stt_engine", settings.stt_engine, [["parakeet", "Parakeet"], ["whisper", "Whisper"]]),
    inputRow(settings.mic_device),
    ...brainRows(settings),
    h("p", { class: "route-note", id: "route-note", text: brainNote || "" }));
}

function brainRows(settings) {
  if (S.state && S.state.brain === "codex") {
    return [h("div", { class: "route-row" },
      h("span", { class: "route-label", text: "Editing and cleanup · Codex" }),
      h("p", { class: "route-note", text: "Codex uses the model and reasoning level set in its own config." }))];
  }
  return [
    modelRow("Editing", "editor_model", "editor_effort", settings),
    modelRow("Cleanup", "refiner_model", "refiner_effort", settings),
    h("p", { class: "route-note", text: "More thinking can make big builds better, but each command takes longer." }),
  ];
}

function modelRow(role, modelKey, effortKey, settings) {
  const id = `route-${modelKey}`;
  const menu = (key, options, what) => h("select", {
    class: "select",
    "aria-label": `${role} ${what}`,
    "data-focus-key": `route-${key}`,
    onchange: (e) => saveSetting(key, e.target.value),
  }, options.map(([value, label]) => h("option", { value, selected: value === settings[key] || null, text: label })));
  return h("div", { class: "route-row" },
    h("span", { class: "route-label", id, text: `${role} · Claude Code` }),
    h("div", { class: "route-pair", role: "group", "aria-labelledby": id },
      menu(modelKey, CLAUDE_MODELS, "model"),
      menu(effortKey, EFFORTS, "thinking")));
}

function routeRow(label, key, value, options) {
  const id = `route-${key}`;
  return h("div", { class: "route-row" },
    h("span", { class: "route-label", id, text: label }),
    h("div", { class: "seg", role: "radiogroup", "aria-labelledby": id },
      options.map(([optionValue, optionLabel]) => h("button", {
        class: "key",
        type: "button",
        role: "radio",
        "aria-checked": String(optionValue === value),
        "data-focus-key": `${id}-${optionValue}`,
        onclick: () => saveSetting(key, optionValue),
      }, optionLabel))));
}

function inputRow(current) {
  const mic = component("microphone");
  const detail = (key) => (mic ? (mic.details.find(([k]) => k === key) || [])[1] : null);
  const devicesText = detail("Devices");
  const devices = devicesText
    ? devicesText.split(" | ").map((part) => {
      const at = part.indexOf("=");
      return [part.slice(0, at), part.slice(at + 1)];
    })
    : [];
  if (current && !devices.some(([index]) => index === current)) devices.unshift([current, "not connected"]);
  const using = detail("Using");
  const autoLabel = current === "" && using ? `Auto · ${using.replace(/ \(auto\)$/, "")}` : "Auto · built-in mic first";
  return h("label", { class: "route-row" },
    h("span", { class: "route-label", text: "Microphone input" }),
    h("select", {
      class: "select",
      "data-focus-key": "route-mic",
      onchange: (e) => saveSetting("mic_device", e.target.value),
    },
      h("option", { value: "", selected: current === "" || null, text: autoLabel }),
      devices.map(([index, name]) => h("option", { value: index, selected: index === current || null, text: `${index} · ${name}` }))));
}

async function saveSetting(key, value) {
  const note = $("route-note");
  try {
    const response = await send("PUT", "/api/settings", { [key]: value });
    if (!response.ok) throw new Error((response.data && response.data.error) || `HTTP ${response.status}`);
    if (S.state) S.state.settings = response.data;
    preservingFocus(renderRouting);
    refresh();
  } catch (error) {
    if (note) {
      note.textContent = `Couldn't save: ${error.message}`;
      note.dataset.tone = "bad";
    }
  }
}

// ---------- activity tape ----------

function renderLog() {
  const list = $("log");
  list.replaceChildren();
  const jobs = [...S.jobs.values()].sort((a, b) => b.started_at - a.started_at).slice(0, 25);
  if (!jobs.length) {
    list.append(h("li", { class: "log-empty" },
      "Nothing has run yet. Press a key on any unit: installs and tests stream here line by line."));
    return;
  }
  for (const job of jobs) list.append(entry(job));
}

function entry(job) {
  const open = S.open.has(job.id);
  const led = job.state === "running" ? "busy" : job.state === "failed" ? "fault" : "live";
  const title = `${nameOf(job.component_id)} · ${actionLabel(job.component_id, job.action)}`;
  const when = job.state === "running" ? `${clock(job.started_at)} · running` : `${clock(job.started_at)} · ${duration(job)}`;
  const body = h("pre", { class: "entry-body", "data-job": job.id, hidden: !open || null });
  body.textContent = job.lines.join("\n") || "Waiting for output…";
  return h("li", { class: "entry", "data-state": job.state },
    h("button", {
      class: "entry-head",
      type: "button",
      "aria-expanded": String(open),
      "data-focus-key": `job-${job.id}`,
      onclick: () => {
        if (S.open.has(job.id)) S.open.delete(job.id);
        else S.open.add(job.id);
        preservingFocus(renderLog);
      },
    },
      h("span", { class: "led", "data-led": led, "aria-hidden": "true" }),
      h("span", { class: "entry-title", text: title }),
      h("span", { class: "entry-time", text: when })),
    job.state === "failed" && job.error ? h("p", { class: "entry-error", text: job.error }) : null,
    job.state === "succeeded" && job.result ? h("p", { class: "entry-result", text: resultLine(job) }) : null,
    body);
}

function appendLine(job, line) {
  const pre = document.querySelector(`pre[data-job="${CSS.escape(job.id)}"]`);
  if (!pre) return;
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  if (pre.textContent === "Waiting for output…") pre.textContent = line;
  else pre.textContent += `\n${line}`;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

// The workflow check logs "▶ id.action", "✓ id.action: …" or "✗ id.action: …" per step.
function trackWorkflow(job, line) {
  if (job.component_id !== "workflow") return;
  const match = /^([▶✓✗])\s+([a-z_]+)\.([a-z_]+)/.exec(line);
  if (!match) return;
  const status = { "▶": "testing", "✓": "passed", "✗": "failed" }[match[1]];
  for (const stage of STEP_STAGES[match[2]] || []) S.stageTests.set(stage, status);
  S.workflowStep = status === "testing" ? match[2] : null;
  preservingFocus(() => {
    renderChain();
    renderRack();
  });
}

// ---------- live updates ----------

function connectEvents() {
  const source = new EventSource("/api/events");
  let wasDown = false;
  source.addEventListener("open", () => {
    S.link = "live";
    renderLink();
    if (wasDown) {
      wasDown = false;
      loadJobs();
      refresh();
    }
  });
  source.addEventListener("error", () => {
    S.link = "reconnecting";
    wasDown = true;
    renderLink();
  });
  source.addEventListener("job.started", (event) => {
    const { job } = JSON.parse(event.data);
    S.jobs.set(job.id, job);
    S.open.add(job.id);
    if (job.component_id === "workflow") S.stageTests.clear();
    renderAll();
  });
  source.addEventListener("job.log", (event) => {
    const { job_id: jobId, line } = JSON.parse(event.data);
    const job = S.jobs.get(jobId);
    if (!job) return;
    job.lines.push(line);
    if (job.lines.length > MAX_LINES) job.lines.splice(0, job.lines.length - MAX_LINES);
    appendLine(job, line);
    trackWorkflow(job, line);
  });
  source.addEventListener("job.finished", (event) => {
    const { job } = JSON.parse(event.data);
    S.jobs.set(job.id, job);
    const what = `${nameOf(job.component_id)} ${actionLabel(job.component_id, job.action).toLowerCase()}`;
    announce(job.state === "failed" ? `${what} failed: ${job.error}` : `${what} finished`);
    if (job.component_id === "workflow") {
      S.workflowStep = null;
      setTimeout(() => { S.stageTests.clear(); renderChain(); }, 12000);
    }
    renderAll();
    refresh();
  });
  source.addEventListener("voice.state", (event) => {
    const data = JSON.parse(event.data);
    const wasOn = voiceOn();
    S.voice = { ...voice(), ...data };
    if (voiceOn() !== wasOn) S.voice.missing = [];
    renderVoice();
  });
  source.addEventListener("voice.command", (event) => {
    upsertCommand(JSON.parse(event.data));
    renderVoice();
  });
  source.addEventListener("component.updated", (event) => {
    // A slow check finished after the page got its snapshot.
    const late = JSON.parse(event.data);
    const shown = component(late.id);
    if (!shown || shown.health !== late.health || shown.summary !== late.summary) {
      clearTimeout(connectEvents.lateTimer);
      connectEvents.lateTimer = setTimeout(refresh, 400);
    }
  });
  source.addEventListener("settings.updated", (event) => {
    if (S.state) S.state.settings = JSON.parse(event.data);
    preservingFocus(renderRouting);
  });
}

async function loadJobs() {
  try {
    const { jobs } = await getJSON("/api/jobs");
    for (const job of jobs) S.jobs.set(job.id, job);
  } catch {
    // The state refresh reports the outage.
  }
  renderLog();
}

let inFlight = null;
// fresh=true (the Refresh key) re-runs every probe; otherwise the server may reuse
// answers from the last few seconds, which keeps a busy Mac responsive.
function refresh(fresh = false) {
  if (inFlight) return inFlight;
  const button = $("refresh");
  button.disabled = true;
  button.textContent = "Checking…";
  inFlight = (async () => {
    try {
      S.state = await getJSON(fresh ? "/api/state?fresh=1" : "/api/state");
      S.voice = S.state.voice || null;
      S.offline = false;
    } catch {
      S.offline = true;
    } finally {
      S.loading = false;
      inFlight = null;
      button.disabled = false;
      button.textContent = "Refresh";
      renderAll();
      scheduleRecheck();
    }
  })();
  return inFlight;
}

// A unit that's still checking (a busy Mac) gets looked at again soon, not in 45 s.
function scheduleRecheck() {
  clearTimeout(scheduleRecheck.timer);
  const pending = components().some((c) => c.health === "checking");
  if (pending && !SNAPSHOT) scheduleRecheck.timer = setTimeout(refresh, 4000);
}

function renderAll() {
  preservingFocus(() => {
    renderHeader();
    renderChain();
    renderVoice();
    renderRack();
    renderRouting();
    renderLog();
  });
}

function start() {
  $("refresh").addEventListener("click", () => refresh(true));
  $("check-all").addEventListener("click", () => {
    const workflow = component("workflow");
    const spec = workflow && workflow.actions.find((a) => a.id === "check");
    if (!spec) return;
    const armed = S.armed && S.armed.cid === "workflow" && S.armed.until > Date.now();
    if (spec.confirm && !armed) {
      S.armed = { cid: "workflow", aid: "check", until: Date.now() + ARM_MS };
      clearTimeout(pressKey.timer);
      pressKey.timer = setTimeout(() => { disarm(); renderHeader(); }, ARM_MS);
      renderAll();
      return;
    }
    runAction("workflow", "check");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && S.talkHeld) {
      cancelTalk();
    } else if (event.key === "Escape" && S.armed) {
      disarm();
      renderHeader();
    }
  });
  document.addEventListener("keyup", (event) => {
    if (S.talkHeld && (event.key === " " || event.key === "Enter")) releaseTalk();
  });

  renderAll();
  loadJobs();
  refresh();
  if (SNAPSHOT) return;

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, REFRESH_MS);
  setInterval(() => {
    // Keep "2 min ago" labels honest without refetching.
    if (!S.armed) preservingFocus(renderRack);
  }, 30000);
  connectEvents();
}

start();
