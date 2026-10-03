// Memories: seeing what it knows about you, and deleting any of it.

import {api, clear, h, t, toast} from "/js/core.js";
import {busy, emptyRow, field, input, meter, subHead} from "/js/ui.js";

export async function render(root) {
  const count = h("section", {class: "card stack gap-m"});
  const list = h("section", {class: "card flush", "aria-label": t("memories")});
  const text = input({type: "text", maxlength: "200", placeholder: t("memory_placeholder"), autocomplete: "off"});
  const add = h("button", {class: "btn btn-primary", type: "submit"}, t("add_memory_btn"));
  const form = h("form", {class: "card stack gap-l", "aria-labelledby": "h-mem"},
    h("h2", {class: "h2", id: "h-mem"}, t("add_memory")),
    field("m-text", t("memory_text"), text), h("div", {}, add));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!text.value.trim()) { text.focus(); return; }
    const r = await busy(add, () => api("POST", "/api/v1/memories", {text: text.value.trim()}));
    if (!r.ok) { toast(r.data.error); return; }
    text.value = "";
    load();
  });
  clear(root).append(subHead(t("memories")), h("p", {class: "note"}, t("memories_help")), count, list, form);

  async function change(method, path, body) {
    const r = await api(method, path, body);
    if (!r.ok) toast(r.data.error);
    load();
  }
  async function load() {
    const r = await api("GET", "/api/v1/memories");
    if (!r.ok) { toast(r.data.error); return; }
    clear(count).append(
      h("div", {class: "spread small"}, h("span", {}, t("memories_saved")),
        h("span", {class: "num"}, t("n_of_max", {n: r.data.count, max: r.data.limit}))),
      meter((r.data.count / r.data.limit) * 100, "thin"));
    clear(list);
    if (!r.data.memories.length) list.append(emptyRow(t("no_memories")));
    for (const m of r.data.memories) {
      list.append(h("div", {class: "row"},
        h("span", {class: "row-main"}, h("span", {class: "row-title"}, m.text),
          m.pinned ? h("span", {class: "row-sub"}, t("pinned_note")) : null),
        h("button", {class: "btn btn-quiet", type: "button",
          onclick: () => change("POST", "/api/v1/memories/" + m.id + "/pin", {pinned: !m.pinned})},
          m.pinned ? t("unpin") : t("pin")),
        h("button", {class: "btn btn-quiet c-danger", type: "button", "aria-label": t("delete") + ": " + m.text,
          onclick: () => change("DELETE", "/api/v1/memories/" + m.id)}, t("delete"))));
    }
  }
  await load();
}
