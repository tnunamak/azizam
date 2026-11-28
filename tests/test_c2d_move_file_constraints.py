from gateway.lifecycle_drivers import SystemdDriver


def test_constraints_precede_activation(tmp_path):
    model = tmp_path / 'model.weights'
    model.write_bytes(b'x')
    svc = {'lifecycle': {'driver': 'systemd', 'driver_config': {
        'file_constraints': {'model': {'absolute': True, 'exists': True, 'suffixes': ['.weights']},
                             'aux': {'absolute': True, 'exists': True}}}}}
    driver = SystemdDriver()
    assert driver.build_activation(svc, model=str(model), aux=None) is not None
    assert driver.build_activation(svc, model=str(model.with_suffix('.other')), aux=None) is None
    assert driver.build_activation(svc, model=str(model), aux=str(tmp_path / 'missing')) is None
    svc['lifecycle']['driver_config']['file_constraints']['model']['suffixes'] = ['.other']
    assert driver.build_activation(svc, model=str(model), aux=None) is None


def test_legacy_precheck_whitespace_and_unknown_selector(tmp_path):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'x')
    driver = SystemdDriver()
    svc = {'lifecycle': {'driver': 'systemd', 'driver_config': {'precheck': ' gguf_path '}}}
    assert driver.build_activation(svc, model=str(model), aux=None) is not None
    svc['lifecycle']['driver_config']['precheck'] = 'unknown'
    assert driver.build_activation(svc, model='no-local-file-required', aux=None) is not None
