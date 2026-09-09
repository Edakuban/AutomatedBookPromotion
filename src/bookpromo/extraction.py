"""Deterministic DOCX extraction with source coordinates, without executing fields.

Read WordprocessingML directly so framed initials and Strict OOXML retain the
same source model. No Word application, external relationships or AI are used.
"""

from pathlib import Path
import re
from uuid import UUID, uuid5
import zipfile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, Field

from .uploads import UploadError, validate_docx

EXTRACTOR_VERSION = 2
NUMBER_PREFIX = r"(?:Kapitel|Chapter)(?:\s*:\s*|\s+)(?:\d+|[IVXLCDM]+)"
CHAPTER = re.compile(r"^" + NUMBER_PREFIX + r"(?:\s*[.:–—-]\s*[^\n]+|\s*\n[^\n]+)?[.:]?\s*$", re.I)
NUMBER_HEADING = re.compile(r"^" + NUMBER_PREFIX + r"[.:]?\s*$", re.I)
SPECIAL = re.compile(r"^(?:Prolog|Epilog|Prologue|Epilogue)(?:\s*[:–—-]\s*.+)?$", re.I)


class SourceSpan(BaseModel):
    paragraph_id: str
    source_start: int
    source_end: int
    start: int
    end: int


class Paragraph(BaseModel):
    id: str
    text: str
    sources: list[SourceSpan]
    outline: int | None = None
    drop_cap: bool = False


class Warning(BaseModel):
    code: str
    message: str
    blocking: bool = False


class Boundary(BaseModel):
    paragraph_id: str
    title: str = Field(min_length=1, max_length=200)
    heading_count: int = Field(default=0, ge=0, le=2)


class Chapter(BaseModel):
    id: str
    position: int
    title: str
    source_text: str
    paragraphs: list[dict]


class Extraction(BaseModel):
    extractor_version: int = EXTRACTOR_VERSION
    version_id: str
    paragraphs: list[Paragraph]
    boundaries: list[Boundary]
    chapters: list[Chapter]
    warnings: list[Warning]
    reviewed: bool = False
    numbered_paragraph_ids: list[str] = Field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return any(w.blocking or (w.code.startswith("chapter_") and not self.reviewed) for w in self.warnings)


def make_chapters(version_id: str, paragraphs: list[Paragraph], boundaries: list[Boundary]) -> list[Chapter]:
    positions = {p.id: i for i, p in enumerate(paragraphs)}
    starts = [positions.get(b.paragraph_id, -1) for b in boundaries]
    if not boundaries or starts[0] != 0 or starts != sorted(set(starts)) or -1 in starts:
        raise UploadError("Kapitel müssen in Textreihenfolge stehen und mit dem ersten Absatz beginnen.")
    chapters = []
    for index, boundary in enumerate(boundaries):
        end = starts[index + 1] if index + 1 < len(starts) else len(paragraphs)
        content_start = starts[index] + boundary.heading_count
        if content_start >= end:
            raise UploadError("Jedes Kapitel braucht mindestens einen Textabsatz nach seiner Überschrift.")
        text, mapped = "", []
        for paragraph in paragraphs[content_start:end]:
            if mapped:
                text += "\n\n"
            offset = len(text)
            text += paragraph.text
            mapped.append({"id": paragraph.id, "start": offset, "end": len(text),
                           "sources": [s.model_dump() for s in paragraph.sources]})
        chapters.append(Chapter(id=str(uuid5(UUID(version_id), boundary.paragraph_id)),
            position=index + 1, title=boundary.title.strip(), source_text=text, paragraphs=mapped))
    return chapters


def apply_boundaries(result: Extraction, boundaries: list[Boundary]) -> Extraction:
    chapters = make_chapters(result.version_id, result.paragraphs, boundaries)
    return review_numbering(result.model_copy(update={"boundaries": boundaries, "chapters": chapters, "reviewed": True}))


