"""Reusable local image/audio/video generation primitives for quote reels."""

from __future__ import annotations

from array import array
import copy
from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from typing import Any, Callable
import uuid
import wave

from PIL import Image, ImageChops, ImageFilter

from .comfy import ComfyClient, load_workflow, output_node_ids, with_output_prefix
from .identity_context import reference_appearance_hints
from .reel_prompts import validate_video_prompt
from .scene_plan import ScenePlan, compile_scene_plan, validate_reference_bindings


Runner = Callable[..., subprocess.CompletedProcess]


def ffmpeg_binary() -> str:
    """Resolve FFmpeg the same way as VocaVid on this workstation."""
    configured = os.environ.get("FFMPEG_BINARY", "").strip()
    if configured:
        return configured
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    vocavid_bundled = Path(r"C:\tmp\Dione\apps\Applio\applio\ffmpeg.exe")
    return str(vocavid_bundled) if vocavid_bundled.is_file() else "ffmpeg"


class ReelGenerationError(RuntimeError):
    pass


CHARACTER_IDENTITY_STRATEGY = "reference-appearance-with-semantic-detail-v2"
REFERENCE_SCENE_STRATEGY = "reference-scene-restaging-v2"
PLANNED_SCENE_STRATEGY = "reference-scene-plan-v2"


def wav_waveform(path: str | Path, *, bins: int = 720) -> list[float]:
    """Return normalized PCM peaks for a compact browser waveform."""
    source = Path(path).resolve(strict=True)
    if not source.is_file() or not 100 <= bins <= 2_000:
        raise ValueError("Invalid WAV waveform request")
    stat = source.stat()
    return list(_wav_waveform_cached(str(source), stat.st_size, stat.st_mtime_ns, bins))


@lru_cache(maxsize=64)
def _wav_waveform_cached(path: str, size: int, modified_ns: int, bins: int) -> tuple[float, ...]:
    del size, modified_ns  # Part of the cache key so replaced files cannot reuse stale peaks.
    with wave.open(path, "rb") as audio:
        if audio.getcomptype() != "NONE" or audio.getsampwidth() not in {1, 2, 3, 4}:
            raise ValueError("Waveform preview requires an uncompressed PCM WAV")
        total_frames = audio.getnframes()
        if total_frames <= 0:
            return tuple()
        width = audio.getsampwidth()
        frames_per_bin = max(1, math.ceil(total_frames / bins))
        maximum = float(1 << (width * 8 - 1))
        peaks: list[float] = []
        while len(peaks) < bins:
            block = audio.readframes(frames_per_bin)
            if not block:
                break
            peaks.append(min(1.0, _pcm_peak(block, width) / maximum))
    if not peaks:
        return tuple()
    peaks.extend([0.0] * (bins - len(peaks)))
    return tuple(round(value, 6) for value in peaks)


def _pcm_peak(data: bytes, width: int) -> int:
    if width == 1:
        return max((abs(value - 128) for value in data), default=0)
    if width in {2, 4}:
        values = array("h" if width == 2 else "i")
        values.frombytes(data[: len(data) - (len(data) % width)])
        if sys.byteorder != "little":
            values.byteswap()
        return max((abs(value) for value in values), default=0)
    return max(
        (abs(int.from_bytes(data[index:index + 3], "little", signed=True))
         for index in range(0, len(data) - 2, 3)),
        default=0,
    )


@dataclass(frozen=True)
class ReelVideoResult:
    raw_video_path: Path
    final_video_path: Path
    prompt_id: str


@dataclass(frozen=True)
class CharacterReferenceSpec:
    name: str
    reference_image_path: Path
    selector_prompt: str
    identity_prompt: str
    forbidden_features: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


