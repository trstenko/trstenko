// First run: a provider and its key, a test the device answers aloud, then a
// name and a language. (Joining home Wi-Fi comes with the board, plan 07.)

import {api, clear, h, loadStrings, t, toast} from "/js/core.js";
import {createFace, facePanel} from "/js/face.js";
import {busy, field, input, result, save, seg} from "/js/ui.js";
import {hostBox, waitForTest} from "/js/views/assistant.js";

export async function render(root) {
  // key -> hello (the test is running) -> passed -> name -> done
  let step = "key";
  let problem = "";
  const face = createFace(t("face_label"));
  const panel = facePanel(face, "setup");
  const bars = [h("span", {class: "on"}), h("span"), h("span")];
  const stepLabel = h("p", {class: "label"});
  const body = h("div", {class: "stack", style: "gap:16px"});
  clear(root).append(
    h("div", {class: "stack gap-m", style: "padding-top:8px"},
      h("div", {class: "steps", "aria-hidden": "true"}, bars), stepLabel),
    panel, body);
  face.start();

  const keys = await api("GET", "/api/v1/keys");
  const settings = await api("GET", "/api/v1/settings");
  if (!keys.ok || !settings.ok) {
    body.append(h("p", {class: "c-danger"}, keys.data.error || settings.data.error));
    return () => face.stop();
  }
  const infos = keys.data.providers;
  let chosen = settings.data.settings.assistant.provider;
  let status = keys.data;
  const owner = settings.data.settings.owner;
  const info = () => infos.find((p) => p.id === chosen) || infos[0];

  function draw() {
    const n = step === "name" ? 2 : step === "done" ? 3 : 1;
    bars.forEach((el, i) => el.classList.toggle("on", i < n));
    stepLabel.textContent = t("step_of", {n, total: 3});
    const big = ["hello", "passed", "done"].includes(step);
    panel.classList.toggle("big", big);
    if (step === "key") face.sleep();
    else face.set(step === "hello" ? "face_talking" : step === "done" ? "face_listening" : "face_idle",
      step === "hello" ? 0.5 : 0);
    clear(body);
    if (n === 1) keyStep();
    else if (n === 2) nameStep();
    else doneStep();
  }

  function keyStep() {
    const p = info();
    const keyed = p.needs_key;
    const testBtn = h("button", {class: "btn grow" + (step === "key" ? " btn-primary" : ""), type: "button"},
      step === "hello" ? t("testing") : step === "passed" ? t("test_again") : t("test"));
    const next = h("button", {class: "btn grow" + (step === "passed" || !keyed ? " btn-primary" : ""), type: "button",
      onclick: () => { step = "name"; draw(); }}, t("continue"));
    testBtn.disabled = step === "hello";
    next.disabled = keyed && step !== "passed";
    const head = h("div", {class: "stack", style: "gap:4px"}, h("h1", {class: "title"}, t("setup_title")),
      step === "key" ? h("p", {class: "muted"}, t("setup_help")) : null);

    if (step !== "key") {
      body.append(h("div", {class: "stack gap-l"}, head,
        h("div", {class: "keyline"}, h("span", {class: "row-title"}, p.label),
          h("span", {class: "mono muted end"}, "•••• " + ((status[p.id] || {}).last4 || ""))),
        step === "hello" ? h("p", {class: "status sm", "aria-live": "polite"}, t("setup_listen"))
          : result("ok", t("setup_passed")),
        h("div", {class: "hstack gap-m"}, testBtn, next)));
      testBtn.addEventListener("click", () => runTest());
      return;
    }

    const key = input({id: "fr-key", type: "password", class: "input mono", autocomplete: "off", spellcheck: "false",
      placeholder: (status[p.id] || {}).set ? "•••• " + status[p.id].last4 : t("paste_key")});
    const list = h("fieldset", {class: "radio-list"}, h("legend", {class: "vh"}, t("provider")),
      infos.map((x) => h("label", {class: "radio-row"},
        h("input", {type: "radio", name: "p", value: x.id, checked: x.id === chosen}),
        x.needs_key ? h("span", {class: "row-title"}, x.label)
          : h("span", {class: "row-main"}, h("span", {class: "row-title"}, x.label),
            h("span", {class: "row-sub"}, t("own_computer"))))));
    list.addEventListener("change", async (event) => {
      if (!(await save({assistant: {provider: event.target.value, model: ""}}))) return;
      chosen = event.target.value;
      problem = "";
      draw();
    });
    testBtn.addEventListener("click", () => busy(testBtn, async () => {
      // Test saves what was typed first; with the box empty it tests the key
      // the device already has.
      if (key.value.trim()) {
        const r = await api("POST", "/api/v1/keys/" + p.id, {key: key.value.trim()});
        if (!r.ok) { problem = r.data.error; draw(); return; }
        status = Object.assign({}, status, {[p.id]: r.data});
      } else if (!(status[p.id] || {}).set) { key.focus(); return; }
      await runTest();
    }));
    body.append(h("div", {class: "stack gap-l"}, head, list,
      keyed ? h("div", {class: "field"}, h("label", {for: "fr-key"}, t("api_key")), key,
        p.key_help_url ? h("a", {class: "small", href: p.key_help_url, target: "_blank", rel: "noopener noreferrer"},
          t("get_key", {provider: p.label})) : null)
        : hostBox(status.ollama || {}),
      problem ? result("err", problem) : null,
      h("div", {class: "hstack gap-m"}, keyed ? testBtn : null, next),
      keyed ? h("p", {class: "hint"}, t("setup_test_hint")) : null,
      h("div", {}, h("button", {class: "btn btn-quiet", type: "button",
        onclick: () => { step = "name"; draw(); }}, t("skip_for_now")))));
  }

  async function runTest() {
    const p = info();
    const r = await api("POST", "/api/v1/keys/" + p.id + "/test");
    if (!r.ok) { step = "key"; problem = r.data.error; draw(); return; }
    step = "hello";
    problem = "";
    draw();
    const test = await waitForTest(p.id);
    if (test.state === "ok") step = "passed";
    else { step = "key"; problem = test.message || t("key_failed", {provider: p.label}); }
    draw();
  }

  function nameStep() {
    const name = input({type: "text", value: owner.name, maxlength: "40", autocomplete: "off", placeholder: t("name_placeholder")});
    const language = seg(t("language"), [["en", "English"], ["sl", "Slovenščina", "sl"]], owner.language, null, true);
    const next = h("button", {class: "btn btn-primary grow", type: "button"}, t("continue"));
    next.addEventListener("click", () => busy(next, async () => {
      owner.name = name.value.trim();
      owner.language = language.value;
      if (!(await save({owner: {name: owner.name, language: owner.language}}))) return;
      await loadStrings(owner.language);
      step = "done";
      draw();
    }));
    body.append(h("h1", {class: "title"}, t("about_you")),
      h("div", {class: "field"}, h("label", {for: "fr-name"}, t("your_name"), " ",
        h("span", {class: "muted", style: "font-weight:500"}, t("optional"))), (name.id = "fr-name", name)),
      h("div", {class: "field"}, h("span", {class: "field-label"}, t("language")), language,
        h("p", {class: "hint"}, t("language_hint"))),
      h("div", {class: "hstack gap-m"},
        h("button", {class: "btn grow", type: "button", onclick: () => {
          step = (status[chosen] || {}).set && info().needs_key ? "passed" : "key";
          if (step === "passed" && (status[chosen].test || {}).state !== "ok") step = "key";
          draw();
        }}, t("back")), next));
  }

  function doneStep() {
    body.append(h("div", {class: "stack gap-s"}, h("h1", {class: "status"}, t("setup_done")),
      h("p", {style: "font-size:18px"}, t("setup_done_body"))),
      // A full load, not just a new hash: the navigation is built in the
      // language that was just chosen.
      h("button", {class: "btn btn-primary btn-lg btn-block", type: "button", onclick: () => {
        location.hash = "#/home";
        location.reload();
      }}, t("go_home")),
      h("p", {class: "hint"}, t("wifi_later")));
  }

  draw();
  return () => face.stop();
}
