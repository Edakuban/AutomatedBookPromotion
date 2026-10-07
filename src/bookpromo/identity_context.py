"""Conservative appearance hints from existing reference-portrait settings.

This is not a second scene planner: uncertain clauses are omitted rather than
letting reference staging, props or style compete with the saved scene plan.
The actual reference image remains authoritative for visual identity.
"""

from __future__ import annotations

import re


_FEATURE = re.compile(
    r"\b(?:face|facial|head|jawline|cheekbones|nose|ears?|eyes?|hair|beard|stubble|"
    r"skin|flesh|fur|feathers?|scales?|shell|surface|markings?|patterns?|"
    r"horns?|wings?|tail|antennae|antlers?|claws?|fangs?|teeth|fingers?|paws?|"
    r"body|build|physique|proportions?|silhouette|anatomy|limbs?|legs?|arms?|"
    r"athletic|lean|slim|slender|curvy|muscular|stocky|massive|graceful|masculine|"
    r"feminine|adult|youthful|elderly|twenties|thirties|forties|"
    r"clothing|outfit|coat|jacket|shirt|trousers|pants|dress|robes?|armor|armour|"
    r"sleeves?|boots?|belt|gloves?|goggles|jewelry|jewellery|"
    r"humanoid|nonhuman|demonic form|true form|"
    r"gesicht|kopf|kiefer|wangenknochen|nase|ohren|augen|haare?|bart|haut|fell|"
    r"federn|schuppen|panzer|muster|hörner|flügel|schwanz|krallen|zähne|finger|"
    r"körper(?:bau)?|statur|athletisch|schlank|muskulös|kräftig|proportionen|gliedmaßen|beine|arme|kleidung|outfit|"
    r"mantel|jacke|hemd|hose|kleid|rüstung|ärmel|stiefel|gürtel|handschuhe|schmuck|"
    r"dämonenform|dämonische form)\b", re.IGNORECASE,
)
_STAGING = re.compile(
    r"\b(?:standing|sitting|seated|lying|kneeling|holding|holds|carrying|carries|"
    r"wielding|wields|aiming|aims|posing|poses|walking|running|moves?|movements?|"
    r"spread|spreading|falling|background|backdrop|camera|composition|lighting|"
    r"head to|upper body focus|waist[- ]up|full[- ]body portrait|framing|close[- ]up|"
    r"cinematic|photorealistic|watercolou?r|anime|cartoon|no text|"
    r"steht|stehend|sitzt|sitzend|liegt|liegend|hält|haltend|trägt in|"
    r"bewegungen|bewegt|hintergrund|kamera|bildkomposition|beleuchtung|aquarell)\b",
    re.IGNORECASE,
)
_PROP = re.compile(
    r"\b(?:weapons?|guns?|pistols?|rifles?|swords?|daggers?|knives|blades?|"
    r"cigars?|cigarettes?|cups?|maps?|books?|staff|wand|"
    r"waffen?|pistole[n]?|gewehr[e]?|schwert(?:er)?|dolch[e]?|messer|klinge[n]?|"
    r"zigarre[n]?|zigarette[n]?|tasse[n]?|karte[n]?|buch|bücher|zauberstab)\b",
    re.IGNORECASE,
)


def reference_appearance_hints(reference_prompt: str) -> str:
    """Keep explicit appearance fragments, scoped to one referenced identity.

    No extra AI call, inference about species, or character-specific defaults.
    Conditional transformations are not promoted into mandatory anatomy.
    Bounds fail closed instead of silently cropping negations or long clauses.
    """
    visual = " ".join(reference_prompt.split())
    hints: list[str] = []
    for sentence in re.split(r"[.!?;]\s*", visual):
        sentence = re.split(r"\b(?:as if|als ob)\b", sentence, maxsplit=1, flags=re.IGNORECASE)[0]
        if re.search(
            r"\b(?:if|when|may|might|could|can|wenn|falls|könnte|kann)\b",
            sentence, re.IGNORECASE,
        ):
            continue
        # A negated list must stay intact: "no horns, wings or tail" does not
        # mean "no horns" followed by positive wings and tail instructions.
        negated = re.search(r"\b(?:no|not|without|never|kein\w*|nicht|ohne)\b", sentence, re.IGNORECASE)
        for fragment in ([sentence] if negated else sentence.split(',')):
            fragment = fragment.strip()
            staging = _STAGING.search(fragment)
            if staging:
                fragment = fragment[:staging.start()].strip(" :-")
                fragment = re.sub(r"\s+(?:and|und|with|mit)$", "", fragment, flags=re.IGNORECASE)
            # Even a worn weapon is scene inventory, not a required identity trait.
            # Keep the reference image, but do not introduce its props as instructions.
            if not fragment or _PROP.search(fragment) or not _FEATURE.search(fragment):
                continue
            if len(fragment) > 500:
                continue
            if fragment.casefold() not in {hint.casefold() for hint in hints}:
                hints.append(fragment)
    result = "; ".join(hints)
    if len(result) > 1600:
        raise ValueError("Reference appearance hints are too long; simplify the reference portrait prompt")
    return result
