# Buch- und Zitatverwaltung (Schritt 9)

## Bedienung

Unter **Buch → Bucheinstellungen bearbeiten** stehen Titel, Autor, Zieladresse, Freigabemodus, Carousel-Aktivierung, Genre, Stimmung, interner Buchkontext, Bildprompt-Basis, Caption-Vorgaben und der Text für die Carousel-Schlussseite. Die Einstellungsseite wird nicht automatisch neu geladen. Das Formular kann auch vor der KI-Analyse verwendet werden.

## Bild-Overlay

Die Einstellungen enthalten zusätzlich eine Auswahlliste der lokal installierten Schriftarten und eine Titelfarbe. Beim Speichern erzeugt das Tool ein transparentes PNG im Instagram-Hochformat 1080×1350 (4:5): Buchtitel links oben, mit leichtem Schatten für wechselnde Bildhintergründe. Der Kapitelname gehört bewusst nicht in diese Datei; n8n ergänzt die gespeicherte Kapitelposition rechts unten als `Kapitel X` in einer normalen Schrift. Ohne Schriftart bleibt das Overlay deaktiviert.

Die PNG-Vorschau liegt nur in der lokalen Buchablage. Beim nächsten **Buchstand nach Supabase übertragen** rendert das Tool sie erneut und lädt sie in den privaten Bucket `book-promotion-assets`. Im Buchprofil steht `overlay_path`; n8n muss die Datei mit seinem Supabase-Credential laden. Der Bucket ist nicht öffentlich und enthält keine Buchtexte.

## Carousel-Schlussseite

Im Bereich **Carousel-Schlussseite** werden Frontcover und Logo getrennt von den Texteinstellungen hochgeladen. Erlaubt sind PNG, JPEG und WebP bis 10 MB. Pillow prüft den tatsächlichen Dateiinhalt, Pixelzahl und Mindestgröße, wendet die EXIF-Ausrichtung an und normalisiert gültige Dateien intern nach PNG. Für Logos empfiehlt sich ein transparentes PNG oder WebP; JPEG besitzt keinen transparenten Hintergrund. Jeder Upload besitzt eine eigene Revision, sodass ein veraltetes Browserfenster kein neueres Asset überschreiben kann.

Aus dem Frontcover rendert Python einen optischen 2.5D-Buch-Mockup mit nahezu vollständig sichtbarer Front, leichter Perspektive, schmaler Papierkante rechts und unten sowie Schlagschatten. Die vollständige Schlussseite kombiniert diesen Mockup rechts mit dem frei gepflegten CTA-Text links, dem bestehenden Buchtitel-Overlay und dem Logo rechts unten. Der CTA-Text steht ohne Kasten frei auf dem dunklen Hintergrund; eine schmale vertikale Linie in der Titelfarbe verbindet ihn optisch mit der Covergestaltung. Der Text verwendet fest Arial, und überlange Einzelwörter erhalten beim Umbruch einen sichtbaren Trennstrich. Das Ergebnis ist ein RGB-JPEG mit exakt 1080×1350 Pixeln und höchstens 8 MiB. Der Digest berücksichtigt Cover, Logo, CTA-Text, Titel-Overlay, CTA-Schriftdatei und Renderer-Version; unveränderte Vorschauen werden aus dem lokalen Cache geladen.

**Instagram-Carousel verwenden und für Promotion aktivieren** kann nur gespeichert werden, wenn Cover, Logo, CTA-Text und Overlay-Schrift vorhanden sind und die Schlussseite tatsächlich erfolgreich gerendert wurde. Es gibt keinen Rückfall auf einen Einzelbild-Post. Der Freigabemodus wird lokal als **Telegram-Prüfung** oder **Automatisch veröffentlichen** gespeichert und beim Sync vom passenden n8n-Carousel-Workflow ausgewertet.

