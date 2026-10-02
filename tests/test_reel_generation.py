import json
from pathlib import Path
import subprocess
import wave

import pytest
from PIL import Image

from bookpromo.comfy import ComfyResult
from bookpromo.reel_generation import (
    CharacterReferenceSpec,
    ReelGenerator,
    audio_duration,
    build_masked_reference_edit_prompt,
    build_forbidden_feature_removal_prompt,
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


def test_reference_workflow_starts_from_scene_latent_with_bounded_denoise():
    workflow = json.loads(Path("workflows/reel-reference.json").read_text(encoding="utf-8"))
    sampler = workflow["92:103"]["inputs"]
    assert sampler["latent_image"] == ["92:126", 0]
    assert sampler["sigmas"] == ["92:116", 1]
    assert workflow["92:116"]["class_type"] == "SplitSigmasDenoise"
    assert 0.25 <= workflow["92:116"]["inputs"]["denoise"] <= 0.5


def test_forbidden_feature_pass_uses_full_latent_only_inside_external_mask(tmp_path):
    captured = {}

    class FakeClient:
        def upload_image(self, path, *, subfolder):
            return f"{subfolder}/{Path(path).name}"

        def run_workflow(self, workflow, **kwargs):
            captured["workflow"] = workflow
            return ComfyResult("feature-edit", True, ["feature.png"])

        def download_output(self, reference, target):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b"feature")
            return Path(target)

    scene = tmp_path / "scene.png"
    reference = tmp_path / "reference.png"
    scene.write_bytes(b"scene")
    reference.write_bytes(b"reference")
    generator = ReelGenerator(
        FakeClient(),
        image_workflow_path="workflows/reel-image.json",
        reference_workflow_path="workflows/reel-reference.json",
        video_workflow_path="workflows/reel-video.json",
        output_root=tmp_path / "output",
        ffmpeg_binary="ffmpeg",
    )
    generator._apply_single_character_reference(
        reel_id="feature-pass",
        scene_image_path=scene,
        reference_image_path=reference,
        prompt="remove wings",
        tag="forbidden",
        timeout_seconds=30,
        preserve_geometry=False,
    )
    sampler = captured["workflow"]["92:103"]["inputs"]
    assert sampler["latent_image"] == ["92:109", 0]
    assert sampler["sigmas"] == ["92:115", 0]


def test_masked_reference_prompt_forbids_duplicates_and_preserves_scene():
    prompt = build_masked_reference_edit_prompt(
        "Unit 7", "Weathered brass body, triangular blue eye, white spiral shoulder mark."
    )
    assert "single existing target character Unit 7" in prompt
    assert "Do not add, duplicate, remove, reposition or replace any person" in prompt
    assert "Do not create a second copy" in prompt
    assert "preserve" in prompt.casefold()


def test_forbidden_feature_prompt_removes_traits_without_changing_pose():
    prompt = build_forbidden_feature_removal_prompt(
        "Unit 7", ("wings", "horns"),
    )
    assert "wings, horns" in prompt
    assert "Reconstruct only the surrounding Image 1 background" in prompt
    assert "exact pose" in prompt
    assert "neutral technical placeholder" in prompt
    assert "no person, face, head, body, limb" in prompt
    assert "Do not add, duplicate, remove or reposition any person" in prompt


def test_mask_detection_workflow_is_visual_prompt_driven_and_locally_cached(tmp_path):
    scene = tmp_path / "scene.png"
    Image.new("RGB", (96, 128), "white").save(scene)
    calls = {}

    class FakeClient:
        def upload_image(self, path, *, subfolder):
            calls["upload"] = (Path(path), subfolder)
            return f"{subfolder}/scene.png"

        def run_workflow(self, workflow, **kwargs):
            calls["workflow"] = workflow
            calls["kwargs"] = kwargs
            return ComfyResult("mask-prompt", True, ["mask.png"])

        def download_output(self, reference, target):
            Image.new("L", (96, 128), 255).save(target)
            return Path(target)

    generator = ReelGenerator.__new__(ReelGenerator)
    generator.client = FakeClient()
    output = generator._detect_character_mask(
        reel_id="generic-book-character",
        scene_image_path=scene,
        selector_prompt="weathered brass automaton with a triangular blue eye",
        output_path=tmp_path / "mask.png",
        timeout_seconds=30,
    )
    workflow = calls["workflow"]
    assert output.is_file()
    assert workflow["3"]["inputs"]["text"] == (
        "weathered brass automaton with a triangular blue eye"
    )
    assert workflow["2"]["class_type"] == "DownloadAndLoadCLIPSeg"
    assert workflow["3"]["class_type"] == "BatchCLIPSeg"
    assert calls["kwargs"]["partial_execution_targets"] == ["7"]


def test_masked_reference_edits_arbitrary_characters_sequentially(tmp_path):
    scene = tmp_path / "scene.png"
    Image.new("RGB", (240, 120), "white").save(scene)
    references = []
    for name, prompt, color in (
        ("Clockwork fox", "copper clockwork fox with green glass eyes", "#a06020"),
        ("Crystal golem", "blue crystal golem with a cracked gold chest", "#2060c0"),
    ):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (80, 100), color).save(path)
        references.append(CharacterReferenceSpec(name, path, prompt, prompt))

    mask_paths = []
    for index, box in enumerate(((20, 15, 90, 110), (150, 10, 225, 110))):
        mask = Image.new("L", (240, 120), 0)
        mask.paste(255, box)
        path = tmp_path / f"mask-{index}.png"
        mask.save(path)
        mask_paths.append(path)

    generator = ReelGenerator.__new__(ReelGenerator)
    generator.reference_workflow_path = tmp_path / "reference.json"
    generator.output_root = tmp_path / "output"
    selectors = []
    edit_names = []

    def detect(**kwargs):
        selectors.append(kwargs["selector_prompt"])
        return mask_paths[len(selectors) - 1]

    def edit(**kwargs):
        edit_names.append(kwargs["prompt"])
        with Image.open(kwargs["scene_image_path"]) as crop:
            output = tmp_path / f"edit-{len(edit_names)}.png"
            Image.new("RGB", crop.size, "#111111").save(output)
        return output

    generator._detect_character_mask = detect
    generator._prepare_character_reference = lambda **kwargs: kwargs["reference_image_path"]
    generator._apply_single_character_reference = edit
    result = generator.apply_character_references_masked(
        reel_id="arbitrary-characters", scene_image_path=scene, references=references,
    )
    assert result.is_file()
    assert selectors == [item.selector_prompt for item in references]
    assert "Clockwork fox" in edit_names[0] and "Crystal golem" in edit_names[1]
    with Image.open(result) as rendered:
        assert rendered.getpixel((55, 60)) != (255, 255, 255)
        assert rendered.getpixel((187, 60)) != (255, 255, 255)
        assert rendered.getpixel((120, 60)) == (255, 255, 255)


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
