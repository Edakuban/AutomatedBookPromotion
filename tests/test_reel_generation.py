import json
from pathlib import Path
import subprocess
import wave

import pytest

from bookpromo.comfy import ComfyResult
from bookpromo.reel_generation import (
    ReelGenerator,
    audio_duration,
    build_reference_edit_prompt,
    inject_reference_inputs,
    inject_video_inputs,
    mux_selected_audio,
    split_audio_segment,
    wav_waveform,
)
from bookpromo.reel_prompts import VIDEO_PROMPT_SYSTEM, build_video_prompt_request, validate_video_prompt


def make_wav(path: Path, seconds=1.0, rate=8000):
    frames = b"\x01\x00" * round(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)


def missing_ffmpeg(*args, **kwargs):
    raise FileNotFoundError


def test_audio_duration_and_wav_split_have_sample_accurate_fallback(tmp_path):
    source = tmp_path / "source.wav"
    target = tmp_path / "clips" / "selection.wav"
    make_wav(source, seconds=2)
    assert audio_duration(source) == 2
    split_audio_segment(source, start_seconds=.5, duration_seconds=.75, output_path=target,
                        source_root=tmp_path, output_root=tmp_path, runner=missing_ffmpeg)
    assert audio_duration(target) == .75
    with pytest.raises(ValueError, match="exceeds"):
        split_audio_segment(source, start_seconds=1.8, duration_seconds=.5, output_path=target,
                            runner=missing_ffmpeg)


def test_wav_waveform_returns_bounded_cached_peaks(tmp_path):
    source = tmp_path / "source.wav"
    make_wav(source, seconds=2)
    peaks = wav_waveform(source, bins=240)
    assert len(peaks) == 240
    assert all(0 <= value <= 1 for value in peaks)
    assert any(value > 0 for value in peaks)


def test_audio_split_rejects_path_escape_and_source_overwrite(tmp_path):
    source = tmp_path / "source.wav"
    make_wav(source)
    with pytest.raises(ValueError, match="outside"):
        split_audio_segment(source, start_seconds=0, duration_seconds=.5,
                            output_path=tmp_path.parent / "outside.wav", output_root=tmp_path,
                            runner=missing_ffmpeg)
    with pytest.raises(ValueError, match="differ"):
        split_audio_segment(source, start_seconds=0, duration_seconds=.5,
                            output_path=source, runner=missing_ffmpeg)


def test_video_prompt_contract_requires_visible_controlled_motion():
    request = build_video_prompt_request(image_prompt="A rainy alley", duration_seconds=10,
                                         genre="Thriller", mood="tense", motion_intensity="dynamic")
    assert "10.000 seconds" in request and "A rainy alley" in request
    assert "one continuous shot" in VIDEO_PROMPT_SYSTEM
    assert "Never invent a new singer" in VIDEO_PROMPT_SYSTEM
    assert "may naturally lip-sync" in VIDEO_PROMPT_SYSTEM
    good = "Rain sweeps visibly across the alley. The camera tracks laterally while existing reflections pulse with the rhythm."
    assert validate_video_prompt(good) == good
    with pytest.raises(ValueError, match="unsupported motion"):
        validate_video_prompt("The camera uses subtle movement while rain moves across the window.")

    performance = (
        "The existing woman naturally lip-syncs to the supplied vocals while colored light pulses "
        "with the song. The camera tracks laterally through the continuous shot."
    )
    assert validate_video_prompt(performance) == performance


@pytest.mark.parametrize("ending", [
    "No dialogue, singing, singer, or lip-sync.",
    "The shot continues without singing or lip sync.",
    "Avoid subtle movement, singing, and lip-sync.",
    "The subject must not perform lip-sync or singing.",
])
def test_video_prompt_contract_allows_explicit_negative_constraints(ending):
    prompt = (
        "The camera tracks laterally while rain crosses the window and the existing subject "
        "turns toward the moving light. " + ending
    )
    assert validate_video_prompt(prompt) == prompt


def test_video_workflow_inputs_replace_image_audio_duration_and_prompt():
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        "2": {"class_type": "LoadAudio", "inputs": {"audio": "old.wav", "audioUI": {}}},
        "3": {"class_type": "PrimitiveFloat", "_meta": {"title": "Duration"}, "inputs": {"value": 5}},
        "4": {"class_type": "PrimitiveString", "_meta": {"title": "Video Prompt"}, "inputs": {"value": "old"}},
    }
    updated = inject_video_inputs(workflow, {"image_path": "new.png", "audio_path": "new.wav",
                                             "duration": "10", "video_prompt": "visible rain"})
    assert updated["1"]["inputs"]["image"] == "new.png"
    assert updated["2"]["inputs"] == {"audio": "new.wav"}
    assert updated["3"]["inputs"]["value"] == 10
    assert updated["4"]["inputs"]["value"] == "visible rain"
    assert workflow["1"]["inputs"]["image"] == "old.png"


