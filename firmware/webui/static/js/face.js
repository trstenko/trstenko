// Trstenko's face, drawn from the same shapes the screen uses (the device
// serves them from faces.json at /api/v1/faces). The screen's own animation
// tracks stay on the screen; here the shapes sit at rest, the eyes blink
// now and then when the device is idle, and the level of the voice or the
// microphone makes the eyes and mouth follow it. Under prefers-reduced-motion
// the face is a still picture of the current state.
//
// The face goes inside a `.screen` panel, which is dark in both themes: it is
// the device's screen, not part of the page.

import {api, hasToken} from "/js/core.js";

const NS = "http://www.w3.org/2000/svg";

// The idle eyes, for the one place the device cannot be asked yet: the lock
// screen, before pairing. Copied from face_idle in faces.json; everything else
// is read from the device.
const RESTING = [
  {name: "eye_l", x: 92, y: 106, w: 70, h: 92, r: 27},
  {name: "eye_r", x: 228, y: 106, w: 70, h: 92, r: 27},
];
const CLOSED = 0.12;      // how far an eye closes in a blink, or asleep

let cache = null;
async function shapes() {
  if (cache) return cache;
  if (!hasToken()) return {faces: {}};
  const r = await api("GET", "/api/v1/faces");
  if (r.ok) cache = r.data;
  return cache || {faces: {}};
}

const still = matchMedia("(prefers-reduced-motion: reduce)");

/** `label` is the text alternative ("Trstenko is listening"); pass a new one
 *  with `set()` as the state changes. */
export function createFace(label) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 320 240");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label || "");
  let state = "";
  let level = 0;
  let nodes = [];
  let asleep = false;
  let blinkUntil = 0;
  let nextBlink = performance.now() + 2500;
  let frame = 0;

  function build(list) {
    svg.replaceChildren();
    nodes = [];
    for (const s of list) {
      const rect = document.createElementNS(NS, "rect");
      svg.append(rect);
      nodes.push({rect, s});
    }
    paint(performance.now());
  }

  function paint(now) {
    let blink = asleep ? CLOSED : 1;
    if (!asleep && !still.matches && state === "face_idle") {
      if (now > nextBlink) { blinkUntil = now + 130; nextBlink = now + 2500 + Math.random() * 3500; }
      if (now < blinkUntil) blink = CLOSED;
    }
    for (const {rect, s} of nodes) {
      let height = s.h;
      if (s.name.startsWith("eye")) {
        height = s.h * blink * (state === "face_listening" ? 1 + level * 0.25 : 1);
      } else if (s.name === "mouth" && state === "face_talking") {
        height = s.h * (0.45 + level * 0.8);
      }
      height = Math.max(height, 2);
      rect.setAttribute("x", s.x - s.w / 2);
      rect.setAttribute("y", s.y - height / 2);
      rect.setAttribute("width", s.w);
      rect.setAttribute("height", height);
      rect.setAttribute("rx", Math.min(s.r, height / 2));
    }
  }

  function loop(now) {
    paint(now);
    frame = requestAnimationFrame(loop);
  }

  return {
    el: svg,
    /** Show the device's face `name` ("face_idle", "face_talking", ...). */
    async set(name, newLevel, newLabel) {
      level = newLevel || 0;
      if (newLabel) svg.setAttribute("aria-label", newLabel);
      if (name !== state || asleep) {
        state = name;
        asleep = false;
        const data = await shapes();
        if (state !== name) return;       // a newer state arrived meanwhile
        build(data.faces[name] || data.faces.face_idle || RESTING);
      }
      if (still.matches) paint(performance.now());
    },
    /** Eyes closed: the face before the page is paired. */
    sleep() {
      asleep = true;
      state = "";
      build(RESTING);
    },
    start() { if (!still.matches && !frame) frame = requestAnimationFrame(loop); },
    stop() { cancelAnimationFrame(frame); frame = 0; },
  };
}

/** The face inside its screen panel. `size` is a class from app.css
 *  ("home", "mini", "tiny", "lock", "setup"). */
export function facePanel(face, size) {
  const el = document.createElement("div");
  el.className = "screen " + size;
  el.append(face.el);
  return el;
}
