// Pairing: the six digits on Trstenko's screen, exchanged for a token.

import {clear, h, pair, t} from "/js/core.js";
import {createFace, facePanel} from "/js/face.js";
import {input} from "/js/ui.js";

export async function render(root) {
  const face = createFace(t("face_asleep"));
  face.sleep();
  const pin = input({id: "pin", class: "input input-pin", inputmode: "numeric", pattern: "[0-9]*",
    maxlength: "6", autocomplete: "one-time-code", placeholder: "······",
    "aria-describedby": "pin-msg"});
  const message = h("p", {id: "pin-msg", "aria-live": "polite"});
  const submit = h("button", {class: "btn btn-primary btn-lg btn-block", type: "submit"}, t("unlock"));
  const form = h("form", {class: "stack", style: "gap:20px"},
    h("div", {class: "field"}, h("label", {class: "vh", for: "pin"}, t("pin_label")), pin, message),
    submit);

  let timer = null;
  function say(text, bad) {
    message.className = bad ? "result err center" : "hint center";
    message.textContent = text;
  }
  // After too many wrong PINs the device refuses for a while. Count it down
  // rather than letting each attempt be silently ignored.
  function lockOut(seconds) {
    let left = Math.ceil(seconds);
    pin.disabled = submit.disabled = true;
    pin.value = "";
    const tick = () => {
      if (left <= 0) {
        clearInterval(timer);
        pin.disabled = submit.disabled = false;
        say(t("pin_hint"));
        pin.focus();
        return;
      }
      say(t("pin_wait", {s: left}), true);
      left -= 1;
    };
    clearInterval(timer);
    tick();
    timer = setInterval(tick, 1000);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pin.value.trim()) return;
    submit.disabled = true;
    const r = await pair(pin.value.trim());
    submit.disabled = false;
    if (r.ok) {
      // Start again from the top: the language and the first-run check both
      // depend on what the device can now tell us.
      location.hash = "";
      location.reload();
      return;
    }
    if (r.status === 429) { lockOut(r.data.retry_after || 30); return; }
    say(r.status === 401 ? t("pin_wrong") : (r.data.error || t("lost_contact")), true);
    pin.select();
  });

  say(t("pin_hint"));
  clear(root).append(
    facePanel(face, "lock"),
    h("div", {class: "stack gap-s center"}, h("h1", {class: "title"}, t("pin_title")),
      h("p", {class: "muted"}, t("pin_help"))),
    form);
  pin.focus();
  return () => clearInterval(timer);
}
