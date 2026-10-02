"""Reusable local image/audio/video generation primitives for quote reels."""

from __future__ import annotations

from array import array
import copy
from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from typing import Any, Callable
import uuid
import wave

from PIL import Image, ImageChops, ImageFilter

from .comfy import ComfyClient, load_workflow, output_node_ids, with_output_prefix
from .reel_prompts import validate_video_prompt


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

    def generate_image(
        self,
        *,
        reel_id: str,
        image_prompt: str,
        timeout_seconds: float = 1800,
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

    def apply_character_references_masked(
        self,
        *,
        reel_id: str,
        scene_image_path: str | Path,
        references: list[CharacterReferenceSpec],
        timeout_seconds: float = 1800,
    ) -> Path:
        """Apply references one character at a time inside semantic SAM masks."""
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
            selector = item.selector_prompt.strip() or "the only visible person"
            mask_path = self._detect_character_mask(
                reel_id=safe_id,
                scene_image_path=source,
                selector_prompt=selector,
                output_path=work / f"mask-{index}-{uuid.uuid4().hex}.png",
                timeout_seconds=min(timeout_seconds, 600),
            )
            mask = _validated_character_mask(mask_path, original.size, item.name)
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
            reference = self._prepare_character_reference(
                reel_id=safe_id,
                reference_image_path=reference,
                selector_prompt=item.selector_prompt,
                output_path=work / f"reference-cutout-{index}-{uuid.uuid4().hex}.png",
                timeout_seconds=min(timeout_seconds, 600),
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
            box = _padded_mask_box(mask, padding_ratio=0.12)
            scene_crop = current.crop(box)
            mask_crop = mask.crop(box)
            crop_path = work / f"masked-scene-{index}-{uuid.uuid4().hex}.png"
            scene_crop.save(crop_path, format="PNG", optimize=True)
            edited = self._apply_single_character_reference(
                reel_id=safe_id,
                scene_image_path=crop_path,
                reference_image_path=reference,
                prompt=build_masked_reference_edit_prompt(item.name, item.identity_prompt),
                tag=f"masked-{index}",
                timeout_seconds=timeout_seconds,
            )
            with Image.open(edited) as opened:
                replacement = opened.convert("RGB").resize(scene_crop.size, Image.Resampling.LANCZOS)
            composited = Image.composite(replacement, scene_crop, mask_crop)
            current.paste(composited, (box[0], box[1]))

        target = work / f"character-masked-{uuid.uuid4().hex}.png"
        current.save(target, format="PNG", optimize=True)
        return target

    def _prepare_character_reference(
        self,
        *,
        reel_id: str,
        reference_image_path: Path,
        selector_prompt: str,
        output_path: Path,
        timeout_seconds: float,
    ) -> Path:
        """Remove a portrait's setting so Flux cannot copy it into the book scene."""
        mask_path = output_path.with_name(f".{output_path.stem}-mask.png")
        self._detect_character_mask(
            reel_id=reel_id,
            scene_image_path=reference_image_path,
            selector_prompt=selector_prompt,
            output_path=mask_path,
            timeout_seconds=timeout_seconds,
        )
        with Image.open(reference_image_path) as opened:
            portrait = opened.convert("RGB")
        with Image.open(mask_path) as opened:
            mask = opened.convert("L").resize(portrait.size, Image.Resampling.BILINEAR)
        mask = mask.point(lambda value: 255 if value >= 32 else 0)
        ratio = sum(mask.get_flattened_data()) / 255 / (portrait.width * portrait.height)
        if ratio < 0.008:
            raise ReelGenerationError(
                "Im Referenzbild wurde die beschriebene Figur nicht sicher erkannt."
            )
        # A nearly full-frame reference has no meaningful background to leak and can be used as-is.
        if ratio > 0.97:
            return reference_image_path
        mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(4))
        neutral = Image.new("RGB", portrait.size, (112, 112, 112))
        Image.composite(portrait, neutral, mask).save(output_path, format="PNG", optimize=True)
        return output_path

    def _detect_character_mask(
        self,
        *,
        reel_id: str,
        scene_image_path: Path,
        selector_prompt: str,
        output_path: Path,
        timeout_seconds: float,
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
                "images": ["1", 0], "text": selector_prompt, "threshold": 0.5,
                "binary_mask": True, "combine_mask": False, "use_cuda": True,
                "blur_sigma": 1.0, "opt_model": ["2", 0],
                "image_bg_level": 0.5, "invert": False,
            }},
            "4": {"class_type": "GrowMask", "inputs": {
                "mask": ["3", 0], "expand": 6, "tapered_corners": True,
            }},
            "5": {"class_type": "FeatherMask", "inputs": {
                "mask": ["4", 0], "left": 10, "top": 10, "right": 10, "bottom": 10,
            }},
            "6": {"class_type": "MaskToImage", "inputs": {"mask": ["5", 0]}},
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
    ) -> Path:
        subfolder = f"BookPromo/reels/{reel_id}/masked-inputs"
        uploaded_scene = self.client.upload_image(scene_image_path, subfolder=subfolder)
        uploaded_reference = self.client.upload_image(reference_image_path, subfolder=subfolder)
        workflow = randomize_workflow_seeds(load_workflow(self.reference_workflow_path))
        workflow = inject_reference_inputs(workflow, uploaded_scene, uploaded_reference, prompt)
        if not preserve_geometry:
            empty_latent = _first_node_id(workflow, "EmptyFlux2LatentImage")
            full_sigmas = _first_node_id(workflow, "Flux2Scheduler")
            if not empty_latent or not full_sigmas:
                raise ReelGenerationError(
                    "Der Referenzworkflow unterstützt keine vollständige Merkmalsentfernung."
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
            if not _set_first_class_input(
                workflow, "SplitSigmasDenoise", "denoise", float(denoise),
            ):
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
        "features, never for the scene, clothing, pose or lighting. "
        "Treat every stated scene-person/reference-portrait pair as an isolated edit: identity "
        "features from that portrait may modify only its named target person and no other face, "
        "hair, or body in Image 1. "
        "Transfer face, facial structure, skin tone, age impression, "
        "complete hair, hairline, hair color, hairstyle, facial hair, glasses and stable body shape. "
        "Re-render these identity features naturally within the existing scene, adapting them "
        "to Image 1's pose, perspective, scale, illumination, color grading, shadows and "
        "occlusion. Do not paste or overlay any reference portrait or rectangular image region. "
        "Do not swap identities. Preserve Image 1's exact number of people, left-to-right placement, "
        "clothing, pose, expression, gaze, interaction, background, props, lighting, camera angle, "
        "framing, atmosphere and original visual style. Produce one seamless, coherent scene "
        "with no collage, pasted cutout, inset image, visible rectangle or hard compositing edge. "
        "Do not add or remove people, do not copy the reference-sheet background, and do not "
        "add text, labels, borders, panels or watermarks."
    )


