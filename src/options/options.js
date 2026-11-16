import { wireSupportLinks } from "../shared/support.js";
import { renderMoreAddons } from "../shared/more-addons.js";

const Color = globalThis.PipetteColor;
const $ = (id) => document.getElementById(id);
const EXAMPLE = { r: 59, g: 130, b: 246 };
const DEFAULTS = { history: [], settings: { format: "hex", lowercase: false } };
let settings = { ...DEFAULTS.settings };

async function load() {
  const got = await browser.storage.local.get(DEFAULTS);
  settings = { ...DEFAULTS.settings, ...(got.settings || {}) };
  if (!Color.FORMATS.includes(settings.format)) settings.format = "hex";
  for (const input of document.querySelectorAll('input[name="format"]')) input.checked = input.value === settings.format;
  $("lowercase").checked = !!settings.lowercase;
  for (const eg of document.querySelectorAll("[data-eg]")) eg.textContent = Color.format(EXAMPLE, eg.dataset.eg, settings);
  $("lowerEg").textContent = Color.format(EXAMPLE, "hex", { lowercase: true });
  const n = Array.isArray(got.history) ? got.history.length : 0;
  $("count").textContent = String(n);
  $("countWord").textContent = n === 1 ? "color" : "colors";
  $("clear").disabled = n === 0;
}

function save(patch) {
  settings = { ...settings, ...patch };
  return browser.storage.local.set({ settings });
}

wireSupportLinks();
renderMoreAddons(document.getElementById("moreAddons")).catch(() => {});

$("format").addEventListener("change", (e) => {
  if (e.target.name === "format" && Color.FORMATS.includes(e.target.value)) save({ format: e.target.value });
});
$("lowercase").addEventListener("change", (e) => save({ lowercase: e.target.checked }));
$("clear").addEventListener("click", () => browser.storage.local.set({ history: [], contrast: { fg: null, bg: null } }));

browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local") load();
});

browser.commands.getAll().then((all) => {
  const cmd = all.find((c) => c.name === "pick-color");
  if (cmd && cmd.shortcut) $("shortcut").textContent = cmd.shortcut;
}, () => {});

load();
