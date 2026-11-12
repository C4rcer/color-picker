"""End-to-end test of Pipette in a real Firefox (Selenium + geckodriver).

  python -m pip install selenium pillow      (once, ideally in a venv)
  python tools/release.py build
  python tests/e2e_firefox.py [main] [dpr2] [dpr125] [dpr175] [rfp] [fpp]

Loads the built zip as a temporary add-on in throwaway profiles, serves
tests/test-page.html on 127.0.0.1:8941 and picks pixels whose color is known.
Every browser runs headless and muted.

Pipette only has activeTab, so nothing works until Firefox sees a real user
action. The test makes those through the browser's own UI (geckodriver
--allow-system-access): it clicks the toolbar button and presses the keyboard
shortcut. Nothing is granted by hand.

Sessions (all run when none is named):
  main    100% screen: popup, shortcut, scrolling, page zoom, formats, history,
          contrast, strict CSP, copy-blocking page, http page, blocked page
  dpr2    high-DPI screen (2 device pixels per CSS pixel)
  dpr125  125% Windows scaling
  dpr175  175% Windows scaling (device pixels do not divide CSS pixels evenly)
  rfp     privacy.resistFingerprinting on (Firefox lies about the screen)
  fpp     canvas randomization on (what private windows and ETP Strict use)
"""
import http.server
import io
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = "pipette-color-picker@c4rcer"
WIDGET_ID = "pipette-color-picker_c4rcer-browser-action"
UUID = "0b7d3c52-51c4-4c8e-9a61-c010a0c0ffee"
PORT = 8941
HOST = f"127.0.0.1:{PORT}"
BASE = f"http://{HOST}/test-page.html"
PLAIN_HOST = "pipette-plain.test"  # mapped to 127.0.0.1; not a secure context, unlike 127.0.0.1 itself
EXT = f"moz-extension://{UUID}"
CSP = "default-src 'none'; style-src 'nonce-pipette-test'; script-src 'nonce-pipette-test'"

BLOCKS = {"b1": "#3B82F6", "b2": "#10B981", "b3": "#F59E0B", "b4": "#123456", "b5": "#000000"}
STRIPES = ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"]
WHITE, TEAL = "#FFFFFF", "#0E7C86"

