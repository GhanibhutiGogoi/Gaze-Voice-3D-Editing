// The calibration page: it shows an outline to line the head up with, then the dots, for the job in components/gaze.py,
// and says when each dot is up. The camera stays with the gaze worker. This page never touches it and is never sent a
// picture, only numbers for where to put a ring. If the page closes, the job turns the camera off.

"use strict";

const $ = (id) => document.getElementById(id);
const S = { stage: "ready", job: null, early: null, source: null, watch: null, beat: null, missed: 0 };
const ACTION = "/api/components/gaze/actions/calibrate";
const ARRIVE_MS = 350;
const WATCH_MS = 1500;
const BEAT_MS = 400;  // during the guide, the page says it is still there this often: the job waits no longer than 3 s
const MISSED_LOOKS = 3;  // looks at the job in a row that got no answer: after this many, the control center has stopped
const STOPPED = { state: "failed", error: "The control center stopped, so the calibration did too. Nothing was saved. Start it again, then try again." };
// What a run found out, worth showing whichever way it ended.
const MEASURED = ["The camera sent", "Readings kept", "Your head moved"];

// The head guide. The outline in the page is a 100 by 100 drawing shown as tall as OUTLINE.height of the page (or
// OUTLINE.maxWidth of its width, if that is less). Its head is an ellipse OUTLINE.head wide and tall as shares of that
// drawing, and its eyes are OUTLINE.eyes down it. These are the numbers in calibrate.html and calibrate.css.
const OUTLINE = { height: 0.7, maxWidth: 0.9, eyes: 0.43, head: { w: 0.36, h: 0.48 } };
// Where the middle of the eyes belongs in the mirrored picture (GUIDE_TARGET in components/gaze.py). The ring is drawn
// where that places it on the outline, and moves from there as the head does.
const GUIDE_AT = [0.5, 0.45];
const RING_SCALE = [0.4, 2.5];  // a head can't look smaller or bigger than this in the ring

// Where this page sits on the screen, in the points macOS uses. A click knows its place both on the screen and in the
// page, so the difference is exact, wherever the browser's bars or the notch have put the page.
function pageArea(event, win, scr, fullscreen) {
  const zoom = fullscreen && win.innerWidth > 0 ? scr.width / win.innerWidth : 1;
  const clicked = event.detail > 0;  // a key press has no place to measure from
  return {
    left: clicked ? event.screenX - event.clientX * zoom : win.screenX,
    top: clicked ? event.screenY - event.clientY * zoom : win.screenY + win.outerHeight - win.innerHeight * zoom,
    width: win.innerWidth * zoom,
    height: win.innerHeight * zoom,
  };
}

// The ring that stands for the head, from a reading of the `gaze.guide` event: its middle and size in the page's pixels,
// or null when there is no face to show. x and y are shares of the mirrored picture; scale is 1 for a head of the size
// the outline stands for.
function guideRing(data, width, height) {
  if (!data.found) return null;
  const box = Math.min(OUTLINE.height * height, OUTLINE.maxWidth * width);
  const scale = Math.min(RING_SCALE[1], Math.max(RING_SCALE[0], data.scale));
  const eyes = height / 2 - (0.5 - OUTLINE.eyes) * box;  // where the outline's eyes are, down the page
  return {
    x: Math.min(width, Math.max(0, width / 2 + (data.x - GUIDE_AT[0]) * width)),
    y: Math.min(height, Math.max(0, eyes + (data.y - GUIDE_AT[1]) * height)),
    w: OUTLINE.head.w * box * scale,
    h: OUTLINE.head.h * box * scale,
  };
}

