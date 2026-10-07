# Globale KI-Auswahl und drei Bildwege

Stand: 05.10.2026. Gilt für die Hauptaktionen bei Zitaten und Kapitel-Teasern.

## Bedienung

Unter Einstellungen → KI & Modelle wird das feste Bild-Preset und die Text-KI
global ausgewählt. Änderungen gelten für neue Aufträge ohne Backend-Neustart.
Qwen bleibt lokal mit dem in ENV konfigurierten Modell. Das externe Textmodell
wird über die vorhandene Open-WebUI-Modellliste in demselben Bereich gewählt.

Beide Inhaltsarten verwenden dieselben drei Buttons:

| Aktion | Modell-Eingaben |
| --- | --- |
| Bild erzeugen | Szenenplan und Buchstil; keine zusätzlich angehängten Charakter-Imageprompts oder Referenzbilder |
| Bild mit Beschreibung erzeugen | Szene/Stil plus gefilterte Aussehensmerkmale der ausgewählten Charakter-Imageprompts |
| Bild mit Referenzbild erzeugen | Zunächst Szene/Stil, danach je ausgewählter Figur ein Edit mit Szenenbild, Referenzbild und Namens-/Positionszuordnung; keine zusätzlichen Aussehensbeschreibungen |

Ein gültiger aktueller Szenenplan wird wiederverwendet; sonst erzeugt die global
gewählte Text-KI einmal einen Plan. Szene, Charakterauswahl und optionale Regie
werden mit dem Bildklick gespeichert. Im Referenzweg bestimmt die Szene die
Kleidung, Pose und den Ausdruck, nicht das Referenzporträt. Bis zu vier Figuren
werden nacheinander bearbeitet. Es ist kein vorhandenes Szenenbild erforderlich.

## Feste Bild-Presets

| Preset | Modell | Encoder | VAE | Sampling |
| --- | --- | --- | --- | --- |
| FLUX.2 Klein 4B Distilled | flux-2-klein-4b-fp8.safetensors | qwen_3_4b.safetensors | flux2-vae.safetensors | Euler, 4 Schritte, CFG 1 |
| FLUX.2 Klein 9B Base | flux-2-klein-base-9b-fp8.safetensors | qwen_3_8b_fp8mixed.safetensors | full_encoder_small_decoder.safetensors | Euler, 50 Schritte, CFG 4 |

9B bleibt die Vorgabe; es wurde keine neue Präferenz für den Benutzer gespeichert.
Die Pakete sind nicht frei mischbar. Fehlen Modelldateien, bricht der neue Auftrag
vor der optionalen Text-KI-Planung ab. Kein stiller Modell-/Provider-Fallback.
Der Prüfbutton liest nur die in ComfyUI angebotenen Modelldateien.

### 9B-Konditionierung, Version 2

Neue 9B-Aufträge verwenden in der Grundszene und jedem Referenz-Edit einen
leer encodierten Negativtext (`CLIPTextEncode`, Text `""`) statt
`ConditioningZeroOut`. Die Referenz-Latents werden weiterhin an beide Zweige
angehängt. Modellpaket, Euler, 50 Schritte und CFG 4 bleiben unverändert.
4B Distilled behält den bisherigen Null-Zweig mit CFG 1.

Ein kontrollierter Café-Test mit identischem Prompt, Referenzbild und Seed
reproduzierte den alten grellen Look pixelgenau; allein der geänderte 9B-Zweig
verbesserte die Belichtung vor und nach dem Referenz-Edit deutlich. Das ist
kein Nachweis zuverlässiger Charakteridentität oder einer Lösung aller
Bildqualitätsprobleme. Eingefrorene 9B-Aufträge der Version 1 behalten ihre
ursprüngliche Verschaltung; neue Aufträge erhalten Version 2.

Der anschließende 4B-Café-Test verwendete denselben Auftrag, dasselbe Referenzbild
und denselben Seed, aber das unveränderte 4B-Paket mit 4 Schritten und CFG 1.
Null-Zweig und leerer Negativtext lieferten in Grundszene und Endbild pixelgenau
identische, normal belichtete Ergebnisse. Dieser konkrete 9B-Fehler trat dort
nicht auf; daraus folgt keine allgemeine Garantie für die 4B-Bildqualität.

## Erhaltung und Wiederaufnahme

- Aufträge speichern das ganze Bild-Preset und die gewählte Text-KI mit Modell-ID.
  Spätere globale Änderungen verändern einen bereits erteilten Bildauftrag nicht.
- Szene und alle Referenz-Edits laufen in einem nativen ComfyUI-Graph mit einer
  Prompt-ID. Nach Wiederaufnahme wird diese ID abgefragt, kein neuer Graph erzeugt.
- Vorhandene Varianten, Bild-/Videoauswahl, Uploads, Carousel-Ausschnitte,
  Publishing und bisherige erweiterte Bildverfahren bleiben erhalten.
- Alte Aufträge ohne Preset behalten ihre bisherigen Verfahren. Die optionalen
  Legacy-Aktionen und die separate Charakterporträt-Erzeugung nutzen weiterhin
  ihre ENV-Workflows; das neue Preset gilt für die drei Hauptaktionen und neue
  automatisch erzeugte Kapitel-Szenenbilder.
- Die automatische Kapitelanalyse erzeugt zunächst ein normales Szenenbild mit
  dem beim Start festgehaltenen Preset. Beschreibung/Referenz sind bewusst
  auswählbare zusätzliche Aktionen pro Kapitel, keine automatische Mehrfachserie.

## Prüfung und Grenzen

Automatisierte Prüfungen decken beide Quellen, drei Modi, feste Modellpakete,
Revisionen, unveränderte Auswahlen und Wiederaufnahme ab. Die Browser-Sichtprüfung
zeigt die drei Aktionen bei Zitaten und Kapiteln sowie die globale Auswahl.
Ein isolierter Live-Test des 4B-Graphs mit Szene plus zwei Referenz-Edits lief in
rund 13 Sekunden durch. Er veränderte keine Buchdaten oder ausgewählten Medien.

Dies ist **keine bestandene visuelle Identitätsabnahme**: Im Live-Test blieb Lys'
nichtmenschliche Form unvollständig. Auch Anatomie, Kleidung und die unveränderte
erste Figur im zweiten Edit sind Modellziele, keine garantierten Eigenschaften.
Die Imagegen-Skill-Prüfkriterien halten Modellfunktion und Bildqualität getrennt;
die gewollten lokalen ComfyUI-Presets wurden nicht durch einen Cloud-Dienst ersetzt.