def review_numbering(result: Extraction) -> Extraction:
    if result.extractor_version < 2:
        return result  # Old snapshots do not identify affected paragraphs.
    warnings = [w for w in result.warnings if w.code != "numbering"]
    included = {s["paragraph_id"] for c in result.chapters for p in c.paragraphs for s in p["sources"]}
    affected = sorted(set(result.numbered_paragraph_ids) & included)
    if affected:
        places = ", ".join(str(int(pid[1:])) for pid in affected[:8])
        warnings.append(Warning(code="numbering", blocking=True, message=
            f"Automatische Word-Nummerierung im Buchtext (Absatz {places}). Die erzeugten Nummern fehlen im Text. Bitte in Word in normalen Text umwandeln oder betroffene Kapitelüberschriften über die Aufteilung auslassen."))
    elif result.numbered_paragraph_ids:
        warnings.append(Warning(code="numbering", message="Automatische Nummern betreffen nur ausgelassene Absätze, etwa Kapitelüberschriften. Der Kapiteltext ist davon nicht betroffen."))
    return result.model_copy(update={"warnings": warnings})


def extract_docx(path: Path, version_id: str, progress=None) -> Extraction:
    validate_docx(path)
    try:
        with zipfile.ZipFile(path) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"), forbid_dtd=True)
            styles = None
            numbering = None
            if "word/styles.xml" in archive.namelist():
                if archive.getinfo("word/styles.xml").file_size > 20 * 1024 * 1024:
                    raise UploadError("Die Word-Formatvorlagen sind zu groß.", 413)
                styles = ElementTree.fromstring(archive.read("word/styles.xml"), forbid_dtd=True)
            if "word/numbering.xml" in archive.namelist():
                if archive.getinfo("word/numbering.xml").file_size > 20 * 1024 * 1024:
                    raise UploadError("Die Word-Nummerierung ist zu groß.", 413)
                numbering = ElementTree.fromstring(archive.read("word/numbering.xml"), forbid_dtd=True)
    except (ElementTree.ParseError, DefusedXmlException):
        raise UploadError("Die Word-Formatvorlagen sind beschädigt. Bitte neu als DOCX speichern.") from None
    return _extract(document, styles, version_id, progress, numbering=numbering)


