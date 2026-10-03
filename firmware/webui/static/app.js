// The page's script, as it was inline in index.html. Moved out so the
// Content-Security-Policy (plan 07) can forbid inline scripts everywhere:
// `default-src 'self'` means a script has to come from the device itself.
// Nothing in it changed.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[<>&]/g, c => (
  {"<": "&lt;", ">": "&gt;", "&": "&amp;"}[c]));

let poll = 4000;
let timer = null;

// Session storage, not local: closing the tab un-pairs, which is the right
// default for a page anyone on the same Wi-Fi can open.
let token = sessionStorage.getItem("trstenko-token") || "";

function headers() {
  return token ? {"Authorization": "Bearer " + token} : {};
}

function needPin(show, message) {
  $("pair").hidden = !show;
  // Everything else is empty until there is a token, and an empty heading
  // reads as a broken page rather than a locked one.
  for (const id of ["update", "checks-section", "device-section",
                    "about-section", "footer"]) {
    if ($(id)) $(id).hidden = show;
  }
  $("pin-error").textContent = message || "";
  if (show) {
    clearTimeout(timer);
    $("lede").textContent = "This device is locked.";
    $("pin").focus();
  }
}

async function pair(event) {
  event.preventDefault();
  const pin = $("pin").value.trim();
  try {
    const res = await fetch("/api/auth", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({pin: pin})
    });
    const payload = await res.json();
    if (res.ok && payload.token !== undefined) {
      token = payload.token;
      sessionStorage.setItem("trstenko-token", token);
      $("pin").value = "";
      needPin(false);
      refresh();
      return;
    }
    if (res.status === 429) {
      needPin(true, "Too many wrong PINs. Try again in "
        + Math.ceil(payload.retry_after) + " seconds.");
    } else {
      needPin(true, payload.error || "That did not work.");
    }
  } catch (err) {
    needPin(true, "Lost contact with the device.");
  }
}

function mb(bytes) {
  if (!bytes) return "—";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function countdown(seconds) {
  if (seconds == null) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  return m + "m " + (s % 60) + "s";
}

function rows(target, pairs) {
  target.innerHTML = pairs.map(([k, v, cls]) =>
    `<div class="row ${cls || ""}"><dt class="k">${esc(k)}</dt>` +
    `<dd class="v">${esc(v)}</dd></div>`).join("");
}

function button(label, action, primary) {
  const b = document.createElement("button");
  b.textContent = label;
  if (primary) b.className = "go";
  b.onclick = () => post(action, b);
  return b;
}

async function post(path, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/update/" + path, {
      method: "POST",
      headers: Object.assign({"Content-Type": "application/json"}, headers()),
      body: "{}"
    });
    if (res.status === 401) { token = ""; needPin(true); return; }
    const payload = await res.json();
    // A refusal answers with the state plus a reason. Keep the reason: the
    // poll that follows would otherwise wipe it within a second.
    render(payload);
    if (payload.error) $("lede").textContent = payload.error;
  } catch (err) {
    $("lede").textContent = "Lost contact with the device.";
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/update", {headers: headers()});
    if (res.status === 401) {
      token = "";
      sessionStorage.removeItem("trstenko-token");
      needPin(true);
      return;
    }
    render(await res.json());
  } catch (err) {
    $("lede").textContent = "Lost contact with the device.";
  }
}

