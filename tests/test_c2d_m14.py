from tests.c2d_policy_support import release_values

def test_upload_storage_policy(monkeypatch, tmp_path):
    with release_values(monkeypatch, "adapter_upload_policy", {"hold_ttl_s": 12, "request_hold_ttl_s": 3, "idle_s": 5, "cache_max_bytes": 99}, "gateway.adapter_choices", "gateway.adapter_upload") as (choices, up):
        assert up.HOLD_TTL_S == choices.RUN_HOLD_TTL_S == 12
        assert choices.HOLD_TTL_S == 3
        assert up.IDLE_S == 5 and up.CACHE_MAX_BYTES == 99

        from tests.test_adapter_upload import source_file
        import os
        source, digest = source_file(tmp_path)
        cache = up.Materializer(tmp_path/'engine')
        (tmp_path/'engine').mkdir()
        monkeypatch.setattr(up.time, 'time', lambda: 100)
        cache.materialize(digest, source, hold='run')
        os.utime(tmp_path/'engine'/cache.name_for(digest), (1,1))
        assert cache.reap(now=111) == []
        assert cache.reap(now=113) == [digest]
