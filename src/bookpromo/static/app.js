// Sections are progressively enhanced: without JavaScript every form and
// action remains visible. Values stay enabled so a shared save keeps all fields.
const quoteProduction = document.querySelector('[data-quote-production-active="true"]');
if (quoteProduction) {
  const refreshQuoteProduction = () => {
    const dialog = document.getElementById('reel-dialog');
    if (dialog?.open) {
      window.setTimeout(refreshQuoteProduction, 4000);
      return;
    }
    window.location.reload();
  };
  window.setTimeout(refreshQuoteProduction, 4000);
}

function wireSimpleImageForms(scope) {
  for (const form of scope.querySelectorAll('[data-simple-image-form]')) {
    const cast = [...form.querySelectorAll('[name="character_ids"]')];
    const referenceButton = form.querySelector('[data-simple-image-reference]');
    const hint = form.querySelector('[data-simple-image-hint]');
    const provider = form.querySelector('[name="ai_provider"]');
    const providerLabel = form.querySelector('[data-simple-image-provider-label]');
    const syncProvider = () => {
      if (providerLabel && provider) providerLabel.textContent = provider.selectedOptions[0]?.textContent || 'Keine Text-KI eingerichtet';
    };
    provider?.addEventListener('change', syncProvider);
    syncProvider();
    form.addEventListener('invalid', event => {
      // Chapter scene fields are compact by default; expose invalid fields.
      for (let parent = event.target.parentElement; parent && parent !== form; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    }, true);
    const busy = form.dataset.simpleImageBusy === 'true';
    const syncCast = () => {
      const selected = cast.filter(input => input.checked);
      const tooMany = selected.length > 4;
      const missing = selected.some(input => input.dataset.hasReference !== 'true');
      if (referenceButton) referenceButton.disabled = busy || form.dataset.simpleImageSubmitting === 'true' || tooMany || !selected.length || missing;
      for (const input of cast) input.setCustomValidity(tooMany ? 'Bitte höchstens vier sichtbare Figuren auswählen.' : '');
      if (hint) {
        hint.textContent = tooMany ? 'Bitte höchstens vier sichtbare Figuren auswählen.'
          : missing ? 'Für den Referenzmodus braucht jede ausgewählte Figur ein Referenzbild. Ohne Referenzen kannst du „Bild mit Beschreibung erzeugen“ nutzen.'
          : !selected.length ? 'Ohne Charakterauswahl nutzt „Bild erzeugen“ nur die Szene. Für den Referenzmodus mindestens eine Figur auswählen.' : '';
        hint.hidden = !hint.textContent;
      }
    };
    for (const input of cast) input.addEventListener('change', syncCast);
    syncCast();
  }
}
wireSimpleImageForms(document);

for (const panel of document.querySelectorAll('[data-image-preset-check]')) {
  const button = panel.querySelector('button');
  const message = panel.querySelector('[role="status"]');
  button.addEventListener('click', async () => {
    button.disabled = true;
    message.hidden = false;
    message.textContent = 'Bild-Presets werden geprüft …';
    try {
      const response = await fetch(panel.dataset.checkUrl, {method: 'POST'});
      const result = await response.json();
      message.textContent = result.message || 'Prüfung fehlgeschlagen.';
    } catch { message.textContent = 'ComfyUI-Prüfung konnte nicht abgeschlossen werden.'; }
    finally { button.disabled = false; }
  });
}

for (const group of document.querySelectorAll("[data-settings-group]")) {
  const sections = [...group.querySelectorAll("[data-settings-section]")];
  const links = [...group.querySelectorAll("[data-settings-link]")];
  const key = `bookpromo-section:${location.pathname}`;
  const known = new Set(sections.map(section => section.dataset.settingsSection));
  const sectionForHash = () => {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return null; }
    const target = document.getElementById(id);
    if (!target || !group.contains(target)) return null;
    return target.closest("[data-settings-section]")?.dataset.settingsSection || null;
  };
  const readSelection = () => { try { return sessionStorage.getItem(key); } catch { return null; } };
  let current = null;
  const select = name => {
    if (!known.has(name) && name !== "all") name = group.dataset.settingsDefault || sections[0]?.dataset.settingsSection;
    current = name;
    sections.forEach(section => { section.hidden = name !== "all" && section.dataset.settingsSection !== name; });
    links.forEach(link => {
      const target = document.getElementById(link.hash.slice(1));
      const selected = target?.closest("[data-settings-section]")?.dataset.settingsSection === name;
      if (selected) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-workspace-settings]").forEach(link => {
      if (!location.pathname.endsWith("/settings")) return;
      const parentSection = ["book-data", "art-direction"].includes(name) ? "profile" : name === "characters" ? "media" : name;
      if (link.dataset.workspaceSettings === parentSection) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-workspace-view]").forEach(link => {
      if (new URL(link.href).pathname !== location.pathname) return;
      const selected = link.dataset.workspaceView === name || (link.dataset.workspaceView === "chapters" && name === "analysis");
      if (selected) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    try { sessionStorage.setItem(key, name); } catch { /* restricted storage */ }
  };
  const showTarget = () => {
    const name = sectionForHash();
    if (!name) return;
    select(name);
    const target = document.getElementById(location.hash.slice(1));
    if (target instanceof HTMLDetailsElement) target.open = true;
  };
  const errorSection = sections.find(section => section.querySelector('[role="alert"], .notice.error'));
  select(sectionForHash() || errorSection?.dataset.settingsSection || readSelection() || group.dataset.settingsDefault || sections[0]?.dataset.settingsSection);
  if (sectionForHash()) requestAnimationFrame(() => { showTarget(); document.getElementById(location.hash.slice(1))?.scrollIntoView({block: "start"}); });
  window.addEventListener("hashchange", showTarget);
  for (const link of links) link.addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const target = document.getElementById(link.hash.slice(1));
    const name = target?.closest("[data-settings-section]")?.dataset.settingsSection;
    if (name) { select(name); if (target instanceof HTMLDetailsElement) target.open = true; }
  });
  for (const link of document.querySelectorAll("[data-workspace-settings]")) link.addEventListener("click", () => {
    if (new URL(link.href).pathname === location.pathname) select(link.dataset.workspaceSettings);
  });
  group.addEventListener("invalid", event => {
    const section = event.target.closest("[data-settings-section]");
    const invalidSections = new Set([...event.target.form?.querySelectorAll("input:invalid, select:invalid, textarea:invalid") || []]
      .map(input => input.closest("[data-settings-section]")?.dataset.settingsSection).filter(Boolean));
    if (invalidSections.size > 1) select("all");
    else if (section?.hidden) select(section.dataset.settingsSection);
    let ancestor = event.target.parentElement;
    while (ancestor && ancestor !== group) { if (ancestor instanceof HTMLDetailsElement) ancestor.open = true; ancestor = ancestor.parentElement; }
  }, true);
  // Always provide an escape hatch for cross-section review.
  const nav = group.querySelector(".settings-nav");
  if (nav) {
    const all = document.createElement("button");
    all.type = "button";
    all.className = "secondary-button";
    all.textContent = "Alles anzeigen";
    all.addEventListener("click", () => select("all"));
    nav.append(all);
  }
}

