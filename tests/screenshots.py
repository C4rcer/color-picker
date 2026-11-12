"""Capture the AMO listing screenshots (1280x800) of Pipette in a real Firefox.

  python tools/release.py build
  python tests/screenshots.py          (needs selenium + pillow)

Writes store-assets/screenshots/{1-loupe,2-popup,3-contrast}.png. The backdrop
is tests/demo-page.html (a made-up studio site) and the history is seeded with
example colors so the popup is not empty; nothing here ships in the add-on.
The picker itself is started the real way, with the keyboard shortcut.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_firefox import ADDON_ID, UUID, Handler, wait  # noqa: E402

import http.server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 8942
DEMO_HOST = "meridian.example.com"  # mapped to 127.0.0.1 via network.dns.localDomains
BASE = f"http://{DEMO_HOST}:{PORT}/demo-page.html"
EXT = f"moz-extension://{UUID}"
OUT = ROOT / "store-assets" / "screenshots"
W, H = 1280, 800
# 1.5 device pixels per CSS pixel. Not 1.6: Firefox can only do scales where 60 / scale is a
# whole number, so 1.6 silently becomes 1.579 and the captures come out 1263x789.
DPR = 1.5

# Newest first. The first two are "white text on ultramarine": passes AA, fails AAA.
HISTORY = ["#3D3BF3", "#FFFFFF", "#FF6B5E", "#FFB627", "#1FA6A0", "#6D2E8C", "#14142B", "#FFF7EC", "#FFE3B8", "#8A4B00",
           "#E4572E", "#17BEBB", "#76B041", "#F4D35E", "#0D3B66", "#EE964B", "#083D77", "#2E282A"]
SEED = f"""
await browser.storage.local.set({{
  history: {json.dumps(HISTORY)},
  settings: {{format: "hex", lowercase: false}},
  stats: {{picks: 38}},
}});
return true;
"""


def fit(img):
    """Crop/pad a capture to exactly 1280x800."""
    canvas = Image.new("RGB", (W, H), (255, 247, 236))
    canvas.paste(img.convert("RGB").crop((0, 0, min(W, img.width), min(H, img.height))), (0, 0))
    return canvas


def card(img, radius=18, scale=1.0):
    """Rounded corners and a soft drop shadow around a capture."""
    img = img.convert("RGBA")
    if scale != 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius, fill=255)
    pad = 40
    out = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    sm = Image.new("L", out.size, 0)
    ImageDraw.Draw(sm).rounded_rectangle((pad, pad + 8, pad + img.width, pad + img.height + 8), radius, fill=130)
    out.putalpha(sm.filter(ImageFilter.GaussianBlur(18)))
    out.paste(img, (pad, pad), mask)
    return out, pad


def find_meeting_point(img, box, probes):
    """A pixel inside box where each (dx, dy, color) probe around it has that exact color."""
    px = img.convert("RGB").load()
    reach = max(max(abs(dx), abs(dy)) for dx, dy, _ in probes)
    x0, y0, x1, y1 = box
    for y in range(y0 + reach, y1 - reach):
        for x in range(x0 + reach, x1 - reach):
            if all(px[x + dx, y + dy] == color for dx, dy, color in probes):
                return x, y
    return None


def rgb(hex_):
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def main():
    os.environ.setdefault("SE_AVOID_STATS", "true")
    zips = sorted((ROOT / "dist").glob("*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        sys.exit("build first: python tools/release.py build")
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    opts = Options()
    opts.binary_location = r"C:\Program Files\Mozilla Firefox\firefox.exe"
    opts.add_argument("-headless")
    opts.set_preference("media.volume_scale", "0.0")
    opts.set_preference("extensions.webextensions.uuids", json.dumps({ADDON_ID: UUID}))
    opts.set_preference("layout.css.devPixelsPerPx", str(DPR))
    opts.set_preference("network.dns.localDomains", DEMO_HOST)
    opts.set_preference("ui.systemUsesDarkTheme", 0)
    opts.set_preference("layout.css.prefers-color-scheme.content-override", 1)  # light
    driver = webdriver.Firefox(options=opts, service=Service(service_args=["--allow-system-access"]))
    unpacked = Path(tempfile.mkdtemp(prefix="pipette-shots-"))
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zips[-1]) as z:
            z.extractall(unpacked)
        driver.execute("INSTALL_ADDON", {"path": str(unpacked), "temporary": True})
        time.sleep(1)
        cw, ch = round(W / DPR), round(H / DPR)
        outer = [cw, ch + 90]
        for _ in range(6):  # grow the window until the page area is 853x533 CSS px (1280x800 on screen)
            driver.set_window_size(*outer)
            time.sleep(0.3)
            inner = driver.execute_script("return [innerWidth, innerHeight]")
            if inner == [cw, ch]:
                break
            outer = [outer[0] + cw - inner[0], outer[1] + ch - inner[1]]
        print("viewport:", inner, "window:", outer)
        page = driver.current_window_handle

        def api(code):
            return driver.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                f"(async () => {{ {code} }})().then(done, (e) => done('ERR ' + e));")

        # Extension pages in a second window, so the demo page stays in front in its own.
        driver.switch_to.new_window("window")
        ext = driver.current_window_handle
        driver.get(f"{EXT}/options/options.html")
        api(SEED)

        # ---- 1: the loupe on the demo page
        driver.switch_to.window(page)
        driver.get(BASE)
        time.sleep(0.8)
        base_path = OUT / "_base.png"
        driver.save_screenshot(str(base_path))
        base = Image.open(base_path)
        art = driver.execute_script("const r = document.getElementById('art').getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]")
        real = driver.execute_script("return devicePixelRatio")
        box = tuple(int(v * real) for v in art)
        # Where the sun, the hill and the wave meet: three colors inside one loupe.
        spot = find_meeting_point(base, box, [(-3, -3, rgb("#FFB627")), (4, 0, rgb("#FF6B5E")), (-3, 4, rgb("#1FA6A0"))])
        if not spot:
            sys.exit("could not find the spot where sun, hill and wave meet")
        sx, sy = spot[0] / real, spot[1] / real

        with driver.context("chrome"):
            ActionChains(driver).key_down(Keys.ALT).key_down(Keys.SHIFT).send_keys("c").key_up(Keys.SHIFT).key_up(Keys.ALT).perform()
        wait(lambda: driver.execute_script("return !!document.querySelector('[data-pipette-overlay]')"), 5)
        ac = ActionChains(driver)
        ac.w3c_actions.pointer_action.move_to_location(int(sx) - 40, int(sy) - 30)
        ac.perform()
        time.sleep(0.5)
        ac = ActionChains(driver)
        ac.w3c_actions.pointer_action.move_to_location(round(sx), round(sy))
        ac.perform()
        time.sleep(0.8)
        driver.save_screenshot(str(OUT / "1-loupe.png"))
        fit(Image.open(OUT / "1-loupe.png")).save(OUT / "1-loupe.png")
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()

        # ---- 2: the popup, rendered in a tab and composited where Firefox opens it
        driver.switch_to.window(ext)
        tab_id = api("""
          const me = await browser.tabs.getCurrent();
          const t = (await browser.tabs.query({ active: true })).find((x) => x.windowId !== me.windowId);
          return t ? t.id : null;""")
        driver.set_window_size(outer[0], 1000)
        driver.get(f"{EXT}/popup/popup.html?tab={tab_id}")
        wait(lambda: len(driver.find_elements(By.CSS_SELECTOR, "#history .swatch")) == len(HISTORY), 5)
        time.sleep(0.6)
        size = driver.execute_script("return [document.body.offsetWidth, document.body.offsetHeight]")
        print("popup CSS size:", size)
        pop_path = OUT / "_popup.png"
        driver.find_element(By.TAG_NAME, "body").screenshot(str(pop_path))
        popup = Image.open(pop_path)

        backdrop = fit(base).convert("RGBA")
        backdrop = Image.alpha_composite(backdrop, Image.new("RGBA", backdrop.size, (20, 20, 43, 90)))
        k = min(1.0, (H - 24) / popup.height)
        shadowed, pad = card(popup, radius=14, scale=k)
        shot = backdrop.copy()
        shot.alpha_composite(shadowed, (W - shadowed.width + pad - 28, 12 - pad))
        shot.convert("RGB").save(OUT / "2-popup.png")

        # ---- 3: the contrast check up close, next to the copy format setting
        contrast_path = OUT / "_contrast.png"
        driver.execute_script("document.getElementById('contrastSection').style.cssText = 'padding: 16px; background: var(--bg); width: 360px;'")
        driver.find_element(By.ID, "contrastSection").screenshot(str(contrast_path))
        driver.get(f"{EXT}/options/options.html")
        time.sleep(0.8)
        driver.execute_script(
            "const st = document.createElement('style');"
            "st.textContent = '.page { max-width: 400px; padding: 0 } .card-block { margin: 0 }';"
            "document.head.append(st);")
        time.sleep(0.3)
        fmt_path = OUT / "_format.png"
        driver.find_element(By.ID, "format").find_element(By.XPATH, "./ancestor::section").screenshot(str(fmt_path))
        keys_path = OUT / "_keys.png"
        driver.find_element(By.ID, "shortcut").find_element(By.XPATH, "./ancestor::section").screenshot(str(keys_path))

        backdrop = fit(base).convert("RGBA")
        backdrop = Image.alpha_composite(backdrop, Image.new("RGBA", backdrop.size, (20, 20, 43, 150)))
        c_img, pad = card(Image.open(contrast_path), radius=18, scale=1.18)
        f_img, _ = card(Image.open(fmt_path), radius=18, scale=0.86)
        k_img, _ = card(Image.open(keys_path), radius=18, scale=0.86)
        gap = 26
        right_h = (f_img.height - 2 * pad) + gap + (k_img.height - 2 * pad)
        total_w = (c_img.width - 2 * pad) + gap + (f_img.width - 2 * pad)
        x_l = (W - total_w) // 2
        x_r = x_l + (c_img.width - 2 * pad) + gap
        shot = backdrop.copy()
        shot.alpha_composite(c_img, (x_l - pad, (H - (c_img.height - 2 * pad)) // 2 - pad))
        top_r = (H - right_h) // 2
        shot.alpha_composite(f_img, (x_r - pad, top_r - pad))
        shot.alpha_composite(k_img, (x_r - pad, top_r + (f_img.height - 2 * pad) + gap - pad))
        shot.convert("RGB").save(OUT / "3-contrast.png")
    finally:
        driver.quit()
        httpd.shutdown()
        shutil.rmtree(unpacked, ignore_errors=True)
        for tmp in OUT.glob("_*.png"):
            tmp.unlink()
    for p in sorted(OUT.glob("*.png")):
        print(p.relative_to(ROOT), Image.open(p).size)


if __name__ == "__main__":
    main()
