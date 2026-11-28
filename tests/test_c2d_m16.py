def test_status_capabilities_ignore_node_classes(tmp_path, monkeypatch):
    import status_endpoint as status
    from gateway import workflow_projections as projections
    path = tmp_path/'graph.json'
    path.write_text('{"1":{"class_type":"NovelPictureInput","inputs":{}}}')
    facts = {'supports_i2v':True,'supports_audio':True,'two_stage':False}
    contract = {'bindings': {'graph': {'workflow': str(path)}}, 'annotations': {'azizam.dev/status-capabilities': facts}}
    original = projections.graph_status_capabilities
    assert original(path, {'sample': contract}) == facts
    monkeypatch.setattr(projections, 'graph_status_capabilities', lambda path, contracts: original(path, {'sample':contract}))
    result = status._read_workflow_caps(path)
    assert all(result[k] == v for k,v in facts.items())


def test_release_status_declarations_preserve_graph_promises():
    import json
    from pathlib import Path
    from gateway.discovery import bundled_workflow_manifests
    from gateway.workflow_projections import graph_status_capabilities
    root = Path(__file__).resolve().parents[1]
    contracts = bundled_workflow_manifests()
    checked = 0
    for contract in contracts.values():
        for binding in contract.get('bindings', {}).values():
            if not isinstance(binding, dict) or not binding.get('workflow'):
                continue
            path = root/binding['workflow']
            if not path.is_file():
                continue
            nodes = [node for node in json.loads(path.read_text()).values() if isinstance(node,dict)]
            old = {
                'supports_i2v':any(node.get('class_type') == 'LoadImage' or 'image_node' in (node.get('_meta') or {}) for node in nodes),
                'supports_audio':any(node.get('class_type') == 'LTXVAudioVAEDecode' for node in nodes),
                'two_stage':any(node.get('class_type') == 'LTXVCropGuides' for node in nodes),
            }
            assert graph_status_capabilities(path, contracts) == old
            checked += 1
    assert checked >= 18
