// The parts every screen is built from: icons, fields, switches, meters,
// banners and the confirmation sheet. One place, so a button on one screen is
// the same button on the next.

import {api, h, lead, t, toast} from "/js/core.js";

// ---- icons ----
// Line icons on a 24 grid, drawn as elements (the page's policy allows no
// markup from strings). Each entry is a list of [tag, attributes].

const NS = "http://www.w3.org/2000/svg";
const ICONS = {
  home: [["path", {d: "M4 11l8-6 8 6v8a1 1 0 0 1-1 1h-4v-5h-6v5H5a1 1 0 0 1-1-1z"}]],
  bell: [["path", {d: "M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"}], ["path", {d: "M10 20.5a2.2 2.2 0 0 0 4 0"}]],
  bars: [["path", {d: "M5 20v-8M10 20V6M15 20v-9M20 20V9"}]],
  sliders: [["path", {d: "M4 7h10M18 7h2M4 17h4M12 17h8"}], ["circle", {cx: 16, cy: 7, r: 2}], ["circle", {cx: 10, cy: 17, r: 2}]],
  key: [["circle", {cx: 8, cy: 15, r: 4}], ["path", {d: "M11 12l8-8M16 7l2.5 2.5"}]],
  download: [["path", {d: "M12 4v11M7 10l5 5 5-5M5 20h14"}]],
  device: [["rect", {x: 4, y: 5, width: 16, height: 12, rx: 3}], ["path", {d: "M9 21h6M12 17v4"}]],
  bookmark: [["path", {d: "M6 4h12v16l-6-4-6 4z"}]],
  shield: [["path", {d: "M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"}]],
  alert: [["circle", {cx: 12, cy: 12, r: 9}], ["path", {d: "M12 7.5v5.5M12 16.5v.01"}]],
  check: [["path", {d: "M5 12.5l4.5 4.5L19 7.5"}]],
  right: [["path", {d: "M9 6l6 6-6 6"}]],
  left: [["path", {d: "M15 6l-6 6 6 6"}]],
  down: [["path", {d: "M6 9l6 6 6-6"}]],
};

export function icon(name, size, stroke, cls) {
  const svg = document.createElementNS(NS, "svg");
  const set = (el, attrs) => { for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); };
  set(svg, {width: size || 20, height: size || 20, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": stroke || 2, "stroke-linecap": "round",
    "stroke-linejoin": "round", "aria-hidden": "true"});
  if (cls) svg.setAttribute("class", cls);
  for (const [tag, attrs] of ICONS[name] || []) {
    const el = document.createElementNS(NS, tag);
    set(el, attrs);
    svg.append(el);
  }
  return svg;
}

// ---- the top of a screen ----

export function head(title, extra) {
  return h("div", {class: "topbar"}, h("h1", {class: "title"}, title), extra);
}

/** The top of a screen that Settings opens: a way back, then the title. */
export function subHead(title) {
  return h("div", {class: "stack", style: "gap:2px"},
    h("a", {class: "back", href: "#/settings"}, icon("left"), t("nav_settings")),
    h("h1", {class: "title"}, title));
}

export const sectionLabel = (text) => h("h2", {class: "section-label"}, text);

// ---- fields ----

/** A labelled field wrapping `control`, with an optional hint. */
export function field(id, label, control, hint) {
  control.id = id;
  return h("div", {class: "field"}, h("label", {for: id}, label), control.wrap || control,
    hint ? h("p", {class: "hint"}, hint) : null);
}

export function input(attrs) {
  return h("input", Object.assign({class: "input"}, attrs));
}

/** A <select> in the page's own frame. Put `.wrap` in the page (field() does). */
export function select(options, value, cls) {
  const el = h("select", {class: "select" + (cls ? " " + cls : "")}, options.map(([v, text]) =>
    h("option", {value: v, selected: String(v) === String(value)}, text)));
  el.wrap = h("div", {class: "select-wrap"}, el, icon("down", 18));
  return el;
}

/** An amount of money: the $ sits inside the box. Returns the wrapper, with
 *  the input as `.input`. */
export function moneyInput(id, value, placeholder) {
  const el = input({id, inputmode: "decimal", autocomplete: "off", value: value ?? "", placeholder});
  const box = h("div", {class: "money"}, h("span", {"aria-hidden": "true"}, "$"), el);
  box.input = el;
  return box;
}

let segCount = 0;
/** A few choices side by side. `.value` reads the chosen one. */
export function seg(label, options, value, onchange, block) {
  const name = "seg" + (++segCount);
  const el = h("fieldset", {class: "seg" + (block ? " block" : ""), "aria-label": label},
    options.map(([v, text, lang]) => h("label", {lang: lang || false},
      h("input", {type: "radio", name, value: v, checked: String(v) === String(value)}), text)));
  Object.defineProperty(el, "value", {get: () => (el.querySelector("input:checked") || {}).value});
  if (onchange) el.addEventListener("change", () => onchange(el.value));
  return el;
}

export function toggle(id, checked, onchange) {
  const el = h("input", {class: "switch", id, type: "checkbox", role: "switch", checked: !!checked});
  if (onchange) el.addEventListener("change", () => onchange(el.checked));
  return el;
}

/** A row with a title, a line under it and a switch at the end. */
export function toggleRow(id, title, sub, checked, onchange) {
  const sw = toggle(id, checked, onchange);
  const row = h("div", {class: "row"}, h("div", {class: "row-main"},
    h("label", {class: "row-title", for: id}, title),
    sub ? h("span", {class: "row-sub"}, sub) : null), sw);
  row.input = sw;
  return row;
}

