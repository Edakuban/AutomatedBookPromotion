"""Local book-teaser projects assembled from finished chapter reels.

The expensive reel videos remain the source of truth.  A teaser project only
stores an edit decision list and a rendered derivative; it never mutates reel
drafts or their publication state.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Callable, Literal
from uuid import UUID, uuid4

from .reel_generation import ReelGenerationError, ffmpeg_binary
from .uploads import LocalUploadStore, UploadError


TeaserAspect = Literal["vertical", "horizontal_crop", "horizontal_fit"]
Runner = Callable[..., subprocess.CompletedProcess]
Progress = Callable[[int, int], None]


def chapter_video_ready(book_id: str, plan: dict) -> bool:
    """Judge the current video, not the draft's aggregate image/job status."""
    draft = plan.get("draft")
    return bool(
        plan.get("state") == "done" and not plan.get("media_active") and draft
        and draft.book_id == str(book_id) and not draft.image_stale and not draft.video_stale
        and draft.selected_video_path and draft.selected_video_sha256
    )


def complete_chapter_segments(book_id: str, chapters, plans: list[dict], reels) -> list["TeaserSegment"]:
    """Build the simple all-chapters cut, never silently fall back to quote reels."""
    by_chapter = {plan["chapter_id"]: plan for plan in plans}
    expected = {chapter.id for chapter in chapters}
    if not expected or len(by_chapter) != len(plans) or set(by_chapter) != expected:
        raise UploadError("Bitte zuerst für alle Kapitel aktuelle Kapitelvideos erzeugen.", 409)
    segments = []
    for chapter in sorted(chapters, key=lambda item: item.position):
        plan = by_chapter[chapter.id]
        draft = plan.get("draft")
        if (
            not chapter_video_ready(book_id, plan)
            or reels.artifact_path(draft, "video") is None
        ):
            raise UploadError("Bitte zuerst für alle Kapitel aktuelle Kapitelvideos erzeugen.", 409)
        segments.append(TeaserSegment(
            chapter_id=chapter.id, draft_id=draft.id,
            video_sha256=draft.selected_video_sha256, included=True,
            position=chapter.position, start_ms=0, duration_ms=draft.duration_ms,
        ))
    return segments


@dataclass(frozen=True)
class TeaserSegment:
    chapter_id: str
    draft_id: str
    video_sha256: str
    included: bool
    position: int
    start_ms: int
    duration_ms: int
    focus_x: int = 50
    focus_y: int = 50

    @classmethod
    def from_dict(cls, value: dict) -> "TeaserSegment":
        try:
            segment = cls(
                chapter_id=str(UUID(str(value["chapter_id"]))),
                draft_id=str(UUID(str(value["draft_id"]))) if value.get("draft_id") else "",
                video_sha256=str(value.get("video_sha256", "")),
                included=bool(value.get("included", False)),
                position=int(value["position"]),
                start_ms=int(value.get("start_ms", 0)),
                duration_ms=int(value.get("duration_ms", 0)),
                focus_x=int(value.get("focus_x", 50)),
                focus_y=int(value.get("focus_y", 50)),
            )
        except (KeyError, TypeError, ValueError):
            raise UploadError("Die gespeicherte Book-Teaser-Schnittliste ist beschädigt.", 500) from None
        if (
            segment.position < 1
            or segment.start_ms < 0
            or segment.duration_ms < 0
            or not 0 <= segment.focus_x <= 100
            or not 0 <= segment.focus_y <= 100
            or (segment.video_sha256 and not _is_sha256(segment.video_sha256))
        ):
            raise UploadError("Die gespeicherte Book-Teaser-Schnittliste ist beschädigt.", 500)
        return segment

    def as_dict(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "draft_id": self.draft_id,
            "video_sha256": self.video_sha256,
            "included": self.included,
            "position": self.position,
            "start_ms": self.start_ms,
            "duration_ms": self.duration_ms,
            "focus_x": self.focus_x,
            "focus_y": self.focus_y,
        }