def build_masked_reference_edit_prompt(name: str, identity_prompt: str) -> str:
    character = " ".join(str(name).split())
    identity = " ".join(str(identity_prompt).split())[:2_000]
    if not character or not identity:
        raise ValueError("Masked character identity is incomplete")
    return (
        f"Edit only the single existing target character {character} in Image 1. "
        "Image 1 is a tightly cropped scene region and is authoritative for the target's exact "
        "pose, body position, clothing, expression, gaze, scale, perspective, lighting, shadows, "
        "occlusion, background and composition. Image 2 is an appearance reference for this same "
        f"character only. Reference-image prompt: {identity} "
        "Transfer identity and stable appearance from Image 2 onto the already existing target in "
        "Image 1. Do not add, duplicate, remove, reposition or replace any person. Do not create a "
        "second copy of the reference character. Do not copy Image 2's pose, clothing, background, "
        "framing or lighting. Preserve every non-target pixel and produce one seamless scene with "
        "no collage, inset, border, label, text, logo or watermark."
    )


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


def _validated_character_mask(path: Path, size: tuple[int, int], name: str) -> Image.Image:
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
    if ratio > 0.78:
        raise ReelGenerationError(
            f"Die automatische Charaktermaske für {name} umfasst fast das ganze Bild und wurde "
            "aus Sicherheitsgründen nicht verwendet."
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
    candidates = []
    for node in updated.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        title = str(node.get("_meta", {}).get("title", "")).casefold()
        if "text" in inputs and "cliptextencode" in str(node.get("class_type", "")).casefold():
            candidates.append(node)
            if "positive" in title:
                inputs["text"] = prompt
                return updated
    if not candidates:
        raise ValueError("Reference workflow has no positive text encoder")
    candidates[0]["inputs"]["text"] = prompt
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
