import copy
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('old,new,refusal', [
    ({}, {'timeout':600}, 'Only the owner can change service settings'),
    # Empty credential removal passes the legacy settings comparison, then the
    # unchanged credential-binding guard refuses it for its own reason.
    ({'api_key_env':''}, {}, 'Only the owner can attach a key to a service'),
    ({'base_path':''}, {'base_path':'/v1'}, None),
])
def test_non_owner_comparison_preserves_legacy_policy(old,new,refusal):
    from gateway.admin_runtime import AdminSQLiteStore, AdminContext
    from gateway import service_registry
    before = service_registry.normalize_service_registry({'services':{'sample':{'config':old}}})
    after = copy.deepcopy(before)
    after['services']['sample']['config'] = new
    store = SimpleNamespace(get_service_registry=lambda:before, _refuse_blocked_native_hosts=lambda *a:None)
    actor = AdminContext(subject='admin',roles=frozenset({'admin'}))
    if refusal:
        with pytest.raises(PermissionError,match=refusal):
            AdminSQLiteStore._settle_credential_bindings(store,after,actor)
    else:
        AdminSQLiteStore._settle_credential_bindings(store,after,actor)


def test_release_only_policies_are_not_owner_sections():
    from gateway import config_runtime
    sections = config_runtime.load_sections()
    for name in ('adapter_upload_policy','adapter_discovery','workflow_retention'):
        assert name not in sections
        assert (config_runtime.DEFAULTS_DIR.parent/(name+'.yml')).is_file()


def test_i2v_example_requires_image_input():
    import firehose_render
    rows=[{'id':'text-only','intent':'video.generate','input_modalities':['text']},
          {'id':'picture-model','intent':'video.generate','input_modalities':['text','image']}]
    html=firehose_render._render_quick_test(rows)
    snippet=html.split('image-to-video')[1]
    assert 'picture-model' in snippet
    assert 'text-only' not in snippet


def test_quick_tests_validated_at_catalog_load(tmp_path):
    from gateway import models_catalog as catalog
    import yaml
    doc=yaml.safe_load(catalog.MODELS_CATALOG_OPERATIONS_PATH.read_text())
    doc['quick_tests'][0]['template']='missing model placeholder'
    path=tmp_path/'operations.yml';path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError,match='quick_tests'):
        catalog.load_models_catalog_operations(path,catalog_path=catalog.MODELS_CATALOG_OPERATIONS_PATH.with_name('operations.yml'))


def test_tts_stream_failure_preserves_published_text():
    from gateway.gateway_messages import GATEWAY_MESSAGES
    assert GATEWAY_MESSAGES['tts_stream_failed']=='TTS engine failed before sending audio (likely GPU0 memory pressure)'


def test_status_contract_copy_cached_by_signature(monkeypatch,tmp_path):
    import status_endpoint as status
    from gateway import discovery
    calls=[]
    monkeypatch.setattr(discovery,'bundled_workflow_manifests',lambda path: calls.append(path) or {})
    status._status_workflow_contracts.cache_clear()
    assert status._status_workflow_contracts(str(tmp_path), ()) == {}
    assert status._status_workflow_contracts(str(tmp_path), ()) == {}
    assert len(calls)==1
    status._status_workflow_contracts(str(tmp_path), (('changed',1,1),))
    assert len(calls)==2


def test_status_words_remove_retired_graph_rules():
    from gateway.status_words import words
    assert not {'audio_decode_class','two_stage_class'} & words('images').keys()


def test_intent_exports_have_one_public_name_source():
    from gateway import local_capabilities as local
    assert {name for name in local.__all__ if name.startswith('INTENT_')} == set(local.load_intent_exports())
