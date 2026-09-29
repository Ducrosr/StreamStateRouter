from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from stream_state_router.services.recovery import CLEANUP_SCHEMA_VERSION, RuntimeMarker


class RuntimeMarkerTests(unittest.TestCase):
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

    def test_finalization_blocks_late_cleanup_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            marker = RuntimeMarker()
            marker.path = path
            marker.start()

            late_cleanup = {
                "kind": "layout_fade",
                "source": "[Webcam] Avatar",
                "collection": "Collection A",
            }

            # Hold the marker lock so the worker-like checkpoint is definitely
            # pending while finalization commits. RLock lets this thread call
            # finish() re-entrantly; once released, the late checkpoint must
            # observe finalized=True and leave the final marker untouched.
            with marker._write_lock:
                worker = threading.Thread(
                    target=marker.checkpoint_pending_cleanup,
                    args=((late_cleanup,),),
                )
                worker.start()
                marker.finish(
                    clean_shutdown=True,
                    cleanup_complete=True,
                    pending_cleanup=(),
                )

            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

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

    def test_invalid_schema3_layout_fade_identity_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
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
                ),
                encoding="utf-8",
            )
            marker = RuntimeMarker()
            marker.path = path

            marker.start()

            self.assertEqual(marker.previous_pending_cleanup, ())

if __name__ == "__main__":
    unittest.main()