def audio_duration(path: str | Path, *, ffprobe_binary: str = "ffprobe", runner: Runner = subprocess.run) -> float:
    """Read an audio duration without decoding the entire file."""
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("Audio source must be a file")
    try:
        with wave.open(str(source), "rb") as handle:
            rate = handle.getframerate()
            if rate <= 0:
                raise ValueError("WAV file has an invalid sample rate")
            return handle.getnframes() / rate
    except (wave.Error, EOFError):
        pass
    command = [
        ffprobe_binary,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        result = runner(command, check=True, capture_output=True, text=True)
        duration = float(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
        raise ReelGenerationError("Audiodauer konnte nicht bestimmt werden; FFprobe fehlt oder die Datei ist ungültig.") from exc
    if duration <= 0:
        raise ReelGenerationError("Die Audiodatei hat keine nutzbare Dauer.")
    return duration


def split_audio_segment(
    audio_path: str | Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    output_path: str | Path,
    ffmpeg_binary: str = "ffmpeg",
    runner: Runner = subprocess.run,
    source_root: str | Path | None = None,
    output_root: str | Path | None = None,
) -> Path:
    """Create a PCM WAV containing exactly the selected source interval.

    FFmpeg handles all supported source formats.  If it is unavailable or
    fails for an ordinary PCM WAV, Python's ``wave`` module provides a local
    sample-accurate fallback.
    """
    start = float(start_seconds)
    duration = float(duration_seconds)
    if start < 0 or not 0 < duration <= 60:
        raise ValueError("Audio start/duration is outside the supported range")
    source = _bounded_path(audio_path, source_root, must_exist=True, label="Audio source")
    target = _bounded_path(output_path, output_root, must_exist=False, label="Audio output")
    if target.suffix.casefold() != ".wav":
        raise ValueError("Audio segment output must be a WAV file")
    if source == target:
        raise ValueError("Audio output must differ from its source")
    probe_binary = _sibling_ffprobe(ffmpeg_binary)
    total = audio_duration(source, ffprobe_binary=probe_binary, runner=runner)
    if start >= total or start + duration > total + 0.002:
        raise ValueError("Selected audio interval exceeds the source duration")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.wav")
    command = [
        ffmpeg_binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-ss", f"{start:.6f}",
        "-t", f"{duration:.6f}",
        "-vn", "-c:a", "pcm_s16le",
        str(temporary),
    ]
    try:
        try:
            runner(command, check=True, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            _split_wav_fallback(source, start, duration, temporary)
        if not temporary.is_file() or temporary.stat().st_size <= 44:
            raise ReelGenerationError("Der erzeugte Audioausschnitt ist leer.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def mux_selected_audio(
    video_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    duration_seconds: float,
    ffmpeg_binary: str = "ffmpeg",
    runner: Runner = subprocess.run,
) -> Path:
    """Replace generated audio with the exact user-selected clip.

    The video stream is copied without another visual encode.  WAV input is
    encoded to AAC for broadly compatible Instagram MP4 playback.
    """
    video = Path(video_path).resolve(strict=True)
    audio = Path(audio_path).resolve(strict=True)
    target = Path(output_path).resolve()
    if not video.is_file() or not audio.is_file():
        raise ValueError("Video and audio inputs must be files")
    if target in {video, audio} or target.suffix.casefold() != ".mp4":
        raise ValueError("Final reel must be a separate MP4 file")
    duration = float(duration_seconds)
    if not 0 < duration <= 60:
        raise ValueError("Final reel duration is outside the supported range")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.mp4")
    command = [
        ffmpeg_binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.6f}", "-movflags", "+faststart", "-map_metadata", "-1",
        str(temporary),
    ]
    try:
        try:
            runner(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise ReelGenerationError("FFmpeg wurde nicht gefunden; das Reel konnte nicht finalisiert werden.") from exc
        except subprocess.CalledProcessError as exc:
            raise ReelGenerationError("FFmpeg konnte Video und ausgewählten Audioausschnitt nicht verbinden.") from exc
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ReelGenerationError("FFmpeg hat kein fertiges Reel erzeugt.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


class ReelGenerator:
    """Run configured ComfyUI workflows and persist their results locally."""

    def __init__(
        self,
        client: ComfyClient,
        *,
        image_workflow_path: str | Path,
        video_workflow_path: str | Path,
        reference_workflow_path: str | Path | None = None,
        output_root: str | Path,
        comfy_output_root: str | Path | None = None,
        ffmpeg_binary: str | None = None,
        runner: Runner = subprocess.run,
    ):
        self.client = client
        self.image_workflow_path = Path(image_workflow_path).resolve()
        self.reference_workflow_path = Path(reference_workflow_path).resolve() if reference_workflow_path else None
        self.video_workflow_path = Path(video_workflow_path).resolve()
        self.output_root = Path(output_root).resolve()
        self.comfy_output_root = Path(comfy_output_root).resolve() if comfy_output_root else None
        self.ffmpeg_binary = ffmpeg_binary or globals()["ffmpeg_binary"]()
        self.runner = runner

    def generate_preset_image(
        self, *, reel_id, image_prompt, preset, references=(), edit_prompts=(), progress=None,
    ) -> Path:
        """One native graph owns all scene/identity stages and one resumable id."""
        from .image_presets import build_preset_workflow
        safe_id = _safe_reel_id(reel_id)
        images = [self.client.upload_input(
            ref.reference_image_path, subfolder=f"BookPromo/reels/{safe_id}/preset-inputs",
        ) for ref in references]
        workflow = build_preset_workflow(
            preset, image_prompt, reference_images=images, edit_prompts=edit_prompts,
            filename_prefix=f"BookPromo/reels/{safe_id}/preset-image",
        )
        result = self.client.run_workflow(
            workflow, timeout_sec=7200, partial_execution_targets=["save"],
            **({"progress": progress} if progress is not None else {}),
        )
        reference = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "preset image")
        target = self.output_root / safe_id / f"preset-image-{uuid.uuid4().hex}.png"
        return localize_comfy_output(self.client, reference, target, comfy_output_root=self.comfy_output_root)

    def generate_image(
        self,
        *,
        reel_id: str,
        image_prompt: str,
        timeout_seconds: float = 1800,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        prompt = image_prompt.strip()
        if not prompt or len(prompt) > 20_000:
            raise ValueError("Image prompt is empty or too long")
        safe_id = _safe_reel_id(reel_id)
        workflow = randomize_workflow_seeds(load_workflow(self.image_workflow_path))
        workflow = inject_image_prompt(workflow, prompt)
        workflow = with_output_prefix(workflow, f"BookPromo/reels/{safe_id}/image")
        result = self.client.run_workflow(
            workflow,
            {"prompt": prompt, "image_prompt": prompt},
            timeout_sec=timeout_seconds,
            **({"progress": progress} if progress is not None else {}),
        )
        reference = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "image")
        extension = Path(reference).suffix.casefold() or ".png"
        target = self.output_root / safe_id / f"image-{uuid.uuid4().hex}{extension}"
        return localize_comfy_output(self.client, reference, target, comfy_output_root=self.comfy_output_root)

    def apply_character_references(
        self,
        *,
        reel_id: str,
        scene_image_path: str | Path,
        reference_sheet_path: str | Path,
        identity_context: str,
        timeout_seconds: float = 1800,
    ) -> Path:
        if self.reference_workflow_path is None:
            raise ReelGenerationError("Kein ComfyUI-Referenzworkflow konfiguriert.")
        safe_id = _safe_reel_id(reel_id)
        scene = Path(scene_image_path).resolve(strict=True)
        sheet = Path(reference_sheet_path).resolve(strict=True)
        if not scene.is_file() or not sheet.is_file():
            raise ValueError("Scene image and character reference sheet must be files")
        subfolder = f"BookPromo/reels/{safe_id}/character-inputs"
        uploaded_scene = self.client.upload_image(scene, subfolder=subfolder)
        uploaded_sheet = self.client.upload_image(sheet, subfolder=subfolder)
        workflow = randomize_workflow_seeds(load_workflow(self.reference_workflow_path))
        prompt = build_reference_edit_prompt(identity_context)
        workflow = inject_reference_inputs(workflow, uploaded_scene, uploaded_sheet, prompt)
        workflow = with_output_prefix(workflow, f"BookPromo/reels/{safe_id}/character-scene")
        targets = output_node_ids(workflow, {"SaveImage"})
        result = self.client.run_workflow(
            workflow, timeout_sec=timeout_seconds, partial_execution_targets=targets or None,
        )
        reference = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "character image")
        extension = Path(reference).suffix.casefold() or ".png"
        target = self.output_root / safe_id / f"character-scene-{uuid.uuid4().hex}{extension}"
        return localize_comfy_output(
            self.client, reference, target, comfy_output_root=self.comfy_output_root,
        )

    def restage_character_references(
        self,
        *,
        reel_id: str,
        scene_image_path: str | Path | None = None,
        references: list[CharacterReferenceSpec],
        scene_prompt: str = "",
        scene_direction: str = "",
        scene_plan: ScenePlan | None = None,
        simple_scene: bool = False,
        target_size: tuple[int, int] | None = None,
        timeout_seconds: float = 1800,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        """Create a new scene from intact identity references and scene text.

        The old scene is read for aspect/size only. It is never uploaded,
        encoded, masked, or used as an identity/pose condition in this mode.
        Existing masked editing and image selection remain independent.
        """
        if self.reference_workflow_path is None:
            raise ReelGenerationError("Kein ComfyUI-Referenzworkflow konfiguriert.")
        safe_id = _safe_reel_id(reel_id)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Reference scene timeout must be positive and finite")
        if type(simple_scene) is not bool or simple_scene and (scene_plan is None or scene_image_path is not None):
            raise ValueError("The simple reference scene needs a plan and no previous scene image")
        if scene_plan is not None:
            if (not isinstance(scene_prompt, str) or not isinstance(scene_direction, str)
                    or scene_prompt.strip() or scene_direction.strip()):
                raise ValueError("A consolidated scene plan cannot include a separate scene prompt or direction")
            prompt = (build_simple_reference_prompt(scene_plan, references) if simple_scene
                      else build_planned_reference_prompt(scene_plan, references))
        else:
            prompt = build_reference_scene_prompt(scene_prompt, references, scene_direction=scene_direction)
        if target_size is not None:
            if scene_image_path is not None:
                raise ValueError("Use either an explicit output size or the legacy scene size")
            if (not isinstance(target_size, tuple) or len(target_size) != 2
                    or any(type(value) is not int or value < 16 for value in target_size)):
                raise ValueError("Output dimensions must be positive integer pixels")
            width, height = _reference_scene_size(target_size)
        elif scene_image_path is not None:
            source = Path(scene_image_path).resolve(strict=True)
            with Image.open(source) as opened:
                width, height = _reference_scene_size(opened.size)
        else:
            # Fresh scenes must not inherit the old image's small face resolution.
            width, height = 736, 1312
        paths = []
        for item in references:
            path = Path(item.reference_image_path).resolve(strict=True)
            with Image.open(path) as opened:
                opened.verify()
            paths.append(path)
        # Validate the active native profile before the first upload or job.
        # No scene LoadImage or source VAE latent is carried into this graph.
        workflow = _build_reference_scene_workflow(
            load_workflow(self.reference_workflow_path), width=width, height=height,
            reference_images=[f"reference-{index}.png" for index in range(len(paths))],
            prompt=prompt, filename_prefix=f"BookPromo/reels/{safe_id}/reference-scene",
        )
        subfolder = f"BookPromo/reels/{safe_id}/reference-scene-inputs"
        for index, path in enumerate(paths):
            workflow[f"restage-reference-{index}-load"]["inputs"]["image"] = self.client.upload_image(
                path, subfolder=subfolder,
            )
        result = self.client.run_workflow(
            workflow, timeout_sec=timeout_seconds, partial_execution_targets=["restage-save"],
            **({"progress": progress} if progress is not None else {}),
        )
        if not result.ok:
            raise ReelGenerationError("Die referenzgestützte Szene konnte nicht erzeugt werden: " + str(result.error))
        output = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "reference scene")
        extension = Path(output).suffix.casefold() or ".png"
        target = self.output_root / safe_id / f"reference-scene-{uuid.uuid4().hex}{extension}"
        return localize_comfy_output(self.client, output, target, comfy_output_root=self.comfy_output_root)

    def resume_image(self, *, reel_id: str, prompt_id: str, timeout_seconds: float = 1800) -> Path:
        """Recover a submitted image after an app restart, never generate a duplicate."""
        safe_id = _safe_reel_id(reel_id)
        result = self.client.wait_for_prompt(prompt_id, timeout_sec=timeout_seconds)
        if not result.ok:
            raise ReelGenerationError("Der vorhandene ComfyUI-Lauf konnte nicht übernommen werden: " + str(result.error))
        output = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "resumed image")
        target = self.output_root / safe_id / f"resumed-image-{uuid.uuid4().hex}{Path(output).suffix.casefold() or '.png'}"
        return localize_comfy_output(self.client, output, target, comfy_output_root=self.comfy_output_root)

    def apply_character_references_masked(
        self,
        *,
        reel_id: str,
        scene_image_path: str | Path,
        references: list[CharacterReferenceSpec],
        timeout_seconds: float = 1800,
    ) -> Path:
        """Apply references one character at a time inside validated semantic masks."""
        if self.reference_workflow_path is None:
            raise ReelGenerationError("Kein ComfyUI-Referenzworkflow konfiguriert.")
        if not 1 <= len(references) <= 4:
            raise ValueError("One to four character references are supported")
        safe_id = _safe_reel_id(reel_id)
        source = Path(scene_image_path).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Scene image must be a file")
        names = [item.name.strip() for item in references]
        if any(not name for name in names) or len(set(name.casefold() for name in names)) != len(names):
            raise ValueError("Character reference names must be unique and non-empty")
        if any(not item.selector_prompt.strip() or not item.identity_prompt.strip() for item in references):
            raise ReelGenerationError(
                "Für die automatische Charaktermaske benötigt jede ausgewählte Figur einen "
                "Referenzbild-Prompt in den Bucheinstellungen."
            )
        work = self.output_root / safe_id
        work.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as opened:
            original = opened.convert("RGB")
        masks: list[Image.Image] = []
        for index, item in enumerate(references):
            mask = self._locate_character_mask(
                reel_id=safe_id,
                scene_image_path=source,
                selector_prompt=item.selector_prompt,
                identity_prompt=item.identity_prompt,
                name=item.name,
                single_target=len(references) == 1,
                output_path=work / f"mask-{index}-{uuid.uuid4().hex}.png",
                timeout_seconds=min(timeout_seconds, 600),
            )
            masks.append(mask)

        # Explicit negative traits in a reference prompt (for example "no wings") live outside
        # the ordinary person/creature silhouette.  Detect those traits separately and assign each
        # connected region to the nearest selected character before extending only that mask.
        forbidden_masks = [Image.new("L", original.size, 0) for _ in references]
        for index, item in enumerate(references):
            for feature_index, feature in enumerate(item.forbidden_features):
                feature_path = self._detect_character_mask(
                    reel_id=safe_id,
                    scene_image_path=source,
                    selector_prompt=feature,
                    output_path=work / (
                        f"forbidden-{index}-{feature_index}-{uuid.uuid4().hex}.png"
                    ),
                    timeout_seconds=min(timeout_seconds, 600),
                )
                owned = _owned_feature_mask(feature_path, original.size, masks, index)
                forbidden_masks[index] = ImageChops.lighter(forbidden_masks[index], owned)

        for index, (mask, item) in enumerate(zip(masks, references, strict=True)):
            for previous, previous_item in zip(masks[:index], references[:index], strict=True):
                if _mask_overlap_ratio(mask, previous) > 0.42:
                    raise ReelGenerationError(
                        f"Die automatischen Masken für {previous_item.name} und {item.name} "
                        "überlappen zu stark. Bitte die Referenzbild-Prompts eindeutiger formulieren."
                    )

        current = original
        for index, (item, mask) in enumerate(zip(references, masks, strict=True)):
            reference = Path(item.reference_image_path).resolve(strict=True)
            if not reference.is_file():
                raise ValueError("Character reference image must be a file")
            reference_output_path = work / f"reference-cutout-{index}-{uuid.uuid4().hex}.png"
            reference = self._prepare_character_reference(
                reel_id=safe_id,
                reference_image_path=reference,
                selector_prompt=item.selector_prompt,
                identity_prompt=item.identity_prompt,
                output_path=reference_output_path,
                timeout_seconds=min(timeout_seconds, 600),
            )
            detail_reference = self._prepare_character_detail_reference(
                reel_id=safe_id,
                reference_image_path=reference,
                selector_prompt=item.selector_prompt,
                identity_prompt=item.identity_prompt,
                reference_mask_path=reference_output_path.with_name(f".{reference_output_path.stem}-mask.png"),
                output_path=work / f"reference-detail-{index}-{uuid.uuid4().hex}.png",
                timeout_seconds=min(timeout_seconds, 120),
            )
            forbidden_mask = forbidden_masks[index]
            if forbidden_mask.getbbox() is not None:
                forbidden_box = _padded_mask_box(forbidden_mask, padding_ratio=0.16)
                forbidden_crop = current.crop(forbidden_box)
                forbidden_mask_crop = forbidden_mask.crop(forbidden_box)
                forbidden_crop_path = work / (
                    f"forbidden-scene-{index}-{uuid.uuid4().hex}.png"
                )
                forbidden_crop.save(forbidden_crop_path, format="PNG", optimize=True)
                neutral_reference = work / f"neutral-reference-{index}.png"
                if not neutral_reference.is_file():
                    Image.new("RGB", (512, 512), (112, 112, 112)).save(
                        neutral_reference, format="PNG", optimize=True,
                    )
                cleaned = self._apply_single_character_reference(
                    reel_id=safe_id,
                    scene_image_path=forbidden_crop_path,
                    reference_image_path=neutral_reference,
                    prompt=build_forbidden_feature_removal_prompt(
                        item.name, item.forbidden_features,
                    ),
                    tag=f"forbidden-{index}",
                    timeout_seconds=timeout_seconds,
                    preserve_geometry=False,
                )
                with Image.open(cleaned) as opened:
                    replacement = opened.convert("RGB").resize(
                        forbidden_crop.size, Image.Resampling.LANCZOS,
                    )
                composited = Image.composite(
                    replacement, forbidden_crop, forbidden_mask_crop,
                )
                current.paste(composited, (forbidden_box[0], forbidden_box[1]))
            # Unselected visible figures have no ownership masks. Do not grow
            # the writable region beyond the validated original silhouette.
            edit_mask = _character_edit_mask(mask, [other for pos, other in enumerate(masks) if pos != index])
            box = _padded_mask_box(edit_mask, padding_ratio=0.12)
            scene_crop = current.crop(box)
            mask_crop = edit_mask.crop(box)
            crop_path = work / f"masked-scene-{index}-{uuid.uuid4().hex}.png"
            scene_crop.save(crop_path, format="PNG", optimize=True)
            edited = self._apply_single_character_reference(
                reel_id=safe_id,
                scene_image_path=crop_path,
                reference_image_path=reference,
                detail_reference_image_path=detail_reference,
                prompt=build_masked_reference_edit_prompt(
                    item.name, detail_reference=detail_reference is not None,
                ),
                tag=f"masked-{index}",
                timeout_seconds=timeout_seconds,
                # Native reference editing uses the scene as conditioning, not as
                # a low-noise starting image that locks in its incorrect identity.
                # Only the validated target mask is composited back into the scene.
                preserve_geometry=False,
            )
            with Image.open(edited) as opened:
                replacement = opened.convert("RGB").resize(scene_crop.size, Image.Resampling.LANCZOS)
            composited = Image.composite(replacement, scene_crop, mask_crop)
            current.paste(composited, (box[0], box[1]))

        target = work / f"character-masked-{uuid.uuid4().hex}.png"
        current.save(target, format="PNG", optimize=True)
        return target

    def _locate_character_mask(
        self, *, reel_id: str, scene_image_path: Path, selector_prompt: str,
        identity_prompt: str, name: str, single_target: bool, output_path: Path,
        timeout_seconds: float, reference: bool = False,
    ) -> Image.Image:
        """Try at most two compact queries; never edit from an implausible mask."""
        with Image.open(scene_image_path) as opened:
            size = opened.size
        last_error = None
        # One selected reference does not imply one visible character. Generic
        # "person" lookup is safe only for an isolated reference portrait, never
        # for assigning a scene mask when other figures may be unselected.
        queries = _character_mask_queries(
            selector_prompt, identity_prompt, single_target=single_target and reference,
        )
        for index, query in enumerate(queries):
            candidate = output_path.with_name(f"{output_path.stem}-attempt-{index}{output_path.suffix}")
            # This is a relative activation threshold, not model confidence.
            # Keep more of a silhouette; validate the raw mask before dilation.
            path = self._detect_character_mask(
                reel_id=reel_id, scene_image_path=scene_image_path, selector_prompt=query,
                output_path=candidate, timeout_seconds=timeout_seconds / len(queries), threshold=0.35,
            )
            try:
                return _validated_character_mask(path, size, name, reference=reference)
            except ReelGenerationError as exc:
                last_error = exc
        where = "Referenzbild" if reference else "Szenenbild"
        raise ReelGenerationError(
            f"Die Figur {name} wurde im {where} nicht sicher erkannt. "
            "Die Optimierung wurde abgebrochen; das bisherige Bild bleibt erhalten. "
            f"Details: {last_error}"
        )

    def _prepare_character_reference(
        self,
        *,
        reel_id: str,
        reference_image_path: Path,
        selector_prompt: str,
        output_path: Path,
        timeout_seconds: float,
        identity_prompt: str = "",
    ) -> Path:
        """Remove a portrait's setting so Flux cannot copy it into the book scene."""
        mask_path = output_path.with_name(f".{output_path.stem}-mask.png")
        mask = self._locate_character_mask(
            reel_id=reel_id,
            scene_image_path=reference_image_path,
            selector_prompt=selector_prompt,
            identity_prompt=identity_prompt,
            name="Referenzfigur",
            single_target=True,
            reference=True,
            output_path=mask_path,
            timeout_seconds=timeout_seconds,
        )
        mask.save(mask_path, format="PNG", optimize=True)
        with Image.open(reference_image_path) as opened:
            portrait = opened.convert("RGB")
        ratio = sum(mask.get_flattened_data()) / 255 / (portrait.width * portrait.height)
        # A nearly full-frame reference has no meaningful background to leak and can be used as-is.
        if ratio > 0.97:
            return reference_image_path
        neutral = Image.new("RGB", portrait.size, (112, 112, 112))
        Image.composite(portrait, neutral, mask).save(output_path, format="PNG", optimize=True)
        return output_path

    def _prepare_character_detail_reference(
        self, *, reel_id: str, reference_image_path: Path, selector_prompt: str,
        identity_prompt: str, output_path: Path, timeout_seconds: float,
        reference_mask_path: Path | None = None,
    ) -> Path | None:
        """Enlarge a semantically located head, not a guessed portrait quadrant.

        The complete figure remains authoritative for body and clothing. A
        detail is supplemental and omitted when segmentation cannot safely
        isolate it, including references without a recognizable head.
        """
        # A coherent CLIPSeg component alone does not establish head identity.
        # Require support from the previously validated complete figure, and
        # skip the optional reference if that evidence is unavailable.
        if reference_mask_path is None:
            return None
        try:
            with Image.open(reference_image_path) as opened:
                portrait = opened.convert("RGB")
            with Image.open(reference_mask_path) as opened:
                support_mask = opened.convert("L").resize(portrait.size, Image.Resampling.NEAREST)
        except OSError:
            return None
        queries = _character_detail_queries(selector_prompt, identity_prompt)
        for index, query in enumerate(queries):
            path = output_path.with_name(f".{output_path.stem}-mask-{index}.png")
            try:
                detected = self._detect_character_mask(
                    reel_id=reel_id, scene_image_path=reference_image_path,
                    selector_prompt=query, output_path=path,
                    timeout_seconds=timeout_seconds / len(queries), threshold=0.35,
                )
                box = _validated_detail_box(detected, portrait.size, support_mask=support_mask)
            except (ReelGenerationError, OSError):
                # An uncertain supplemental reference must not stop a valid
                # full-figure transfer or substitute an invented fixed crop.
                continue
            if box is not None:
                portrait.crop(box).save(output_path, format="PNG", optimize=True)
                return output_path
        return None

    def _detect_character_mask(
        self,
        *,
        reel_id: str,
        scene_image_path: Path,
        selector_prompt: str,
        output_path: Path,
        timeout_seconds: float,
        threshold: float = 0.5,
    ) -> Path:
        uploaded = self.client.upload_image(
            scene_image_path, subfolder=f"BookPromo/reels/{reel_id}/mask-inputs",
        )
        prefix = f"BookPromo/reels/{reel_id}/masks"
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": uploaded}},
            # This model is stored below ComfyUI/models after its first load, so later ComfyUI
            # restarts do not depend on an external account or token.  Character identity remains
            # entirely data-driven by selector_prompt.
            "2": {"class_type": "DownloadAndLoadCLIPSeg", "inputs": {
                "model": "Kijai/clipseg-rd64-refined-fp16",
            }},
            "3": {"class_type": "BatchCLIPSeg", "inputs": {
                "images": ["1", 0], "text": selector_prompt, "threshold": threshold,
                "binary_mask": True, "combine_mask": False, "use_cuda": True,
                "blur_sigma": 0.0, "opt_model": ["2", 0],
                "image_bg_level": 0.5, "invert": False,
            }},
            "6": {"class_type": "MaskToImage", "inputs": {"mask": ["3", 0]}},
            "7": {"class_type": "SaveImage", "inputs": {
                "images": ["6", 0], "filename_prefix": prefix,
            }},
        }
        result = self.client.run_workflow(
            workflow, timeout_sec=timeout_seconds, partial_execution_targets=["7"],
        )
        if not result.ok:
            raise ReelGenerationError(
                "Die automatische Charaktermaske konnte nicht erzeugt werden: " + str(result.error)
            )
        reference = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "character mask")
        return self.client.download_output(reference, output_path)

    def _apply_single_character_reference(
        self,
        *,
        reel_id: str,
        scene_image_path: Path,
        reference_image_path: Path,
        prompt: str,
        tag: str,
        timeout_seconds: float,
        denoise: float | None = None,
        preserve_geometry: bool = True,
        detail_reference_image_path: Path | None = None,
    ) -> Path:
        subfolder = f"BookPromo/reels/{reel_id}/masked-inputs"
        uploaded_scene = self.client.upload_image(scene_image_path, subfolder=subfolder)
        uploaded_reference = self.client.upload_image(reference_image_path, subfolder=subfolder)
        workflow = randomize_workflow_seeds(load_workflow(self.reference_workflow_path))
        workflow = inject_reference_inputs(workflow, uploaded_scene, uploaded_reference, prompt)
        if detail_reference_image_path is not None:
            uploaded_detail = self.client.upload_image(detail_reference_image_path, subfolder=subfolder)
            workflow = append_detail_reference_input(workflow, uploaded_detail)
        if not preserve_geometry:
            if _first_node_id(workflow, "TextEncodeQwenImageEditPlus"):
                # Qwen's native sampler uses a full schedule at denoise=1.
                # Its source VAE latent supplies output size; retain that link.
                if not _set_first_class_input(workflow, "KSampler", "denoise", 1.0):
                    raise ReelGenerationError(
                        "Der Qwen-Referenzworkflow unterstützt kein vollständiges referenzgestütztes Editing."
                    )
            else:
                empty_latent = _first_node_id(workflow, "EmptyFlux2LatentImage")
                full_sigmas = _first_node_id(workflow, "Flux2Scheduler")
                if not empty_latent or not full_sigmas:
                    raise ReelGenerationError(
                        "Der Referenzworkflow unterstützt kein vollständiges referenzgestütztes Editing."
                    )
                _set_first_class_input(
                    workflow, "SamplerCustomAdvanced", "latent_image", [empty_latent, 0],
                )
                _set_first_class_input(
                    workflow, "SamplerCustomAdvanced", "sigmas", [full_sigmas, 0],
                )
        if denoise is not None:
            if not 0 < denoise <= 1:
                raise ValueError("Reference edit denoise must be in (0, 1]")
            sampler_class = "KSampler" if _first_node_id(workflow, "TextEncodeQwenImageEditPlus") else "SplitSigmasDenoise"
            if not _set_first_class_input(workflow, sampler_class, "denoise", float(denoise)):
                raise ReelGenerationError(
                    "Der Referenzworkflow unterstützt keine getrennte Merkmalskorrektur."
                )
        workflow = with_output_prefix(workflow, f"BookPromo/reels/{reel_id}/{tag}")
        targets = output_node_ids(workflow, {"SaveImage"})
        result = self.client.run_workflow(
            workflow, timeout_sec=timeout_seconds, partial_execution_targets=targets or None,
        )
        reference = _pick_output(result, {".png", ".jpg", ".jpeg", ".webp"}, "character image")
        target = self.output_root / reel_id / f"{tag}-{uuid.uuid4().hex}{Path(reference).suffix or '.png'}"
        return localize_comfy_output(
            self.client, reference, target, comfy_output_root=self.comfy_output_root,
        )

    def generate_video(
        self,
        *,
        reel_id: str,
        image_path: str | Path,
        audio_clip_path: str | Path,
        duration_seconds: float,
        video_prompt: str,
        timeout_seconds: float = 3600,
    ) -> ReelVideoResult:
        safe_id = _safe_reel_id(reel_id)
        image = Path(image_path).resolve(strict=True)
        audio = Path(audio_clip_path).resolve(strict=True)
        if not image.is_file() or not audio.is_file():
            raise ValueError("Image and selected audio clip must be files")
        duration = float(duration_seconds)
        if not 1 <= duration <= 60:
            raise ValueError("Reel duration must be between 1 and 60 seconds")
        actual_audio_duration = audio_duration(
            audio,
            ffprobe_binary=_sibling_ffprobe(self.ffmpeg_binary),
            runner=self.runner,
        )
        if abs(actual_audio_duration - duration) > 0.05:
            raise ValueError("Selected audio clip duration does not match the reel duration")
        motion = validate_video_prompt(video_prompt)
        subfolder = f"BookPromo/reels/{safe_id}/inputs"
        uploaded_image = self.client.upload_input(image, subfolder=subfolder)
        uploaded_audio = self.client.upload_input(audio, subfolder=subfolder)
        variables = {
            "image_path": uploaded_image,
            "input_image_path": uploaded_image,
            "audio_path": uploaded_audio,
            "duration": f"{duration:.6f}",
            "video_prompt": motion,
            "prompt": motion,
        }
        workflow = randomize_workflow_seeds(load_workflow(self.video_workflow_path))
        workflow = inject_video_inputs(workflow, variables)
        workflow = with_output_prefix(workflow, f"BookPromo/reels/{safe_id}/video")
        targets = output_node_ids(workflow, {"SaveVideo", "SaveWEBM", "VHS_VideoCombine"})
        result = self.client.run_workflow(
            workflow,
            variables,
            timeout_sec=timeout_seconds,
            partial_execution_targets=targets or None,
        )
        reference = _pick_output(result, {".mp4", ".webm", ".mov", ".mkv"}, "video")
        directory = self.output_root / safe_id
        raw_extension = Path(reference).suffix.casefold() or ".mp4"
        raw = directory / f"raw-{uuid.uuid4().hex}{raw_extension}"
        localize_comfy_output(self.client, reference, raw, comfy_output_root=self.comfy_output_root)
        final = directory / f"reel-{uuid.uuid4().hex}.mp4"
        mux_selected_audio(
            raw,
            audio,
            final,
            duration_seconds=duration,
            ffmpeg_binary=self.ffmpeg_binary,
            runner=self.runner,
        )
        return ReelVideoResult(raw, final, result.prompt_id)