for (const cover of document.querySelectorAll("[data-book-cover]")) {
  const fallback = () => { cover.hidden = true; };
  cover.addEventListener("error", fallback);
  if (cover.complete && !cover.naturalWidth) fallback();
}

const libraryTools = document.querySelector("[data-library-tools]");
if (libraryTools) {
  libraryTools.hidden = false;
  const search = libraryTools.querySelector("[data-library-search]");
  const filter = libraryTools.querySelector("[data-library-filter]");
  const items = [...document.querySelectorAll("[data-library-item]")];
  const update = () => {
    const query = search.value.toLocaleLowerCase("de").trim();
    for (const item of items) {
      const matches = filter.value === "all" || item.dataset.libraryKind === filter.value || (filter.value === "promotion" && item.dataset.libraryPromotion === "yes");
      item.hidden = !matches || !item.textContent.toLocaleLowerCase("de").includes(query);
    }
    libraryTools.querySelector("[data-library-count]").textContent = `${items.filter(item => !item.hidden).length} von ${items.length} Büchern auf dieser Seite`;
  };
  search.addEventListener("input", update);
  filter.addEventListener("change", update);
  update();
}
document.querySelector("[data-upload-open]")?.addEventListener("click", () => {
  const upload = document.getElementById("add-book");
  if (upload) upload.open = true;
});

const form = document.getElementById("upload-form");
if (form) {
  const input = document.getElementById("word-file");
  const submit = document.getElementById("upload-submit");
  const feedback = document.getElementById("upload-feedback");
  const message = document.getElementById("upload-message");
  const progress = document.getElementById("upload-progress");
  let busy = false;

  function show(text, error = false) {
    feedback.hidden = false;
    feedback.classList.toggle("error", error);
    message.textContent = text;
  }
  function upload(files) {
    if (busy) return;
    if (files.length !== 1) { show("Bitte genau eine Word-Datei auswählen.", true); return; }
    const file = files[0];
    if (!file.name.toLowerCase().endsWith(".docx")) { show("Bitte eine Datei im Format .docx auswählen.", true); return; }
    if (file.size === 0 || file.size > Number(form.dataset.maxBytes)) {
      show("Die Datei ist leer oder größer als das angegebene Upload-Limit.", true); return;
    }
    busy = true;
    input.disabled = submit.disabled = true;
    form.setAttribute("aria-busy", "true");
    progress.hidden = false;
    progress.value = 0;
    show("Datei wird hochgeladen …");
    const request = new XMLHttpRequest();
    request.open("POST", form.action);
    request.setRequestHeader("Accept", "application/json");
    request.timeout = 120000;
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) progress.value = Math.round(event.loaded / event.total * 100);
      if (event.lengthComputable && event.loaded === event.total) {
        show("Datei wird geprüft und für den Hintergrundimport gespeichert …");
        progress.removeAttribute("value");
      }
    });
    function finishError(text) {
      show(text, true);
      busy = false;
      input.disabled = submit.disabled = false;
      form.removeAttribute("aria-busy");
      progress.hidden = true;
    }
    request.addEventListener("load", () => {
      let result, destination;
      try {
        result = JSON.parse(request.responseText);
        if (request.status >= 200 && request.status < 300) destination = new URL(result.url, window.location.href);
      } catch { finishError("Unerwartete Serverantwort. Bitte erneut versuchen."); return; }
      if (request.status < 200 || request.status >= 300) {
        finishError(result.error || "Upload fehlgeschlagen. Bitte erneut versuchen."); return;
      }
      if (!destination || destination.origin !== window.location.origin || !destination.pathname.startsWith("/books/local/")) {
        finishError("Die Antwort enthält keine gültige lokale Buchadresse."); return;
      }
      show(result.message);
      window.location.assign(destination.href);
    });
    request.addEventListener("error", () => finishError("Server nicht erreichbar. Bitte prüfen, ob die Anwendung läuft."));
    request.addEventListener("timeout", () => finishError("Die Antwort dauert zu lange. Du kannst den Upload wiederholen; identische Dateien werden erkannt."));
    const body = new FormData();
    body.append("file", file);
    request.send(body);
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); upload(input.files); });
  for (const eventName of ["dragover", "drop"]) {
    document.addEventListener(eventName, (event) => {
      if (!Array.from(event.dataTransfer?.types ?? []).includes("Files")) return;
      event.preventDefault();
      const overForm = form.contains(event.target);
      if (eventName === "dragover") {
        event.dataTransfer.dropEffect = overForm && !busy ? "copy" : "none";
        form.classList.toggle("dragging", overForm && !busy);
      } else {
        form.classList.remove("dragging");
        if (overForm) upload(event.dataTransfer.files);
        else show("Bitte die Datei im Bereich „Neues Buch hinzufügen“ ablegen.", true);
      }
    });
  }
  form.addEventListener("dragleave", (event) => {
    if (!form.contains(event.relatedTarget)) form.classList.remove("dragging");
  });
}

// Native dialogs retain keyboard focus, support Escape and keep media URLs usable
// as regular links when JavaScript is disabled.
for (const dialog of document.querySelectorAll("dialog.teaser-dialog")) {
  let opener = null;
  dialog.addEventListener("teaser-open", event => { opener = event.detail; });
  dialog.querySelectorAll("[data-teaser-dialog-close]").forEach(button => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
  dialog.addEventListener("close", () => {
    const image = dialog.querySelector("[data-teaser-lightbox-image]");
    if (image) { image.removeAttribute("src"); image.alt = ""; }
    if (opener?.isConnected) opener.focus();
    opener = null;
  });
}
for (const button of document.querySelectorAll("[data-teaser-dialog-open]")) {
  button.addEventListener("click", () => {
    const dialog = document.getElementById(button.dataset.teaserDialogOpen);
    if (!(dialog instanceof HTMLDialogElement)) return;
    dialog.dispatchEvent(new CustomEvent("teaser-open", {detail: button}));
    dialog.showModal();
  });
}
const chapterImageLightbox = document.getElementById("chapter-image-lightbox");
if (chapterImageLightbox) {
  for (const link of document.querySelectorAll("[data-teaser-image-open]")) {
    link.addEventListener("click", event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const image = chapterImageLightbox.querySelector("[data-teaser-lightbox-image]");
      const title = link.querySelector("img")?.alt || "Kapitelbild";
      image.src = link.href;
      image.alt = title;
      chapterImageLightbox.querySelector("h2").textContent = title;
      chapterImageLightbox.dispatchEvent(new CustomEvent("teaser-open", {detail: link}));
      chapterImageLightbox.showModal();
    });
  }
}

