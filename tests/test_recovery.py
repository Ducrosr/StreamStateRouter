from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from stream_state_router.services.recovery import (
    CLEANUP_SCHEMA_VERSION,
    RuntimeMarker,
    RuntimeMarkerFormatError,
)


class RuntimeMarkerTests(unittest.TestCase):
    def test_pre_cleanup_legacy_marker_remains_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "clean_shutdown": False,
                        "updated_at": "legacy",
                    }
                ),
                encoding="utf-8",
            )
            marker = RuntimeMarker()
            marker.path = path

            marker.start()

            self.assertTrue(marker.previous_unclean)
            self.assertFalse(marker.previous_cleanup_incomplete)
            self.assertEqual(marker.previous_pending_cleanup, ())
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(rewritten["cleanup_schema"], CLEANUP_SCHEMA_VERSION)

    def test_unversioned_marker_rejects_invalid_state_flags(self):
        mutations = (
            {"clean_shutdown": "false"},
            {"clean_shutdown": False, "cleanup_complete": 0},
        )
        for invalid in mutations:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                original = dict(invalid)
                original["pending_cleanup"] = []
                path.write_text(json.dumps(original), encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaises(RuntimeMarkerFormatError):
                    marker.start()

                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    original,
                )

    def test_legacy_activation_cleanup_marker_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "clean_shutdown": False,
                        "cleanup_complete": False,
                        "pending_cleanup": [
                            {
                                "policy": "egg",
                                "collection": "Collection A",
                                "target": {
                                    "container": "[Module] EasterEgg",
                                    "container_kind": "scene",
                                    "source": "Cloud",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            marker = RuntimeMarker()
            marker.path = path

            marker.start()

            self.assertTrue(marker.previous_unclean)
            self.assertTrue(marker.previous_cleanup_incomplete)
            self.assertEqual(len(marker.previous_pending_cleanup), 1)
            self.assertEqual(
                marker.previous_pending_cleanup[0]["kind"],
                "activation_hide",
            )

    def test_finalization_rejects_late_cleanup_checkpoint_explicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            marker = RuntimeMarker()
            marker.path = path
            marker.start()
            marker.finish(
                clean_shutdown=True,
                cleanup_complete=True,
                pending_cleanup=(),
            )

            late_cleanup = {
                "kind": "layout_fade",
                "source": "[Webcam] Avatar",
                "collection": "Collection A",
            }

            with self.assertRaisesRegex(
                RuntimeError,
                "déjà finalisé",
            ):
                marker.checkpoint_pending_cleanup((late_cleanup,))

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["clean_shutdown"])
            self.assertTrue(data["cleanup_complete"])
            self.assertEqual(data["pending_cleanup"], [])

    def test_layout_fade_cleanup_is_persisted_with_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            marker = RuntimeMarker()
            marker.path = path
            fade = {
                "kind": "layout_fade",
                "source": "[Webcam] Avatar",
                "collection": "Collection A",
                "created_at": 1.0,
                "attempts": 2,
                "last_error": "offline",
            }

            marker.finish(
                clean_shutdown=True,
                cleanup_complete=False,
                pending_cleanup=(fade,),
            )

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["cleanup_schema"], CLEANUP_SCHEMA_VERSION)
            self.assertFalse(data["cleanup_complete"])
            self.assertEqual(len(data["pending_cleanup"]), 1)
            self.assertEqual(
                data["pending_cleanup"][0]["source"],
                fade["source"],
            )
            self.assertEqual(
                data["pending_cleanup"][0]["collection"],
                fade["collection"],
            )
            self.assertTrue(data["pending_cleanup"][0]["legacy"])


    def test_schema3_layout_fade_identity_round_trips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            marker = RuntimeMarker()
            marker.path = path
            fade = {
                "kind": "layout_fade",
                "source": "[Webcam] Avatar",
                "collection": "Collection A",
                "helper_id": "abc123",
                "source_uuid": "input-uuid",
                "source_kind": "image_source",
                "connection": {"host": "127.0.0.1", "port": 4455},
                "filter_name": "[SSR] Layout Fade::abc123",
                "filter_kind": "color_filter_v2",
                "cleanup_action": "neutralize_disable",
                "created_at": 1.0,
                "attempts": 0,
                "last_error": "",
            }

            marker.finish(
                clean_shutdown=False,
                cleanup_complete=False,
                pending_cleanup=(fade,),
            )

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["cleanup_schema"], CLEANUP_SCHEMA_VERSION)
            self.assertEqual(data["pending_cleanup"][0]["helper_id"], "abc123")
            self.assertFalse(data["pending_cleanup"][0]["legacy"])

            resumed = RuntimeMarker()
            resumed.path = path
            resumed.start()

            self.assertEqual(len(resumed.previous_pending_cleanup), 1)
            restored = resumed.previous_pending_cleanup[0]
            self.assertEqual(restored["helper_id"], "abc123")
            self.assertEqual(restored["source_uuid"], "input-uuid")
            self.assertFalse(restored["legacy"])

    def test_checkpoint_refuses_unknown_cleanup_without_overwriting_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            marker = RuntimeMarker()
            marker.path = path
            marker.start()
            before = path.read_bytes()

            with self.assertRaisesRegex(
                RuntimeMarkerFormatError,
                "inconnue ou incomplète",
            ):
                marker.checkpoint_pending_cleanup(
                    ({"kind": "future_cleanup_kind", "opaque": True},)
                )

            self.assertEqual(path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_previous_runtime_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                "pending_cleanup": [
                    {
                        "kind": "layout_fade",
                        "source": "[Webcam] Avatar",
                        "collection": "Collection A",
                    }
                ],
                "updated_at": "before",
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with patch(
                "stream_state_router.services.recovery.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    marker.finish(
                        clean_shutdown=True,
                        cleanup_complete=True,
                        pending_cleanup=(),
                    )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )
            self.assertEqual(
                list(path.parent.glob(f".{path.name}.*.tmp")),
                [],
            )

    def test_schema2_runtime_flags_require_booleans(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": "false",
                "cleanup_complete": False,
                "cleanup_schema": 2,
                "pending_cleanup": [],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(
                RuntimeMarkerFormatError,
                "clean_shutdown invalide|cleanup_complete invalide",
            ):
                marker.start()

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_schema3_runtime_flags_require_booleans(self):
        mutations = (
            ("clean_shutdown", "false"),
            ("cleanup_complete", 0),
        )
        for field, invalid in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                original = {
                    "clean_shutdown": False,
                    "cleanup_complete": False,
                    "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                    "pending_cleanup": [],
                }
                original[field] = invalid
                path.write_text(json.dumps(original), encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaisesRegex(
                    RuntimeMarkerFormatError,
                    "clean_shutdown invalide|cleanup_complete invalide",
                ):
                    marker.start()

                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    original,
                )

    def test_schema3_fade_guard_flags_require_boolean_consistency(self):
        mutations = (
            ("legacy", True),
            ("ambiguous", 0),
        )
        for field, invalid in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                item = {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                    "helper_id": "abc123",
                    "source_uuid": "input-uuid",
                    "source_kind": "image_source",
                    "connection": {"host": "127.0.0.1", "port": 4455},
                    "filter_name": "[SSR] Layout Fade::abc123",
                    "filter_kind": "color_filter_v2",
                    "cleanup_action": "neutralize_disable",
                    "legacy": False,
                    "ambiguous": False,
                }
                item[field] = invalid
                original = {
                    "clean_shutdown": False,
                    "cleanup_complete": False,
                    "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                    "pending_cleanup": [item],
                }
                path.write_text(json.dumps(original), encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaisesRegex(
                    RuntimeMarkerFormatError,
                    "inconnue ou incomplète|contradictoire",
                ):
                    marker.start()

                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    original,
                )

    def test_schema3_cannot_downgrade_owned_fade_to_legacy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                "pending_cleanup": [
                    {
                        "kind": "layout_fade",
                        "source": "[Webcam] Avatar",
                        "collection": "Collection A",
                        "source_uuid": "input-uuid",
                        "source_kind": "image_source",
                        "connection": {"host": "127.0.0.1", "port": 4455},
                        "filter_name": "[SSR] Layout Fade::abc123",
                        "filter_kind": "color_filter_v2",
                        "cleanup_action": "neutralize_disable",
                        "legacy": False,
                        "ambiguous": False,
                    }
                ],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(
                RuntimeMarkerFormatError,
                "layout_fade contradictoire",
            ):
                marker.start()

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_invalid_schema3_layout_fade_identity_is_preserved_and_blocks_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                "pending_cleanup": [
                    {
                        "kind": "layout_fade",
                        "source": "[Webcam] Avatar",
                        "collection": "Collection A",
                        "helper_id": "abc123",
                        "source_uuid": "input-uuid",
                        "source_kind": "image_source",
                        # Missing connection/filter identity on purpose.
                    }
                ],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(
                RuntimeMarkerFormatError,
                "inconnue ou incomplète|contradictoire",
            ):
                marker.start()

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertEqual(marker.previous_pending_cleanup, ())

    def test_schema3_non_string_identity_is_preserved_and_blocks_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                "pending_cleanup": [
                    {
                        "kind": "layout_fade",
                        "source": {"unexpected": "mapping"},
                        "collection": "Collection A",
                        "helper_id": 123,
                        "source_uuid": "input-uuid",
                        "source_kind": "image_source",
                        "connection": {"host": "127.0.0.1", "port": 4455},
                        "filter_name": "[SSR] Layout Fade::abc123",
                        "filter_kind": "color_filter_v2",
                        "cleanup_action": "neutralize_disable",
                    }
                ],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(
                RuntimeMarkerFormatError,
                "inconnue ou incomplète|contradictoire",
            ):
                marker.start()

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_explicit_legacy_schema2_remains_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": 2,
                "pending_cleanup": [
                    {
                        "kind": "layout_fade",
                        "source": "[Webcam] Avatar",
                        "collection": "Collection A",
                    }
                ],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            marker.start()

            self.assertEqual(len(marker.previous_pending_cleanup), 1)
            self.assertTrue(marker.previous_pending_cleanup[0]["legacy"])
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(rewritten["cleanup_schema"], CLEANUP_SCHEMA_VERSION)

    def test_legacy_cleanup_cannot_promote_helper_ownership(self):
        for legacy_schema in (None, 2):
            with self.subTest(schema=legacy_schema), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                item = {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                    "helper_id": "abc123",
                    "source_uuid": "input-uuid",
                    "source_kind": "image_source",
                    "connection": {"host": "127.0.0.1", "port": 4455},
                    "filter_name": "[SSR] Layout Fade::abc123",
                    "filter_kind": "color_filter_v2",
                    "cleanup_action": "neutralize_disable",
                }
                original = {
                    "clean_shutdown": False,
                    "cleanup_complete": False,
                    "pending_cleanup": [item],
                }
                if legacy_schema is not None:
                    original["cleanup_schema"] = legacy_schema
                path.write_text(json.dumps(original), encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaisesRegex(
                    RuntimeMarkerFormatError,
                    "legacy contient une identité helper",
                ):
                    marker.start()

                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    original,
                )

    def test_malformed_schema2_cleanup_is_preserved_and_blocks_rewrite(self):
        malformed_pending = (
            {"unexpected": "mapping"},
            ["not-an-object"],
            {"kind": "unknown_cleanup_kind"},
        )
        for pending_cleanup in malformed_pending:
            with self.subTest(pending_cleanup=pending_cleanup), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                original = {
                    "clean_shutdown": False,
                    "cleanup_complete": False,
                    "cleanup_schema": 2,
                    "pending_cleanup": pending_cleanup,
                }
                path.write_text(json.dumps(original), encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaises(RuntimeMarkerFormatError):
                    marker.start()

                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    original,
                )

    def test_unknown_explicit_past_schema_is_preserved_and_blocks_rewrite(self):
        for unsupported_schema in (1, 0, -1):
            with self.subTest(schema=unsupported_schema), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                original = {
                    "clean_shutdown": False,
                    "cleanup_complete": False,
                    "cleanup_schema": unsupported_schema,
                    "pending_cleanup": [],
                }
                path.write_text(json.dumps(original), encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaisesRegex(
                    RuntimeMarkerFormatError,
                    "cleanup_schema inconnu",
                ):
                    marker.start()

                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    original,
                )

    def test_future_cleanup_schema_is_preserved_and_blocks_downgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": CLEANUP_SCHEMA_VERSION + 1,
                "pending_cleanup": [
                    {
                        "kind": "future_cleanup_kind",
                        "opaque": {"do_not_drop": True},
                    }
                ],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(RuntimeMarkerFormatError, "schéma futur|cleanup_schema futur"):
                marker.start()

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_corrupt_runtime_marker_is_preserved_and_blocks_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = b'{not-json\\x00still-here'
            path.write_bytes(original)
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(RuntimeMarkerFormatError, "runtime.json illisible"):
                marker.start()

            self.assertEqual(path.read_bytes(), original)


    def test_schema3_requires_pending_cleanup_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            original = {
                "clean_shutdown": False,
                "cleanup_complete": False,
                "cleanup_schema": CLEANUP_SCHEMA_VERSION,
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            marker = RuntimeMarker()
            marker.path = path

            with self.assertRaisesRegex(
                RuntimeMarkerFormatError,
                "pending_cleanup absent",
            ):
                marker.start()

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_schema3_rejects_invalid_fade_metadata_without_rewrite(self):
        mutations = (
            ("created_at", "damaged"),
            ("created_at", float("inf")),
            ("attempts", "damaged"),
            ("attempts", True),
            ("attempts", -1),
            ("last_error", {"damaged": True}),
        )
        for field, invalid in mutations:
            with self.subTest(field=field, invalid=invalid), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "runtime.json"
                item = {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                    "helper_id": "abc123",
                    "source_uuid": "input-uuid",
                    "source_kind": "image_source",
                    "connection": {"host": "127.0.0.1", "port": 4455},
                    "filter_name": "[SSR] Layout Fade::abc123",
                    "filter_kind": "color_filter_v2",
                    "cleanup_action": "neutralize_disable",
                    "legacy": False,
                    "ambiguous": False,
                    "created_at": 1.0,
                    "attempts": 2,
                    "last_error": "",
                }
                item[field] = invalid
                original = {
                    "clean_shutdown": False,
                    "cleanup_complete": False,
                    "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                    "pending_cleanup": [item],
                }
                original_text = json.dumps(original)
                path.write_text(original_text, encoding="utf-8")
                marker = RuntimeMarker()
                marker.path = path

                with self.assertRaisesRegex(
                    RuntimeMarkerFormatError,
                    field,
                ):
                    marker.start()

                self.assertEqual(path.read_text(encoding="utf-8"), original_text)

    def test_schema2_fade_migration_writes_canonical_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "clean_shutdown": False,
                        "cleanup_complete": False,
                        "cleanup_schema": 2,
                        "pending_cleanup": [
                            {
                                "kind": "layout_fade",
                                "source": "[Webcam] Avatar",
                                "collection": "Collection A",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            marker = RuntimeMarker()
            marker.path = path

            marker.start()

            rewritten = json.loads(path.read_text(encoding="utf-8"))
            item = rewritten["pending_cleanup"][0]
            self.assertEqual(rewritten["cleanup_schema"], CLEANUP_SCHEMA_VERSION)
            self.assertTrue(item["legacy"])
            self.assertEqual(item["created_at"], 0.0)
            self.assertEqual(item["attempts"], 0)
            self.assertEqual(item["last_error"], "")

if __name__ == "__main__":
    unittest.main()
