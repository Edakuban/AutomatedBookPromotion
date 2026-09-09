# DOCX-Import und Quellzuordnung

Schritt 5 verwendet `extraction.py` als von FastAPI unabhängige Importlogik. `extraction_store.py` speichert Ergebnisse vorerst in der privaten lokalen SQLite-Ablage. Es werden keine Supabase- oder KI-Aufrufe ausgeführt und keine Word-Felder, Makros oder externen Dokumentverknüpfungen ausgeführt/abgerufen.

## Word-Struktur

Der Parser liest `word/document.xml` und optional `word/styles.xml` mit DTD-/Entity-Schutz. Paketgrößen und CRC werden vor dem Einlesen geprüft; die XML-Verschachtelung ist begrenzt. Unterstützt werden die Transitional- und Strict-WordprocessingML-Namensräume. Zusätzliche XML-Dateien müssen dieselben Größenlimits einhalten.

Absätze und Textläufe werden in Dokumentreihenfolge verarbeitet. Word speichert eine Initiale unter anderem mit `w:framePr` und `w:dropCap`; die Werte `drop` und `margin` kennzeichnen entsprechende Rahmen. Siehe [Microsoft: FrameProperties.DropCap](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.frameproperties.dropcap?view=openxml-3.0.1). Diese Struktur wird ausdrücklich ausgewertet, anstatt aus der Schriftgröße auf einen zu verbindenden Buchstaben zu schließen.

Gliederungsebenen und benannte Überschriftformate werden einschließlich `basedOn`-Vererbung berücksichtigt. Kapitelnummern mit oder ohne Doppelpunkt und nachfolgende Titelzeilen werden zusätzlich über begrenzte Muster erkannt. Heuristische Zuordnungen sind keine Garantie für jedes Word-Layout: fehlende Grenzen, Vorspann, leere Kapitel oder unsichere Titelzuordnungen verlangen Prüfung. Alle erhaltenen Absätze bleiben in der Vorschau sichtbar, auch wenn sie als Überschrift aus dem Kapiteltext ausgeschlossen werden.

## Stabile Fundstellen

- Jeder physische Absatz im Hauptteil bekommt eine deterministische ID `p000001` usw. Übersprungene Absätze zählen mit. Kopf-/Fußzeilen liegen außerhalb dieses Hauptteils.
- Ein logischer Absatz enthält Text und `sources`. Bei einer verbundenen Initiale zeigt `sources` auf beide physischen Absätze. Die logische ID bleibt die des ersten Absatzes.
- `source_start`/`source_end` beziehen sich auf die vom Parser gelesene Zeichenfolge des jeweiligen physischen Absatzes vor dem äußeren Trimmen. `start`/`end` im SourceSpan beschreiben dieselbe Teilzeichenfolge im logischen Absatz. Endpositionen sind exklusiv; Positionen zählen Unicode-Zeichen wie Python-Strings, nicht UTF-16-Codeeinheiten oder XML-Bytes.
- Innere Leerzeichen, Satzzeichen, Tabulatoren und Textzeilenumbrüche bleiben erhalten. Äußere Absatzleerzeichen werden entfernt; Initiale und Folgewort ohne zusätzliches Leerzeichen verbunden. Es erfolgt keine stilistische Überarbeitung oder Silbentrennungsreparatur.
- `chapters.source_text` enthält die Inhaltsabsätze mit jeweils zwei Zeilenumbrüchen als Trennzeichen. Die Kapitel-Absatzliste enthält deren `start`/`end` in diesem Text und die ursprünglichen Sources. Überschriftabsätze liegen weiterhin im vollständigen Extraktionssnapshot.
- Kapitel-IDs sind UUIDv5 aus Buchversions-ID und Anfangsabsatz. Umbenennen ändert die ID nicht; ein anderer Kapitelbeginn erhält eine andere ID. Originaldateihash und Extraktorversion bleiben nachvollziehbar.

## Grenzen und Prüfhinweise

