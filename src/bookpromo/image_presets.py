"""Two fixed FLUX.2 bundles, shared by quote and chapter buttons.

No workflow-file parsing and no arbitrary loader/sampling combinations. A whole
reference action is one resumable Comfy prompt: scene, then one identity edit per
reference. Reference edits never receive character appearance prose.
"""
from dataclasses import asdict, dataclass, replace
import secrets

from .scene_plan import validate_reference_bindings


@dataclass(frozen=True)
class ImagePreset:
    id: str
    label: str
    model: str
    encoder: str
    vae: str
    steps: int
    cfg: float
    sampler: str = "euler"
    version: int = 1


PRESETS = {
    p.id: p for p in (
        ImagePreset("flux2-klein-4b-distilled", "FLUX.2 Klein 4B Distilled",
                    "flux-2-klein-4b-fp8.safetensors", "qwen_3_4b.safetensors", "flux2-vae.safetensors", 4, 1),
        ImagePreset("flux2-klein-9b-base", "FLUX.2 Klein 9B Base",
                    "flux-2-klein-base-9b-fp8.safetensors", "qwen_3_8b_fp8mixed.safetensors",
                    "full_encoder_small_decoder.safetensors", 50, 4, version=2),
    )
}
DEFAULT_PRESET = "flux2-klein-9b-base"


def preset_snapshot(preset_id):
    try:
        return asdict(PRESETS[preset_id])
    except (KeyError, TypeError):
        raise ValueError("Bitte eines der beiden festen Bild-Presets auswählen.") from None


def checked_preset(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Der Bild-Preset-Snapshot ist ungültig. Kein Modellwechsel erfolgt.")
    expected = preset_snapshot(snapshot.get("id"))
    preset = PRESETS[snapshot["id"]]
    # Keep frozen v1 jobs reproducible; only new 9B jobs use the corrected
    # empty-text negative branch. Never reinterpret a persisted snapshot.
    if preset.id == "flux2-klein-9b-base" and snapshot.get("version") == 1:
        preset = replace(preset, version=1)
        expected = asdict(preset)
    # Old queued jobs retain their original display label; this cosmetic rename
    # must not invalidate their otherwise identical frozen technical bundle.
    legacy_label = {
        "flux2-klein-4b-distilled": "FLUX.2 Klein 4B Distilled · VocaVid",
        "flux2-klein-9b-base": "FLUX.2 Klein 9B Base · BookPromo",
    }[snapshot["id"]]
    if (snapshot.get("label") not in {expected["label"], legacy_label}
            or {**snapshot, "label": expected["label"]} != expected):
        raise ValueError("Der Bild-Preset-Snapshot ist ungültig. Kein Modellwechsel erfolgt.")
    return preset


def check_preset_available(client, snapshot):
    preset = checked_preset(snapshot)
    for cls, field, value in (("UNETLoader", "unet_name", preset.model),
                              ("CLIPLoader", "clip_name", preset.encoder),
                              ("VAELoader", "vae_name", preset.vae)):
        info = client.transport.get_json(client.base_url + "/object_info/" + cls)
        available = info.get(cls, {}).get("input", {}).get("required", {}).get(field, [[]])[0]
        if value not in available:
            raise ValueError(f"{preset.label}: {value} fehlt in ComfyUI. Kein Ersatzmodell wird verwendet.")


def identity_edit_prompts(plan, characters):
    """Map names using the planned staging, not appearance/portrait-generation text."""
    validate_reference_bindings(plan, characters)
    prompts = []
    key = lambda v: " ".join(v.casefold().split())
    for character in characters:
        labels = {key(character.name), *(key(alias) for alias in character.aliases)}
        actor = next(actor for actor in plan.actors if key(actor.name) in labels)
        mapping = " ".join(f"{other.name}: {other.pose}" for other in plan.actors)
        prompts.append(
            f"Image 1 is the scene to edit. Image 2 is the reference image of {character.name}. "
            f"Who is who in Image 1: {mapping}. The target is {actor.name}. "
            "Replace only this character's identity with the individual in Image 2. "
            "Keep the target's clothing, pose, expression, gaze and position from Image 1. "
            "Keep every other character unchanged. Keep the scene, background, lighting, style "
            "and framing from Image 1. Do not copy the reference pose, outfit or background."
        )
    return prompts


def build_preset_workflow(snapshot, prompt, *, reference_images=(), edit_prompts=(),
                          width=736, height=1312, filename_prefix="BookPromo/image", seed=None):
    preset = checked_preset(snapshot)
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000
            or len(reference_images) != len(edit_prompts) or len(reference_images) > 4
            or any(not isinstance(p, str) or not p.strip() or len(p) > 20000 for p in edit_prompts)
            or type(width) is not int or type(height) is not int or min(width, height) < 16
            or width % 16 or height % 16 or width * height > 1_000_000):
        raise ValueError("Der Bildauftrag oder die Referenzzuordnung ist ungültig.")
    seed = secrets.randbits(63) if seed is None else seed
    graph = {
        "model": {"class_type": "UNETLoader", "inputs": {"unet_name": preset.model, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": preset.encoder, "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": preset.vae}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": preset.sampler}},
    }

    def stage(prefix, text, images):
        graph[prefix + "text"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": text}}
        if preset.id == "flux2-klein-9b-base" and preset.version >= 2:
            # 9B Base requires encoded empty text, not all-zero conditioning.
            # Reference latents still attach to both branches below.
            graph[prefix + "zero"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": ""}}
        else:
            # Official distilled 4B graph (CFG1), and frozen legacy 9B jobs.
            graph[prefix + "zero"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": [prefix + "text", 0]}}
        branches = {"positive": prefix + "text", "negative": prefix + "zero"}
        for index, image in enumerate(images):
            node = prefix + f"ref{index}"
            graph[node + "scale"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
                "image": image, "upscale_method": "nearest-exact", "megapixels": 1, "resolution_steps": 1}}
            graph[node + "encode"] = {"class_type": "VAEEncode", "inputs": {"pixels": [node + "scale", 0], "vae": ["vae", 0]}}
            for branch in branches:
                graph[node + branch] = {"class_type": "ReferenceLatent", "inputs": {
                    "conditioning": [branches[branch], 0], "latent": [node + "encode", 0]}}
                branches[branch] = node + branch
        graph[prefix + "noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
        graph[prefix + "sigmas"] = {"class_type": "Flux2Scheduler", "inputs": {"steps": preset.steps, "width": width, "height": height}}
        graph[prefix + "latent"] = {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}
        graph[prefix + "guider"] = {"class_type": "CFGGuider", "inputs": {
            "model": ["model", 0], "cfg": preset.cfg, **{b: [n, 0] for b, n in branches.items()}}}
        graph[prefix + "sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": [prefix + "noise", 0], "guider": [prefix + "guider", 0], "sampler": ["sampler", 0],
            "sigmas": [prefix + "sigmas", 0], "latent_image": [prefix + "latent", 0]}}
        graph[prefix + "decode"] = {"class_type": "VAEDecode", "inputs": {"samples": [prefix + "sample", 0], "vae": ["vae", 0]}}
        return [prefix + "decode", 0]

    current = stage("scene-", prompt, [])
    for index, (image, edit_prompt) in enumerate(zip(reference_images, edit_prompts, strict=True)):
        load = f"identity-{index}"
        graph[load] = {"class_type": "LoadImage", "inputs": {"image": image}}
        current = stage(f"edit-{index}-", edit_prompt, [current, [load, 0]])
    graph["save"] = {"class_type": "SaveImage", "inputs": {"images": current, "filename_prefix": filename_prefix}}
    return graph
