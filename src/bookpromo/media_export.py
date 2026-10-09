"""Create a path-safe ZIP containing the current media for a book's quotes."""

from __future__ import annotations

from pathlib import Path
import re
import unicodedata
from zipfile import ZIP_STORED, ZipFile

from .reels import ReelStore


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
    """Write selected images and reels from the current analysis to ``output``.

    Quote numbers are assigned by their position in the chapter text, independent
    of the order in which media were generated.
    """
    chapter_positions = {chapter.id: chapter.position for chapter in chapters}
    current_drafts = {
        draft.quote_id: draft for draft in drafts if draft.analysis_run_id == analysis_run_id
    }
    quotes_by_chapter: dict[str, list] = {}
    for item in quote_items:
        quote = item["quote"]
        if quote.chapter_id in chapter_positions:
            quotes_by_chapter.setdefault(quote.chapter_id, []).append(quote)

    max_chapter = max(chapter_positions.values(), default=0)
    chapter_width = max(2, len(str(max_chapter)))
    entries: list[tuple[str, Path]] = []
    for chapter_id, quotes in sorted(
        quotes_by_chapter.items(), key=lambda pair: chapter_positions[pair[0]],
    ):
        ordered = sorted(quotes, key=lambda quote: (quote.source_start, quote.source_end, quote.id))
        quote_width = max(2, len(str(len(ordered))))
        for quote_number, quote in enumerate(ordered, 1):
            draft = current_drafts.get(quote.id)
            if draft is None:
                continue
            stem = (
                f"{chapter_positions[chapter_id]:0{chapter_width}d}-"
                f"{quote_number:0{quote_width}d}-{path_slug(quote.text)}"
            )
            image = reels.artifact_path(draft, "image")
            if image is not None:
                entries.append((stem + image.suffix.lower(), image))
            video = reels.artifact_path(draft, "video")
            if video is not None:
                entries.append((stem + video.suffix.lower(), video))

    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        for archive_name, source in entries:
            archive.write(source, archive_name)
    return len(entries)
