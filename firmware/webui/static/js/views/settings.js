// Settings: a list that opens sub-screens, the way phone settings do. Each row
// says the one thing worth knowing about what is behind it.

import {api, clear, h, t} from "/js/core.js";
import {facts, head, icon, pill} from "/js/ui.js";

const SUB = {
  assistant: () => import("/js/views/assistant.js"),
  device: () => import("/js/views/device.js"),
  memories: () => import("/js/views/memories.js"),
  updates: () => import("/js/views/updates.js"),
  privacy: () => import("/js/views/privacy.js"),
};

const gb = (bytes) => (bytes == null ? null
  : bytes >= 1e9 ? (bytes / 1e9).toFixed(1) + " GB" : Math.round(bytes / 1e6) + " MB");

export async function render(root, ctx) {
  const sub = ctx.rest[0];
  if (sub && SUB[sub]) return (await SUB[sub]()).render(root, ctx);

  const [keys, settings, memories, update] = await Promise.all([api("GET", "/api/v1/keys"),
    api("GET", "/api/v1/settings"), api("GET", "/api/v1/memories"), api("GET", "/api/update")]);
  const s = settings.ok ? settings.data.settings : null;
  const u = update.ok ? update.data : {};
  const p = u.product || {};
  const d = u.device || {};

  let assistant = "";
  if (keys.ok && s) {
    const chosen = keys.data.providers.find((x) => x.id === s.assistant.provider);
    const set = chosen && (!chosen.needs_key || keys.data[chosen.id].set);
    assistant = (chosen ? chosen.label : s.assistant.provider) + " · " + (set ? t("key_set_lower") : t("no_key_lower"));
  }
  const device = s ? [s.owner.name, s.owner.language === "sl" ? "Slovenščina" : "English", s.owner.timezone]
    .filter(Boolean).join(" · ") : "";
  const waiting = u.release && ["awaiting_consent", "scheduled", "staged"].includes(u.state);

  const row = (id, glyph, title, text, extra) => h("a", {class: "row", href: "#/settings/" + id},
    h("span", {class: "row-ico"}, icon(glyph, 20, 1.8)),
    h("span", {class: "row-main"}, h("span", {class: "row-title"}, title),
      text ? h("span", {class: "row-sub"}, text) : null),
    extra, icon("right", 20, 2, "chev"));

  clear(root).append(head(t("nav_settings")),
    h("nav", {class: "card flush", "aria-label": t("settings_sections")},
      row("assistant", "key", t("assistant"), assistant),
      row("device", "device", t("device"), device),
      row("memories", "bookmark", t("memories"),
        memories.ok ? t("n_of_max", {n: memories.data.count, max: memories.data.limit}) : ""),
      row("updates", "download", t("updates"), u.version ? t("installed_version", {version: u.version}) : "",
        waiting ? pill(t("update_pill")) : null),
      row("privacy", "shield", t("privacy"),
        s ? (s.assistant.conversation_log ? t("log_on") : t("log_off")) : "")),
    h("section", {class: "card", "aria-label": t("this_device")}, facts([
      [t("firmware"), u.version, true],
      [t("serial"), p.serial, true],
      [t("network"), d.online === false ? t("offline") : t("online")],
      [t("free_space"), gb(d.free_bytes)],
    ])),
    h("p", {class: "note"}, t("served_note")));
}
