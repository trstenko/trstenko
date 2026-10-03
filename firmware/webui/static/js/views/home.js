// Home: the face, what it is doing, and what needs a look.

import {api, clear, countdown, h, money, onLink, repeatKey, span, subscribe, t, toast, when} from "/js/core.js";
import {createFace, facePanel} from "/js/face.js";
import {banner, emptyRow, meter, pill, range, save, toggle} from "/js/ui.js";

// What the status line says for each stage of a turn, and the line under it.
const STAGES = {idle: "idle", waiting: "idle", done: "idle", listening: "listening",
  thinking: "thinking", speaking: "speaking", error: "error"};

// Where each of the assistant's problems is fixed, and how loudly to say it.
const ALERTS = {
  no_key: ["danger", "key", "#/settings/assistant"],
  no_stt: ["warn", "key", "#/settings/assistant"],
  no_voice: ["warn", "alert", "#/settings/device"],
  no_wake_word: ["warn", "alert", null],
  tables: ["danger", "alert", null],
};

export async function render(root) {
  const face = createFace(t("face_label"));
  const link = h("span", {class: "pill neutral"});
  const alerts = h("div", {class: "stack gap-m", hidden: true});

  // Something going off takes the place of the face until it is dealt with.
  const ringTitle = h("p", {class: "status"});
  const ringSub = h("p", {class: "ring-sub"});
  const stopBtn = h("button", {class: "btn btn-stop", type: "button"}, t("stop"));
  const laterBtn = h("button", {class: "btn btn-on-accent", type: "button"});
  const ringScreen = h("div", {class: "screen"});
  const ring = h("section", {class: "ring", "aria-live": "assertive", hidden: true},
    ringScreen, h("div", {class: "ring-body stack gap-l"}, h("div", {}, ringTitle, ringSub), stopBtn, laterBtn));

  const status = h("p", {class: "status"});
  const sub = h("p", {class: "muted", style: "margin-top:4px"});
  const homeScreen = facePanel(face, "home");
  const faceCard = h("section", {class: "card facecard", "aria-label": t("status")},
    homeScreen, h("div", {class: "status-block", "aria-live": "polite"}, status, sub));

  const timerCount = h("span", {class: "label"});
  const timerRows = h("div");
  const timers = h("section", {class: "card flush", "aria-labelledby": "h-timers"},
    h("div", {class: "spread card-head"}, h("h2", {class: "h2", id: "h-timers"}, t("timers")), timerCount),
    timerRows);
  const nextText = h("p", {class: "lead"});
  const nextWhen = h("p", {class: "muted small"});
  const next = h("section", {class: "card stack gap-s", "aria-labelledby": "h-next"},
    h("div", {class: "spread"}, h("h2", {class: "h2", id: "h-next"}, t("next_reminder")),
      h("a", {class: "small", href: "#/reminders"}, t("all_reminders"))),
    nextText, nextWhen);

  const volumeNum = h("span", {class: "num md"});
  const volume = range("vol", 60);
  const quiet = toggle("quiet", false, (on) => save({quiet: {enabled: on}}));
  const quietSub = h("span", {class: "row-sub"});
  const sound = h("section", {class: "card", "aria-label": t("sound")},
    h("div", {class: "spread"}, h("label", {class: "field-label", for: "vol"}, t("volume")), volumeNum),
    volume, h("div", {class: "divider"}),
    h("div", {class: "spread", style: "padding-top:8px"},
      h("div", {class: "row-main"}, h("label", {class: "row-title", for: "quiet"}, t("quiet_hours")), quietSub),
      quiet));
  const month = h("a", {class: "card stack", href: "#/usage", style: "gap:10px"});

  clear(root).append(
    h("div", {class: "topbar only-phone"}, h("span", {class: "wordmark"}, "Trstenko"), link),
    alerts, ring, faceCard,
    h("div", {class: "grid2"}, timers, next),
    h("div", {class: "grid2"}, sound, month));
  face.start();

  // ---- what changes every second ----

  let live = null;
  let ringing = null;       // the job that is going off, once we have fetched it
  let wantRing = null;

  function draw() {
    if (!live) return;
    const key = live.muted ? "muted" : (STAGES[live.stage] || "idle");
    status.textContent = t("stage_" + key);
    sub.textContent = t("sub_" + key);
    face.set(live.face, live.level, t("face_is", {state: status.textContent}));

    clear(timerRows);
    timerCount.textContent = live.timers.length ? t("n_running", {n: live.timers.length}) : "";
    if (!live.timers.length) timerRows.append(emptyRow(t("no_timers")));
    for (const tm of live.timers) {
      // The device's clock may differ from the phone's: count from the
      // device's own "now", plus the time since it was sent.
      const left = tm.due_wall - live.now - (Date.now() - live.received) / 1000;
      const name = tm.text || t("timer");
      timerRows.append(h("div", {class: "row"},
        h("div", {class: "row-main"}, h("span", {class: "row-title"}, name),
          tm.length ? h("span", {class: "row-sub"}, t("timer_of", {length: span(tm.length)})) : null),
        h("span", {class: "num lg"}, countdown(left)),
        h("button", {class: "btn btn-sm", type: "button", "aria-label": t("cancel_named", {name}),
          onclick: () => act("/api/v1/reminders/" + tm.id + "/cancel")}, t("cancel"))));
    }

    if (live.ringing !== wantRing) {
      wantRing = live.ringing;
      ringing = null;
      if (wantRing != null) loadRinging(wantRing);
    }
    const on = ringing != null && live.ringing === ringing.id;
    ring.hidden = !on;
    faceCard.hidden = on;
    const home = on ? ringScreen : homeScreen;
    if (face.el.parentNode !== home) home.append(face.el);
    if (on) {
      const timer = ringing.kind === "timer";
      ringTitle.textContent = timer ? t("timer_done") : t("reminder_now");
      ringSub.textContent = (ringing.text || t("timer"))
        + (timer && ringing.length ? " · " + span(ringing.length) : "");
      laterBtn.textContent = timer ? t("add_minute") : t("snooze_10");
    }
  }

  async function loadRinging(id) {
    const r = await api("GET", "/api/v1/reminders?state=ringing");
    const job = r.ok && r.data.reminders.find((j) => j.id === id);
    if (job && wantRing === id) { ringing = job; draw(); }
  }
  async function act(path, body) {
    const r = await api("POST", path, body);
    if (!r.ok) toast(r.data.error);
  }
  stopBtn.addEventListener("click", () => act("/api/v1/alert/stop"));
  laterBtn.addEventListener("click", () => ringing && act("/api/v1/reminders/" + ringing.id + "/snooze",
    {minutes: ringing.kind === "timer" ? 1 : 10}));

  const stop = subscribe((data) => { live = Object.assign({}, data, {received: Date.now()}); draw(); });
  const ticker = setInterval(draw, 1000);
  const unlink = onLink((up) => {
    clear(link).append(h("span", {class: "dot" + (up ? "" : " off")}), up ? t("connected") : t("not_connected"));
  });

  // ---- what changes now and then ----

  let dragging = false;
  async function load() {
    const [state, assistant, use, settings, pending, keys] = await Promise.all([
      api("GET", "/api/v1/state"), api("GET", "/api/v1/assistant"),
      api("GET", "/api/v1/usage?days=7"), api("GET", "/api/v1/settings"),
      api("GET", "/api/v1/reminders?state=pending"), api("GET", "/api/v1/keys")]);
    const found = [];
    const label = (id) => ((keys.ok && keys.data.providers.find((p) => p.id === id)) || {label: id}).label;
    if (assistant.ok) {
      for (const a of assistant.data.alerts || []) {
        const [kind, glyph, href] = ALERTS[a.kind] || ["warn", "alert", null];
        found.push(banner(t("assistant_" + a.kind), kind, glyph, href));
      }
      for (const b of assistant.data.banners || []) {
        found.push(banner(t("banner_" + b.kind, {provider: label(b.provider)}), "danger", "key", "#/settings/assistant"));
      }
    }
    if (state.ok) {
      for (const a of state.data.alerts) {
        found.push(banner(t("alert_" + a.kind, {name: a.name || ""}), "warn", "alert"));
      }
      const up = state.data.update;
      if (up && up.available) {
        found.push(banner(t("update_ready", {version: up.available}), "", "download", "#/settings/updates"));
      }
    }
    if (use.ok) {
      const m = use.data.summary.month;
      if (m.percent != null && m.percent >= 100) {
        found.unshift(banner(t("limit_reached_short", {limit: money(m.limit_usd, 2)}), "danger", "alert", "#/usage"));
      }
      clear(month).append(
        h("div", {class: "spread"}, h("h2", {class: "h2"}, t("this_month")),
          h("span", {class: "num md"}, money(m.spent_usd), m.limit_usd != null
            ? h("span", {class: "muted", style: "font-weight:600"}, " / " + money(m.limit_usd, 2)) : null)),
        meter(m.percent, "thin", true),
        h("p", {class: "muted small"}, use.data.summary.free_tier ? t("free_tier_note") : t("estimate_note")));
    }
    clear(alerts).append(...found);
    alerts.hidden = !found.length;

    if (pending.ok) {
      const tz = state.ok ? state.data.time.timezone : undefined;
      const now = state.ok ? state.data.time.now : undefined;
      const job = pending.data.reminders.find((j) => j.kind !== "timer");
      nextText.textContent = job ? job.text : t("no_reminders");
      nextText.className = job ? "lead" : "muted";
      nextWhen.textContent = job ? when(job.due_wall, tz, now) + " · " + t("repeat_" + repeatKey(job.repeat)) : "";
    }
    if (settings.ok) {
      const s = settings.data.settings;
      if (!dragging) {
        volume.value = Math.round(s.voice.volume * 100);
        volume.fill();
        volumeNum.textContent = volume.value + "%";
      }
      quiet.checked = s.quiet.enabled;
      quietSub.textContent = t("quiet_summary", {from: s.quiet.from, to: s.quiet.to,
        volume: Math.round(s.quiet.volume * 100)});
    }
  }
  volume.addEventListener("input", () => { dragging = true; volumeNum.textContent = volume.value + "%"; });
  volume.addEventListener("change", async () => {
    await save({voice: {volume: volume.value / 100}});
    dragging = false;
  });
  await load();
  const refresh = setInterval(load, 15000);
  return () => { stop(); unlink(); face.stop(); clearInterval(ticker); clearInterval(refresh); };
}
