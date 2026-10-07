# Generische Charakter-Referenzbearbeitung

Die Charakterbearbeitung gilt für alle Bücher, Bildstile und Figurentypen. Sie
enthält keine Namen, Buch-IDs, Stilvorgaben oder menschlichen Gesichts-Proportionen
als Sonderregeln. Der bisherige maskierte Modus bleibt erhalten. In diesem Modus
werden ein bis vier ausgewählte Referenzen einzeln zugeordnet und bearbeitet;
unsichere bzw. stark überlappende Masken führen
zu einem Fehler, nicht zu einem automatischen Austausch fremder Figuren.

## Referenzfiguren neu inszenieren

Der zusätzliche Modus priorisiert die erkennbare Settings-Figur gegenüber einer
pixelgleichen Ausgangsszene. Er verwendet ausschließlich die Referenzbilder als
Bild-Conditioning und den gespeicherten Szenenprompt als Vorgabe für Buchstil,
Handlung und Umgebung. Das alte Szenenbild liefert nur das Ausgabeformat; seine
Pixel werden nicht hochgeladen oder in das Sampling eingebracht.

Die vollständigen Referenzbilder werden unabhängig und in eindeutiger Reihenfolge
verwendet, nicht zu einem beschnittenen Sheet zusammengesetzt. Namen und Aliasse
ordnen die Bilder den Figuren zu. Gesicht, Körperbau, Kleidung und getragene
Ausrüstung stammen aus den Referenzen; eine neue Pose und gehaltene Gegenstände
folgen der Szene. Die vollständigen Portraitprompts werden nicht angehängt, weil
sie einen konkurrierenden Stil, Hintergrund oder eine andere Handlung enthalten
können.

Für diesen Modus benötigt jeder ausgewählte Charakter ein gültiges Referenzbild.
Mehrdeutige Namen oder Aliasse werden abgelehnt. Alte Jobs ohne Strategie und
bisherige Sammelaktionen behalten ihr maskiertes Verfahren; die Medien-Werkstatt
bietet die Neuinszenierung als vorausgewählte Methode an.

Hintergrund, Kamerawinkel, Positionen und Pose können sich dabei ändern. Auch
weitere Figuren und Requisiten werden neu erzeugt; dieser Modus verspricht keinen
Schutz alter Hintergrundpixel. Wer die vorhandene Szene bewahren möchte, kann
weiterhin den maskierten Modus wählen. Neue Ergebnisse bleiben Bildkandidaten:
die aktive Auswahl für Video oder Carousel wird erst mit „Auswahl übernehmen“
geändert. Das alte Szenenbild und bisherige Dateien werden nicht gelöscht.

Der erste unterstützte Workflow ist natives Flux-2-Referenzsampling mit leerem
Latent, vollständigem Scheduler, CFG 4 und 50 Schritten. Ungeeignete oder
mehrdeutige Workflowprofile werden abgelehnt; es gibt keinen stillen Wechsel auf
einen anderen Dienst oder automatische Modelldownloads.

Job-Metadaten verwenden `identity_transfer_strategy=reference-scene-restaging-v2`
und `optimization_strategy=reference_scene`. Sie enthalten den wirksamen Prompt
und die geordnete Buchreferenz-Zuordnung inklusive Aliassen und Bildhashes. Ein
Maskenvalidierungs-Nachweis wird für die Neuinszenierung nicht behauptet.

### Pose und Requisiten präzisieren

Die Medien-Werkstatt bietet bei der Neuinszenierung ein optionales, eingeklapptes
Feld „Pose & Requisiten präzisieren“. Eine konkrete Vorgabe benennt die Haltung,
die betroffene Figur (Name oder Alias), die gewählten Gegenstände und deren
natürliche Kontaktpunkte. Sie ersetzt vage Posen oder alternative Requisiten
im Szenenprompt, nicht dessen Handlung, Umgebung oder Stil und nicht die
Referenz-Identität bzw. Kleidung. Die Anatomie folgt weiterhin der jeweiligen
Referenz, auch bei nichtmenschlichen Figuren.

Die Vorgabe ist auf 2000 Zeichen begrenzt und wird lokal am Reel-Entwurf gespeichert;
der gespeicherte Bildprompt und die aktive Bildauswahl werden nicht verändert. Sie
kann separat gespeichert oder geleert werden. Beim Optimieren werden Vorgabe und
Job-Snapshot atomar und revisionsgesichert gespeichert. Erfolgreiche Jobs
auditieren die Vorgabe zusätzlich zum tatsächlichen Renderprompt. Leere Felder
ändern bestehende Aufrufe nicht. Bei „Bestehende Szene bearbeiten“ wird eine
nichtleere Vorgabe in alten API-Aufrufen weiterhin abgelehnt. Das neue UI-Formular
bewahrt bei dieser Methode die gespeicherte Vorgabe, wendet sie aber nicht an – auch
ohne JavaScript.

