// Color maths shared by the background, the popup, the options page and the
// in-page picker. A classic script (no modules) so scripting.executeScript can
// load it into a page next to content/picker.js.
(function (root) {
  "use strict";

  const HEX_RE = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;

  /** "#abc" / "abc" / "#aabbcc" -> {r, g, b}, or null when it is not a color. */
  function parseHex(text) {
    const m = HEX_RE.exec(String(text == null ? "" : text).trim());
    if (!m) return null;
    let h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    const n = parseInt(h, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  /** {r, g, b} -> canonical "#RRGGBB" (upper case: the form kept in storage). */
  function toHex(c) {
    const n = ((c.r & 255) << 16) | ((c.g & 255) << 8) | (c.b & 255);
    return "#" + n.toString(16).padStart(6, "0").toUpperCase();
  }

  /** Any accepted hex spelling -> canonical "#RRGGBB", or null. */
  function normalizeHex(text) {
    const c = parseHex(text);
    return c ? toHex(c) : null;
  }

  /** {r, g, b} -> {h: 0..359, s: 0..100, l: 0..100}, rounded like CSS authors write it. */
  function toHsl(c) {
    const r = c.r / 255, g = c.g / 255, b = c.b / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const l = (max + min) / 2;
    const d = max - min;
    let h = 0, s = 0;
    if (d !== 0) {
      s = d / (1 - Math.abs(2 * l - 1));
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
      if (h < 0) h += 360;
    }
    return { h: Math.round(h) % 360, s: Math.round(s * 100), l: Math.round(l * 100) };
  }

  const FORMATS = ["hex", "rgb", "hsl"];

  /** Text that goes on the clipboard for a color in the given format. */
  function format(color, fmt, opts) {
    const c = typeof color === "string" ? parseHex(color) : color;
    if (!c) return "";
    if (fmt === "rgb") return `rgb(${c.r}, ${c.g}, ${c.b})`;
    if (fmt === "hsl") {
      const k = toHsl(c);
      return `hsl(${k.h}, ${k.s}%, ${k.l}%)`;
    }
    const hex = toHex(c);
    return opts && opts.lowercase ? hex.toLowerCase() : hex;
  }

  /** WCAG 2.x relative luminance of an sRGB color, 0 (black) to 1 (white). */
  function luminance(c) {
    const lin = (v) => {
      const s = v / 255;
      return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  }

  /** WCAG contrast ratio between two colors, 1 to 21. */
  function contrast(a, b) {
    const la = luminance(a), lb = luminance(b);
    const hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }

  /**
   * Pass/fail for the four WCAG 2.x text levels. The ratio is compared
   * unrounded, and shown cut (never rounded up) to two decimals, so a 4.499
   * can not read as a passing 4.5.
   */
  function wcag(a, b) {
    const ratio = contrast(a, b);
    return {
      ratio,
      text: (Math.floor(ratio * 100 + 1e-9) / 100).toFixed(2) + ":1",
      aaNormal: ratio >= 4.5,
      aaLarge: ratio >= 3,
      aaaNormal: ratio >= 7,
      aaaLarge: ratio >= 4.5,
    };
  }

  /** Black or white, whichever reads better on top of the color. */
  function inkOn(color) {
    const c = typeof color === "string" ? parseHex(color) : color;
    if (!c) return "#000000";
    return contrast(c, { r: 0, g: 0, b: 0 }) >= contrast(c, { r: 255, g: 255, b: 255 }) ? "#000000" : "#FFFFFF";
  }

  root.PipetteColor = { FORMATS, parseHex, toHex, normalizeHex, toHsl, format, luminance, contrast, wcag, inkOn };
})(globalThis);
