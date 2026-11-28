def test_service_comparison_defaults_are_release_data():
    from gateway.engine_record_defaults import release_defaults
    assert release_defaults()["service"]["base_path"] == "/v1"
    assert release_defaults()["service"]["timeout"] == 600