def inject_image_prompt(workflow: dict[str, Any], image_prompt: str) -> dict[str, Any]:
    updated = copy.deepcopy(workflow)
    candidates: list[dict[str, Any]] = []
    for node in updated.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        title = str(node.get("_meta", {}).get("title", "")).casefold()
        class_type = str(node.get("class_type", "")).casefold()
        if "text" in inputs and "cliptextencode" in class_type:
            candidates.append(node)
            if "positive" in title or "prompt" in title:
                inputs["text"] = image_prompt
                return updated
        if "prompt" in inputs and ("textencode" in class_type or "qwen" in class_type):
            candidates.append(node)
            if "positive" in title or "prompt" in title:
                inputs["prompt"] = image_prompt
                return updated
    for node in candidates[:1]:
        field = "text" if "text" in node["inputs"] else "prompt"
        node["inputs"][field] = image_prompt
    return updated


def build_simple_reference_prompt(plan: ScenePlan, references: list[CharacterReferenceSpec]) -> str:
    """Bind image, intrinsic appearance and staging once per actor, in one block.

    Indices follow the reference pixel order, NOT actor order or profile-list order.
    Keep legacy compilers separate: this is the controlled new-flow variant.
    """
    if not 1 <= len(references) <= 4:
        raise ValueError("One to four character references are supported")
    plan = ScenePlan.model_validate(plan.model_dump())
    validate_reference_bindings(plan, references)
    labels = {}
    for index, reference in enumerate(references, 1):
        for label in (reference.name, *reference.aliases):
            labels[" ".join(label.split()).casefold()] = (index, reference)
    lines = [
        f"Create one coherent new scene containing exactly {len(plan.actors)} principal characters. "
        "Each of the following named characters appears exactly once; no extra people, no doubles, "
        "no separate human/transformed versions of the same individual."
    ]
    for actor in plan.actors:
        binding = labels.get(" ".join(actor.name.split()).casefold())
        if binding:
            index, reference = binding
            line = f"The single character {actor.name} has the complete visual identity from Image {index}. "
            appearance = reference_appearance_hints(reference.identity_prompt)
            if appearance:
                line += f"Appearance hints describing this SAME individual only: {appearance}. "
        else:
            line = f"The single character {actor.name}. "
        line += "Pose and expression of this SAME individual: " + actor.pose
        if actor.contacts:
            line += " Contacts: " + "; ".join(
                f"{contact.part} {contact.action} with object {contact.object_id}" for contact in actor.contacts
            ) + "."
        if actor.free_parts:
            line += " Unoccupied existing body parts: " + ", ".join(actor.free_parts) + "."
        lines.append(line)
    lines.append(
        "Each assigned reference image is authoritative for its character's exact face/head, "
        "hair/fur/surface, intrinsic anatomy, body build and clothing construction. "
        "Adapt these features naturally to the specified pose and expression. "
        "Do not transfer the reference pose, background, held objects or rendering style. "
        "Do not merge identities or replace a referenced creature with a generic human."
    )
    lines.extend([f"SCENE: {plan.setting}", f"COMPOSITION: {plan.composition}", f"STYLE: {plan.art_direction}"])
    if plan.props:
        lines.append("SCENE OBJECTS: " + "; ".join(
            f"{prop.id}: exactly {prop.count} {prop.label}" for prop in plan.props
        ) + ".")
    else:
        lines.append("No held scene props.")
    lines.append("Keep object shapes and existing anatomy coherent; no lettering, panels or watermarks.")
    prompt = "\n".join(lines)
    if len(prompt) > 20000:
        raise ValueError("Scene plan prompt including character mapping is too long")
    return prompt