function resultText(job) {
  const lines = (job.lines || []).filter((line) => MEASURED.some((start) => line.startsWith(start)));
  if (job.state !== "succeeded") {
    // A run that got through the dots but wasn't kept says so in its result. One that stopped part way doesn't.
    const refused = Boolean(job.result && job.result.refused);
    return { title: refused ? "Not calibrated" : "Calibration didn't finish", text: job.error || "Something went wrong.", lines };
  }
  const { median, p90 } = job.result || {};
  return {
    title: "Calibrated",
    text: `Typically off by ${Math.round(median * 100)}% of your screen, and 9 times in 10 within ${Math.round(p90 * 100)}%. `
      + "Press Test on the Gaze unit, with Blender open, to see it follow your eyes.",
    lines,
  };
}

function setStage(stage) {
  S.stage = stage;
  document.body.dataset.stage = stage;
  $("cal-card").hidden = stage !== "ready" && stage !== "result";
  $("cal-begin").hidden = stage !== "begin";
  $("cal-wait").hidden = stage !== "waiting";
  $("cal-guide").hidden = stage !== "guide";
  $("cal-dot").hidden = stage !== "dots";
  $("cal-progress").hidden = stage !== "dots";
  if (stage !== "guide") {
    clearInterval(S.beat);
    S.beat = null;
  }
}

function post(path, body, keepalive = false) {
  return fetch(path, {
    method: "POST",
    keepalive,
    headers: { "X-GazeVoice": "1", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// A hidden page says nothing, even though its timers still fire: that silence is how the job learns the page has gone and
// turns the camera off. (The single "left" sent as the page is hidden can be lost, or arrive before the job is ready.)
function sayAlive() {
  if (document.hidden) return;
  post("/api/gaze/page", { job: S.job, alive: true }).catch(() => {});
}

function startBeat() {
  if (document.hidden) return;
  sayAlive();
  S.beat = setInterval(sayAlive, BEAT_MS);
}

// A reading of where the head is. The first one opens the guide; it then keeps saying the page is still there.
function showGuide(guide) {
  if (S.stage !== "guide") {
    setStage("guide");
    $("guide-skip").disabled = false;
    startBeat();
  }
  const ring = guideRing(guide, window.innerWidth, window.innerHeight);
  const shape = $("guide-ring");
  shape.hidden = ring === null;
  if (ring) {
    shape.style.left = `${ring.x}px`;
    shape.style.top = `${ring.y}px`;
    shape.style.width = `${ring.w}px`;
    shape.style.height = `${ring.h}px`;
  }
  $("cal-guide").classList.toggle("fits", guide.fits === true);
  if ($("guide-advice").textContent !== guide.advice) $("guide-advice").textContent = guide.advice;  // read out only when it changes
}

function skipGuide() {
  if (!S.job) return;
  $("guide-skip").disabled = true;
  post("/api/gaze/page", { job: S.job, skip: true })
    .then((reply) => { if (!reply.ok) $("guide-skip").disabled = false; })
    .catch(() => { $("guide-skip").disabled = false; });
}

function showDot(dot) {
  setStage("dots");
  const shape = $("cal-dot");
  shape.style.left = `${dot.uv[0] * 100}%`;
  shape.style.top = `${dot.uv[1] * 100}%`;
  $("cal-bar").style.width = `${(dot.number / dot.total) * 100}%`;
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const centred = "translate(-50%, -50%)";
    shape.animate([{ transform: `${centred} scale(2.2)` }, { transform: `${centred} scale(1)` }],
      { duration: ARRIVE_MS, easing: "ease-out" });  // it catches the eye, and has settled before readings start
  }
  // Two frames later it has really been drawn. A hidden page draws nothing, so the job hears nothing and stops.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    post("/api/gaze/page", { job: dot.job_id, number: dot.number }).catch(() => {});
  }));
}

function finish(job) {
  if (S.stage === "result") return;
  S.job = null;
  clearInterval(S.watch);
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  const { title, text, lines } = resultText(job);
  $("cal-title").textContent = title;
  $("cal-text").textContent = text;
  $("cal-lines").replaceChildren(...lines.map((line) => Object.assign(document.createElement("li"), { textContent: line })));
  $("cal-lines").hidden = lines.length === 0;
  $("cal-start").textContent = job.state === "succeeded" ? "Calibrate again" : "Try again";
  $("cal-note").hidden = true;
  setStage("result");
}

