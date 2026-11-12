# Pipette: Color Picker & Eyedropper

Firefox add-on (desktop) that picks any pixel on a page with a zoomed loupe,
copies it as HEX, RGB or HSL, keeps a history and checks WCAG contrast between
two picked colors. Free, every feature unlocked, no ads, no tracking, optional
Ko-fi tip.

Why this one: people search AMO for a color picker a lot, and the choice is thin.
ColorZilla has 133k daily users, Colorpick Eyedropper 17k (last update 2023),
Color Picker 12k at 2.4 stars. Pipette uses only `tabs.captureVisibleTab` and a
canvas, so there are no site-specific parts to break.

Separate from YouTube/Twitch Enhancer on purpose: YTE only runs on youtube.com
and twitch.tv and has no color tools, and a color picker has to work everywhere.

Desktop only: see [Android](#android).

## Features

- **Loupe**: an 11 x 11 pixel magnifier follows the cursor; the framed middle cell
  is the pixel that gets picked, and the label under it shows the color in the
  chosen format.
- **Copy**: a click copies the color and shows "Copied" at that spot. Shift+click
  keeps the picker open for the next color.
- **Keyboard**: `Alt+Shift+C` starts (and stops) picking; arrow keys move one
  screen pixel (ten with Shift), Enter picks, Esc or a right-click cancels.
- **Formats**: HEX, RGB, HSL. The default is set in the popup or the options
  page; the popup always offers all three. Optional lower-case HEX.
- **History**: the last 30 colors, newest first, no repeats. Clicking one copies
  it again.
- **Contrast check**: WCAG 2 ratio between two colors with pass/fail for AA and
  AAA at normal and large text sizes and a live sample. It follows the last two
  picks (text first, then background) until you put a color in a slot yourself.

## How the picking works

Firefox has no `EyeDropper` API, so the picker reads from a screenshot.

1. The popup button or the shortcut makes the background inject
   `content/picker.js` into the tab (`scripting.executeScript`, top frame only).
2. The picker asks the background for a picture. The background calls
   `tabs.captureVisibleTab` with `scale = devicePixelRatio / tab zoom`. Firefox
   multiplies the scale by the tab zoom itself, so the image has exactly
   `devicePixelRatio` pixels per CSS pixel: the page's real device pixels at any
   zoom level and on any screen. The cursor is then mapped with
   `floor(clientX * devicePixelRatio)`.
3. The PNG is decoded by hand (base64 to bytes to `createImageBitmap`) into a
   canvas that never enters the DOM. Colors are read one pixel at a time.
4. Anything that moves the page under the picture (scroll, resize, zoom, coming
   back to the tab) throws the picture away and takes a new one. The overlay is
   hidden for that moment so the loupe is never in its own screenshot. A click
   that lands in between waits for the new picture.

Things that were measured in Firefox 147 while building it:

- `captureVisibleTab` works with `activeTab` alone (Firefox 126+), and the
  permission survives switching to another tab and back.
- Without an explicit scale Firefox renders at `window DPR * zoom`, which is not
  the page's real pixel grid whenever `60 / scale` is not a whole number (110%,
  133%, 175% screens...). The image is then a slightly different size than the
  screen. Asking for `devicePixelRatio / zoom` avoids that.
- With pinch zoom (touchpad, touch screen) the capture is still the whole layout
  viewport and mouse coordinates are still layout coordinates, so nothing
  special is needed.
- Picks stay exact with `privacy.resistFingerprinting` on (which scrambles a
  page's own canvas reads and lies about the screen) and with canvas
  randomization on. The picker does not need `devicePixelRatio` to be the real
  screen scale: it only needs the image to be `devicePixelRatio` times the
  viewport, and that is what it asks for.
- `layout.css.devPixelsPerPx = 1.6` really gives 1.579 (Firefox snaps to
  `60 / whole number`). The screenshot script uses 1.5, which is exact.

## Privacy and permissions

- `activeTab`, `scripting`, `storage`. No host permissions, so Firefox shows no
  permission prompt at install and the add-on can not see any page until the
  user presses its button or shortcut there.
- The screenshot lives in memory in the tab's content script while picking and
  is dropped afterwards. The background refuses to capture for a tab that is
  not in front, and drops a capture if the front tab changed meanwhile.
- The overlay sits in a closed shadow root (the page can not read the loupe) and
  is styled only through the CSSOM, so it also works under a strict
  Content-Security-Policy. It is shown as a popover (top layer), so it is above
  any z-index. Its listeners sit on the window, so it still works when a page's
  modal dialog makes everything else inert.
- Copying uses the click's own user gesture (`execCommand("copy")` with a copy
  listener, then `navigator.clipboard` if a page swallows copy events), so no
  `clipboardWrite` permission is needed and it works on http pages too.
