import { wireSupportLinks, maybeShowMilestone } from "../shared/support.js";

const Color = globalThis.PipetteColor;
const $ = (id) => document.getElementById(id);

// ?tab=<id> drives the popup from a normal tab (the E2E and the listing screenshots).
const params = new URLSearchParams(location.search);
const forcedTab = params.has("tab") ? Number(params.get("tab")) : null;

const DEFAULTS = {
  history: [],
  settings: { format: "hex", lowercase: false },
  contrast: { fg: null, bg: null },
  stats: { picks: 0 },
};
let state = structuredClone(DEFAULTS);
let selected = null; // the color in the big card: the latest pick, or a history swatch the user clicked
let newest = null;
let toastTimer = 0;
let clearArmed = 0;

const show = (color, format) => Color.format(color, format, state.settings);

// --------------------------------------------------------------------- state

async function load() {
  const got = await browser.storage.local.get(DEFAULTS);
  const history = (Array.isArray(got.history) ? got.history : []).map(Color.normalizeHex).filter(Boolean);
  const settings = { ...DEFAULTS.settings, ...(got.settings || {}) };
  if (!Color.FORMATS.includes(settings.format)) settings.format = "hex";
  const c = got.contrast || {};
  const contrast = { fg: Color.normalizeHex(c.fg), bg: Color.normalizeHex(c.bg) };
  state = { history, settings, contrast, stats: { picks: (got.stats && got.stats.picks) || 0 } };
  // A fresh pick takes over the big card; otherwise keep what the user was looking at.
  if (history[0] !== newest || !selected || !history.includes(selected)) selected = history[0] || null;
  newest = history[0] || null;
  render();
}

/** The two colors being compared: pinned by the user, or simply the last two picks. */
function pair() {
  const { fg, bg } = state.contrast;
  if (fg && bg) return { fg, bg, pinned: true };
  const h = state.history;
  return { fg: h[1] || null, bg: h[0] || null, pinned: false }; // text first, then what it sits on
}

// -------------------------------------------------------------------- render

function render() {
  const has = !!selected;
  $("current").hidden = !has;
  $("empty").hidden = has;
  if (has) {
    $("bigSwatch").style.background = selected;
    for (const row of document.querySelectorAll(".value")) {
      row.querySelector(".v").textContent = show(selected, row.dataset.format);
      row.classList.toggle("is-default", row.dataset.format === state.settings.format);
    }
  }
  for (const b of $("format").children) {
    const on = b.dataset.format === state.settings.format;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  renderHistory();
  renderContrast();
}

function renderHistory() {
  $("historySection").hidden = !state.history.length;
  const box = $("history");
  box.replaceChildren();
  for (const hex of state.history) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch";
    b.style.background = hex;
    b.dataset.hex = hex;
    b.title = show(hex, state.settings.format);
    b.setAttribute("aria-label", b.title);
    b.setAttribute("aria-pressed", String(hex === selected));
    box.append(b);
  }
}

function renderContrast() {
  const p = pair();
  const section = $("contrastSection");
  section.hidden = !state.history.length && !p.pinned;
  const ready = !!(p.fg && p.bg);
  for (const node of section.querySelectorAll(".slots, #sample, .result")) node.hidden = !ready;
  $("contrastReset").hidden = !p.pinned;
  $("contrastMode").textContent = ready && !p.pinned ? "· your last two picks" : "";
  $("contrastHint").textContent = ready
    ? "Click Text or Background to put the selected color there."
    : "Pick one more color to check the contrast between the two.";
  if (!ready) return;
  for (const [id, hex] of [["slotFg", p.fg], ["slotBg", p.bg]]) {
    $(id).querySelector(".chip").style.background = hex;
    $(id).querySelector(".slot-hex").textContent = show(hex, "hex");
  }
  const sample = $("sample");
  sample.style.background = p.bg;
  sample.style.color = p.fg;
  const w = Color.wcag(Color.parseHex(p.fg), Color.parseHex(p.bg));
  $("ratio").textContent = w.text;
  const badges = $("badges");
  badges.replaceChildren();
  for (const [name, pass] of [["AA", w.aaNormal], ["AA large", w.aaLarge], ["AAA", w.aaaNormal], ["AAA large", w.aaaLarge]]) {
    const s = document.createElement("span");
    s.className = "badge " + (pass ? "pass" : "fail");
    s.dataset.level = name;
    s.textContent = (pass ? "✓ " : "✕ ") + name;
    s.title = name + (pass ? ": passes" : ": fails") + " (WCAG 2 text contrast)";
    badges.append(s);
  }
}

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 1500);
}

