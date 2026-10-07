import json
from pathlib import Path
import subprocess
import wave

import pytest
from PIL import Image

from bookpromo.comfy import ComfyResult
from bookpromo.reel_generation import (
    CharacterReferenceSpec,
    ReelGenerationError,
    ReelGenerator,
    audio_duration,
    build_masked_reference_edit_prompt,
    build_forbidden_feature_removal_prompt,
    build_reference_edit_prompt,
    inject_reference_inputs,
    append_detail_reference_input,
    inject_video_inputs,
    mux_selected_audio,
    split_audio_segment,
    wav_waveform,
    _character_mask_queries,
    _validated_character_mask,
    _character_detail_queries,
    _validated_detail_box,
    _character_edit_mask,
    REFERENCE_SCENE_STRATEGY,
    PLANNED_SCENE_STRATEGY,
    build_planned_reference_prompt,
    build_reference_scene_prompt,
    _build_reference_scene_workflow,
    _reference_scene_size,
)
from bookpromo.reel_prompts import VIDEO_PROMPT_SYSTEM, build_video_prompt_request, validate_video_prompt
from bookpromo.scene_plan import ScenePlan, compile_scene_plan


def make_wav(path: Path, seconds=1.0, rate=8000):
    frames = b"\x01\x00" * round(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)


def missing_ffmpeg(*args, **kwargs):
    raise FileNotFoundError


def full_reference_support(tmp_path, size):
    path = tmp_path / 'reference-support.png'
    Image.new('L', size, 255).save(path)
    return path


def qwen_reference_workflow():
    # Negative encoder intentionally comes first and has no editor role title:
    # the sampler links, not dictionary order, determine positive conditioning.
    return {
        'scene': {'class_type': 'LoadImage', 'inputs': {'image': 'scene-old.png'}},
        'reference': {'class_type': 'LoadImage', 'inputs': {'image': 'reference-old.png'}},
        'negative': {'class_type': 'TextEncodeQwenImageEditPlus', 'inputs': {
            'prompt': 'keep-negative', 'image1': ['scene', 0], 'image2': ['reference', 0],
            'clip': ['clip', 0], 'vae': ['vae', 0],
        }},
        'positive': {'class_type': 'TextEncodeQwenImageEditPlus', 'inputs': {
            'prompt': 'old-positive', 'image1': ['scene', 0], 'image2': ['reference', 0],
            'clip': ['clip', 0], 'vae': ['vae', 0],
        }},
        'clip': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'local-qwen-vl.safetensors'}},
        'vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'local-qwen-vae.safetensors'}},
        'encode-scene': {'class_type': 'VAEEncode', 'inputs': {'pixels': ['scene', 0], 'vae': ['vae', 0]}},
        'sampler': {'class_type': 'KSampler', 'inputs': {
            'positive': ['positive', 0], 'negative': ['negative', 0],
            'latent_image': ['encode-scene', 0], 'denoise': .42, 'seed': 123,
        }},
        'save': {'class_type': 'SaveImage', 'inputs': {'images': ['sampler', 0], 'filename_prefix': 'old'}},
    }


def renumber_workflow_nodes(workflow):
    ids = {key: f'custom-{index}' for index, key in enumerate(workflow)}
    for node in workflow.values():
        for key, value in node['inputs'].items():
            if isinstance(value, list) and len(value) == 2 and value[0] in ids:
                node['inputs'][key] = [ids[value[0]], value[1]]
    return {ids[key]: node for key, node in workflow.items()}, ids


def restage_fixture(tmp_path, *, workflow=None, count=1, scene_size=(512, 896), result=None):
    scene = tmp_path / 'original-scene.png'
    Image.new('RGB', scene_size, 'red').save(scene)
    references = []
    for index in range(count):
        path = tmp_path / f'complete-reference-{index}.png'
        Image.new('RGB', (160, 240), (index * 40, 100, 200)).save(path)
        references.append(CharacterReferenceSpec(f'Character {index}', path, '', '', aliases=(f'Alias {index}',)))
    template = tmp_path / 'restage-profile.json'
    profile = workflow if workflow is not None else json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    template.write_text(json.dumps(profile), encoding='utf-8')
    captured = {'uploads': [], 'jobs': []}

    class Client:
        def upload_image(self, path, *, subfolder):
            captured['uploads'].append(Path(path))
            return f'{subfolder}/{Path(path).name}'

        def run_workflow(self, submitted, **kwargs):
            captured['jobs'].append((submitted, kwargs))
            return result if result is not None else ComfyResult('restaged', True, ['new-scene.png'])

        def download_output(self, reference, target):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (512, 896), 'white').save(target)
            return Path(target)

    generator = ReelGenerator(
        Client(), image_workflow_path='workflows/reel-image.json', reference_workflow_path=template,
        video_workflow_path='workflows/reel-video.json', output_root=tmp_path / 'output',
    )
    return generator, scene, references, captured, template


@pytest.mark.parametrize('count', [1, 2, 3, 4])
@pytest.mark.parametrize('renumber', [False, True])
def test_restage_uses_only_full_independent_references_and_matching_cfg_chains(tmp_path, count, renumber):
    profile = json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    if renumber:
        profile, _ = renumber_workflow_nodes(profile)
    generator, scene, references, captured, template = restage_fixture(tmp_path, workflow=profile, count=count)
    snapshots = {path: path.read_bytes() for path in (scene, template, *(r.reference_image_path for r in references))}
    target = generator.restage_character_references(
        reel_id='generic-book', scene_image_path=scene, references=references,
        scene_prompt='Watercolor fantasy library. The listed characters read together.', timeout_seconds=77,
    )
    assert target.is_file() and target not in snapshots
    assert target.name.startswith('reference-scene-')
    assert captured['uploads'] == [r.reference_image_path.resolve() for r in references]
    assert scene.resolve() not in captured['uploads']
    assert len(captured['jobs']) == 1
    graph, kwargs = captured['jobs'][0]
    assert kwargs == {'timeout_sec': 77, 'partial_execution_targets': ['restage-save']}
    assert len([n for n in graph.values() if n['class_type'] == 'LoadImage']) == count
    assert 'original-scene.png' not in json.dumps(graph)
    assert 'characters.png' not in json.dumps(graph)
    assert graph['restage-empty']['inputs'] == {'width': 512, 'height': 896, 'batch_size': 1}
    assert graph['restage-sigmas']['inputs'] == {'width': 512, 'height': 896, 'steps': 50}
    assert graph['restage-guider']['inputs']['cfg'] == 4
    assert graph['restage-sampler']['inputs']['latent_image'] == ['restage-empty', 0]
    assert graph['restage-sampler']['inputs']['sigmas'] == ['restage-sigmas', 0]
    assert not any(n['class_type'] in ('SplitSigmasDenoise', 'GetImageSize', 'BatchCLIPSeg') for n in graph.values())
    for branch in ('positive', 'negative'):
        link = graph['restage-guider']['inputs'][branch]
        for index in reversed(range(count)):
            node = graph[link[0]]
            assert node['class_type'] == 'ReferenceLatent'
            assert node['inputs']['latent'] == [f'restage-reference-{index}-encode', 0]
            link = node['inputs']['conditioning']
        assert link == [f'restage-{branch}', 0]
    for index, item in enumerate(references):
        assert f'Image {index + 1}: {item.name} (also called Alias {index})' in graph['restage-positive']['inputs']['text']
        scale = graph[f'restage-reference-{index}-scale']['inputs']
        assert scale['image'] == [f'restage-reference-{index}-load', 0]
        assert scale['megapixels'] == 1
    assert graph['restage-negative']['inputs']['text'] == ''
    assert all(path.read_bytes() == original for path, original in snapshots.items())


