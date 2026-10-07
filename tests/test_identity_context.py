"""Existing appearance settings must not become a competing scene prompt."""

from pathlib import Path

import pytest

from bookpromo.identity_context import reference_appearance_hints
from bookpromo.reel_generation import CharacterReferenceSpec, build_planned_reference_prompt
from test_reel_generation import resolved_reference_plan, restage_fixture


def test_keeps_nonhuman_form_build_clothes_and_removes_reference_staging():
    hints = reference_appearance_hints(
        'A female creature in her true form. Her skin is red with black patterns, '
        'as if fire moves beneath it. Elegant horns rise from her long dark hair. '
        'Her fingers end in sharp claws, large black wings spread from her back, '
        'and a long tail moves slowly behind her. Her body is slim, curvy and graceful, '
        'and her movements should feel predatory. She wears a fitted black coat holding a pistol. '
        'Standing in front of a cathedral with a sword. Cinematic lighting, gothic background.'
    )
    for appearance in ('true form', 'red with black patterns', 'horns', 'dark hair',
                       'sharp claws', 'black wings', 'long tail', 'slim', 'curvy', 'black coat'):
        assert appearance in hints
    for staging in ('moves', 'spread', 'predatory', 'pistol', 'sword', 'cathedral', 'Cinematic', 'background'):
        assert staging not in hints


def test_negated_lists_and_conditional_transformations_are_not_positive_anatomy():
    hints = reference_appearance_hints(
        'Her skin is pale. She is human with no horns, wings or tail. '
        'When she transforms, her skin is green, her horns grow long. '
        'She may have fangs, black wings and claws. She wears a blue jacket.'
    )
    assert 'no horns, wings or tail' in hints
    assert 'pale' in hints and 'blue jacket' in hints
    assert 'green' not in hints and 'grow long' not in hints
    assert 'fangs' not in hints and 'black wings' not in hints


def test_animal_mechanical_and_german_appearance_without_species_defaults():
    assert reference_appearance_hints(
        'Its fur is silver grey. It has four copper wings and two antennae. '
        'Watercolor background. Holding a map.'
    ) == 'Its fur is silver grey; It has four copper wings and two antennae'
    hints = reference_appearance_hints(
        'Schlanker Körperbau. Rote Haut und schwarze Flügel. '
        'Keine Hörner, Krallen oder Zähne. Schwarze Jacke. Stehend vor einer Burg mit Schwert.'
    )
    assert 'Rote Haut und schwarze Flügel' in hints
    assert 'Keine Hörner, Krallen oder Zähne' in hints
    assert 'Schwarze Jacke' in hints and 'Schwert' not in hints


def test_text_hints_are_scoped_to_each_reference_not_shared_or_used_as_props():
    references = [
        CharacterReferenceSpec('Copper Fox', Path('fox.png'), '',
                               'Silver fur. No horns, wings or tail. Holding a sword.'),
        CharacterReferenceSpec('Moss Dragon', Path('dragon.png'), '',
                               'Green scales. Large black wings. Two curled horns.'),
    ]
    prompt = build_planned_reference_prompt(resolved_reference_plan([r.name for r in references]), references)
    fox, dragon = prompt.split('Image 2:')
    assert 'Appearance hints for Copper Fox ONLY' in fox
    assert 'No horns, wings or tail' in fox and 'Green scales' not in fox
    assert 'Appearance hints for Moss Dragon ONLY' in dragon
    assert 'Green scales' in dragon and 'Two curled horns' in dragon
    assert 'No horns' not in dragon and 'sword' not in prompt


def test_appearance_hints_reach_actual_native_conditioning_without_extra_ai(tmp_path):
    from dataclasses import replace
    generator, scene, references, captured, _ = restage_fixture(tmp_path)
    references = [replace(r, identity_prompt='Dark red skin. Black wings. '
                          'Standing in a cathedral holding a pistol.') for r in references]
    generator.restage_character_references(
        reel_id='generic', scene_image_path=scene, references=references,
        scene_plan=resolved_reference_plan([r.name for r in references]),
    )
    graph, _ = captured['jobs'][0]
    prompt = graph['restage-positive']['inputs']['text']
    assert 'Dark red skin' in prompt and 'Black wings' in prompt
    assert 'cathedral' not in prompt and 'pistol' not in prompt
    assert len(captured['jobs']) == 1


def test_empty_and_unrecognized_settings_use_visual_reference_without_inventing_traits():
    assert reference_appearance_hints('') == ''
    assert reference_appearance_hints('Standing portrait holding a sword or silver daggers.') == ''


def test_oversized_appearance_context_fails_without_truncating_exclusions():
    with pytest.raises(ValueError, match='too long'):
        reference_appearance_hints(' '.join(f'Her skin has marking number {i}.' for i in range(100)))