CANVAS_NOISE_JS = """
  // Two colors: Firefox leaves single-color canvases alone.
  const c = document.createElement('canvas'); c.width = c.height = 64;
  const g = c.getContext('2d'); g.fillStyle = '#3B82F6'; g.fillRect(0, 0, 64, 64);
  g.fillStyle = '#F59E0B'; g.fillRect(32, 0, 32, 64);
  const d = g.getImageData(0, 0, 64, 64).data; let off = 0;
  for (let i = 0; i < d.length; i += 4) {
    const left = (i / 4) % 64 < 32;
    const want = left ? [0x3B, 0x82, 0xF6] : [0xF5, 0x9E, 0x0B];
    if (d[i] !== want[0] || d[i + 1] !== want[1] || d[i + 2] !== want[2]) off++;
  }
  return off;"""

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    line = ("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else "")
    print(line.encode("ascii", "replace").decode(), flush=True)  # Windows consoles choke on a check mark


def wait(fn, timeout=8.0, step=0.1):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(step)
    return last


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "tests"), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        if "csp=1" in self.path:
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def contrast_ratio(a, b):
    """WCAG 2 contrast, written out again here so the add-on's own maths is not marking itself."""
    def lum(hex_):
        out = []
        for i in (1, 3, 5):
            s = int(hex_[i:i + 2], 16) / 255
            out.append(s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    hi, lo = max(lum(a), lum(b)), min(lum(a), lum(b))
    return (hi + 0.05) / (lo + 0.05)


class Fx:
    """One headless Firefox with the built add-on, a page window and a window on an extension page."""

    def __init__(self, unpacked, prefs=None):
        opts = Options()
        opts.binary_location = r"C:\Program Files\Mozilla Firefox\firefox.exe"
        opts.add_argument("-headless")
        opts.set_preference("media.volume_scale", "0.0")
        opts.set_preference("extensions.webextensions.uuids", json.dumps({ADDON_ID: UUID}))
        opts.set_preference("network.dns.localDomains", PLAIN_HOST)
        for k, v in (prefs or {}).items():
            opts.set_preference(k, v)
        self.d = webdriver.Firefox(options=opts, service=Service(service_args=["--allow-system-access"]))
        self.d.set_window_size(1280, 900)
        self.d.execute("INSTALL_ADDON", {"path": str(unpacked), "temporary": True})
        time.sleep(1.0)
        self.page = self.d.current_window_handle
        # The extension page lives in a second window, so looking at storage never hides the test page.
        self.d.switch_to.new_window("window")
        self.ext = self.d.current_window_handle
        self.d.get(f"{EXT}/options/options.html")
        self.to_page()

    def quit(self):
        self.d.quit()

    # ---- windows
    def to_page(self):
        if self.d.current_window_handle != self.page:
            self.d.switch_to.window(self.page)

    def to_ext(self):
        if self.d.current_window_handle != self.ext:
            self.d.switch_to.window(self.ext)

    def js(self, script, *args):
        self.to_page()
        return self.d.execute_script(script, *args)

    def api(self, code):
        """Run async code with the WebExtension API, on the extension page."""
        self.to_ext()
        return self.d.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            f"(async () => {{ {code} }})().then(done, (e) => done('ERR ' + e));")

    def chrome(self, script, *args):
        """Run privileged code in the browser window that holds the test page."""
        self.to_page()
        with self.d.context("chrome"):
            return self.d.execute_script(script, *args)

    # ---- browser UI, the way a user reaches it
    def shortcut(self):
        self.to_page()
        with self.d.context("chrome"):
            ActionChains(self.d).key_down(Keys.ALT).key_down(Keys.SHIFT).send_keys("c") \
                .key_up(Keys.SHIFT).key_up(Keys.ALT).perform()

    def click_toolbar_button(self):
        return self.chrome("""
          const item = document.getElementById(arguments[0]);
          if (!item) return "no toolbar item";
          const where = item.closest("#nav-bar") ? "nav-bar" : (item.closest("panel, toolbar") || {}).id;
          (item.querySelector(".unified-extensions-item-action-button") || item).click();
          return where;
        """, WIDGET_ID)

    def popup_panel(self):
        return self.chrome("""
          const b = document.querySelector("browser.webextension-popup-browser");
          return b && b.currentURI ? b.currentURI.spec : null;
        """)

    def close_popup_panel(self):
        self.chrome("""
          const b = document.querySelector("browser.webextension-popup-browser");
          const p = b && b.closest("panel");
          if (p) p.hidePopup();
        """)

    def has_active_tab(self):
        return self.chrome(
            "return WebExtensionPolicy.getByID(arguments[0]).extension.tabManager.hasActiveTabPermission(gBrowser.selectedTab);",
            ADDON_ID)

    def clipboard(self):
        return self.chrome("""
          try {
            const t = Cc["@mozilla.org/widget/transferable;1"].createInstance(Ci.nsITransferable);
            t.init(null);
            t.addDataFlavor("text/plain");
            Services.clipboard.getData(t, Ci.nsIClipboard.kGlobalClipboard, window.browsingContext.currentWindowContext);
            const out = {};
            t.getTransferData("text/plain", out);
            return out.value.QueryInterface(Ci.nsISupportsString).data;
          } catch (e) { return null; }
        """)

    def in_content(self, code):
        """Run privileged code inside the test page's process (used to pinch-zoom it)."""
        self.chrome("gBrowser.selectedBrowser.messageManager.loadFrameScript('data:,' + encodeURIComponent(arguments[0]), false);", code)

    def clear_clipboard(self):
        self.chrome("Services.clipboard.emptyClipboard(Ci.nsIClipboard.kGlobalClipboard);")

    # ---- picker
    def page_tab_id(self):
        return self.api("""
          const me = await browser.tabs.getCurrent();
          const t = (await browser.tabs.query({ active: true })).find((x) => x.windowId !== me.windowId);
          return t ? t.id : null;""")

    def overlay(self):
        return self.js("return !!document.querySelector('[data-pipette-overlay]')")

    def start(self):
        """Shortcut, then wait for the overlay."""
        self.shortcut()
        return wait(self.overlay, 5) is True

    def stop(self):
        if self.overlay():
            self.to_page()
            ActionChains(self.d).send_keys(Keys.ESCAPE).perform()
            wait(lambda: not self.overlay(), 3)

    def store(self):
        return self.api("return await browser.storage.local.get();")

    def picks(self):
        return (self.store().get("stats") or {}).get("picks", 0)

    def move(self, x, y):
        self.to_page()
        ac = ActionChains(self.d)
        ac.w3c_actions.pointer_action.move_to_location(int(x), int(y))
        ac.perform()

    def pick(self, x, y, keep=True, settle=0.0):
        """Point at a viewport position and click (Shift+click keeps the picker open). Returns the saved color."""
        before = self.picks()
        self.to_page()
        ac = ActionChains(self.d)
        if keep:
            ac.key_down(Keys.SHIFT)
        ac.w3c_actions.pointer_action.move_to_location(int(x), int(y))
        ac.w3c_actions.key_action.pause()
        if settle:
            ac.pause(settle)
        ac.click()
        if keep:
            ac.key_up(Keys.SHIFT)
        ac.perform()
        return self.wait_pick(before)

    def wait_pick(self, before):
        got = wait(lambda: (lambda s: s if (s.get("stats") or {}).get("picks", 0) > before else None)(self.store()), 6)
        return got["history"][0] if isinstance(got, dict) else None

    def keys(self, *keys):
        self.to_page()
        ActionChains(self.d).send_keys(*keys).perform()

    def shift_keys(self, *keys):
        self.to_page()
        ActionChains(self.d).key_down(Keys.SHIFT).send_keys(*keys).key_up(Keys.SHIFT).perform()

    def rect(self, el_id):
        return self.js(
            "const r = document.getElementById(arguments[0]).getBoundingClientRect();"
            "return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, cx: r.left + r.width / 2, cy: r.top + r.height / 2};",
            el_id)

    def viewport(self):
        return self.js("return {w: document.documentElement.clientWidth, h: document.documentElement.clientHeight, dpr: devicePixelRatio}")

    def scroll(self, x, y):
        self.js("window.scrollTo(arguments[0], arguments[1])", x, y)

    def screen_pixel(self, x, y):
        """Color of the pixel the user sees at a viewport position (from a real screenshot, loupe included)."""
        self.to_page()
        img = Image.open(io.BytesIO(self.d.get_screenshot_as_png())).convert("RGB")
        k = img.width / self.js("return window.innerWidth")
        r, g, b = img.getpixel((int(x * k), int(y * k)))
        return f"#{r:02X}{g:02X}{b:02X}"


# ------------------------------------------------------------------ test parts