def test_restage_retains_active_native_loader_settings_not_unrelated_nodes(tmp_path):
    profile = json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    profile['92:106']['inputs']['weight_dtype'] = 'fp8_e4m3fn'
    profile['92:111']['inputs']['device'] = 'cpu'
    profile['92:107']['inputs']['vae_name'] = 'custom-flux2-vae.safetensors'
    profile['92:102']['inputs']['sampler_name'] = 'heun'
    profile = {'inactive-loader': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'qwen-image.safetensors'}}, **profile}
    original = json.loads(json.dumps(profile))
    generator, scene, references, captured, _ = restage_fixture(tmp_path, workflow=profile)
    generator.restage_character_references(reel_id='test', scene_image_path=scene, references=references, scene_prompt='Ink illustration in a snowy forest.')
    graph = captured['jobs'][0][0]
    assert graph['restage-model']['inputs'] == profile['92:106']['inputs']
    assert graph['restage-clip']['inputs'] == profile['92:111']['inputs']
    assert graph['restage-vae']['inputs'] == profile['92:107']['inputs']
    assert graph['restage-sampler-select']['inputs'] == profile['92:102']['inputs']
    assert 'inactive-loader' not in graph
    assert profile == original


@pytest.mark.parametrize('style', ['watercolor', 'photorealistic noir', 'flat cartoon', '3D clay animation'])
def test_reference_scene_prompt_is_style_species_and_book_agnostic(style):
    references = [
        CharacterReferenceSpec('Mechanical Fox', Path('fox.png'), 'fox in snow', 'robot in a factory', aliases=('FX',)),
        CharacterReferenceSpec('Moss Dragon', Path('dragon.png'), '', 'photo standing in a desert', forbidden_features=('wings',)),
    ]
    prompt = build_reference_scene_prompt(f'{style}: FX and Moss Dragon share a map in a library.', references)
    assert f'{style}:' in prompt
    assert 'Image 1: Mechanical Fox (also called FX)' in prompt
    assert 'Image 2: Moss Dragon; do not add wings' in prompt
    assert 'face/head' in prompt and 'body build' in prompt and 'sleeve lengths' in prompt
    assert 'mechanical anatomy' in prompt and 'never merge or swap identities' in prompt
    assert 'Do not copy held props from references' in prompt
    assert 'choose one alternative only' in prompt
    assert 'single complete plausible object' in prompt
    assert 'including its handle and fragments' in prompt
    assert 'anatomically correct' in prompt
    assert prompt.endswith('No text, labels, names, captions, signatures, logos or watermarks.')
    assert 'robot in a factory' not in prompt and 'photo standing in a desert' not in prompt
    assert 'fox in snow' not in prompt
    assert REFERENCE_SCENE_STRATEGY == 'reference-scene-restaging-v2'


@pytest.mark.parametrize('count', [1, 2, 4])
def test_restage_direction_reaches_actual_graph_without_changing_sources(tmp_path, count):
    generator, scene, references, captured, template = restage_fixture(tmp_path, count=count)
    original = {p: p.read_bytes() for p in (scene, template, *(r.reference_image_path for r in references))}
    scene_prompt = 'Watercolor library. The figures stand with a map or a book.'
    direction = '  Alias 0 holds one open book in both paws; other figures lean towards it.  '
    generator.restage_character_references(
        reel_id='generic', scene_image_path=scene, references=references,
        scene_prompt=scene_prompt, scene_direction=direction,
    )
    text = captured['jobs'][0][0]['restage-positive']['inputs']['text']
    assert text == build_reference_scene_prompt(scene_prompt, references, scene_direction=direction)
    assert scene_prompt in text and direction.strip() in text
    assert text.count(direction.strip()) == 1
    assert 'replacing vague poses or alternative prop options above' in text
    assert 'setting and artistic style' in text
    assert 'Where the character\'s anatomy has hands' in text
    assert all(p.read_bytes() == content for p, content in original.items())
    assert captured['uploads'] == [r.reference_image_path.resolve() for r in references]


@pytest.mark.parametrize('direction', ['', '   ', '\n\t'])
def test_empty_direction_keeps_reference_prompt_compatible(direction):
    refs = [CharacterReferenceSpec('Copper Fox', Path('fox.png'), '', '')]
    assert build_reference_scene_prompt('Ink forest.', refs, scene_direction=direction) == build_reference_scene_prompt('Ink forest.', refs)


def resolved_reference_plan(names):
    return ScenePlan.model_validate({
        'setting': 'A moonlit library with one open window.',
        'composition': 'Full figures in a wide shared scene.',
        'art_direction': 'Ink and watercolor.',
        'actors': [
            {'name': name, 'pose': 'Leans towards the window with relaxed manipulating limbs.',
             'free_parts': ['left manipulating limb', 'right manipulating limb'], 'contacts': []}
            for name in names
        ],
        'props': [],
    })


@pytest.mark.parametrize('count', [1, 2, 4])
def test_planned_scene_actual_conditioning_is_one_compiled_plan_and_identity_mapping(tmp_path, count):
    generator, scene, references, captured, template = restage_fixture(tmp_path, count=count)
    original = {p: p.read_bytes() for p in (scene, template, *(r.reference_image_path for r in references))}
    plan = resolved_reference_plan([item.name for item in references])
    generator.restage_character_references(
        reel_id='generic', scene_image_path=scene, references=references, scene_plan=plan,
    )
    graph, _ = captured['jobs'][0]
    text = graph['restage-positive']['inputs']['text']
    assert text == build_planned_reference_prompt(plan, references)
    assert text.startswith(compile_scene_plan(plan))
    assert text.count(compile_scene_plan(plan)) == 1
    assert 'New scene description:' not in text and 'Explicit staging direction' not in text
    assert 'choose one alternative' not in text
    assert PLANNED_SCENE_STRATEGY == 'reference-scene-plan-v2'
    assert graph['restage-negative']['inputs']['text'] == ''
    assert captured['uploads'] == [r.reference_image_path.resolve() for r in references]
    assert all(p.read_bytes() == content for p, content in original.items())


@pytest.mark.parametrize('legacy_inputs', [
    {'scene_prompt': 'A character holds a sword or two daggers.'},
    {'scene_direction': 'Both hands hold one cigar.'},
    {'scene_direction': None},
])
def test_planned_scene_rejects_legacy_text_before_upload(tmp_path, legacy_inputs):
    generator, scene, references, captured, _ = restage_fixture(tmp_path)
    with pytest.raises(ValueError, match='consolidated scene plan'):
        generator.restage_character_references(
            reel_id='generic', scene_image_path=scene, references=references,
            scene_plan=resolved_reference_plan([item.name for item in references]), **legacy_inputs,
        )
    assert not captured['uploads'] and not captured['jobs']


@pytest.mark.parametrize('actor_names', [['Unrelated Character'], ['Alias 0', 'Character 0']])
def test_planned_scene_rejects_unbound_actors_before_upload(tmp_path, actor_names):
    generator, scene, references, captured, _ = restage_fixture(tmp_path)
    with pytest.raises(ValueError):
        generator.restage_character_references(
            reel_id='generic', scene_image_path=scene, references=references,
            scene_plan=resolved_reference_plan(actor_names),
        )
    assert not captured['uploads'] and not captured['jobs']


def test_planned_scene_keeps_story_participants_without_identity_references():
    references = [CharacterReferenceSpec('Copper Fox', Path('fox.png'), '', '')]
    plan = resolved_reference_plan(['Copper Fox', 'Background librarian'])
    prompt = build_planned_reference_prompt(plan, references)
    assert 'Background librarian' in prompt
    assert 'Image 1: Copper Fox' in prompt
    assert 'Image 2:' not in prompt


