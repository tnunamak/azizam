"""The S4-S8 interface file, the shared kernel and the fakes behave as the design says
(docs/design/CODE_TO_DATA_S4_S8.md, sections 3 and 5)."""

import asyncio
import hashlib
import unittest

from gateway.job_envelope import canonical_bytes
from gateway.workflow_kernel import (
    canonical_token, execution_contract, execution_digest, generated_names, name_clashes, route_key, run_identity,
    selector_matches, selector_problems,
)
from gateway.workflow_runtime_types import (
    ID_CLASH, IDEMPOTENCY_CONFLICT, Actor, FrontendRow, JobNotFound, MediaRef, NameOwner, RegistryConflict,
    RegistryRefused, ResultUnavailable, RouteSpec, RunRefused, RunRequest, WORKFLOW_EXISTS,
)
from tests.workflow_runtime_fakes import FACTS, FakeJobService, FakeRunCore, FakeStore, entry

DOC = {"schema_version": 2, "id": "poster.generate.demo", "version": "1", "intent": "poster.generate",
       "inputs": {"photo": {"type": "asset", "accepts": ["image/*"], "annotations": {"example.org/n": 1}}},
       "outputs": {"poster": {"media_type": "image/png"}}, "bindings": {"fake": {}}}
ME = Actor("owner", "o1")
ROW = FrontendRow("poster-api", "json", (RouteSpec(("POST",), "/v1/posters"),), {"ids": ["poster.generate.demo"]},
                  "job", "workflow")