def test_reference_workflow_receives_scene_sheet_and_unambiguous_identity_map():
    workflow = {
        "76": {"class_type": "LoadImage", "inputs": {"image": "old-scene.png"}},
        "81": {"class_type": "LoadImage", "inputs": {"image": "old-sheet.png"}},
        "113": {"class_type": "CLIPTextEncode", "_meta": {"title": "Positive Prompt"},
                "inputs": {"text": "old"}},
    }
    prompt = build_reference_edit_prompt(
        "Reference sheet identity order, left to right: left portrait is Kira; right portrait is Lukas."
    )
    updated = inject_reference_inputs(workflow, "scene.png", "sheet.png", prompt)
    assert updated["76"]["inputs"]["image"] == "scene.png"
    assert updated["81"]["inputs"]["image"] == "sheet.png"
    assert "left portrait is Kira" in updated["113"]["inputs"]["text"]
    assert "Do not swap identities" in updated["113"]["inputs"]["text"]
    assert "Image 2 is authoritative" in updated["113"]["inputs"]["text"]
    assert "isolated edit" in updated["113"]["inputs"]["text"]
    assert workflow["76"]["inputs"]["image"] == "old-scene.png"


def test_reference_edit_ignores_portrait_background_and_integrates_identity_into_scene():
    prompt = build_reference_edit_prompt("The sole reference portrait shows Sam.")
    assert "Image 1 is the sole source of truth for the scene and composition" in prompt
    assert "Ignore every reference portrait's background" in prompt
    assert "never for the scene, clothing, pose or lighting" in prompt
    assert "Do not paste or overlay any reference portrait or rectangular image region" in prompt
    assert "illumination, color grading, shadows and occlusion" in prompt
    assert "one seamless, coherent scene" in prompt
    assert "original visual style" in prompt


def test_final_mux_maps_selected_audio_as_only_audio_track(tmp_path):
    video, audio, target = tmp_path / "raw.mp4", tmp_path / "clip.wav", tmp_path / "final.mp4"
    video.write_bytes(b"raw")
    make_wav(audio)
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"final")
        return subprocess.CompletedProcess(command, 0, "", "")

    mux_selected_audio(video, audio, target, duration_seconds=1, runner=runner)
    command = commands[0]
    assert command[command.index("-map") + 1] == "0:v:0"
    second_map = command.index("-map", command.index("-map") + 1)
    assert command[second_map + 1] == "1:a:0"
    assert target.read_bytes() == b"final"


def test_generator_uploads_inputs_localizes_output_and_remuxes_audio(tmp_path):
    image_workflow = tmp_path / "image.json"
    video_workflow = tmp_path / "video.json"
    image_workflow.write_text(json.dumps({"1": {"class_type": "CLIPTextEncode", "_meta": {"title": "Positive"},
                                                      "inputs": {"text": "old"}}}), encoding="utf-8")
    video_workflow.write_text(json.dumps({
        "1": {"class_type": "LoadImage", "inputs": {"image": "old"}},
        "2": {"class_type": "LoadAudio", "inputs": {"audio": "old"}},
        "3": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "old"}},
    }), encoding="utf-8")
    source_image = tmp_path / "source.png"
    source_image.write_bytes(b"png")
    audio = tmp_path / "selection.wav"
    make_wav(audio)

    class FakeClient:
        def __init__(self): self.uploads = []; self.calls = []
        def upload_input(self, path, *, subfolder):
            self.uploads.append((Path(path), subfolder))
            return f"{subfolder}/{Path(path).name}"
        def run_workflow(self, workflow, variables, **kwargs):
            self.calls.append((workflow, variables, kwargs))
            output = "image.png" if len(self.calls) == 1 else "video.mp4"
            return ComfyResult(f"p{len(self.calls)}", True, [output])
        def download_output(self, reference, target):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(reference.encode())
            return Path(target)

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"muxed")
        return subprocess.CompletedProcess(command, 0, "", "")

    client = FakeClient()
    generator = ReelGenerator(client, image_workflow_path=image_workflow,
                              video_workflow_path=video_workflow, output_root=tmp_path / "output", runner=runner)
    generated_image = generator.generate_image(reel_id="quote-1", image_prompt="dark forest")
    result = generator.generate_video(reel_id="quote-1", image_path=source_image, audio_clip_path=audio,
                                      duration_seconds=1, video_prompt=(
                                          "Rain moves clearly through the forest. The camera tracks laterally while branches sway with the rhythm."
                                      ))
    assert generated_image.read_bytes() == b"image.png"
    assert result.raw_video_path.read_bytes() == b"video.mp4"
    assert result.final_video_path.read_bytes() == b"muxed"
    assert len(client.uploads) == 2
    assert client.calls[1][2]["partial_execution_targets"] == ["3"]