Neue vollständige Zitat-Analysen (`book-analysis-v3-scene-direction`) erstellen nach
Auswahl und Deduplizierung für jedes verwendbare Zitat eine konkrete englische
Szenenregie. Die zusätzlichen KI-Aufrufe sind checkpointed und zählen zum bestehenden
persistenten Anfragebudget; strukturierte Fehler werden höchstens einmal lokal oder
extern wiederholt. Profil- und reine Charakteranalysen erzeugen keine Szenenregie.
Alte abgeschlossene Analysen bleiben lesbar, alte Zitate haben einen leeren Standardwert.
Neue Werkstatt-Entwürfe übernehmen die analysierte Vorgabe; vorhandene Entwürfe werden
beim Laden nicht überschrieben.

„Text + Bildprompt erzeugen“ liefert die Szenenregie im selben ausgewählten Text-KI-Aufruf
mit und füllt nur eine leere Entwurfsvorgabe. Für bestehende Zitate gibt es außerdem
„Pose & Requisiten mit KI erstellen“: Dieser ausdrücklich ausgelöste Auftrag ersetzt
nur die Vorgabe, nicht Bildprompt oder Medien. Eine zwischenzeitliche manuelle Änderung
verhindert das Speichern einer verspäteten KI-Antwort. Laden und Speichern allein
lösen keine KI-Aufrufe aus. Keine neue KI-Verbindung oder automatische Modelldownloads
werden eingerichtet.

Die Text-KI erhält Originalzitat und unmittelbaren Kontext, optional nachrangige
Bildprompt-Hinweise und Namens-/Alias-Zuordnungen. Sie bekommt keine Portraitbilder,
keine globalen Buchfakten und keine Charakter-Aussehensbeschreibungen. Haltung und
eindeutige Requisitenkontakte müssen zum belegten Moment passen; unbelegte Waffen,
Alternativen-Mischungen, menschliche Anatomie bei anderen Spezies sowie Stil- oder
Kleidungsänderungen sind untersagt. Der Planer kennt deshalb keine konkrete Referenzpose;
auch diese neue Textplanung garantiert keine erfolgreiche visuelle Entkopplung.

Die Vorgabe erhöht die Steuerbarkeit, garantiert aber keine fehlerfreie Pose oder
Requisite. Ein referenzbildanalysierender lokaler Planer wird nicht vorgeschaltet: isolierte
Qwen-4B-Versuche lieferten zwar parsebares JSON, aber widersprüchliche Objekt- und
Handzuordnungen. Auch eine generativ vorbereitete Referenz entfernte zwar eine
gehaltene Requisite, behielt aber die unerwünschte Referenzpose. Beides wird nicht
als geprüfte automatische Lösung eingebaut oder als neue Settingsreferenz gespeichert.

## Rollen der Eingabebilder

- Bild 1: vorhandene Szene; maßgeblich für Bildstil und Medium, Pose, Ausdruck,
  Blickrichtung, gehaltene Szenen-Requisiten, Beleuchtung und Komposition.
- Bild 2: Referenz der jeweiligen Figur; maßgeblich für deren erkennbare Identität
  und intrinsische Merkmale, Körperbau, Kleidung und getragene Ausrüstung,
  auch bei Tieren, Robotern und Fantasiewesen. Gehaltene Referenz-Requisiten und
  die Referenzpose sollen nicht übernommen werden.
- Optional Bild 3: semantisch erkanntes Kopfdetail derselben Referenzfigur. Es
  wird nur verwendet, wenn eine zuvor validierte Figurenmaske mindestens 90 %
  der Detailmaskenpixel unterstützt. Unsichere Details werden ausgelassen;
  es gibt keinen festen oberen Portraitausschnitt.
- Der ursprüngliche Referenzbild-Prompt bleibt für Maskenzuordnung und die
  Erkennung explizit ausgeschlossener Merkmale relevant. Er wird nicht erneut an
  den Identitäts-Edit-Prompt angehängt: Er kann einen abweichenden Stil, Hintergrund
  oder eine andere Pose beschreiben.

