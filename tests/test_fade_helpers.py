from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from stream_state_router.obs.fade_helpers import (
    FadeHelperManifestError,
    FadeHelperManifestStore,
    HELPER_MANIFEST_SCHEMA_VERSION,
    LAYOUT_FADE_FILTER_PREFIX,
)


class FadeHelperManifestStoreTests(unittest.TestCase):
    def test_prepare_persists_generated_identity_before_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-manifest.json"
            store = FadeHelperManifestStore(path)

            identity = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=7,
            )

            self.assertTrue(identity.helper_id)
            self.assertTrue(identity.filter_name.startswith(LAYOUT_FADE_FILTER_PREFIX))
            self.assertEqual(identity.state, "prepared")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], HELPER_MANIFEST_SCHEMA_VERSION)
            self.assertEqual(raw["helpers"][0]["helper_id"], identity.helper_id)

    def test_same_uuid_reuses_helper_and_updates_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FadeHelperManifestStore(
                Path(temp_dir) / "helper-manifest.json"
            )
            first = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=1,
            )
            second = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar Renamed",
                source_kind="image_source",
                session_generation=2,
            )

            self.assertEqual(first.helper_id, second.helper_id)
            self.assertEqual(second.source_alias, "Avatar Renamed")

    def test_same_name_new_uuid_is_a_distinct_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FadeHelperManifestStore(
                Path(temp_dir) / "helper-manifest.json"
            )
            first = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="uuid-old",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=1,
            )
            second = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="uuid-new",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=2,
            )

            self.assertNotEqual(first.helper_id, second.helper_id)
            self.assertEqual(len(store.entries()), 2)

    def test_mark_observed_records_only_non_temporary_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FadeHelperManifestStore(
                Path(temp_dir) / "helper-manifest.json"
            )
            identity = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=1,
            )

            observed = store.mark_observed(
                identity.helper_id,
                source_alias="Avatar",
                non_temporary_settings={
                    "opacity": 0.25,
                    "contrast": 0.1,
                },
            )

            self.assertEqual(observed.state, "observed")
            self.assertEqual(
                dict(observed.non_temporary_settings or {}),
                {"contrast": 0.1},
            )
            self.assertTrue(
                store.settings_compatible(
                    observed,
                    {"opacity": 1.0, "contrast": 0.1},
                )
            )
            self.assertFalse(
                store.settings_compatible(
                    observed,
                    {"opacity": 1.0, "contrast": 0.2},
                )
            )

    def test_duplicate_helper_id_is_rejected_as_ambiguous_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-manifest.json"
            store = FadeHelperManifestStore(path)
            store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=1,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["helpers"].append(dict(raw["helpers"][0]))
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaisesRegex(
                FadeHelperManifestError,
                "duplicate helper_id",
            ):
                store.entries()

    def test_duplicate_qualified_source_ownership_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-manifest.json"
            store = FadeHelperManifestStore(path)
            first = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=1,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            duplicate = dict(raw["helpers"][0])
            duplicate["helper_id"] = "another-helper-id"
            duplicate["filter"] = dict(duplicate["filter"])
            duplicate["filter"]["name"] = (
                LAYOUT_FADE_FILTER_PREFIX + duplicate["helper_id"]
            )
            raw["helpers"].append(duplicate)
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaisesRegex(
                FadeHelperManifestError,
                "multiple helpers for one qualified source",
            ):
                store.entries()

            self.assertEqual(first.source_uuid, "input-uuid")

    def test_future_manifest_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-manifest.json"
            original = {
                "schema": HELPER_MANIFEST_SCHEMA_VERSION + 1,
                "helpers": [],
                "future": "preserve-me",
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            store = FadeHelperManifestStore(path)

            with self.assertRaises(FadeHelperManifestError):
                store.prepare_layout_fade(
                    connection_host="127.0.0.1",
                    connection_port=4455,
                    collection="Collection A",
                    source_uuid="input-uuid",
                    source_alias="Avatar",
                    source_kind="image_source",
                    session_generation=1,
                )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_failed_atomic_replace_preserves_previous_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-manifest.json"
            store = FadeHelperManifestStore(path)
            first = store.prepare_layout_fade(
                connection_host="127.0.0.1",
                connection_port=4455,
                collection="Collection A",
                source_uuid="input-uuid",
                source_alias="Avatar",
                source_kind="image_source",
                session_generation=1,
            )
            before = path.read_bytes()

            with patch(
                "stream_state_router.obs.fade_helpers.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(
                    FadeHelperManifestError,
                    "replace failed",
                ):
                    store.prepare_layout_fade(
                        connection_host="127.0.0.1",
                        connection_port=4455,
                        collection="Collection A",
                        source_uuid=first.source_uuid,
                        source_alias="Avatar Renamed",
                        source_kind="image_source",
                        session_generation=2,
                    )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                list(path.parent.glob(f".{path.name}.*.tmp")),
                [],
            )

    def test_corrupt_manifest_is_rejected_without_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-manifest.json"
            path.write_text("{not-json", encoding="utf-8")
            store = FadeHelperManifestStore(path)

            with self.assertRaises(FadeHelperManifestError):
                store.entries()

            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")


if __name__ == "__main__":
    unittest.main()
