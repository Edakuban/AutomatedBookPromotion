# Buchanalyse und Originalzitate (Schritt 8)

## Ablauf

Die Buchseite startet die Analyse ausdrücklich über `POST /books/local/<book-id>/analyze`. Der Start verlangt eine vorhandene, prüffreie Extraktion sowie Open-WebUI-Konfiguration. Upload, Seitenaufruf und Textextraktion starten keine KI-Generierung. `GET /books/local/<book-id>/analysis-status` liefert bereinigte Statusmetadaten ohne Buchtexte, Token oder Zugangsdaten.

Der vorhandene Hintergrund-Worker verarbeitet Import und Analyse nacheinander. `analysis.py` enthält die unabhängige Analyselogik, `analysis_store.py` die lokale Persistenz und `analysis_worker.py` die HTTP-/Worker-Anbindung.

1. Kapitel in überlappende Textabschnitte teilen und jeden Abschnitt zusammenfassen. Die maximale Abschnittsgröße ist zeichenbasiert, kein Tokenlimit; 800 Zeichen Überlappung sichern Fundstellen an Abschnittsgrenzen.
2. Lange Kapitelzusammenfassungen in Gruppen von höchstens sechs verdichten. Anschließend den Kontext aller Kapitel mehrstufig auf eine begrenzte Eingabe für das Buchprofil reduzieren.
3. Ein internes Profil mit Zusammenfassung, Genre, Stimmung, Welt, Figuren, Spoilern sowie Bildprompt- und Caption-Vorschlägen erzeugen. Diese internen Texte können das Buchende verraten und sind in der Oberfläche entsprechend gekennzeichnet.
4. Mit dem fertigen Gesamtkontext je Textabschnitt bis zu acht Kandidaten auswählen und strukturiert bewerten lassen.
5. Kandidaten im Original prüfen, Duplikate bereinigen, je Kapitel begrenzen und Ergebnis atomar speichern.

Alle Buchtexte, Titel und Zusammenfassungen werden im Systemprompt ausdrücklich als Daten behandelt. Der Client fordert keine Werkzeuge an. Antworten müssen den begrenzten Pydantic-Schemata entsprechen. Ungültige Struktur oder unzulässige Quellzuordnung erlauben höchstens einen zusätzlichen fachlichen Versuch für den jeweiligen Arbeitsschritt.

## Was als Originalzitat gilt

Ein Kandidat umfasst 20–800 Zeichen und benennt den Absatz seines Anfangs. Er muss im angegebenen Abschnitt ab diesem Absatz genau einmal wortgetreu vorkommen. Zitate dürfen in nachfolgende Absätze reichen. Auslassungen, geänderte Interpunktion und erfundene Wörter bestehen die Prüfung nicht. Gespeichert wird der Ausschnitt aus dem tatsächlichen Kapiteltext, mit stabiler ID, exklusiven Zeichenpositionen, betroffenen Absatz-IDs und je bis zu 200 Zeichen Kontext davor und danach.

Bewertungen von 1–5 erfassen Verständlichkeit, Neugier, Emotionalität und Bildhaftigkeit. Geeignet ist ein Zitat, wenn Durchschnitt und Verständlichkeit den konfigurierten Mindestwert erreichen und die Spoilerstufe nicht hoch ist. Bei widersprüchlichen Bewertungen gleicher oder stark überlappender Stellen hat die hohe Spoilerstufe Vorrang. Identische, normalisiert gleiche und zu mindestens 50 Prozent der kürzeren Stelle überlappende Zitate werden bei der Auswahl bereinigt. Standardmäßig werden höchstens zwölf Zitate je Kapitel gespeichert. Gültige, aber ungeeignete Kandidaten können mit ihrer Einstufung sichtbar bleiben.

Die Originaltreue ist durch den Quellvergleich abgesichert. Literarische Eignung, Vollständigkeit der Zusammenfassung und Spoilererkennung sind Modellbewertungen und können fehlerhaft sein. Der Test mit einem kurzen synthetischen Buch ersetzt keine Prüfung eines vollständigen Manuskripts.

## Speicherung, Wiederaufnahme und Änderungen

