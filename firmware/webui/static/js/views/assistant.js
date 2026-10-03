// The assistant: which provider, its key (write-only), and a real test.
// The key is typed here and sent once; the device never sends it back, only
// whether one is set and its last four characters.

import {api, clear, h, t, toast} from "/js/core.js";
import {createFace, facePanel} from "/js/face.js";
import {busy, field, icon, input, pill, result, save, select, subHead, toggleRow} from "/js/ui.js";

/** Ask for the test's result until it has one. The test runs on the device's
 *  worker and never blocks it; a slow provider just takes a few asks. */
export async function waitForTest(provider) {
  for (let i = 0; i < 40; i++) {
    const r = await api("GET", "/api/v1/keys");
    const test = r.ok ? r.data[provider].test : null;
    if (test && test.state !== "running") return test;
    await new Promise((go) => setTimeout(go, 500));
  }
  return {state: "failed"};
}

/** The key box for one provider: empty, set, testing, passed or refused.
 *  `info` is the provider's row from /api/v1/keys and `status` its key status.
 *  `onChange` runs after a key is saved or removed. */
export function keyBox(info, status, onChange) {
  const box = h("div", {class: "stack", style: "gap:10px"});
  let replacing = false;
  let test = status.test && status.test.state !== "none" ? status.test : null;

  function draw() {
    clear(box);
    const title = h("div", {class: "spread"},
      h("h2", {class: "field-label", id: "h-key-" + info.id, style: "font-size:15px"}, t("api_key_for", {provider: info.label})),
      status.set ? pill(t("key_set"), "", icon("check", 14, 2.6)) : pill(t("no_key"), "neutral"));
    box.append(title);

    if (!status.set || replacing) {
      const key = input({type: "password", class: "input mono", autocomplete: "off", spellcheck: "false",
        placeholder: t("paste_key"), "aria-labelledby": "h-key-" + info.id});
      const saveBtn = h("button", {class: "btn btn-primary", type: "button"}, t("save_key"));
      saveBtn.addEventListener("click", () => busy(saveBtn, async () => {
        if (!key.value.trim()) { key.focus(); return; }
        const r = await api("POST", "/api/v1/keys/" + info.id, {key: key.value.trim()});
        if (!r.ok) { toast(r.data.error); return; }
        status = Object.assign({}, r.data);
        replacing = false;
        test = null;
        draw();
        if (onChange) onChange();
      }));
      box.append(key, h("p", {class: "hint"}, t("key_stored_hint")),
        h("div", {class: "hstack wrap gap-m"}, saveBtn,
          replacing ? h("button", {class: "btn", type: "button", onclick: () => { replacing = false; draw(); }}, t("cancel")) : null,
          replacing ? h("button", {class: "btn btn-danger-quiet", type: "button", onclick: async () => {
            const r = await api("POST", "/api/v1/keys/" + info.id, {key: ""});
            if (!r.ok) { toast(r.data.error); return; }
            status = Object.assign({}, r.data);
            replacing = false;
            test = null;
            draw();
            if (onChange) onChange();
          }}, t("remove_key")) : null));
      if (replacing) key.focus();
    } else {
      const refused = test && test.state === "failed";
      const testBtn = h("button", {class: "btn" + (refused ? "" : " btn-primary"), type: "button"}, t("test_key"));
      testBtn.disabled = !!test && test.state === "running";
      testBtn.addEventListener("click", async () => {
        const r = await api("POST", "/api/v1/keys/" + info.id + "/test");
        if (!r.ok) { toast(r.data.error); return; }
        test = {state: "running"};
        draw();
        test = await waitForTest(info.id);
        draw();
      });
      box.append(h("div", {class: "keyline"},
        h("span", {class: "mono dots"}, "•••• " + (status.last4 || "")),
        !test && status.checked_ok ? h("span", {class: "muted small end"}, t("key_worked_before")) : null));
      if (test && test.state === "running") {
        const face = createFace(t("face_label"));
        face.set("face_talking", 0.5);
        box.append(h("div", {class: "result busy", "aria-live": "polite"}, facePanel(face, "tiny"),
          h("span", {}, t("testing_listen"))));
      } else if (test && test.state === "ok") {
        box.append(result("ok", t("key_works")));
      } else if (refused) {
        box.append(result("err", test.message || t("key_failed", {provider: info.label})));
      }
      box.append(h("div", {class: "hstack wrap gap-m"}, testBtn,
        h("button", {class: "btn" + (refused ? " btn-primary" : ""), type: "button",
          onclick: () => { replacing = true; draw(); }}, t("replace_key"))));
    }
    if (info.key_help_url) {
      box.append(h("p", {class: "small"}, h("a", {href: info.key_help_url, target: "_blank", rel: "noopener noreferrer"},
        t("get_key", {provider: info.label}))));
    }
    if (!info.verified) box.append(h("p", {class: "hint"}, t("not_verified")));
  }
  draw();
  return box;
}