def build_planned_reference_prompt(plan: ScenePlan, references: list[CharacterReferenceSpec]) -> str:
    """Compile one scene plus ordered visual identities and appearance-only hints."""
    if not 1 <= len(references) <= 4:
        raise ValueError("One to four character references are supported")
    validate_reference_bindings(plan, [
        {"name": item.name, "aliases": list(item.aliases)} for item in references
    ])
    mapping = []
    for index, item in enumerate(references, 1):
        name = " ".join(item.name.split())
        aliases = ", ".join(" ".join(alias.split()) for alias in item.aliases if alias.strip())
        label = f"Image {index}: {name}" + (f" (also called {aliases})" if aliases else "")
        appearance = reference_appearance_hints(item.identity_prompt)
        if appearance:
            label += f". Appearance hints for {name} ONLY, from its reference settings: {appearance}"
        mapping.append(label + ".")
    prompt = (
        compile_scene_plan(plan) + "\n\nIdentity references: " + " ".join(mapping) + " "
        "Each image is the complete visual identity of its assigned character. Preserve its "
        "face/head, hair/fur/surface, anatomy, proportions, clothing construction and worn accessories. "
        "Adapt those identities to the plan's artistic style, pose and composition. "
        "Reference backgrounds, poses, rendering styles and held props are not scene instructions. "
        "Appearance hints apply only to their assigned identity; never transfer anatomy, exclusions "
        "or clothing between characters. Keep distinctive nonhuman anatomy visible and recognizable "
        "within the planned composition; do not replace a referenced creature with a generic human. "
        "The scene plan defines all scene objects and contacts."
    )
    if len(prompt) > 20_000:
        raise ValueError("Scene plan prompt including character mapping is too long")
    return prompt


