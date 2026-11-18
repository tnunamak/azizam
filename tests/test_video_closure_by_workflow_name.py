import copy
import json
from dataclasses import replace
from unittest.mock import patch

import proxy
from test_video_remix_replace import RemixReplaceBase, SOURCE_ID, NS, PNG
from tests.support.config_snapshot import section_key_published
from tests.support.access import access_context
from gateway.workflow_projections import SnapshotRegistry
import base64


class VideoClosureByWorkflowNameTests(RemixReplaceBase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        portal = self.client.portal
        self.client = TestClient(proxy.app, client=("203.0.113.31", 1234))
        self.client.portal = portal

    def custom(self, intent, *, closed=True, missing=True):
        key = 'review-custom-video-' + intent.split('.')[-1]
        provider = proxy.GATEWAY_REGISTRY_PROVIDER
        snapshot = provider.current()
        original = snapshot.get('video.edit.scail-2-replace')
        contract = proxy.workflow_run.plain(original.contract)
        wid = intent + '.' + key
        contract['id'] = wid
        contract['intent'] = intent
        contract['bindings'][proxy.DERIVED._GRAPH_BINDING]['workflow'] = 'workflows/videos/' + key + '.json'
        entry = replace(original, id=wid, contract=contract,
                        facts=replace(original.facts, exposure='disabled' if closed else 'default'))
        snapshot = replace(snapshot, workflows={**snapshot.workflows, wid: entry})
        self.stack.enter_context(patch.object(provider, '_snapshot', snapshot))
        self.stack.enter_context(patch.object(proxy, 'WORKFLOW_SNAPSHOTS', provider))
        self.stack.enter_context(patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(snapshot)))
        workflows = copy.deepcopy(dict(proxy._video_workflow_table()))
        cfg = copy.deepcopy(workflows['video-scail-2-replace'])
        manifest = proxy._find_video_manifest('video-scail-2-replace', cfg)
        workflows[key] = cfg
        self.stack.enter_context(section_key_published('videos', 'workflows', workflows))
        self.stack.enter_context(section_key_published('videos', 'remix_workflow', key))
        self.stack.enter_context(patch.object(proxy, '_find_video_manifest', return_value=None if missing else manifest))
        return key, wid, provider, snapshot, entry

    def body(self, key):
        return {'model': key, 'prompt': 'a', 'extensions': {NS: {
            'reference_image': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}}}

    def test_closed_custom_generate_remix(self):
        key, *_ = self.custom('video.generate')
        response = self.client.post(f'/v1/videos/{SOURCE_ID}/remix', json=self.body(key))
        assert response.status_code == 409, (response.status_code, response.text, len(self.comfy.uploads), len(self.comfy.submitted))
        assert response.json()['error']['code'] == 'workflow_disabled'
        assert not self.comfy.uploads and not self.comfy.submitted

    def test_open_custom_edit_and_remix(self):
        key, *_ = self.custom('video.edit', closed=False, missing=False)
        for count, route in enumerate(('/v1/videos/edits', f'/v1/videos/{SOURCE_ID}/remix'), 1):
            body = self.body(key)
            if route.endswith('/edits'):
                body['video'] = {'id': SOURCE_ID}
            response = self.client.post(route, json=body)
            assert response.status_code == 200, (response.status_code, response.text)
            self.comfy.wait_for_graphs(count)
            assert len(self.comfy.submitted) == count

    def test_closed_edit_at_next_step(self):
        key, wid, provider, snapshot, entry = self.custom('video.edit', closed=False)
        # This is the real validation and policy path used by media-job submit.
        body = self.body(key)
        assert proxy._validate_video_media_job_body(body) == (key, None)
        auth = access_context()
        assert not proxy._video_job_policy_decision(auth, key, key).denied
        closed = replace(entry, facts=replace(entry.facts, exposure='disabled'))
        snapshot = replace(snapshot, workflows={**snapshot.workflows, wid: closed})
        with patch.object(provider, '_snapshot', snapshot), patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(snapshot)), patch.object(proxy, '_auth_context_from_image_job_payload', return_value=auth):
            assert proxy._closed_workflow_reason(wid)
            result = proxy._video_media_job_step_auth({'body': body}, {'workflow': key})
            assert isinstance(result, tuple), ('next step authorized', type(result).__name__)
            assert result[1]['error_code'] == 'workflow_disabled'

    def test_worker_stops_when_custom_edit_is_disabled_after_submit(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from gateway import media_jobs
        from test_video_media_job_integration import _StubJsonRequest
        from fastapi import Response

        key, wid, provider, snapshot, entry = self.custom('video.edit', closed=False)
        auth = access_context()
        body = self.body(key)
        polls = []
        async def status(*args, **kwargs):
            polls.append(args[0])
            return Response(json.dumps({'status': 'failed'}), media_type='application/json')
        async def journey():
            response = await proxy._submit_video_media_job(request=_StubJsonRequest(body), body=body)
            assert response.status_code == 202, (response.status_code, response.body)
            job_id = json.loads(response.body)['job_id']
            stored = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH)
            assert stored['workflow'] == key
            # Resume a submitted video after the operator closes its custom edit record.
            media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                metadata_patch={'upstream_video_id': SOURCE_ID}, path=media_jobs.DB_PATH)
            closed = replace(entry, facts=replace(entry.facts, exposure='disabled'))
            current = replace(snapshot, workflows={**snapshot.workflows, wid: closed})
            with patch.object(provider, '_snapshot', current), patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(current)):
                assert proxy._closed_workflow_reason(wid)
                await proxy._run_video_media_job(job_id)
            assert not polls, ('closed edit reached next worker step', polls)
            final = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH)
            assert final['status'] == media_jobs.STATUS_FAILED
            assert final['error_code'] == 'workflow_disabled'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media_jobs.init_db(root / 'jobs.db')
            with patch.object(media_jobs, 'DB_PATH', root / 'jobs.db'), patch.object(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', root / 'payloads'), patch.object(proxy, '_schedule_video_media_job', return_value=True), patch.object(proxy, '_auth_context_from_image_job_payload', return_value=auth), patch.object(proxy, '_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS', 0), patch.object(proxy, '_video_media_job_semaphore', asyncio.Semaphore(1)), patch.object(proxy, 'proxy_graph_video_status', side_effect=status):
                self.client.portal.call(journey)

    def test_same_stem_different_video_intents_are_not_name_clashes(self):
        from gateway.registry_snapshot import workflow_names
        from gateway.workflow_kernel import name_clashes
        from gateway.workflow_identity import find_workflow_record
        from gateway.workflow_projections import record_of
        snapshot = proxy.GATEWAY_REGISTRY_PROVIDER.current()
        original = snapshot.get('video.edit.scail-2-replace')
        entries = []
        for intent in ('video.generate', 'video.edit'):
            contract = proxy.workflow_run.plain(original.contract)
            wid = intent + '.same-custom-name'
            contract.update(id=wid, intent=intent, aliases=[])
            contract['bindings'][proxy.DERIVED._GRAPH_BINDING]['workflow'] = 'workflows/videos/same-custom-name.json'
            entries.append(replace(original, id=wid, contract=contract))
        assert not name_clashes([pair for entry in entries for pair in workflow_names(entry)])
        records = [record_of(entry) for entry in entries]
        for intent in ('video.generate', 'video.edit'):
            assert find_workflow_record(records, intent, 'same-custom-name').id == intent + '.same-custom-name'

    def ambiguous(self, *, closed):
        key, wid, provider, snapshot, entry = self.custom('video.generate', closed=False, missing=False)
        contract = proxy.workflow_run.plain(entry.contract)
        edit_id = 'video.edit.other-custom-video'
        contract.update(id=edit_id, intent='video.edit', aliases=[])
        edit = replace(entry, id=edit_id, contract=contract,
                       facts=replace(entry.facts, exposure='disabled' if closed else 'default'))
        snapshot = replace(snapshot, workflows={**snapshot.workflows, edit_id: edit})
        self.stack.enter_context(patch.object(provider, '_snapshot', snapshot))
        self.stack.enter_context(patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(snapshot)))
        return key

    def test_ambiguous_stem_with_closed_record_is_refused(self):
        key = self.ambiguous(closed=True)
        registry = proxy.GATEWAY_WORKFLOW_REGISTRY
        records = registry.records()
        for ordered in (records, list(reversed(records))):
            with patch.object(registry, 'records', return_value=ordered), \
                    patch.object(proxy, '_find_video_manifest', return_value=None):
                decision = proxy._video_job_policy_decision(access_context(), key, key)
            assert decision.denied and decision.code == 'workflow_disabled'
        assert not self.comfy.uploads and not self.comfy.submitted

    def test_shipped_mapping_wins_over_a_closed_ambiguous_stem(self):
        key = self.ambiguous(closed=True)
        mapped = 'video.generate.' + key
        with patch.object(proxy, 'workflow_id_for_video_key', return_value=mapped):
            decision = proxy._video_job_policy_decision(access_context(), key, key)
        assert not decision.denied

    def test_ambiguous_stem_with_open_records_runs(self):
        key = self.ambiguous(closed=False)
        with patch.object(proxy, '_find_video_manifest', return_value=None):
            ids = proxy._video_closure_workflow_ids(key)
            assert len(ids) == 2
            assert not proxy._video_job_policy_decision(access_context(), key, key).denied
        response = self.client.post(f'/v1/videos/{SOURCE_ID}/remix', json=self.body(key))
        assert response.status_code == 200, response.text
        self.comfy.wait_for_graphs(1)
        assert len(self.comfy.submitted) == 1

    def test_unrelated_closed_suffix_does_not_block_open_binding(self):
        key, wid, provider, snapshot, entry = self.custom('video.generate', closed=False, missing=False)
        contract = proxy.workflow_run.plain(entry.contract)
        other_id = 'video.generate.' + key
        # Give the selected record a distinct id: it is selected by its binding stem.
        selected_id = 'video.generate.selected-open-record'
        selected_contract = copy.deepcopy(contract)
        selected_contract['id'] = selected_id
        selected_contract['aliases'] = []
        selected = replace(entry, id=selected_id, contract=selected_contract)
        contract['id'] = other_id
        contract['aliases'] = []
        contract['bindings'][proxy.DERIVED._GRAPH_BINDING]['workflow'] = 'workflows/videos/unrelated-closed-graph.json'
        unrelated = replace(entry, id=other_id, contract=contract, facts=replace(entry.facts, exposure='disabled'))
        workflows = {k: v for k, v in snapshot.workflows.items() if k != wid}
        workflows[selected_id] = selected
        before = replace(snapshot, workflows=workflows)
        with patch.object(provider, '_snapshot', before), patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(before)):
            response = self.client.post('/v1/videos/edits', json={**self.body(key), 'video': {'id': SOURCE_ID}})
            assert response.status_code == 200, response.text
            self.comfy.wait_for_graphs(1)
        after = replace(before, workflows={**workflows, other_id: unrelated})
        from gateway.registry_snapshot import workflow_names
        from gateway.workflow_kernel import name_clashes
        assert not name_clashes([pair for item in (selected, unrelated) for pair in workflow_names(item)])
        from gateway.workflow_identity import find_workflow_record
        assert find_workflow_record(SnapshotRegistry(after).records(), 'video.generate', key).id == selected_id
        with patch.object(provider, '_snapshot', after), patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(after)):
            with patch.object(proxy, '_find_video_manifest', return_value=None):
                assert proxy._video_closure_workflow_ids(key) == (selected_id,)
            response = self.client.post('/v1/videos/edits', json={**self.body(key), 'video': {'id': SOURCE_ID}})
        assert response.status_code == 200, (response.status_code, response.text)
        self.comfy.wait_for_graphs(2)
        assert len(self.comfy.submitted) == 2


    def test_manifest_identity_survives_config_removal_before_next_poll(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from gateway import media_jobs
        from test_video_media_job_integration import _StubJsonRequest
        from fastapi import Response

        key, wid, provider, snapshot, entry = self.custom('video.edit', closed=False)
        contract = proxy.workflow_run.plain(entry.contract)
        contract['bindings'][proxy.DERIVED._GRAPH_BINDING]['workflow'] = 'workflows/videos/different-artifact.json'
        entry = replace(entry, contract=contract)
        snapshot = replace(snapshot, workflows={**snapshot.workflows, wid: entry})
        saved_key = 'operator-choice'
        body = self.body(saved_key)
        auth = access_context()
        polls = []

        async def status(*args, **kwargs):
            polls.append(args[0])
            return Response(json.dumps({'status': 'failed'}), media_type='application/json')

        async def journey():
            with patch.object(proxy, '_video_workflow_table', return_value={saved_key: {'manifest': 'custom.yml'}}), \
                    patch.object(proxy, '_find_video_manifest', return_value=contract), \
                    patch.object(proxy, '_video_job_envelope', return_value=None):
                response = await proxy._submit_video_media_job(request=_StubJsonRequest(body), body=body)
            assert response.status_code == 202, response.body
            job_id = json.loads(response.body)['job_id']
            accepted = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH)
            media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                metadata_patch={'upstream_video_id': SOURCE_ID}, path=media_jobs.DB_PATH)
            closed = replace(entry, facts=replace(entry.facts, exposure='disabled'))
            current = replace(snapshot, workflows={**snapshot.workflows, wid: closed})
            with patch.object(provider, '_snapshot', current), \
                    patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(current)), \
                    patch.object(proxy, '_video_workflow_table', return_value={}):
                await proxy._run_video_media_job(job_id)
            assert not polls, ('closed manifest-only job reached status poll', polls)
            final = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH)
            assert final['status'] == media_jobs.STATUS_FAILED
            assert final['error_code'] == 'workflow_disabled'
            assert accepted['metadata']['accepted_workflow_ids'] == [wid]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media_jobs.init_db(root / 'jobs.db')
            with patch.object(provider, '_snapshot', snapshot), \
                    patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(snapshot)), \
                    patch.object(media_jobs, 'DB_PATH', root / 'jobs.db'), \
                    patch.object(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', root / 'payloads'), \
                    patch.object(proxy, '_schedule_video_media_job', return_value=True), \
                    patch.object(proxy, '_auth_context_from_image_job_payload', return_value=auth), \
                    patch.object(proxy, '_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS', 0), \
                    patch.object(proxy, '_video_media_job_semaphore', asyncio.Semaphore(1)), \
                    patch.object(proxy, 'proxy_graph_video_status', side_effect=status):
                self.client.portal.call(journey)

    def test_legacy_job_without_saved_ids_still_checks_current_key(self):
        key, *_ = self.custom('video.edit', closed=True)
        with patch.object(proxy, '_auth_context_from_image_job_payload', return_value=access_context()):
            result = proxy._video_media_job_step_auth({'body': self.body(key)}, {'workflow': key, 'metadata': {}})
        assert isinstance(result, tuple) and result[1]['error_code'] == 'workflow_disabled'

    def test_configured_manifest_id_wins_over_closed_matching_stem(self):
        key = self.ambiguous(closed=True)
        assert proxy._video_closure_workflow_ids(key) == ('video.edit.scail-2-replace',)
        decision = proxy._video_job_policy_decision(access_context(), key, key)
        assert not decision.denied

    def test_legacy_job_without_saved_or_resolvable_ids_keeps_running(self):
        with patch.object(proxy, '_auth_context_from_image_job_payload', return_value=access_context()), \
                patch.object(proxy, '_video_workflow_table', return_value={}):
            result = proxy._video_media_job_step_auth({'body': self.body('removed-key')},
                                                      {'workflow': 'removed-key', 'metadata': {}})
        assert isinstance(result, proxy.AuthContext)

    def test_later_step_checks_current_identity_alongside_saved_identity(self):
        key, wid, provider, snapshot, entry = self.custom('video.edit', closed=False)
        new_id = 'video.generate.replaced-workflow'
        contract = proxy.workflow_run.plain(entry.contract)
        contract.update(id=new_id, intent='video.generate')
        closed = replace(entry, id=new_id, contract=contract, facts=replace(entry.facts, exposure='disabled'))
        snapshot = replace(snapshot, workflows={**snapshot.workflows, new_id: closed})
        job = {'workflow': key, 'metadata': {'accepted_workflow_ids': [wid]}}
        with patch.object(provider, '_snapshot', snapshot), \
                patch.object(proxy, 'GATEWAY_WORKFLOW_REGISTRY', SnapshotRegistry(snapshot)), \
                patch.object(proxy, '_find_video_manifest', return_value={'id': new_id}), \
                patch.object(proxy, '_auth_context_from_image_job_payload', return_value=access_context()):
            result = proxy._video_media_job_step_auth({'body': self.body(key)}, job)
        assert isinstance(result, tuple) and result[1]['error_code'] == 'workflow_disabled'
