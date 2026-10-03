// Reminders and timers: what was said aloud, readable and fixable.

import {api, clear, clockTime, countdown, dayKey, h, longDay, repeatKey, span, t, toast, when} from "/js/core.js";
import {busy, emptyRow, field, head, input, sectionLabel, seg} from "/js/ui.js";

export async function render(root) {
  const ringing = h("div");
  const lists = h("div", {class: "stack"});

  // ---- adding one ----
  // "In" some minutes is a timer with a name: it counts down, works before the
  // device knows the time, and cannot repeat. "At a time" is a reminder.
  const what = input({type: "text", maxlength: "120", placeholder: t("reminder_example"), autocomplete: "off"});
  const minutes = input({type: "number", class: "input input-short", min: "1", max: "1440", value: "15",
    "aria-label": t("minutes_label")});
  const date = input({type: "date", "aria-label": t("date")});
  const time = input({type: "time", class: "input input-time", "aria-label": t("time")});
  const inBox = h("div", {class: "input-group"}, minutes, h("span", {class: "unit"}, t("minutes")));
  const atBox = h("div", {class: "input-group", hidden: true}, date, time);
  const repeat = seg(t("repeats"), [["", t("repeat_once")], ["daily", t("repeat_daily")],
    ["weekdays", t("repeat_weekdays")]], "", null, true);
  const repeatField = h("div", {class: "field", hidden: true},
    h("span", {class: "field-label"}, t("repeats")), repeat);
  const mode = seg(t("when"), [["in", t("in")], ["at", t("at_time")]], "in", (v) => {
    inBox.hidden = v !== "in";
    atBox.hidden = repeatField.hidden = v !== "at";
  }, true);
  const add = h("button", {class: "btn btn-primary", type: "submit"}, t("add_reminder_btn"));
  const form = h("form", {class: "card stack gap-l", "aria-labelledby": "h-add"},
    h("h2", {class: "h2", id: "h-add"}, t("add_reminder")),
    field("r-what", t("what"), what),
    h("div", {class: "grid2"},
      h("div", {class: "field"}, h("span", {class: "field-label"}, t("when")), mode, inBox, atBox),
      repeatField),
    h("div", {}, add));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {text: what.value.trim()};
    if (mode.value === "in") {
      body.in_seconds = Math.round(Number(minutes.value) * 60);
    } else {
      if (!date.value || !time.value) { (date.value ? time : date).focus(); return; }
      body.local_time = date.value + "T" + time.value;
      if (repeat.value) body.repeat = repeat.value;
    }
    const r = await busy(add, () => api("POST", "/api/v1/reminders", body));
    if (!r.ok) { toast(r.data.error); return; }
    what.value = "";
    toast(t("added"));
    load();
  });

  clear(root).append(head(t("nav_reminders")), ringing, form, lists);

  // ---- what there is ----

  let items = [];
  let now = 0;
  let tz;
  let received = Date.now();
  const deviceNow = () => now + (Date.now() - received) / 1000;

  const quietBtn = (label, name, fn) => h("button", {class: "btn btn-quiet", type: "button",
    "aria-label": label + ": " + name, onclick: fn}, label);
  const card = (label, rows) => [sectionLabel(label), h("section", {class: "card flush", "aria-label": label}, rows)];

  function draw() {
    const going = items.filter((j) => j.state === "ringing");
    clear(ringing).append(...going.map((j) => h("section", {class: "card soft", "aria-live": "assertive"},
      h("div", {class: "spread wrap"},
        h("div", {class: "row-main"},
          h("span", {class: "label ink"}, t("ringing_now") + (j.kind === "timer" ? "" : " · " + clockTime(j.due_wall, tz))),
          h("span", {class: "h2"}, j.text || t("timer"))),
        h("div", {class: "hstack gap-m"},
          h("button", {class: "btn btn-primary btn-sm", type: "button",
            onclick: () => act("/api/v1/reminders/" + j.id + "/snooze", {minutes: j.kind === "timer" ? 1 : 10})},
            j.kind === "timer" ? t("add_minute") : t("snooze_10")),
          h("button", {class: "btn btn-sm", type: "button", onclick: () => act("/api/v1/alert/stop")}, t("stop")))))));

    clear(lists);
    const timers = items.filter((j) => j.state === "pending" && j.kind === "timer");
    if (timers.length) {
      lists.append(...card(t("timers"), timers.map((j) => {
        const name = j.text || t("timer");
        return h("div", {class: "row"},
          h("div", {class: "row-main"}, h("span", {class: "row-title"}, name),
            j.length ? h("span", {class: "row-sub"}, t("timer_of", {length: span(j.length)})) : null),
          h("span", {class: "num lg"}, countdown(j.due_wall - deviceNow())),
          h("button", {class: "btn btn-sm", type: "button", "aria-label": t("cancel_named", {name}),
            onclick: () => act("/api/v1/reminders/" + j.id + "/cancel")}, t("cancel")));
      })));
    }

    // Reminders, a card per day, in the device's time zone.
    const days = new Map();
    for (const j of items.filter((x) => x.state === "pending" && x.kind !== "timer")) {
      const key = dayKey(j.due_wall, tz);
      if (!days.has(key)) days.set(key, []);
      days.get(key).push(j);
    }
    const today = dayKey(deviceNow(), tz);
    const tomorrow = dayKey(deviceNow() + 86400, tz);
    for (const [key, jobs] of days) {
      const long = longDay(jobs[0].due_wall, tz);
      const label = key === today ? t("today") + " · " + long
        : key === tomorrow ? t("tomorrow") + " · " + long : long;
      lists.append(...card(label, jobs.map((j) => h("div", {class: "row"},
        h("div", {class: "row-main"}, h("span", {class: "row-title"}, j.text),
          h("span", {class: "row-sub"}, clockTime(j.due_wall, tz) + " · " + t("repeat_" + repeatKey(j.repeat)))),
        quietBtn(t("cancel"), j.text, () => act("/api/v1/reminders/" + j.id + "/cancel"))))));
    }
    if (!timers.length && !days.size) {
      lists.append(...card(t("upcoming"), [emptyRow(t("no_reminders"))]));
    }

    const late = items.filter((j) => j.state === "missed");
    if (late.length) {
      lists.append(...card(t("missed"), late.map((j) => h("div", {class: "row"},
        h("div", {class: "row-main"}, h("span", {class: "row-title"}, j.text || t("timer")),
          h("span", {class: "row-sub"}, when(j.due_wall, tz, deviceNow()))),
        quietBtn(t("dismiss"), j.text || t("timer"), () => act("/api/v1/reminders/" + j.id + "/cancel"))))));
    }
  }

  async function act(path, body) {
    const r = await api("POST", path, body);
    if (!r.ok) toast(r.data.error);
    load();
  }
  async function load() {
    const [all, state] = await Promise.all([api("GET", "/api/v1/reminders"), api("GET", "/api/v1/state")]);
    if (state.ok) {
      now = state.data.time.now;
      tz = state.data.time.timezone;
      received = Date.now();
      if (!date.value) date.value = dayKeyIso(now, tz);
    }
    if (all.ok) items = all.data.reminders;
    draw();
  }
  await load();
  // Only the countdowns move between loads; redraw when there is one to move.
  const timer = setInterval(() => {
    if (items.some((j) => j.state === "pending" && j.kind === "timer")
        && !lists.contains(document.activeElement)) draw();
  }, 1000);
  const refresh = setInterval(load, 5000);
  return () => { clearInterval(timer); clearInterval(refresh); };
}

/** Today on the device as YYYY-MM-DD, which is what a date field takes. */
function dayKeyIso(epoch, tz) {
  try {
    return new Date(epoch * 1000).toLocaleDateString("en-CA", {timeZone: tz || undefined});
  } catch (e) {
    return new Date(epoch * 1000).toLocaleDateString("en-CA");
  }
}
