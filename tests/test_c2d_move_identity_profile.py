from gateway import identity_scene as scene


def test_profile_changes_crop_and_capacity(tmp_path):
    path = tmp_path / 'identity.yml'
    path.write_text('profile:\n  max_image_slots: 2\n  head_box_scale: 1\n  head_rise: 0\n')
    profile = scene.load_profile(path)
    assert profile['max_image_slots'] == 2
    assert scene.head_crop_box([50, 50, 100, 100], (200, 200), profile=profile) == (50, 50, 100, 100)
    assert scene.head_crop_box([50, 50, 100, 100], (200, 200)) != (50, 50, 100, 100)


def test_profile_capacity_enforced_on_real_scene_path():
    import pytest
    from test_identity_scene import scene as run_scene, Fakes, OUT_LIST, PHOTO
    request = scene.SceneRequest(photos=[PHOTO], prompt="x", refine="off", attempts=1,
                                 profile={**scene.DEFAULT_PROFILE, "max_image_slots": 1})
    with pytest.raises(scene.IdentityError, match="too many reference images"):
        run_scene(Fakes([OUT_LIST[1]]), request)


def test_api_and_advertised_schema_resolve_owner_profile(monkeypatch):
    from gateway import identity_api
    profile = {**scene.DEFAULT_PROFILE, 'max_refs_per_person': 7, 'default_refs_per_person': 6}
    monkeypatch.setattr(scene, 'load_profile', lambda **kwargs: profile)
    request = identity_api.build_scene_request({}, [b'picture'], 'portrait')
    assert request.max_refs_per_person == 6
    assert identity_api.build_scene_request({'max_refs_per_person': '7'}, [b'picture'], 'portrait').max_refs_per_person == 7
    spec = identity_api.identity_openapi()
    field = spec['requestBody']['content']['multipart/form-data']['schema']['properties']['max_refs_per_person']
    assert field['default'] == 6 and field['maximum'] == 7


def test_gateway_schema_tracks_profile_reload(monkeypatch):
    import proxy
    monkeypatch.setattr(proxy, '_OPENAPI_INPUTS', [''])
    monkeypatch.setattr(proxy.app, 'openapi_schema', None)
    profile = {'default_refs_per_person': 6, 'max_refs_per_person': 7}
    monkeypatch.setattr(proxy, '_identity_cfg', lambda: {'profile': profile})
    def refs():
        return proxy.app.openapi()['paths']['/gateway/v1/images/identity']['post']['requestBody']['content']['multipart/form-data']['schema']['properties']['max_refs_per_person']
    assert refs()['default'] == 6 and refs()['maximum'] == 7
    profile.update(default_refs_per_person=7, max_refs_per_person=8)
    assert refs()['default'] == 7 and refs()['maximum'] == 8
