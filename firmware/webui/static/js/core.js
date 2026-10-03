// The small things every screen uses: building elements safely, talking to the
// device, the token, the strings, and the live stream.
//
// Nothing here assumes the page is served by the device: every path is relative
// to the origin and the token lives in this one module, so the same code can run
// inside an app wrapper later (plan 07).

// ---- elements ----
// Text is only ever set with textContent, never as HTML: a memory or a reminder
// is whatever somebody said aloud, and it must not be able to run anything.

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function clear(el) { el.replaceChildren(); return el; }

let toastTimer = null;
export function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

// ---- the token ----
// Session storage, not local: closing the tab un-pairs, which is the right
// default for a page anyone on the same Wi-Fi can open.

let token = "";
try { token = sessionStorage.getItem("trstenko-token") || ""; } catch (e) { /* private mode */ }

export const hasToken = () => !!token;
export function setToken(value) {
  token = value || "";
  try {
    if (token) sessionStorage.setItem("trstenko-token", token);
    else sessionStorage.removeItem("trstenko-token");
  } catch (e) { /* private mode */ }
}

const lockListeners = new Set();
export const onLocked = (fn) => { lockListeners.add(fn); return () => lockListeners.delete(fn); };

// ---- the device ----

/** Resolves to {ok, status, data}. Never rejects: a lost connection is
 *  {ok:false, status:0, data:{error}} so every screen handles it the same way. */
export async function api(method, path, body) {
  const init = {method, headers: {}};
  if (token) init.headers["Authorization"] = "Bearer " + token;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    return {ok: false, status: 0, data: {error: t("lost_contact")}};
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* an empty body */ }
  if (res.status === 401 && path !== "/api/auth") {
    setToken("");
    lockListeners.forEach((fn) => fn());
  }
  return {ok: res.ok, status: res.status, data};
}

export async function pair(pin) {
  const r = await api("POST", "/api/auth", {pin});
  if (r.ok && r.data.token !== undefined) setToken(r.data.token);
  return r;
}

// ---- strings ----

let strings = {};
let fallback = {};
export let lang = "en";

export async function loadStrings(wanted) {
  const get = async (name) => {
    try { return await (await fetch("/i18n/" + name + ".json")).json(); }
    catch (e) { return {}; }
  };
  lang = wanted === "sl" ? "sl" : "en";
  fallback = await get("en");
  strings = lang === "en" ? fallback : await get(lang);
  document.documentElement.lang = lang;
}

export function t(key, vars) {
  let s = strings[key] ?? fallback[key] ?? key;
  for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(v);
  return s;
}

// ---- formatting ----

/** Dollars. Small amounts keep a third decimal, since a day on a free tier
 *  is a fraction of a cent and "$0.00" would say nothing. */
export function money(usd, places) {
  if (usd == null) return "—";
  if (places == null) places = usd > 0 && usd < 0.1 ? 3 : 2;
  return "$" + usd.toFixed(places);
}

/** A countdown the way a kitchen timer shows it: 08:42, or 1:05:00. */
export function countdown(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const two = (n) => String(n).padStart(2, "0");
  const h = Math.floor(s / 3600);
  return (h ? h + ":" : "") + two(Math.floor((s % 3600) / 60)) + ":" + two(s % 60);
}

/** How long a timer was set for, in words: "10 min", "1 h 30 min", "45 s". */
export function span(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return s + " s";
  const m = Math.round(s / 60);
  if (m < 60) return m + " min";
  return Math.floor(m / 60) + " h" + (m % 60 ? " " + (m % 60) + " min" : "");
}

function fmt(epoch, options, tz) {
  try {
    return new Date(epoch * 1000).toLocaleString(lang, Object.assign({timeZone: tz || undefined}, options));
  } catch (e) {
    return new Date(epoch * 1000).toLocaleString(lang, options);
  }
}

export const clockTime = (epoch, tz) => fmt(epoch, {hour: "2-digit", minute: "2-digit", hour12: false}, tz);
export const longDay = (epoch, tz) => fmt(epoch, {weekday: "long", day: "numeric", month: "long"}, tz);
export const shortDay = (epoch, tz) => fmt(epoch, {weekday: "short", day: "numeric", month: "short"}, tz);
/** YYYY-MM-DD in the device's time zone, to tell today from tomorrow. */
export const dayKey = (epoch, tz) => fmt(epoch, {year: "numeric", month: "2-digit", day: "2-digit"}, tz);

/** "Today at 18:30", "Tomorrow at 08:00", "Mon 5 Oct at 06:50". `now` is the
 *  device's clock, which is the one the reminder will go by. */
export function when(epoch, tz, now) {
  if (!epoch) return "";
  const at = clockTime(epoch, tz);
  now = now || Date.now() / 1000;
  const key = dayKey(epoch, tz);
  let day = shortDay(epoch, tz);
  if (key === dayKey(now, tz)) day = t("today");
  else if (key === dayKey(now + 86400, tz)) day = t("tomorrow");
  else if (key === dayKey(now - 86400, tz)) day = t("yesterday");
  return t("day_at", {day, time: at});
}

/** Which of the repeat strings a job's repeat is: once, daily, weekdays, weekly. */
export const repeatKey = (spec) => (!spec ? "once" : spec.startsWith("weekly") ? "weekly" : spec);

/** The first sentence, and the rest: a banner says the first one in bold. */
export function lead(text) {
  const m = /^(.*?[.!?])\s+(.+)$/.exec(text || "");
  return m ? [m[1], m[2]] : [text || "", ""];
}

// ---- the live stream ----
// A long fetch with the token in a header (EventSource cannot send one). If the
// stream is refused or drops, the page falls back to asking once a second and
// tries the stream again later.

const subscribers = new Set();
let latest = null;
let running = false;

// Whether the device is answering, for the "Connected" mark.
const linkListeners = new Set();
let linked = true;
export function onLink(fn) { linkListeners.add(fn); fn(linked); return () => linkListeners.delete(fn); }
function setLink(up) {
  if (up === linked) return;
  linked = up;
  linkListeners.forEach((fn) => fn(up));
}

export function subscribe(fn) {
  subscribers.add(fn);
  if (latest) fn(latest);
  start();
  return () => { subscribers.delete(fn); };
}

function publish(data) {
  latest = data;
  setLink(true);
  subscribers.forEach((fn) => fn(data));
}

async function start() {
  if (running) return;
  running = true;
  while (subscribers.size && hasToken()) {
    const streamed = await stream();
    // Poll for a while when the stream is not available, then try it again.
    for (let i = 0; i < (streamed ? 2 : 20) && subscribers.size && hasToken(); i++) {
      const r = await api("GET", "/api/v1/live");
      if (r.ok) publish(r.data);
      else if (r.status === 0) setLink(false);
      await new Promise((go) => setTimeout(go, streamed ? 1500 : 1000));
    }
  }
  running = false;
}

async function stream() {
  let res;
  try {
    res = await fetch("/api/v1/events", {headers: {Authorization: "Bearer " + token}});
  } catch (e) { return false; }
  if (!res.ok || !res.body) return false;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (subscribers.size) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let at;
      while ((at = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, at);
        buffer = buffer.slice(at + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (line) { try { publish(JSON.parse(line.slice(6))); } catch (e) { /* skip */ } }
      }
    }
  } catch (e) { /* the page or the device went away */ }
  try { await reader.cancel(); } catch (e) { /* closed already */ }
  return true;
}
