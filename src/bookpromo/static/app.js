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
        window.location.reload();
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
        window.location.reload();
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
    rows.lastElementChild.querySelector("input").focus();
  });
  rows.addEventListener("click", (event) => {
    if (event.target.matches(".remove-chapter") && rows.children.length > 1) {
      event.target.closest(".chapter-row").remove();
      chapterEdits = true;
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
          window.location.reload();
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
        if (!chapterEdits) { window.location.reload(); return; }
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
      if (!status.active) { window.location.reload(); return; }
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
    const duration = content.querySelector('[name="duration_seconds"]');
    const output = content.querySelector("[data-reel-duration-output]");
    if (duration && output) {
      const showDuration = () => { output.textContent = `${Number(duration.value).toLocaleString("de-DE")} s`; };
      duration.addEventListener("input", showDuration);
      showDuration();
    }
    fragmentCleanup = wireAudioEditor();
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
    if (active) pollTimer = window.setTimeout(() => loadWorkshop(workshopUrl, false), 2000);
  }

  async function loadWorkshop(url, announce = true) {
    window.clearTimeout(pollTimer);
    fragmentCleanup();
    fragmentCleanup = () => {};
    workshopUrl = url;
    if (announce) content.innerHTML = '<p class="muted">Reel-Daten werden geladen …</p>';
    try {
      const response = await fetch(url, {headers: {Accept: "text/html"}, cache: "no-store"});
      if (!response.ok) {
        let message = "Die Reel-Werkstatt konnte nicht geladen werden.";
        try { message = (await response.json()).error || message; } catch {}
        throw new Error(message);
      }
      content.innerHTML = await response.text();
      title.textContent = content.querySelector("[data-reel-title]")?.textContent || "Reel erzeugen";
      wireReelFragment();
    } catch (error) {
      content.innerHTML = "";
      reelMessage(error.message, true);
    }
  }

  for (const opener of document.querySelectorAll("[data-reel-open]")) {
    opener.addEventListener("click", () => {
      reelDialog.showModal();
      loadWorkshop(opener.dataset.reelUrl);
    });
  }
  reelDialog.querySelector("[data-reel-close]").addEventListener("click", () => reelDialog.close());
  reelDialog.addEventListener("cancel", () => window.clearTimeout(pollTimer));
  reelDialog.addEventListener("close", () => {
    window.clearTimeout(pollTimer);
    fragmentCleanup();
    fragmentCleanup = () => {};
  });
  reelDialog.addEventListener("click", event => {
    if (event.target === reelDialog) reelDialog.close();
  });
  content.addEventListener("submit", async event => {
    const form = event.target.closest("form[data-reel-action]");
    if (!form) return;
    event.preventDefault();
    window.clearTimeout(pollTimer);
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach(button => { button.disabled = true; });
    reelMessage(form.dataset.waiting || "Aktion wird gestartet …");
    try {
      const response = await fetch(form.action, {
        method: form.method || "POST", body: new FormData(form), headers: {Accept: "application/json"}, cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Die Aktion konnte nicht abgeschlossen werden.");
      await loadWorkshop(result.workshop_url || workshopUrl, false);
    } catch (error) {
      reelMessage(error.message, true);
      buttons.forEach(button => { button.disabled = false; });
    }
  });
}
