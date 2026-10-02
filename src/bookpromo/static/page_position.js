// Keep position across POST/redirect and background-job reloads. Store only DOM
// identifiers and viewport coordinates, never form values, prompts or book text.
(() => {
  const main = document.getElementById("content");
  if (!main) return;
  const storageKey = `bookpromo:page-position:${window.location.pathname}`;
  const anchorSelector = "[data-page-position-anchor]";
  const counts = new Map();
  for (const element of main.querySelectorAll("form, button, a, input, select, textarea, summary, details")) {
    const anchor = element.closest(anchorSelector)?.dataset.pagePositionAnchor || "page";
    const form = element.closest("form");
    const action = form ? new URL(form.action, window.location.href).pathname : "";
    const base = JSON.stringify([anchor, action, element.tagName, element.id || element.name || ""]);
    const index = counts.get(base) || 0;
    counts.set(base, index + 1);
    element.dataset.pagePositionKey = `${base}:${index}`;
  }
  const find = key => key ? [...main.querySelectorAll("[data-page-position-key]")]
    .find(element => element.dataset.pagePositionKey === key) : null;
  let submitted = null;
  let restoreAfterAction = false;

  function snapshot() {
    let target = submitted || document.activeElement;
    // The editor is closed after a redirect: return to its opener, not a hidden
    // Save button. The same applies to the image lightbox.
    const dialog = target?.closest?.("dialog");
    if (dialog) {
      target = find(dialog.dataset.pagePositionOpenerKey) ||
        [...main.querySelectorAll("[data-teaser-dialog-open]")]
          .find(button => button.dataset.teaserDialogOpen === dialog.id);
    }
    const rect = target?.getBoundingClientRect?.();
    const visible = rect && rect.bottom > 0 && rect.top < window.innerHeight;
    const anchor = visible ? target.closest(anchorSelector) : null;
    const form = visible ? target.closest("form") : null;
    const state = {
      time: Date.now(), restoreAfterAction,
      x: window.scrollX, y: window.scrollY,
      target: visible ? target.dataset.pagePositionKey : null,
      targetTop: visible ? rect.top : null,
      anchor: anchor?.dataset.pagePositionAnchor || null,
      anchorTop: anchor?.getBoundingClientRect().top ?? null,
      form: form?.dataset.pagePositionKey || null,
      formTop: form?.getBoundingClientRect().top ?? null,
      details: [...main.querySelectorAll("details[open]")]
        .map(element => element.dataset.pagePositionKey),
    };
    try { window.sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch { /* Storage may be disabled. */ }
  }

  document.addEventListener("submit", event => {
    if (!main.contains(event.target)) return;
    const action = new URL(event.target.action, window.location.href);
    if (action.origin !== window.location.origin) return;
    submitted = event.submitter || event.target.querySelector('button[type="submit"], button:not([type])');
    restoreAfterAction = true;
    snapshot();
  }, true);
  document.addEventListener("click", event => {
    const opener = event.target.closest?.("[data-teaser-image-open], [data-teaser-dialog-open]");
    if (opener && main.contains(opener)) {
      const dialog = document.getElementById(opener.dataset.teaserDialogOpen || "chapter-image-lightbox");
      if (dialog) dialog.dataset.pagePositionOpenerKey = opener.dataset.pagePositionKey;
    }
    const link = event.target.closest?.("a[href]");
    if (!link || !main.contains(link) || link.download || link.target === "_blank" ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const destination = new URL(link.href, window.location.href);
    if (destination.origin !== window.location.origin ||
        destination.pathname !== window.location.pathname || destination.hash) return;
    submitted = link;
    restoreAfterAction = true;
    snapshot();
  }, true);
  window.addEventListener("pagehide", snapshot);

  window.addEventListener("pageshow", event => {
    let state;
    try {
      state = JSON.parse(window.sessionStorage.getItem(storageKey));
      window.sessionStorage.removeItem(storageKey); // One-shot; do not jump on later visits.
    } catch { return; }
    const navigation = window.performance?.getEntriesByType("navigation")[0]?.type;
    if (!state || event.persisted || navigation === "back_forward" ||
        (window.location.hash && !state.restoreAfterAction) ||
        Date.now() - state.time > 120000 ||
        (!state.restoreAfterAction && navigation !== "reload") ||
        !Number.isFinite(state.x) || !Number.isFinite(state.y)) return;
    for (const key of state.details || []) {
      const details = find(key);
      if (details?.tagName === "DETAILS") details.open = true;
    }
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const target = find(state.target);
      const anchor = [...main.querySelectorAll(anchorSelector)]
        .find(element => element.dataset.pagePositionAnchor === state.anchor);
      const form = find(state.form);
      const visible = element => element && !element.closest("dialog:not([open])") &&
        element.getClientRects().length > 0;
      const fallback = visible(anchor) ? anchor : (visible(form) ? form : null);
      const reference = visible(target) ? target : fallback;
      const top = reference === target ? state.targetTop :
        (reference === anchor ? state.anchorTop : state.formTop);
      const y = reference && Number.isFinite(top)
        ? window.scrollY + reference.getBoundingClientRect().top - top : state.y;
      window.scrollTo({left: state.x, top: Math.max(0, y), behavior: "instant"});
      const focus = visible(target) && !target.disabled ? target : fallback;
      if (focus) {
        if (focus === fallback) focus.setAttribute("tabindex", "-1");
        focus.focus({preventScroll: true});
      }
    }));
  });
})();