def test_planned_scene_prompt_uses_exact_object_counts_and_contacts_without_portrait_instructions():
    references = [CharacterReferenceSpec(
        'Mechanical Fox', Path('fox.png'), 'in a studio', 'Standing portrait holding a sword or silver daggers.',
        aliases=('FX',), forbidden_features=('legacy forbidden trait',),
    )]
    plan = ScenePlan.model_validate({
        'setting': 'A library alcove.', 'composition': 'The fox in profile.', 'art_direction': 'Ink illustration.',
        'actors': [{
            'name': 'FX', 'pose': 'Leans forward to inspect the unfolded map.', 'free_parts': ['tail'],
            'contacts': [
                {'part': 'left forepaw', 'object_id': 'map', 'action': 'supports the left edge'},
                {'part': 'right forepaw', 'object_id': 'map', 'action': 'supports the right edge'},
            ],
        }],
        'props': [{'id': 'map', 'label': 'unfolded map', 'count': 1}],
    })
    prompt = build_planned_reference_prompt(plan, references)
    assert 'map: exactly 1 unfolded map' in prompt
    assert 'left forepaw: supports the left edge; object map' in prompt
    assert 'right forepaw: supports the right edge; object map' in prompt
    assert 'Image 1: Mechanical Fox (also called FX)' in prompt
    assert references[0].identity_prompt not in prompt
    assert references[0].selector_prompt not in prompt
    assert 'legacy forbidden trait' not in prompt
    assert 'sword' not in prompt and 'daggers' not in prompt


def test_planned_scene_rejects_shared_identity_aliases_even_when_actors_use_canonical_names():
    references = [
        CharacterReferenceSpec('Copper Fox', Path('fox.png'), '', '', aliases=('Shared',)),
        CharacterReferenceSpec('Moss Dragon', Path('dragon.png'), '', '', aliases=(' shared ',)),
    ]
    with pytest.raises(ValueError):
        build_planned_reference_prompt(resolved_reference_plan(['Copper Fox', 'Moss Dragon']), references)


@pytest.mark.parametrize('direction', [None, True, {}, ['pose'], 'x' * 2001])
def test_invalid_direction_fails_before_upload_or_comfy_job(tmp_path, direction):
    generator, scene, references, captured, _ = restage_fixture(tmp_path)
    with pytest.raises(ValueError, match='Scene direction'):
        generator.restage_character_references(
            reel_id='generic', scene_image_path=scene, references=references,
            scene_prompt='New forest scene.', scene_direction=direction,
        )
    assert not captured['uploads'] and not captured['jobs']


def test_direction_and_scene_share_final_prompt_size_limit():
    refs = [CharacterReferenceSpec('Copper Fox', Path('fox.png'), '', '')]
    with pytest.raises(ValueError, match='including character mapping'):
        build_reference_scene_prompt('x' * 18000, refs, scene_direction='y' * 2000)


@pytest.mark.parametrize('names,aliases', [
    (['Sam', ' sam '], [(), ()]),
    (['Samuel Hellsworth', 'Samuel  Hellsworth'], [(), ()]),
    (['Samuel', 'Sam'], [('Sam',), ()]),
    (['A', 'B'], [('same alias',), (' Same   Alias ',)]),
])
def test_reference_scene_rejects_ambiguous_names_and_aliases(names, aliases):
    references = [CharacterReferenceSpec(name, Path('ref.png'), '', '', aliases=alias) for name, alias in zip(names, aliases)]
    with pytest.raises(ValueError, match='unique|one character'):
        build_reference_scene_prompt('New library scene.', references)


@pytest.mark.parametrize('size', [(512, 896), (819, 1024), (4096, 4096), (7680, 4320), (100, 800)])
def test_reference_scene_size_caps_pixels_rounds_to_sixteen_and_preserves_aspect(size):
    width, height = _reference_scene_size(size)
    assert width % 16 == height % 16 == 0
    assert width * height <= 1_000_000
    assert width <= size[0] and height <= size[1]
    assert abs(width / height - size[0] / size[1]) <= .04


@pytest.mark.parametrize('size', [(0, 100), (15, 100), (100, 900), (True, 128), (float('inf'), 128), (10**1000, 10**1000)])
def test_reference_scene_size_rejects_invalid_or_extreme_inputs(size):
    with pytest.raises(ValueError):
        _reference_scene_size(size)


@pytest.mark.parametrize('failure', [
    'two-saves', 'wrong-decode', 'wrong-model', 'distilled-model', 'model-wrapper',
    'wrong-clip-type', 'different-negative-clip', 'different-vae', 'bad-output-slot',
    'bool-output-slot', 'bool-noise-seed', 'bad-scheduler', 'unknown-conditioning',
    'conditioning-cycle', 'shared-encoder', 'bad-conditioning-link', 'malformed-loader-inputs',
    'malformed-encoder-inputs', 'malformed-noise-inputs',
])
def test_restage_incompatible_or_ambiguous_profile_fails_before_any_upload_or_job(tmp_path, failure):
    profile = json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    if failure == 'two-saves':
        profile['other-save'] = json.loads(json.dumps(profile['94']))
    elif failure == 'wrong-decode':
        profile['92:104']['class_type'] = 'CustomVAEDecode'
    elif failure in ('wrong-model', 'distilled-model'):
        profile['92:106']['inputs']['unet_name'] = 'qwen.safetensors' if failure == 'wrong-model' else 'flux-2-klein-4b-fp8.safetensors'
    elif failure == 'model-wrapper':
        profile['wrapped-model'] = {'class_type': 'LoraLoaderModelOnly', 'inputs': {'model': ['92:106', 0]}}
        profile['92:114']['inputs']['model'] = ['wrapped-model', 0]
    elif failure == 'wrong-clip-type':
        profile['92:111']['inputs']['type'] = 'qwen_image'
    elif failure == 'different-negative-clip':
        profile['92:87']['inputs']['clip'] = ['other-clip', 0]
    elif failure == 'different-vae':
        profile['92:129']['inputs']['vae'] = ['other-vae', 0]
    elif failure in ('bad-output-slot', 'bool-output-slot'):
        profile['94']['inputs']['images'][1] = 1 if failure == 'bad-output-slot' else False
    elif failure == 'bool-noise-seed':
        profile['92:105']['inputs']['noise_seed'] = True
    elif failure == 'bad-scheduler':
        profile['92:115']['class_type'] = 'BasicScheduler'
    elif failure == 'unknown-conditioning':
        profile['custom-conditioning'] = {'class_type': 'ControlNetApply', 'inputs': {'conditioning': ['92:130', 0]}}
        profile['92:114']['inputs']['positive'] = ['custom-conditioning', 0]
    elif failure == 'conditioning-cycle':
        profile['92:130']['inputs']['conditioning'] = ['92:130', 0]
    elif failure == 'shared-encoder':
        profile['92:125']['inputs']['conditioning'] = ['92:113', 0]
    elif failure == 'bad-conditioning-link':
        profile['92:114']['inputs']['positive'] = None
    elif failure == 'malformed-loader-inputs':
        profile['92:106']['inputs'] = None
    elif failure == 'malformed-encoder-inputs':
        profile['92:113']['inputs'] = None
    elif failure == 'malformed-noise-inputs':
        profile['92:105']['inputs'] = None
    generator, scene, references, captured, _ = restage_fixture(tmp_path, workflow=profile)
    with pytest.raises(ReelGenerationError, match='native Flux'):
        generator.restage_character_references(reel_id='test', scene_image_path=scene, references=references, scene_prompt='A new forest scene.')
    assert captured['uploads'] == [] and captured['jobs'] == []


