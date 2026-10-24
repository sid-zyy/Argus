// Results page, Value tab: what each incident cost in AI and what it saved (GET /roi), with editable assumptions.
// Wrapped in its own scope because results.js shares the page.
(() => {
  const $ = (id) => document.getElementById(id);
  let data = null, base = null, now = null;
  const money = (v) => v >= 1e7 ? `₹${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${Math.round(v || 0).toLocaleString("en-IN")}`;
  const paise = (v) => `₹${(v || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const FIELDS = [["units_per_minute", "per minute"], ["value_per_unit", "₹ each"], ["manual_mttr_minutes", "Manual fix, min"], ["engineers_paged", "Engineers paged"]];

  // The same sums as db/business.py impact(), with this page's assumptions in place of the stored ones.
  function value(row, a) {
    const imp = row.impact, s = a.scenarios[row.scenario];
    const ai = Math.round(row.tokens / 1000 * a.ai_per_1k * 100) / 100;  // rounded per incident, like the server
    if (!row.counted || !imp || !s) return { ai, saved: 0, people: 0, hours: 0, fixed: false };
    const perMin = s.units_per_minute * imp.failure_share * s.value_per_unit;
    const manual = imp.detection_minutes + s.manual_mttr_minutes;
    const savedMin = imp.fixed ? Math.max(0, manual - imp.minutes_so_far) : 0;
    const hours = imp.fixed ? s.manual_mttr_minutes * s.engineers_paged / 60 : 0;
    return { ai, saved: perMin * savedMin, hours, people: hours * a.engineer_rate, fixed: imp.fixed, minutes: imp.minutes_so_far, manual };
  }

  function render() {
    const rows = data.rows.map((r) => ({ r, v: value(r, now) }));
    const t = rows.reduce((t, { v }) => ({ saved: t.saved + v.saved, people: t.people + v.people, hours: t.hours + v.hours, ai: t.ai + v.ai }),
      { saved: 0, people: 0, hours: 0, ai: 0 });
    const total = t.saved + t.people, multiple = t.ai >= 0.01 ? Math.round(total / t.ai) : null;
    $("val-headline").textContent = t.ai ? `${money(total)} saved for ${paise(t.ai)} of AI` : "Value";
    $("val-tiles").innerHTML = [
      ["Revenue protected", money(t.saved), "Orders and sessions that didn't fail", "good"],
      ["Engineer time saved", `${t.hours.toFixed(1)} h`, `About ${money(t.people)} of people's time`],
      ["AI cost", paise(t.ai), `${data.rows.reduce((n, r) => n + r.tokens, 0).toLocaleString("en-IN")} tokens at ${paise(now.ai_per_1k)} per 1,000`],
      ["Return on AI spend", multiple ? `${multiple.toLocaleString("en-IN")}×` : "–", `Net ${money(total - t.ai)}`, "good"],
    ].map(([k, v, s, kind]) => `<div class="tile ${kind || ""}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");
    $("val-table").innerHTML = `<thead><tr><th>Incident</th><th>Status</th><th class="n">Fixed in</th><th class="n">Manual</th><th class="n">Revenue protected</th>
        <th class="n">People's time</th><th class="n">AI cost</th></tr></thead><tbody>${rows.map(({ r, v }) => `<tr class="${v.fixed ? "" : "muted"}">
        <td><a href="/incidents/${r.id}/view">${incKey(r.id)}</a> ${esc(r.title)}${r.mode === "replay" ? ` <span class="faint small">(replay)</span>` : ""}</td>
        <td>${esc(STATUS_LABEL[r.status] || r.status)}</td>
        <td class="n">${v.fixed ? `${Math.round(v.minutes)} min` : "–"}</td><td class="n">${v.fixed ? `${Math.round(v.manual)} min` : "–"}</td>
        <td class="n">${v.fixed ? money(v.saved) : "–"}</td><td class="n">${v.fixed ? money(v.people) : "–"}</td><td class="n">${paise(v.ai)}</td></tr>`).join("")
      || `<tr><td colspan="7" class="faint">No incidents yet. Start a demo incident from the dashboard.</td></tr>`}</tbody>
      <tfoot><tr><td colspan="4">Total</td><td class="n">${money(t.saved)}</td><td class="n">${money(t.people)}</td><td class="n">${paise(t.ai)}</td></tr></tfoot>`;
  }

  function renderFields() {
    const num = (id, label, v, step = "any") => `<div><label for="${id}">${esc(label)}</label><input id="${id}" type="number" min="0" step="${step}" value="${v}"></div>`;
    $("val-fields").innerHTML = `<h3>Everywhere</h3><div class="grid2">${num("f-ai", "AI ₹ per 1,000 tokens", now.ai_per_1k)}${num("f-rate", "Engineer ₹ per hour", now.engineer_rate)}</div>`
      + Object.entries(now.scenarios).map(([name, s]) => `<h3>${esc(s.service_label)} <span class="faint small">${esc(name)}</span></h3><div class="grid2">
        ${FIELDS.map(([k, label]) => num(`f-${name}-${k}`, k === "units_per_minute" ? `${s.unit[0].toUpperCase()}${s.unit.slice(1)} per minute` : label, s[k])).join("")}</div>`).join("");
    $("val-fields").querySelectorAll("input").forEach((el) => el.oninput = () => {
      const v = Math.max(0, Number(el.value) || 0);
      if (el.id === "f-ai") now.ai_per_1k = v;
      else if (el.id === "f-rate") now.engineer_rate = v;
      else { const [, name, key] = el.id.match(/^f-(.+)-(units_per_minute|value_per_unit|manual_mttr_minutes|engineers_paged)$/); now.scenarios[name][key] = v; }
      render();
    });
  }

  $("val-reset").onclick = () => { now = structuredClone(base); renderFields(); render(); };
  api("/roi").then((d) => {
    data = d;
    const rate = Object.values(d.scenarios).find((s) => s && s.engineer_cost_per_hour)?.engineer_cost_per_hour || 0;
    base = { ai_per_1k: d.ai_price.per_1k_tokens, engineer_rate: rate,
      scenarios: Object.fromEntries(Object.entries(d.scenarios).filter(([, s]) => s).map(([n, s]) => [n, { ...s }])) };
    now = structuredClone(base);
    renderFields(); render();
  }).catch((e) => { $("val-headline").textContent = "Could not load the figures"; $("val-sub").textContent = e.message; });
})();