def accuracy(fx, tag, stripes):
    """Blocks, a fixed box near the far corner (catches any drift in scale), and 1px stripes."""
    vp = fx.viewport()
    wrong = []
    for el_id, color in BLOCKS.items():
        r = fx.rect(el_id)
        if r["right"] > vp["w"] or r["bottom"] > vp["h"]:
            continue
        for x, y in ((r["cx"], r["cy"]), (int(r["left"]) + 2, int(r["top"]) + 2), (int(r["right"]) - 3, int(r["bottom"]) - 3)):
            got = fx.pick(x, y)
            if got != color:
                wrong.append(f"{el_id}@{int(x)},{int(y)}={got}")
    check(f"{tag}: block colors exact (center and corners)", not wrong, ", ".join(wrong))

    fx.scroll(0, 700)  # nothing but white page under the fixed box here, at every zoom
    r = fx.rect("fixed")
    wrong = []
    cases = [
        (math.ceil(r["left"]) + 1, r["cy"], TEAL), (math.floor(r["left"]) - 2, r["cy"], WHITE),
        (math.floor(r["right"]) - 2, r["cy"], TEAL), (math.ceil(r["right"]) + 1, r["cy"], WHITE),
        (r["cx"], math.ceil(r["top"]) + 1, TEAL), (r["cx"], math.floor(r["top"]) - 2, WHITE),
        (r["cx"], math.floor(r["bottom"]) - 2, TEAL), (r["cx"], math.ceil(r["bottom"]) + 1, WHITE),
    ]
    for x, y, color in cases:
        got = fx.pick(x, y)
        if got != color:
            wrong.append(f"{int(x)},{int(y)}={got} want {color}")
    check(f"{tag}: edges of a box at the far corner land within 2px", not wrong, ", ".join(wrong))
    fx.scroll(0, 0)

    if stripes:
        hs, vs = fx.rect("hs"), fx.rect("vs")
        wrong = []
        for i in (0, 1, 2, 3, 4, 17, 30, 62, 63):
            got = fx.pick(hs["left"] + i, hs["top"] + 20)
            if got != STRIPES[i % 4]:
                wrong.append(f"col{i}={got}")
            got = fx.pick(vs["left"] + 20, vs["top"] + i)
            if got != STRIPES[i % 4]:
                wrong.append(f"row{i}={got}")
        check(f"{tag}: 1px stripes, every column and row exact", not wrong, ", ".join(wrong))
        check(f"{tag}: pixel just outside the stripes is the page background",
              fx.pick(hs["left"] - 1, hs["top"] + 20) == WHITE and fx.pick(hs["left"] + 64, hs["top"] + 20) == WHITE)


def scrolling(fx, tag):
    """The scroll happens while the picker is open; the click follows at once, with no wait."""
    fx.clear_clipboard()
    fx.scroll(0, 1700)
    check(f"{tag}: click right after scrolling down gets the new pixel", fx.pick(100, 150) == "#D946EF")
    check(f"{tag}: and that pick, finished after the new screenshot, is still copied",
          wait(lambda: fx.clipboard() == "#D946EF", 3), fx.clipboard())
    for el_id, color, name in (("deeper", "#0EA5E9", "far down the page"), ("far", "#84CC16", "scrolled sideways"),
                               ("corner", "#7C2D12", "scrolled both ways")):
        at = fx.js("document.getElementById(arguments[0]).scrollIntoView({block: 'center', inline: 'center'});"
                   "return [Math.round(scrollX), Math.round(scrollY)]", el_id)
        r = fx.rect(el_id)
        got = fx.pick(r["cx"], r["cy"])
        check(f"{tag}: {name}", got == color and (at[0] > 0 or at[1] > 0), f"{got} at scroll {at}")
    r = fx.rect("fixed")
    check(f"{tag}: fixed element still where it is drawn", fx.pick(r["cx"], r["cy"]) == TEAL)
    fx.scroll(0, 0)
    check(f"{tag}: back at the top", fx.pick(100, 100) == "#3B82F6")


