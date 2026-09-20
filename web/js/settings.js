import { state, library, cfgGet } from './state.js';
import { TABS, FIELD_BY_KEY, isBoolField } from './settings-model.js';
import { el } from './dom.js';
import { api } from './api.js';

export function createSettings({ setPage, onConfigLoaded }) {
  function fieldDisplay(f) {
    if (state.vals[f.k] !== undefined) return state.vals[f.k];
    const c = cfgGet(f.k);
    if (c === undefined || c === null) return "";
    if (isBoolField(f)) return c ? "On" : "Off";
    return c;
  }

  function renderField(f) {
    const row = el("div", { class: "frow" });
    const left = el("div", {}, el("div", { class: "flabel" }, f.l));
    if (state.keys) left.append(el("div", { class: "fkey" }, f.k));
    row.append(left);

    const right = el("div", {});
    if (["text", "number", "list"].includes(f.t)) {
      const value = fieldDisplay(f);
      right.append(el("input", {
        class: "input m", type: f.t === "number" ? "number" : "text", value: Array.isArray(value) ? value.join(", ") : value, style: "max-width: 420px;",
        oninput: e => { state.vals[f.k] = e.target.value; },
      }));
    } else if (f.t === "choice") {
      const opts = el("div", { class: "opts" });
      f.o.forEach(o => opts.append(el("button", {
        type: "button", class: "opt", "aria-pressed": fieldDisplay(f) === o ? "true" : "false",
        onclick: () => { state.vals[f.k] = o; renderSettings(); },
      }, o)));
      right.append(opts);
    }
    if (f.h) right.append(el("p", { class: "hint" }, f.h));
    row.append(right);
    return row;
  }

  async function loadConfig() {
    try {
      state.config = await api("config");
      onConfigLoaded();
    } catch (e) { state.config = null; }
    renderSettings();
  }

  async function saveSettings() {
    const status = document.getElementById("saveStatus");
    const payload = {};
    for (const [k, v] of Object.entries(state.vals)) {
      const f = FIELD_BY_KEY[k];
      let val = (f && isBoolField(f)) ? (v === "On") : v;
      if (f?.t === "number") val = Number(v);
      if (f?.t === "list") val = String(v).split(",").map(s => s.trim()).filter(Boolean);
      const parts = k.split(".");
      let node = payload;
      parts.forEach((p, i) => { if (i === parts.length - 1) node[p] = val; else node = (node[p] = node[p] || {}); });
    }
    if (!Object.keys(payload).length) { status.textContent = "Nothing changed."; return; }
    status.textContent = "Saving…";
    try {
      const data = await api("config", {
        method: "POST",  json: payload,
      });
      state.config = data.config;
      state.vals = {};
      library.loaded = false;   // connect.* may have changed — rescan next visit
      status.textContent = "Saved ✓";
      renderSettings();
      onConfigLoaded();
    } catch (err) {
      status.textContent = "Save failed: " + err.message;
    }
  }

  function renderSettings() {
    const cur = TABS.find(t => t.id === state.tab) || TABS[0];

    const tabsBox = document.getElementById("settingsTabs");
    tabsBox.replaceChildren(...TABS.map(t => el("button", {
      type: "button", class: "tab", "aria-current": t.id === state.tab ? "page" : "false",
      onclick: () => { setPage("Settings", t.id); },
    }, t.title)));

    document.getElementById("toggleKeys").setAttribute("aria-pressed", state.keys ? "true" : "false");

    const groups = document.getElementById("settingsGroups");
    groups.replaceChildren(...cur.groups.map(g => {
      const panel = el("div", { class: "panel", style: "padding: 20px 24px 22px;" },
        el("h3", { style: "font-size: 17px; margin: 0 0 2px;" }, g.title));
      if (g.desc) panel.append(el("p", { style: "margin: 0 0 8px; font-size: 13px; color: var(--muted); max-width: 70ch;" }, g.desc));
      g.fields.forEach(f => panel.append(renderField(f)));
      return panel;
    }));
  }

  return { loadConfig, saveSettings, renderSettings };
}