// What one look at the job came to. `status` is the HTTP status of the reply, or 0 if nothing answered; `job` is its body,
// if it was one. Returns how many looks in a row have found nothing, and, when that is the end of it, the job to show.
// A control center that doesn't know the job has restarted (404), and one that has said nothing for a few looks has
// stopped. Either way the job is over, and the page must not sit on a dot for ever.
function lookedAt(status, job, missed) {
  if (status === 404) return { missed: MISSED_LOOKS, over: STOPPED };
  if (status === 200 && job) return { missed: 0, over: job.state === "running" ? null : job };
  missed += 1;
  return { missed, over: missed >= MISSED_LOOKS ? STOPPED : null };
}

function listen() {
  if (S.source) return;
  S.source = new EventSource("/api/events");
  S.source.addEventListener("gaze.guide", (event) => {
    const guide = JSON.parse(event.data);
    // One that beat the reply saying which job this is, or came after the dots began, is dropped: another follows.
    if (guide.job_id === S.job && (S.stage === "waiting" || S.stage === "guide")) showGuide(guide);
  });
  S.source.addEventListener("gaze.dot", (event) => {
    const dot = JSON.parse(event.data);
    if (dot.job_id === S.job) showDot(dot);
    else if (S.job === null) S.early = dot;  // it beat the reply that says which job this is
  });
  S.source.addEventListener("job.finished", (event) => {
    const { job } = JSON.parse(event.data);
    if (job.id === S.job) finish(job);
  });
}

function leave() {
  if (S.job) post("/api/gaze/page", { job: S.job, left: true }, true).catch(() => {});
}

async function start() {
  listen();
  try {
    await document.documentElement.requestFullscreen();
  } catch {
    // it still works in a window, just over less of the screen
  }
  setStage("begin");
  $("cal-begin").focus();
}

async function begin(event) {
  const area = pageArea(event, window, screen, Boolean(document.fullscreenElement));
  S.early = null;
  S.missed = 0;
  setStage("waiting");
  let response;
  try {
    response = await post(ACTION, { area });
  } catch {
    finish({ state: "failed", error: "Couldn't reach the control center." });
    return;
  }
  const data = await response.json().catch(() => null);
  if (response.status !== 202 || !data || !data.job) {
    finish({ state: "failed", error: (data && data.error) || `It didn't start (HTTP ${response.status}).` });
    return;
  }
  S.job = data.job.id;
  // The events can drop, so the job is watched as well: it never leaves this page waiting for ever.
  S.watch = setInterval(async () => {
    const reply = await fetch(`/api/jobs/${S.job}`).catch(() => null);
    const job = reply && reply.ok ? await reply.json().catch(() => null) : null;
    const look = lookedAt(reply ? reply.status : 0, job, S.missed);
    S.missed = look.missed;
    if (look.over) finish(look.over);
  }, WATCH_MS);
  if (S.early && S.early.job_id === S.job) showDot(S.early);
}

async function load() {
  const reply = await fetch("/api/state").catch(() => null);
  const state = reply && reply.ok ? await reply.json().catch(() => null) : null;
  const gaze = state && state.components.find((component) => component.id === "gaze");
  if (gaze && gaze.health !== "checking" && !gaze.actions.some((action) => action.id === "calibrate")) {
    $("cal-text").textContent = gaze.summary;
    $("cal-start").disabled = true;
    $("cal-note").hidden = true;
  }
}

$("cal-start").addEventListener("click", start);
$("cal-begin").addEventListener("click", begin);
$("guide-skip").addEventListener("click", skipGuide);
document.addEventListener("fullscreenchange", () => {
  if (document.fullscreenElement) return;
  if (S.stage === "begin") setStage("ready");
  else leave();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) leave();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (S.stage === "begin") setStage("ready");
  else leave();
});
addEventListener("pagehide", leave);
load();