def session_main(unpacked):
    fx = Fx(unpacked)
    d = fx.d
    try:
        fx.to_page()
        d.get(BASE)

        # ---- the toolbar button, clicked for real
        check("no access to the page before the user acts", fx.has_active_tab() is False)
        where = fx.click_toolbar_button()
        check("toolbar button sits in the navigation bar", where == "nav-bar", where)
        url = wait(fx.popup_panel, 5)
        check("clicking it opens the popup", isinstance(url, str) and url.endswith("/popup/popup.html"), url)
        check("and gives activeTab for this tab", fx.has_active_tab() is True)
        fx.close_popup_panel()
        perms = fx.api("return (await browser.permissions.getAll());")
        check("no host permissions at all", perms.get("origins") == [], perms)
        check("permissions are activeTab, scripting, storage",
              sorted(perms.get("permissions", [])) == ["activeTab", "scripting", "storage"], perms.get("permissions"))

        # ---- popup (as a tab in the second window, pointed at the page tab)
        tab_id = fx.page_tab_id()
        check("extension API reachable from the harness", isinstance(tab_id, int), tab_id)
        fx.to_ext()
        d.get(f"{EXT}/popup/popup.html?tab={tab_id}")
        wait(lambda: d.find_element(By.ID, "empty").is_displayed())
        check("popup starts empty with a how-to", d.find_element(By.ID, "empty").is_displayed()
              and not d.find_element(By.ID, "current").is_displayed()
              and not d.find_element(By.ID, "historySection").is_displayed())
        check("popup shows the shortcut", wait(lambda: d.find_element(By.ID, "shortcut").text == "Alt+Shift+C"),
              d.find_element(By.ID, "shortcut").text)
        check("popup has the Ko-fi pill",
              d.find_element(By.CSS_SELECTOR, "header a[data-support]").get_attribute("href") == "https://ko-fi.com/carcer7378")
        check("no thank-you card for a new user", not d.find_element(By.ID, "supportCard").is_displayed())
        fx.clear_clipboard()
        fx.to_ext()
        d.find_element(By.ID, "pick").click()
        check("Pick button starts the picker on the page", wait(fx.overlay, 5) is True)
        r = fx.rect("b1")
        got = fx.pick(r["cx"], r["cy"], keep=False)
        check("click picks the pixel under the cursor", got == "#3B82F6", got)
        check("and copies it as HEX", wait(lambda: fx.clipboard() == "#3B82F6", 3), fx.clipboard())
        check("picker closes itself after the pick", wait(lambda: not fx.overlay(), 4) is True)
        ev = fx.js("return window.pageEvents")
        check("the page saw no click, mousedown or mousemove", ev["click"] == 0 and ev["mousedown"] == 0 and ev["mousemove"] == 0, ev)
        fx.to_ext()
        check("popup shows the new color without reopening",
              wait(lambda: d.find_element(By.CSS_SELECTOR, '.value[data-format="hex"] .v').text == "#3B82F6"))
        check("popup lists RGB and HSL too",
              d.find_element(By.CSS_SELECTOR, '.value[data-format="rgb"] .v').text == "rgb(59, 130, 246)"
              and d.find_element(By.CSS_SELECTOR, '.value[data-format="hsl"] .v').text == "hsl(217, 91%, 60%)",
              d.find_element(By.CSS_SELECTOR, '.value[data-format="hsl"] .v').text)

        # ---- keyboard shortcut, pressed for real
        check("shortcut starts the picker", fx.start())
        accuracy(fx, "100%", stripes=True)

        # ---- the loupe is what the user sees
        r = fx.rect("b2")
        fx.move(r["cx"], r["cy"])
        time.sleep(0.4)
        check("loupe is drawn around the cursor (white ring over the next block)",
              fx.screen_pixel(r["cx"] + 67, r["cy"]) == WHITE, fx.screen_pixel(r["cx"] + 67, r["cy"]))
        check("its middle cell shows the color that will be picked",
              fx.screen_pixel(r["cx"], r["cy"]) == "#10B981", fx.screen_pixel(r["cx"], r["cy"]))
        b1 = fx.rect("b1")
        fx.move(b1["right"] - 1, b1["cy"])  # last blue column, green starts one pixel to the right
        time.sleep(0.3)
        cell = 12
        check("loupe magnifies neighbours: one cell right is the next block's color",
              fx.screen_pixel(b1["right"] - 1 + cell, b1["cy"] + 3) == "#10B981"
              and fx.screen_pixel(b1["right"] - 1 - cell, b1["cy"] + 3) == "#3B82F6",
              fx.screen_pixel(b1["right"] - 1 + cell, b1["cy"] + 3))

        # ---- the loupe must never be in its own screenshot
        px, py = b1["right"] - 1, b1["cy"]
        check("pick on the block edge", fx.pick(px, py) == "#3B82F6")
        fx.js("document.getElementById('scroller').scrollTop = 1")  # forces a new screenshot with the loupe up
        time.sleep(0.6)
        got = fx.pick(px + 3, py)
        check("new screenshot does not contain the loupe", got == "#10B981", got)
        fx.move(px, py)
        time.sleep(0.2)
        fx.js("document.getElementById('scroller').scrollTop = 0")
        time.sleep(0.6)
        got = fx.pick(px, py + 90)
        check("nor its label", got == WHITE, got)

        # ---- hover styles of the page stay out of it
        h = fx.rect("hoverbox")
        fx.move(h["cx"], h["cy"])
        fx.js("document.getElementById('scroller').scrollTop = 2")
        time.sleep(0.6)
        got = fx.pick(h["cx"], h["cy"])
        check("page is not hovered while picking", got == "#4477AA", got)

        # ---- above anything the page stacks on top, and usable over a modal dialog
        fx.js("""
          const top = document.createElement('div'); top.id = 'ztop';
          top.style.cssText = 'position:fixed;left:600px;top:300px;width:200px;height:200px;background:#B45309;z-index:2147483647';
          document.body.append(top);""")
        time.sleep(0.6)  # the new box moved nothing, so make sure the next picture has it
        fx.js("document.getElementById('scroller').scrollTop = 3")
        time.sleep(0.6)
        z = fx.rect("ztop")
        check("pick on an element with the highest z-index", fx.pick(z["cx"], z["cy"]) == "#B45309")
        check("loupe is drawn above it", fx.screen_pixel(z["cx"] + 67, z["cy"]) == WHITE, fx.screen_pixel(z["cx"] + 67, z["cy"]))
        fx.js("""
          document.getElementById('ztop').remove();
          const dlg = document.createElement('dialog'); dlg.id = 'dlg';
          dlg.style.cssText = 'position:fixed;margin:0;left:900px;top:300px;width:240px;height:200px;border:0;padding:0;background:#065F46';
          const btn = document.createElement('button'); btn.id = 'dlgbtn'; btn.textContent = 'OK';
          btn.style.cssText = 'position:absolute;left:60px;top:60px;width:120px;height:80px;border:0;background:#FDE68A';
          btn.addEventListener('click', () => { window.dialogClicks = (window.dialogClicks || 0) + 1; });
          dlg.append(btn); document.body.append(dlg); dlg.showModal();""")
        fx.js("document.getElementById('scroller').scrollTop = 4")
        time.sleep(0.6)
        b = fx.rect("dlgbtn")
        got = fx.pick(b["left"] + 110, b["top"] + 70)
        check("pick inside a modal dialog (the page makes everything else inert)", got == "#FDE68A", got)
        check("loupe is drawn above the dialog", fx.screen_pixel(b["left"] + 110 + 67, b["top"] + 70) == WHITE,
              fx.screen_pixel(b["left"] + 110 + 67, b["top"] + 70))
        check("the dialog's own button was not pressed", fx.js("return window.dialogClicks || 0") == 0)
        fx.js("document.getElementById('dlg').close(); document.getElementById('dlg').remove(); document.getElementById('scroller').scrollTop = 0")
        time.sleep(0.6)

        # ---- embedded frame from another origin
        f = fx.rect("frame")
        check("frame is cross-origin", fx.js("try { document.getElementById('frame').contentDocument.body; return false } catch (e) { return true }")
              or fx.js("return document.getElementById('frame').contentDocument === null"))
        check("pixel inside a cross-origin frame", fx.pick(f["left"] + 50, f["top"] + 50) == "#FFD700")
        check("frame background", fx.pick(f["left"] + 200, f["top"] + 50) == "#663399")

        # ---- scrolling with the picker open
        scrolling(fx, "100%")
        fx.to_page()
        for _ in range(4):  # a real mouse wheel, turned over the overlay
            ActionChains(d).scroll_from_origin(ScrollOrigin.from_viewport(300, 300), 0, 400).perform()
        wait(lambda: fx.js("return scrollY") > 1000, 4)
        last = [-1]

        def settled():
            y = fx.js("return scrollY")
            done = y == last[0]
            last[0] = y
            return done
        wait(settled, 4, 0.25)
        r = fx.rect("deep")
        got = fx.pick(r["cx"], r["cy"])
        check("mouse wheel scrolls the page while picking, and the pick follows", got == "#D946EF", f"{got} scrollY={last[0]}")
        fx.scroll(0, 0)
        fx.js("document.getElementById('scroller').scrollTop = 520")
        s = fx.rect("scroller")
        check("scrolling a box inside the page", fx.pick(s["left"] + 50, s["top"] + 80, settle=0.5) == "#228833")
        fx.js("document.getElementById('scroller').scrollTop = 0")

        # ---- keyboard: arrows move one pixel, Enter picks, Esc cancels
        hs = fx.rect("hs")
        fx.move(hs["left"], hs["top"] + 20)
        time.sleep(0.5)
        before = fx.picks()
        fx.keys(Keys.ARROW_RIGHT)
        fx.shift_keys(Keys.ENTER)
        check("arrow right, Shift+Enter picks the next pixel", fx.wait_pick(before) == "#00FF00")
        before = fx.picks()
        fx.keys(Keys.ARROW_RIGHT, Keys.ARROW_RIGHT, Keys.ARROW_LEFT)
        fx.shift_keys(Keys.ENTER)
        check("arrows right, right, left", fx.wait_pick(before) == "#0000FF")
        before = fx.picks()
        fx.shift_keys(Keys.ARROW_RIGHT, Keys.ARROW_RIGHT, Keys.ARROW_UP, Keys.ENTER)  # column 2 + 20 = 22
        got = fx.wait_pick(before)
        check("Shift+arrow jumps ten pixels", got == STRIPES[22 % 4], got)
        before = fx.picks()
        fx.keys(*([Keys.ARROW_DOWN] * 35), Keys.ENTER)  # 10 + 35 rows down: below the 40px tall stripes
        check("arrow down out of the stripes, Enter picks and closes",
              fx.wait_pick(before) == WHITE and wait(lambda: not fx.overlay(), 4) is True)
        check("page did not scroll from the arrow keys", fx.js("return [scrollX, scrollY]") == [0, 0])

        before = fx.picks()
        check("shortcut again", fx.start())
        fx.keys(Keys.ESCAPE)
        check("Esc cancels", wait(lambda: not fx.overlay(), 3) is True)
        check("shortcut once more", fx.start())
        fx.shortcut()
        check("pressing the shortcut again switches it off", wait(lambda: not fx.overlay(), 3) is True)
        check("start for right-click", fx.start())
        fx.to_page()
        ac = ActionChains(d)
        ac.w3c_actions.pointer_action.move_to_location(300, 300)
        ac.context_click().perform()
        check("right-click cancels", wait(lambda: not fx.overlay(), 3) is True)
        check("nothing was saved by the cancelled runs", fx.picks() == before)

        # ---- page scrolled before the picker starts
        fx.scroll(0, 1700)
        check("start on a scrolled page", fx.start())
        check("scrolled before start", fx.pick(100, 150) == "#D946EF")
        fx.scroll(0, 0)
        time.sleep(0.3)

        # ---- leaving the tab and coming back
        fx.to_page()
        d.switch_to.new_window("tab")
        other = d.current_window_handle
        d.get(f"http://{HOST}/frame.html")
        time.sleep(0.4)
        d.close()
        d.switch_to.window(fx.page)
        check("picker survives a trip to another tab", fx.overlay() and fx.pick(100, 100, settle=0.6) == "#3B82F6", other)

        # ---- the real popup panel, end to end: toolbar button, then the Pick button inside the panel
        fx.stop()
        before = fx.picks()
        fx.click_toolbar_button()
        wait(fx.popup_panel, 5)
        time.sleep(0.6)
        fx.chrome("""
          const b = document.querySelector("browser.webextension-popup-browser");
          b.messageManager.loadFrameScript("data:," + encodeURIComponent("content.document.getElementById('pick').click();"), false);
        """)
        check("real popup: its Pick button starts the picker on the tab behind it", wait(fx.overlay, 5) is True)
        check("real popup: it closes itself to get out of the way", wait(lambda: fx.popup_panel() is None, 4) is True)
        r = fx.rect("b2")
        check("real popup: the pick lands", fx.pick(r["cx"], r["cy"], keep=False) == "#10B981" and fx.picks() == before + 1)
        wait(lambda: not fx.overlay(), 4)

        # ---- pinch zoom (touchpad or touch screen): the page is magnified without being laid out again
        fx.in_content("content.windowUtils.setResolutionAndScaleTo(2);")
        check("page is pinch-zoomed 2x", wait(lambda: fx.js("return visualViewport.scale") == 2, 4) is True)
        fx.start()
        wrong = [f"{k}={got}" for k, got in ((k, fx.pick(fx.rect(k)["cx"], fx.rect(k)["cy"])) for k in ("b1", "b2", "b4")) if got != BLOCKS[k]]
        hs = fx.rect("hs")
        wrong += [f"col{i}" for i in (0, 1, 2, 3, 30) if fx.pick(hs["left"] + i, hs["top"] + 20) != STRIPES[i % 4]]
        check("pinch zoom: blocks and stripes exact", not wrong, ", ".join(wrong))
        fx.in_content("content.windowUtils.scrollToVisual(300, 60, 0, 0);")
        check("pinch zoom: panned view", wait(lambda: fx.js("return visualViewport.offsetLeft") == 300, 4) is True)
        wrong = [f"{k}={got}" for k, got in ((k, fx.pick(fx.rect(k)["cx"], fx.rect(k)["cy"], settle=0.3)) for k in ("b3", "b4", "b5")) if got != BLOCKS[k]]
        check("pinch zoom: exact after panning", not wrong, ", ".join(wrong))
        fx.in_content("content.windowUtils.setResolutionAndScaleTo(1);")
        # Un-zooming turns the pan into ordinary scrolling, a moment later.
        wait(lambda: fx.js("return visualViewport.scale === 1 && visualViewport.offsetLeft === 0"), 4)
        fx.scroll(0, 0)
        wait(lambda: fx.js("return scrollX === 0 && scrollY === 0"), 3)

        # ---- page zoom (Ctrl +/-), including steps where device pixels do not divide evenly
        tab_id = fx.page_tab_id()
        for zoom, stripes in ((1.5, False), (2, True), (1.1, False), (0.67, False), (0.5, False), (3, False), (1.33, False)):
            fx.api(f"await browser.tabs.setZoom({tab_id}, {zoom}); return true;")
            wait(lambda: abs(fx.js("return devicePixelRatio") - zoom) < 0.05, 3)
            time.sleep(0.3)
            if not fx.overlay():
                fx.start()
            accuracy(fx, f"zoom {int(zoom * 100)}%", stripes)
        scrolling(fx, "zoom 133%")
        fx.api(f"await browser.tabs.setZoom({tab_id}, 1); return true;")
        wait(lambda: abs(fx.js("return devicePixelRatio") - 1) < 0.01, 3)
        fx.stop()
        ev = fx.js("return window.pageEvents")
        check("after all that, the page still saw no mouse events", ev["click"] == 0 and ev["mousedown"] == 0 and ev["mousemove"] == 0, ev)

        # ---- copy formats (options page)
        fx.to_ext()
        d.get(f"{EXT}/options/options.html")
        wait(lambda: d.find_element(By.ID, "shortcut").text == "Alt+Shift+C")
        check("options page shows the shortcut", d.find_element(By.ID, "shortcut").text == "Alt+Shift+C")
        check("options page has the Ko-fi link",
              d.find_element(By.CSS_SELECTOR, "a[data-support]").get_attribute("href") == "https://ko-fi.com/carcer7378")
        d.find_element(By.CSS_SELECTOR, 'input[name="format"][value="rgb"]').click()
        wait(lambda: (fx.store().get("settings") or {}).get("format") == "rgb")
        fx.start()
        r = fx.rect("b3")
        fx.pick(r["cx"], r["cy"], keep=False)
        check("RGB as the default format", wait(lambda: fx.clipboard() == "rgb(245, 158, 11)", 3), fx.clipboard())
        wait(lambda: not fx.overlay(), 4)
        fx.to_ext()
        d.find_element(By.CSS_SELECTOR, 'input[name="format"][value="hsl"]').click()
        wait(lambda: (fx.store().get("settings") or {}).get("format") == "hsl")
        fx.start()
        r = fx.rect("b1")
        fx.pick(r["cx"], r["cy"], keep=False)
        check("HSL as the default format", wait(lambda: fx.clipboard() == "hsl(217, 91%, 60%)", 3), fx.clipboard())
        wait(lambda: not fx.overlay(), 4)
        fx.to_ext()
        d.find_element(By.CSS_SELECTOR, 'input[name="format"][value="hex"]').click()
        d.find_element(By.ID, "lowercase").click()
        wait(lambda: (fx.store().get("settings") or {}) == {"format": "hex", "lowercase": True})
        fx.start()
        r = fx.rect("b3")
        fx.pick(r["cx"], r["cy"], keep=False)
        check("lower-case HEX option", wait(lambda: fx.clipboard() == "#f59e0b", 3), fx.clipboard())
        wait(lambda: not fx.overlay(), 4)
        fx.to_ext()
        d.find_element(By.ID, "lowercase").click()
        wait(lambda: (fx.store().get("settings") or {}).get("lowercase") is False)

        # ---- history and contrast in the popup
        fx.start()
        for el_id in ("b4", "b2"):
            r = fx.rect(el_id)
            fx.pick(r["cx"], r["cy"])
        r = fx.rect("b5")
        fx.pick(r["cx"], r["cy"])            # black: the text
        fx.pick(r["right"] + 30, r["cy"])    # white: its background
        fx.stop()
        hist = fx.store()["history"]
        check("history is newest first, without repeats",
              hist[:4] == [WHITE, "#000000", "#10B981", "#123456"] and len(hist) == len(set(hist)), hist[:6])
        fx.to_ext()
        d.get(f"{EXT}/popup/popup.html?tab={tab_id}")
        wait(lambda: len(d.find_elements(By.CSS_SELECTOR, "#history .swatch")) == len(hist))
        sw = d.find_elements(By.CSS_SELECTOR, "#history .swatch")
        check("popup shows every history color in order",
              [s.get_attribute("data-hex") for s in sw] == hist, len(sw))
        check("contrast check uses the last two picks", d.find_element(By.ID, "ratio").text == "21.00:1",
              d.find_element(By.ID, "ratio").text)
        check("black on white passes every level",
              [b.get_attribute("class") for b in d.find_elements(By.CSS_SELECTOR, "#badges .badge")] == ["badge pass"] * 4)
        check("sample is drawn in the two colors",
              d.execute_script("const s = getComputedStyle(document.getElementById('sample')); return [s.color, s.backgroundColor]")
              == ["rgb(0, 0, 0)", "rgb(255, 255, 255)"])

        fx.clear_clipboard()
        fx.to_ext()
        d.find_element(By.CSS_SELECTOR, '#history .swatch[data-hex="#10B981"]').click()
        check("clicking a history color copies it", wait(lambda: fx.clipboard() == "#10B981", 3), fx.clipboard())
        fx.to_ext()
        check("with a visible confirmation", "Copied #10B981" in d.find_element(By.ID, "toast").text
              and "show" in d.find_element(By.ID, "toast").get_attribute("class"), d.find_element(By.ID, "toast").text)
        check("and selects it", d.find_element(By.CSS_SELECTOR, '.value[data-format="rgb"] .v').text == "rgb(16, 185, 129)")
        d.find_element(By.CSS_SELECTOR, '.value[data-format="hsl"]').click()
        check("format rows copy their own format", wait(lambda: fx.clipboard() == "hsl(160, 84%, 39%)", 3), fx.clipboard())
        fx.to_ext()
        check("row says Copied", wait(lambda: d.find_element(By.CSS_SELECTOR, '.value[data-format="hsl"] .act').text == "Copied", 2))

        d.find_element(By.ID, "slotFg").click()  # green text on white
        want = contrast_ratio("#10B981", WHITE)
        check("Text slot takes the selected color",
              wait(lambda: d.find_element(By.ID, "ratio").text == f"{int(want * 100) / 100:.2f}:1", 3),
              f'{d.find_element(By.ID, "ratio").text} want {want:.4f}')
        cls = [b.get_attribute("class").split()[-1] for b in d.find_elements(By.CSS_SELECTOR, "#badges .badge")]
        check("green on white: fails AA, AA large, AAA, AAA large as WCAG says",
              cls == ["pass" if want >= t else "fail" for t in (4.5, 3, 7, 4.5)], cls)
        check("pinned pair is saved", fx.store().get("contrast") == {"fg": "#10B981", "bg": WHITE}, fx.store().get("contrast"))
        fx.to_ext()
        d.find_element(By.CSS_SELECTOR, '#history .swatch[data-hex="#123456"]').click()
        d.find_element(By.ID, "slotBg").click()
        want = contrast_ratio("#10B981", "#123456")
        check("Background slot takes the selected color",
              wait(lambda: d.find_element(By.ID, "ratio").text == f"{int(want * 100) / 100:.2f}:1", 3),
              f'{d.find_element(By.ID, "ratio").text} want {want:.4f}')
        d.find_element(By.ID, "swap").click()
        check("swap", wait(lambda: fx.store().get("contrast") == {"fg": "#123456", "bg": "#10B981"}, 3))
        fx.to_ext()
        d.find_element(By.ID, "contrastReset").click()
        check("back to following the latest picks", wait(lambda: d.find_element(By.ID, "ratio").text == "21.00:1", 3))

        d.find_element(By.CSS_SELECTOR, '#format button[data-format="rgb"]').click()
        check("format switch in the popup is saved", wait(lambda: fx.store()["settings"]["format"] == "rgb", 3))
        fx.to_ext()
        d.find_element(By.CSS_SELECTOR, '#format button[data-format="hex"]').click()
        wait(lambda: fx.store()["settings"]["format"] == "hex", 3)
        fx.to_ext()
        # ---- the one-time thank-you card (well over 50 picks by now)
        card = d.find_element(By.ID, "supportCard")
        check("thank-you card appears after 50 picks, inside the popup only",
              fx.picks() >= 50 and card.is_displayed() and "picked 50 colors" in card.text, card.text[:60])
        fx.to_ext()
        [b for b in card.find_elements(By.CSS_SELECTOR, "button") if b.text == "Don't ask again"][0].click()
        check("Don't ask again is remembered", wait(lambda: (fx.store().get("supportNudge") or {}).get("optOut") is True, 3))
        fx.to_ext()
        d.refresh()
        wait(lambda: d.find_element(By.ID, "current").is_displayed(), 3)
        time.sleep(0.3)
        check("and the card stays away", not d.find_element(By.ID, "supportCard").is_displayed())

        d.find_element(By.ID, "clear").click()
        time.sleep(0.3)
        check("Clear asks for a second click", len(fx.store()["history"]) == len(hist))
        fx.to_ext()
        d.find_element(By.ID, "clear").click()
        check("second click clears the history", wait(lambda: fx.store()["history"] == [], 3))
        fx.to_ext()
        check("popup is back to its empty state", wait(lambda: d.find_element(By.ID, "empty").is_displayed(), 3))

        # ---- strict Content-Security-Policy page
        fx.to_page()
        d.get(BASE + "?csp=1")
        blocked = fx.js("""
          const s = document.createElement('style'); s.textContent = '#b1 { outline: 5px solid red }'; document.head.append(s);
          return getComputedStyle(document.getElementById('b1')).outlineStyle;""")
        check("CSP page really blocks injected styles", blocked == "none", blocked)
        check("picker starts under a strict CSP", fx.start())
        r = fx.rect("b2")
        fx.move(r["cx"], r["cy"])
        time.sleep(0.5)
        check("loupe is styled and visible under a strict CSP",
              fx.screen_pixel(r["cx"] + 67, r["cy"]) == WHITE and fx.screen_pixel(r["cx"], r["cy"]) == "#10B981",
              fx.screen_pixel(r["cx"] + 67, r["cy"]))
        fx.clear_clipboard()
        check("pick under a strict CSP", fx.pick(r["cx"], r["cy"], keep=False) == "#10B981")
        check("copy under a strict CSP", wait(lambda: fx.clipboard() == "#10B981", 3), fx.clipboard())
        wait(lambda: not fx.overlay(), 4)

        # ---- a page that swallows copy events
        fx.to_page()
        d.get(BASE + "?blockcopy=1")
        fx.clear_clipboard()
        fx.start()
        r = fx.rect("b3")
        fx.pick(r["cx"], r["cy"], keep=False)
        check("copy still works when the page blocks copy events", wait(lambda: fx.clipboard() == "#F59E0B", 3), fx.clipboard())
        wait(lambda: not fx.overlay(), 4)

        # ---- plain http page: no navigator.clipboard there
        fx.to_page()
        d.get(f"http://{PLAIN_HOST}:{PORT}/test-page.html")
        check("http page is not a secure context", fx.js("return window.isSecureContext === false && !navigator.clipboard"))
        fx.clear_clipboard()
        fx.start()
        r = fx.rect("b4")
        check("pick on a plain http page", fx.pick(r["cx"], r["cy"], keep=False) == "#123456")
        check("copy on a plain http page", wait(lambda: fx.clipboard() == "#123456", 3), fx.clipboard())
        wait(lambda: not fx.overlay(), 4)

        # ---- a page Firefox keeps add-ons out of
        fx.to_page()
        d.get("about:addons")
        tab_id = fx.page_tab_id()
        fx.to_ext()
        d.get(f"{EXT}/popup/popup.html?tab={tab_id}")
        d.find_element(By.ID, "pick").click()
        check("popup explains when a page is off limits",
              wait(lambda: d.find_element(By.ID, "pickError").is_displayed(), 4) is True
              and "doesn't let add-ons" in d.find_element(By.ID, "pickError").text, d.find_element(By.ID, "pickError").text)
        fx.shortcut()
        check("shortcut on such a page flags the toolbar button",
              wait(lambda: fx.api("return await browser.action.getBadgeText({});") == "!", 3))
        check("and the flag clears itself", wait(lambda: fx.api("return await browser.action.getBadgeText({});") == "", 5))
    finally:
        fx.quit()