def _extract(document, styles, version_id: str, progress=None, *, numbering=None) -> Extraction:
    for root in (document, styles, numbering):
        if root is None: continue
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > 100:
                raise UploadError("Die Word-Struktur ist zu stark verschachtelt. Bitte vereinfacht als DOCX speichern.")
            stack.extend((child, depth + 1) for child in node)
    ns = document.tag.split("}")[0] + "}"
    q = lambda name: ns + name
    warnings: dict[str, Warning] = {}

    def warn(code, message, blocking=False):
        warnings[code] = Warning(code=code, message=message, blocking=blocking)

    style_map = {s.get(q("styleId")): s for s in styles} if styles is not None else {}
    number_map = {n.get(q("numId")): n for n in numbering.findall(q("num"))} if numbering is not None else {}
    abstract_map = {n.get(q("abstractNumId")): n for n in numbering.findall(q("abstractNum"))} if numbering is not None else {}
    numbered_ids = []

    def list_kind(props):
        num_id, level = None, None
        present = False
        for prop in props:
            num = prop.find(q("numPr")) if prop is not None else None
            if num is None: continue
            present = True
            ref, ilvl = num.find(q("numId")), num.find(q("ilvl"))
            if num_id is None and ref is not None: num_id = ref.get(q("val"))
            if level is None and ilvl is not None: level = ilvl.get(q("val"))
        if not present or num_id == "0": return None
        instance = number_map.get(num_id)
        if instance is None: return "numbered"
        ref = instance.find(q("abstractNumId"))
        abstract = abstract_map.get(ref.get(q("val"))) if ref is not None else None
        definition = None
        for override in instance.findall(q("lvlOverride")):
            if override.get(q("ilvl")) == (level or "0"):
                definition = override.find(q("lvl"))
        if definition is None and abstract is not None:
            definition = next((n for n in abstract.findall(q("lvl")) if n.get(q("ilvl")) == (level or "0")), None)
        fmt = definition.find(q("numFmt")) if definition is not None else None
        label = definition.find(q("lvlText")) if definition is not None else None
        # Only known, single-character bullet markers can safely be omitted.
        if (fmt is not None and fmt.get(q("val")) == "bullet" and label is not None
            and label.get(q("val"), "") in {"•", "·", "o", "▪", "◦", "-", "–", "\uf0b7", "\uf0a7"}):
            return "bullet"
        return "numbered"

    def properties(p):
        props = [p.find(q("pPr"))]
        style = props[0].find(q("pStyle")) if props[0] is not None else None
        style_id = style.get(q("val")) if style is not None else None
        names, visited = [], set()
        while style_id and style_id not in visited:
            visited.add(style_id)
            names.append(style_id)
            item = style_map.get(style_id)
            if item is None:
                break
            name = item.find(q("name"))
            if name is not None:
                names.append(name.get(q("val"), ""))
            props.append(item.find(q("pPr")))
            base = item.find(q("basedOn"))
            style_id = base.get(q("val")) if base is not None else None
        outline, frame = None, None
        for prop in props:
            if prop is None:
                continue
            level = prop.find(q("outlineLvl"))
            if outline is None and level is not None:
                value = level.get(q("val"), "")
                outline = int(value) if value.isdigit() else 9
            if frame is None:
                frame = prop.find(q("framePr"))
        if outline is None:
            for name in names:
                match = re.fullmatch(r"(?:heading|überschrift)\s*([1-9])", name, re.I)
                if match:
                    outline = int(match[1]) - 1
                    break
        toc = any(re.fullmatch(r"(?:toc|verzeichnis)\s*\d+|toc\s*heading|verzeichnisüberschrift|inhaltsverzeichnis", n, re.I) for n in names)
        kind = list_kind(props)
        if kind == "numbered": numbered_ids.append(ids[id(p)])
        elif kind == "bullet":
            warn("bullets", "Aufzählungszeichen werden ausgelassen; der Text der Listeneinträge bleibt erhalten.")
        drop = frame is not None and frame.get(q("dropCap")) in {"drop", "margin"}
        if frame is not None and not drop:
            warn("frames", "Positionierte Textrahmen erkannt. Die Lesereihenfolge muss in Word geprüft werden.", True)
        return outline if outline is not None and outline < 9 else None, drop, toc

    body = document.find(q("body"))
    # IDs count all physical body paragraphs, including deliberately skipped ones.
    ids = {id(p): f"p{i:06d}" for i, p in enumerate(body.iter(q("p")), 1)}
    if progress: progress("Text lesen", 0, len(ids))
    fields: list[str] = []

    def warn_alternative(node):
        tags = {n.tag.rsplit("}", 1)[-1] for n in node.iter()}
        meaningful = {"object", "altChunk", "sym", "fldChar", "instrText", "numPr", "pStyle",
                      "footnoteReference", "endnoteReference", "oMath", "oMathPara"}
        has_text = any(n.tag.rsplit("}", 1)[-1] in {"t", "delText"} and (n.text or "").strip() for n in node.iter())
        if tags & {"drawing", "pict"} and not has_text and not tags & meaningful:
            warn("images", "Grafiken oder leere Zeichenobjekte werden ausgelassen. Falls Grafiken Buchtext enthalten, diesen bitte als normale Word-Absätze ergänzen.")
        else:
            warn("objects", "Alternative Zeichen-/Textobjekte erkannt; bitte in Word prüfen. Diese Objekte werden ausgelassen.", True)

    def text_of(node):
        tag = node.tag
        if tag in {q("pPr"), q("rPr")}:
            return ""
        if tag in {q("drawing"), q("pict"), q("object"), q("txbxContent"), q("altChunk")}:
            if tag in {q("object"), q("txbxContent"), q("altChunk")} or any(n.tag == q("txbxContent") for n in node.iter()):
                warn("objects", "Textfelder oder eingebettete Inhalte erkannt; ihr Inhalt wird nicht als Buchtext gelesen. Bitte in Word prüfen.", True)
            else:
                warn("images", "Grafiken werden nicht als Text gelesen. Falls sie Buchtext enthalten, diesen bitte als normale Word-Absätze ergänzen.")
            return ""
        if tag.endswith("}AlternateContent"):
            warn_alternative(node)
            return ""
        if tag.endswith("}oMath") or tag.endswith("}oMathPara"):
            warn("math", "Formeln werden nicht als Buchtext gelesen. Bitte die betroffenen Stellen in Word prüfen.", True)
            return ""
        if tag == q("r"):
            hidden = node.find(f"{q('rPr')}/{q('vanish')}")
            if hidden is not None and hidden.get(q("val"), "true") not in {"0", "false", "off"}:
                warn("hidden", "Ausgeblendeter Word-Text wurde ausgelassen. Bitte in Word prüfen.", True)
                return ""
        if tag in {q("del"), q("moveFrom")}:
            return ""
        if tag == q("fldChar"):
            kind = node.get(q("fldCharType"))
            if kind == "begin": fields.append("")
            elif kind == "end" and fields: fields.pop()
            return ""
        if tag == q("instrText"):
            if fields: fields[-1] += node.text or ""
            return ""
        if tag == q("fldSimple") and re.match(r"\s*TOC\b", node.get(q("instr"), ""), re.I):
            return ""
        if tag == q("t"):
            return "" if any(re.match(r"\s*TOC\b", f, re.I) for f in fields) else node.text or ""
        if tag == q("tab"):
            return "\t"
        if tag in {q("br"), q("cr")}:
            return "\n" if node.get(q("type"), "textWrapping") == "textWrapping" else ""
        if tag == q("noBreakHyphen"): return "\u2011"
        if tag == q("softHyphen"): return "\u00ad"
        if tag in {q("footnoteReference"), q("endnoteReference"), q("sym")}:
            warn("references", "Fuß-/Endnoten oder Schriftartsymbole erkannt; diese Inhalte werden nicht übernommen. Bitte in Word prüfen.", True)
            return ""
        return "".join(text_of(child) for child in node)

    tracked = {q(t) for t in ("ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange", "sectPrChange", "tblPrChange")}
    if any(node.tag in tracked for node in body.iter()):
        warn("tracked_changes", "Offene Änderungsverfolgung erkannt. Bitte Änderungen in Word annehmen oder verwerfen und die bereinigte Datei importieren.", True)

    def walk(node):
        if node.tag == q("p"):
            if progress: progress("Text lesen", int(ids[id(node)][1:]), len(ids))
            outline, drop, toc = properties(node)
            raw = text_of(node)
            if raw.strip() and not toc:
                left, right = len(raw) - len(raw.lstrip()), len(raw.rstrip())
                text = raw[left:right]
                yield Paragraph(id=ids[id(node)], text=text, outline=outline, drop_cap=drop,
                    sources=[SourceSpan(paragraph_id=ids[id(node)], source_start=left,
                                        source_end=right, start=0, end=len(text))])
            return
        if node.tag in {q("del"), q("moveFrom")}:
            return
        if node.tag == q("sdt"):
            gallery = node.find(f"{q('sdtPr')}//{q('docPartGallery')}")
            if gallery is not None and gallery.get(q("val"), "").lower() in {"table of contents", "inhaltsverzeichnis"}:
                return
            content = node.find(q("sdtContent"))
            if content is not None:
                yield from walk(content)
            return
        if node.tag in {q("altChunk"), q("txbxContent")}:
            warn("objects", "Textfelder oder eingebettete Inhalte erkannt; bitte in Word in normale Absätze umwandeln.", True)
            return
        if node.tag == q("tbl"):
            warn("tables", "Tabellen werden zeilenweise und je Zeile von links nach rechts gelesen. Bitte die Lesereihenfolge in Word prüfen.", True)
        if node.tag.endswith("}AlternateContent"):
            warn_alternative(node)
            return
        for child in node:
            yield from walk(child)

    paragraphs = list(walk(body))
    if fields:
        warn("fields", "Ein Word-Feld ist nicht geschlossen. Bitte das Dokument in Word prüfen.", True)
    if not paragraphs:
        raise UploadError("Kein lesbarer Text im Word-Hauptteil gefunden. Textfelder oder gescannte Seiten bitte in normale Word-Absätze umwandeln.")
    joined = []
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if paragraph.drop_cap and re.fullmatch(r"[„“\"»«‚‘']*[^\W\d_]", paragraph.text, re.UNICODE):
            following = paragraphs[index + 1] if index + 1 < len(paragraphs) else None
            if (following and following.outline is None and following.text[0].islower()
                and not CHAPTER.fullmatch(following.text) and not SPECIAL.fullmatch(following.text)):
                offset = len(paragraph.text)
                paragraph = paragraph.model_copy(update={"text": paragraph.text + following.text,
                    "sources": paragraph.sources + [s.model_copy(update={"start": s.start + offset, "end": s.end + offset}) for s in following.sources]})
                index += 1
            else:
                warn("initial", "Eine Initiale konnte nicht eindeutig mit dem folgenden Wort verbunden werden. Bitte im Text prüfen und gegebenenfalls in Word korrigieren.", True)
        elif (len(paragraph.text) == 1 and paragraph.text.isupper() and index + 1 < len(paragraphs)
              and paragraphs[index + 1].text[0].islower()):
            warn("initial", "Ein einzelner Buchstabe ohne Word-Initialenformat wurde gefunden. Er bleibt als eigener Absatz erhalten; bitte die Verbindung zum Folgewort in Word prüfen.", True)
        joined.append(paragraph)
        index += 1
    paragraphs = joined
    if progress: progress("Kapitel zuordnen", 0, 0)
    levels = [p.outline for p in paragraphs if p.outline is not None]
    chapter_level = min(levels) if levels else None
    boundaries = []
    consumed = set()
    for i, paragraph in enumerate(paragraphs):
        if i in consumed: continue
        label = bool(CHAPTER.fullmatch(paragraph.text) or SPECIAL.fullmatch(paragraph.text))
        heading = paragraph.outline is not None and paragraph.outline == chapter_level
        if not label and not heading: continue
        title, count = paragraph.text.replace("\n", " · ").replace("\t", " ").strip(), 1
        if NUMBER_HEADING.fullmatch(paragraph.text) and i + 1 < len(paragraphs):
            following = paragraphs[i + 1]
            # Merge a separate title only with explicit heading/uppercase evidence.
            if (2 <= len(following.text) <= 120 and len(following.text.split()) <= 12
                and not CHAPTER.fullmatch(following.text) and not SPECIAL.fullmatch(following.text)
                and (following.outline is not None or following.text.isupper())
                and following.text[-1] not in ".!?…" and not following.drop_cap):
                title += " · " + following.text
                count = 2
                consumed.add(i + 1)
                if following.outline is None and not (i + 2 < len(paragraphs) and paragraphs[i + 2].drop_cap):
                    warn("chapter_title_guess", "Eine Titelzeile wurde anhand ihrer Großschreibung zugeordnet. Bitte die Kapitelaufteilung prüfen.")
        if len(title) > 200:
            warn("chapter_long_heading", "Eine ungewöhnlich lange Überschrift wurde gekürzt. Bitte die Kapitelaufteilung prüfen.")
        boundaries.append(Boundary(paragraph_id=paragraph.id, title=title[:200], heading_count=count))
    if not boundaries:
        boundaries = [Boundary(paragraph_id=paragraphs[0].id, title="Gesamter Text")]
        warn("chapter_missing", "Keine eindeutigen Kapitelgrenzen erkannt. Bitte die Aufteilung bestätigen oder Kapitelanfänge ergänzen.")
    elif boundaries[0].paragraph_id != paragraphs[0].id:
        boundaries.insert(0, Boundary(paragraph_id=paragraphs[0].id, title="Vorspann"))
        warn("chapter_preface", "Text vor dem ersten Kapitel wurde als Vorspann erhalten. Bitte die Aufteilung prüfen.")
    # Empty headings stay visible as provisional text, never vanish silently.
    try:
        chapters = make_chapters(version_id, paragraphs, boundaries)
    except UploadError:
        warn("chapter_empty", "Mindestens eine Überschrift hat keinen Kapiteltext. Bitte die Kapitelgrenzen korrigieren.")
        boundaries = [b.model_copy(update={"heading_count": 0}) for b in boundaries]
        chapters = make_chapters(version_id, paragraphs, boundaries)
    return review_numbering(Extraction(version_id=version_id, paragraphs=paragraphs, boundaries=boundaries,
                      chapters=chapters, warnings=list(warnings.values()), numbered_paragraph_ids=numbered_ids))