@pytest.mark.parametrize('failure', ['no-references', 'five-references', 'empty-scene', 'too-long-scene', 'too-long-alias', 'missing-reference', 'invalid-reference', 'unsafe-id', 'timeout'])
def test_restage_invalid_input_fails_before_external_actions(tmp_path, failure):
    generator, scene, references, captured, _ = restage_fixture(tmp_path)
    prompt, reel_id, timeout = 'New forest scene.', 'test', 60
    if failure == 'no-references':
        references = []
    elif failure == 'five-references':
        references *= 5
    elif failure == 'empty-scene':
        prompt = ' '
    elif failure == 'too-long-scene':
        prompt = 'a' * 20_000
    elif failure == 'too-long-alias':
        references = [CharacterReferenceSpec('A', references[0].reference_image_path, '', '', aliases=('a' * 20_000,))]
    elif failure == 'missing-reference':
        references[0].reference_image_path.unlink()
    elif failure == 'invalid-reference':
        references[0].reference_image_path.write_bytes(b'not an image')
    elif failure == 'unsafe-id':
        reel_id = '../outside'
    elif failure == 'timeout':
        timeout = float('nan')
    with pytest.raises((ValueError, OSError)):
        generator.restage_character_references(reel_id=reel_id, scene_image_path=scene, references=references, scene_prompt=prompt, timeout_seconds=timeout)
    assert captured['uploads'] == [] and captured['jobs'] == []


def test_restage_failed_job_does_not_localize_a_stale_output(tmp_path):
    generator, scene, references, captured, _ = restage_fixture(tmp_path, result=ComfyResult('failed', False, ['stale.png'], error='model unavailable'))
    with pytest.raises(ReelGenerationError, match='model unavailable'):
        generator.restage_character_references(reel_id='test', scene_image_path=scene, references=references, scene_prompt='New forest scene.')
    assert len(captured['jobs']) == 1
    assert not generator.output_root.exists()


@pytest.mark.parametrize('width,height,images', [(511, 896, ['ref.png']), (512, True, ['ref.png']), (1024, 1024, ['ref.png']), (512, 896, []), (512, 896, ['ref.png'] * 5)])
def test_native_restage_builder_rejects_invalid_direct_input(width, height, images):
    profile = json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    with pytest.raises(ValueError):
        _build_reference_scene_workflow(profile, width=width, height=height, reference_images=images, prompt='New forest scene.', filename_prefix='test')


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
    assert "body proportions, clothing and worn accessories" in prompt
    assert "never for the scene, pose or lighting" in prompt
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


@pytest.mark.parametrize("tag", ["forbidden", "masked"])
@pytest.mark.parametrize("renumber", [False, True])
def test_reference_edit_uses_full_sampling_with_scene_as_conditioning(tmp_path, tag, renumber):
    captured = {}
    workflow = json.loads(Path("workflows/reel-reference.json").read_text(encoding="utf-8"))
    if renumber:
        ids = {key: f"custom-{index}" for index, key in enumerate(workflow)}
        for node in workflow.values():
            for key, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and value[0] in ids:
                    node["inputs"][key] = [ids[value[0]], value[1]]
        workflow = {ids[key]: node for key, node in workflow.items()}
    template = tmp_path / "reference.json"
    template.write_text(json.dumps(workflow), encoding="utf-8")

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
        reference_workflow_path=template,
        video_workflow_path="workflows/reel-video.json",
        output_root=tmp_path / "output",
        ffmpeg_binary="ffmpeg",
    )
    generator._apply_single_character_reference(
        reel_id="feature-pass",
        scene_image_path=scene,
        reference_image_path=reference,
        prompt="reference edit",
        tag=tag,
        timeout_seconds=30,
        preserve_geometry=False,
    )
    rendered = captured["workflow"]
    by_type = {node["class_type"]: (key, node["inputs"]) for key, node in rendered.items()}
    sampler = by_type["SamplerCustomAdvanced"][1]
    assert sampler["latent_image"] == [by_type["EmptyFlux2LatentImage"][0], 0]
    assert sampler["sigmas"] == [by_type["Flux2Scheduler"][0], 0]
    # The scene remains reference Image 1 even though it no longer initializes sampling.
    scene_encoder = next(key for key, node in rendered.items()
                         if node["class_type"] == "VAEEncode"
                         and node["inputs"]["pixels"] == [ids["92:110"] if renumber else "92:110", 0])
    assert any(node["inputs"].get("latent") == [scene_encoder, 0]
               for node in rendered.values() if node["class_type"] == "ReferenceLatent")
    assert json.loads(template.read_text(encoding="utf-8")) == workflow
    assert scene.read_bytes() == b"scene" and reference.read_bytes() == b"reference"


def test_masked_reference_prompt_forbids_duplicates_and_preserves_scene():
    prompt = build_masked_reference_edit_prompt("Unit 7")
    assert "single existing target character Unit 7" in prompt
    assert "original number of characters" in prompt
    assert "without duplicate subjects" in prompt
    assert "Image 2 is authoritative" in prompt
    assert "Image 1 is authoritative for artistic style and rendering medium" in prompt
    assert "Correct the target's mismatched identity features" in prompt
    assert "surface materials" in prompt and "intrinsic anatomy" in prompt
    assert "preserve" in prompt.casefold()


@pytest.mark.parametrize("missing_type", ["EmptyFlux2LatentImage", "Flux2Scheduler"])
def test_incompatible_reference_workflow_fails_without_submitting_an_image_job(tmp_path, missing_type):
    workflow = json.loads(Path("workflows/reel-reference.json").read_text(encoding="utf-8"))
    workflow = {key: node for key, node in workflow.items() if node["class_type"] != missing_type}
    template = tmp_path / "reference.json"
    template.write_text(json.dumps(workflow), encoding="utf-8")

    class Client:
        def upload_image(self, path, *, subfolder):
            return str(path)

        def run_workflow(self, *args, **kwargs):
            pytest.fail("An incompatible workflow must not queue an image job")

    generator = ReelGenerator.__new__(ReelGenerator)
    generator.client = Client()
    generator.reference_workflow_path = template
    with pytest.raises(ReelGenerationError, match="referenzgestütztes Editing"):
        generator._apply_single_character_reference(
            reel_id="generic", scene_image_path=tmp_path / "scene.png",
            reference_image_path=tmp_path / "portrait.png",
            prompt=build_masked_reference_edit_prompt("Target"), tag="masked",
            timeout_seconds=60, preserve_geometry=False,
        )


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
    assert workflow["3"]["inputs"]["blur_sigma"] == 0
    assert workflow["6"]["inputs"]["mask"] == ["3", 0]
    assert calls["kwargs"]["partial_execution_targets"] == ["7"]


def test_scattered_edge_mask_like_sam_is_rejected_before_edit(tmp_path):
    mask = Image.new('L', (512, 896), 0)
    mask.paste(255, (0, 370, 40, 420))
    mask.paste(255, (470, 365, 510, 410))
    path = tmp_path / 'scattered.png'
    mask.save(path)
    assert sum(mask.get_flattened_data()) / 255 / (512 * 896) > .008
    with pytest.raises(ReelGenerationError, match='verstreuten Teilflächen'):
        _validated_character_mask(path, mask.size, 'Sam')


