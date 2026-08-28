/* Harmonia D-pad extension — background service worker.
 *
 * Minimal: the extension's real work is the content script (spatial_nav.js).
 * This worker exists so the extension has a stable presence (and a place for
 * any future cross-tab logic). It logs load so `--load-extension` success is
 * easy to confirm. */
chrome.runtime.onInstalled.addListener(() => {
  console.info("[Harmonia] D-pad extension installed/loaded");
});
