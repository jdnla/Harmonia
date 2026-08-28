/* Harmonia D-pad spatial navigation.
 *
 * The media center's remote injects real arrow-key events (via ydotool/CEC).
 * Many sites lack 2-D spatial navigation, so this content script adds it:
 * arrow keys move an on-screen highlight to the nearest focusable element in the
 * pressed direction; Enter/OK clicks the focused element; Back/Esc is left to
 * the site/app. It draws its own focus ring (site :focus styles are often
 * hidden) and cooperates with whatever the page already does — if the site
 * itself handles an arrow key (e.g. Netflix's own player), we can defer.
 *
 * Design notes:
 *  - Pure content-script; no bridge changes. It just consumes the arrow keys
 *    that are already delivered to the focused window.
 *  - Candidate set = visible, in-viewport, interactive elements (links,
 *    buttons, [tabindex], role=button, video controls, site-specific tiles).
 *  - Scoring = classic TV spatial-nav: strongly prefer elements in the pressed
 *    direction, penalize lateral offset, then distance. Ported from the pattern
 *    used by smart-TV spatial-navigation polyfills.
 *  - Site profiles tune the candidate selector + whether to defer to the site.
 */
(function () {
  "use strict";

  if (window.__harmoniaDpadLoaded) return;      // guard against double-inject
  window.__harmoniaDpadLoaded = true;

  const DIR = { 37: "left", 38: "up", 39: "right", 40: "down" };
  const ENTER_KEYS = new Set([13]);             // Enter / OK

  // ---- site profiles -------------------------------------------------------
  const HOST = location.hostname;
  function profileFor(host) {
    if (/netflix\.com$/.test(host)) {
      return {
        name: "netflix",
        // Netflix browse: title cards, jaw buttons, nav. The player UI is a
        // DRM/protected context we mostly can't touch, so if the video element
        // is fullscreen+playing we DEFER arrows to Netflix's own handler.
        extraSelectors: [
          ".title-card a", ".slider-refocus", ".playLink",
          ".billboard-links a", "[data-uia='nav-'] a",
          ".galleryLockups a", ".titleCard--container a"
        ],
        deferWhenPlayer: true
      };
    }
    if (/sportsnet\.ca$/.test(host) || /tsn\.ca$/.test(host)) {
      return {
        name: "sportsnet",
        extraSelectors: [
          ".video-tile a", ".card a", ".vjs-control", ".player-controls button",
          "article a", ".headline a"
        ],
        deferWhenPlayer: false
      };
    }
    return { name: "generic", extraSelectors: [], deferWhenPlayer: false };
  }
  const PROFILE = profileFor(HOST);

  const BASE_SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea",
    "[tabindex]:not([tabindex='-1'])", "[role='button']", "[role='link']",
    "[role='menuitem']", "[role='option']", "video"
  ].concat(PROFILE.extraSelectors).join(",");

  // ---- focus ring ----------------------------------------------------------
  let ring = null;
  function ensureRing() {
    if (ring && document.body.contains(ring)) return ring;
    ring = document.getElementById("harmonia-dpad-ring");
    if (!ring) {
      ring = document.createElement("div");
      ring.id = "harmonia-dpad-ring";
      (document.body || document.documentElement).appendChild(ring);
    }
    return ring;
  }
  function drawRing(el) {
    const r = el.getBoundingClientRect();
    const ring = ensureRing();
    ring.style.top = `${Math.max(0, r.top - 3)}px`;
    ring.style.left = `${Math.max(0, r.left - 3)}px`;
    ring.style.width = `${r.width + 6}px`;
    ring.style.height = `${r.height + 6}px`;
    ring.classList.add("harmonia-visible");
  }
  function hideRing() {
    if (ring) ring.classList.remove("harmonia-visible");
  }

  // ---- candidate discovery -------------------------------------------------
  let current = null;

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) return false;
    if (r.bottom < 0 || r.top > innerHeight) return false;
    if (r.right < 0 || r.left > innerWidth) return false;
    const s = getComputedStyle(el);
    if (s.visibility === "hidden" || s.display === "none" || s.opacity === "0")
      return false;
    if (el.disabled) return false;
    return true;
  }

  function candidates() {
    const out = [];
    document.querySelectorAll(BASE_SELECTOR).forEach((el) => {
      if (visible(el)) out.push(el);
    });
    return out;
  }

  function center(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, r };
  }

  // Directional scoring: candidate must be in the pressed direction; score
  // = primary-axis distance + weighted lateral offset. Lower = better.
  function pick(dir, from) {
    const src = from ? center(from) : { x: innerWidth / 2, y: innerHeight / 2 };
    let best = null, bestScore = Infinity;
    for (const el of candidates()) {
      if (el === from) continue;
      const c = center(el);
      const dx = c.x - src.x, dy = c.y - src.y;
      let primary, lateral, inDir;
      switch (dir) {
        case "left":  inDir = dx < -4; primary = -dx; lateral = Math.abs(dy); break;
        case "right": inDir = dx > 4;  primary = dx;  lateral = Math.abs(dy); break;
        case "up":    inDir = dy < -4; primary = -dy; lateral = Math.abs(dx); break;
        case "down":  inDir = dy > 4;  primary = dy;  lateral = Math.abs(dx); break;
      }
      if (!inDir) continue;
      // Lateral offset weighted heavily so we stay in the same row/column.
      const score = primary + lateral * 2;
      if (score < bestScore) { bestScore = score; best = el; }
    }
    return best;
  }

  function focus(el) {
    if (!el) return;
    current = el;
    try { el.focus({ preventScroll: false }); } catch (e) {}
    // Ensure it's on-screen, then draw the ring after any scroll settles.
    el.scrollIntoView({ block: "nearest", inline: "nearest" });
    requestAnimationFrame(() => drawRing(el));
  }

  // Is a video player actively in the foreground (for Netflix deferral)?
  function playerActive() {
    const v = document.querySelector("video");
    if (!v) return false;
    const r = v.getBoundingClientRect();
    const big = r.width > innerWidth * 0.7 && r.height > innerHeight * 0.7;
    return big && !v.paused;
  }

  // ---- key handling --------------------------------------------------------
  function onKeyDown(e) {
    // Ignore when typing in a field.
    const t = e.target;
    if (t && (t.isContentEditable ||
        /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;

    if (ENTER_KEYS.has(e.keyCode)) {
      if (current && document.body.contains(current)) {
        e.preventDefault();
        current.click();
      }
      return;
    }

    const dir = DIR[e.keyCode];
    if (!dir) return;

    // Netflix: while its protected player is up and playing, let Netflix own
    // the arrows (scrub/skip) rather than fighting it.
    if (PROFILE.deferWhenPlayer && playerActive()) {
      hideRing();
      current = null;
      return;
    }

    // If our current focus is stale/offscreen, restart from viewport center.
    if (current && !visible(current)) current = null;

    const next = pick(dir, current);
    if (next) {
      e.preventDefault();
      e.stopPropagation();
      focus(next);
    }
    // If nothing found in that direction, let the event through (site may act).
  }

  // Keep the ring glued to the focused element on scroll/resize.
  function reposition() {
    if (current && document.body.contains(current) && visible(current)) {
      drawRing(current);
    } else {
      hideRing();
    }
  }

  // Capture phase so we see keys before the site does (but we only preventDefault
  // when we actually move, so sites keep keys we don't use).
  window.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("scroll", reposition, true);
  window.addEventListener("resize", reposition, true);

  // Seed focus on the first meaningful element once the page settles.
  function seed() {
    if (current) return;
    const first = pick("down", null) || pick("right", null) || candidates()[0];
    if (first) focus(first);
  }
  setTimeout(seed, 800);

  console.info("[Harmonia] D-pad spatial navigation active:", PROFILE.name);
})();