def test_valid_mask_keeps_disconnected_head_and_supports_nonhuman_and_offcenter(tmp_path):
    mask = Image.new('L', (160, 240), 0)
    mask.paste(255, (5, 55, 60, 220))
    mask.paste(255, (10, 20, 50, 48))
    path = tmp_path / 'body-and-head.png'
    mask.save(path)
    checked = _validated_character_mask(path, mask.size, 'off-center character')
    assert checked.getpixel((25, 30)) > 245  # Don't discard a detached face region.
    assert checked.getpixel((25, 100)) == 255
    assert checked.getpixel((150, 120)) == 0
    mask = Image.new('L', (240, 120), 0)
    mask.paste(255, (25, 45, 215, 85))
    mask.save(path)
    assert _validated_character_mask(path, mask.size, 'clockwork fox').getbbox()


def test_dense_but_fragmented_mask_is_rejected(tmp_path):
    mask = Image.new('L', (100, 100), 0)
    for left, top in [(10, 10), (55, 10), (10, 55), (55, 55)]:
        mask.paste(255, (left, top, left + 25, top + 25))
    path = tmp_path / 'four-regions.png'; mask.save(path)
    with pytest.raises(ReelGenerationError, match='verstreuten Teilflächen'):
        _validated_character_mask(path, mask.size, 'four artifacts')


def test_full_frame_reference_is_valid_but_full_scene_is_not(tmp_path):
    path = tmp_path / 'full.png'
    Image.new('L', (100, 150), 255).save(path)
    with pytest.raises(ReelGenerationError, match='fast das ganze Bild'):
        _validated_character_mask(path, (100, 150), 'scene')
    assert _validated_character_mask(path, (100, 150), 'portrait', reference=True).getextrema() == (255, 255)


def test_short_queries_do_not_force_humans_or_merge_multi_character_targets():
    selector = 'dark fantasy demon hunter, strong realistic build, short dark brown hair, pale skin'
    assert _character_mask_queries(selector, 'normal human skin', single_target=True) == (
        'person', 'dark fantasy demon hunter',
    )
    multi = _character_mask_queries(selector, 'normal human skin', single_target=False)
    assert 'person' not in multi and 'short dark brown hair' in multi[0]
    beast = _character_mask_queries('copper clockwork fox with green glass eyes',
                                    'non-human creature, no human anatomy', single_target=True)
    assert all('person' not in query for query in beast)
    assert len(beast) <= 2


def test_bad_detection_retries_once_with_raw_masks_and_preserves_head(tmp_path):
    scene = tmp_path / 'scene.png'
    Image.new('RGB', (100, 150), 'navy').save(scene)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs)
        mask = Image.new('L', (100, 150), 0)
        if len(calls) == 1:
            mask.paste(255, (0, 80, 7, 90)); mask.paste(255, (93, 80, 100, 90))
        else:
            mask.paste(255, (20, 15, 75, 140))
        mask.save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    mask = generator._locate_character_mask(
        reel_id='test', scene_image_path=scene, selector_prompt='dark hunter, short black hair, black jacket',
        identity_prompt='a human man', name='Sam', single_target=True,
        output_path=tmp_path / 'mask.png', timeout_seconds=60,
    )
    assert [call['selector_prompt'] for call in calls] == ['man with short black hair', 'man with black jacket']
    assert all(call['threshold'] == .35 and call['timeout_seconds'] == 30 for call in calls)
    assert mask.getpixel((40, 20)) > 200


def test_failed_detection_never_invokes_edit_or_changes_source(tmp_path):
    scene = tmp_path / 'scene.png'; Image.new('RGB', (100, 150), 'navy').save(scene)
    original = scene.read_bytes()
    reference = tmp_path / 'portrait.png'; Image.new('RGB', (100, 150), 'red').save(reference)
    generator = ReelGenerator.__new__(ReelGenerator)
    generator.reference_workflow_path = tmp_path / 'workflow.json'
    generator.output_root = tmp_path / 'work'
    calls = []

    def detect(**kwargs):
        calls.append(kwargs['selector_prompt'])
        Image.new('L', (100, 150), 0).save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    generator._apply_single_character_reference = lambda **kwargs: pytest.fail('Invalid mask must never edit')
    with pytest.raises(ReelGenerationError, match='bisherige Bild bleibt erhalten'):
        generator.apply_character_references_masked(reel_id='test', scene_image_path=scene,
            references=[CharacterReferenceSpec('Sam', reference, 'dark hunter', 'a human man')])
    assert calls == ['dark hunter'] and scene.read_bytes() == original


def test_reference_cutout_also_retries_fragmented_mask_and_keeps_identity(tmp_path):
    reference = tmp_path / 'reference.png'; Image.new('RGB', (100, 150), 'red').save(reference)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs['selector_prompt'])
        mask = Image.new('L', (100, 150), 0)
        if len(calls) == 1:
            mask.paste(255, (0, 80, 7, 90)); mask.paste(255, (93, 80, 100, 90))
        else:
            mask.paste(255, (20, 15, 75, 140))
        mask.save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    output = generator._prepare_character_reference(reel_id='test', reference_image_path=reference,
        selector_prompt='dark hunter', identity_prompt='a human man',
        output_path=tmp_path / 'cutout.png', timeout_seconds=60)
    assert calls == ['person', 'dark hunter']
    with Image.open(output) as result:
        assert result.getpixel((40, 40)) == (255, 0, 0)
        assert result.getpixel((99, 0)) == (112, 112, 112)


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
        assert kwargs["preserve_geometry"] is False
        assert kwargs["reference_image_path"] == references[len(edit_names)].reference_image_path
        edit_names.append(kwargs["prompt"])
        with Image.open(kwargs["scene_image_path"]) as crop:
            output = tmp_path / f"edit-{len(edit_names)}.png"
            Image.new("RGB", crop.size, "#111111").save(output)
        return output

    generator._detect_character_mask = detect
    generator._prepare_character_reference = lambda **kwargs: kwargs["reference_image_path"]
    generator._prepare_character_detail_reference = lambda **kwargs: None
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


@pytest.mark.parametrize("name,identity", [
    ("Mira", "Elderly woman with silver curls. Photorealistic studio reference portrait."),
    ("Eli", "Young boy with a round face. Manga reference portrait on a white backdrop."),
    ("Fox", "Silver fox with black ears. Watercolor illustration in a forest."),
    ("Unit 7", "Brass automaton with a triangular blue eye. Technical blueprint style."),
])
def test_identity_edit_is_generic_and_does_not_inject_portrait_style(tmp_path, name, identity):
    scene = tmp_path / "scene.png"
    reference = tmp_path / "portrait.png"
    Image.new("RGB", (240, 160), "navy").save(scene)
    Image.new("RGB", (100, 150), "red").save(reference)
    original_scene, original_portrait = scene.read_bytes(), reference.read_bytes()
    mask = Image.new("L", (240, 160), 0)
    mask.paste(255, (80, 20, 150, 140))
    mask_path = tmp_path / "mask.png"
    mask.save(mask_path)
    calls = []
    generator = ReelGenerator.__new__(ReelGenerator)
    generator.reference_workflow_path = tmp_path / "workflow.json"
    generator.output_root = tmp_path / "output"
    generator._detect_character_mask = lambda **kwargs: mask_path
    generator._prepare_character_reference = lambda **kwargs: kwargs["reference_image_path"]
    generator._prepare_character_detail_reference = lambda **kwargs: None

    def edit(**kwargs):
        calls.append(kwargs)
        output = tmp_path / "edit.png"
        with Image.open(kwargs["scene_image_path"]) as crop:
            Image.new("RGB", crop.size, "red").save(output)
        return output

    generator._apply_single_character_reference = edit
    result = generator.apply_character_references_masked(
        reel_id="generic", scene_image_path=scene,
        references=[CharacterReferenceSpec(name, reference, identity, identity)],
    )
    assert len(calls) == 1
    assert calls[0]["preserve_geometry"] is False
    prompt = calls[0]["prompt"]
    assert f"single existing target character {name}" in prompt
    assert identity not in prompt  # The portrait's medium/setting must not override the scene's.
    assert "Image 1 is authoritative for artistic style and rendering medium" in prompt
    assert "Render Image 2's identity in Image 1's existing visual style" in prompt
    with Image.open(result) as rendered:
        assert rendered.getpixel((110, 80)) == (255, 0, 0)
        assert rendered.getpixel((0, 0)) == (0, 0, 128)
        assert rendered.getpixel((230, 80)) == (0, 0, 128)
    assert scene.read_bytes() == original_scene and reference.read_bytes() == original_portrait


