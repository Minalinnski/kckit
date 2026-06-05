"""Unit tests for NGA import pipeline (no API call needed)."""
import json, tempfile, os
import pytest
from tools.import_nga import correct_equip_names, load_equip_db
from core.schema import MapStrategy, save_strategy, load_strategy, merge_strategy


MOCK_CLAUDE_OUTPUT = {
    "map": "5-4",
    "notes": "5-4ボス撃破周回",
    "source": "test",
    "presets": [
        {
            "name": "標準編成",
            "fleet": {
                "type": "single",
                "slots": [
                    {"ship_class": ["CA"], "count": 2},
                    {"ship_class": ["DD"], "count": 4},
                ],
            },
            "requirements": {
                "air_state": "none",
                "los_formula": 33,
                "los_min": 145.0,
            },
            "equip_notes": "DDは魚雷+電探",
            "example": [
                {
                    "ship": "高雄改二",
                    "equips": [
                        "20.3cm(2号)連装砲",
                        "20.3cm(2号)連装砲",
                        "夜間瑞雲",
                        "32号対水上電探",
                    ],
                }
            ],
            "tags": ["standard"],
        }
    ],
}


class TestSchemaValidation:
    def test_mock_output_validates(self):
        strategy = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        assert strategy.map == "5-4"
        assert len(strategy.presets) == 1
        assert strategy.presets[0].name == "標準編成"

    def test_fleet_slot_count(self):
        strategy = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        fleet = strategy.presets[0].fleet
        total = sum(s.count for s in fleet.slots)
        assert total == 6

    def test_requirements_los_set(self):
        strategy = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        req = strategy.presets[0].requirements
        assert req.los_min == 145.0


class TestEquipNameCorrection:
    def test_exact_match_unchanged(self):
        db = {"1": "12.7cm連装砲", "2": "魚雷"}
        strategy = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        # Inject a name that IS in DB
        strategy.presets[0].example[0].equips = ["20.3cm(2号)連装砲"]
        result = correct_equip_names(strategy, {"x": "20.3cm(2号)連装砲"})
        assert result.presets[0].example[0].equips[0] == "20.3cm(2号)連装砲"

    def test_typo_corrected(self):
        # "32号対水上電探" with slight OCR typo "32号対水上電槽"
        db = {"100": "32号対水上電探"}
        strategy = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        strategy.presets[0].example[0].equips = ["32号対水上電槽"]
        result = correct_equip_names(strategy, db)
        assert result.presets[0].example[0].equips[0] == "32号対水上電探"

    def test_no_db_returns_unchanged(self):
        strategy = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        original = strategy.presets[0].example[0].equips[:]
        result = correct_equip_names(strategy, {})
        assert result.presets[0].example[0].equips == original

    def test_real_equip_db_if_available(self):
        """If the real equip DB is built, verify a known name is in it."""
        db = load_equip_db()
        if not db:
            pytest.skip("equip DB not built yet")
        # 12cm単装砲 should always be in any KanColle equipment list
        assert "1" in db
        assert db["1"] == "12cm単装砲"


class TestYamlMerge:
    def test_merge_adds_new_preset(self):
        strategy1 = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        strategy2 = MapStrategy.model_validate({
            **MOCK_CLAUDE_OUTPUT,
            "presets": [{
                **MOCK_CLAUDE_OUTPUT["presets"][0],
                "name": "低コスト編成",
            }],
        })

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            path = f.name
        try:
            save_strategy(strategy1, path)
            merged = merge_strategy(path, strategy2)
            assert len(merged.presets) == 2
            names = [p.name for p in merged.presets]
            assert "標準編成" in names
            assert "低コスト編成" in names
        finally:
            os.unlink(path)

    def test_merge_updates_existing_preset(self):
        strategy1 = MapStrategy.model_validate(MOCK_CLAUDE_OUTPUT)
        updated = MapStrategy.model_validate({
            **MOCK_CLAUDE_OUTPUT,
            "presets": [{
                **MOCK_CLAUDE_OUTPUT["presets"][0],
                "equip_notes": "更新済みのメモ",
            }],
        })

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            path = f.name
        try:
            save_strategy(strategy1, path)
            merged = merge_strategy(path, updated)
            assert len(merged.presets) == 1
            assert merged.presets[0].equip_notes == "更新済みのメモ"
        finally:
            os.unlink(path)