// Chapter plans use saved inputs. Unsaved cast, prompt or direction edits must
// be saved before a new plan or a render based on the saved plan can start.
const chapterImageMethods = [...document.querySelectorAll("[data-chapter-image-method]")];
const chapterPlanRows = [...document.querySelectorAll("[data-chapter-plan-row]")];
const chapterPlanDirty = row => [...row.querySelectorAll(
  '[data-chapter-plan-inputs] [name="scene_direction"], [name="image_prompt"], [name="character_id"]'
)].some(input => input.type === "checkbox" ? input.checked !== input.defaultChecked
  : input.value.trim() !== input.defaultValue.trim());
const syncChapterImageMethods = () => {
  for (const form of chapterImageMethods) {
    const method = form.querySelector("[data-chapter-method]");
    const render = form.querySelector("[data-chapter-method-render]");
    const message = form.querySelector("[data-chapter-method-message]");
    if (!method || !render) continue;
    const planned = method.value === "planned_scene" || method.value === "scene_plan";
    const row = form.closest("[data-chapter-plan-row]");
    const dirty = row ? chapterPlanDirty(row) : chapterPlanRows.some(chapterPlanDirty);
    const ready = planned ? form.dataset.planReady === "true" : form.dataset.legacyReady === "true";
    render.disabled = !ready || (planned && dirty);
    if (message) {
      if (!message.dataset.initialMessage) message.dataset.initialMessage = message.textContent;
      message.textContent = dirty && planned
        ? "Auswahl, Bildprompt oder Regie geändert: im Editor speichern und den Szenenplan aktualisieren."
        : message.dataset.initialMessage;
      message.hidden = !planned || (ready && !dirty);
    }
  }
  for (const row of chapterPlanRows) {
    const generation = row.querySelector("[data-chapter-plan-generate]");
    const button = generation?.querySelector('button[type="submit"]');
    if (!button) continue;
    if (!button.dataset.initialDisabled) button.dataset.initialDisabled = String(button.disabled);
    const dirty = chapterPlanDirty(row);
    button.disabled = button.dataset.initialDisabled === "true" || dirty;
    const pending = generation.querySelector("[data-chapter-plan-pending]");
    if (pending) pending.hidden = !dirty;
  }
};
for (const form of chapterImageMethods) {
  form.querySelector("[data-chapter-method]")?.addEventListener("change", syncChapterImageMethods);
  form.addEventListener("submit", event => {
    syncChapterImageMethods();
    if (form.querySelector("[data-chapter-method-render]")?.disabled) event.preventDefault();
  });
}
for (const row of chapterPlanRows) {
  row.addEventListener("input", syncChapterImageMethods);
  row.addEventListener("change", event => {
    const source = event.target;
    if (source.matches('[data-chapter-cast-source] [name="character_id"]')) {
      const editorInput = [...row.querySelectorAll('[data-chapter-plan-inputs] [name="character_id"]')]
        .find(input => input.value === source.value);
      if (editorInput && editorInput.checked !== source.checked) {
        editorInput.checked = source.checked;
        editorInput.dispatchEvent(new Event("change", {bubbles: true}));
      }
    }
    syncChapterImageMethods();
  });
}
syncChapterImageMethods();

// Chapter and final-teaser queue forms are outside the dynamically loaded reel
// workshop, so their scheduled-date validation must be wired independently.
for (const publication of document.querySelectorAll("[data-publication-form]")) {
  const time = publication.querySelector("[data-publication-time]");
  const modes = [...publication.querySelectorAll('[name="queue_mode"]')];
  if (!time) continue;
  const syncTime = () => {
    const scheduled = modes.some(input => input.checked && input.value === "scheduled");
    time.required = scheduled;
    // Keep this field submitted: publication endpoints require an explicit empty
    // scheduled_for for daily mode rather than an omitted disabled input.
    time.readOnly = !scheduled;
    if (!scheduled) time.value = "";
  };
  modes.forEach(input => input.addEventListener("change", syncTime));
  syncTime();
}

const chapterProduction = document.querySelector("[data-chapter-production]");
if (chapterProduction) {
  const label = chapterProduction.querySelector("[data-chapter-production-label]");
  const stage = chapterProduction.querySelector("[data-chapter-production-stage]");
  const count = chapterProduction.querySelector("[data-chapter-production-count]");
  const progress = chapterProduction.querySelector("[data-chapter-production-progress]");
  let state = chapterProduction.dataset.initialState;

  async function pollChapterProduction() {
    try {
      const response = await fetch(chapterProduction.dataset.statusUrl, {
        headers: {Accept: "application/json"}, cache: "no-store",
      });
      if (!response.ok) throw new Error();
      const result = await response.json();
      if (label) label.textContent = result.label;
      if (stage) stage.textContent = result.stage;
      if (count) count.textContent = `${result.completed} von ${result.total}`;
      if (progress) {
        progress.max = Math.max(1, Number(result.total));
        progress.value = Number(result.completed);
      }
      if (!result.active || result.state !== state) {
        window.bookpromoUnsaved.reload();
        return;
      }
      state = result.state;
    } catch {
      if (stage) stage.textContent = "Fortschritt gerade nicht erreichbar – neuer Versuch läuft …";
    }
    window.setTimeout(pollChapterProduction, 2500);
  }
  window.setTimeout(pollChapterProduction, 1200);
}

const bookTeaserRender = document.querySelector("[data-book-teaser-render]");
if (bookTeaserRender) {
  const stage = bookTeaserRender.querySelector("[data-book-teaser-render-stage]");
  const count = bookTeaserRender.querySelector("[data-book-teaser-render-count]");
  const progress = bookTeaserRender.querySelector("[data-book-teaser-render-progress]");
  let state = bookTeaserRender.dataset.initialState;

  async function pollBookTeaserRender() {
    try {
      const response = await fetch(bookTeaserRender.dataset.statusUrl, {
        headers: {Accept: "application/json"}, cache: "no-store",
      });
      if (!response.ok) throw new Error();
      const result = await response.json();
      if (stage) stage.textContent = result.stage;
      if (count) count.textContent = `${(result.progress_ms / 1000).toLocaleString("de-DE", {maximumFractionDigits: 1})} / ${(result.total_ms / 1000).toLocaleString("de-DE", {maximumFractionDigits: 1})} s`;
      if (progress) {
        progress.max = Math.max(1, Number(result.total_ms));
        progress.value = Number(result.progress_ms);
      }
      if (!result.active || result.state !== state) {
        window.bookpromoUnsaved.reload();
        return;
      }
      state = result.state;
    } catch {
      if (stage) stage.textContent = "Exportstatus gerade nicht erreichbar – neuer Versuch läuft …";
    }
    window.setTimeout(pollBookTeaserRender, 2500);
  }
  window.setTimeout(pollBookTeaserRender, 1200);
}

