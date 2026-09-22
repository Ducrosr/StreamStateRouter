from __future__ import annotations

from dataclasses import replace
import unittest

from stream_state_router.obs.catalog import (
    InputRef,
    OBSResourceCatalog,
    SceneItemRef,
    SceneRef,
)
from stream_state_router.obs.observed import build_execution_bindings
from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    ObservedState,
    ObservedValue,
    PropertyKey,
    build_execution_plan,
)
from stream_state_router.services.declarative_execution import (
    DeclarativeExecutor,
    PreparedExecution,
)


class _PlanningStub:
    def __init__(self, catalog):
        self.catalog = catalog
        self.catalog_epoch = 7
        self.collection = catalog.collection
        self.generation = catalog.session_generation
        self.stale = False
        self.context_error = ""

    def catalog_status(self):
        return {
            "available": True,
            "stale": self.stale,
            "catalog_epoch": self.catalog_epoch,
        }

    def context_identity(self):
        if self.context_error:
            raise RuntimeError(self.context_error)
        return self.collection, self.generation


class _ExecutorClient:
    def __init__(self):
        self.session_generation = 1
        self.requests = []
        self.scene_uuid = "scene-1"
        self.scene_items = [
            {
                "sourceName": "Probe",
                "sourceUuid": "probe-1",
                "sceneItemId": 0,
            }
        ]
        self.scene_enabled = False
        self.input_uuid = "mic-1"
        self.input_muted = False
        self.input_volume_db = -12.0
        self.ignore_writes = False

    def send(self, request, data=None, *, expected_session_generation=None):
        if expected_session_generation != self.session_generation:
            raise RuntimeError("session mismatch")
        payload = dict(data or {})
        self.requests.append((request, payload))
        if request == "GetSceneList":
            return {
                "scenes": [
                    {
                        "sceneName": "Lab",
                        "sceneUuid": self.scene_uuid,
                    }
                ]
            }
        if request == "GetSceneItemList":
            return {"sceneItems": list(self.scene_items)}
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": self.scene_enabled}
        if request == "SetSceneItemEnabled":
            if not self.ignore_writes:
                self.scene_enabled = bool(payload["sceneItemEnabled"])
            return {}
        if request == "GetInputList":
            return {
                "inputs": [
                    {
                        "inputName": "Mic",
                        "inputUuid": self.input_uuid,
                    }
                ]
            }
        if request == "GetInputMute":
            return {"inputMuted": self.input_muted}
        if request == "SetInputMute":
            if not self.ignore_writes:
                self.input_muted = bool(payload["inputMuted"])
            return {}
        if request == "GetInputVolume":
            return {"inputVolumeDb": self.input_volume_db}
        if request == "SetInputVolume":
            if not self.ignore_writes:
                self.input_volume_db = float(payload["inputVolumeDb"])
            return {}
        raise AssertionError(request)


def _catalog():
    requests = frozenset(
        {
            "GetSceneList",
            "GetSceneItemList",
            "GetSceneItemEnabled",
            "SetSceneItemEnabled",
            "GetInputList",
            "GetInputMute",
            "SetInputMute",
            "GetInputVolume",
            "SetInputVolume",
        }
    )
    return OBSResourceCatalog(
        collection="Lab Collection",
        current_program_scene="Lab",
        current_program_scene_uuid="scene-1",
        canvas=(1920, 1080),
        scenes=(SceneRef("Lab", "scene-1", 0),),
        groups=(),
        scene_items=(
            SceneItemRef(
                collection="Lab Collection",
                root_scene="Lab",
                container="Lab",
                container_kind="scene",
                path=("Lab",),
                source="Probe",
                source_uuid="probe-1",
                source_kind="input",
                occurrence=0,
                scene_item_id=0,
                enabled=False,
            ),
        ),
        inputs=(InputRef("Mic", "wasapi_input_capture", "mic-1"),),
        transitions=(),
        available_requests=requests,
        session_generation=1,
    )


def _prepared(desired, observed):
    catalog = _catalog()
    plan = build_execution_plan(desired, ObservedState(observed))
    return PreparedExecution.create(
        desired=desired,
        plan=plan,
        collection=catalog.collection,
        session_generation=1,
        catalog_epoch=7,
        config_revision="cfg",
        dispatch_generation=2,
        resume_generation=3,
        bindings=build_execution_bindings(catalog, desired),
    )