def build_reference_scene_prompt(
    scene_prompt: str, references: list[CharacterReferenceSpec], *, scene_direction: str = "",
) -> str:
    """Separate scene/style text from ordered, independently visible identities."""
    scene = scene_prompt.strip()
    if not scene or len(scene) > 20_000:
        raise ValueError("Reference scene prompt is empty or too long")
    if not isinstance(scene_direction, str) or len(scene_direction) > 2_000:
        raise ValueError("Scene direction must be text of at most 2000 characters")
    direction = scene_direction.strip()
    if not 1 <= len(references) <= 4:
        raise ValueError("One to four character references are supported")
    names = [" ".join(item.name.split()) for item in references]
    if any(not name for name in names) or len({name.casefold() for name in names}) != len(names):
        raise ValueError("Character reference names must be unique and non-empty")
    owners = {}
    for index, (item, name) in enumerate(zip(references, names, strict=True)):
        for label in (name, *item.aliases):
            normalized = " ".join(label.split()).casefold()
            if not normalized:
                continue
            if normalized in owners and owners[normalized] != index:
                raise ValueError("Character reference names and aliases must identify only one character")
            owners[normalized] = index
    mapping = []
    for index, (item, name) in enumerate(zip(references, names, strict=True), 1):
        aliases = ", ".join(" ".join(alias.split()) for alias in item.aliases if alias.strip())
        label = f"Image {index}: {name}" + (f" (also called {aliases})" if aliases else "")
        if item.forbidden_features:
            label += "; do not add " + ", ".join(item.forbidden_features)
        mapping.append(label + ".")
    blocking = (
        "Explicit staging direction for this render: " + direction + " "
        "This direction is authoritative for concrete body/limb positions, gestures, "
        "scene-object choices and their physical contact relationships, replacing vague "
        "poses or alternative prop options above. Retain the described story action, "
        "setting and artistic style; retain each reference identity, anatomy and outfit. "
    ) if direction else ""
    prompt = (
        "Create a distinctly new scene featuring the exact characters from the reference images. "
        + " ".join(mapping) + " "
        "Each image is an independent complete identity reference, not a character sheet. "
        "Preserve each character's recognizable face/head, hair/fur/surface, age, body build, "
        "proportions, exact clothing/armor construction, sleeve lengths, garment colors and worn "
        "accessories from that character's own reference. "
        "Keep human, animal, creature and mechanical anatomy as shown; never merge or swap identities. "
        "Show every listed character exactly once in the new scene. "
        "The scene description below defines setting, artistic style, action, lighting and composition, "
        "not a replacement appearance or outfit. Adapt reference identity to that artistic style. "
        "Use a distinctly different natural pose with clearly different limb positions and framing, "
        "not the reference photograph's pose or background. "
        "Do not copy held props from references. Use only the objects required by the scene description. "
        "If the scene offers alternatives, choose one coherent option, not a combination. "
        "New scene description: " + scene + " "
        + blocking +
        "Keep the exact reference character identities and outfits, but show the new scene pose and action. "
        "Use clearly different limb positions and gestures from the reference portraits. "
        "Choose a coherent natural pose appropriate to the scene rather than reusing a portrait pose. "
        "If the scene lists alternative held objects, choose one alternative only. "
        "Each held object must be a single complete plausible object, without duplicates, fused "
        "objects, or extra handles. Completely remove any reference-held object not requested by "
        "the new scene, including its handle and fragments; never combine it with the new scene object. "
        "Use only the minimum number of held objects needed for the chosen action. "
        "Where the character's anatomy has hands or other manipulating limbs, they must "
        "grip or interact with scene objects naturally. Keep anatomically correct limbs "
        "and contacts, no extra limbs and no objects passing through the body. "
        "No text, labels, names, captions, signatures, logos or watermarks."
    )
    if len(prompt) > 20_000:
        raise ValueError("Reference scene prompt including character mapping is too long")
    return prompt


def _reference_scene_size(size: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    if (type(width) is not int or type(height) is not int or min(width, height) < 16
            or max(width, height) > 1_000_000 or max(width, height) / min(width, height) > 8):
        raise ValueError("Reference scene dimensions are too small or extreme")
    factor = min(1.0, math.sqrt(1_000_000 / (width * height)))
    return max(16, math.floor(width * factor / 16) * 16), max(16, math.floor(height * factor / 16) * 16)


def _build_reference_scene_workflow(
    profile: dict[str, Any], *, width: int, height: int,
    reference_images: list[str], prompt: str, filename_prefix: str,
) -> dict[str, Any]:
    """Derive a reference-only native Flux graph from the active profile roots.

    Only a supported, unambiguous loader/sampler path is accepted. Unsupported
    model/LoRA wrappers are rejected rather than silently discarded, and
    disconnected editor nodes never affect the selected active settings.
    """
    error = "Der Referenzworkflow unterstützt keine eindeutige native Flux-2-Klein-Base-Szenenerstellung."
    if (type(width) is not int or type(height) is not int or min(width, height) < 16
            or width % 16 or height % 16 or width * height > 1_000_000
            or max(width, height) / min(width, height) > 8):
        raise ValueError("Invalid native reference scene dimensions")
    if not 1 <= len(reference_images) <= 4 or any(not isinstance(image, str) or not image.strip() for image in reference_images):
        raise ValueError("One to four non-empty independent reference images are required")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
        raise ValueError("Reference scene prompt is empty or too long")

    def node_inputs(node: dict[str, Any]) -> dict[str, Any]:
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise ReelGenerationError(error)
        return inputs

    def linked(node: dict[str, Any], field: str, class_type: str | tuple[str, ...], *, output: int = 0) -> dict[str, Any]:
        inputs = node_inputs(node)
        link = inputs.get(field)
        if (not isinstance(link, list) or len(link) != 2 or type(link[1]) is not int or link[1] != output
                or not isinstance(link[0], str) or link[0] not in profile):
            raise ReelGenerationError(error)
        target = profile[link[0]]
        classes = (class_type,) if isinstance(class_type, str) else class_type
        if not isinstance(target, dict) or target.get("class_type") not in classes:
            raise ReelGenerationError(error)
        return target

    def loader(node: dict[str, Any], field: str) -> dict[str, Any]:
        inputs = node_inputs(node)
        if not isinstance(inputs.get(field), str) or not inputs[field].strip():
            raise ReelGenerationError(error)
        if any(isinstance(value, (list, dict)) for value in inputs.values()):
            raise ReelGenerationError(error)
        return copy.deepcopy(node)

    saves = [node for node in profile.values() if isinstance(node, dict) and node.get("class_type") == "SaveImage"]
    if len(saves) != 1:
        raise ReelGenerationError(error)
    decode = linked(saves[0], "images", "VAEDecode")
    sampler = linked(decode, "samples", "SamplerCustomAdvanced")
    guider = linked(sampler, "guider", "CFGGuider")
    model = loader(linked(guider, "model", "UNETLoader"), "unet_name")
    model_name = model["inputs"]["unet_name"].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if not re.match(r"flux[-_]?2[-_]klein[-_]base[-_](?:4b|9b)(?:[-_.]|$)", model_name):
        raise ReelGenerationError(error)
    vae = loader(linked(decode, "vae", "VAELoader"), "vae_name")
    def encoder(branch: str) -> tuple[str, dict[str, Any]]:
        current, field = guider, branch
        seen = set()
        while True:
            target = linked(current, field, ("ReferenceLatent", "CLIPTextEncode"))
            node_id = current["inputs"][field][0]
            if node_id in seen:
                raise ReelGenerationError(error)
            seen.add(node_id)
            if target["class_type"] == "CLIPTextEncode":
                if not isinstance(node_inputs(target).get("text"), str):
                    raise ReelGenerationError(error)
                return node_id, target
            encoded = linked(target, "latent", "VAEEncode")
            if node_inputs(encoded).get("vae") != decode["inputs"].get("vae"):
                raise ReelGenerationError(error)
            current, field = target, "conditioning"

    positive_id, positive = encoder("positive")
    negative_id, negative = encoder("negative")
    if positive_id == negative_id:
        raise ReelGenerationError(error)
    clip = loader(linked(positive, "clip", "CLIPLoader"), "clip_name")
    if negative.get("inputs", {}).get("clip") != positive["inputs"].get("clip") or clip["inputs"].get("type") != "flux2":
        raise ReelGenerationError(error)
    noise = copy.deepcopy(linked(sampler, "noise", "RandomNoise"))
    if (type(node_inputs(noise).get("noise_seed")) is not int
            or set(noise["inputs"]) != {"noise_seed"}):
        raise ReelGenerationError(error)
    sampler_select = loader(linked(sampler, "sampler", "KSamplerSelect"), "sampler_name")
    sigma_link = sampler.get("inputs", {}).get("sigmas")
    if (not isinstance(sigma_link, list) or len(sigma_link) != 2
            or not isinstance(sigma_link[0], str) or sigma_link[0] not in profile):
        raise ReelGenerationError(error)
    sigma_node = profile[sigma_link[0]]
    if not isinstance(sigma_node, dict) or type(sigma_link[1]) is not int:
        raise ReelGenerationError(error)
    if sigma_node.get("class_type") == "SplitSigmasDenoise":
        if sigma_link[1] not in (0, 1):
            raise ReelGenerationError(error)
        linked(sigma_node, "sigmas", "Flux2Scheduler")
    elif sigma_node.get("class_type") != "Flux2Scheduler" or sigma_link[1] != 0:
        raise ReelGenerationError(error)
    noise["inputs"]["noise_seed"] = secrets.randbits(63)
    workflow = {
        "restage-model": model, "restage-clip": clip, "restage-vae": vae,
        "restage-noise": noise, "restage-sampler-select": sampler_select,
        "restage-positive": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["restage-clip", 0], "text": prompt}},
        "restage-negative": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["restage-clip", 0], "text": negative["inputs"].get("text", "")}},
        "restage-empty": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "restage-sigmas": {"class_type": "Flux2Scheduler", "inputs": {"steps": 50, "width": width, "height": height}},
        "restage-guider": {"class_type": "CFGGuider", "inputs": {"model": ["restage-model", 0], "cfg": 4}},
        "restage-sampler": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["restage-noise", 0], "guider": ["restage-guider", 0], "sampler": ["restage-sampler-select", 0],
            "sigmas": ["restage-sigmas", 0], "latent_image": ["restage-empty", 0],
        }},
        "restage-decode": {"class_type": "VAEDecode", "inputs": {"samples": ["restage-sampler", 0], "vae": ["restage-vae", 0]}},
        "restage-save": {"class_type": "SaveImage", "inputs": {"images": ["restage-decode", 0], "filename_prefix": filename_prefix}},
    }
    branches = {"positive": "restage-positive", "negative": "restage-negative"}
    for index, image in enumerate(reference_images):
        prefix = f"restage-reference-{index}"
        workflow[f"{prefix}-load"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        workflow[f"{prefix}-scale"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [f"{prefix}-load", 0], "upscale_method": "lanczos", "megapixels": 1, "resolution_steps": 1,
        }}
        workflow[f"{prefix}-encode"] = {"class_type": "VAEEncode", "inputs": {"pixels": [f"{prefix}-scale", 0], "vae": ["restage-vae", 0]}}
        for branch in branches:
            node_id = f"{prefix}-{branch}"
            workflow[node_id] = {"class_type": "ReferenceLatent", "inputs": {
                "conditioning": [branches[branch], 0], "latent": [f"{prefix}-encode", 0],
            }}
            branches[branch] = node_id
    for branch, node_id in branches.items():
        workflow["restage-guider"]["inputs"][branch] = [node_id, 0]
    return workflow


