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
        f"{context} Match each named person already visible in Image 1 with the corresponding "
        "portrait position in Image 2. Use each visual matching description only to locate the "
        "correct target person in Image 1; Image 2 is authoritative for identity and appearance. "
        "Treat every stated scene-person/reference-portrait pair as an isolated edit: identity "
        "features from that portrait may modify only its named target person and no other face, "
        "hair, or body in Image 1. "
        "Transfer face, facial structure, skin tone, age impression, "
        "complete hair, hairline, hair color, hairstyle, facial hair, glasses and stable body shape. "
        "Do not swap identities. Preserve Image 1's exact number of people, left-to-right placement, "
        "clothing, pose, expression, gaze, interaction, background, props, lighting, camera angle, "
        "framing, atmosphere and photorealistic style. Do not add or remove people, do not copy the "
        "reference-sheet background, and do not add text, labels, borders, panels or watermarks."
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
