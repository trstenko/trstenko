// The page. A hash router, the two navigations (a tab bar on a phone, a rail
// on a desktop), and one module per screen in /js/views.
// No build step: these are plain ES modules (plan 07).

import {api, clear, h, hasToken, loadStrings, onLocked, t, toast} from "/js/core.js";
import {createFace, facePanel} from "/js/face.js";
import {icon} from "/js/ui.js";

const SCREENS = {
  home: () => import("/js/views/home.js"),
  reminders: () => import("/js/views/reminders.js"),
  usage: () => import("/js/views/usage.js"),
  settings: () => import("/js/views/settings.js"),
  setup: () => import("/js/views/setup.js"),
  lock: () => import("/js/views/lock.js"),
};
const TABS = [["home", "home"], ["reminders", "bell"], ["usage", "bars"], ["settings", "sliders"]];
// Screens with no navigation around them. Setup is the wider of the two.
const BARE = ["lock", "setup"];

const view = document.getElementById("view");
const rail = document.getElementById("rail");
const tabbar = document.getElementById("tabbar");
let cleanup = null;
let shown = 0;
let railFace = null;
let identity = "";

function route() {
  const path = (location.hash || "#/home").replace(/^#\/?/, "").split("/");
  return {name: path[0] || "home", rest: path.slice(1)};
}

export function go(hash) { location.hash = hash; }

function buildNav(current) {
  const link = (cls, name, glyph, inside) => h("a", {class: cls, href: "#/" + name,
    "aria-current": current === name ? "page" : false}, inside(icon(glyph, 22, 1.8)), t("nav_" + name));
  if (!railFace) {
    railFace = createFace(t("face_label"));
    railFace.set("face_idle");
  }
  clear(rail).append(
    h("div", {class: "rail-brand"}, facePanel(railFace, "mini"), h("span", {class: "wordmark"}, "Trstenko")),
    ...TABS.map(([name, glyph]) => link("navi", name, glyph, (svg) => svg)),
    h("div", {class: "rail-foot"}, h("span", {}, t("connected_to")), h("span", {class: "mono"}, identity)));
  clear(tabbar).append(...TABS.map(([name, glyph]) =>
    link("tab", name, glyph, (svg) => h("span", {class: "t-ico"}, svg))));
}

async function show() {
  const ticket = ++shown;
  if (cleanup) { try { cleanup(); } catch (e) { /* leaving anyway */ } cleanup = null; }
  let {name, rest} = route();
  if (!hasToken()) name = "lock";
  if (!SCREENS[name]) name = "home";
  const bare = BARE.includes(name);
  document.body.classList.toggle("bare", bare);
  document.body.classList.toggle("wide", name === "setup");
  rail.hidden = tabbar.hidden = bare;
  if (!bare) buildNav(name);
  const content = h("div", {class: "content"});
  clear(view).append(content);
  let module;
  try { module = await SCREENS[name](); } catch (e) {
    content.append(h("p", {class: "c-danger"}, t("lost_contact")));
    return;
  }
  if (ticket !== shown) return;
  const result = await module.render(content, {rest, go, api, toast});
  if (ticket !== shown) { if (typeof result === "function") result(); return; }
  cleanup = typeof result === "function" ? result : null;
  window.scrollTo(0, 0);
  view.focus({preventScroll: true});
}

async function boot() {
  // The language: the owner's setting once we can read it, else the browser's.
  let wanted = (navigator.language || "en").slice(0, 2);
  if (hasToken()) {
    const [settings, info] = await Promise.all([api("GET", "/api/v1/settings"), api("GET", "/api/v1/info")]);
    if (settings.ok) wanted = settings.data.settings.owner.language;
    if (info.ok) identity = info.data.firmware + " · " + info.data.product.serial;
  }
  await loadStrings(wanted);
  onLocked(() => { location.hash = "#/home"; show(); });
  window.addEventListener("hashchange", show);
  if (hasToken() && !location.hash) {
    // First visit after pairing? A device with no key goes to the setup flow.
    const k = await api("GET", "/api/v1/keys");
    const chosen = k.ok && (k.data.providers || []).find((p) => p.id === k.data.selected);
    if (chosen && chosen.needs_key && !k.data[chosen.id].set) location.hash = "#/setup";
  }
  show();
}

boot();