const chapterForm = document.getElementById("chapter-form");
let chapterEdits = false;
if (chapterForm) {
  chapterForm.addEventListener("input", () => { chapterEdits = true; });
  const rows = document.getElementById("chapter-rows");
  document.getElementById("add-chapter").addEventListener("click", () => {
    if (rows.children.length >= 250) return;
    rows.append(document.getElementById("chapter-row-template").content.cloneNode(true));
    chapterEdits = true;
    window.bookpromoUnsaved.refresh(chapterForm);
    rows.lastElementChild.querySelector("input").focus();
  });
  rows.addEventListener("click", (event) => {
    if (event.target.matches(".remove-chapter") && rows.children.length > 1) {
      event.target.closest(".chapter-row").remove();
      chapterEdits = true;
      window.bookpromoUnsaved.refresh(chapterForm);
    }
  });
}

const profileGenerator = document.getElementById("profile-generator");
if (profileGenerator) {
  const start = profileGenerator.querySelector("[data-profile-start]");
  const apply = profileGenerator.querySelector("[data-profile-apply]");
  const status = profileGenerator.querySelector("[data-profile-status]");
  const progress = profileGenerator.querySelector("[data-profile-progress]");
  const provider = profileGenerator.querySelector("[data-profile-provider]");
  const form = document.querySelector(".book-settings-form");
  const fields = ["genre", "mood", "internal_summary", "world", "characters", "spoilers", "image_prompt_base", "caption_guidelines"];
  let timer;
  async function request(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options, headers: {Accept: "application/json"}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Die Profilanalyse ist gerade nicht erreichbar.");
    return data;
  }
  async function refresh() {
    window.clearTimeout(timer);
    try {
      const data = await request(profileGenerator.dataset.statusUrl);
      if (data.provider && provider.querySelector(`option[value="${CSS.escape(data.provider)}"]`)) {
        provider.value = data.provider;
      }
      provider.disabled = data.active;
      start.disabled = data.active;
      start.textContent = data.state === "failed" ? "Profilanalyse fortsetzen" : "Profilfelder mit KI erstellen";
      apply.hidden = data.state !== "done";
      progress.hidden = !data.active;
      status.textContent = data.error || (data.active
        ? `${data.stage} · ${data.calls_started} KI-Anfragen · ${data.completed_steps} Zwischenstände gespeichert`
        : data.label);
      if (data.active) timer = window.setTimeout(refresh, 2000);
    } catch (error) {
      status.textContent = error.message;
      timer = window.setTimeout(refresh, 5000);
    }
  }
  start.addEventListener("click", async () => {
    window.clearTimeout(timer);
    start.disabled = true;
    apply.hidden = true;
    status.textContent = "Profilanalyse wird vorgemerkt …";
    try {
      await request(profileGenerator.dataset.startUrl, {
        method: "POST", body: new URLSearchParams({ai_provider: provider.value}),
      });
      await refresh();
    } catch (error) {
      start.disabled = false;
      status.textContent = error.message;
    }
  });
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      const data = await request(profileGenerator.dataset.resultUrl);
      if (typeof data.id !== "string" || !data.fields || Object.keys(data.fields).length !== fields.length ||
          fields.some(name => typeof data.fields[name] !== "string" || data.fields[name].length > form.elements.namedItem(name).maxLength)) {
        throw new Error("Die Profilvorschläge sind unvollständig oder zu lang.");
      }
      for (const name of fields) form.elements.namedItem(name).value = data.fields[name];
      form.elements.namedItem("profile_run_id").value = data.id;
      window.bookpromoUnsaved.refresh(form);
      status.textContent = "Vorschläge eingesetzt. Bitte prüfen, bei Bedarf bearbeiten und mit Bucheinstellungen speichern übernehmen.";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      apply.disabled = false;
    }
  });
  refresh();
}