Ohne gespeichertes Profil erscheinen die aktuellen KI-Vorschläge als Vorbelegung. Nach dem ersten Speichern gehört das gesamte Profil der manuellen Verwaltung und bleibt bei neuen Analysen unverändert. Ein neues KI-Profil kann über **Aktuellen KI-Profilvorschlag zur Bearbeitung laden** angesehen, bearbeitet und ausdrücklich gespeichert werden. Dieser Aufruf liest nur; er ändert weder die Datenbank noch Titel, Autor, Zieladresse oder Promotion-Auswahl. Das Laden verlässt allerdings das aktuelle Formular und verwirft dessen ungespeicherte Eingaben.

Auf der Kapitelseite schaltet **Zitat sperren / Sperre aufheben** ausschließlich die manuelle Sperre um. Nutzbar bedeutet weiterhin: technisch belegte Quelle, passende KI-Einstufung und keine manuelle Sperre. Originalzitat, Quellpositionen, Scores und Spoilereinstufung sind nicht editierbar. Die Filter „Gesperrt“ und „KI: nicht geeignet“ können dasselbe Zitat enthalten, da die Kriterien unabhängig sind.

Die Übersicht zeigt die Anzahl nutzbarer und gesperrter Zitate. Auf Kapitelseiten stehen bis zu 20 Zitate je Seite, mit Originalkontext und der internen Kapitelzusammenfassung. Der vollständige Kapiteltext bleibt darunter sichtbar.

## Persistenz und Konfliktschutz

### Profilfelder mit KI erstellen

Unter **Bucheinstellungen → Profil mit KI ausfüllen** startet eine eigene Hintergrundanalyse. Sie verarbeitet den gesamten eingelesenen Buchtext abschnittsweise und verdichtet die Zusammenfassungen zu Buchkontext. Danach entstehen mit jeweils einem eigenen strukturierten Modellaufruf: interne Zusammenfassung, Genre, Stimmung, Welt und Schauplätze, Figuren, Spoilerhinweise, Bildprompt-Basis und Caption-Vorgaben. Lange Bücher benötigen zusätzlich mehrere Kontextaufrufe; es sind daher insgesamt mehr als acht Aufrufe. Das Original-DOCX wird nicht als Anhang gesendet, sondern sein zuvor eingelesener Text.

Jedes Profilfeld wird separat als Zwischenstand gespeichert. Die Ausführung verwendet das gewählte Open-WebUI-Modell, die vorhandenen Aufruflimits und die Wiederaufnahme des Analyse-Workers. Ein identischer fertiger Lauf wird wiederverwendet. Profil- und Zitat-Analyse teilen gespeicherte Kontextschritte, wenn Buchrevision, Modell, Endpunkt, Kontextprompt-Version und Abschnittsgröße übereinstimmen. Ein erneuter Profil-Lauf benötigt bei vollständigem Kontext nur acht logische Feldaufrufe, zuzüglich möglicher Validierungswiederholungen. Kapiteltexte und Aufteilung werden nie geändert. Die Profilanalyse wählt keine Zitate aus und ersetzt keine bestehende Zitat-Analyse. Für dasselbe Buch laufen Profil- und Zitat-Analyse nacheinander.

Profilprompt-Version 2 fordert kurze, vollständige Texte und genau einen vollständigen Listeneintrag pro Figur beziehungsweise Spoiler. Zeichenlimits werden dem Modell als Beschreibung mitgegeben und lokal weiterhin strikt validiert; die Generierung verwendet keine harten `maxLength`-Grenzen, die Wörter mitten im Text beenden können. Figuren haben bis zu 600 Zeichen pro Eintrag, insgesamt höchstens 6000; Spoiler bis zu 400 Zeichen pro Eintrag, insgesamt höchstens 4000. Ältere separate Profilvorschläge werden zum Neuerstellen markiert; gespeicherte manuelle Profile bleiben erhalten.