def session_screen(unpacked, tag, prefs, stripes):
    """Same accuracy checks on another kind of screen."""
    fx = Fx(unpacked, prefs)
    try:
        fx.to_page()
        fx.d.get(BASE)
        vp = fx.viewport()
        if prefs.get("privacy.resistFingerprinting"):
            off = fx.js(CANVAS_NOISE_JS)
            check(f"{tag}: Firefox scrambles the page's own canvas reads", off > 1000, off)
        check(f"{tag}: picker starts", fx.start(), vp)
        accuracy(fx, tag, stripes)
        scrolling(fx, tag)
        if stripes and abs(vp["dpr"] - 2) < 0.01:
            hs = fx.rect("hs")
            fx.move(hs["left"], hs["top"] + 20)
            time.sleep(0.5)
            got = []
            for _ in range(4):
                before = fx.picks()
                fx.shift_keys(Keys.ENTER)
                got.append(fx.wait_pick(before))
                fx.keys(Keys.ARROW_RIGHT)
            check(f"{tag}: arrow keys step single device pixels (two per stripe)",
                  got == [STRIPES[0], STRIPES[0], STRIPES[1], STRIPES[1]], got)
            fx.scroll(0, 0.5)
            time.sleep(0.2)
            top = fx.rect("vs")["top"]
            if abs(top % 1 - 0.5) < 0.01:
                y = math.ceil(top) + 10
                got = fx.pick(fx.rect("vs")["left"] + 20, y, settle=0.4)
                check(f"{tag}: half-pixel scroll offset", got == STRIPES[int(y - top) % 4], f"{got} top={top}")
            fx.scroll(0, 0)
        fx.stop()
    finally:
        fx.quit()