def build_reference_edit_prompt(identity_context: str) -> str:
    context = " ".join(str(identity_context).split())
    if not context or len(context) > 4_000:
        raise ValueError("Character identity context is empty or too long")
    return (
        "Edit Image 1 using Image 2 only as a character identity reference sheet. "
        "Image 1 is the sole source of truth for the scene and composition. Ignore every "
        "reference portrait's background, scenery, props, lighting, framing and rectangular "
        "image boundaries in Image 2; they are not part of the character identity. "
        f"{context} Match each named person already visible in Image 1 with the corresponding "
        "portrait position in Image 2. Use each visual matching description only to locate the "
        "correct target person in Image 1; Image 2 is authoritative only for character identity "
        "features, body proportions, clothing and worn accessories, never for the scene, pose or lighting. "
        "Treat every stated scene-person/reference-portrait pair as an isolated edit: identity "
        "features from that portrait may modify only its named target person and no other face, "
        "hair, or body in Image 1. "
        "Transfer face, facial structure, skin tone, age impression, "
        "complete hair, hairline, hair color, hairstyle, facial hair, glasses, stable body shape, "
        "outfit, garment shapes, garment materials, colors and worn accessories. "
        "Re-render these identity features naturally within the existing scene, adapting them "
        "to Image 1's pose, perspective, scale, illumination, color grading, shadows and "
        "occlusion. Do not paste or overlay any reference portrait or rectangular image region. "
        "Do not swap identities. Preserve Image 1's exact number of people, left-to-right placement, "
        "pose, expression, gaze, interaction, background, scene props, lighting, camera angle, "
        "framing, atmosphere and original visual style. Produce one seamless, coherent scene "
        "with no collage, pasted cutout, inset image, visible rectangle or hard compositing edge. "
        "Do not add or remove people, do not copy the reference-sheet background, and do not "
        "add text, labels, borders, panels or watermarks."
    )


def build_masked_reference_edit_prompt(name: str, *, detail_reference: bool = False) -> str:
    """Keep visual identity and scene style independent for every character type.

    The portrait-generation prompt is deliberately not appended: it can contain
    a competing medium, setting or pose from a different book/context.
    That prompt still locates masks; the actual portrait supplies visual identity.
    """
    character = " ".join(str(name).split())
    if not character:
        raise ValueError("Masked character name is empty")
    detail_role = (
        "Image 3 is an enlarged detail of this SAME Image 2 character, not another character. "
        "Use it to accurately match the head shape, facial geometry, eyes, hairstyle, facial hair, "
        "markings or constructed components actually visible there. Preserve those exact visual "
        "features; do not invent a different face, beard, species or design. Image 2 remains "
        "authoritative for the complete body and outfit. "
        if detail_reference else ""
    )
    return (
        f"Transfer the exact visual identity from Image 2 to the single existing target character "
        f"{character} in Image 1, maintaining exact likeness. Image 2 is authoritative for "
        "recognizable identity: head and facial structure, eyes, hair, fur, feathers, scales, "
        "surface materials, characteristic colors, markings, intrinsic anatomy, body build, "
        "body proportions, clothing, garment shapes, garment materials, garment colors, armor "
        "and worn accessories, as applicable to this character. Replace the target's mismatched "
        "face, build and outfit with Image 2's appearance; do not blend their identities. "
        "Correct the target's mismatched identity features to match Image 2. "
        f"{detail_role}"
        "Image 1 is authoritative for artistic style and rendering medium, pose, expression, gaze, "
        "position, scale, scene props, perspective, lighting, shadows, occlusion and composition. "
        "Render Image 2's identity in Image 1's existing visual style and scene lighting, rather "
        "than copying Image 2's rendering style, pose or setting. Adapt Image 2's exact build and "
        "outfit naturally to Image 1's pose and perspective. Preserve every non-target "
        "pixel, all other characters and the original number of characters. Produce one seamless "
        "scene with the same target in the same place, without duplicate subjects, pasted portraits, "
        "insets, borders, labels, text, logos or watermarks."
    )


def _character_detail_queries(selector: str, identity: str) -> tuple[str, ...]:
    """Ask for anatomy semantically, irrespective of pose or species."""
    text = " ".join(f"{selector} {identity}".split())
    subject = re.split(r",|\b(?:with|wearing|dressed|standing|sitting|lying)\b", selector,
                       maxsplit=1, flags=re.IGNORECASE)[0].strip()
    # A robot head, a fox head and a human head can occur anywhere in an image.
    # Never infer a fixed top-of-frame rectangle from the species or layout.
    human_text = re.sub(r"\b(?:not|no|non)[ -]+(?:a\s+)?human\b", "", text, flags=re.IGNORECASE)
    human = bool(re.search(r"\b(?:man|woman|boy|girl|person|human)\b", human_text, re.IGNORECASE))
    second = "face" if human else f"{' '.join(subject.split()[:8])} head".strip()
    return tuple(dict.fromkeys(("head", second)))


def _validated_detail_box(
    path: Path, size: tuple[int, int], *, support_mask: Image.Image | None = None,
) -> tuple[int, int, int, int] | None:
    """Reject empty, fragmented, whole-figure or background-like detail masks."""
    with Image.open(path) as opened:
        mask = opened.convert("L").resize(size, Image.Resampling.NEAREST).point(
            lambda pixel: 255 if pixel >= 32 else 0,
        )
    box = mask.getbbox()
    if box is None:
        return None
    width, height = size
    area = sum(mask.get_flattened_data()) / 255
    if support_mask is None:
        return None
    support = support_mask.resize(size, Image.Resampling.NEAREST).convert("L").point(
        lambda value: 255 if value >= 128 else 0,
    )
    supported_area = sum(ImageChops.multiply(mask, support).get_flattened_data()) / 255
    if supported_area / area < .9:
        return None
    box_area = (box[2] - box[0]) * (box[3] - box[1])
    if (not .0015 <= area / (width * height) <= .35
            or box_area / (width * height) > .5
            or area / box_area < .25
            or _largest_component_area(mask) / area < .75):
        return None
    padding = max(2, round(max(box[2] - box[0], box[3] - box[1]) * .18))
    return (max(0, box[0] - padding), max(0, box[1] - padding),
            min(width, box[2] + padding), min(height, box[3] + padding))


def _character_edit_mask(mask: Image.Image, other_masks: list[Image.Image]) -> Image.Image:
    """Fence known other figures without expanding into unowned scene pixels."""
    owned = mask.copy()
    for other in other_masks:
        # Include faint feathered boundaries, not just the other body's core.
        blocked = other.point(lambda value: 255 if value > 0 else 0)
        owned = ImageChops.subtract(owned, blocked)
    return owned


