// Software updates: every state of the update manager, as it was on the old
// page (updates/manager.py owns them). Only the look changed.

import {api, clear, countdown, h, t, when} from "/js/core.js";
import {facts, meter, result, sectionLabel, subHead} from "/js/ui.js";

const mb = (bytes) => (bytes ? (bytes / 1048576).toFixed(1) + " MB" : "—");

export async function render(root) {
  const body = h("div");
  const checks = h("div", {class: "stack"});
  const info = h("div", {class: "stack"});
  clear(root).append(subHead(t("updates")), body, checks, info);
  let timer = null;
  let alive = true;
  let note = "";

  async function act(path, button) {
    if (button) button.disabled = true;
    const r = await api("POST", "/api/update/" + path, {});
    // A refusal answers with the state plus a reason: keep the reason, the
    // next poll would otherwise wipe it.
    note = r.data.error || (r.ok ? "" : r.data.message || "");
    draw(r.data);
  }
  const button = (label, path, primary) => {
    const b = h("button", {class: "btn" + (primary ? " btn-primary" : ""), type: "button"}, label);
    b.addEventListener("click", () => act(path, b));
    return b;
  };

  async function refresh() {
    if (!alive) return;
    const r = await api("GET", "/api/update");
    if (r.ok) draw(r.data);
    else {
      // Expected while it restarts into a new release: keep asking.
      clear(body).append(result("err", r.data.error || t("lost_contact")));
      timer = setTimeout(refresh, 3000);
    }
  }

  function draw(s) {
    if (!alive || !s || !s.state) return;
    clearTimeout(timer);
    let poll = 4000;
    const rel = s.release;
    const actions = h("div", {class: "hstack wrap gap-m"});
    const extra = h("div", {class: "stack gap-m"});
    const say = (headline, detail, tone) => h("section", {class: "card stack gap-l", "aria-live": "polite"},
      h("div", {class: "stack gap-s"}, h("p", {class: "status sm" + (tone ? " c-" + tone : "")}, headline),
        detail ? h("p", {class: "muted"}, detail) : null), extra, actions);
    let card;
    switch (s.state) {
      case "checking": card = say(t("upd_checking")); break;
      case "up_to_date":
      case "idle":
        card = say(t("upd_up_to_date"), s.last_check ? t("upd_last_checked", {when: when(s.last_check)}) : t("upd_not_checked"));
        actions.append(button(t("upd_check"), "check"));
        break;
      case "awaiting_consent":
        card = say(t("upd_available", {version: rel.version}),
          (rel.summary || t("upd_no_summary")) + " " + t("upd_install_note"));
        actions.append(button(t("upd_install", {version: rel.version}), "approve", true),
          button(t("upd_not_now"), "defer"), button(t("upd_skip"), "skip"));
        break;
      case "scheduled":
        card = say(t("upd_urgent", {version: rel.version}),
          (rel.summary || "") + " " + t("upd_starting_in", {t: countdown(s.seconds_until_auto)}), "warn");
        actions.append(button(t("upd_install_now"), "approve", true), button(t("upd_hold"), "defer"));
        poll = 1000;
        break;
      case "deferred":
        card = say(t("upd_postponed"), t("upd_postponed_note"));
        actions.append(button(t("upd_check"), "check"));
        break;
      case "downloading": {
        const pct = Math.round((s.progress || 0) * 100);
        card = say(t("upd_downloading", {version: rel.version}));
        extra.append(h("div", {class: "spread small"}, h("span", {class: "muted"}, mb(rel.size_bytes)),
          h("span", {class: "num"}, pct + "%")), meter(pct, "progress"));
        actions.append(button(t("cancel"), "cancel"));
        poll = 700;
        break;
      }
      case "staged":
        card = say(t("upd_staged", {version: rel.version}),
          s.will_install ? (s.device && s.device.busy ? t("upd_waiting_turn") : t("upd_installing_soon")) : t("upd_waiting_told"));
        if (!s.will_install) actions.append(button(t("upd_install", {version: rel.version}), "install", true));
        actions.append(button(t("upd_discard"), "cancel"));
        poll = 1500;
        break;
      case "installing":
        card = say(t("upd_installing", {version: rel ? rel.version : ""}), t("upd_installing_note"));
        poll = 1500;
        break;
      case "applied":
      case "restarting":
        card = say(t("upd_restarting"), t("upd_restarting_note"));
        poll = 2000;
        break;
      case "on_trial":
        card = say(t("upd_trial"), t("upd_trial_note"));
        poll = 2000;
        break;
      case "blocked":
        card = say(rel ? t("upd_blocked", {version: rel.version}) : t("upd_cannot"), s.error, "warn");
        actions.append(button(t("upd_check_again"), "check"));
        break;
      case "failed":
        card = say(t("upd_failed"), s.error, "danger");
        actions.append(button(t("upd_try_again"), "check"));
        break;
      default:
        card = say(s.state, s.error || "");
        actions.append(button(t("upd_check"), "check"));
    }
    if (rel && !rel.signed && ["awaiting_consent", "scheduled", "staged"].includes(s.state)) {
      extra.append(result("err", t("upd_unsigned", {note: rel.signature_note})));
    }
    if (note) extra.append(result("err", note));
    if (s.can_revert && ["idle", "up_to_date", "failed"].includes(s.state)) {
      actions.append(button(t("upd_revert"), "revert"));
    }
    extra.hidden = !extra.childNodes.length;
    actions.hidden = !actions.childNodes.length;
    clear(body).append(card);

    const failing = ((s.checks && s.checks.checks) || []).filter((c) => !c.ok);
    clear(checks);
    if (failing.length) {
      checks.append(sectionLabel(failing.some((c) => c.blocking) ? t("upd_in_the_way") : t("upd_worth_knowing")),
        h("section", {class: "card flush"}, failing.map((c) => h("div", {class: "row"},
          h("div", {class: "row-main"}, h("span", {class: "row-title"}, c.name.replace(/_/g, " ")),
            h("span", {class: "row-sub " + (c.blocking ? "c-danger" : "c-warn")}, c.detail || t("failed")))))));
    }
    const d = s.device || {};
    clear(info).append(sectionLabel(t("this_device")), h("section", {class: "card"}, facts([
      [t("firmware"), s.version || s.installed, true],
      [t("installed_as"), s.running_release || t("not_bundle"), !!s.running_release],
      [t("signing"), s.signatures_required ? t("required") : t("no_keys")],
      [t("channel"), s.channel],
      [t("free_space"), d.free_bytes != null ? mb(d.free_bytes) : null],
      [t("network"), d.online === false ? t("offline") : t("online")],
      [t("microphone"), d.muted ? t("mic_off") : t("mic_live")],
    ])));
    timer = setTimeout(refresh, poll);
  }

  await refresh();
  return () => { alive = false; clearTimeout(timer); };
}
