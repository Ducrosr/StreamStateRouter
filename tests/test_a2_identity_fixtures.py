from __future__ import annotations

import json
from pathlib import Path
import unittest


_FIXTURE = Path("tests/fixtures/a2_identity_scenarios.json")
_REQUIRED_IDS = {
    "rename_same_uuid",
    "delete_recreate_same_name_new_uuid",
    "duplicate_name_occurrences",
    "duplicate_reorder",
    "scene_item_id_renumber",
    "collection_round_trip_a_b_a",
    "transport_reconnect_same_collection",
    "legacy_name_only_profile",
    "nested_group_move",
}


class A2IdentityFixtureTests(unittest.TestCase):
    def _payload(self) -> dict:
        return json.loads(_FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_schema_and_ids_are_stable(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["schema"], 1)
        scenarios = payload["scenarios"]
        ids = [str(item["id"]) for item in scenarios]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(_REQUIRED_IDS.issubset(ids))

    def test_each_scenario_states_safety_not_implementation(self) -> None:
        for scenario in self._payload()["scenarios"]:
            self.assertTrue(str(scenario.get("category") or "").strip())
            self.assertTrue(str(scenario.get("setup") or "").strip())
            self.assertTrue(str(scenario.get("event") or "").strip())
            expectations = scenario.get("safety_expectations")
            self.assertIsInstance(expectations, list)
            self.assertGreaterEqual(len(expectations), 2)
            self.assertNotIn("implementation", scenario)
            self.assertNotIn("algorithm", scenario)

    def test_replacement_and_ambiguity_cases_are_fail_closed(self) -> None:
        by_id = {
            str(item["id"]): item
            for item in self._payload()["scenarios"]
        }
        combined = " ".join(
            by_id[scenario_id]["safety_expectations"][1]
            for scenario_id in (
                "delete_recreate_same_name_new_uuid",
                "duplicate_name_occurrences",
                "legacy_name_only_profile",
            )
        ).casefold()
        self.assertTrue(
            "block" in combined or "blocked" in combined,
            combined,
        )


if __name__ == "__main__":
    unittest.main()