Die private `uploads.sqlite3` enthält `local_analysis_runs` und `local_analysis_checkpoints`. Ein Lauf speichert einen unveränderlichen Extraktionssnapshot, dessen Revision, Modell-ID, Analyseoptionen, Promptversion, einen Hash des Endpunkts, Status und Anfragezähler. Zugangsdaten und die Endpunktadresse werden dort nicht gespeichert.

Der Fingerabdruck umfasst Buch, Quellrevision, Modell, Optionen, Promptversion und Endpunkt. Ein Doppelklick vervielfacht einen aktiven Lauf nicht. Derselbe abgeschlossene Lauf wird wiederverwendet; ein fehlgeschlagener Lauf mit gleichem Fingerabdruck nutzt seine validierten Zwischenstände weiter. Geänderte Einstellungen starten einen neuen Lauf. Das beim Start ausgewählte Modell bleibt für diesen Lauf fest; aktuelle Credentials werden erst bei der Bearbeitung geladen. Ein anderer Endpunkt oder eine andere Promptversion verhindert dessen Fortsetzung.

Ein zeitlich begrenzter Bearbeitungsanspruch mit zufälligem Token schützt Checkpoints und Abschluss. Der Worker erneuert ihn alle zwei Sekunden; nach 30 Sekunden ohne Erneuerung kann ein Nachfolger übernehmen. Nach drei unterbrochenen Versuchen wird ein manueller Neustart verlangt. Laufende HTTP-Arbeit wird bei verlorenem Anspruch oder Stoppsignal abgebrochen; bereits beim Anbieter gestartete Generierung kann trotzdem Kosten verursacht haben.

Zwischenstände werden pro Abschnitt, Verdichtung und Profil gespeichert. Ein Abbruch wiederholt nur noch nicht abgeschlossene Schritte. Ein Ergebnis erscheint erst nach vollständigem Abschluss. Der Zustand `empty` bedeutet ausdrücklich: Analyse abgeschlossen, aber keine geeigneten Zitate. Kapitelkorrekturen markieren ältere Quellrevisionen atomar als `stale`, entziehen laufenden Aufträgen ihren Anspruch und blenden ihre Ergebnisse als aktuellen Stand aus.

`ANALYSIS_MAX_CALLS` begrenzt logische KI-Anfragen einschließlich fachlicher Wiederholungen für den gesamten Lauf. Der Zähler wird vor dem Aufruf gespeichert und bei Wiederaufnahme nicht zurückgesetzt. Zusätzliche HTTP-Versuche des Clients sind separat durch `OPENWEBUI_MAX_RETRIES` begrenzt und können die Anzahl tatsächlicher HTTP-Anfragen erhöhen. Ein ausgeschöpftes Budget lässt sich nicht durch erneutes Anklicken zurücksetzen. Ein höheres konfiguriertes Budget ergibt einen neuen Fingerabdruck und somit einen neuen Lauf.

## Verifikation und nächste Schritte

Die vollständige Testsuite umfasst nach Schritt 8 143 erfolgreiche Tests. Die neuen Fälle prüfen unter anderem Quellfälschungen und Mehrdeutigkeit, Spoilerkonflikte, Überlappungen, mehrstufige Zusammenfassung, begrenzte Wiederholungen, persistente Budgets, Wiederaufnahme, veraltete Ansprüche und Quellrevisionen, Webrouten sowie einen separaten Worker gegen eine lokale Test-API.

Der zusätzliche Live-Test an der konfigurierten Open-WebUI-Instanz analysierte einen eigens erstellten Text mit zwei Kapiteln: fünf logische Anfragen, fünf gespeicherte Zwischenstände und vier geeignete Originalzitate. Alle vier wurden erneut gegen ihre Quellposition geprüft. Es wurde kein Nutzerbuch analysiert und nichts veröffentlicht.

Profilbearbeitung und Zitatverwaltung sind seit Schritt 9 implementiert; siehe [book-management.md](book-management.md). Manuelle Einstellungen und Zitatsperren liegen separat und werden durch die Analyse nicht überschrieben. Supabase-Schemaeinrichtung (3.2) und ausdrückliche Übernahme fertiger lokaler Buchstände sind inzwischen umgesetzt; siehe [Supabase-Integration](supabase-integration.md). Der [n8n-Export](../n8n/README.md) ist für Import und kontrollierten Gesamttest vorbereitet.
