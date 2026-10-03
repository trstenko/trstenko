// Usage: this month against the limit, the cost per day, and the limits.

import {api, clear, h, lang, money, shortDay, t, toast} from "/js/core.js";
import {banner, busy, head, meter, moneyInput, pill, save, seg, input} from "/js/ui.js";

const DAY = 86400;

export async function render(root) {
  let days = 30;
  const body = h("div", {class: "stack"});
  clear(root).append(head(t("nav_usage")), body);

  async function load() {
    const [use, settings, state, keys] = await Promise.all([api("GET", "/api/v1/usage?days=" + days),
      api("GET", "/api/v1/settings"), api("GET", "/api/v1/state"), api("GET", "/api/v1/keys")]);
    if (!use.ok) { clear(body).append(h("p", {class: "c-danger"}, use.data.error)); return; }
    const s = use.data.summary;
    const b = settings.ok ? settings.data.settings.budget : {};
    const now = state.ok ? state.data.time.now : Date.now() / 1000;
    const tz = state.ok ? state.data.time.timezone : undefined;
    const chosen = settings.ok ? settings.data.settings.assistant.provider : "";
    const label = (id) => ((keys.ok && keys.data.providers.find((p) => p.id === id)) || {label: id}).label;
    const iso = (epoch) => new Date(epoch * 1000).toLocaleDateString("en-CA", {timeZone: tz});
    const pct = s.month.percent;
    const over = pct != null && pct >= 100;
    clear(body);

    if (over) {
      // The first of next month, in words, is when it starts again.
      const d = new Date(now * 1000);
      const first = new Date(d.getFullYear(), d.getMonth() + 1, 1).toLocaleDateString(lang, {day: "numeric", month: "long"});
      body.append(banner(t("limit_reached", {limit: money(s.month.limit_usd, 2), provider: label(chosen), date: first}),
        "danger", "alert", "#limits"));
    }
    for (const x of s.banners) {
      body.append(banner(t("banner_" + x.kind, {provider: label(x.provider)}), "danger", "key", "#/settings/assistant"));
    }

    const monthName = new Date(now * 1000).toLocaleDateString(lang, {month: "long", timeZone: tz});
    body.append(h("section", {class: "card stack", "aria-labelledby": "h-month"},
      h("div", {class: "spread wrap"},
        h("h2", {class: "h2", id: "h-month"}, monthName.charAt(0).toUpperCase() + monthName.slice(1)),
        over ? pill(t("limit_pill"), "danger")
          : pct != null && pct >= 80 ? pill(t("percent_of_limit", {n: Math.round(pct)}), "warn") : null),
      h("p", {}, h("span", {class: "hero"}, money(s.month.spent_usd)),
        s.month.limit_usd != null ? h("span", {class: "muted", style: "font-size:18px;font-weight:600"},
          " " + t("of_limit", {limit: money(s.month.limit_usd, 2)})) : null),
      meter(pct, "", true),
      h("p", {class: "muted small"}, over ? t("paused_note") : s.free_tier ? t("free_tier_note") : t("estimate_note"))));

    // One bar per day, oldest on the left, including the days nothing was asked.
    const byDay = new Map(use.data.days.map((d) => [d.day, d]));
    const series = [];
    for (let i = days - 1; i >= 0; i--) {
      const at = now - i * DAY;
      const row = byDay.get(iso(at));
      series.push({at, cost: row ? row.cost_usd : 0, requests: row ? row.requests : 0});
    }
    const max = Math.max(...series.map((d) => d.cost));
    const places = max >= 1 ? 2 : 3;
    const readDay = h("span", {class: "muted"});
    const readVal = h("span", {class: "num"});
    const slots = series.map((d, i) => {
      const last = i === series.length - 1;
      const height = max > 0 && d.cost > 0 ? Math.max(2, (d.cost / max) * 100) : 0;
      return h("div", {class: "slot", onmouseenter: () => read(i)},
        h("div", {class: "bar" + (last ? " today" : ""), style: "height:" + height.toFixed(1) + "%"}));
    });
    function read(i) {
      const d = series[i];
      readDay.textContent = i === series.length - 1 ? t("today") : shortDay(d.at, tz);
      readVal.textContent = money(d.cost, places) + " · " + t("n_requests", {n: d.requests});
      slots.forEach((el, n) => el.classList.toggle("on", n === i && i !== series.length - 1));
    }
    read(series.length - 1);
    const chart = h("div", {class: "chart" + (days === 90 ? " dense" : ""), role: "img",
      "aria-label": t("chart_label", {days}), onmouseleave: () => read(series.length - 1)}, slots);
    body.append(h("section", {class: "card stack", "aria-labelledby": "h-chart"},
      h("div", {class: "spread wrap"}, h("h2", {class: "h2", id: "h-chart"}, t("per_day")),
        seg(t("range"), [7, 30, 90].map((n) => [n, t("days", {n})]), days, (v) => { days = Number(v); load(); })),
      h("p", {class: "small", "aria-live": "polite"}, readDay, " · ", readVal),
      h("div", {class: "chart-box"}, h("div", {class: "gridline", style: "top:0"}), h("div", {class: "gridline mid"}),
        h("span", {class: "micro muted y-max"}, money(max, places)), chart),
      h("div", {class: "spread micro muted"}, h("span", {}, shortDay(series[0].at, tz)), h("span", {}, t("today")))));

    const stat = (name, value) => h("div", {}, h("span", {class: "label"}, name), h("span", {class: "num lg"}, value));
    body.append(h("section", {class: "card stack", style: "gap:10px", "aria-labelledby": "h-today"},
      h("h2", {class: "h2", id: "h-today"}, t("today")),
      h("div", {class: "stat3"}, stat(t("spend"), money(s.day.spent_usd)),
        stat(t("questions"), String(s.turns_today)), stat(t("requests"), String(s.requests_today)))));

    // The limits. An empty box is no limit.
    const monthly = moneyInput("l-month", b.monthly_usd != null ? Number(b.monthly_usd).toFixed(2) : "", t("no_limit"));
    const daily = moneyInput("l-day", b.daily_usd != null ? Number(b.daily_usd).toFixed(2) : "", t("no_limit"));
    const turns = input({id: "l-q", inputmode: "numeric", autocomplete: "off", value: b.daily_turns ?? "", placeholder: t("no_limit")});
    const saveBtn = h("button", {class: "btn btn-primary", type: "submit"}, t("save_limits"));
    const form = h("form", {class: "card stack gap-l", id: "limits", "aria-labelledby": "h-lim"},
      h("div", {}, h("h2", {class: "h2", id: "h-lim"}, t("limits")), h("p", {class: "hint"}, t("limit_hint"))),
      h("div", {class: "grid2"},
        h("div", {class: "field"}, h("label", {for: "l-month"}, t("limit_month")), monthly),
        h("div", {class: "field"}, h("label", {for: "l-day"}, t("limit_day")), daily),
        h("div", {class: "field"}, h("label", {for: "l-q"}, t("limit_turns")), turns)),
      h("div", {}, saveBtn));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      // A comma is how half of Europe writes a decimal.
      const num = (el) => (el.value.trim() === "" ? null : Number(el.value.trim().replace(",", ".")));
      const patch = {monthly_usd: num(monthly.input), daily_usd: num(daily.input), daily_turns: num(turns)};
      if (Object.values(patch).some((v) => v != null && !Number.isFinite(v))) { toast(t("not_a_number")); return; }
      if (await busy(saveBtn, () => save({budget: patch}, true))) load();
    });
    body.append(form);
  }
  await load();
}