// ------------------------------------------------------------------- actions

async function copy(text, row) {
  let ok = true;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    ok = false;
  }
  toast(ok ? "✓ Copied " + text : "Could not copy " + text);
  if (ok && row) {
    const act = row.querySelector(".act");
    row.classList.add("copied");
    act.textContent = "Copied";
    setTimeout(() => {
      row.classList.remove("copied");
      act.textContent = "Copy";
    }, 1300);
  }
}

async function startPicking() {
  const btn = $("pick");
  const err = $("pickError");
  err.hidden = true;
  btn.disabled = true;
  let res;
  try {
    let tabId = forcedTab;
    if (tabId == null) {
      const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
      tabId = tab ? tab.id : null;
    }
    res = await browser.runtime.sendMessage({ type: "pipette:begin", tabId });
  } catch {
    res = { ok: false, error: "gone" };
  }
  btn.disabled = false;
  if (res && res.ok) {
    if (forcedTab == null) window.close(); // out of the way: the page is the picker now
    return;
  }
  err.textContent = res && res.error === "unsupported"
    ? "Pipette can't pick from this kind of document. It works on regular web pages."
    : "Firefox doesn't let add-ons work on this page. Open a regular website and try again.";
  err.hidden = false;
}

function setContrast(next) {
  state.contrast = next;
  renderContrast();
  return browser.storage.local.set({ contrast: next });
}

// -------------------------------------------------------------------- wiring

wireSupportLinks();

$("btnOptions").addEventListener("click", () => {
  browser.runtime.openOptionsPage();
  if (forcedTab == null) window.close();
});
$("pick").addEventListener("click", startPicking);

for (const row of document.querySelectorAll(".value")) {
  row.addEventListener("click", () => {
    if (selected) copy(show(selected, row.dataset.format), row);
  });
}

$("format").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-format]");
  if (!b || b.dataset.format === state.settings.format) return;
  state.settings = { ...state.settings, format: b.dataset.format };
  render();
  browser.storage.local.set({ settings: state.settings });
});
$("format").addEventListener("keydown", (e) => {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  e.preventDefault();
  const i = Color.FORMATS.indexOf(state.settings.format);
  const next = $("format").children[(i + step + Color.FORMATS.length) % Color.FORMATS.length];
  next.click();
  next.focus();
});

$("history").addEventListener("click", (e) => {
  const b = e.target.closest(".swatch");
  if (!b) return;
  selected = b.dataset.hex;
  render();
  copy(show(selected, state.settings.format));
});

$("clear").addEventListener("click", () => {
  const btn = $("clear");
  if (!clearArmed) { // two clicks, so a slip of the mouse does not wipe the list
    btn.textContent = "Click again to clear";
    clearArmed = setTimeout(() => {
      clearArmed = 0;
      btn.textContent = "Clear";
    }, 3000);
    return;
  }
  clearTimeout(clearArmed);
  clearArmed = 0;
  btn.textContent = "Clear";
  browser.storage.local.set({ history: [], contrast: { fg: null, bg: null } });
});

$("slotFg").addEventListener("click", () => {
  const p = pair();
  if (selected) setContrast({ fg: selected, bg: p.bg });
});
$("slotBg").addEventListener("click", () => {
  const p = pair();
  if (selected) setContrast({ fg: p.fg, bg: selected });
});
$("swap").addEventListener("click", () => {
  const p = pair();
  setContrast({ fg: p.bg, bg: p.fg });
});
$("contrastReset").addEventListener("click", () => setContrast({ fg: null, bg: null }));

browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local") load();
});

browser.commands.getAll().then((all) => {
  const cmd = all.find((c) => c.name === "pick-color");
  if (cmd && cmd.shortcut) {
    $("shortcut").textContent = cmd.shortcut;
    $("shortcut").hidden = false;
  }
}, () => {});

load().then(() =>
  maybeShowMilestone($("supportCard"), state.stats.picks, [50, 500], (m) =>
    `You've picked ${m} colors with Pipette.`));
