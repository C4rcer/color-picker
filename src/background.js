// Event page. Three jobs:
//   1. start the picker in a tab (popup button or keyboard shortcut),
//   2. take the screenshot the picker reads pixels from,
//   3. keep the history of picked colors.
// The only access to pages is activeTab: Firefox grants it for the tab the user
// opened the popup on, or pressed the shortcut on, and takes it away on navigation.
"use strict";

const HISTORY_MAX = 30; // three rows of ten in the popup
const Color = globalThis.PipetteColor;

/** Inject the picker into a tab and switch it on. */
async function startPicking(tabId, toggle) {
  let tab = null;
  try {
    tab = tabId != null
      ? await browser.tabs.get(tabId)
      : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
  } catch {
    tab = null;
  }
  if (!tab) return { ok: false, error: "no-tab" };
  try {
    await browser.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["shared/color.js", "content/picker.js"],
    });
    const res = await browser.tabs.sendMessage(tab.id, { type: "pipette:start", toggle: !!toggle }, { frameId: 0 });
    return res && res.ok === false ? res : { ok: true };
  } catch (e) {
    // about: pages, addons.mozilla.org, the PDF viewer, reader view: Firefox
    // lets no add-on in there, with or without activeTab.
    return { ok: false, error: "blocked", detail: String((e && e.message) || e) };
  }
}

/**
 * Screenshot of the visible part of the picker's tab, as a PNG data URL.
 *
 * `dpr` is the page's devicePixelRatio. Firefox renders the capture at
 * `scale * tab zoom`, so asking for dpr / zoom gives an image at exactly `dpr`
 * image pixels per CSS pixel: the page's own device pixels, at any zoom level
 * and on any screen. The picker then maps cursor to pixel with that one number.
 */
async function capture(sender, dpr) {
  const tab = sender.tab;
  if (!tab || sender.frameId !== 0) return { ok: false, error: "bad-sender" };
  if (!tab.active) return { ok: false, error: "inactive" };
  const options = { format: "png" };
  let scale = null;
  if (Number.isFinite(dpr) && dpr > 0) {
    try {
      const zoom = await browser.tabs.getZoom(tab.id);
      if (Number.isFinite(zoom) && zoom > 0) {
        options.scale = dpr / zoom;
        scale = dpr;
      }
    } catch {
      // No zoom info: capture at Firefox's default scale and let the picker
      // work the scale out from the image size.
    }
  }
  try {
    const url = await browser.tabs.captureVisibleTab(tab.windowId, options);
    // captureVisibleTab shoots whichever tab is in front. If the user switched
    // tabs meanwhile, this picture is not the picker's page: drop it.
    const [front] = await browser.tabs.query({ active: true, windowId: tab.windowId });
    if (!front || front.id !== tab.id) return { ok: false, error: "inactive" };
    return { ok: true, url, scale };
  } catch (e) {
    return { ok: false, error: "capture-failed", detail: String((e && e.message) || e) };
  }
}

// Picks are saved one at a time so two quick clicks can not lose one.
let saving = Promise.resolve();

function savePick(hex) {
  const color = Color.normalizeHex(hex);
  if (!color) return Promise.resolve({ ok: false });
  saving = saving.then(async () => {
    const got = await browser.storage.local.get({ history: [], stats: { picks: 0 } });
    const old = Array.isArray(got.history) ? got.history : [];
    const history = [color, ...old.filter((h) => h !== color)].slice(0, HISTORY_MAX);
    const picks = ((got.stats && got.stats.picks) || 0) + 1;
    await browser.storage.local.set({ history, stats: { picks } });
  }).catch(() => {});
  return saving.then(() => ({ ok: true }));
}

/** The shortcut was pressed on a page add-ons can not touch: say so on the toolbar button. */
async function flagBlocked() {
  try {
    await browser.action.setBadgeBackgroundColor({ color: "#B3261E" });
    await browser.action.setBadgeText({ text: "!" });
    await browser.action.setTitle({ title: "Pipette can't pick colors on this page (Firefox blocks add-ons here)" });
    setTimeout(() => {
      browser.action.setBadgeText({ text: "" });
      browser.action.setTitle({ title: null });
    }, 2500);
  } catch {
    // The badge is a nicety only.
  }
}

browser.commands.onCommand.addListener(async (name) => {
  if (name !== "pick-color") return;
  const res = await startPicking(null, true);
  if (!res.ok && res.error === "blocked") flagBlocked();
});

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || sender.id !== browser.runtime.id) return undefined;
  switch (msg.type) {
    case "pipette:begin": // from the popup; an explicit tabId wins over the sender's own tab
      return startPicking(Number.isInteger(msg.tabId) ? msg.tabId : null, false);
    case "pipette:capture": // from the picker
      return capture(sender, msg.dpr);
    case "pipette:picked": // from the picker
      return savePick(msg.hex);
    default:
      return undefined;
  }
});