def session_fpp(unpacked):
    """Canvas randomization: pages get noise in canvas read-backs. The picker must still be exact."""
    fx = Fx(unpacked, {
        "privacy.fingerprintingProtection": True,
        # Only this one protection: with Firefox 147's other defaults left on, no noise is added at all.
        "privacy.fingerprintingProtection.overrides": "-AllTargets,+CanvasRandomization",
    })
    try:
        fx.to_page()
        fx.d.get(BASE)
        noisy = fx.js(CANVAS_NOISE_JS)
        check("fpp: Firefox is adding noise to the page's own canvas reads", noisy > 0, noisy)
        check("fpp: picker starts", fx.start())
        accuracy(fx, "fpp", stripes=True)
        fx.stop()
    finally:
        fx.quit()


SESSIONS = {
    "main": session_main,
    "dpr2": lambda u: session_screen(u, "2x screen", {"layout.css.devPixelsPerPx": "2"}, True),
    "dpr125": lambda u: session_screen(u, "125% screen", {"layout.css.devPixelsPerPx": "1.25"}, False),
    "dpr175": lambda u: session_screen(u, "175% screen", {"layout.css.devPixelsPerPx": "1.75"}, False),
    "rfp": lambda u: session_screen(u, "resistFingerprinting", {"privacy.resistFingerprinting": True}, True),
    "fpp": session_fpp,
}


def main():
    os.environ.setdefault("SE_AVOID_STATS", "true")  # Selenium Manager: no usage ping
    zips = sorted((ROOT / "dist").glob("*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        sys.exit("build first: python tools/release.py build")
    names = [a for a in sys.argv[1:] if a in SESSIONS] or list(SESSIONS)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # Install the *built zip*, extracted, by path. Handing geckodriver the zip
    # itself installs from a temp copy it deletes, and scripts then fail to load.
    unpacked = Path(tempfile.mkdtemp(prefix="pipette-e2e-"))
    try:
        with zipfile.ZipFile(zips[-1]) as z:
            z.extractall(unpacked)
        for name in names:
            print(f"--- {name}", flush=True)
            try:
                SESSIONS[name](unpacked)
            except Exception as e:  # noqa: BLE001
                check(f"{name}: session ran to the end", False, repr(e)[:300])
    finally:
        httpd.shutdown()
        shutil.rmtree(unpacked, ignore_errors=True)
        failed = [n for n, ok in results if not ok]
        print(f"\n{len(results) - len(failed)}/{len(results)} passed")
        if failed:
            print("FAILED: " + ", ".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