Diese Trennung folgt den Empfehlungen zu eindeutigen Bildrollen und gezielter
Merkmalsübertragung in den [BFL Multi-Reference-Hinweisen](https://github.com/black-forest-labs/skills/blob/master/skills/flux-image-best-practices/rules/multi-reference-editing.md).

## Sampling und Schutz der Szene

Der maskierte Identitätsschritt verwendet natives Referenz-Editing: ein leeres
`EmptyFlux2LatentImage` als Sampling-Ausgangspunkt und den vollständigen
`Flux2Scheduler`-Verlauf. Die Szene bleibt über `ReferenceLatent` als Bild 1 im
Conditioning. Das entspricht dem mit ComfyUI ausgelieferten Workflow
`image_flux2_klein_image_edit_9b_base.json`; ein Denoise-Wert von 0,42 wird für diesen
Schritt nicht mehr als Obergrenze verwendet.

Zurück in die Szene werden ausschließlich Pixel innerhalb der validierten,
weich auslaufenden Zielmaske übernommen. Figuren werden sequenziell bearbeitet;
vorangegangene Bearbeitungen und nicht betroffene Bildbereiche bleiben außerhalb
der nächsten Maske erhalten. Die Maske wird nicht zusätzlich erweitert, weil
nicht ausgewählte Nachbarfiguren nicht separat lokalisiert werden. Starke
Änderungen der Körperproportionen sind deshalb begrenzt. Identität, Pose,
Kleidung und Stil innerhalb der Maske werden durch Bild-Conditioning und den
Edit-Prompt angeleitet; dies ist keine Garantie perfekter Übereinstimmung.

Der Zusatzschritt zur Entfernung ausgeschlossener Merkmale bleibt erhalten und
ist unabhängig von der Identitätsübertragung. Seine Merkmalsmasken sind kein
semantischer Beweis, dass ein Merkmal tatsächlich vorhanden ist.

Erfolgreiche Optimierungen speichern die Job-Metadaten
`identity_transfer_strategy=reference-appearance-with-semantic-detail-v2`. Vorhandene
Bildvarianten, Videos und die aktive Auswahl werden nicht durch eine Codeänderung
oder einen fehlgeschlagenen Lauf ersetzt. Laufende Medien-Worker benötigen einen
Neustart, damit der neue Code für künftige Optimierungen gilt.

## Verifikation

Automatisierte Tests prüfen den tatsächlichen Sampling-Graph, umbenannte Node-IDs,
Referenzzuordnung, sequenzielle Bearbeitung, unveränderte Pixel außerhalb der Maske,
unveränderte Quelldateien sowie Menschen, Tiere und künstliche Figuren mit
verschiedenen Referenzstilen. Sie prüfen keine vom Modell garantierte visuelle
Ähnlichkeit. Die Bildqualität muss nach einem neuen Lauf visuell beurteilt werden.
Ein technisch erfolgreicher Job oder grüne Tests sind kein Nachweis, dass Gesicht,
Körperbau, Kleidung und Szene-Pose korrekt übertragen wurden.

Die kontrollierten lokalen Vergleiche zeigen: Kurze Rollenprompts, eine zusätzliche
Detailreferenz und eine geänderte Referenzreihenfolge allein lösen die Konkurrenz
zwischen alter Szenenidentität und Settingsreferenz nicht zuverlässig. Die
Neutralisierung der falschen Ausgangsfigur verbessert die Wiedererkennbarkeit,
kann aber die Referenzpose und gehaltene Gegenstände kopieren. Diese Versuche
sind isolierte Diagnoseausgaben, keine automatisch ausgewählten App-Bilder.

Ein isolierter Referenz-zuerst-Lauf mit Szenentext hat die Wiedererkennbarkeit und
Kleidung deutlich besser erhalten als die Bearbeitung der falschen Ausgangsfigur.
Er zeigte aber auch fehlerhafte gehaltene Gegenstände. Auch beim neuen Modus
bleiben Requisiten, Pose und Ähnlichkeit visuell zu prüfen; technische Tests
garantieren keine fehlerfreie Bildgenerierung.

Ein zweites Buch bestätigte die Richtung mit einer anderen Figur und einer neuen
Lesepose. Materialdetails und Zubehör können trotzdem abweichen. Die generische
Zuordnung mehrerer Figuren ist durch Graph- und Worker-Tests abgesichert, nicht
bereits über sämtliche Bücher, Medien und Mehrfigurenbilder visuell bestätigt.

Ein weiterer kontrollierter Einzelcharaktervergleich mit fester Zufallszahl zeigte:
Ein vollständig neu formulierter, kurzer Szenenauftrag mit konkreten Armpositionen
und genau einem Schwert vermied die Taschenhand und die Pistolen-/Klingenmischung.
Der tatsächliche Renderpfad mit dem alten, mehrdeutigen Szenenprompt plus zusätzlicher
Regievorgabe änderte zwar die Haltung, erzeugte aber erneut zusätzliche Waffen und
Referenz-Requisitenreste. Das optionale Regiefeld ist deshalb eine Steuerungsmöglichkeit,
kein belegter automatischer Fix für Referenzpose oder Requisiten. Die bessere kurze
Inszenierung ist ein isoliertes Testbild; sie ersetzt weder App-Bildkandidaten noch
eine ausgewählte Version.

Ein weiterer isolierter Versuch zerlegte eine Referenz in semantisch maskierte
Gesichts-, Oberbekleidungs- und Unterbekleidungsdetails. Die automatische Handmaske
markierte auch Ärmel; die verwendeten Ausschnitte mussten daher manuell visuell
geprüft werden. Der Render vermied Taschenhand und gehaltene Referenzpistole,
erzeugte aber weiterhin unplausibel gemeinsam gegriffene Klingen. Diese Aufbereitung
ist kein generischer Produktionsfix und wird nicht in App-Referenzen oder ausgewählte
Bildkandidaten übernommen. Zusätzlich ist ihr Drei-Referenzen-Aufwand für mehrere
Figuren unter dem Vier-Referenzen-Limit ungeeignet.