class DeclarativeExecutorTests(unittest.TestCase):
    def test_input_mute_write_requires_readback_and_converges(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertTrue(result.converged)
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.steps[-1].status, "applied")
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputMute"),
            1,
        )
        self.assertGreaterEqual(
            [name for name, _data in client.requests].count("GetInputMute"),
            2,
        )

    def test_input_volume_write_requires_readback_and_converges(self):
        key = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, -6.25)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(-12.0)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertTrue(result.converged)
        self.assertEqual(result.status, "converged")
        self.assertEqual(result.steps[-1].status, "applied")
        self.assertAlmostEqual(result.steps[-1].ack_value, -6.25)
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputVolume"),
            1,
        )
        set_payload = next(
            data
            for name, data in client.requests
            if name == "SetInputVolume"
        )
        self.assertEqual(set_payload["inputUuid"], "mic-1")
        self.assertAlmostEqual(set_payload["inputVolumeDb"], -6.25)

    def test_input_volume_missing_capability_blocks_before_any_write(self):
        key = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, -6.0)])
        catalog = _catalog()
        catalog = replace(
            catalog,
            available_requests=frozenset(
                request
                for request in catalog.available_requests
                if request != "SetInputVolume"
            ),
        )
        plan = build_execution_plan(
            desired,
            ObservedState({key: ObservedValue.known_value(-12.0)}),
        )
        prepared = PreparedExecution.create(
            desired=desired,
            plan=plan,
            collection=catalog.collection,
            session_generation=catalog.session_generation,
            catalog_epoch=7,
            config_revision="cfg",
            dispatch_generation=2,
            resume_generation=3,
            bindings=build_execution_bindings(catalog, desired),
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(catalog))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.replan_required)
        self.assertFalse(
            any(name.startswith("Set") for name, _data in client.requests)
        )

    def test_mixed_mute_and_volume_plan_converges_serially(self):
        mute = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        volume = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(mute, True),
                DesiredAssignment.create(volume, -6.25),
            ]
        )
        prepared = _prepared(
            desired,
            {
                mute: ObservedValue.known_value(False),
                volume: ObservedValue.known_value(-12.0),
            },
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertTrue(result.converged)
        self.assertTrue(client.input_muted)
        self.assertAlmostEqual(client.input_volume_db, -6.25)
        self.assertEqual(
            [
                name
                for name, _data in client.requests
                if name in {"SetInputMute", "SetInputVolume"}
            ],
            ["SetInputMute", "SetInputVolume"],
        )
        by_key = {step.key: step for step in result.steps}
        self.assertEqual(by_key[mute].status, "applied")
        self.assertEqual(by_key[volume].status, "applied")

    def test_input_volume_float_roundtrip_tolerance_avoids_noop_write(self):
        key = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, -99.9389)]
        )
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(-99.93891906738281)},
        )
        client = _ExecutorClient()
        client.input_volume_db = -99.93891906738281
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertTrue(result.converged)
        self.assertEqual(result.steps[0].status, "already_converged")
        self.assertFalse(
            any(name == "SetInputVolume" for name, _data in client.requests)
        )

    def test_input_volume_difference_beyond_tolerance_is_written(self):
        key = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, -99.9389)]
        )
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(-99.9386)},
        )
        client = _ExecutorClient()
        client.input_volume_db = -99.9386
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertTrue(result.converged)
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputVolume"),
            1,
        )

    def test_input_volume_protocol_boundaries_are_executable(self):
        for target in (-100.0, 26.0):
            with self.subTest(target=target):
                key = PropertyKey.input_volume_db(
                    collection="Lab Collection",
                    input_name="Mic",
                )
                desired = DesiredState.build(
                    [DesiredAssignment.create(key, target)]
                )
                prepared = _prepared(
                    desired,
                    {key: ObservedValue.known_value(-12.0)},
                )
                client = _ExecutorClient()
                executor = DeclarativeExecutor(
                    client,
                    _PlanningStub(_catalog()),
                )

                result = executor.execute(
                    prepared,
                    validate_target=lambda: (True, ""),
                )

                self.assertTrue(result.converged)
                self.assertAlmostEqual(client.input_volume_db, target)

    def test_finite_volume_above_db_write_limit_can_reconverge(self):
        key = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, 26.0)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(26.0206)},
        )
        client = _ExecutorClient()
        client.input_volume_db = 26.0206
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertTrue(result.converged)
        self.assertAlmostEqual(client.input_volume_db, 26.0)
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputVolume"),
            1,
        )

    def test_input_volume_positive_set_requires_matching_readback(self):
        key = PropertyKey.input_volume_db(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, -6.0)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(-12.0)},
        )
        client = _ExecutorClient()
        client.ignore_writes = True
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertEqual(result.status, "divergent")
        self.assertTrue(result.replan_required)
        self.assertEqual(result.steps[-1].status, "divergent")
        self.assertAlmostEqual(result.steps[-1].ack_value, -12.0)
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputVolume"),
            1,
        )

    def test_converged_plan_sends_no_writes(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, False)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertTrue(result.converged)
        self.assertEqual(result.steps[0].status, "already_converged")
        self.assertFalse(any(name.startswith("Set") for name, _data in client.requests))

    def test_out_of_allowlist_property_blocks_entire_plan(self):
        key = PropertyKey.program_scene(
            collection="Lab Collection",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, "Other")])
        plan = build_execution_plan(
            desired,
            ObservedState({key: ObservedValue.known_value("Lab")}),
        )
        prepared = PreparedExecution.create(
            desired=desired,
            plan=plan,
            collection="Lab Collection",
            session_generation=1,
            catalog_epoch=7,
            config_revision="cfg",
            dispatch_generation=2,
            resume_generation=3,
            bindings=(),
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertEqual(result.status, "blocked")
        self.assertFalse(any(name.startswith("Set") for name, _data in client.requests))

    def test_wrong_session_before_execution_requires_replan(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        planning = _PlanningStub(_catalog())
        planning.generation = 2
        executor = DeclarativeExecutor(_ExecutorClient(), planning)

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)

    def test_input_recreated_under_same_name_requires_replan_without_write(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        client.input_uuid = "mic-recreated"
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertFalse(
            any(name == "SetInputMute" for name, _data in client.requests)
        )

    def test_catalog_epoch_change_requires_replan_without_write(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        planning = _PlanningStub(_catalog())
        planning.catalog_epoch += 1
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, planning)

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertFalse(
            any(name.startswith("Set") for name, _data in client.requests)
        )

    def test_scene_item_duplicate_added_requires_replan_without_write(self):
        key = PropertyKey.scene_item_visibility(
            collection="Lab Collection",
            container="Lab",
            source="Probe",
            occurrence=0,
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        client.scene_items.append(
            {
                "sourceName": "Probe",
                "sourceUuid": "probe-1",
                "sceneItemId": 9,
            }
        )
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertEqual(result.status, "replan_required")
        self.assertFalse(
            any(name == "SetSceneItemEnabled" for name, _data in client.requests)
        )

    def test_scene_item_id_zero_is_executable(self):
        key = PropertyKey.scene_item_visibility(
            collection="Lab Collection",
            container="Lab",
            source="Probe",
            occurrence=0,
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertTrue(result.converged)
        write = next(
            data
            for name, data in client.requests
            if name == "SetSceneItemEnabled"
        )
        self.assertEqual(write["sceneItemId"], 0)

    def test_target_revalidation_immediately_before_write_can_cancel_mutation(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))
        calls = 0

        def validate_target():
            nonlocal calls
            calls += 1
            return (calls < 3, "target changed")

        result = executor.execute(prepared, validate_target=validate_target)

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertFalse(any(name.startswith("Set") for name, _data in client.requests))

    def test_atomic_commit_rejects_runtime_change_without_write(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))
        commits = 0

        def commit_write(_write):
            nonlocal commits
            commits += 1
            return False, "runtime target changed at commit boundary"

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
            commit_write=commit_write,
        )

        self.assertEqual(commits, 1)
        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertFalse(any(name.startswith("Set") for name, _data in client.requests))
        self.assertEqual(result.steps[0].status, "not_run")

    def test_binding_drift_during_final_target_revalidation_prevents_write(self):
        key = PropertyKey.scene_item_visibility(
            collection="Lab Collection",
            container="Lab",
            source="Probe",
            occurrence=0,
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))
        validations = 0

        def validate_target():
            nonlocal validations
            validations += 1
            if validations == 3:
                client.scene_items.append(
                    {
                        "sourceName": "Probe",
                        "sourceUuid": "probe-1",
                        "sceneItemId": 9,
                    }
                )
            return True, ""

        result = executor.execute(
            prepared,
            validate_target=validate_target,
        )

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertFalse(
            any(name == "SetSceneItemEnabled" for name, _data in client.requests)
        )

    def test_blocked_second_operation_after_applied_write_requires_replan(self):
        mute = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        visibility = PropertyKey.scene_item_visibility(
            collection="Lab Collection",
            container="Lab",
            source="Probe",
            occurrence=0,
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(mute, True),
                DesiredAssignment.create(visibility, True),
            ]
        )
        prepared = _prepared(
            desired,
            {
                mute: ObservedValue.known_value(False),
                visibility: ObservedValue.known_value(False),
            },
        )
        client = _ExecutorClient()
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        def progress(steps):
            if any(step.key == mute and step.status == "applied" for step in steps):
                client.scene_items[0] = {
                    "sourceName": "Probe",
                    "sceneItemId": 0,
                }

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
            progress=progress,
        )

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.replan_required)
        by_key = {step.key: step for step in result.steps}
        self.assertEqual(by_key[mute].status, "applied")
        self.assertEqual(by_key[visibility].status, "not_run")
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputMute"),
            1,
        )
        self.assertEqual(
            [name for name, _data in client.requests].count("SetSceneItemEnabled"),
            0,
        )

    def test_session_change_between_operations_preserves_not_run_step(self):
        mute = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        visibility = PropertyKey.scene_item_visibility(
            collection="Lab Collection",
            container="Lab",
            source="Probe",
            occurrence=0,
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(mute, True),
                DesiredAssignment.create(visibility, True),
            ]
        )
        prepared = _prepared(
            desired,
            {
                mute: ObservedValue.known_value(False),
                visibility: ObservedValue.known_value(False),
            },
        )
        client = _ExecutorClient()
        planning = _PlanningStub(_catalog())
        executor = DeclarativeExecutor(client, planning)

        def progress(steps):
            if any(step.key == mute and step.status == "applied" for step in steps):
                planning.generation = 2

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
            progress=progress,
        )

        self.assertEqual(result.status, "replan_required")
        by_key = {step.key: step for step in result.steps}
        self.assertEqual(by_key[mute].status, "applied")
        self.assertEqual(by_key[visibility].status, "not_run")
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputMute"),
            1,
        )
        self.assertEqual(
            [name for name, _data in client.requests].count("SetSceneItemEnabled"),
            0,
        )

    def test_context_read_failure_after_write_preserves_partial_result(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        planning = _PlanningStub(_catalog())
        executor = DeclarativeExecutor(client, planning)

        def progress(steps):
            if any(
                step.key == key and step.code == "awaiting_readback"
                for step in steps
            ):
                planning.context_error = (
                    "OBS session changed while reading declarative context identity"
                )

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
            progress=progress,
        )

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].status, "unacknowledged")
        self.assertEqual(result.steps[0].code, "context_changed_after_write")
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputMute"),
            1,
        )

    def test_collection_change_after_matching_ack_is_not_reported_applied(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        planning = _PlanningStub(_catalog())
        original_send = client.send
        mute_reads = 0

        def send(request, data=None, *, expected_session_generation=None):
            nonlocal mute_reads
            response = original_send(
                request,
                data,
                expected_session_generation=expected_session_generation,
            )
            if request == "GetInputMute":
                mute_reads += 1
                if mute_reads == 4:
                    planning.collection = "Other Collection"
            return response

        client.send = send
        executor = DeclarativeExecutor(client, planning)

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertEqual(result.status, "replan_required")
        self.assertTrue(result.replan_required)
        self.assertEqual(result.steps[0].status, "unacknowledged")
        self.assertEqual(result.steps[0].code, "context_changed_after_write")
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputMute"),
            1,
        )

    def test_unknown_final_observation_requires_replan(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        original_send = client.send
        mute_reads = 0

        def send(request, data=None, *, expected_session_generation=None):
            nonlocal mute_reads
            if request == "GetInputMute":
                mute_reads += 1
                if mute_reads == 5:
                    if expected_session_generation != client.session_generation:
                        raise RuntimeError("session mismatch")
                    client.requests.append((request, dict(data or {})))
                    return {"inputMuted": "unknown"}
            return original_send(
                request,
                data,
                expected_session_generation=expected_session_generation,
            )

        client.send = send
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(
            prepared,
            validate_target=lambda: (True, ""),
        )

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.converged)
        self.assertTrue(result.replan_required)
        self.assertEqual(result.steps[0].status, "applied")
        self.assertTrue(
            any("final observation unavailable" in item for item in result.diagnostics)
        )

    def test_sensitive_setting_is_redacted_even_when_plan_is_not_executable(self):
        key = PropertyKey.input_setting(
            collection="Lab Collection",
            input_name="Browser",
            setting="url",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    "https://example.invalid/?token=secret",
                )
            ]
        )
        plan = build_execution_plan(
            desired,
            ObservedState(
                {
                    key: ObservedValue.known_value(
                        "https://old.invalid/?token=old-secret"
                    )
                }
            ),
        )
        prepared = PreparedExecution.create(
            desired=desired,
            plan=plan,
            collection="Lab Collection",
            session_generation=1,
            catalog_epoch=7,
            config_revision="cfg",
            dispatch_generation=2,
            resume_generation=3,
            bindings=(),
        )

        diagnostic = str(prepared.as_mapping())

        self.assertNotIn("token=secret", diagnostic)
        self.assertNotIn("old-secret", diagnostic)
        self.assertIn("<redacted>", diagnostic)

    def test_positive_set_response_requires_matching_readback(self):
        key = PropertyKey.input_mute(
            collection="Lab Collection",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        prepared = _prepared(
            desired,
            {key: ObservedValue.known_value(False)},
        )
        client = _ExecutorClient()
        client.ignore_writes = True
        executor = DeclarativeExecutor(client, _PlanningStub(_catalog()))

        result = executor.execute(prepared, validate_target=lambda: (True, ""))

        self.assertEqual(result.status, "divergent")
        self.assertFalse(result.converged)
        self.assertEqual(result.steps[-1].status, "divergent")
        self.assertEqual(
            [name for name, _data in client.requests].count("SetInputMute"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