def test_appearance_roles_take_build_and_clothing_from_reference_not_scene():
    prompt = build_masked_reference_edit_prompt('Any character', detail_reference=True)
    assert 'body build, body proportions, clothing' in prompt
    assert "Replace the target's mismatched face, build and outfit with Image 2's appearance" in prompt
    assert "Image 3 is an enlarged detail of this SAME Image 2 character" in prompt
    scene_role = prompt.split('Image 1 is authoritative for')[1].split('Render Image 2')[0]
    assert 'clothing' not in scene_role and 'outfit' not in scene_role and 'body proportions' not in scene_role
    assert 'artistic style' in scene_role and 'pose' in scene_role
    assert 'Image 3' not in build_masked_reference_edit_prompt('Other character')


@pytest.mark.parametrize('selector,identity,second', [
    ('human woman with silver hair', 'adult human woman', 'face'),
    ('red fox with black ears', 'non-human animal', 'red fox head'),
    ('brass automaton with blue lenses', 'mechanical figure', 'brass automaton head'),
    ('winged dragon, gold scales', 'no human anatomy', 'winged dragon head'),
])
def test_detail_queries_follow_anatomy_without_assuming_portrait_layout(selector, identity, second):
    assert _character_detail_queries(selector, identity) == ('head', second)


def test_semantic_detail_crop_supports_horizontal_animal_with_head_at_bottom_right(tmp_path):
    reference = tmp_path / 'fox.png'
    Image.new('RGB', (200, 100), 'orange').save(reference)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs)
        mask = Image.new('L', (200, 100), 0)
        mask.paste(255, (155, 65, 190, 95))
        mask.save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    output = generator._prepare_character_detail_reference(
        reel_id='fox', reference_image_path=reference, selector_prompt='fox',
        identity_prompt='non-human fox', output_path=tmp_path / 'detail.png', timeout_seconds=60,
        reference_mask_path=full_reference_support(tmp_path, (200, 100)),
    )
    assert output is not None
    with Image.open(output) as cropped:
        assert cropped.size == (47, 41)
        assert cropped.getpixel((20, 20)) == (255, 165, 0)
    assert calls[0]['selector_prompt'] == 'head' and len(calls) == 1


@pytest.mark.parametrize('invalid', ['empty', 'whole', 'fragmented'])
def test_uncertain_detail_falls_back_without_creating_a_fixed_head_crop(tmp_path, invalid):
    reference = tmp_path / 'reference.png'
    Image.new('RGB', (200, 100), 'blue').save(reference)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs)
        mask = Image.new('L', (200, 100), 255 if invalid == 'whole' else 0)
        if invalid == 'fragmented':
            mask.paste(255, (5, 5, 20, 20))
            mask.paste(255, (180, 80, 195, 95))
        mask.save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    output = tmp_path / 'detail.png'
    assert generator._prepare_character_detail_reference(
        reel_id='any', reference_image_path=reference, selector_prompt='robot',
        identity_prompt='mechanical figure', output_path=output, timeout_seconds=60,
        reference_mask_path=full_reference_support(tmp_path, (200, 100)),
    ) is None
    assert not output.exists() and len(calls) == 2
    assert all(call['timeout_seconds'] == 30 for call in calls)


def test_detail_retry_can_use_second_semantic_query(tmp_path):
    reference = tmp_path / 'reference.png'
    Image.new('RGB', (100, 150), 'red').save(reference)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs['selector_prompt'])
        mask = Image.new('L', (100, 150), 0)
        if len(calls) == 2:
            mask.paste(255, (35, 25, 65, 55))
        mask.save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    assert generator._prepare_character_detail_reference(
        reel_id='any', reference_image_path=reference, selector_prompt='human woman',
        identity_prompt='woman', output_path=tmp_path / 'detail.png', timeout_seconds=60,
        reference_mask_path=full_reference_support(tmp_path, (100, 150)),
    ) is not None
    assert calls == ['head', 'face']


def test_detail_outside_validated_character_support_is_not_used(tmp_path):
    mask = Image.new('L', (200, 100), 0)
    mask.paste(255, (155, 60, 190, 90))
    path = tmp_path / 'detail-mask.png'; mask.save(path)
    support = Image.new('L', (200, 100), 0)
    support.paste(255, (5, 5, 100, 95))
    assert _validated_detail_box(path, mask.size, support_mask=support) is None
    assert _validated_detail_box(path, mask.size) is None
    support.paste(255, (150, 55, 195, 95))
    assert _validated_detail_box(path, mask.size, support_mask=support) is not None


def test_optional_detail_without_verified_support_runs_no_segmentation(tmp_path):
    generator = ReelGenerator.__new__(ReelGenerator)
    generator._detect_character_mask = lambda **kwargs: pytest.fail('No support means no supplemental detection')
    assert generator._prepare_character_detail_reference(
        reel_id='any', reference_image_path=tmp_path / 'reference.png', selector_prompt='robot',
        identity_prompt='mechanical figure', output_path=tmp_path / 'detail.png', timeout_seconds=60,
    ) is None


def test_corrupt_optional_detail_mask_falls_back_without_aborting_transfer(tmp_path):
    reference = tmp_path / 'reference.png'; Image.new('RGB', (100, 150), 'red').save(reference)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs['selector_prompt'])
        kwargs['output_path'].write_bytes(b'not an image')
        return kwargs['output_path']

    generator._detect_character_mask = detect
    assert generator._prepare_character_detail_reference(
        reel_id='any', reference_image_path=reference, selector_prompt='human woman',
        identity_prompt='woman', output_path=tmp_path / 'detail.png', timeout_seconds=60,
        reference_mask_path=full_reference_support(tmp_path, (100, 150)),
    ) is None
    assert calls == ['head', 'face']


def test_compact_human_scene_queries_replace_job_title_with_noun_and_visible_traits():
    selector = ('dark fantasy demon hunter, strong realistic build, '
                'short dark brown to nearly black hair, pale to lightly tanned skin')
    identity = ('A dark fantasy demon hunter in his early thirties. He has short black hair. '
                'He wears a black leather jacket. Keep normal human skin, no horns.')
    assert _character_mask_queries(selector, identity, single_target=False) == (
        'man with black hair', 'man with black leather jacket',
    )
    assert _character_mask_queries('human woman with silver curls, red coat',
        'elderly woman with silver hair and a red coat', single_target=False) == (
        'woman with silver hair', 'woman with red coat',
    )


