from helixgen.dataset import ModelCatalog

# Line 6 internal model names carry the block family as a prefix, so the
# category in helix_model_information.json can be checked against it. Categories
# drive footswitch auto-assignment, the LLM catalog grouping and the cab-skip
# rule, so a wrong one silently changes generated presets.
PREFIX_CATEGORIES = {
    "HD2_Amp": "Amp",
    "HD2_Preamp": "Amp",
    "HD2_Cab": "Cab",
    "HD2_ImpulseResponse": "Cab",
    "HD2_Dist": "Distortion",
    "HD2_Delay": "Delay",
    "VIC_Delay": "Delay",
    "HD2_Reverb": "Reverb",
    "VIC_Reverb": "Reverb",
    "HD2_Chorus": "Modulation",
    "HD2_Flanger": "Modulation",
    "HD2_Phaser": "Modulation",
    "HD2_Tremolo": "Modulation",
    "HD2_Vibrato": "Modulation",
    "HD2_RingModulator": "Modulation",
    "HD2_Compressor": "Dynamics",
    "HD2_Gate": "Dynamics",
    "HD2_Wah": "Wah",
    "HD2_Pitch": "Pitch/Synth",
    "VIC_Pitch": "Pitch/Synth",
    "L6SPB_Poly": "Pitch/Synth",
    "HD2_Synth": "Pitch/Synth",
    "HD2_EQ": "EQ",
    "HD2_Filter": "Filter",
    "HD2_VolPan": "Volume/Pan",
    "HD2_AppDSPFlow": "Routing",
    "HelixStomp_AppDSPFlow": "Routing",
    "HD2_FXLoop": "Routing",
    "HD2_Looper": "Routing",
}

# Models whose internal name does not encode their family.
EXPLICIT_CATEGORIES = {
    "HD2_CaliQ": "EQ",
    "HD2_DM4BlueComp": "Dynamics",
    "HD2_FM4VoiceBox": "Filter",
    "HD2_RetroReel": "Modulation",
    "L6SPB_AcousGtrSim": "EQ",
    "VIC_DynPlate": "Reverb",
    "VIC_FeedbackSim": "Dynamics",
    "VIC_FlexoVibe": "Modulation",
    "Victoria_EuclideanDelay": "Delay",
}


def _expected_category(internal_name: str) -> str | None:
    if internal_name in EXPLICIT_CATEGORIES:
        return EXPLICIT_CATEGORIES[internal_name]
    matches = [p for p in PREFIX_CATEGORIES if internal_name.startswith(p)]
    if not matches:
        return None
    return PREFIX_CATEGORIES[max(matches, key=len)]


def test_every_model_category_matches_its_internal_name(dataset_path):
    problems = []
    for model in ModelCatalog(dataset_path).models():
        expected = _expected_category(model.internal_name)
        if expected is None:
            problems.append(
                f"{model.display_name} ({model.internal_name}): unknown prefix, "
                "add it to PREFIX_CATEGORIES or EXPLICIT_CATEGORIES"
            )
        elif model.category != expected:
            problems.append(
                f"{model.display_name} ({model.internal_name}): "
                f"category {model.category!r}, expected {expected!r}"
            )
    assert not problems, "\n".join(problems)