- `data_collection_permissions: none`. No network requests at all.

## Android

Not listed for Firefox for Android in 0.1.0, and the manifest has no
`gecko_android` block:

- `tabs.captureVisibleTab` exists there (MDN: since Firefox 54), but `commands`
  (the shortcut) and `tabs.getZoom` do not.
- The loupe follows a mouse pointer. Touch needs its own design (drag with an
  offset loupe, pick on release).
- There was no Android device or emulator on the build machine, so none of it
  could be tested. An untested listing would be a bad first impression.

## Layout

```
src/                       everything that ships
  manifest.json
  background.js            starts the picker, takes the screenshot, saves history
  content/picker.js        the in-page loupe (injected on demand)
  popup/                   toolbar popup: pick button, formats, history, contrast
  options/                 copy format, shortcut help, history, support
  shared/color.js          hex/rgb/hsl and WCAG maths (classic script, used everywhere)
  shared/support.*         Ko-fi pill + one-time milestone card (from the template)
store-assets/listing.json  AMO listing text, category, Ko-fi contributions_url
store-assets/screenshots/  1280x800 listing images (made by tests/screenshots.py)
tests/                     real-Firefox E2E, test pages, screenshot generator
tools/release.py           build / lint / submit / status / contrib / screenshots
```

Storage keys (`browser.storage.local`): `history` (up to 30 `#RRGGBB` strings,
newest first), `settings` (`format`, `lowercase`), `contrast` (`fg`, `bg`, both
null while it follows the latest picks), `stats` (`picks`, for the milestone
card), `supportNudge`.

## Develop and test

```powershell
python tools/release.py lint                 # build + web-ext lint the zip (expect 0/0/0)
python -m pip install selenium pillow        # once, ideally in a venv, for the tests
python tests/e2e_firefox.py                  # 187 checks in headless Firefox, about 4 min
python tests/e2e_firefox.py main             # one session only: main dpr2 dpr125 dpr175 rfp fpp
python tests/screenshots.py                  # regenerate store screenshots
```

The E2E installs the extracted built zip and picks pixels of known color on
`tests/test-page.html`. Because the add-on only has `activeTab`, the test goes
through Firefox's own UI to get it (geckodriver `--allow-system-access`): it
clicks the real toolbar button, clicks Pick inside the real popup panel, and
presses the real shortcut. Nothing is granted by hand. The clipboard is read
back from Firefox itself; headless Firefox has its own clipboard, so the test
does not touch yours.

What it covers: block colors and 1px stripes at 100%, on 125%, 175% and 2x
screens, at page zoom 50% to 300% and with pinch zoom; the edges of a fixed box
at the far corner (catches any drift in scale); scrolling down, sideways and
inside a scroll box with the picker open, including a click right after the
scroll; a half-pixel scroll offset; a cross-origin frame; the loupe not showing
up in its own screenshot; page hover styles staying out; arrow keys, Enter,
Esc, right-click, the shortcut as a toggle; the page seeing no mouse events;
formats and lower-case HEX; history order; contrast numbers against a second
implementation of the WCAG formula; the milestone card and its opt-out; a
strict CSP page, a page that blocks copy events, a plain http page, a page
add-ons can not enter; `privacy.resistFingerprinting` and canvas randomization.

Manual: `about:debugging#/runtime/this-firefox` → Load Temporary Add-on → `src/manifest.json`.

## Known limits

- The picture is a still. On a playing video or an animation the color is the
  one from the frame that was captured, which is the frame the loupe shows.
- Scrolling by wheel moves the page, not a scroll box under the cursor (the
  overlay takes the pointer). Scroll the box first, or use its scrollbar.
- Standalone SVG or XML documents are not supported (the popup says so).

## Release

Submitted to AMO on 2026-11-10: add-on id 3124270, slug `pipette-color-picker`,
version 0.1.0, waiting for review. Screenshots, captions and the Ko-fi
Contribute link are on the listing.

```powershell
python tools/release.py status               # review state
python tools/release.py submit --notes "What changed"   # later versions
```

Bump `version` in `src/manifest.json` for every later submit.

AMO reads the listing description as Markdown and shows HTML tags as plain
text, so `store-assets/listing.json` uses `**Heading**`, not `<b>Heading</b>`.
`release.py` only sends the description when it creates the listing; a later
text change needs a PATCH of `description` on `/addons/addon/<gecko id>/`.
