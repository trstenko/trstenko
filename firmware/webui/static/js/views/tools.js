// Tools: what Trstenko can do. The built-in ones are listed and locked; the
// ones the owner installed (a .trsttool file) can be switched off or removed.

import {api, clear, h, t, toast} from "/js/core.js";
import {banner, confirmSheet, emptyRow, icon, sectionLabel, subHead, toggle} from "/js/ui.js";

// The eight built-in functions, as the four things an owner would call them.
const BUILT_IN = ["timers", "nav_reminders", "memories", "volume"];
const MAX_FILE = 256 * 1024;

function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(text);
}

/** "Internet: a.example.org · Its own storage", or that it has neither. */
function permissions(tool) {
  const parts = [];
  if (tool.hosts.length) parts.push(t("tools_internet", {hosts: tool.hosts.join(", ")}));
  if (tool.storage) parts.push(t("tools_storage"));
  return parts.length ? parts.join(" · ") : t("tools_no_permissions");
}

export async function render(root) {
  const body = h("div", {class: "stack"});
  const picker = h("input", {type: "file", accept: ".trsttool,application/zip", hidden: true});
  clear(root).append(subHead(t("tools")), body, picker);

  async function change(method, path, payload) {
    const r = await api(method, path, payload);
    if (!r.ok) toast(r.data.error || t("lost_contact"));
    load();
    return r.ok;
  }

  async function remove(tool) {
    const ran = await confirmSheet({
      title: t("tools_remove_title", {name: tool.name}), body: t("tools_remove_body"),
      action: t("tools_remove"), cancel: t("keep"),
      run: async () => {
        const r = await api("DELETE", "/api/v1/tools/" + encodeURIComponent(tool.id));
        return r.ok ? "" : (r.data.error || t("lost_contact"));
      }});
    if (ran) { toast(t("tools_removed")); load(); }
  }

  // Nothing is kept until the owner has read what the file is and what it
  // asks for, and typed the PIN from the device's screen.
  async function install(file) {
    if (file.size > MAX_FILE) { toast(t("tools_too_big")); return; }
    const data = base64(await file.arrayBuffer());
    const seen = await api("POST", "/api/v1/tools/preview", {file: data});
    if (!seen.ok) { toast(seen.data.error || t("lost_contact")); return; }
    const tool = seen.data;
    const may = [];
    if (tool.hosts.length) may.push(t("tools_may_reach", {hosts: tool.hosts.join(", ")}));
    if (tool.storage) may.push(t("tools_may_store"));
    const lines = (items) => h("ul", {class: "plain"}, items.map((x) => h("li", {}, x)));
    const part = (label, content) => h("div", {}, h("div", {class: "muted"}, label), content);
    const many = tool.will_enable && tool.enabled_functions_after >= tool.warn_from;
    const details = h("div", {class: "stack gap-m small"},
      h("p", {class: "muted"}, t("tools_by", {version: tool.version, author: tool.author}),
        tool.replaces ? " · " + t("tools_replaces", {version: tool.replaces}) : null),
      tool.about ? h("p", {}, tool.about) : null,
      part(t("tools_adds"), lines(tool.tools.map((f) => f.name + ": " + f.description))),
      part(t("tools_may"), lines(may.length ? may : [t("tools_may_nothing")])),
      part(t("tools_not_given"), lines([t("tools_not_given_body")])),
      many ? h("p", {class: "c-warn"}, t("tools_many", {n: tool.enabled_functions_after})) : null,
      !tool.will_enable && tool.replaces === null ? h("p", {class: "c-warn"}, t("tools_full", {max: tool.max})) : null,
      h("p", {}, t("tools_warning")));
    let done = null;
    const ran = await confirmSheet({
      title: t("tools_install_title", {name: tool.name}), body: details, action: t("tools_install_action"),
      cancel: t("cancel"), pin: true, agree: t("tools_agree"), calm: true,
      run: async (pin) => {
        const r = await api("POST", "/api/v1/tools", {file: data, pin});
        if (r.ok) { done = r.data.tool; return ""; }
        return r.status === 403 ? t("pin_wrong") : (r.data.error || t("lost_contact"));
      }});
    if (!ran) return;
    toast(done && !done.enabled ? t("tools_installed_off", {max: tool.max}) : t("tools_installed_toast"));
    load();
  }

  picker.addEventListener("change", () => {
    const file = picker.files[0];
    picker.value = "";
    if (file) install(file);
  });

  function row(tool) {
    if (tool.error) {
      return h("div", {class: "row"},
        h("div", {class: "row-main"}, h("span", {class: "row-title"}, tool.name),
          h("span", {class: "row-sub c-danger"}, t("tools_broken", {why: tool.error}))),
        h("button", {class: "btn btn-sm btn-danger-quiet", type: "button", onclick: () => remove(tool)}, t("tools_remove")));
    }
    const id = "tool-" + tool.id.replace(/[^a-z0-9]/g, "-");
    const sw = toggle(id, tool.enabled, async (on) => {
      sw.disabled = true;
      await change("POST", "/api/v1/tools/" + encodeURIComponent(tool.id) + "/enable", {on});
    });
    return h("div", {class: "row", style: "align-items:flex-start"},
      h("div", {class: "row-main", style: "gap:3px"},
        h("label", {class: "row-title", for: id}, tool.name),
        h("span", {class: "row-sub"}, t("tools_by", {version: tool.version, author: tool.author})),
        tool.about ? h("span", {class: "small"}, tool.about) : null,
        h("span", {class: "row-sub"}, permissions(tool)),
        h("div", {}, h("button", {class: "btn btn-sm btn-danger-quiet", type: "button", style: "margin-top:6px",
          "aria-label": t("tools_remove") + ": " + tool.name, onclick: () => remove(tool)}, t("tools_remove")))),
      sw);
  }

  async function load() {
    const r = await api("GET", "/api/v1/tools");
    if (!r.ok) { toast(r.data.error || t("lost_contact")); return; }
    const d = r.data;
    const installed = h("section", {class: "card flush", "aria-label": t("tools_installed")});
    if (!d.installed.length) installed.append(emptyRow(t("tools_none")));
    for (const tool of d.installed) installed.append(row(tool));

    clear(body);
    if (d.enabled_functions >= d.warn_from) body.append(banner(t("tools_many", {n: d.enabled_functions}), "warn"));
    body.append(
      sectionLabel(t("tools_built_in")),
      h("section", {class: "card flush", "aria-label": t("tools_built_in")},
        BUILT_IN.map((key) => h("div", {class: "row"},
          h("span", {class: "row-ico"}, icon("lock", 20, 1.8)),
          h("span", {class: "row-main"}, h("span", {class: "row-title"}, t(key)))))),
      h("p", {class: "note"}, t("tools_built_in_note")),
      h("div", {class: "spread"}, sectionLabel(t("tools_installed")),
        h("span", {class: "small muted num"}, t("tools_on_count", {n: d.enabled_functions, max: d.max}))),
      installed,
      h("div", {}, h("button", {class: "btn btn-primary", type: "button", onclick: () => picker.click()}, t("tools_install"))),
      h("p", {class: "note"}, t("tools_file_hint") + " " + t("tools_trust_note")));
  }
  await load();
}