**Vorschläge in die acht Profilfelder einsetzen** ersetzt ausschließlich diese Felder im geöffneten Formular. Eigene Eingaben bleiben während der Analyse erhalten; erst der ausdrückliche Einsetzen-Klick ändert sie. Titel, Autor, Zieladresse und Promotion-Schalter bleiben manuell. **Bucheinstellungen speichern** übernimmt den bearbeiteten Vorschlag dauerhaft. Beim Seitenwechsel kann die Analyse weiterlaufen; die fertigen Vorschläge lassen sich später wieder laden. Kapiteländerungen machen frühere Vorschläge ungültig, auch zwischen Einsetzen und Speichern.

Technisch sind dies `local_analysis_runs` mit `options_json.purpose = profile`; ältere und reguläre Zitat-Analysen gelten als `full`. Beide verwenden dieselben geprüften Aufträge und Checkpoints, aber getrennte Ergebnisabfragen. Eine reine Profilanalyse schaltet keine Supabase-Übertragung oder Promotion frei; hierfür bleibt eine vollständige Zitat-Analyse erforderlich.

`management.py` verwendet die bestehende private `uploads.sqlite3`:

- `local_book_settings`: Buch-ID, Revisionsnummer, validierte Buchdaten als JSON und Änderungszeit. Der Titel wird in derselben Transaktion in `local_books` aktualisiert, damit Navigation und doppelte Uploads denselben Titel verwenden.
- `local_book_assets`: Buch-ID, Assetart (`cover_front` oder `logo`), eigene Revision, ursprünglicher Anzeigename, normalisierte Metadaten, SHA-256 und privater relativer Dateipfad. Die Tabelle entsteht erst beim ersten Bild-Upload.
- `local_quote_controls`: Buch-ID, Hash der Originalfundstelle, manuelle Sperre, Revision und Änderungszeit. Der Fundstellenhash enthält Buchversions-ID, ursprüngliche Absatz-IDs und exakten Zitattext. Modellwechsel und andere Analyseläufe verändern ihn nicht. Eine Kapitelumbenennung oder geänderte Kapitelgrenze hebt eine Sperre derselben Originalstelle nicht auf.

Lesende Aufrufe legen keine Tabellen an. Beim ersten Speichern entstehen die zusätzlichen Tabellen automatisch. Schreibaktionen laufen in einer SQLite-Transaktion und verlangen den aktuellen Formularstand. Alte Profile, überholte Analysestände, Zitate aus anderen Kapiteln und gleichzeitig veränderte Sperren werden mit einem verständlichen Konflikt abgelehnt. Sperren alter Quellen bleiben gespeichert, werden aber nur auf passende aktuelle Originalstellen angewendet.

Alle Änderungen verlangen die lokale Herkunft. Serverseitige Feldlängen und HTTP-/HTTPS-URL-Prüfung ergänzen die HTML-Formulare. Fehlerhafte Feldeingaben bleiben zur Korrektur sichtbar und werden nicht gespeichert. HTML wird bei der Anzeige maskiert. Neue API-Zugänge oder ENV-Einstellungen sind nicht nötig.

Die Tests prüfen zusätzlich PNG-/JPEG-/WebP-Uploads, EXIF-Ausrichtung, Byte- und Pixelgrenzen, sichere generierte Pfade, Assetrevisionen, atomaren Austausch, ungewöhnliche Coverformate, transparente Logos, CTA-Layout, Ausgabevalidierung, deterministische Digests, Cachetreffer und das vollständige Aktivierungs-Gate.

## Supabase und Nutzungshistorie

Änderungen werden zunächst lokal gespeichert. **Buchstand nach Supabase übertragen** übernimmt den fertig analysierten Stand einschließlich Promotion-Vormerkung, Profil und Zitatsperren. Spätere Änderungen verlangen eine erneute Übertragung. Die Kapitelseite zeigt bei aktivierter Verbindung letzte bestätigte Veröffentlichung und Reservierung sowie Filter für unbenutzte/verwendete Zitate. Fehlende oder nicht erreichbare Datensätze gelten nicht als unbenutzt. Manuell bearbeitete Profile steuern die Promotion; die interne Quellenanalyse erzeugt weiterhin einen separaten KI-Profilvorschlag. Details: [Supabase-Integration](supabase-integration.md).