Strukturell erkennbare Inhaltsverzeichnisse (TOC-Felder, entsprechende Formatvorlagen oder Inhaltssteuerelemente), Leerabsätze und Kopf-/Fußzeilen sind kein Zitatmaterial. Ein manuell geschriebenes Inhaltsverzeichnis oder bloß optisch gestaltete Überschriften können eine Korrektur benötigen.

Textfelder mit Inhalt, unbekannte alternative Objekt-Repräsentationen, eingebettete Inhalte, Tabellen, offene Änderungsverfolgung, Formeln, direkt ausgeblendete Textläufe, Fuß-/Endnotenverweise und Schriftartsymbole führen zu Prüfhinweisen mit Sperre. Bei Tabellen wird der Text vorläufig zeilenweise gelesen; bei Änderungen wird nur der angenommene Endstand als Vorschau gelesen. Nicht unterstützter Inhalt ist keine belastbare Zitatquelle. Ein bestätigtes Kapitelraster entfernt diese Sperren nicht; die bereinigte Word-Datei muss neu importiert werden.

Seit Extraktor v2 werden alternative Grafikdarstellungen ohne Text, Felder oder sonstige texttragende Inhalte wie Grafiken behandelt. Leere Zeichenobjekte blockieren damit keine Analyse. Texte innerhalb einer Grafik werden weiterhin nicht per OCR erkannt.

Die Nummerierungsdefinitionen aus `word/numbering.xml` werden einschließlich expliziter Ebenenüberschreibungen geprüft. Bekannte einfache Aufzählungszeichen werden ausgelassen, ihr Listentext bleibt erhalten. Automatische Nummern in ausgelassenen Kapitelüberschriften blockieren nicht; automatische Nummern im verwendeten Buchtext oder unbekannte Listenmarker bleiben prüfpflichtig. Der Hinweis nennt betroffene Absatznummern. Eine geänderte Kapitelaufteilung bewertet diese Zuordnung erneut. `numId=0` hebt eine geerbte Nummerierung auf. Grundlage: [Microsoft: NumberingId](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.numberingid).

Grafiken werden ohne OCR ausgelassen und mit einem Hinweis gemeldet. Schriftgröße, visuelle Seitenanordnung und automatisch erzeugte Listennummern werden nicht rekonstruiert. Eine Randinitiale mit unklarem Anschluss bleibt sichtbar zur Prüfung stehen. Die Tests verwenden synthetische DOCX-Pakete; die konkrete Word-Datei des Nutzers muss noch in der Vorschau geprüft werden.

## Speicherung und Wiederholung

`local_extractions` enthält pro Buch einen vollständigen Snapshot oder eine verständliche Fehlermeldung. Seit Schritt 6 berechnet der separate Worker den Text außerhalb einer Schreibtransaktion, damit Statusabfragen und weitere Uploads bedienbar bleiben. Snapshot und abgeschlossener Auftrag werden gemeinsam atomar gespeichert. Absturz vor Commit hinterlässt keinen halben Textstand. Der Worker nimmt abgebrochene Aufträge erneut auf; Details stehen in [import-worker.md](import-worker.md).

Vor der erstmaligen Extraktion wird die Originaldatei gegen den beim Upload gespeicherten Hash geprüft. Bestehende erfolgreiche Snapshots werden bei doppeltem Upload wiederverwendet. Korrekturen prüfen eine Revision, damit ein altes Browserformular keine neueren Änderungen überschreibt. Kapitelraster sind zusammenhängend, decken alle erhaltenen Absätze ab und dürfen nur ihre ausdrücklich ausgewählten Überschriftabsätze auslassen.

Die KI-Analyse aus Schritt 8 prüft `needs_review` und lehnt blockierte Stände ab. Eine nachträgliche Kapitelkorrektur markiert vorhandene Analysen derselben Buchversion atomar als veraltet; deren Zitate werden bis zur erneuten Analyse nicht als aktuelles Ergebnis angezeigt. Die Extraktion selbst erzeugt weder Zitate noch veröffentlichbare Buchversionen.
