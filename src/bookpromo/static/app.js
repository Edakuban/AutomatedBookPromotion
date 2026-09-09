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
      await request(profileGenerator.dataset.startUrl, {method: "POST"});
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
