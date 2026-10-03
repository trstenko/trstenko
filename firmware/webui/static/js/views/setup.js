// First run: join the home Wi-Fi (only while the device is in setup mode, on
// its own hotspot), then a provider and its key with a test the device answers
// aloud, then a name and a language.

import {api, clear, h, loadStrings, t, toast} from "/js/core.js";
import {createFace, facePanel} from "/js/face.js";
import {busy, field, input, result, save, seg} from "/js/ui.js";
import {hostBox, waitForTest} from "/js/views/assistant.js";

export async function render(root) {
  // wifi -> joining -> key -> hello (the test is running) -> passed -> name -> done
  let step = "key";
  let wifi = null;          // the device's setup status, while it is in setup mode
  let picked = "";
  let poll = null;
  let problem = "";
  const face = createFace(t("face_label"));
  const panel = facePanel(face, "setup");
  const barBox = h("div", {class: "steps", "aria-hidden": "true"});
  const stepLabel = h("p", {class: "label"});
  const body = h("div", {class: "stack", style: "gap:16px"});
  clear(root).append(
    h("div", {class: "stack gap-m", style: "padding-top:8px"},
      barBox, stepLabel),
    panel, body);
  face.start();

  const keys = await api("GET", "/api/v1/keys");
  const settings = await api("GET", "/api/v1/settings");
  if (!keys.ok || !settings.ok) {
    body.append(h("p", {class: "c-danger"}, keys.data.error || settings.data.error));
    return () => face.stop();
  }
  const net = await api("GET", "/api/v1/setup");
  if (net.ok && ["starting", "waiting", "joining"].includes(net.data.state)) {
    wifi = net.data;
    step = wifi.state === "joining" ? "joining" : "wifi";
  }
  const withWifi = !!wifi;
  const infos = keys.data.providers;
  let chosen = settings.data.settings.assistant.provider;
  let status = keys.data;
  const owner = settings.data.settings.owner;
  const info = () => infos.find((p) => p.id === chosen) || infos[0];

  function draw() {
    const onWifi = step === "wifi" || step === "joining";
    const first = withWifi ? 1 : 0;
    const n = onWifi ? 1 : first + (step === "name" ? 2 : step === "done" ? 3 : 1);
    const total = 3 + first;
    clear(barBox).append(...Array.from({length: total}, (_, i) => h("span", {class: i < n ? "on" : ""})));
    stepLabel.textContent = t("step_of", {n, total});
    const big = ["hello", "passed", "done"].includes(step);
    panel.classList.toggle("big", big);
    clearTimeout(poll);
    if (step === "key" || onWifi) face.sleep();
    else face.set(step === "hello" ? "face_talking" : step === "done" ? "face_listening" : "face_idle",
      step === "hello" ? 0.5 : 0);
    clear(body);
    if (step === "wifi") wifiStep();
    else if (step === "joining") joiningStep();
    else if (step === "name") nameStep();
    else if (step === "done") doneStep();
    else keyStep();
  }

  // The device is on its own hotspot and so is this phone. Pick the home
  // network; the device leaves the hotspot to join it, and takes the page's
  // connection with it.
  function wifiStep() {
    const nets = wifi.networks || [];
    if (!nets.some((x) => x.ssid === picked)) picked = nets.length ? nets[0].ssid : "";
    const chosen = () => nets.find((x) => x.ssid === picked) || {secure: true};
    const password = input({id: "fr-wifi", type: "password", autocomplete: "off", spellcheck: "false", maxlength: "63"});
    const passField = h("div", {class: "field", hidden: !chosen().secure},
      h("label", {for: "fr-wifi"}, t("wifi_password")), password);
    const list = h("fieldset", {class: "radio-list"}, h("legend", {class: "vh"}, t("wifi_network")),
      nets.map((x) => h("label", {class: "radio-row"},
        h("input", {type: "radio", name: "w", value: x.ssid, checked: x.ssid === picked}),
        h("span", {class: "row-main"}, h("span", {class: "row-title"}, x.ssid),
          x.secure ? null : h("span", {class: "row-sub"}, t("wifi_open"))),
        h("span", {class: "muted small"}, x.signal + "%"))));
    list.addEventListener("change", (event) => {
      picked = event.target.value;
      passField.hidden = !chosen().secure;
    });
    const join = h("button", {class: "btn btn-primary grow", type: "button"}, t("wifi_join"));
    const again = h("button", {class: "btn grow", type: "button"}, wifi.scanning ? t("wifi_scanning") : t("wifi_scan"));
    join.disabled = !nets.length || wifi.state !== "waiting";
    again.disabled = wifi.scanning || wifi.state !== "waiting";
    join.addEventListener("click", () => busy(join, async () => {
      if (chosen().secure && password.value.length < 8) { password.focus(); return; }
      const r = await api("POST", "/api/v1/setup/join", {ssid: picked, password: chosen().secure ? password.value : ""});
      if (!r.ok) { wifi.error = r.data.error; draw(); return; }
      wifi = r.data;
      step = "joining";
      draw();
    }));
    again.addEventListener("click", () => busy(again, async () => {
      // The hotspot drops for a moment while the device looks around.
      const r = await api("POST", "/api/v1/setup/scan");
      if (r.ok) wifi = r.data;
      draw();
    }));
    body.append(h("div", {class: "stack", style: "gap:4px"}, h("h1", {class: "title"}, t("wifi_title")),
      h("p", {class: "muted"}, t("wifi_help"))),
      nets.length ? list : h("p", {class: "muted"}, wifi.state === "starting" ? t("wifi_starting") : t("wifi_none")),
      passField, wifi.error ? result("err", wifi.error) : "",
      h("div", {class: "hstack gap-m"}, again, join),
      h("div", {}, h("button", {class: "btn btn-quiet", type: "button",
        onclick: () => { step = "key"; draw(); }}, t("skip_for_now"))));
    if (wifi.state === "starting" || wifi.scanning) poll = setTimeout(refreshWifi, 1500);
  }

  function joiningStep() {
    const ssid = wifi.joining || picked;
    const url = wifi.home_url || "http://trstenko.local";
    body.append(h("div", {class: "stack gap-s"}, h("h1", {class: "title"}, t("wifi_joining", {ssid})),
      h("p", {class: "muted"}, t("wifi_joining_help", {ssid}))),
      h("a", {class: "btn btn-primary btn-lg btn-block", href: url},
        t("wifi_open_home", {url: url.replace("http://", "")})),
      h("p", {class: "hint"}, t("wifi_address_hint")));
    poll = setTimeout(refreshWifi, 3000);
  }

  // While it joins, the device is out of reach, and that is expected. If the
  // join fails the hotspot comes back, the phone finds it again, and the page
  // can say why.
  async function refreshWifi() {
    const r = await api("GET", "/api/v1/setup");
    if (step !== "wifi" && step !== "joining") return;
    if (r.ok) {
      wifi = r.data;
      if (wifi.state === "waiting" || wifi.state === "starting") step = "wifi";
      else if (wifi.state !== "joining") step = "key";   // joined, and still in reach
      draw();
    } else {
      poll = setTimeout(refreshWifi, 3000);
    }
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
      }}, t("go_home")));
  }

  draw();
  return () => { face.stop(); clearTimeout(poll); };
}