def build_forbidden_feature_removal_prompt(
    name: str,
    features: tuple[str, ...],
) -> str:
    character = " ".join(str(name).split())
    forbidden = ", ".join(" ".join(item.split()) for item in features if item.strip())
    if not character or not forbidden:
        raise ValueError("Forbidden-feature edit prompt is incomplete")
    return (
        f"Remove only these incorrect visible features from the existing character {character} "
        f"in Image 1: {forbidden}. Reconstruct only the surrounding Image 1 background, "
        "architecture, lighting and "
        "occlusion naturally where those features were removed. Image 1 remains authoritative for "
        "the character's exact pose, body, face, clothing, scale and location. Image 2 is a neutral "
        "technical placeholder and must contribute no subject, shape, colour or setting. "
        "The removed area must contain empty continuation of Image 1's environment: no person, "
        "face, head, body, limb, hand, clothing, creature or character fragment. Do not add, "
        "duplicate, remove or reposition any person. Produce one seamless scene with no collage, "
        "border, text, logo or watermark."
    )


def _character_mask_queries(selector: str, identity: str, *, single_target: bool) -> tuple[str, ...]:
    """Short segmentation queries are separate from the full appearance/edit prompt."""
    visual = " ".join(selector.split())
    subject = re.split(r",|\b(?:with|wearing|dressed|standing|sitting|lying)\b", visual,
                       maxsplit=1, flags=re.IGNORECASE)[0].strip()
    detailed = " ".join(visual.split()[:18])
    # single_target is used only for an isolated reference portrait by the caller.
    human_text = re.sub(r"\b(?:not|no|non)[ -]+(?:a\s+)?human\b", "", f"{visual} {identity}",
                        flags=re.IGNORECASE)
    human = re.search(r"\b(?:man|woman|boy|girl|person|human)\b", human_text, re.IGNORECASE)
    if single_target and human:
        candidates = ["person", subject]
    elif human:
        nouns = re.findall(r"\b(?:woman|man|girl|boy|person)\b", human_text, re.IGNORECASE)
        noun = nouns[0].casefold() if nouns else (
            "woman" if re.search(r"\bshe\b", identity, re.IGNORECASE) else
            "man" if re.search(r"\bhe\b|\bmasculine\b", identity, re.IGNORECASE) else "person"
        )
        traits = _compact_segmentation_traits(visual, identity)
        candidates = [f"{noun} with {trait}" for trait in traits[:2]] if traits else [detailed, subject]
    else:
        candidates = [detailed, subject]
    queries = tuple(dict.fromkeys(query for query in candidates if query))
    if not queries:
        raise ReelGenerationError("Für die Charaktererkennung fehlt eine sichtbare Beschreibung.")
    return queries


def _compact_segmentation_traits(selector: str, identity: str) -> list[str]:
    """Use short visible anatomy/outfit phrases, not a job title or scene genre."""
    text = f"{selector}. {identity}"
    traits = []
    for feature in (r"hair|fur|feathers|scales", r"coat|jacket|shirt|robe|armor|armour|shell"):
        for clause in re.split(r"[,.!?;]", text):
            if re.search(r"\b(?:no|not|without|never|may|could)\b", clause, re.IGNORECASE):
                continue
            match = re.search(rf"\b(?:{feature})\b", clause, re.IGNORECASE)
            if match is None:
                continue
            before = re.split(r"\b(?:has|have|with|wears|wearing|is|are|his|her|its|their|a|an|the|beneath|to)\b",
                              clause[:match.start()], flags=re.IGNORECASE)[-1]
            words = before.split()[-4:]
            words = [word for word in words if word.casefold() not in ('nearly', 'slightly', 'almost')]
            trait = ' '.join([*words, match.group(0)]).strip()
            # Bare "hair" or "jacket" is not a distinguishing scene locator.
            if len(trait.split()) > 1 and trait.casefold() not in [item.casefold() for item in traits]:
                traits.append(trait)
                break
    return traits


def _largest_component_area(mask: Image.Image) -> int:
    """Measure connected support, not a union of unrelated isolated activations."""
    width, height = mask.size
    source = mask.tobytes()
    visited = bytearray(len(source))
    largest = 0
    for start, value in enumerate(source):
        if not value or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        count = 0
        while stack:
            position = stack.pop()
            count += 1
            y, x = divmod(position, width)
            for neighbor in (position - 1 if x else -1,
                             position + 1 if x + 1 < width else -1,
                             position - width if y else -1,
                             position + width if y + 1 < height else -1):
                if neighbor >= 0 and source[neighbor] and not visited[neighbor]:
                    visited[neighbor] = 1
                    stack.append(neighbor)
        largest = max(largest, count)
    return largest


def _validated_character_mask(path: Path, size: tuple[int, int], name: str,
                              *, reference: bool = False) -> Image.Image:
    with Image.open(path) as opened:
        mask = opened.convert("L").resize(size, Image.Resampling.BILINEAR)
    mask = mask.point(lambda value: 255 if value >= 32 else 0)
    area = sum(mask.get_flattened_data()) / 255
    ratio = area / (size[0] * size[1])
    if ratio < 0.008:
        raise ReelGenerationError(
            f"Für {name} wurde im Szenenbild keine sichere Charaktermaske gefunden. "
            "Bitte den Referenzbild-Prompt mit klaren sichtbaren Merkmalen ergänzen."
        )
    if ratio > 0.78 and not reference:
        raise ReelGenerationError(
            f"Die automatische Charaktermaske für {name} umfasst fast das ganze Bild und wurde "
            "aus Sicherheitsgründen nicht verwendet."
        )
    box = mask.getbbox()
    density = area / ((box[2] - box[0]) * (box[3] - box[1]))
    if density < 0.20 or _largest_component_area(mask) / area < 0.60:
        raise ReelGenerationError(
            f"Die Charaktermaske für {name} besteht aus verstreuten Teilflächen statt einer "
            "sicher erkannten Figur und wurde nicht verwendet."
        )
    return mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(6))


def _mask_overlap_ratio(first: Image.Image, second: Image.Image) -> float:
    first_binary = first.point(lambda value: 255 if value >= 64 else 0)
    second_binary = second.point(lambda value: 255 if value >= 64 else 0)
    intersection = sum(ImageChops.multiply(first_binary, second_binary).get_flattened_data()) / 255
    first_area = sum(first_binary.get_flattened_data()) / 255
    second_area = sum(second_binary.get_flattened_data()) / 255
    return intersection / max(1.0, min(first_area, second_area))


def _owned_feature_mask(
    path: Path,
    size: tuple[int, int],
    character_masks: list[Image.Image],
    owner_index: int,
) -> Image.Image:
    """Keep only connected feature regions nearest to the requested character mask."""
    with Image.open(path) as opened:
        feature = opened.convert("L").resize(size, Image.Resampling.BILINEAR)
    binary = feature.point(lambda value: 255 if value >= 64 else 0)
    width, height = size
    source = bytearray(binary.tobytes())
    visited = bytearray(len(source))
    selected = bytearray(len(source))
    centers = []
    for mask in character_masks:
        box = mask.point(lambda value: 255 if value >= 64 else 0).getbbox()
        centers.append(
            ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            if box else (width / 2, height / 2)
        )
    minimum_area = max(12, round(width * height * 0.0002))
    for start, value in enumerate(source):
        if value == 0 or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        component: list[int] = []
        sum_x = sum_y = 0
        while stack:
            position = stack.pop()
            component.append(position)
            y, x = divmod(position, width)
            sum_x += x
            sum_y += y
            if x and source[position - 1] and not visited[position - 1]:
                visited[position - 1] = 1
                stack.append(position - 1)
            if x + 1 < width and source[position + 1] and not visited[position + 1]:
                visited[position + 1] = 1
                stack.append(position + 1)
            if y and source[position - width] and not visited[position - width]:
                visited[position - width] = 1
                stack.append(position - width)
            if y + 1 < height and source[position + width] and not visited[position + width]:
                visited[position + width] = 1
                stack.append(position + width)
        if len(component) < minimum_area:
            continue
        center = (sum_x / len(component), sum_y / len(component))
        nearest = min(
            range(len(centers)),
            key=lambda candidate: (
                (centers[candidate][0] - center[0]) ** 2
                + (centers[candidate][1] - center[1]) ** 2
            ),
        )
        if nearest == owner_index:
            for position in component:
                selected[position] = 255
    return (
        Image.frombytes("L", size, bytes(selected))
        .filter(ImageFilter.MaxFilter(5))
        .filter(ImageFilter.GaussianBlur(4))
    )


def _padded_mask_box(mask: Image.Image, *, padding_ratio: float) -> tuple[int, int, int, int]:
    box = mask.point(lambda value: 255 if value >= 32 else 0).getbbox()
    if box is None:
        raise ReelGenerationError("Die Charaktermaske ist leer.")
    width, height = mask.size
    padding = max(24, round(max(box[2] - box[0], box[3] - box[1]) * padding_ratio))
    return (
        max(0, box[0] - padding), max(0, box[1] - padding),
        min(width, box[2] + padding), min(height, box[3] + padding),
    )


