# Strukturierter Szenenplan

Die neue Werkstatt-Methode „Szenenplan verwenden“ trennt die Identität aus den
Referenzbildern von Pose, Gegenständen, Kontakten und Umgebung. Der Renderauftrag
wird aus diesem gespeicherten Plan, Referenzzuordnung und gefilterten
Aussehenshinweisen aus dem vorhandenen Referenzbild-Prompt gebaut.
Der ursprüngliche Bildprompt und zusätzliche Regie werden nicht daneben angehängt.

## Bedienung

1. Bildprompt, sichtbare Charaktere und eine optionale Pose-/Requisiten-Vorgabe speichern.
2. „Szenenplan mit KI erstellen“ beziehungsweise „Szenenplan aktualisieren“ wählen.
3. Den gespeicherten Plan aufklappen und Szene, Objektanzahl und Kontakte prüfen.
4. „Charaktere optimieren“ startet den Render. Dieser Schritt enthält keinen weiteren Text-KI-Aufruf.
5. Ein erzeugtes Bild erst nach visueller Prüfung mit „Auswahl übernehmen“ verwenden.

Neue vollständige Analysen planen jedes nutzbare Zitat im bestehenden
checkpoint-/budgetgesicherten Analyseablauf. „Text + Bildprompt erzeugen“ liefert
den Plan im selben Copy-Aufruf. Alte Entwürfe benötigen einmal eine explizite Planung.
Vorhandene manuelle Regie wird berücksichtigt, nicht automatisch überschrieben.

Planänderungen ersetzen keine Bilder, Videos, Carousel-Vorbereitungen oder aktive
Auswahl. Die beiden bisherigen Optimierungsverfahren bleiben verfügbar.

## Kapitel und Book-Teaser

Neue Kapitelanalysen liefern den Plan im selben vorhandenen Text-KI-Aufruf mit.
Das initiale Szenenbild verwendet den kompilierten Plan (`scene-plan-v1`), ohne
alte Bild-, Charakter- oder Regieprompts zusätzlich anzuhängen. Charakteroptimierte
Varianten verwenden danach denselben Plan mit den Identitätsreferenzen
(`reference-scene-plan-v2`). Figuren ohne Referenzportrait sind für ein normales
Szenenbild zulässig; die referenzgestützte Optimierung braucht für jede ausgewählte
Figur ein Bild.

Bei bestehenden Kapiteln öffnet „Szenenplan · Figuren & Pose“ den Editor.
Figuren und Regie speichern, dann „Szenenplan aktualisieren“. Die Sammelaktion
aktualisiert nur fehlende oder veraltete Pläne, mit einem expliziten Text-KI-Aufruf
pro Kapitel und ohne Bildjobs. Bereits gespeicherte Ergebnisse bleiben bei einem
späteren Fehler erhalten. Die Sammeloptimierung startet dagegen nur, wenn alle
betroffenen Kapitel gültige Pläne und vollständige Referenzen haben; sie plant
nicht im Hintergrund nach. Die bisherige Maskenoptimierung und der ursprüngliche
Bildprompt bleiben als auswählbare Verfahren verfügbar.

Plan- und Regieänderungen erhalten vorhandene Bilder, Videos und deren Auswahl.
Bildpromptänderungen behalten die bisherige Veraltungslogik. Ein neues Bild oder
Video wird erst durch die jeweilige explizite Renderaktion erzeugt. Alte Kapitel-
Läufe und Analyse-Retries bleiben lesbar und verwendbar.

Der finale Book-Teaser setzt Kapitelvideos zusammen und hat keinen eigenen
Charakterbild-Generator; er übernimmt die verbesserten Kapitelbilder über diese
Videos. Vorhandene fertige Videos werden nicht automatisch ersetzt.

## Quellen und Gültigkeit

- Zitat und unmittelbarer Kontext bestimmen den dargestellten Moment.
- Gespeicherte Regie konkretisiert die Inszenierung innerhalb dieses Moments.
- Buch-Art-Direction liefert nur Stil, keine konkurrierenden Szenenmotive.
- Charakterlabels ordnen Namen/Aliasse zu. Referenzbilder liefern Aussehen und Kleidung.
  Der vorhandene Referenzbild-Prompt liefert zusätzlich explizite Aussehenshinweise
  pro Figur, etwa Körperbau, Haut, Hörner, Flügel und Kleidung. Eine konservative
  lokale Filterung entfernt Referenzpose, Requisiten und Hintergrund/Stil; unsichere
  oder bedingte Transformationsangaben werden nicht zu Pflichtmerkmalen. Es gibt
  weder ein zweites Beschreibungsfeld noch einen zusätzlichen Text-KI-Aufruf.

Ein SHA-256-Fingerprint bindet den Plan an Zitat, Bildprompt, Regie, Buchstil und
ausgewählte Figuren einschließlich Revision und Referenzbildhash. Änderungen an
Prompt, Regie oder Auswahl entfernen den Plan; Änderungen an Buchstil und
Charaktersettings werden bei Anzeige und Worker-Vorprüfung als veraltet erkannt.
Die Queue übernimmt den gespeicherten Plan revisionsgesichert. Der Worker prüft
ihn einschließlich tatsächlicher Referenzdateihashes vor Upload/Generierung.
Aktive Jobs verhindern das Planspeichern innerhalb derselben Speichertransaktion.

Pläne verwenden speziesneutrale Körperteilnamen, eindeutige Objekt-IDs und genaue
Anzahlen. Doppelte Kontaktzuweisungen und freie/gehaltene Konflikte werden
abgewiesen. Gemeinsame Objekte können mehrere Figurenkontakte haben.

## Grenzen und Prüfung

Das Schema prüft Struktur und eindeutige Zuweisungen, nicht sämtliche sprachlichen
Widersprüche oder Anatomie. Synonyme wie „Hand“ und „Handfläche“ sind nicht automatisch
derselbe Körperteil. Ein gültiger Plan garantiert auch weder korrekte Finger noch
perfekte Wiedererkennbarkeit oder Requisitengeometrie des Bildmodells.

Tests prüfen Tier-, künstliche und Mehrfigurenpläne, Quellenrollen, Fingerprints,
Queue-/Revisionskonflikte, echte Workflow-Conditioning und Erhaltung ausgewählter
Medien mit gemockten Providern. Neue Bildqualität muss nach einem echten Render
visuell beurteilt werden. Die Job-Metadaten halten Plan und effektiven Renderprompt
unter `reference-scene-plan-v2` fest (ältere Läufe: `reference-scene-plan-v1`).
Die Aussehensfilterung ist eine begrenzte Sprachheuristik, keine semantische
Analyse; das Referenzbild bleibt maßgeblich und neue Ergebnisse brauchen Sichtprüfung.
