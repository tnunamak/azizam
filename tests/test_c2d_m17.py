def test_compatibility_exports_follow_declared_operations(tmp_path):
    from gateway import local_capabilities as local
    import yaml
    path = tmp_path/'rows.yml'
    doc = yaml.safe_load(local._LOCAL_CAPABILITY_ROWS_PATH.read_text())
    doc['intent_exports'] = {'INTENT_NOVEL': 'speech.translation'}
    path.write_text(yaml.safe_dump(doc))
    assert local.load_intent_exports(path) == {'INTENT_NOVEL':'speech.translation'}