def inject_reference_inputs(
    workflow: dict[str, Any], scene_image: str, reference_sheet: str, prompt: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(workflow)
    load_nodes = [
        (str(node_id), node) for node_id, node in updated.items()
        if isinstance(node, dict) and str(node.get("class_type", "")).casefold() == "loadimage"
        and isinstance(node.get("inputs"), dict)
    ]
    by_id = {node_id: node for node_id, node in load_nodes}
    ordered = [by_id["76"], by_id["81"]] if {"76", "81"}.issubset(by_id) else [node for _, node in load_nodes[:2]]
    if len(ordered) != 2:
        raise ValueError("Reference workflow needs two LoadImage nodes")
    ordered[0]["inputs"]["image"] = scene_image
    ordered[1]["inputs"]["image"] = reference_sheet
    # Resolve the actual positive conditioning branch before falling back to
    # editor titles/order. Qwen's native edit encoder uses "prompt", not "text".
    positive_id = _reference_positive_encoder_id(updated)
    if positive_id is not None:
        positive = updated[positive_id]
        field = "prompt" if positive["class_type"] == "TextEncodeQwenImageEditPlus" else "text"
        positive["inputs"][field] = prompt
        return updated
    candidates = []
    titled_positive = []
    for node in updated.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        title = str(node.get("_meta", {}).get("title", "")).casefold()
        class_type = str(node.get("class_type", "")).casefold()
        field = "prompt" if class_type == "textencodeqwenimageeditplus" else "text"
        if field in inputs and ("cliptextencode" in class_type or class_type == "textencodeqwenimageeditplus"):
            candidates.append((node, field))
            if "positive" in title and "negative" not in title:
                titled_positive.append((node, field))
    if not candidates:
        raise ValueError("Reference workflow has no positive text encoder")
    if len(titled_positive) == 1:
        node, field = titled_positive[0]
    elif len(candidates) == 1 and not titled_positive:
        node, field = candidates[0]
        if "negative" in str(node.get("_meta", {}).get("title", "")).casefold():
            raise ValueError("Reference workflow has no positive text encoder")
    else:
        raise ValueError("Reference workflow has ambiguous positive text encoders")
    node["inputs"][field] = prompt
    return updated


def _reference_positive_encoder_id(workflow: dict[str, Any]) -> str | None:
    """Follow the sampler's positive branch through conditioning wrappers."""
    starts = []
    negative_starts = []
    for node in workflow.values():
        if node.get("class_type") in ("KSampler", "CFGGuider"):
            link = node.get("inputs", {}).get("positive")
            if isinstance(link, list) and len(link) == 2:
                starts.append(link[0])
            link = node.get("inputs", {}).get("negative")
            if isinstance(link, list) and len(link) == 2:
                negative_starts.append(link[0])
    positive_ids = _reference_conditioning_encoder_ids(workflow, starts)
    if len(positive_ids) > 1:
        raise ValueError("Reference workflow has ambiguous positive text encoders")
    if not positive_ids:
        return None
    positive_id = next(iter(positive_ids))
    if positive_id in _reference_conditioning_encoder_ids(workflow, negative_starts):
        raise ValueError("Reference workflow shares its positive text encoder with negative conditioning")
    return positive_id


def _reference_conditioning_encoder_ids(workflow: dict[str, Any], starts: list[str]) -> set[str]:
    """Resolve all native text encoders, without relying on node order."""
    encoders = set()
    seen = set()
    stack = list(reversed(starts))
    while stack:
        node_id = stack.pop()
        if not isinstance(node_id, str) or node_id in seen or node_id not in workflow:
            continue
        seen.add(node_id)
        node = workflow[node_id]
        if node.get("class_type") in ("CLIPTextEncode", "TextEncodeQwenImageEditPlus"):
            encoders.add(node_id)
            continue
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                stack.append(value[0])
    return encoders


def append_detail_reference_input(workflow: dict[str, Any], detail_image: str) -> dict[str, Any]:
    """Append Image 3 to both native Flux or Qwen conditioning branches."""
    updated = copy.deepcopy(workflow)
    qwen_encoders = [node for node in updated.values() if node.get("class_type") == "TextEncodeQwenImageEditPlus"]
    if qwen_encoders:
        # A custom workflow may already use its third native image slot. Never
        # silently replace that source with our optional semantic head crop.
        if any(encoder.get("inputs", {}).get("image3") is not None for encoder in qwen_encoders):
            raise ReelGenerationError(
                "Der Qwen-Referenzworkflow verwendet Bild 3 bereits; die zusätzliche "
                "Detailreferenz würde eine bestehende Bildquelle ersetzen."
            )
        prefix = "character-detail-load"
        while prefix in updated:
            prefix += "-extra"
        updated[prefix] = {"class_type": "LoadImage", "inputs": {"image": detail_image}}
        for encoder in qwen_encoders:
            encoder["inputs"]["image3"] = [prefix, 0]
        return updated
    vae_id = _first_node_id(updated, "VAELoader")
    guider_id = _first_node_id(updated, "CFGGuider")
    if not vae_id or not guider_id:
        raise ReelGenerationError("Der Referenzworkflow unterstützt keine zusätzliche Detailreferenz.")
    guider = updated[guider_id]["inputs"]
    for branch in ("positive", "negative"):
        link = guider.get(branch)
        if not isinstance(link, list) or len(link) != 2 or link[0] not in updated:
            raise ReelGenerationError("Der Referenzworkflow hat keine gültige Referenz-Konditionierung.")
        if updated[link[0]].get("class_type") != "ReferenceLatent":
            raise ReelGenerationError("Die Detailreferenz benötigt native ReferenceLatent-Ketten.")
    prefix = "character-detail"
    while any(f"{prefix}-{suffix}" in updated for suffix in ("load", "scale", "encode", "positive", "negative")):
        prefix += "-extra"
    load_id, scale_id, encode_id = (f"{prefix}-{suffix}" for suffix in ("load", "scale", "encode"))
    updated[load_id] = {"class_type": "LoadImage", "inputs": {"image": detail_image}}
    updated[scale_id] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
        "image": [load_id, 0], "upscale_method": "lanczos", "megapixels": 1,
        "resolution_steps": 1,
    }}
    updated[encode_id] = {"class_type": "VAEEncode", "inputs": {
        "pixels": [scale_id, 0], "vae": [vae_id, 0],
    }}
    for branch in ("positive", "negative"):
        node_id = f"{prefix}-{branch}"
        updated[node_id] = {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": guider[branch], "latent": [encode_id, 0],
        }}
        guider[branch] = [node_id, 0]
    return updated


def inject_video_inputs(workflow: dict[str, Any], variables: dict[str, str]) -> dict[str, Any]:
    updated = copy.deepcopy(workflow)
    _set_first_class_input(updated, "LoadImage", "image", variables["image_path"])
    if not _set_first_class_input(updated, "LoadAudio", "audio", variables["audio_path"]):
        _set_first_class_input(updated, "VHS_LoadAudio", "audio_file", variables["audio_path"])
    for node in updated.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        title = str(node.get("_meta", {}).get("title", "")).casefold()
        class_type = str(node.get("class_type", "")).casefold()
        if "duration" in title and "value" in inputs:
            inputs["value"] = float(variables["duration"])
        if "prompt" in title:
            if "value" in inputs:
                inputs["value"] = variables["video_prompt"]
            elif "text" in inputs:
                inputs["text"] = variables["video_prompt"]
            elif "prompt" in inputs:
                inputs["prompt"] = variables["video_prompt"]
        # Remove stale browser-only metadata from ComfyUI's audio widget.
        if class_type == "loadaudio" and "audio" in inputs:
            inputs.pop("audioUI", None)
    return updated


def randomize_workflow_seeds(workflow: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(workflow)
    for node in updated.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        for key, value in list(node["inputs"].items()):
            name = str(key).casefold()
            if isinstance(value, int) and (name == "seed" or name.endswith("_seed") or name.endswith(".seed")):
                node["inputs"][key] = secrets.randbelow(2**63 - 1) + 1
    return updated


def localize_comfy_output(
    client: ComfyClient,
    output_reference: str,
    target: str | Path,
    *,
    comfy_output_root: str | Path | None = None,
) -> Path:
    destination = Path(target).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if comfy_output_root is not None:
        root = Path(comfy_output_root).resolve(strict=True)
        candidate = (root / output_reference.replace("\\", "/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            candidate = root / "__outside_output_root__"
        if candidate.is_file():
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copy2(candidate, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination
    return client.download_output(output_reference, destination)


def _pick_output(result, extensions: set[str], kind: str) -> str:
    if not result.ok:
        raise ReelGenerationError(result.error or f"ComfyUI {kind} generation failed")
    for reference in reversed(result.output_files):
        if Path(reference).suffix.casefold() in extensions:
            return reference
    raise ReelGenerationError(f"ComfyUI returned no {kind} output")


def _set_first_class_input(workflow: dict[str, Any], class_type: str, field: str, value: Any) -> bool:
    for node in workflow.values():
        if not isinstance(node, dict) or str(node.get("class_type", "")).casefold() != class_type.casefold():
            continue
        if isinstance(node.get("inputs"), dict):
            node["inputs"][field] = value
            return True
    return False


def _first_node_id(workflow: dict[str, Any], class_type: str) -> str | None:
    for node_id, node in workflow.items():
        if isinstance(node, dict) and str(node.get("class_type", "")).casefold() == class_type.casefold():
            return str(node_id)
    return None


def _split_wav_fallback(source: Path, start: float, duration: float, target: Path) -> None:
    try:
        with wave.open(str(source), "rb") as input_file:
            rate = input_file.getframerate()
            start_frame = min(input_file.getnframes(), max(0, round(start * rate)))
            frame_count = min(input_file.getnframes() - start_frame, max(0, round(duration * rate)))
            input_file.setpos(start_frame)
            frames = input_file.readframes(frame_count)
            with wave.open(str(target), "wb") as output_file:
                output_file.setparams(input_file.getparams())
                output_file.setnframes(0)
                output_file.writeframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ReelGenerationError("FFmpeg ist nicht verfügbar und die Quelldatei ist keine lesbare PCM-WAV-Datei.") from exc


def _bounded_path(path: str | Path, root: str | Path | None, *, must_exist: bool, label: str) -> Path:
    candidate = Path(path).resolve(strict=must_exist)
    if root is not None:
        boundary = Path(root).resolve(strict=True)
        try:
            candidate.relative_to(boundary)
        except ValueError as exc:
            raise ValueError(f"{label} lies outside the allowed directory") from exc
    if must_exist and not candidate.is_file():
        raise ValueError(f"{label} must be a file")
    return candidate


def _safe_reel_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in normalized):
        raise ValueError("Invalid reel ID")
    return normalized


def _sibling_ffprobe(ffmpeg_binary: str) -> str:
    path = Path(ffmpeg_binary)
    if path.name.casefold() in {"ffmpeg", "ffmpeg.exe"} and path.parent != Path("."):
        suffix = ".exe" if path.suffix.casefold() == ".exe" else ""
        return str(path.with_name("ffprobe" + suffix))
    return "ffprobe"
