// The device's own settings: name, language, volume, quiet hours, screen, ears.
// Switches, sliders and choices save as they change; typed fields have a button.

import {api, clear, h, t, toast} from "/js/core.js";
import {busy, field, input, range, result, save, seg, subHead, toggleRow} from "/js/ui.js";

export async function render(root) {
  const r = await api("GET", "/api/v1/settings");
  if (!r.ok) { clear(root).append(subHead(t("device")), h("p", {class: "c-danger"}, r.data.error)); return; }
  const s = r.data.settings;

  // A slider with its value beside the label, saved when let go.
  function slider(id, label, value, patch, attrs, show) {
    show = show || ((v) => v + "%");
    const num = h("span", {class: "num md"}, show(value));
    const el = range(id, value, attrs);
    el.addEventListener("input", () => { num.textContent = show(el.value); });
    el.addEventListener("change", () => save(patch(Number(el.value))));
    return h("div", {}, h("div", {class: "spread"}, h("label", {class: "field-label", for: id}, label), num), el);
  }
  const flat = (row) => { row.style.padding = "4px 0"; row.style.minHeight = "52px"; return row; };
  const card = (title, ...kids) => h("section", {class: "card stack gap-l"}, h("h2", {class: "h2"}, title), ...kids);

  const name = input({type: "text", value: s.owner.name, maxlength: "40", autocomplete: "off", placeholder: t("name_placeholder")});
  const tz = input({type: "text", value: s.owner.timezone, placeholder: "Europe/Ljubljana", autocomplete: "off", spellcheck: "false"});
  const place = input({type: "text", value: s.owner.location, maxlength: "60", autocomplete: "off"});
  const language = seg(t("language"), [["en", "English"], ["sl", "Slovenščina", "sl"]], s.owner.language, async (v) => {
    // The page speaks the same language as the device: load it again in it.
    if (await save({owner: {language: v}})) location.reload();
  });
  const saveYou = h("button", {class: "btn btn-primary", type: "submit"}, t("save"));
  const you = h("form", {class: "card stack gap-l"}, h("h2", {class: "h2"}, t("you")),
    field("d-name", t("your_name"), name, t("name_hint")),
    h("div", {class: "field"}, h("span", {class: "field-label"}, t("language")), h("div", {}, language),
      h("p", {class: "hint"}, t("language_hint"))),
    h("div", {class: "grid2"}, field("d-tz", t("timezone"), tz),
      field("d-place", t("location"), place, t("location_hint"))),
    h("div", {}, saveYou));
  you.addEventListener("submit", (event) => {
    event.preventDefault();
    busy(saveYou, () => save({owner: {name: name.value.trim(), timezone: tz.value.trim(),
      location: place.value.trim()}}, true));
  });

  const from = input({type: "time", value: s.quiet.from});
  const to = input({type: "time", value: s.quiet.to});
  const saveTimes = () => { if (from.value && to.value) save({quiet: {from: from.value, to: to.value}}); };
  from.addEventListener("change", saveTimes);
  to.addEventListener("change", saveTimes);

  const sleep = input({type: "number", class: "input input-short", min: "1", max: "240", value: s.screen.sleep_after_min});
  sleep.addEventListener("change", () => {
    if (Number(sleep.value) >= 1) save({screen: {sleep_after_min: Number(sleep.value)}});
  });

  // The pairing PIN: keep it off the screen, or choose your own.
  const oldPin = input({type: "password", inputmode: "numeric", pattern: "[0-9]*", maxlength: "10", autocomplete: "off"});
  const newPin = input({type: "password", inputmode: "numeric", pattern: "[0-9]*", maxlength: "10", autocomplete: "off"});
  const pinNote = h("div");
  const savePin = h("button", {class: "btn btn-primary", type: "submit"}, t("pin_change"));
  const pinForm = h("form", {class: "stack gap-l"},
    h("div", {class: "grid2"}, field("d-pin-old", t("pin_current"), oldPin),
      field("d-pin-new", t("pin_new"), newPin, t("pin_new_hint"))),
    pinNote, h("div", {}, savePin));
  pinForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!oldPin.value || !newPin.value) { (oldPin.value ? newPin : oldPin).focus(); return; }
    busy(savePin, async () => {
      const r = await api("POST", "/api/v1/pin", {pin: oldPin.value, new_pin: newPin.value});
      clear(pinNote);
      if (r.ok) {
        oldPin.value = newPin.value = "";
        toast(t("pin_changed"));
        return;
      }
      pinNote.append(result("err", r.status === 403 ? t("pin_wrong")
        : r.status === 429 ? t("pin_wait", {s: Math.ceil(r.data.retry_after || 30)}) : (r.data.error || t("lost_contact"))));
    });
  });
  const security = s.security || {show_pin: true};

  clear(root).append(subHead(t("device")), you,
    card(t("sound"),
      slider("d-vol", t("volume"), Math.round(s.voice.volume * 100), (v) => ({voice: {volume: v / 100}})),
      flat(toggleRow("d-barge", t("barge_in"), t("barge_hint"), s.voice.barge_in, (on) => save({voice: {barge_in: on}})))),
    card(t("quiet_hours"),
      flat(toggleRow("d-quiet", t("quiet_on"), t("quiet_hint"), s.quiet.enabled, (on) => save({quiet: {enabled: on}}))),
      h("div", {class: "grid2"}, field("d-from", t("from"), from), field("d-to", t("to"), to)),
      slider("d-qvol", t("quiet_volume"), Math.round(s.quiet.volume * 100), (v) => ({quiet: {volume: v / 100}}))),
    card(t("screen"),
      h("div", {class: "field"}, h("label", {for: "d-sleep"}, t("sleep_after")),
        h("div", {class: "input-group"}, (sleep.id = "d-sleep", sleep), h("span", {class: "unit"}, t("minutes")))),
      flat(toggleRow("d-night", t("night_off"), null, s.screen.night_off, (on) => save({screen: {night_off: on}}))),
      flat(toggleRow("d-idle", t("idle_animations"), t("idle_animations_hint"), s.screen.idle_animations !== false,
        (on) => save({screen: {idle_animations: on}})))),
    card(t("pin_card"),
      flat(toggleRow("d-showpin", t("pin_show"), t("pin_show_hint"), security.show_pin,
        (on) => save({security: {show_pin: on}}))),
      h("div", {class: "divider"}), pinForm),
    card(t("listening"),
      h("div", {class: "field"}, h("span", {class: "field-label"}, t("wake_sensitivity")),
        h("div", {}, seg(t("wake_sensitivity"), [["low", t("wake_low")], ["normal", t("wake_normal")], ["high", t("wake_high")]],
          s.audio.wake_sensitivity, (v) => save({audio: {wake_sensitivity: v}}))),
        h("p", {class: "hint"}, t("wake_hint"))),
      h("div", {}, slider("d-vad", t("vad"), s.audio.vad_cutoff, (v) => ({audio: {vad_cutoff: v}}),
        {min: "0.1", max: "0.9", step: "0.05"}, (v) => Number(v).toFixed(2)),
        h("p", {class: "hint"}, t("vad_hint")))));
}
