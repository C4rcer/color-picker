// The in-page eyedropper. Injected on demand (popup button or shortcut) with
// scripting.executeScript, so it only ever runs on a tab the user asked for.
//
// Firefox has no EyeDropper API, so the picker works from a screenshot of the
// visible page (tabs.captureVisibleTab, taken by the background):
//   * the screenshot is drawn into a canvas that never touches the DOM,
//   * the cursor position is turned into one image pixel (see toPixel),
//   * the loupe shows the 11 x 11 image pixels around it, and the middle one
//     is the color that gets copied. What the loupe shows is what you get.
// A screenshot is a still picture, so anything that moves the page under it
// (scrolling, resizing, zooming, coming back to the tab) throws it away and
// takes a new one. A click that lands in between waits for the new picture.
//
// The overlay lives in a closed shadow root and is styled only through the
// CSSOM (element.style), so page styles can not reach it and a strict
// Content-Security-Policy can not block it. Nothing here asks for money,
// phones home or reads the page's content.
(() => {
  "use strict";
  if (globalThis.__pipettePicker) return; // already loaded in this page; its listener below is still alive
  globalThis.__pipettePicker = true;

  const Color = globalThis.PipetteColor;
  const GRID = 11; // the loupe shows GRID x GRID image pixels
  const HALF = (GRID - 1) / 2;
  const CELL = 12; // each one drawn about CELL x CELL CSS pixels
  const RING = 3; // white ring around the loupe, CSS pixels
  const ATTR = "data-pipette-overlay";
  const XHTML = "http://www.w3.org/1999/xhtml";
  const ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const FONT = "600 12px/16px ui-monospace, 'Cascadia Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace";
  const PILL =
    "position:absolute;left:0;top:0;display:none;align-items:center;gap:7px;padding:5px 10px 5px 6px;" +
    "border-radius:9px;background:rgba(22,22,26,.96);color:#fff;white-space:nowrap;font:" + FONT + ";" +
    "box-shadow:0 0 0 1px rgba(255,255,255,.16),0 6px 18px rgba(0,0,0,.38);";
  // Inline !important beats anything a page stylesheet can say about a div or a popover.
  const HOST_CSS = [
    "all:initial", "position:fixed", "inset:0", "width:auto", "height:auto", "margin:0", "padding:0", "border:0",
    "background:transparent", "overflow:visible", "display:block", "z-index:2147483647", "pointer-events:auto",
    "opacity:1", "visibility:visible", "transform:none", "filter:none", "max-width:none", "max-height:none",
  ].map((d) => d + " !important").join(";");

  let active = false; // overlay is up
  let closing = false; // a color was picked: listeners are off, the "Copied" label is still showing
  let ui = null;
  let shot = null; // { canvas, ctx, w, h, sx, sy, ox, oy, gen }
  let gen = 0; // goes up whenever the page moves under the screenshot
  let capturing = false;
  let refreshTimer = 0;
  let closeTimer = 0;
  let hintTimer = 0;
  let flashTimer = 0;
  let frame = 0;
  let pos = null; // where the user points, CSS pixels in the viewport
  let pendingPick = null; // a click that is waiting for a fresh screenshot
  let flash = null; // { text, color } shown in the label right after a pick
  let hinted = false; // the how-to line was shown for this session
  let raisedAt = 0; // when the overlay was last moved to the top of the top layer
  let settings = { format: "hex", lowercase: false };

  // ------------------------------------------------------------------ overlay

  function make(tag, css) {
    const n = document.createElementNS(XHTML, tag);
    n.style.cssText = css;
    return n;
  }

  function build() {
    for (const old of document.querySelectorAll("[" + ATTR + "]")) old.remove(); // left by an older copy of this script
    const host = make("div", HOST_CSS);
    host.setAttribute(ATTR, "");
    host.setAttribute("aria-hidden", "true");
    const shadow = host.attachShadow({ mode: "closed" });

    // Covers the viewport and takes the pointer, so the page under it does not
    // react to hover or clicks while a color is being chosen.
    const root = make("div", "position:fixed;inset:0;direction:ltr;cursor:crosshair;touch-action:none;-moz-user-select:none;user-select:none;");
    // 100vw x 100vh, never painted: tells us the exact (fractional) viewport size.
    const probe = make("div", "position:fixed;left:0;top:0;width:100vw;height:100vh;visibility:hidden;pointer-events:none;");
    // Everything that is painted. Hidden while a screenshot is taken, so the
    // loupe never ends up in its own picture.
    const layer = make("div", "position:absolute;inset:0;overflow:hidden;pointer-events:none;visibility:hidden;");

    const loupe = make("div",
      "position:absolute;left:0;top:0;display:none;box-sizing:content-box;border-radius:50%;overflow:hidden;" +
      "border:" + RING + "px solid #fff;background:#16161a;" +
      "box-shadow:0 0 0 1px rgba(0,0,0,.65),0 8px 26px rgba(0,0,0,.4);");
    const canvas = make("canvas", "display:block;");
    loupe.append(canvas);

    const label = make("div", PILL);
    const swatch = make("span", "display:block;width:16px;height:16px;border-radius:5px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.35);");
    const text = make("span", "display:block;");
    label.append(swatch, text);

    const hint = make("div", PILL + "padding:6px 12px;font:500 12px/16px system-ui,sans-serif;transition:opacity .25s;");
    hint.textContent = "Click to copy  ·  Shift+click to keep picking  ·  Esc to cancel";

    layer.append(loupe, label, hint);
    root.append(probe, layer);
    shadow.append(root);
    document.documentElement.append(host);
    try {
      host.popover = "manual";
    } catch {
      // Still a fixed, max z-index overlay without popover support.
    }
    ui = { host, root, probe, layer, loupe, canvas, g: canvas.getContext("2d"), label, swatch, text, hint, cell: 0 };
    raise();
  }

  /**
   * Put the overlay on top of the top layer: above every z-index, and above
   * dialogs, popovers and fullscreen elements that are already open. Things a
   * page opens later land above it again, so this is repeated (see onPointer).
   */
  function raise() {
    raisedAt = Date.now();
    try {
      ui.host.hidePopover(); // does nothing when it is not showing yet
    } catch {
      // No popover support, or the page took the host out of the document.
    }
    try {
      ui.host.showPopover();
    } catch {
      // Without it this is still a fixed overlay with the highest z-index.
    }
  }

  // --------------------------------------------------------------- screenshot

  function fresh() {
    return !!shot && shot.gen === gen;
  }

  /** Everything that decides which part of the page the viewport shows, and how big. */
  function viewState() {
    const r = ui.probe.getBoundingClientRect();
    const vv = window.visualViewport;
    return [
      window.scrollX, window.scrollY, r.width, r.height, window.devicePixelRatio,
      vv ? vv.offsetLeft : 0, vv ? vv.offsetTop : 0, vv ? vv.scale : 1,
    ].join(",");
  }

  function frames(n) {
    const raf = new Promise((done) => {
      const step = () => (--n <= 0 ? done() : requestAnimationFrame(step));
      requestAnimationFrame(step);
    });
    // requestAnimationFrame stalls in a throttled window; never wait on it forever.
    return Promise.race([raf, new Promise((done) => setTimeout(done, 300))]);
  }

  async function decode(url) {
    // Decoded by hand instead of <img src> or fetch(), which a page's
    // Content-Security-Policy could refuse.
    const b64 = url.slice(url.indexOf(",") + 1);
    let bytes;
    if (typeof Uint8Array.fromBase64 === "function") {
      bytes = Uint8Array.fromBase64(b64);
    } else {
      const bin = atob(b64);
      bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    }
    const blob = new Blob([bytes], { type: "image/png" });
    let bitmap;
    try {
      bitmap = await createImageBitmap(blob, { colorSpaceConversion: "none" });
    } catch {
      bitmap = await createImageBitmap(blob);
    }
    const canvas = document.createElementNS(XHTML, "canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    return { canvas, ctx, w: canvas.width, h: canvas.height };
  }

  /**
   * How viewport CSS pixels map onto screenshot pixels.
   *
   * The background asks Firefox for exactly `scale` image pixels per CSS pixel
   * (the page's devicePixelRatio), with the viewport's top left corner at 0,0.
   * That is trusted when the image size agrees with it. If it does not (an
   * unexpected Firefox change, or no scale at all), the scale is measured from
   * the image instead: the viewport size whose x and y scales agree is the one
   * that was captured.
   */
  function mapping(w, h, scale) {
    const all = ui.probe.getBoundingClientRect(); // viewport with scrollbars
    const inner = ui.root.getBoundingClientRect(); // viewport without scrollbars
    const near = (a, b) => Math.abs(a - b) <= 2;
    if (scale > 0) {
      const fits = (r) => near(w, r.width * scale) && near(h, r.height * scale);
      if (fits(all) || fits(inner)) return { sx: scale, sy: scale, ox: 0, oy: 0 };
    }
    const cands = [
      { W: all.width, H: all.height, ox: 0, oy: 0 },
      { W: inner.width, H: inner.height, ox: 0, oy: 0 },
    ];
    const vv = window.visualViewport;
    if (vv && Math.abs(vv.scale - 1) > 0.01) cands.push({ W: vv.width, H: vv.height, ox: vv.offsetLeft, oy: vv.offsetTop });
    let best = null;
    for (const c of cands) {
      if (!(c.W > 0 && c.H > 0)) continue;
      const sx = w / c.W, sy = h / c.H;
      const err = Math.abs(sx - sy) / Math.max(sx, sy);
      if (!best || err < best.err - 1e-9) best = { sx, sy, ox: c.ox, oy: c.oy, err };
    }
    return best || { sx: 1, sy: 1, ox: 0, oy: 0 };
  }

  /** Viewport position (CSS px) -> the screenshot pixel under it. */
  function toPixel(p) {
    const clamp = (v, max) => Math.min(max, Math.max(0, v));
    return {
      // The epsilon keeps 17 * 1.7647... = 29.999999 from landing one pixel short.
      ix: clamp(Math.floor((p.x - shot.ox) * shot.sx + 1e-6), shot.w - 1),
      iy: clamp(Math.floor((p.y - shot.oy) * shot.sy + 1e-6), shot.h - 1),
    };
  }

  /** Screenshot pixel -> the viewport position (CSS px) of its middle. */
  function toPoint(px) {
    return { x: (px.ix + 0.5) / shot.sx + shot.ox, y: (px.iy + 0.5) / shot.sy + shot.oy };
  }

  function readPixel(px) {
    const d = shot.ctx.getImageData(px.ix, px.iy, 1, 1).data;
    return { r: d[0], g: d[1], b: d[2] };
  }

  function invalidate() {
    if (!active || closing) return;
    gen++;
    requestDraw(); // hides the loupe until the new picture is in
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 90); // once the scroll or resize has settled
  }

  async function refresh() {
    if (!active || closing || capturing) return;
    if (document.hidden) return; // visibilitychange calls invalidate() when the tab is back
    capturing = true;
    const mine = gen;
    const mineUi = ui;
    ui.layer.style.visibility = "hidden";
    await frames(2);
    const view = active && ui === mineUi ? viewState() : "";

    let res = null;
    let next = null;
    if (active && ui === mineUi) {
      try {
        res = await browser.runtime.sendMessage({ type: "pipette:capture", dpr: window.devicePixelRatio });
      } catch {
        res = { ok: false, error: "gone" }; // the add-on was updated or removed under us
      }
      if (active && ui === mineUi && res && res.ok && mine === gen) {
        try {
          next = await decode(res.url);
        } catch {
          res = { ok: false, error: "decode" };
        }
      }
    }
    capturing = false;
    if (active && !closing && ui !== mineUi) {
      refresh(); // the picker was stopped and started again while this picture was on its way
      return;
    }
    if (!active || closing) return;
    ui.layer.style.visibility = "visible";

    if (mine !== gen || view !== viewState()) { // the page moved again while the picture was being taken
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refresh, 90);
      return;
    }
    if (!next) {
      if (res && res.error === "inactive") { // another tab was in front: try again shortly
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refresh, 400);
        return;
      }
      fail(res && res.error === "gone" ? "" : "Pipette could not take a picture of this page");
      return;
    }
    if (shot) shot.canvas.width = 0; // free the old picture now, not at the next GC
    shot = { ...next, gen: mine, view, ...mapping(next.w, next.h, res.scale) };
    const first = !hinted;
    hinted = true;
    if (pendingPick) {
      const p = pendingPick;
      pendingPick = null;
      pos = p.pos;
      commit(p.keep);
    } else {
      if (first) showHint("", 3200);
      requestDraw();
    }
  }

  // --------------------------------------------------------------------- draw

  function requestDraw() {
    if (!frame) frame = requestAnimationFrame(draw);
  }

  function place(node, x, y) {
    node.style.transform = "translate(" + x.toFixed(2) + "px," + y.toFixed(2) + "px)";
  }

  function draw() {
    frame = 0;
    if (!active || !ui) return;
    if (!fresh() || !pos) {
      ui.loupe.style.display = "none";
      if (!flash) ui.label.style.display = "none";
      return;
    }
    const px = toPixel(pos);
    const at = toPoint(px);
    const rgb = readPixel(px);
    const hex = Color.toHex(rgb);

    // Loupe canvas: whole device pixels per cell, so the grid stays crisp.
    const dpr = window.devicePixelRatio || 1;
    const cell = Math.max(2, Math.round(CELL * dpr));
    const size = cell * GRID;
    const css = size / dpr;
    const c = ui.canvas, g = ui.g;
    if (ui.cell !== cell) {
      ui.cell = cell;
      c.width = size;
      c.height = size;
      c.style.width = css + "px";
      c.style.height = css + "px";
    }
    g.imageSmoothingEnabled = false;
    g.fillStyle = "#16161a"; // beyond the edge of the page
    g.fillRect(0, 0, size, size);
    const x0 = px.ix - HALF, y0 = px.iy - HALF;
    const cx0 = Math.max(0, x0), cy0 = Math.max(0, y0);
    const cx1 = Math.min(shot.w, x0 + GRID), cy1 = Math.min(shot.h, y0 + GRID);
    if (cx1 > cx0 && cy1 > cy0) {
      g.drawImage(shot.canvas, cx0, cy0, cx1 - cx0, cy1 - cy0,
        (cx0 - x0) * cell, (cy0 - y0) * cell, (cx1 - cx0) * cell, (cy1 - cy0) * cell);
    }
    g.fillStyle = "rgba(127,127,127,.3)";
    for (let i = 1; i < GRID; i++) {
      g.fillRect(i * cell, 0, 1, size);
      g.fillRect(0, i * cell, size, 1);
    }
    // The middle cell is the pick: white frame with a dark hairline, readable on any color.
    const t = Math.max(1, Math.round(dpr));
    const m = HALF * cell;
    g.lineWidth = t;
    g.strokeStyle = "rgba(0,0,0,.85)";
    g.strokeRect(m - 2.5 * t, m - 2.5 * t, cell + 5 * t, cell + 5 * t);
    g.lineWidth = 2 * t;
    g.strokeStyle = "#fff";
    g.strokeRect(m - t, m - t, cell + 2 * t, cell + 2 * t);

    const r = css / 2 + RING;
    ui.loupe.style.display = "block";
    place(ui.loupe, at.x - r, at.y - r);

    if (!flash) {
      ui.swatch.style.background = hex;
      ui.text.style.color = "#fff";
      ui.text.textContent = Color.format(rgb, settings.format, settings);
    }
    placeLabel(at, r);
  }

  /** Label under the loupe, or above it near the bottom edge, kept inside the viewport. */
  function placeLabel(at, r) {
    const label = ui.label;
    label.style.display = "flex";
    const vw = ui.root.clientWidth, vh = ui.root.clientHeight;
    const lw = label.offsetWidth, lh = label.offsetHeight;
    let y = at.y + r + 8;
    if (y + lh > vh - 4) y = at.y - r - 8 - lh;
    const x = Math.min(Math.max(4, at.x - lw / 2), Math.max(4, vw - lw - 4));
    place(label, x, Math.max(4, y));
  }

  function showHint(message, ms) {
    const hint = ui.hint;
    if (message) hint.textContent = message;
    hint.style.display = "flex";
    hint.style.opacity = "1";
    place(hint, Math.max(4, (ui.root.clientWidth - hint.offsetWidth) / 2), 14);
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      if (ui) ui.hint.style.opacity = "0";
    }, ms);
  }

  // --------------------------------------------------------------------- pick

  /** Copy inside the click's user gesture. Works on http pages too, where navigator.clipboard does not exist. */
  function copyText(text) {
    let handled = false;
    const onCopy = (e) => {
      e.clipboardData.setData("text/plain", text);
      e.preventDefault();
      e.stopImmediatePropagation();
      handled = true;
    };
    window.addEventListener("copy", onCopy, true);
    let ran = false;
    try {
      ran = document.execCommand("copy");
    } catch {
      ran = false;
    }
    window.removeEventListener("copy", onCopy, true);
    if (ran && handled) return Promise.resolve(true);
    // A page that swallows copy events: the async clipboard goes around it.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(() => true, () => false);
    }
    return Promise.resolve(false);
  }

  function pick(keep) {
    if (!active || closing) return;
    // A click can beat the scroll event of the scroll that came just before it.
    if (fresh() && shot.view !== viewState()) invalidate();
    if (!pos) {
      if (!fresh()) return;
      pos = toPoint({ ix: Math.floor(shot.w / 2), iy: Math.floor(shot.h / 2) }); // Enter before any pointer move
    }
    if (!fresh()) {
      pendingPick = { keep, pos: { x: pos.x, y: pos.y } }; // the new screenshot finishes the job
      return;
    }
    commit(keep);
  }

  function commit(keep) {
    const rgb = readPixel(toPixel(pos));
    const hex = Color.toHex(rgb);
    const text = Color.format(rgb, settings.format, settings);
    const copied = copyText(text);
    browser.runtime.sendMessage({ type: "pipette:picked", hex }).catch(() => {});

    const mineUi = ui;
    flash = { text: "✓ Copied " + text, color: "#6ee7a8" };
    ui.swatch.style.background = hex;
    ui.text.style.color = flash.color;
    ui.text.textContent = flash.text;
    copied.then((ok) => {
      if (ok || ui !== mineUi || !flash) return;
      ui.text.style.color = "#ffd48a";
      ui.text.textContent = text + " saved to Pipette (this page blocked copying)";
    });
    clearTimeout(hintTimer);
    ui.hint.style.opacity = "0";

    if (frame) cancelAnimationFrame(frame);
    draw(); // loupe and label on the picked pixel, with the "Copied" text
    if (keep) {
      clearTimeout(flashTimer);
      flashTimer = setTimeout(() => {
        flash = null;
        requestDraw();
      }, 1100);
      return;
    }
    // Done: give the page back at once, leave the confirmation up for a moment.
    closing = true;
    unlisten();
    ui.root.style.pointerEvents = "none";
    ui.loupe.style.display = "none";
    closeTimer = setTimeout(stop, 1300);
  }

  function nudge(dx, dy) {
    if (!fresh()) return;
    const from = pos ? toPixel(pos) : { ix: Math.floor(shot.w / 2), iy: Math.floor(shot.h / 2) };
    pos = toPoint({
      ix: Math.min(shot.w - 1, Math.max(0, from.ix + dx)),
      iy: Math.min(shot.h - 1, Math.max(0, from.iy + dy)),
    });
    requestDraw();
  }

  function fail(message) {
    if (!message) {
      stop();
      return;
    }
    closing = true;
    unlisten();
    ui.root.style.pointerEvents = "none";
    ui.loupe.style.display = "none";
    ui.label.style.display = "none";
    ui.layer.style.visibility = "visible";
    showHint(message, 2200);
    closeTimer = setTimeout(stop, 2500);
  }

  // ------------------------------------------------------------------- events

  function swallow(e) {
    e.stopImmediatePropagation();
    if (e.cancelable && e.type !== "pointerdown" && e.type !== "pointerup") e.preventDefault();
  }

  function onPointer(e) {
    if (e.isPrimary === false) return;
    pos = { x: e.clientX, y: e.clientY };
    requestDraw();
    e.stopImmediatePropagation();
    // The pointer is over something that is not the overlay: the page opened a
    // dialog or went fullscreen after the picker started. Get back on top.
    if (e.target !== ui.host && Date.now() - raisedAt > 400) raise();
  }

  function onClick(e) {
    swallow(e);
    if (e.button !== 0) return;
    // The loupe follows pointer events, which carry sub-pixel positions where
    // Firefox has them. Only fall back to the click's own position if there were none.
    if (!pos) pos = { x: e.clientX, y: e.clientY };
    pick(e.shiftKey);
  }

  function onContextMenu(e) {
    swallow(e);
    stop(); // right-click cancels
  }

  function onKey(e) {
    const arrow = ARROWS[e.key];
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (e.key === "Escape" || (plain && (e.key === "Enter" || arrow))) {
      swallow(e);
      if (e.type !== "keydown") return;
      if (e.key === "Escape") stop();
      else if (e.key === "Enter") {
        if (!e.repeat) pick(e.shiftKey);
      } else {
        const step = e.shiftKey ? 10 : 1; // arrow keys move one screen pixel, with Shift ten
        nudge(arrow[0] * step, arrow[1] * step);
      }
    }
  }

  function onVisibility() {
    if (!document.hidden) invalidate();
  }

  const SWALLOWED = [
    "pointerup", "pointerover", "pointerout", "pointerenter", "pointerleave",
    "mousedown", "mouseup", "mousemove", "mouseover", "mouseout", "mouseenter", "mouseleave",
    "dblclick", "auxclick", "dragstart", "selectstart",
  ];
  const LISTENERS = [
    [window, "pointermove", onPointer], [window, "pointerdown", onPointer],
    [window, "click", onClick], [window, "contextmenu", onContextMenu],
    [window, "keydown", onKey], [window, "keyup", onKey], [window, "keypress", onKey],
    [window, "scroll", invalidate], [window, "resize", invalidate], [window, "pagehide", stop],
    [document, "visibilitychange", onVisibility],
    ...SWALLOWED.map((type) => [window, type, swallow]),
  ];
  let listening = false;

  function listen() {
    if (listening) return;
    listening = true;
    // Window, capture phase: first in line, and it still hears everything when a
    // page's modal dialog makes the overlay itself inert.
    for (const [target, type, fn] of LISTENERS) target.addEventListener(type, fn, { capture: true, passive: false });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", invalidate);
      window.visualViewport.addEventListener("scroll", invalidate);
    }
  }

  function unlisten() {
    if (!listening) return;
    listening = false;
    for (const [target, type, fn] of LISTENERS) target.removeEventListener(type, fn, { capture: true });
    if (window.visualViewport) {
      window.visualViewport.removeEventListener("resize", invalidate);
      window.visualViewport.removeEventListener("scroll", invalidate);
    }
  }

  // --------------------------------------------------------------- start/stop

  function start() {
    stop();
    active = true;
    build();
    listen();
    browser.storage.local.get({ settings }).then((got) => {
      settings = { format: "hex", lowercase: false, ...(got.settings || {}) };
      if (!Color.FORMATS.includes(settings.format)) settings.format = "hex";
      if (active && !flash) requestDraw();
    }, () => {});
    gen++;
    refresh();
  }

  function stop() {
    unlisten();
    clearTimeout(refreshTimer);
    clearTimeout(closeTimer);
    clearTimeout(hintTimer);
    clearTimeout(flashTimer);
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    if (ui) ui.host.remove();
    if (shot) shot.canvas.width = 0;
    ui = null;
    shot = null;
    pos = null;
    pendingPick = null;
    flash = null;
    hinted = false;
    active = false;
    closing = false;
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.type !== "pipette:start") return undefined;
    if (!(document.documentElement instanceof HTMLElement)) {
      return Promise.resolve({ ok: false, error: "unsupported" }); // a bare SVG or XML document
    }
    if (active && !closing) {
      if (msg.toggle) stop(); // the shortcut again switches the picker off
      return Promise.resolve({ ok: true });
    }
    start();
    return Promise.resolve({ ok: true });
  });
})();
