from __future__ import annotations

import json
import tempfile
import unittest
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
            self.assertEqual(data["pending_cleanup"], [fade])


if __name__ == "__main__":
    unittest.main()