function render(s) {
  if (!s || !s.state) return;
  needPin(false);
  $("installed").textContent = s.version || s.installed || "";

  const rel = s.release;
  const body = $("update-body");
  const actions = $("actions");
  actions.innerHTML = "";
  body.innerHTML = "";

  const say = (headline, detail, cls) => {
    body.innerHTML =
      `<p class="headline">${esc(headline)}</p>` +
      (detail ? `<p class="detail">${esc(detail)}</p>` : "");
    if (cls) body.className = cls;
    else body.removeAttribute("class");
  };

  switch (s.state) {
    case "checking":
      say("Checking for a newer release.", "");
      break;

    case "up_to_date":
    case "idle":
      say("Up to date.",
          s.last_check
            ? "Last checked " + new Date(s.last_check * 1000).toLocaleString() + "."
            : "This device has not checked yet.");
      actions.append(button("Check now", "check"));
      break;

    case "awaiting_consent":
      say(rel.version + " is available.",
          (rel.summary || "No summary was published with this release.") +
          " Installing downloads it, unpacks it and restarts — a few " +
          "minutes, and you do not need to stay on this page.");
      actions.append(button("Install " + rel.version, "approve", true));
      actions.append(button("Not now", "defer"));
      actions.append(button("Skip this version", "skip"));
      break;

    case "scheduled":
      body.innerHTML =
        `<div class="notice"><p class="headline">` +
        `${esc(rel.version)} is an urgent update. It will download, ` +
        `install and restart on its own.</p>` +
        `<p class="detail">${esc(rel.summary || "")}</p>` +
        `<p class="detail">Starting in ${esc(countdown(s.seconds_until_auto))}.` +
        `</p></div>`;
      actions.append(button("Install now", "approve", true));
      actions.append(button("Hold it for a few hours", "defer"));
      poll = 1000;
      break;

    case "deferred":
      say("Postponed.", "The device will offer it again later.");
      actions.append(button("Check now", "check"));
      break;

    case "downloading":
      say("Downloading " + rel.version + ".", mb(rel.size_bytes) + " total.");
      const bar = document.createElement("div");
      bar.className = "bar";
      bar.innerHTML = `<i style="width:${Math.round((s.progress || 0) * 100)}%"></i>`;
      body.append(bar);
      actions.append(button("Cancel", "cancel"));
      poll = 700;
      break;

    case "staged":
      // Normally a step you never see: a yes carries through to the install
      // on its own. It shows when the device is waiting for something --
      // mid-conversation, or a bundle left staged by an earlier boot.
      say(rel.version + " is downloaded and its checksum matches.",
          s.will_install
            ? (s.device && s.device.busy
                ? "Waiting for the conversation to finish, then installing."
                : "Installing shortly.")
            : "Waiting to be told to install it.");
      if (!s.will_install) {
        actions.append(button("Install " + rel.version, "install", true));
      }
      actions.append(button("Discard the download", "cancel"));
      poll = 1500;
      break;

    case "installing":
      say("Installing " + (rel ? rel.version : "") + ".",
          "Unpacking and building. The device restarts when it is done.");
      poll = 1500;
      break;

    case "applied":
    case "restarting":
      say("Restarting into " + (s.applied ? s.applied.version : "the new " +
          "release") + ".",
          "This page will come back on its own. If the new version does not " +
          "start, the device goes back to " +
          ((s.applied && s.applied.previous) || "the previous release") +
          " by itself.");
      poll = 2000;
      break;

    case "on_trial":
      say("Checking that " + (s.on_trial ? s.on_trial.new : "this release") +
          " works.",
          "It has a couple of minutes to come up properly. If it does not, " +
          "the device returns to " +
          ((s.on_trial && s.on_trial.previous) || "the previous release") +
          " without being asked.");
      poll = 2000;
      break;

    case "blocked":
      body.innerHTML =
        `<div class="notice"><p class="headline">` +
        `${esc(rel ? rel.version + " cannot be installed right now." : "Cannot update.")}` +
        `</p><p class="detail">${esc(s.error)}</p></div>`;
      actions.append(button("Check again", "check"));
      break;

    case "failed":
      body.innerHTML =
        `<div class="notice"><p class="headline">The update failed.</p>` +
        `<p class="detail">${esc(s.error)}</p></div>`;
      actions.append(button("Try again", "check"));
      break;

    default:
      say(s.state, s.error || "");
      actions.append(button("Check now", "check"));
  }

  if (!["scheduled", "downloading"].includes(s.state)) poll = 4000;

  // Only worth a section when something failed. A list of eight rows all
  // reading "ok" is noise on a phone held at arm's length.
  const all = (s.checks && s.checks.checks) || [];
  // An unsigned release is worth saying out loud even when the device is
  // configured to accept one.
  if (rel && !rel.signed && ["awaiting_consent", "scheduled", "staged"]
      .includes(s.state)) {
    body.insertAdjacentHTML("beforeend",
      `<p class="detail" style="color:var(--amber)">This release is not ` +
      `verified: ${esc(rel.signature_note)}.</p>`);
  }

  const checks = all.filter(c => !c.ok);
  $("checks-section").hidden = !checks.length;
  if (checks.length) {
    $("checks-title").textContent = checks.some(c => c.blocking)
      ? "What is in the way" : "Worth knowing";
    rows($("checks"), checks.map(c => [
      c.name.replace(/_/g, " "),
      c.ok ? "ok" : (c.detail || "failed"),
      c.ok ? "ok" : (c.blocking ? "fail" : "warn")
    ]));
  }

  const d = s.device || {};
  rows($("device"), [
    ["firmware", s.version || s.installed],
    ["installed as", s.running_release || "not from a release bundle"],
    ["base image", s.base != null ? "v" + s.base : "—"],
    ["release signing", s.signatures_required
      ? "required" : "no keys on this device",
      s.signatures_required ? "ok" : "warn"],
    ["channel", s.channel],
    ["releases", s.repo],
    ["free space", d.free_bytes != null ? mb(d.free_bytes) : "—"],
    ["network", d.online === false ? "offline" : "connected",
      d.online === false ? "fail" : "ok"],
    ["microphone", d.muted ? "switched off at the back" : "live"],
    ["face", d.face || "—"],
  ]);

  $("lede").textContent = rel
    ? "Software update waiting."
    : "Everything on this page is served by the device itself.";
  const revert = $("revert");
  revert.innerHTML = "";
  if (s.can_revert && ["idle", "up_to_date", "failed"].includes(s.state)) {
    revert.append(button("Go back to the previous release", "revert"));
  }

  const p = s.product || {};
  rows($("about"), [
    ["model", p.model_id ? p.model + " (" + p.model_id + ")" : "—"],
    ["serial", p.serial || "—"],
    ["made by", p.manufacturer || "—"],
    ["address", p.address || "—"],
    ["security updates until", p.support_until || "—"],
    ["report a problem", p.security_contact || "—"],
  ]);
  $("ai-notice").textContent = p.ai_notice || "";

  $("footer").textContent =
    "Connected to Trstenko over its own Wi-Fi. Nothing on this page leaves " +
    "the device. Updates replace the app, not the operating system — a " +
    "release that needs a newer base image says so and asks for the card to " +
    "be reflashed.";

  clearTimeout(timer);
  timer = setTimeout(refresh, poll);
}

$("pin-form").addEventListener("submit", pair);
refresh();
