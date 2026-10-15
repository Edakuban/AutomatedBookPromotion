"""Create a path-safe ZIP containing a book's quotes and current media."""

from __future__ import annotations

from pathlib import Path
import re
import unicodedata
from zipfile import ZIP_STORED, ZipFile

from .chapter_teasers import ChapterTeaserStore
from .reels import ReelStore


ZipEntry = tuple[str, Path | bytes]


def path_slug(value: str, *, max_length: int = 56) -> str:
    """Return a short ASCII slug that is safe on common desktop filesystems."""
    value = value.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue",
                                          "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                                          "ß": "ss"}))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "zitat"


def write_book_media_zip(
    output: Path,
    *,
    chapters,
    quote_items,
    drafts,
    analysis_run_id: str,
    reels: ReelStore,
) -> int:
    """Write usable quote texts plus selected images and reels to ``output``.

    Quote numbers are assigned by their position in the chapter text, independent
    of the order in which media were generated.
    """
    chapter_positions = {chapter.id: chapter.position for chapter in chapters}
    current_drafts = {
        draft.quote_id: draft for draft in drafts if draft.analysis_run_id == analysis_run_id
    }
    quotes_by_chapter: dict[str, list] = {}
    for item in quote_items:
        if not item["usable"]:
            continue
        quote = item["quote"]
        if quote.chapter_id in chapter_positions:
            quotes_by_chapter.setdefault(quote.chapter_id, []).append(quote)

    max_chapter = max(chapter_positions.values(), default=0)
    chapter_width = max(2, len(str(max_chapter)))
    entries: list[ZipEntry] = []
    for chapter_id, quotes in sorted(
        quotes_by_chapter.items(), key=lambda pair: chapter_positions[pair[0]],
    ):
        ordered = sorted(quotes, key=lambda quote: (quote.source_start, quote.source_end, quote.id))
        quote_width = max(2, len(str(len(ordered))))
        for quote_number, quote in enumerate(ordered, 1):
            stem = (
                f"{chapter_positions[chapter_id]:0{chapter_width}d}-"
                f"{quote_number:0{quote_width}d}-{path_slug(quote.text)}"
            )
            entries.append((stem + ".txt", quote.text.encode("utf-8")))
            draft = current_drafts.get(quote.id)
            if draft is None:
                continue
            image = reels.artifact_path(draft, "image")
            if image is not None:
                entries.append((stem + image.suffix.lower(), image))
            video = reels.artifact_path(draft, "video")
            if video is not None:
                entries.append((stem + video.suffix.lower(), video))

    _write_zip(output, entries)
    return len(entries)


def write_book_chapter_zip(
    output: Path,
    *,
    chapters,
    chapter_run,
    drafts,
    reels: ReelStore,
    chapter_teasers: ChapterTeaserStore,
) -> int:
    """Write every chapter text plus its current image and reel to ``output``."""
    plans = (
        {plan["chapter_id"]: plan for plan in chapter_run.get("plans", [])}
        if chapter_run and chapter_run.get("state") != "stale" else {}
    )
    drafts_by_id = {draft.id: draft for draft in drafts}
    max_chapter = max((chapter.position for chapter in chapters), default=0)
    chapter_width = max(2, len(str(max_chapter)))
    entries: list[ZipEntry] = []
    for chapter in sorted(chapters, key=lambda item: (item.position, item.id)):
        stem = f"{chapter.position:0{chapter_width}d}-{path_slug(chapter.title)}"
        entries.append((stem + ".txt", chapter.source_text.encode("utf-8")))
        plan = plans.get(chapter.id)
        draft = drafts_by_id.get(plan.get("draft_id")) if plan else None
        if draft is None:
            continue
        image = reels.artifact_path(draft, "image")
        if image is not None:
            entries.append((stem + image.suffix.lower(), image))
        video = None
        if not draft.video_stale:
            video = chapter_teasers.text_video_path(
                plan, book_id=draft.book_id, draft_id=draft.id,
            ) or reels.artifact_path(draft, "video")
        if video is not None:
            entries.append((stem + video.suffix.lower(), video))

    _write_zip(output, entries)
    return len(entries)


def _write_zip(output: Path, entries: list[ZipEntry]) -> None:
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        for archive_name, source in entries:
            if isinstance(source, Path):
                archive.write(source, archive_name)
            else:
                archive.writestr(archive_name, source)