@pytest.mark.parametrize('renumber', [False, True])
def test_detail_reference_is_third_image_in_both_conditioning_chains(renumber):
    workflow = json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    if renumber:
        ids = {key: f'node-{index}' for index, key in enumerate(workflow)}
        for node in workflow.values():
            for key, value in node['inputs'].items():
                if isinstance(value, list) and len(value) == 2 and value[0] in ids:
                    node['inputs'][key] = [ids[value[0]], value[1]]
        workflow = {ids[key]: node for key, node in workflow.items()}
    original = json.loads(json.dumps(workflow))
    updated = append_detail_reference_input(workflow, 'detail.png')
    guider_id = next(key for key, node in workflow.items() if node['class_type'] == 'CFGGuider')
    previous = workflow[guider_id]['inputs']
    for branch in ('positive', 'negative'):
        appended_id = updated[guider_id]['inputs'][branch][0]
        appended = updated[appended_id]['inputs']
        assert appended['conditioning'] == previous[branch]
        encoder_id = appended['latent'][0]
        assert updated[encoder_id]['class_type'] == 'VAEEncode'
        scale_id = updated[encoder_id]['inputs']['pixels'][0]
        load_id = updated[scale_id]['inputs']['image'][0]
        assert updated[load_id]['inputs']['image'] == 'detail.png'
        assert updated[previous[branch][0]] == workflow[previous[branch][0]]
    assert workflow == original


def test_detail_node_ids_cannot_overwrite_custom_nodes():
    workflow = json.loads(Path('workflows/reel-reference.json').read_text(encoding='utf-8'))
    workflow['character-detail-load'] = {'class_type': 'LoadImage', 'inputs': {'image': 'keep.png'}}
    updated = append_detail_reference_input(workflow, 'detail.png')
    assert updated['character-detail-load']['inputs']['image'] == 'keep.png'
    assert updated['character-detail-extra-load']['inputs']['image'] == 'detail.png'


def test_detail_reference_is_uploaded_and_attached_in_actual_edit_workflow(tmp_path):
    captured = {}

    class Client:
        def upload_image(self, path, *, subfolder):
            captured.setdefault('uploads', []).append(Path(path))
            return f'{subfolder}/{Path(path).name}'

        def run_workflow(self, workflow, **kwargs):
            captured['workflow'] = workflow
            return ComfyResult('edit', True, ['result.png'])

        def download_output(self, reference, target):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (80, 120), 'white').save(target)
            return Path(target)

    scene, reference, detail = (tmp_path / name for name in ('scene.png', 'reference.png', 'detail.png'))
    for path in (scene, reference, detail):
        Image.new('RGB', (80, 120), 'red').save(path)
    generator = ReelGenerator(
        Client(), image_workflow_path='workflows/reel-image.json',
        reference_workflow_path='workflows/reel-reference.json',
        video_workflow_path='workflows/reel-video.json', output_root=tmp_path / 'out',
    )
    generator._apply_single_character_reference(
        reel_id='generic', scene_image_path=scene, reference_image_path=reference,
        detail_reference_image_path=detail,
        prompt=build_masked_reference_edit_prompt('Target', detail_reference=True),
        tag='masked', timeout_seconds=60, preserve_geometry=False,
    )
    assert captured['uploads'] == [scene, reference, detail]
    workflow = captured['workflow']
    assert workflow['92:114']['inputs']['positive'] == ['character-detail-positive', 0]
    assert workflow['92:114']['inputs']['negative'] == ['character-detail-negative', 0]
    assert workflow['92:103']['inputs']['latent_image'] == ['92:109', 0]
    assert workflow['92:113']['inputs']['text'] == build_masked_reference_edit_prompt('Target', detail_reference=True)


@pytest.mark.parametrize('renumber', [False, True])
def test_qwen_prompt_injection_follows_positive_branch_and_preserves_negative(renumber):
    workflow = qwen_reference_workflow()
    ids = {key: key for key in workflow}
    if renumber:
        workflow, ids = renumber_workflow_nodes(workflow)
    original = json.loads(json.dumps(workflow))
    updated = inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'exact identity transfer')
    assert updated[ids['positive']]['inputs']['prompt'] == 'exact identity transfer'
    assert updated[ids['negative']]['inputs']['prompt'] == 'keep-negative'
    assert updated[ids['scene']]['inputs']['image'] == 'new-scene.png'
    assert updated[ids['reference']]['inputs']['image'] == 'new-reference.png'
    assert 'text' not in updated[ids['positive']]['inputs']
    assert workflow == original


def test_qwen_detail_image_connects_both_native_encoders_without_flux_nodes():
    workflow = qwen_reference_workflow()
    original = json.loads(json.dumps(workflow))
    updated = append_detail_reference_input(workflow, 'head-detail.png')
    for branch in ('positive', 'negative'):
        inputs = updated[branch]['inputs']
        assert inputs['image1'] == ['scene', 0]
        assert inputs['image2'] == ['reference', 0]
        assert inputs['image3'] == ['character-detail-load', 0]
        assert inputs['prompt'] == workflow[branch]['inputs']['prompt']
    assert updated['character-detail-load']['inputs']['image'] == 'head-detail.png'
    assert not any(node['class_type'] == 'ReferenceLatent' for node in updated.values())
    assert workflow == original


@pytest.mark.parametrize('branch', ['positive', 'negative'])
@pytest.mark.parametrize('existing_image', [['existing-detail', 0], 'custom-image.png', []])
def test_qwen_detail_reference_rejects_occupied_third_image_without_mutating_workflow(branch, existing_image):
    workflow = qwen_reference_workflow()
    workflow[branch]['inputs']['image3'] = existing_image
    original = json.loads(json.dumps(workflow))
    with pytest.raises(ReelGenerationError, match='Bild 3 bereits'):
        append_detail_reference_input(workflow, 'new-head-detail.png')
    assert workflow == original
    assert 'character-detail-load' not in workflow


def test_qwen_detail_reference_accepts_explicitly_disconnected_third_image():
    workflow = qwen_reference_workflow()
    workflow['positive']['inputs']['image3'] = None
    workflow['negative']['inputs']['image3'] = None
    updated = append_detail_reference_input(workflow, 'head-detail.png')
    assert updated['positive']['inputs']['image3'] == ['character-detail-load', 0]
    assert updated['negative']['inputs']['image3'] == ['character-detail-load', 0]
    assert workflow['positive']['inputs']['image3'] is None


def test_unresolved_positive_branch_does_not_overwrite_negative_by_dictionary_order():
    workflow = qwen_reference_workflow()
    workflow.pop('sampler')
    original = json.loads(json.dumps(workflow))
    with pytest.raises(ValueError, match='ambiguous positive'):
        inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'new-positive')
    assert workflow == original


def test_unresolved_positive_branch_uses_only_unique_explicit_positive_title():
    workflow = qwen_reference_workflow()
    workflow.pop('sampler')
    workflow['positive']['_meta'] = {'title': 'Positive Prompt'}
    updated = inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'new-positive')
    assert updated['positive']['inputs']['prompt'] == 'new-positive'
    assert updated['negative']['inputs']['prompt'] == 'keep-negative'


def test_sole_explicit_negative_encoder_is_not_used_as_positive_fallback():
    workflow = qwen_reference_workflow()
    workflow.pop('sampler')
    workflow.pop('positive')
    workflow['negative']['_meta'] = {'title': 'Negative Prompt'}
    with pytest.raises(ValueError, match='no positive'):
        inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'new-positive')
    assert workflow['negative']['inputs']['prompt'] == 'keep-negative'


def test_positive_conditioning_combination_with_multiple_encoders_fails_closed():
    workflow = qwen_reference_workflow()
    workflow['second-positive'] = json.loads(json.dumps(workflow['positive']))
    workflow['combined-positive'] = {'class_type': 'ConditioningCombine', 'inputs': {
        'conditioning_1': ['positive', 0], 'conditioning_2': ['second-positive', 0],
    }}
    workflow['sampler']['inputs']['positive'] = ['combined-positive', 0]
    original = json.loads(json.dumps(workflow))
    with pytest.raises(ValueError, match='ambiguous positive'):
        inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'new-positive')
    assert workflow == original