@dataclass(frozen=True)
class BookTeaserProject:
    id: str
    book_id: str
    revision: int
    aspect: str
    audio_track_id: str
    transition_ms: int
    segments_json: str
    state: str
    output_path: str | None
    output_sha256: str | None
    output_duration_ms: int | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "BookTeaserProject":
        return cls(**{key: row[key] for key in cls.__annotations__})

    @property
    def segments(self) -> tuple[TeaserSegment, ...]:
        try:
            values = json.loads(self.segments_json)
            if not isinstance(values, list):
                raise ValueError()
            return tuple(TeaserSegment.from_dict(value) for value in values)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise UploadError("Die gespeicherte Book-Teaser-Schnittliste ist beschädigt.", 500) from None


@dataclass(frozen=True)
class TeaserRenderSegment:
    path: Path
    start_seconds: float
    duration_seconds: float
    focus_x: float = .5
    focus_y: float = .5


class BookTeaserStore:
    def __init__(self, uploads: LocalUploadStore):
        self.uploads = uploads
        self.root = uploads.root

    def connection(self):
        connection = sqlite3.connect(self.uploads.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            create table if not exists local_book_teasers (
                id text primary key,
                book_id text not null unique,
                revision integer not null,
                aspect text not null check(aspect in
                    ('vertical','horizontal_crop','horizontal_fit')),
                audio_track_id text not null,
                transition_ms integer not null,
                segments_json text not null,
                state text not null check(state in ('editing','ready','failed')),
                output_path text,
                output_sha256 text,
                output_duration_ms integer,
                error text,
                created_at real not null,
                updated_at real not null
            );
            create index if not exists local_book_teasers_book
                on local_book_teasers(book_id);
        """)

    def get(self, book_id: str) -> BookTeaserProject | None:
        if not self.uploads.db_path.is_file():
            return None
        normalized = str(UUID(str(book_id)))
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_book_teasers'"
            ).fetchone():
                return None
            row = connection.execute(
                "select * from local_book_teasers where book_id=?", (normalized,)
            ).fetchone()
        return BookTeaserProject.from_row(row) if row else None

    def save(
        self,
        book_id: str,
        revision: int,
        *,
        aspect: TeaserAspect,
        audio_track_id: str,
        transition_ms: int,
        segments: list[TeaserSegment],
    ) -> BookTeaserProject:
        normalized = str(UUID(str(book_id)))
        if aspect not in {"vertical", "horizontal_crop", "horizontal_fit"}:
            raise UploadError("Bitte ein gültiges Book-Teaser-Format auswählen.")
        if not 0 <= transition_ms <= 2_000:
            raise UploadError("Die Überblendung muss zwischen 0 und 2 Sekunden liegen.")
        if not segments or len(segments) > 500:
            raise UploadError("Der Book-Teaser benötigt mindestens ein Kapitel.")
        positions = [segment.position for segment in segments]
        if len(positions) != len(set(positions)):
            raise UploadError("Jede Kapitelposition darf nur einmal vorkommen.")
        serialized = json.dumps(
            [segment.as_dict() for segment in sorted(segments, key=lambda item: item.position)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = time.time()
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            if not connection.execute("select 1 from local_books where id=?", (normalized,)).fetchone():
                raise UploadError("Das Buch wurde nicht gefunden.", 404)
            try:
                normalized_track = str(UUID(str(audio_track_id)))
            except ValueError:
                raise UploadError("Bitte einen Song für den Book-Teaser auswählen.") from None
            if not connection.execute(
                "select 1 from local_audio_tracks where id=? and book_id=?",
                (normalized_track, normalized),
            ).fetchone():
                raise UploadError("Der ausgewählte Song gehört nicht zu diesem Buch.", 409)
            if connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_book_teaser_jobs'"
            ).fetchone() and connection.execute(
                """select 1 from local_book_teaser_jobs where book_id=?
                and state in ('queued','running')""", (normalized,),
            ).fetchone():
                raise UploadError(
                    "Der Schnitt kann während des langen Exports nicht geändert werden.", 409,
                )
            row = connection.execute(
                "select * from local_book_teasers where book_id=?", (normalized,)
            ).fetchone()
            if row is None:
                if revision != 0:
                    raise UploadError("Der Book-Teaser wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
                project_id = str(uuid4())
                connection.execute(
                    """insert into local_book_teasers
                    (id,book_id,revision,aspect,audio_track_id,transition_ms,segments_json,state,
                     output_path,output_sha256,output_duration_ms,error,created_at,updated_at)
                    values (?,?,1,?,?,?,?, 'editing',null,null,null,null,?,?)""",
                    (project_id, normalized, aspect, normalized_track, transition_ms, serialized, now, now),
                )
            else:
                if row["revision"] != revision:
                    raise UploadError("Der Book-Teaser wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
                connection.execute(
                    """update local_book_teasers set revision=revision+1,aspect=?,audio_track_id=?,
                    transition_ms=?,segments_json=?,state='editing',output_path=null,output_sha256=null,
                    output_duration_ms=null,error=null,updated_at=? where id=? and revision=?""",
                    (aspect, normalized_track, transition_ms, serialized, now, row["id"], revision),
                )
            saved = connection.execute(
                "select * from local_book_teasers where book_id=?", (normalized,)
            ).fetchone()
        return BookTeaserProject.from_row(saved)

    def mark_rendered(
        self, project_id: str, revision: int, output: Path, duration_ms: int
    ) -> BookTeaserProject:
        normalized_id = str(UUID(str(project_id)))
        path = output.resolve(strict=True)
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            row = connection.execute(
                "select * from local_book_teasers where id=?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise UploadError("Der Book-Teaser wurde nicht gefunden.", 404)
            expected_root = (self.root / "book-teasers" / row["book_id"] / normalized_id).resolve()
            try:
                relative = path.relative_to(self.root.resolve()).as_posix()
                path.relative_to(expected_root)
            except ValueError:
                raise UploadError("Der Book-Teaser-Ausgabepfad ist ungültig.", 503) from None
            if row["revision"] != revision:
                raise UploadError("Der Book-Teaser wurde während des Renderns geändert.", 409)
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            connection.execute(
                """update local_book_teasers set state='ready',output_path=?,output_sha256=?,
                output_duration_ms=?,error=null,updated_at=? where id=? and revision=?""",
                (relative, digest, duration_ms, time.time(), normalized_id, revision),
            )
            saved = connection.execute(
                "select * from local_book_teasers where id=?", (normalized_id,)
            ).fetchone()
        return BookTeaserProject.from_row(saved)

    def mark_failed(self, project_id: str, revision: int, error: str) -> None:
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute(
                """update local_book_teasers set state='failed',error=?,updated_at=?
                where id=? and revision=?""",
                (str(error)[:2000], time.time(), str(project_id), revision),
            )

    def render_path(self, project: BookTeaserProject) -> Path:
        root = (self.root / "book-teasers" / project.book_id / project.id).resolve()
        expected_parent = (self.root / "book-teasers" / project.book_id).resolve()
        if root.parent != expected_parent:
            raise UploadError("Der Book-Teaser-Ausgabepfad ist ungültig.", 503)
        root.mkdir(parents=True, exist_ok=True)
        return root / f"book-teaser-r{project.revision}.mp4"

    def output_path(self, project: BookTeaserProject) -> Path | None:
        if not project.output_path:
            return None
        path = (self.root / project.output_path).resolve()
        expected_root = (self.root / "book-teasers" / project.book_id / project.id).resolve()
        try:
            path.relative_to(expected_root)
        except ValueError:
            raise UploadError("Der gespeicherte Book-Teaser-Pfad ist ungültig.", 503) from None
        if not path.is_file():
            raise UploadError("Die gespeicherte Book-Teaser-Datei fehlt.", 503)
        if not _is_sha256(project.output_sha256 or ""):
            raise UploadError("Der gespeicherte Book-Teaser-Hash ist ungültig.", 503)
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != project.output_sha256:
                raise UploadError("Die gespeicherte Book-Teaser-Datei ist beschädigt.", 503)
        return path


def teaser_duration_ms(segments: list[TeaserRenderSegment], transition_ms: int) -> int:
    if not segments:
        return 0
    overlap = transition_ms * max(0, len(segments) - 1)
    return max(0, round(sum(item.duration_seconds * 1000 for item in segments)) - overlap)


def render_book_teaser(
    segments: list[TeaserRenderSegment],
    audio_path: str | Path,
    output_path: str | Path,
    *,
    aspect: TeaserAspect,
    transition_ms: int = 500,
    media_binary: str | None = None,
    runner: Runner = subprocess.run,
    progress: Progress | None = None,
) -> int:
    """Render an ordered reel edit with one continuous audio track."""
    if not segments:
        raise ValueError("At least one teaser segment is required")
    if aspect not in {"vertical", "horizontal_crop", "horizontal_fit"}:
        raise ValueError("Unsupported teaser aspect")
    if not 0 <= transition_ms <= 2_000:
        raise ValueError("Transition is outside the supported range")
    sources: list[TeaserRenderSegment] = []
    for item in segments:
        path = Path(item.path).resolve(strict=True)
        if path.suffix.casefold() != ".mp4" or not path.is_file():
            raise ValueError("Teaser sources must be MP4 files")
        if item.start_seconds < 0 or item.duration_seconds <= 0:
            raise ValueError("Teaser source timing is invalid")
        if not 0 <= item.focus_x <= 1 or not 0 <= item.focus_y <= 1:
            raise ValueError("Teaser focus is invalid")
        sources.append(TeaserRenderSegment(
            path, float(item.start_seconds), float(item.duration_seconds),
            float(item.focus_x), float(item.focus_y),
        ))
    if len(sources) > 1 and any(
        item.duration_seconds <= transition_ms / 1000 for item in sources
    ):
        raise ValueError("Every teaser segment must be longer than its transition")
    audio = Path(audio_path).resolve(strict=True)
    if not audio.is_file():
        raise ValueError("Teaser audio must be a file")
    target = Path(output_path).resolve()
    if target.suffix.casefold() != ".mp4" or target == audio or target in {item.path for item in sources}:
        raise ValueError("Teaser output must be a separate MP4 file")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.mp4")
    binary = media_binary or ffmpeg_binary()
    video_encoder_args = _video_encoder_args(binary, runner)
    command = [binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for item in sources:
        command += ["-ss", f"{item.start_seconds:.3f}", "-t", f"{item.duration_seconds:.3f}", "-i", str(item.path)]
    command += ["-stream_loop", "-1", "-i", str(audio)]
    filters: list[str] = []
    width, height = (1080, 1920) if aspect == "vertical" else (1920, 1080)
    for index, item in enumerate(sources):
        base = f"[{index}:v]fps=30,settb=AVTB"
        if aspect == "horizontal_fit":
            filters.append(
                f"{base},split=2[bg{index}in][fg{index}in];"
                f"[bg{index}in]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma=24:steps=2[bg{index}];"
                f"[fg{index}in]scale={width}:{height}:force_original_aspect_ratio=decrease[fg{index}];"
                f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p,"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
        else:
            filters.append(
                f"{base},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(in_w-out_w)*{item.focus_x:.4f}':"
                f"y='(in_h-out_h)*{item.focus_y:.4f}',setsar=1,format=yuv420p,"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
    transition_seconds = transition_ms / 1000
    if len(sources) > 1 and transition_ms == 0:
        final_video = "joined"
        filters.append(
            "".join(f"[v{index}]" for index in range(len(sources)))
            + f"concat=n={len(sources)}:v=1:a=0[{final_video}]"
        )
    else:
        final_video = "v0"
        elapsed = sources[0].duration_seconds
        for index in range(1, len(sources)):
            output = f"mix{index}"
            offset = elapsed - transition_seconds
            filters.append(
                f"[{final_video}][v{index}]xfade=transition=fade:duration={transition_seconds:.3f}:"
                f"offset={offset:.3f}[{output}]"
            )
            final_video = output
            elapsed += sources[index].duration_seconds - transition_seconds
    duration_ms = teaser_duration_ms(sources, transition_ms)
    duration_seconds = duration_ms / 1000
    audio_index = len(sources)
    fade_out_start = max(0, duration_seconds - .5)
    filters.append(
        f"[{audio_index}:a]aresample=48000,asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d=0.25,afade=t=out:st={fade_out_start:.3f}:d=0.5[aout]"
    )
    command += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{final_video}]", "-map", "[aout]",
        *video_encoder_args,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration_seconds:.3f}", "-movflags", "+faststart", "-map_metadata", "-1",
        str(temporary),
    ]
    try:
        try:
            if progress is None:
                runner(command, check=True, capture_output=True, text=True)
            else:
                _run_with_progress(command, duration_ms, progress)
        except FileNotFoundError as exc:
            raise ReelGenerationError("FFmpeg wurde nicht gefunden; der Book-Teaser konnte nicht erzeugt werden.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            message = "FFmpeg konnte die Kapitel-Reels nicht zum Book-Teaser verbinden."
            if detail:
                message += f" {detail[-500:]}"
            raise ReelGenerationError(message) from exc
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ReelGenerationError("FFmpeg hat keinen fertigen Book-Teaser erzeugt.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return duration_ms


def _run_with_progress(command: list[str], duration_ms: int, progress: Progress) -> None:
    tracked = [*command[:-1], "-progress", "pipe:1", "-nostats", command[-1]]
    try:
        process = subprocess.Popen(
            tracked, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise
    output: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.strip()
        output.append(line)
        if len(output) > 80:
            output.pop(0)
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                completed_ms = max(0, min(duration_ms, int(line.split("=", 1)[1]) // 1000))
            except ValueError:
                continue
            progress(completed_ms, duration_ms)
    returncode = process.wait()
    if returncode:
        raise subprocess.CalledProcessError(returncode, tracked, stderr="\n".join(output))
    progress(duration_ms, duration_ms)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _video_encoder_args(binary: str, runner: Runner) -> list[str]:
    """Select an MP4/H.264 encoder supported by full or VocaVid FFmpeg builds."""
    try:
        result = runner(
            [binary, "-hide_banner", "-encoders"],
            check=True, capture_output=True, text=True,
        )
        available = {
            fields[1]
            for line in result.stdout.splitlines()
            if len(fields := line.split()) >= 2 and fields[0].startswith("V")
        }
    except (FileNotFoundError, subprocess.CalledProcessError, AttributeError):
        available = set()
    if "libx264" in available:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    if "h264_mf" in available:
        return [
            "-c:v", "h264_mf", "-rate_control", "pc_vbr", "-b:v", "8M",
            "-maxrate", "12M", "-bufsize", "24M", "-scenario", "archive",
        ]
    if "h264_nvenc" in available:
        return ["-c:v", "h264_nvenc", "-b:v", "8M", "-maxrate", "12M", "-bufsize", "24M"]
    if "mpeg4" in available:
        return ["-c:v", "mpeg4", "-q:v", "3"]
    # Keep the common encoder name in the final command so a nonstandard probe
    # failure produces a useful FFmpeg error instead of hiding the root cause.
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
