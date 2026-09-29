"""Book-scoped, revisioned character profiles and local reference images."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import BinaryIO, Iterable
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from .uploads import LocalUploadStore, UploadError


@dataclass(frozen=True)
class BookCharacter:
    id: str
    book_id: str
    name: str
    aliases: tuple[str, ...]
    description: str
    image_prompt: str
    source_evidence: str
    derived_from_id: str | None
    reference_image_path: str | None
    reference_image_sha256: str | None
    approved: bool
    manual: bool
    revision: int
    analysis_run_id: str
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "BookCharacter":
        return cls(
            id=row["id"], book_id=row["book_id"], name=row["name"],
            aliases=tuple(json.loads(row["aliases_json"])), description=row["description"],
            image_prompt=row["image_prompt"], source_evidence=row["source_evidence"],
            derived_from_id=row["derived_from_id"], reference_image_path=row["reference_image_path"],
            reference_image_sha256=row["reference_image_sha256"], approved=bool(row["approved"]),
            manual=bool(row["manual"]), revision=row["revision"], analysis_run_id=row["analysis_run_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @property
    def has_reference(self) -> bool:
        return bool(self.reference_image_path and self.reference_image_sha256)


class CharacterStore:
    def __init__(self, uploads: LocalUploadStore):
        self.uploads = uploads
        self.root = uploads.root

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.uploads.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            create table if not exists local_book_characters (
                id text primary key, book_id text not null, name text not null,
                name_key text not null, aliases_json text not null,
                description text not null, image_prompt text not null,
                source_evidence text not null, derived_from_id text,
                reference_image_path text, reference_image_sha256 text,
                approved integer not null check(approved in (0,1)),
                manual integer not null check(manual in (0,1)),
                revision integer not null, analysis_run_id text not null,
                created_at real not null, updated_at real not null,
                unique(book_id,name_key)
            );
            create index if not exists local_book_characters_book
                on local_book_characters(book_id,name_key,id);
        """)

    @staticmethod
    def _book_id(value: str) -> str:
        try:
            return str(UUID(str(value)))
        except ValueError:
            raise UploadError("Das Buch wurde nicht gefunden.", 404) from None

    @staticmethod
    def _key(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _text(value: object, label: str, limit: int, *, required: bool = False) -> str:
        text = str(value or "").strip()
        if (required and not text) or len(text) > limit or any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise UploadError(f"Bitte {label} vollständig und ohne Steuerzeichen angeben.")
        return text

    @staticmethod
    def _aliases(value: object) -> tuple[str, ...]:
        raw = value if isinstance(value, (list, tuple)) else re.split(r"[,\n]", str(value or ""))
        aliases: list[str] = []
        for item in raw:
            alias = str(item).strip()
            if alias and alias.casefold() not in {old.casefold() for old in aliases}:
                if len(alias) > 120:
                    raise UploadError("Ein Charakter-Alias ist zu lang.")
                aliases.append(alias)
        if len(aliases) > 20:
            raise UploadError("Bitte höchstens 20 Charakter-Aliasse angeben.")
        return tuple(aliases)

    @staticmethod
    def _require_book(connection: sqlite3.Connection, book_id: str) -> str:
        normalized = CharacterStore._book_id(book_id)
        if connection.execute("select 1 from local_books where id=?", (normalized,)).fetchone() is None:
            raise UploadError("Das Buch wurde nicht gefunden.", 404)
        return normalized

    def list(self, book_id: str) -> list[BookCharacter]:
        normalized = self._book_id(book_id)
        if not self.uploads.db_path.is_file():
            return []
        with closing(self.connection()) as connection:
            if connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_book_characters'"
            ).fetchone() is None:
                return []
            rows = connection.execute(
                "select * from local_book_characters where book_id=? order by name_key,id", (normalized,)
            ).fetchall()
        return [BookCharacter.from_row(row) for row in rows]

    def get(self, book_id: str, character_id: str) -> BookCharacter | None:
        normalized = self._book_id(book_id)
        try:
            character_id = str(UUID(str(character_id)))
        except ValueError:
            return None
        if not self.uploads.db_path.is_file():
            return None
        with closing(self.connection()) as connection:
            if connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_book_characters'"
            ).fetchone() is None:
                return None
            row = connection.execute(
                "select * from local_book_characters where id=? and book_id=?", (character_id, normalized)
            ).fetchone()
        return BookCharacter.from_row(row) if row else None

    def create(self, book_id: str, *, name: str, description: str = "", image_prompt: str = "") -> BookCharacter:
        name = self._text(name, "einen Charakternamen", 160, required=True)
        description = self._text(description, "eine gültige Beschreibung", 5000)
        image_prompt = self._text(image_prompt, "einen gültigen Bildprompt", 6000)
        now, character_id = time.time(), str(uuid4())
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            book_id = self._require_book(connection, book_id)
            try:
                connection.execute(
                    """insert into local_book_characters values
                    (?,?,?,?,?,?,?,?,null,null,null,0,1,0,'',?,?)""",
                    (character_id, book_id, name, self._key(name), "[]", description,
                     image_prompt, "Manuell angelegt", now, now),
                )
            except sqlite3.IntegrityError:
                raise UploadError("Für dieses Buch existiert bereits ein Charakter mit diesem Namen.", 409) from None
            row = connection.execute("select * from local_book_characters where id=?", (character_id,)).fetchone()
        return BookCharacter.from_row(row)

    def save(
        self, book_id: str, character_id: str, revision: int, *, name: str,
        aliases: object, description: str, image_prompt: str, approved: bool,
    ) -> BookCharacter:
        name = self._text(name, "einen Charakternamen", 160, required=True)
        aliases_value = self._aliases(aliases)
        description = self._text(description, "eine gültige Beschreibung", 5000)
        image_prompt = self._text(image_prompt, "einen gültigen Bildprompt", 6000)
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            book_id = self._require_book(connection, book_id)
            row = connection.execute(
                "select * from local_book_characters where id=? and book_id=?", (str(character_id), book_id)
            ).fetchone()
            if row is None:
                raise UploadError("Der Charakter wurde nicht gefunden.", 404)
            if row["revision"] != int(revision):
                raise UploadError("Der Charakter wurde zwischenzeitlich geändert. Bitte die Seite neu öffnen.", 409)
            try:
                connection.execute(
                    """update local_book_characters set name=?,name_key=?,aliases_json=?,description=?,
                    image_prompt=?,approved=?,manual=1,revision=revision+1,updated_at=? where id=?""",
                    (name, self._key(name), json.dumps(aliases_value, ensure_ascii=False), description,
                     image_prompt, int(bool(approved)), time.time(), row["id"]),
                )
            except sqlite3.IntegrityError:
                raise UploadError("Für dieses Buch existiert bereits ein Charakter mit diesem Namen.", 409) from None
            saved = connection.execute("select * from local_book_characters where id=?", (row["id"],)).fetchone()
        return BookCharacter.from_row(saved)

    @staticmethod
    def merge_analysis_in_connection(
        connection: sqlite3.Connection, book_id: str, analysis_run_id: str, suggestions: Iterable[object],
    ) -> None:
        now = time.time()
        for suggestion in suggestions:
            data = suggestion.model_dump() if hasattr(suggestion, "model_dump") else dict(suggestion)
            name = CharacterStore._text(data.get("name"), "einen Charakternamen", 160, required=True)
            key = CharacterStore._key(name)
            aliases = CharacterStore._aliases(data.get("aliases", []))
            description = CharacterStore._text(data.get("description"), "eine gültige Beschreibung", 5000)
            prompt = CharacterStore._text(data.get("image_prompt"), "einen gültigen Bildprompt", 6000)
            evidence = CharacterStore._text(data.get("source_evidence"), "gültige Quellenhinweise", 4000)
            old = connection.execute(
                "select * from local_book_characters where book_id=? and name_key=?", (book_id, key)
            ).fetchone()
            if old is None:
                connection.execute(
                    """insert into local_book_characters values
                    (?,?,?,?,?,?,?,?,null,null,null,0,0,0,?,?,?)""",
                    (str(uuid4()), book_id, name, key, json.dumps(aliases, ensure_ascii=False),
                     description, prompt, evidence, analysis_run_id, now, now),
                )
            elif not old["manual"] and not old["approved"]:
                connection.execute(
                    """update local_book_characters set name=?,aliases_json=?,description=?,image_prompt=?,
                    source_evidence=?,analysis_run_id=?,revision=revision+1,updated_at=? where id=?""",
                    (name, json.dumps(aliases, ensure_ascii=False), description, prompt, evidence,
                     analysis_run_id, now, old["id"]),
                )

    def merge_analysis(self, book_id: str, analysis_run_id: str, suggestions: Iterable[object]) -> None:
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            book_id = self._require_book(connection, book_id)
            self.merge_analysis_in_connection(connection, book_id, analysis_run_id, suggestions)

    def reference_path(self, character: BookCharacter) -> Path | None:
        if not character.reference_image_path:
            return None
        root = (self.root / "characters" / character.book_id / character.id).resolve()
        path = (self.root / character.reference_image_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise UploadError("Der gespeicherte Charakterbildpfad ist ungültig.", 503) from None
        return path if path.is_file() else None

    def save_reference_file(
        self, book_id: str, character_id: str, revision: int, source: str | Path | BinaryIO,
    ) -> BookCharacter:
        character = self.get(book_id, character_id)
        if character is None:
            raise UploadError("Der Charakter wurde nicht gefunden.", 404)
        if character.revision != int(revision):
            raise UploadError("Der Charakter wurde zwischenzeitlich geändert. Bitte die Seite neu öffnen.", 409)
        try:
            if isinstance(source, (str, Path)):
                image = Image.open(Path(source).resolve(strict=True))
            else:
                source.seek(0)
                image = Image.open(source)
            with image:
                if image.width > 8192 or image.height > 8192 or image.width * image.height > 40_000_000:
                    raise UploadError("Das Charakterbild hat zu große Abmessungen.")
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                normalized.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                if normalized.width < 256 or normalized.height < 256:
                    raise UploadError("Das Charakterbild muss mindestens 256 × 256 Pixel groß sein.")
                output = BytesIO()
                normalized.save(output, format="PNG", optimize=True)
                data = output.getvalue()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
            raise UploadError("Das Charakterbild ist keine gültige PNG-, JPEG- oder WebP-Datei.", 415) from None
        digest = hashlib.sha256(data).hexdigest()
        directory = (self.root / "characters" / character.book_id / character.id).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"reference-{uuid4().hex}.png"
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        relative = target.relative_to(self.root.resolve()).as_posix()
        try:
            with closing(self.connection()) as connection, connection:
                connection.execute("begin immediate")
                result = connection.execute(
                    """update local_book_characters set reference_image_path=?,reference_image_sha256=?,
                    revision=revision+1,updated_at=? where id=? and book_id=? and revision=?""",
                    (relative, digest, time.time(), character.id, character.book_id, int(revision)),
                )
                if result.rowcount != 1:
                    raise UploadError("Der Charakter wurde zwischenzeitlich geändert. Bitte die Seite neu öffnen.", 409)
                row = connection.execute("select * from local_book_characters where id=?", (character.id,)).fetchone()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return BookCharacter.from_row(row)


def character_mention_index(character: BookCharacter, scene_text: str) -> int | None:
    """Return the first whole-name/alias occurrence used to order scene identities."""
    haystack = str(scene_text).casefold()
    positions = [
        match.start()
        for name in (character.name, *character.aliases)
        if name
        for match in [re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", haystack)]
        if match is not None
    ]
    return min(positions) if positions else None


def order_scene_characters(
    characters: Iterable[BookCharacter], scene_text: str,
) -> list[BookCharacter]:
    """Keep identities in narrative order rather than the store's alphabetical order."""
    indexed = list(enumerate(characters))
    return [
        character for _, character in sorted(
            indexed,
            key=lambda item: (
                character_mention_index(item[1], scene_text) is None,
                character_mention_index(item[1], scene_text) or 0,
                item[0],
            ),
        )
    ]


def _identity_positions(count: int) -> list[str]:
    positions = {
        1: ["only person"],
        2: ["left person", "right person"],
        3: ["left person", "center person", "right person"],
        4: ["leftmost person", "second person from the left",
            "second person from the right", "rightmost person"],
    }
    if count not in positions:
        raise ValueError("One to four scene characters are supported")
    return positions[count]


def character_scene_prompt(base_prompt: str, characters: Iterable[BookCharacter]) -> str:
    """Add book-scoped identity descriptions only for the local ComfyUI render."""
    prompt = str(base_prompt).strip()
    if not prompt:
        raise ValueError("Scene image prompt is empty")
    selected = list(characters)
    entries = []
    for index, character in enumerate(selected):
        description = " ".join(character.description.split())
        visual = " ".join(character.image_prompt.split())
        details = []
        if description:
            details.append(f"book description: {description[:700]}")
        if visual:
            details.append(f"human visual identity: {visual[:900]}")
        if details:
            entries.append(f"{index + 1}. {character.name}: {'; '.join(details)}")
    if entries:
        layout = "; ".join(
            f"{position} is {character.name}"
            for position, character in zip(_identity_positions(len(selected)), selected, strict=True)
        )
        prompt += (
            "\n\nMANDATORY SCENE IDENTITY LAYOUT, fixed from left to right: " + layout + ". "
            "Keep every named person in that exact position and never exchange faces, ages, hair, "
            "or other identity features. Use the human visual identity for appearance. Book "
            "descriptions are context only; do not introduce animal forms, transformations, magic, "
            "or extra people unless the main scene prompt explicitly requests them.\n"
            "BOOK-SPECIFIC CHARACTER LOCKS:\n" + "\n".join(entries)
        )
    if len(prompt) > 20_000:
        raise ValueError("Scene image prompt is too long")
    return prompt


def stitch_character_references(
    characters: list[tuple[BookCharacter, Path]], output_path: str | Path,
) -> tuple[Path, str]:
    """Create a left-to-right identity sheet and matching prompt context."""
    if not characters:
        raise ValueError("At least one character reference is required")
    if len(characters) > 4:
        raise ValueError("At most four character references are supported")
    cell_width, cell_height = 512, 640
    sheet = Image.new("RGB", (cell_width * len(characters), cell_height), "#151a21")
    for index, (_, path) in enumerate(characters):
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            fitted = ImageOps.fit(image, (cell_width, cell_height), method=Image.Resampling.LANCZOS)
            sheet.paste(fitted, (index * cell_width, 0))
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    sheet.save(temporary, format="PNG", optimize=True)
    temporary.replace(target)
    def identity(character: BookCharacter) -> str:
        description = " ".join(character.description.split())
        visual = " ".join(character.image_prompt.split())
        details = []
        if description:
            details.append(f"book description: {description[:180]}")
        if visual:
            details.append(f"visual identity: {visual[:320]}")
        return f"{character.name}. {'; '.join(details)}" if details else character.name

    def build_context(include_details: bool) -> str:
        identify = identity if include_details else lambda character: character.name
        if len(characters) == 1:
            return (
                f"The sole reference portrait shows {identify(characters[0][0])}. "
                f"The only person in Image 1 is {characters[0][0].name}."
            )
        scene_positions = _identity_positions(len(characters))
        reference_positions = [position.replace("person", "portrait") for position in scene_positions]
        return "Reference sheet identity order, left to right: " + "; ".join(
            f"{reference_positions[index]} is {identify(character)}"
            for index, (character, _) in enumerate(characters)
        ) + ". Scene assignment in Image 1: " + "; ".join(
            f"{scene_positions[index]} is {character.name} and may receive identity features only "
            f"from the {reference_positions[index]}"
            for index, (character, _) in enumerate(characters)
        ) + ". Never swap these identities or leak one portrait's features into another person."
    context = build_context(include_details=True)
    if len(context) > 3_900:
        context = build_context(include_details=False)
    return target, context