class KernelRules(unittest.TestCase):
    def test_one_token_for_names_that_differ_only_in_case_and_punctuation(self):
        self.assertEqual(canonical_token(" A.b-C "), "a_b_c")
        self.assertEqual(canonical_token("a b_c"), canonical_token("A.B.C"))
        self.assertEqual(generated_names("x.y-z"), {"operation": "run_x_y_z", "component": "x_y_z"})
        self.assertEqual(route_key("post", "/V1/Images/"), "POST /v1/images")

    def test_two_owners_of_one_token_clash_and_one_owner_never_clashes_with_itself(self):
        pairs = [("a-b", NameOwner("workflow", "a-b")), ("a.b", NameOwner("workflow", "a.b"))]
        clash = name_clashes(pairs)
        self.assertEqual([p.code for p in clash], [ID_CLASH])
        self.assertIn("a-b", clash[0].message)
        self.assertEqual(name_clashes([("w", NameOwner("workflow", "w")), ("w", NameOwner("component", "w"))]), ())

    def test_a_workflow_id_clashes_with_a_model_alias_but_not_with_a_route(self):
        self.assertTrue(name_clashes([("Fast", NameOwner("model_alias", "m")), ("fast", NameOwner("workflow", "fast"))]))
        self.assertEqual(name_clashes([("fast", NameOwner("workflow", "fast")),
                                       (route_key("POST", "/fast"), NameOwner("route", "r"))]), ())

    def test_an_alias_and_its_workflow_are_one_owner_but_an_alias_of_another_clashes(self):
        self.assertEqual(name_clashes([("a.b", NameOwner("workflow", "a.b")),
                                       ("A-B", NameOwner("workflow_alias", "a.b"))]), ())
        self.assertEqual(name_clashes([("m1", NameOwner("model", "m1")), ("M-1", NameOwner("model_alias", "m1"))]), ())
        self.assertTrue(name_clashes([("a.b", NameOwner("workflow", "a.b")), ("a-b", NameOwner("workflow_alias", "c"))]))

    def test_output_schema_annotations_are_stripped_but_values_named_annotations_are_data(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer", "annotations": {"example.org/u": "s"}}}}
        out = {**DOC, "outputs": {"r": {"media_type": "application/json", "schema": schema}}}
        noted = {**out, "outputs": {"r": {"media_type": "application/json", "annotations": {"example.org/a": 1},
                                          "schema": {**schema, "annotations": {"example.org/b": 2}}}}}
        self.assertEqual(execution_digest(out), execution_digest(noted))
        with_default = {**DOC, "inputs": {"o": {"type": "object", "default": {"annotations": 1}}}}
        self.assertIn("annotations", execution_contract(with_default)["inputs"]["o"]["default"])

    def test_the_execution_digest_is_the_digest_of_the_envelope_bytes(self):
        doc = {**DOC, "title": "Café ☕"}
        body = canonical_bytes(execution_contract(doc))
        self.assertEqual(execution_digest(doc), "sha256:" + hashlib.sha256(body).hexdigest())

    def test_run_identity_ignores_spool_keys_and_filenames_but_not_bytes(self):
        a = RunRequest("w", {"t": "x"}, media={"photo": (MediaRef("sha256:1", "image/png", 3, "a.png", spool_key="s1"),)})
        b = RunRequest("w", {"t": "x"}, media={"photo": (MediaRef("sha256:1", "image/png", 3, "b.png", spool_key="s2"),)},
                       wait_s=30, frontend_id="other")
        c = RunRequest("w", {"t": "x"}, media={"photo": (MediaRef("sha256:2", "image/png", 3, spool_key="s1"),)})
        self.assertEqual(run_identity("o", "m", a), run_identity("o", "m", b))
        self.assertNotEqual(run_identity("o", "m", a), run_identity("o", "m", c))
        self.assertNotEqual(run_identity("o", "m", a), run_identity("o", "m2", a))

    def test_annotations_never_change_the_execution_digest(self):
        plain = {**DOC, "inputs": {"photo": {"type": "asset", "accepts": ["image/*"]}}}
        noted = {**DOC, "annotations": {"example.org/why": "x"}}
        self.assertEqual(execution_digest(plain), execution_digest(noted))
        self.assertNotEqual(execution_digest(DOC), execution_digest({**DOC, "version": "2"}))

    def test_the_closed_selector_schema(self):
        self.assertEqual(selector_problems({"ids": ["a"]}), ())
        self.assertTrue(selector_problems({"ids": ["a"], "match": {"inputs": {}}}))
        self.assertTrue(selector_problems({"match": {"intent": "poster.generate"}}))
        by_media = {"match": {"inputs": {"photo": {"media_type": "image/png"}}, "outputs": {"kind": "assets"}}}
        self.assertEqual(selector_problems(by_media), ())
        self.assertTrue(selector_matches(by_media, DOC["id"], DOC))
        self.assertFalse(selector_matches({"match": {"outputs": {"kind": "json"}}}, DOC["id"], DOC))
        self.assertFalse(selector_matches({"match": {"inputs": {"photo": {"media_type": "audio/*"}}}}, DOC["id"], DOC))

    def test_the_selector_validator_accepts_only_selectors_the_matcher_can_decide(self):
        refused = [{"match": {"inputs": {"photo": {"media_type": 7}}}}, {"match": {"inputs": {}}},
                   {"match": {"outputs": {}}}, {"match": {"inputs": {"photo": {"type": "png"}}}},
                   {"match": {"outputs": {"media_type": "image"}}}, {"ids": []}, {"ids": ["a"], "extra": 1}]
        for selector in refused:
            with self.subTest(selector=selector):
                self.assertTrue(selector_problems(selector))
        for selector in ({"match": {"inputs": {"photo": {"type": "asset"}}}}, {"match": {"outputs": {"kind": "json"}}}):
            self.assertEqual(selector_problems(selector), ())
            self.assertIsInstance(selector_matches(selector, DOC["id"], DOC), bool)


class FakesKeepTheContracts(unittest.TestCase):
    def test_compare_and_swap_refuses_a_stale_revision_and_create_refuses_an_existing_id(self):
        store = FakeStore({})
        first = store.put_workflow(DOC, if_match=None, actor=ME, facts=FACTS)
        self.assertEqual(first.revision, 2)
        with self.assertRaises(RegistryConflict) as exists:
            store.put_workflow(DOC, if_match=None, actor=ME)
        self.assertEqual(exists.exception.code, WORKFLOW_EXISTS)
        with self.assertRaises(RegistryConflict) as stale:
            store.set_state(DOC["id"], "disabled", if_match=1, actor=ME)
        self.assertEqual(stale.exception.current_revision, 2)

    def test_missing_facts_stay_missing_and_an_edit_keeps_state_published_and_incarnation(self):
        store = FakeStore({})
        store.put_workflow(DOC, if_match=None, actor=ME)
        made = store.snapshot().get(DOC["id"])
        self.assertTrue(made.needs_configuration)
        self.assertFalse(made.published)
        store.set_state(DOC["id"], "disabled", if_match=2, actor=ME)
        store.put_workflow({**DOC, "version": "2"}, if_match=3, actor=ME, facts=FACTS)
        edited = store.snapshot().get(DOC["id"])
        self.assertEqual((edited.state, edited.incarnation), ("disabled", made.incarnation))
        store.put_workflow({**DOC, "version": "3"}, if_match=4, actor=ME, facts=None)
        self.assertTrue(store.snapshot().get(DOC["id"]).needs_configuration)

    def test_delete_and_create_again_is_a_new_incarnation(self):
        store = FakeStore({})
        store.put_workflow(DOC, if_match=None, actor=ME)
        before = store.snapshot().get(DOC["id"]).incarnation
        store.delete_workflow(DOC["id"], if_match=2, actor=ME)
        self.assertIsNone(store.snapshot().get(DOC["id"]))
        store.put_workflow(DOC, if_match=None, actor=ME)
        self.assertNotEqual(store.snapshot().get(DOC["id"]).incarnation, before)

    def test_restore_by_record_revision_tells_annotation_only_saves_apart(self):
        store = FakeStore({})
        store.put_workflow(DOC, if_match=None, actor=ME)
        store.put_workflow({**DOC, "annotations": {"example.org/v": "b"}}, if_match=2, actor=ME)
        first, second = store.history(DOC["id"])[::-1]
        self.assertEqual(first.contract_digest, second.contract_digest)
        self.assertNotEqual(first.record_digest, second.record_digest)
        commit = store.restore(DOC["id"], first.record_revision, if_match=3, actor=ME)
        self.assertEqual((commit.revision, commit.record_digest), (4, first.record_digest))

    def test_a_frontend_or_name_change_changes_the_snapshot_digest_and_views_are_read_only(self):
        a = FakeStore({DOC["id"]: entry(DOC)}).snapshot()
        b = FakeStore({DOC["id"]: entry(DOC)}, models=(("m", NameOwner("model", "m")),)).snapshot()
        c = FakeStore({DOC["id"]: entry(DOC)}, frontends={ROW.id: ROW}).snapshot()
        self.assertEqual(len({a.digest, b.digest, c.digest}), 3)
        with self.assertRaises(TypeError):
            a.get(DOC["id"]).contract["id"] = "other"

    def test_names_are_looked_up_by_token_and_hold_frontends_and_routes(self):
        snap = FakeStore({DOC["id"]: entry(DOC)}, frontends={ROW.id: ROW}).snapshot()
        self.assertEqual(snap.names.owner("workflow", "Poster-Generate.DEMO"), NameOwner("workflow", DOC["id"]))
        self.assertEqual(snap.names.owner("frontend", "poster_api"), NameOwner("frontend", "poster-api"))
        self.assertEqual(snap.names.owner("route", route_key("post", "/V1/Posters/")), NameOwner("route", "poster-api"))

    def test_a_save_whose_id_takes_a_model_alias_or_frontend_token_is_refused(self):
        store = FakeStore({}, models=(("Poster.Generate-Demo", NameOwner("model_alias", "m")),))
        with self.assertRaises(RegistryRefused) as refused:
            store.put_workflow(DOC, if_match=None, actor=ME)
        self.assertEqual(refused.exception.problems[0].code, ID_CLASH)
        store = FakeStore({}, frontends={ROW.id: ROW})
        with self.assertRaises(RegistryRefused):
            store.put_workflow({**DOC, "id": "poster.api"}, if_match=None, actor=ME)
        store.put_workflow(DOC, if_match=None, actor=ME)
        store.put_workflow({**DOC, "version": "2"}, if_match=2, actor=ME)     # its own names are not a clash

    def test_a_failing_listener_does_not_stop_the_swap_or_the_next_listener(self):
        store = FakeStore({DOC["id"]: entry(DOC)})
        seen = []
        store.subscribe(lambda old, new: 1 / 0)
        stop = store.subscribe(lambda old, new: seen.append(new.revision))
        with self.assertLogs("tests.workflow_runtime_fakes", "ERROR"):
            store.set_published(DOC["id"], False, if_match=1, actor=ME)
        stop()
        store.set_published(DOC["id"], True, if_match=2, actor=ME)
        self.assertEqual((seen, store.snapshot().revision), ([2], 3))

    def test_the_fake_core_replays_by_identity_refuses_a_changed_request_and_the_job_service_cancels(self):
        store = FakeStore({DOC["id"]: entry(DOC)})
        core = FakeRunCore()
        jobs = FakeJobService(core)
        photo = MediaRef("sha256:1", "image/png", 3, spool_key="s1")
        req = RunRequest(DOC["id"], {"text": "a"}, media={"photo": (photo,)}, idempotency_key="k1")
        restaged = RunRequest(DOC["id"], {"text": "a"}, idempotency_key="k1",
                              media={"photo": (MediaRef("sha256:1", "image/png", 3, spool_key="s9"),)})
        first = asyncio.run(core.submit_run(req, "o", snapshot=store.snapshot()))
        again = asyncio.run(core.submit_run(restaged, "o", snapshot=store.snapshot()))
        changed = asyncio.run(core.submit_run(RunRequest(DOC["id"], {"text": "b"}, idempotency_key="k1"), "o",
                                              snapshot=store.snapshot()))
        self.assertEqual((first.disposition, again.disposition, again.job_id), ("new", "replay", first.job_id))
        self.assertEqual((changed.http_status, changed.code), (409, IDEMPOTENCY_CONFLICT))
        self.assertEqual(jobs.cancel(first.job_id, "o").status, "canceled")

    def test_a_terminal_job_stays_terminal_and_results_are_read_by_reference(self):
        store = FakeStore({DOC["id"]: entry(DOC)})
        core = FakeRunCore()
        jobs = FakeJobService(core)
        canceled = asyncio.run(core.submit_run(RunRequest(DOC["id"], {}), "o", snapshot=store.snapshot()))
        jobs.cancel(canceled.job_id, "o")
        asyncio.run(core.execute_run(canceled.job_id))
        self.assertEqual(jobs.get(canceled.job_id, "o").status, "canceled")
        done = asyncio.run(core.submit_run(RunRequest(DOC["id"], {"n": 1}), "o", snapshot=store.snapshot()))
        asyncio.run(core.execute_run(done.job_id))
        ref = jobs.get(done.job_id, "o").results[0]
        self.assertFalse(hasattr(ref, "value"))
        self.assertEqual(jobs.open_result(done.job_id, ref, "o").value, {"ok": True})
        with self.assertRaises(ResultUnavailable):
            jobs.open_result(canceled.job_id, ref, "o")
        with self.assertRaises(JobNotFound):
            asyncio.run(jobs.wait("missing", "o", 1))


if __name__ == "__main__":
    unittest.main()