const characterAnalysis = document.querySelector("[data-character-analysis]");
if (characterAnalysis) {
  const start = characterAnalysis.querySelector("[data-character-analysis-start]");
  const status = characterAnalysis.querySelector("[data-character-analysis-status]");
  const progress = characterAnalysis.querySelector("[data-character-analysis-progress]");
  const provider = characterAnalysis.querySelector("[data-character-analysis-provider]");
  if (start && status && progress && provider) {
    let timer;
    let observedActive = false;
    async function request(url, options = {}) {
      const response = await fetch(url, {
        cache: "no-store", ...options, headers: {Accept: "application/json"},
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Die Charakteranalyse ist gerade nicht erreichbar.");
      return data;
    }
    async function refresh() {
      window.clearTimeout(timer);
      try {
        const data = await request(characterAnalysis.dataset.statusUrl);
        if (data.provider && provider.querySelector(`option[value="${CSS.escape(data.provider)}"]`)) {
          provider.value = data.provider;
        }
        provider.disabled = data.active;
        if (observedActive && !data.active && data.state === "done") {
          window.bookpromoUnsaved.reload();
          return;
        }
        observedActive ||= data.active;
        start.disabled = data.active || data.state === "done";
        start.textContent = data.state === "failed"
          ? "Charakteranalyse fortsetzen"
          : data.state === "done" ? "Charakteranalyse abgeschlossen" : "Charaktere aus Buch analysieren";
        progress.hidden = !data.active;
        status.textContent = data.error || (data.active
          ? `${data.stage} · ${data.calls_started} KI-Anfragen · ${data.completed_steps} Zwischenstände gespeichert`
          : data.label);
        if (data.active) timer = window.setTimeout(refresh, 2000);
      } catch (error) {
        status.textContent = error.message;
        timer = window.setTimeout(refresh, 5000);
      }
    }
    start.addEventListener("click", async () => {
      window.clearTimeout(timer);
      start.disabled = true;
      status.textContent = "Charakteranalyse wird vorgemerkt …";
      try {
        await request(characterAnalysis.dataset.startUrl, {
          method: "POST", body: new URLSearchParams({ai_provider: provider.value}),
        });
        await refresh();
      } catch (error) {
        start.disabled = false;
        status.textContent = error.message;
      }
    });
    refresh();
  }
}

const carouselBackgroundColors = document.querySelector("[data-carousel-background-colors]");
if (carouselBackgroundColors) {
  const titleColor = document.querySelector('[name="overlay_title_color"]');
  const bottomColor = carouselBackgroundColors.querySelector('[name="carousel_background_bottom_color"]');
  function darkenedTitleColor() {
    const channels = titleColor.value.slice(1).match(/.{2}/g).map(value => Math.round(parseInt(value, 16) * 0.4));
    return `#${channels.map(value => value.toString(16).padStart(2, "0")).join("")}`;
  }
  let followsTitleColor = bottomColor.value.toLowerCase() === darkenedTitleColor();
  titleColor.addEventListener("input", () => {
    if (followsTitleColor) bottomColor.value = darkenedTitleColor();
  });
  bottomColor.addEventListener("input", () => { followsTitleColor = false; });
}

const analysisPanel = document.getElementById("analysis-progress");
if (analysisPanel) {
  async function refreshAnalysis() {
    try {
      const status = await readImportStatus(analysisPanel.dataset.statusUrl);
      if (!status.active) {
        if (!chapterEdits) { window.bookpromoUnsaved.reload(); return; }
        analysisPanel.querySelector("[data-analysis-stage]").textContent = `${status.label}. Speichere deine Kapiteländerungen oder lade die Seite neu, um das Ergebnis zu sehen.`;
        analysisPanel.querySelector("progress").hidden = true;
        return;
      }
      analysisPanel.querySelector("[data-analysis-stage]").textContent = status.stage;
      analysisPanel.querySelector("[data-analysis-count]").textContent = `${status.completed_steps} Zwischenstände gespeichert · ${status.calls_started} KI-Anfragen gestartet`;
    } catch {
      analysisPanel.querySelector("[data-analysis-stage]").textContent = "Analysestatus nicht erreichbar. Die Anzeige versucht es erneut.";
    }
    window.setTimeout(refreshAnalysis, 2000);
  }
  window.setTimeout(refreshAnalysis, 500);
}
for (const analysisForm of document.querySelectorAll(".analysis-start-form")) {
  analysisForm.addEventListener("submit", () => {
    const button = analysisForm.querySelector("button");
    button.disabled = true;
    button.textContent = "KI-Analyse wird vorgemerkt …";
  });
}

const deleteBookButton = document.querySelector("[data-delete-book-open]");
const deleteBookDialog = document.getElementById("delete-book-dialog");
if (deleteBookButton && deleteBookDialog) {
  const cancelButton = deleteBookDialog.querySelector("[data-delete-book-cancel]");
  const deleteForm = deleteBookDialog.querySelector("form");
  const submitButton = deleteBookDialog.querySelector("[data-delete-book-submit]");
  deleteBookButton.addEventListener("click", () => deleteBookDialog.showModal());
  cancelButton.addEventListener("click", () => deleteBookDialog.close());
  deleteForm.addEventListener("submit", () => {
    submitButton.disabled = true;
    submitButton.textContent = "Wird gelöscht …";
  });
}
for (const extractionForm of document.querySelectorAll(".extract-form")) {
  extractionForm.addEventListener("submit", () => {
    const button = extractionForm.querySelector("button");
    button.disabled = true;
    button.textContent = "Import wird vorgemerkt …";
  });
}

async function readImportStatus(url) {
  const response = await fetch(url, {headers: {Accept: "application/json"}, cache: "no-store"});
  if (!response.ok) throw new Error("Status nicht verfügbar");
  return response.json();
}
const importPanel = document.getElementById("import-progress");
if (importPanel) {
  async function refreshImport() {
    try {
      const status = await readImportStatus(importPanel.dataset.statusUrl);
      if (!status.active) { window.bookpromoUnsaved.reload(); return; }
      document.getElementById("import-stage").textContent = status.stage;
      const meter = document.getElementById("import-meter");
      if (status.total > 0) { meter.max = status.total; meter.value = status.completed; }
      else { meter.removeAttribute("value"); }
      document.getElementById("import-count").textContent = status.total > 0
        ? `Absatz ${status.completed} von ${status.total}` : "";
    } catch {
      document.getElementById("import-stage").textContent = "Status vorübergehend nicht erreichbar. Die Anzeige versucht es erneut.";
    }
    window.setTimeout(refreshImport, 2000);
  }
  window.setTimeout(refreshImport, 500);
}
for (const row of document.querySelectorAll("[data-import-row]")) {
  async function refreshRow() {
    try {
      const status = await readImportStatus(row.dataset.importRow);
      row.querySelector("[data-import-label]").textContent = status.label;
      row.querySelector("[data-chapter-count]").textContent = status.chapter_count ?? "—";
      if (!status.active && status.state !== "uploaded") return;
    } catch {
      row.querySelector("[data-import-label]").textContent = "Status nicht erreichbar";
    }
    window.setTimeout(refreshRow, 3000);
  }
  window.setTimeout(refreshRow, 1000);
}

const aiSettings = document.getElementById("openwebui-settings");
if (aiSettings) {
  const message = document.getElementById("openwebui-message");
  const picker = document.getElementById("openwebui-model-picker");
  const filter = document.getElementById("openwebui-filter");
  const select = document.getElementById("openwebui-model");
  const selection = document.getElementById("openwebui-selection");
  const buttons = [...aiSettings.querySelectorAll("button")];
  let models = [], currentModel = null;
  function showMessage(text, error = false) {
    message.hidden = false;
    message.classList.toggle("error", error);
    message.textContent = text;
  }
  function renderModels() {
    const chosen = select.value || currentModel;
    const search = filter.value.toLocaleLowerCase();
    const fragment = document.createDocumentFragment();
    for (const model of models) {
      if (!`${model.name} ${model.id}`.toLocaleLowerCase().includes(search)) continue;
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.name === model.id ? model.id : `${model.name} (${model.id})`;
      option.selected = model.id === chosen;
      fragment.append(option);
    }
    select.replaceChildren(fragment);
    if (!chosen || ![...select.options].some(option => option.value === chosen)) select.selectedIndex = -1;
    selection.textContent = currentModel ? `Gespeichertes Modell: ${currentModel}` : "Noch kein Modell gespeichert.";
  }
  async function action(url, body, waiting, onSuccess) {
    buttons.forEach(button => { button.disabled = true; });
    showMessage(waiting);
    try {
      const response = await fetch(url, {method: "POST", body, headers: {Accept: "application/json"}});
      const result = await response.json();
      if (!response.ok) { showMessage(result.error || "Die Aktion konnte nicht abgeschlossen werden.", true); return; }
      showMessage(result.message);
      if (onSuccess) onSuccess(result);
    } catch {
      showMessage("Die Anwendung ist nicht erreichbar oder lieferte keine gültige Antwort. Bitte erneut versuchen.", true);
    } finally {
      buttons.forEach(button => { button.disabled = false; });
    }
  }
  document.getElementById("openwebui-check").addEventListener("click", () => {
    action(aiSettings.dataset.checkUrl, undefined, "Verbindung wird geprüft und Modelle werden geladen …", result => {
      models = result.models;
      currentModel = result.selected_model;
      filter.value = "";
      picker.hidden = false;
      renderModels();
      if (!result.model_available) showMessage("Zugang geprüft. Bitte ein verfügbares Modell auswählen und speichern.");
    });
  });
  document.getElementById("openwebui-save").addEventListener("click", () => {
    if (!select.value) { showMessage("Bitte zuerst ein Modell auswählen.", true); return; }
    const body = new FormData();
    body.append("model", select.value);
    action(aiSettings.dataset.modelUrl, body, "Modell wird geprüft und gespeichert …", result => {
      currentModel = result.selected_model;
      selection.textContent = `Gespeichertes Modell: ${currentModel}`;
    });
  });
  document.getElementById("openwebui-test").addEventListener("click", () => {
    action(aiSettings.dataset.testUrl, undefined, "Technische Testantwort wird erzeugt …");
  });
  filter.addEventListener("input", renderModels);
}

const comfySettings = document.getElementById("comfyui-settings");
if (comfySettings) {
  const button = document.getElementById("comfyui-check");
  const message = document.getElementById("comfyui-message");
  button.addEventListener("click", async () => {
    button.disabled = true;
    message.hidden = false;
    message.classList.remove("error");
    message.textContent = "ComfyUI wird geprüft …";
    try {
      const response = await fetch(comfySettings.dataset.checkUrl, {
        method: "POST", headers: {Accept: "application/json"}, cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Die Prüfung ist fehlgeschlagen.");
      message.textContent = result.message;
    } catch (error) {
      message.classList.add("error");
      message.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}

const r2Check = document.querySelector("[data-r2-check]");
if (r2Check) {
  const button = r2Check.querySelector("button");
  const message = r2Check.querySelector("[role='status']");
  button.addEventListener("click", async () => {
    button.disabled = true;
    message.hidden = false;
    message.classList.remove("error", "success");
    message.textContent = "R2 wird mit einem kleinen Testobjekt geprüft …";
    try {
      const response = await fetch(r2Check.dataset.checkUrl, {
        method: "POST", headers: {Accept: "application/json"}, cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Die R2-Prüfung ist fehlgeschlagen.");
      message.classList.add("success");
      message.textContent = result.message;
    } catch (error) {
      message.classList.add("error");
      message.textContent = error.message;
    } finally { button.disabled = false; }
  });
}

const reelDialog = document.getElementById("reel-dialog");
if (reelDialog) {
  const content = reelDialog.querySelector("[data-reel-content]");
  const title = reelDialog.querySelector("#reel-dialog-title");
  let workshopUrl = "";
  let workshopRequest = 0;
  let pollTimer;
  let fragmentCleanup = () => {};

  function reelMessage(text, error = false) {
    let node = content.querySelector("[data-reel-message]");
    if (!node) {
      node = document.createElement("p");
      node.dataset.reelMessage = "";
      node.className = "notice";
      content.prepend(node);
    }
    node.hidden = false;
    node.classList.toggle("error", error);
    node.textContent = text;
    if (error) node.scrollIntoView({behavior: "smooth", block: "nearest"});
  }

  function wireAudioEditor() {
    const editor = content.querySelector("[data-audio-editor]");
    if (!editor) return () => {};
    const track = content.querySelector("[data-audio-track]");
    const startInput = content.querySelector("[data-audio-start]");
    const durationInput = content.querySelector("[data-audio-duration]");
    const startLabel = editor.querySelector("[data-audio-start-label]");
    const endLabel = editor.querySelector("[data-audio-end-label]");
    const canvas = editor.querySelector("[data-audio-waveform]");
    const audio = editor.querySelector("[data-audio-source]");
    const play = editor.querySelector("[data-audio-play]");
    const stop = editor.querySelector("[data-audio-stop]");
    const loop = editor.querySelector("[data-audio-loop]");
    if (!track || !startInput || !durationInput || !canvas || !audio) return () => {};

    const abort = new AbortController();
    let peaks = [];
    let total = 0;
    let requestNumber = 0;
    let dragging = null;
    let dragOffset = 0;
    let selectionPlayback = false;

    const clamp = (value, minimum, maximum) => Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
    const formatTime = value => {
      const seconds = Math.max(0, Number(value) || 0);
      const minutes = Math.floor(seconds / 60);
      return `${minutes}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
    };
    const values = () => {
      const duration = clamp(Number(durationInput.value) || 4, 4, Math.min(30, total || 30));
      const start = clamp(Number(startInput.value) || 0, 0, Math.max(0, total - duration));
      return {start, duration, end: Math.min(total || start + duration, start + duration)};
    };

    function drawWaveform() {
      const width = Math.max(320, Math.round(canvas.clientWidth || 800));
      const height = Math.max(112, Math.round(canvas.clientHeight || 140));
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
        canvas.width = width * ratio;
        canvas.height = height * ratio;
      }
      const context = canvas.getContext("2d");
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.fillStyle = "#111923";
      context.fillRect(0, 0, width, height);
      const middle = height / 2;
      context.strokeStyle = "#718196";
      context.lineWidth = Math.max(1, width / Math.max(peaks.length, 1));
      context.beginPath();
      if (peaks.length) {
        peaks.forEach((peak, index) => {
          const x = (index + .5) * width / peaks.length;
          const amplitude = Math.max(1, Math.pow(peak, .55) * (middle - 10));
          context.moveTo(x, middle - amplitude);
          context.lineTo(x, middle + amplitude);
        });
      } else {
        context.moveTo(0, middle);
        context.lineTo(width, middle);
      }
      context.stroke();

      const selection = values();
      const startX = total ? selection.start / total * width : 0;
      const endX = total ? selection.end / total * width : 0;
      context.fillStyle = "rgb(39 110 204 / 34%)";
      context.fillRect(startX, 0, Math.max(0, endX - startX), height);
      context.strokeStyle = "#5ca4ff";
      context.lineWidth = 3;
      for (const x of [startX, endX]) {
        context.beginPath();
        context.moveTo(x, 0);
        context.lineTo(x, height);
        context.stroke();
      }
      if (Number.isFinite(audio.currentTime) && audio.currentTime > 0 && total) {
        const playX = audio.currentTime / total * width;
        context.strokeStyle = "#f7c948";
        context.lineWidth = 2;
        context.beginPath();
        context.moveTo(playX, 0);
        context.lineTo(playX, height);
        context.stroke();
      }
    }

    function syncSelection() {
      const selection = values();
      startInput.value = selection.start.toFixed(1).replace(/\.0$/, "");
      durationInput.value = (Math.round(selection.duration * 2) / 2).toString();
      startLabel.textContent = formatTime(selection.start);
      endLabel.textContent = formatTime(selection.end);
      canvas.setAttribute("aria-label", `Wellenform. Auswahl von ${formatTime(selection.start)} bis ${formatTime(selection.end)}.`);
      drawWaveform();
    }

    function setSelection(start, duration) {
      startInput.value = (Math.round(start * 10) / 10).toString();
      durationInput.value = (Math.round(duration * 2) / 2).toString();
      startInput.dispatchEvent(new Event("input", {bubbles: true}));
      durationInput.dispatchEvent(new Event("input", {bubbles: true}));
    }

    async function selectTrack() {
      const option = track.selectedOptions[0];
      requestNumber += 1;
      const currentRequest = requestNumber;
      selectionPlayback = false;
      audio.pause();
      audio.src = option?.dataset.audioUrl || "";
      total = Number(option?.dataset.durationMs || 0) / 1000;
      peaks = [];
      syncSelection();
      if (!option?.dataset.waveformUrl) return;
      try {
        const response = await fetch(option.dataset.waveformUrl, {
          headers: {Accept: "application/json"}, signal: abort.signal, cache: "no-store",
        });
        if (!response.ok) throw new Error("Wellenform nicht verfügbar");
        const result = await response.json();
        if (currentRequest !== requestNumber) return;
        total = Number(result.duration_ms || 0) / 1000;
        peaks = Array.isArray(result.peaks) ? result.peaks : [];
        syncSelection();
      } catch (error) {
        if (error.name !== "AbortError") drawWaveform();
      }
    }

    function pointerTime(event) {
      const bounds = canvas.getBoundingClientRect();
      return clamp((event.clientX - bounds.left) / bounds.width * total, 0, total);
    }
    canvas.addEventListener("pointerdown", event => {
      if (!total) return;
      const time = pointerTime(event);
      const selection = values();
      const tolerance = Math.max(.4, total * 12 / canvas.clientWidth);
      if (Math.abs(time - selection.start) <= tolerance) dragging = "start";
      else if (Math.abs(time - selection.end) <= tolerance) dragging = "end";
      else if (time > selection.start && time < selection.end) {
        dragging = "move";
        dragOffset = time - selection.start;
      } else {
        dragging = "move";
        dragOffset = 0;
        setSelection(clamp(time, 0, total - selection.duration), selection.duration);
      }
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", event => {
      if (!dragging || !total) return;
      const time = pointerTime(event);
      const selection = values();
      if (dragging === "move") {
        setSelection(clamp(time - dragOffset, 0, total - selection.duration), selection.duration);
      } else if (dragging === "start") {
        const start = clamp(time, 0, selection.end - 4);
        setSelection(start, selection.end - start);
      } else {
        setSelection(selection.start, clamp(time - selection.start, 4, Math.min(30, total - selection.start)));
      }
    });
    const endDrag = event => {
      if (dragging && canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      dragging = null;
    };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    track.addEventListener("change", selectTrack);
    startInput.addEventListener("input", syncSelection);
    durationInput.addEventListener("input", syncSelection);
    play.addEventListener("click", async () => {
      const selection = values();
      selectionPlayback = true;
      audio.currentTime = selection.start;
      try { await audio.play(); } catch { selectionPlayback = false; }
    });
    stop.addEventListener("click", () => {
      selectionPlayback = false;
      audio.pause();
      audio.currentTime = values().start;
      drawWaveform();
    });
    audio.addEventListener("timeupdate", () => {
      const selection = values();
      if (selectionPlayback && audio.currentTime >= selection.end - .03) {
        if (loop.checked) audio.currentTime = selection.start;
        else { selectionPlayback = false; audio.pause(); }
      }
      drawWaveform();
    });
    const resize = new ResizeObserver(drawWaveform);
    resize.observe(canvas);
    selectTrack();
    return () => { abort.abort(); resize.disconnect(); audio.pause(); };
  }

  function wireReelFragment() {
    wireSimpleImageForms(content);
    // Only forms restored from a real user edit are dirty at entry. Initializers
    // normalize display values (e.g. audio 20.0 -> 20); accepting another form
    // refreshes every form and can otherwise mistake that normalization for an edit.
    const restoredDirtyForms = new Set(content.querySelectorAll('form[data-reel-dirty="true"]'));
    const stepKey = `bookpromo-reel-steps:${new URL(workshopUrl, location.href).pathname}`;
    const steps = [...content.querySelectorAll("[data-reel-step]")];
    let savedSteps = null;
    try { savedSteps = JSON.parse(sessionStorage.getItem(stepKey)); } catch { /* first open or restricted storage */ }
    if (Array.isArray(savedSteps)) steps.forEach(step => { step.open = savedSteps.includes(step.dataset.reelStep); });
    steps.forEach(step => step.addEventListener("toggle", () => {
      try { sessionStorage.setItem(stepKey, JSON.stringify(steps.filter(item => item.open).map(item => item.dataset.reelStep))); } catch { /* restricted storage */ }
    }));
    content.querySelectorAll("form[data-reel-action]").forEach(form => {
      form.addEventListener("invalid", event => {
        const step = event.target.closest("[data-reel-step]");
        if (step) step.open = true;
      }, true);
      const strategy = form.querySelector('[name="strategy"]');
      const direction = form.querySelector('[name="scene_direction"]');
      const saveDirection = form.querySelector('button[formaction]');
      if (strategy && direction) {
        const saveWasDisabled = !!saveDirection?.disabled;
        const render = form.querySelector('[data-plan-render]');
        const renderWasDisabled = !!render?.disabled;
        const planSources = [...content.querySelectorAll('[name="image_prompt"], [name="character_ids"], [name="scene_direction"]')];
        const planButton = content.querySelector('[data-plan-generate] button[type="submit"]');
        const planWasDisabled = !!planButton?.disabled;
        const syncDirection = () => {
          const masked = strategy.value === "masked";
          direction.disabled = masked;
          if (saveDirection) saveDirection.disabled = masked || saveWasDisabled;
          const sourceChanged = planSources.some(input => input.type === "checkbox"
            ? input.checked !== input.defaultChecked : input.value.trim() !== input.defaultValue.trim());
          if (render) render.disabled = renderWasDisabled || (strategy.value === "planned_scene" && (
            render.dataset.planCurrent !== "true" || sourceChanged
          ));
          if (planButton) planButton.disabled = planWasDisabled || sourceChanged;
          const pending = content.querySelector('[data-plan-pending]');
          if (pending) pending.hidden = !sourceChanged;
        };
        strategy.addEventListener("change", syncDirection);
        for (const input of planSources) {
          input.addEventListener("input", syncDirection);
          input.addEventListener("change", syncDirection);
        }
        syncDirection();
      }
    });
    const duration = content.querySelector('[name="duration_seconds"]');
    const output = content.querySelector("[data-reel-duration-output]");
    if (duration && output) {
      const showDuration = () => { output.textContent = `${Number(duration.value).toLocaleString("de-DE")} s`; };
      duration.addEventListener("input", showDuration);
      showDuration();
    }
    const audioCleanup = wireAudioEditor();
    const cropCleanup = window.bookpromoCrop.wire(content);
    fragmentCleanup = () => { audioCleanup(); cropCleanup(); };
    const caption = content.querySelector('[name="addition"]');
    const counter = content.querySelector("[data-caption-count]");
    if (caption && counter) {
      const showCount = () => { counter.textContent = `${caption.value.length} Zeichen Begleittext`; };
      caption.addEventListener("input", showCount);
      showCount();
    }
    const publication = content.querySelector("[data-publication-form]");
    if (publication) {
      const time = publication.querySelector("[data-publication-time]");
      const modes = [...publication.querySelectorAll('[name="queue_mode"]')];
      const syncTime = () => {
        const scheduled = modes.find(input => input.checked)?.value === "scheduled";
        time.required = scheduled;
        if (!scheduled) time.value = "";
      };
      modes.forEach(input => input.addEventListener("change", syncTime));
      syncTime();
    }
    const active = content.querySelector("[data-reel-active='true']");
    content.querySelectorAll("form[data-reel-action]").forEach(form => {
      if (restoredDirtyForms.has(form)) window.bookpromoUnsaved.refresh(form);
      else window.bookpromoUnsaved.accept(form);
    });
    if (active) pollTimer = window.setTimeout(() => loadWorkshop(workshopUrl, false), 2000);
  }

  async function loadWorkshop(url, announce = true) {
    window.clearTimeout(pollTimer);
    if (!reelDialog.open) return;
    const request = ++workshopRequest;
    // Browsers cannot copy file selections into replacement DOM. Keep them intact.
    if (!announce && (content.querySelector('[data-carousel-crop][data-reel-dirty="true"]') || [...content.querySelectorAll('form[data-reel-dirty="true"] input[type="file"]')].some(input => input.files.length))) {
      pollTimer = window.setTimeout(() => loadWorkshop(url, false), 2000);
      return;
    }
    workshopUrl = url;
    const snapshotForms = () => [...content.querySelectorAll('form[data-reel-dirty="true"]')].map(form => ({
      action: form.action,
      controls: [...form.elements].filter(input => input.name && input.type !== "hidden" && input.type !== "file" && input.type !== "submit").map((input, index) => ({index, name: input.name, value: input.value, checked: input.checked})),
    }));
    if (announce) {
      fragmentCleanup();
      fragmentCleanup = () => {};
      content.innerHTML = '<p class="muted">Medien-Daten werden geladen …</p>';
    }
    try {
      const response = await fetch(url, {headers: {Accept: "text/html"}, cache: "no-store"});
      if (!response.ok) {
        let message = "Die Reel-Werkstatt konnte nicht geladen werden.";
        try { message = (await response.json()).error || message; } catch {}
        throw new Error(message);
      }
      const html = await response.text();
      if (!reelDialog.open || request !== workshopRequest) return;
      window.bookpromoUnsaved.hasChanges(content);
      if (content.querySelector('[data-carousel-crop][data-reel-dirty="true"]') || [...content.querySelectorAll('form[data-reel-dirty="true"] input[type="file"]')].some(input => input.files.length)) {
        pollTimer = window.setTimeout(() => loadWorkshop(url, false), 2000);
        return;
      }
      const unsaved = snapshotForms();
      fragmentCleanup();
      fragmentCleanup = () => {};
      content.innerHTML = html;
      window.bookpromoUnsaved.track(content);
      for (const saved of unsaved) {
        const form = [...content.querySelectorAll("form[data-reel-action]")].find(item => item.action === saved.action);
        if (!form) continue;
        const controls = [...form.elements].filter(input => input.name && input.type !== "hidden" && input.type !== "file" && input.type !== "submit");
        for (const value of saved.controls) {
          const input = controls[value.index];
          if (!input || input.name !== value.name) continue;
          input.value = value.value;
          if (typeof value.checked === "boolean") input.checked = value.checked;
        }
        form.dataset.reelDirty = "true";
      }
      title.textContent = "Medien-Werkstatt";
      wireReelFragment();
    } catch (error) {
      if (!reelDialog.open || request !== workshopRequest) return;
      if (announce) content.innerHTML = "";
      reelMessage(error.message, true);
    }
  }

  for (const opener of document.querySelectorAll("[data-reel-open]")) {
    opener.addEventListener("click", event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      reelDialog.showModal();
      loadWorkshop(opener.dataset.reelUrl);
    });
  }
  const closeWorkshop = () => { if (window.bookpromoUnsaved.confirmDiscard(reelDialog)) reelDialog.close(); };
  reelDialog.querySelector("[data-reel-close]").addEventListener("click", closeWorkshop);
  reelDialog.addEventListener("cancel", event => {
    if (!window.bookpromoUnsaved.confirmDiscard(reelDialog)) event.preventDefault();
  });
  reelDialog.addEventListener("close", () => {
    workshopRequest += 1;
    window.clearTimeout(pollTimer);
    fragmentCleanup();
    fragmentCleanup = () => {};
    window.bookpromoUnsaved.forget(content);
    content.innerHTML = "";
  });
  reelDialog.addEventListener("click", event => {
    if (event.target === reelDialog) closeWorkshop();
  });
  content.addEventListener("submit", async event => {
    const form = event.target.closest("form[data-reel-action]");
    if (!form) return;
    event.preventDefault();
    window.clearTimeout(pollTimer);
    const buttons = [...form.querySelectorAll("button")];
    const buttonDisabledStates = buttons.map(button => button.disabled);
    if (form.hasAttribute('data-simple-image-form')) form.dataset.simpleImageSubmitting = 'true';
    buttons.forEach(button => { button.disabled = true; });
    reelMessage(event.submitter?.dataset.waiting || form.dataset.waiting || "Aktion wird gestartet …");
    const submittedValues = window.bookpromoUnsaved.capture(form);
    try {
      const action = event.submitter?.hasAttribute("formaction") ? event.submitter.formAction : form.action;
      const body = new FormData(form);
      // Named submit buttons are not included by FormData(form), and buttons
      // are disabled above while waiting. Preserve the explicitly chosen mode.
      if (event.submitter?.name) body.append(event.submitter.name, event.submitter.value);
      const response = await fetch(action, {
        method: form.method || "POST", body, headers: {Accept: "application/json"}, cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Die Aktion konnte nicht abgeschlossen werden.");
      window.bookpromoUnsaved.accept(form, submittedValues);
      await loadWorkshop(result.workshop_url || workshopUrl, false);
    } catch (error) {
      reelMessage(error.message, true);
      delete form.dataset.simpleImageSubmitting;
      buttons.forEach((button, index) => { button.disabled = buttonDisabledStates[index]; });
      form.querySelector('[name="character_ids"]')?.dispatchEvent(new Event("change", {bubbles: true}));
      form.querySelector('[name="strategy"]')?.dispatchEvent(new Event("change", {bubbles: true}));
    }
  });
}