/** Ollama runs on the owner's own computer: an address, not a key. */
export function hostBox(status, onChange) {
  const host = input({type: "url", class: "input mono", autocomplete: "off", spellcheck: "false",
    value: status.host || "", placeholder: "http://192.168.1.20:11434"});
  const btn = h("button", {class: "btn btn-primary", type: "button"}, t("save"));
  btn.addEventListener("click", () => busy(btn, async () => {
    const r = await api("POST", "/api/v1/keys/ollama", {host: host.value.trim()});
    if (!r.ok) { toast(r.data.error); return; }
    toast(t("saved"));
    if (onChange) onChange();
  }));
  return h("div", {class: "stack", style: "gap:10px"}, field("ollama-host", t("ollama_host"), host, t("ollama_hint")),
    h("div", {}, btn));
}

export async function render(root) {
  const body = h("div", {class: "stack"});
  clear(root).append(subHead(t("assistant")), body);

  async function load() {
    const [keys, settings] = await Promise.all([api("GET", "/api/v1/keys"), api("GET", "/api/v1/settings")]);
    if (!keys.ok || !settings.ok) {
      clear(body).append(h("p", {class: "c-danger"}, keys.data.error || settings.data.error));
      return;
    }
    const a = settings.data.settings.assistant;
    const infos = keys.data.providers;
    const current = infos.find((p) => p.id === a.provider) || infos[0];
    const second = infos.find((p) => p.id === a.fallback_provider);

    const provider = select(infos.map((p) => [p.id, p.label]), a.provider);
    provider.addEventListener("change", async () => {
      if (await save({assistant: {provider: provider.value, model: ""}})) load();
    });
    const model = select([["", t("default_model", {model: current.default_model})],
      ...current.models.map((m) => [m, m])], a.model, "mono");
    model.addEventListener("change", () => save({assistant: {model: model.value}}, true));
    const fallback = select([["", t("none")], ...infos.filter((p) => p.id !== a.provider && p.needs_key)
      .map((p) => [p.id, p.label])], a.fallback_provider);
    fallback.addEventListener("change", async () => {
      if (await save({assistant: {fallback_provider: fallback.value}})) load();
    });
    const free = toggleRow("free-tier", t("free_tier_switch"), t("free_tier_switch_hint"), a.free_tier,
      (on) => save({assistant: {free_tier: on}}));
    free.style.padding = "10px 0 0";

    clear(body).append(
      h("section", {class: "card stack gap-l"},
        field("prov", t("provider"), provider,
          current.free_tier ? t("free_tier", {note: current.free_tier}) : null),
        h("div", {class: "divider"}),
        current.needs_key ? keyBox(current, keys.data[current.id]) : hostBox(keys.data.ollama)),
      h("p", {class: "note"}, t("http_note")),
      h("section", {class: "card"}, h("details", {},
        h("summary", {}, t("advanced"), icon("down", 18)),
        h("div", {class: "stack gap-l", style: "padding-top:8px"},
          h("div", {class: "grid2"}, field("model", t("model"), model, t("model_hint")),
            field("fb", t("fallback"), fallback, t("fallback_hint"))),
          second ? keyBox(second, keys.data[second.id]) : null,
          h("div", {class: "divider"}), free))));
  }
  await load();
}
