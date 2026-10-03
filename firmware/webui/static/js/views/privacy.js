// Privacy and about: what leaves the device, your data, and what this is.

import {api, clear, h, setToken, t, toast} from "/js/core.js";
import {confirmSheet, facts, save, sectionLabel, subHead, toggleRow} from "/js/ui.js";

export async function render(root) {
  const body = h("div", {class: "stack"});
  clear(root).append(subHead(t("privacy")), body);

  async function download() {
    const r = await api("GET", "/api/v1/privacy/export");
    if (!r.ok) { toast(r.data.error); return; }
    const url = URL.createObjectURL(new Blob([JSON.stringify(r.data, null, 2)], {type: "application/json"}));
    const a = h("a", {href: url, download: "trstenko-data.json"});
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  // Deleting cannot be undone, so it asks for the PIN on the device's screen
  // again: whoever is holding the phone has to be in the room.
  async function wipe(what) {
    const ran = await confirmSheet({
      title: t("delete_title_" + what), body: t("delete_body_" + what),
      action: t("delete_action_" + what), cancel: t("keep"), pin: true,
      run: async (pin) => {
        const r = await api("POST", "/api/v1/privacy/delete", {what, pin});
        if (r.ok) return "";
        return r.status === 403 ? t("pin_wrong") : (r.data.error || t("lost_contact"));
      }});
    if (ran) { toast(t("deleted")); load(); }
  }

  // Everything, and the Wi-Fi with it. The device restarts into setup mode
  // with a new PIN, so this page has nothing left to talk to.
  async function factoryReset() {
    const ran = await confirmSheet({
      title: t("reset_title"), body: t("reset_body"), action: t("reset_action"), cancel: t("cancel"), pin: true,
      run: async (pin) => {
        const r = await api("POST", "/api/v1/factory-reset", {pin});
        if (r.ok) return "";
        return r.status === 403 ? t("pin_wrong") : (r.data.error || t("lost_contact"));
      }});
    if (!ran) return;
    setToken("");
    clear(root).append(h("div", {class: "stack gap-s", style: "padding-top:24px"},
      h("h1", {class: "status"}, t("reset_done")), h("p", {class: "muted"}, t("reset_done_body"))));
  }

  async function load() {
    const [settings, update, memories, keys] = await Promise.all([api("GET", "/api/v1/settings"),
      api("GET", "/api/update"), api("GET", "/api/v1/memories"), api("GET", "/api/v1/keys")]);
    const a = settings.ok ? settings.data.settings.assistant : {};
    const p = (update.ok && update.data.product) || {};
    const chosen = keys.ok && keys.data.providers.find((x) => x.id === a.provider);
    const provider = chosen ? chosen.label : t("the_provider");
    const n = memories.ok ? memories.data.count : 0;

    const del = (what, title, sub, off) => h("div", {class: "row"},
      h("div", {class: "row-main"}, h("span", {class: "row-title"}, title),
        sub ? h("span", {class: "row-sub"}, sub) : null),
      h("button", {class: "btn btn-sm btn-danger-quiet", type: "button", disabled: !!off,
        "aria-label": t("delete") + ": " + title, onclick: () => wipe(what)}, t("delete")));

    clear(body).append(
      h("section", {class: "card stack", style: "gap:10px", "aria-labelledby": "h-leaves"},
        h("h2", {class: "h2", id: "h-leaves"}, t("what_leaves")),
        h("ul", {class: "plain small"},
          h("li", {}, t("leaves_audio", {provider})),
          h("li", {}, t("leaves_memories", {provider})),
          h("li", {}, t("leaves_stays")),
          h("li", {}, t("leaves_keys")))),
      h("section", {class: "card flush", "aria-label": t("your_data")},
        toggleRow("log", t("conversation_log"), t("conversation_log_hint"), a.conversation_log,
          (on) => save({assistant: {conversation_log: on}})),
        h("div", {class: "row"},
          h("div", {class: "row-main"}, h("span", {class: "row-title"}, t("download_data")),
            h("span", {class: "row-sub"}, t("download_hint"))),
          h("button", {class: "btn btn-sm", type: "button", onclick: download}, t("download")))),
      sectionLabel(t("delete")),
      h("section", {class: "card flush", "aria-label": t("delete")},
        del("memories", t("memories"), t("n_facts", {n}), !n),
        del("log", t("log_name"), a.conversation_log ? t("log_on") : t("log_off")),
        del("usage", t("usage_history"), t("usage_history_hint")),
        h("div", {class: "row"},
          h("div", {class: "row-main"}, h("span", {class: "row-title"}, t("reset_name")),
            h("span", {class: "row-sub"}, t("reset_hint"))),
          h("button", {class: "btn btn-sm btn-danger", type: "button", onclick: factoryReset}, t("reset_button")))),
      h("section", {class: "card stack gap-l", "aria-labelledby": "h-about"},
        h("h2", {class: "h2", id: "h-about"}, t("about")),
        facts([
          [t("product"), p.model_id ? p.model + " (" + p.model_id + ")" : p.model],
          [t("firmware"), p.firmware, true],
          [t("serial"), p.serial, true],
          [t("made_by"), p.manufacturer],
          p.address ? [t("address"), p.address] : null,
          [t("support_until"), p.support_until],
          [t("report_problem"), p.security_contact],
        ]),
        h("p", {class: "small muted"}, p.ai_notice || t("ai_notice")),
        h("p", {class: "small muted"}, t("licences_body"))));
  }
  await load();
}
