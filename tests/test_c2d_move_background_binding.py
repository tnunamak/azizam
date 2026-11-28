import proxy


def test_declared_sink_input_changes_without_code():
    graph = {'sink': {'class_type': 'CustomSink', 'inputs': {'pixels': ['raw', 0]}}, 'raw': {}, 'cut': {}}
    meta = {'background_removal': {'save_node': 'sink', 'save_input': 'pixels',
                                  'plain_image': ['raw', 0], 'transparent_image': ['cut', 1]}}
    assert proxy.set_workflow_background_removal(graph, meta, True)
    assert graph['sink']['inputs']['pixels'] == ['cut', 1]
    assert proxy.set_workflow_background_removal(graph, meta, False)
    assert graph['sink']['inputs']['pixels'] == ['raw', 0]
