import json

import pytest

from helixgen.device.symbols import (
    DeviceSymbols,
    SymbolsError,
    normalize_parameter_name,
)

# A stand-in for Helix.sym, built to the shape HX Edit ships: a JSON array whose
# position is the model's wire identity, with Mono/Stereo variants carried as
# separate symbols that have different parameter counts *and* orders.
SAMPLE = [
    {"symbol": "HD2_DistHorizonDrive", "parameters": ["Drive", "Level", "Bright"]},
    {
        "symbol": "HD2_Chorus70sChorusMono",
        "parameters": ["Mode", "SyncSelect1", "ChorusIntensity", "Mix", "Level"],
    },
    {
        "symbol": "HD2_Chorus70sChorusStereo",
        # Note the stereo-only "Spread" sitting mid-list, not at the end: this is
        # exactly the interleaving that makes a host-side order unusable.
        "parameters": ["Mode", "SyncSelect1", "ChorusIntensity", "Spread", "Mix", "Level"],
    },
    {"symbol": "HD2_ReverbHallStereo", "parameters": ["Decay", "Mix"]},
    {"symbol": "HD2_NoParams"},
]


@pytest.fixture
def symbols() -> DeviceSymbols:
    return DeviceSymbols.parse(json.dumps(SAMPLE))


def test_array_position_is_the_model_identity(symbols):
    assert len(symbols) == 5
    assert symbols.by_index(0).symbol == "HD2_DistHorizonDrive"
    assert symbols.by_index(3).symbol == "HD2_ReverbHallStereo"
    assert symbols.index_of("HD2_Chorus70sChorusStereo") == 2
    assert symbols.by_index(99) is None
    assert symbols.index_of("HD2_Nonexistent") is None


def test_missing_parameters_key_is_an_empty_list(symbols):
    assert symbols.by_symbol("HD2_NoParams").parameters == ()


def test_variants_of_reports_what_the_device_actually_offers(symbols):
    assert symbols.variants_of("HD2_Chorus70sChorus") == ["Mono", "Stereo"]
    # Reverbs are commonly stereo-only; the absence of a Mono symbol must not be
    # read as "default to Mono".
    assert symbols.variants_of("HD2_ReverbHall") == ["Stereo"]
    # An unsuffixed symbol, as most amps and cabs are.
    assert symbols.variants_of("HD2_DistHorizonDrive") == [""]
    assert symbols.variants_of("HD2_Unknown") == []


def test_resolve_picks_the_only_variant_when_there_is_no_choice(symbols):
    # stereo=False must not turn into a lookup for a Mono symbol that does not exist.
    assert symbols.resolve("HD2_ReverbHall", stereo=False).symbol == "HD2_ReverbHallStereo"
    assert symbols.resolve("HD2_ReverbHall").symbol == "HD2_ReverbHallStereo"
    assert symbols.resolve("HD2_DistHorizonDrive", stereo=True).symbol == "HD2_DistHorizonDrive"


def test_resolve_honours_the_stereo_flag_where_there_is_a_real_choice(symbols):
    assert symbols.resolve("HD2_Chorus70sChorus", stereo=True).symbol == "HD2_Chorus70sChorusStereo"
    assert symbols.resolve("HD2_Chorus70sChorus", stereo=False).symbol == "HD2_Chorus70sChorusMono"
    assert symbols.resolve("HD2_Chorus70sChorus").symbol == "HD2_Chorus70sChorusMono"
    assert symbols.resolve("HD2_Unknown") is None


def test_parameter_ordinals_differ_between_variants(symbols):
    mono = symbols.by_symbol("HD2_Chorus70sChorusMono")
    stereo = symbols.by_symbol("HD2_Chorus70sChorusStereo")

    # The shared prefix agrees...
    assert mono.ordinal_of("ChorusIntensity") == stereo.ordinal_of("ChorusIntensity") == 2
    # ...and everything after the stereo-only parameter is shifted. This is the bug
    # class the whole module exists to prevent.
    assert mono.ordinal_of("Mix") == 3
    assert stereo.ordinal_of("Mix") == 4
    assert mono.ordinal_of("Spread") is None


def test_ordinal_lookup_tolerates_spelling_drift(symbols):
    entry = symbols.by_symbol("HD2_Chorus70sChorusMono")
    for spelling in ("ChorusIntensity", "chorus intensity", "Chorus_Intensity", "CHORUSINTENSITY"):
        assert entry.ordinal_of(spelling) == 2


def test_resolve_by_value_count_picks_the_matching_variant(symbols):
    assert symbols.resolve_by_value_count("HD2_Chorus70sChorus", 5).symbol.endswith("Mono")
    assert symbols.resolve_by_value_count("HD2_Chorus70sChorus", 6).symbol.endswith("Stereo")
    # A reverb carries one extra trailing value (the Trails switch) past its symbol.
    assert symbols.resolve_by_value_count("HD2_ReverbHall", 3).symbol == "HD2_ReverbHallStereo"
    assert symbols.resolve_by_value_count("HD2_ReverbHall", 9) is None


def test_normalize_parameter_name():
    assert normalize_parameter_name("High Cut") == normalize_parameter_name("HighCut")
    assert normalize_parameter_name("Gate_Range") == normalize_parameter_name("GateRange")


@pytest.mark.parametrize(
    "payload",
    ['{"not": "an array"}', "[1, 2, 3]", '[{"parameters": []}]', "not json at all"],
)
def test_malformed_symbol_tables_are_rejected(payload):
    with pytest.raises(SymbolsError):
        DeviceSymbols.parse(payload)


def test_load_reports_a_missing_file_helpfully(tmp_path):
    with pytest.raises(SymbolsError, match="your own HX Edit"):
        DeviceSymbols.load(tmp_path / "Helix.sym")


def test_load_reads_from_disk(tmp_path):
    path = tmp_path / "Helix.sym"
    path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    assert len(DeviceSymbols.load(path)) == 5
