// UI-only protection. Form values stay in memory, never in browser storage.
(() => {
  const states = new Map();
  let allowNavigation = false;
  let refreshPending = false;
  let banner;
  const capture = form => JSON.stringify([...form.elements]
    .filter(input => input.name && !["hidden", "submit", "button", "reset"].includes(input.type))
    .map(input => [input.name, input.type, input.type === "file"
      ? [...input.files].map(file => [file.name, file.size, file.lastModified])
      : input.type === "checkbox" || input.type === "radio" ? [input.value, input.checked]
      : input.multiple ? [...input.selectedOptions].map(option => option.value) : input.value]));

  function refresh(form) {
    const state = states.get(form);
    if (!state) return;
    state.dirty = capture(form) !== state.baseline;
    state.notice.hidden = !state.dirty;
    if (form.hasAttribute("data-reel-action")) {
      if (state.dirty) form.dataset.reelDirty = "true";
      else delete form.dataset.reelDirty;
    }
  }

  function hasChanges(root = document, except = null) {
    let dirty = false;
    for (const [form] of states) {
      if (!form.isConnected) { states.delete(form); continue; }
      refresh(form);
      if (form !== except && root.contains(form) && states.get(form).dirty) dirty = true;
    }
    return dirty;
  }

  function render() {
    const dirty = hasChanges();
    if (!banner && (dirty || refreshPending)) {
      banner = document.createElement("div");
      banner.className = "notice unsaved-banner";
      banner.setAttribute("role", "status");
      const message = document.createElement("span");
      message.dataset.unsavedMessage = "";
      banner.append(message);
      const reload = document.createElement("button");
      reload.type = "button";
      reload.className = "secondary-button";
      reload.textContent = "Anzeige aktualisieren";
      reload.addEventListener("click", () => {
        if (!hasChanges() || confirmDiscard()) {
          allowNavigation = true;
          window.location.reload();
        }
      });
      banner.append(reload);
      document.getElementById("content")?.prepend(banner);
    }
    if (!banner) return;
    banner.hidden = !dirty && !refreshPending;
    banner.querySelector("[data-unsaved-message]").textContent = refreshPending
      ? "Der Hintergrundstatus hat sich geändert. Bitte offene Eingaben speichern, bevor du die Anzeige aktualisierst."
      : "Noch nicht gespeichert · Bereichswechsel behalten deine Eingaben. Vor dem Verlassen bitte speichern.";
    banner.querySelector("button").hidden = !refreshPending;
  }

  function track(root = document) {
    for (const form of root.querySelectorAll("form")) {
      if (states.has(form) || !(form.hasAttribute("data-unsaved-form") ||
          form.hasAttribute("data-reel-action") || form.querySelector('input[type="file"]'))) continue;
      let notice = form.querySelector("[data-unsaved-status]");
      if (!notice) {
        notice = document.createElement("p");
        notice.dataset.unsavedStatus = "";
        notice.className = "unsaved-status";
        notice.textContent = "Noch nicht gespeichert";
        notice.setAttribute("role", "status");
        notice.hidden = true;
        form.append(notice);
      }
      states.set(form, {baseline: capture(form), dirty: false, notice});
    }
  }

  function accept(form, baseline = capture(form)) {
    if (states.has(form)) states.get(form).baseline = baseline;
    refresh(form);
    render();
  }

  function confirmDiscard(root = document) {
    return !hasChanges(root) || window.confirm("Es gibt noch nicht gespeicherte Eingaben. Änderungen verwerfen und fortfahren?");
  }

  function forget(root) {
    for (const [form] of states) if (root.contains(form)) states.delete(form);
    render();
  }

  document.addEventListener("input", event => queueMicrotask(() => { refresh(event.target.form); render(); }));
  document.addEventListener("change", event => queueMicrotask(() => { refresh(event.target.form); render(); }));
  document.addEventListener("reset", () => queueMicrotask(render));
  document.addEventListener("submit", event => {
    const form = event.target;
    // The workshop saves via fetch and keeps other forms on screen.
    if (form.hasAttribute("data-reel-action") || form.method === "dialog") return;
    if (hasChanges(document, form) && !confirmDiscardOtherForms(form)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    allowNavigation = true;
    queueMicrotask(() => { if (event.defaultPrevented) allowNavigation = false; });
  }, true);
  function confirmDiscardOtherForms(form) {
    return window.confirm("Andere Bereiche enthalten noch nicht gespeicherte Eingaben. Diese werden bei dieser Aktion nicht gespeichert. Trotzdem fortfahren?");
  }
  window.addEventListener("beforeunload", event => {
    if (!allowNavigation && hasChanges()) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pageshow", () => { allowNavigation = false; });

  window.bookpromoUnsaved = {
    track, capture, accept, hasChanges, confirmDiscard, forget,
    refresh: form => { refresh(form); render(); },
    reload: () => {
      if (!hasChanges()) { window.location.reload(); return true; }
      refreshPending = true;
      render();
      return false;
    },
  };
  track();
})();
