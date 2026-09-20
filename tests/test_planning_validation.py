from __future__ import annotations

import unittest

from stream_state_router.obs.catalog import (
    OBSFilterRef,
    OBSInputRef,
    OBSResourceCatalog,
    OBSSceneItemRef,
)
from stream_state_router.planning import (
    DesiredProperty,
    DesiredState,
    ResourceKey,
    validate_desired_state,
    validate_resource,
)


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = OBSResourceCatalog(
            scene_collection="Midgar",
            scene_items=(
                OBSSceneItemRef(
                    root_scene="In Game",
                    container="In Game",
                    container_kind="scene",
                    path=("In Game",),
                    source_name="Chat",
                    source_kind="input",
                    occurrence=1,
                    enabled=True,
                ),
            ),
            inputs=(
                OBSInputRef(
                    name="Capture de jeu",
                    kind="game_capture",
                    settings={
                        "window": "Overwatch:Class:Overwatch.exe",
                        "rgb10a2_space": "2100pq",
                    },
                ),
            ),
            filters=(
                OBSFilterRef(
                    source_name="Avatar Dynamic",
                    name="Avatar FX",
                    kind="shader_filter",
                    enabled=False,
                    settings={"strength": 1.0},
                ),
            ),
        )

    def test_existing_resources_are_verified(self):
        keys = [
            ResourceKey.scene_item_visibility(
                collection="Midgar",
                container="In Game",
                source="Chat",
            ),
            ResourceKey.input_setting(
                "Capture de jeu",
                "window",
                collection="Midgar",
            ),
            ResourceKey.filter_enabled(
                "Avatar Dynamic",
                "Avatar FX",
                collection="Midgar",
            ),
            ResourceKey.filter_setting(
                "Avatar Dynamic",
                "Avatar FX",
                "strength",
                collection="Midgar",
            ),
        ]

        results = [validate_resource(self.catalog, key) for key in keys]

        self.assertTrue(all(result.status == "verified" for result in results))

    def test_missing_resource_is_confirmed_only_with_complete_catalog(self):
        key = ResourceKey.input_setting(
            "Missing",
            "file",
            collection="Midgar",
        )

        complete = validate_resource(self.catalog, key)
        partial = validate_resource(
            OBSResourceCatalog(
                scene_collection="Midgar",
                warnings=("GetInputList failed",),
            ),
            key,
        )

        self.assertEqual(complete.status, "missing")
        self.assertEqual(complete.code, "input_missing")
        self.assertEqual(partial.status, "unknown")
        self.assertEqual(partial.code, "input_missing_catalog_partial")

    def test_absent_plugin_setting_is_partial_not_declared_invalid(self):
        key = ResourceKey.filter_setting(
            "Avatar Dynamic",
            "Avatar FX",
            "dynamic_default",
            collection="Midgar",
        )

        result = validate_resource(self.catalog, key)

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.code, "filter_setting_not_observed")

    def test_settings_not_loaded_are_reported_as_partial(self):
        catalog = OBSResourceCatalog(
            scene_collection="Midgar",
            inputs=(OBSInputRef(name="Capture de jeu", kind="game_capture"),),
        )
        key = ResourceKey.input_setting(
            "Capture de jeu",
            "window",
            collection="Midgar",
        )

        result = validate_resource(catalog, key)

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.code, "input_settings_not_loaded")

    def test_scene_collection_mismatch_is_explicit(self):
        key = ResourceKey.filter_enabled(
            "Avatar Dynamic",
            "Avatar FX",
            collection="Other",
        )

        result = validate_resource(self.catalog, key)

        self.assertEqual(result.status, "missing")
        self.assertEqual(result.code, "scene_collection_mismatch")

    def test_desired_state_validation_keeps_provenance_and_delegates_layout(self):
        layout = ResourceKey.layout_profile()
        desired = DesiredState.build(
            [
                DesiredProperty.create(
                    layout,
                    "Dofus",
                    provenance="LayoutProfile Dofus",
                )
            ]
        )

        (result,) = validate_desired_state(desired, self.catalog)

        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.code, "layout_owned_by_layout_manager")
        self.assertEqual(result.provenance, ("LayoutProfile Dofus",))


if __name__ == "__main__":
    unittest.main()