def test_shared_positive_negative_encoder_is_not_overwritten():
    workflow = qwen_reference_workflow()
    workflow['sampler']['inputs']['negative'] = ['positive', 0]
    with pytest.raises(ValueError, match='shares its positive'):
        inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'new-positive')
    assert workflow['positive']['inputs']['prompt'] == 'old-positive'


def test_positive_encoder_resolution_follows_wrappers_with_cycles_safely():
    workflow = qwen_reference_workflow()
    workflow['wrapped-positive'] = {'class_type': 'ReferenceLatent', 'inputs': {
        'conditioning': ['positive', 0], 'ignored-cycle': ['wrapped-positive', 0],
    }}
    workflow['sampler']['inputs']['positive'] = ['wrapped-positive', 0]
    updated = inject_reference_inputs(workflow, 'new-scene.png', 'new-reference.png', 'new-positive')
    assert updated['positive']['inputs']['prompt'] == 'new-positive'
    assert updated['negative']['inputs']['prompt'] == 'keep-negative'


def test_qwen_occupied_detail_slot_rejects_before_submitting_workflow(tmp_path):
    workflow = qwen_reference_workflow()
    workflow['positive']['inputs']['image3'] = ['existing-image', 0]
    template = tmp_path / 'qwen-reference.json'
    template.write_text(json.dumps(workflow), encoding='utf-8')

    class Client:
        def upload_image(self, path, *, subfolder):
            return str(path)

        def run_workflow(self, *args, **kwargs):
            pytest.fail('An occupied native reference slot must not submit an image job')

    generator = ReelGenerator.__new__(ReelGenerator)
    generator.client = Client()
    generator.reference_workflow_path = template
    with pytest.raises(ReelGenerationError, match='Bild 3 bereits'):
        generator._apply_single_character_reference(
            reel_id='generic', scene_image_path=tmp_path / 'scene.png',
            reference_image_path=tmp_path / 'reference.png',
            detail_reference_image_path=tmp_path / 'detail.png', prompt='replace identity',
            tag='masked', timeout_seconds=60, preserve_geometry=False,
        )
    assert json.loads(template.read_text(encoding='utf-8')) == workflow


@pytest.mark.parametrize('renumber', [False, True])
@pytest.mark.parametrize('denoise', [None, .65])
def test_qwen_full_edit_keeps_source_vae_latent_and_accepts_native_denoise(tmp_path, renumber, denoise):
    workflow = qwen_reference_workflow()
    ids = {key: key for key in workflow}
    if renumber:
        workflow, ids = renumber_workflow_nodes(workflow)
    template = tmp_path / 'qwen-reference.json'
    template.write_text(json.dumps(workflow), encoding='utf-8')
    captured = {}

    class Client:
        def upload_image(self, path, *, subfolder):
            return f'{subfolder}/{Path(path).name}'

        def run_workflow(self, submitted, **kwargs):
            captured['workflow'] = submitted
            return ComfyResult('qwen-edit', True, ['result.png'])

        def download_output(self, reference, target):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (80, 120), 'white').save(target)
            return Path(target)

    generator = ReelGenerator(
        Client(), image_workflow_path='workflows/reel-image.json', reference_workflow_path=template,
        video_workflow_path='workflows/reel-video.json', output_root=tmp_path / 'out',
    )
    generator._apply_single_character_reference(
        reel_id='generic', scene_image_path=tmp_path / 'scene.png',
        reference_image_path=tmp_path / 'reference.png',
        detail_reference_image_path=tmp_path / 'detail.png', prompt='transfer reference appearance',
        tag='masked', timeout_seconds=60, preserve_geometry=False, denoise=denoise,
    )
    submitted = captured['workflow']
    sampler = submitted[ids['sampler']]['inputs']
    assert sampler['denoise'] == (1.0 if denoise is None else denoise)
    assert sampler['latent_image'] == [ids['encode-scene'], 0]
    assert submitted[ids['positive']]['inputs']['prompt'] == 'transfer reference appearance'
    assert submitted[ids['negative']]['inputs']['prompt'] == 'keep-negative'
    assert submitted[ids['positive']]['inputs']['image3'] == ['character-detail-load', 0]
    assert submitted[ids['negative']]['inputs']['image3'] == ['character-detail-load', 0]
    assert not any(node['class_type'] in ('EmptyFlux2LatentImage', 'Flux2Scheduler') for node in submitted.values())
    assert json.loads(template.read_text(encoding='utf-8')) == workflow


def test_qwen_missing_native_sampler_fails_before_submitting_edit(tmp_path):
    workflow = qwen_reference_workflow()
    workflow.pop('sampler')
    workflow['positive']['_meta'] = {'title': 'Positive Prompt'}
    template = tmp_path / 'bad-qwen.json'
    template.write_text(json.dumps(workflow), encoding='utf-8')

    class Client:
        def upload_image(self, path, *, subfolder):
            return str(path)

        def run_workflow(self, *args, **kwargs):
            pytest.fail('An incompatible native Qwen workflow must not submit an image job')

    generator = ReelGenerator.__new__(ReelGenerator)
    generator.client = Client()
    generator.reference_workflow_path = template
    with pytest.raises(ReelGenerationError, match='Qwen-Referenzworkflow'):
        generator._apply_single_character_reference(
            reel_id='generic', scene_image_path=tmp_path / 'scene.png',
            reference_image_path=tmp_path / 'reference.png', prompt='replace identity',
            tag='masked', timeout_seconds=60, preserve_geometry=False,
        )


def test_body_outline_never_expands_into_unowned_pixels_and_fences_another_character():
    mask = Image.new('L', (200, 200), 0)
    mask.paste(255, (40, 40, 100, 180))
    other = Image.new('L', (200, 200), 0)
    other.paste(255, (105, 40, 160, 180))
    owned = _character_edit_mask(mask, [other])
    assert owned.getpixel((38, 100)) == 0
    assert owned.getpixel((110, 100)) == 0
    assert owned.getpixel((102, 100)) == 0
    assert owned.getpixel((50, 100)) == 255
    assert owned.getpixel((0, 100)) == 0
    assert not any(new > old for new, old in zip(owned.get_flattened_data(), mask.get_flattened_data()))
    assert _character_edit_mask(mask, []).tobytes() == mask.tobytes()


def test_single_selected_reference_does_not_use_generic_person_scene_query(tmp_path):
    scene = tmp_path / 'scene.png'
    Image.new('RGB', (200, 200), 'white').save(scene)
    generator = ReelGenerator.__new__(ReelGenerator)
    calls = []

    def detect(**kwargs):
        calls.append(kwargs['selector_prompt'])
        mask = Image.new('L', (200, 200), 0)
        # Two comparably sized people are not a valid isolated target.
        mask.paste(255, (10, 20, 60, 180))
        mask.paste(255, (130, 20, 180, 180))
        mask.save(kwargs['output_path'])
        return kwargs['output_path']

    generator._detect_character_mask = detect
    with pytest.raises(ReelGenerationError, match='nicht sicher erkannt'):
        generator._locate_character_mask(
            reel_id='any', scene_image_path=scene, selector_prompt='woman with silver hair',
            identity_prompt='human woman', name='Mira', single_target=True,
            output_path=tmp_path / 'mask.png', timeout_seconds=60,
        )
    assert 'person' not in calls
    assert calls == ['woman with silver hair']


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