/** A slider whose filled part follows the thumb. */
export function range(id, value, attrs) {
  const el = h("input", Object.assign({class: "range", id, type: "range", min: "0", max: "100", step: "5", value}, attrs));
  const fill = () => {
    const lo = Number(el.min), hi = Number(el.max);
    el.style.setProperty("--v", ((Number(el.value) - lo) / (hi - lo)) * 100 + "%");
  };
  el.addEventListener("input", fill);
  el.fill = fill;
  fill();
  return el;
}

// ---- showing things ----

/** A bar filled to `percent`. It turns amber from 80 and red at 100 only when
 *  `limit` is set: a download reaching 100% is not a warning. */
export function meter(percent, cls, limit) {
  const p = Math.max(0, Math.min(100, percent || 0));
  const level = !limit ? "" : p >= 100 ? " over" : p >= 80 ? " warn" : "";
  return h("div", {class: "meter" + (cls ? " " + cls : "") + level + (p ? "" : " empty"),
    role: "progressbar", "aria-valuenow": Math.round(p), "aria-valuemin": "0", "aria-valuemax": "100"},
    h("span", {style: "width:" + p + "%"}));
}

export function pill(text, kind, lead) {
  return h("span", {class: "pill" + (kind ? " " + kind : "")}, lead, text);
}

/** A calm, tappable line at the top of a screen. The first sentence is bold. */
export function banner(text, kind, iconName, href) {
  const [first, rest] = lead(text);
  const inside = [h("span", {class: "b-ico"}, icon(iconName || "alert")),
    h("span", {class: "b-text"}, h("b", {}, first), rest ? " " + rest : null),
    href ? icon("right", 20, 2, "chev") : null];
  return href ? h("a", {class: "banner " + (kind || ""), href}, inside)
    : h("div", {class: "banner " + (kind || "")}, inside);
}

/** The outcome of something the owner just did: "ok", "err" or "busy". */
export function result(kind, text, leadEl) {
  return h("div", {class: "result " + kind},
    leadEl || (kind === "busy" ? null : icon(kind === "ok" ? "check" : "alert", 20, kind === "ok" ? 2.4 : 2)),
    h("span", {}, text));
}

export function emptyRow(text) {
  return h("div", {class: "row empty"}, text);
}

/** A list of facts: label on the left, value on the right. */
export function facts(rows) {
  return h("dl", {class: "facts"}, rows.filter(Boolean).map(([k, v, mono]) =>
    h("div", {}, h("dt", {}, k), h("dd", {class: mono ? "mono" : ""}, v ?? "—"))));
}

// ---- doing things ----

/** PATCH a settings change. Says so only when it fails, unless `loud`. */
export async function save(patch, loud) {
  const r = await api("PATCH", "/api/v1/settings", patch);
  if (!r.ok) { toast(r.data.error || t("lost_contact")); return false; }
  if (loud) toast(t("saved"));
  return true;
}

/** Keep a button from being pressed twice while `work` runs. */
export async function busy(button, work) {
  button.disabled = true;
  try { return await work(); } finally { button.disabled = false; }
}

/** Ask before something that cannot be undone: a sheet from the bottom on a
 *  phone, a dialog on a desktop. With `pin`, the owner types the PIN from the
 *  device's screen again (plan 07). `run(pin)` does the work and returns an
 *  error sentence, or nothing when it went through; the sheet stays open on an
 *  error so a mistyped PIN can be typed again. Resolves to whether it ran. */
export function confirmSheet({title, body, action, cancel, pin, run}) {
  return new Promise((done) => {
    const before = document.activeElement;
    const error = h("p", {class: "result err", role: "alert", hidden: true});
    const pinBox = pin ? input({id: "sheet-pin", class: "input input-pin sm", inputmode: "numeric",
      pattern: "[0-9]*", maxlength: "10", autocomplete: "off", placeholder: "······"}) : null;
    const go = h("button", {class: "btn btn-danger btn-lg btn-block", type: "submit"}, action);
    const keep = h("button", {class: "btn btn-lg btn-block", type: "button"}, cancel || t("cancel"));
    const scrim = h("div", {class: "scrim"});
    const sheet = h("form", {class: "sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "sheet-title"},
      h("div", {class: "grabber"}),
      h("div", {class: "stack gap-s"}, h("h2", {class: "title", id: "sheet-title"}, title),
        body ? h("p", {class: "muted"}, body) : null),
      pinBox ? h("div", {class: "field"}, h("label", {for: "sheet-pin"}, t("pin_from_screen")), pinBox) : null,
      error, go, keep);
    const close = (ran) => {
      scrim.remove();
      sheet.remove();
      document.removeEventListener("keydown", onKey);
      if (before && before.focus) before.focus();
      done(ran);
    };
    const onKey = (e) => { if (e.key === "Escape") close(false); };
    sheet.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (pinBox && !pinBox.value.trim()) { pinBox.focus(); return; }
      const problem = await busy(go, () => run(pinBox ? pinBox.value.trim() : undefined));
      if (!problem) { close(true); return; }
      error.textContent = problem;
      error.hidden = false;
      if (pinBox) pinBox.select();
    });
    keep.addEventListener("click", () => close(false));
    scrim.addEventListener("click", () => close(false));
    document.addEventListener("keydown", onKey);
    document.body.append(scrim, sheet);
    (pinBox || keep).focus();
  });
}
