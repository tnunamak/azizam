from tests.c2d_policy_support import release_values

def test_upload_size_policy(monkeypatch):
    from gateway import adapter_upload as up
    assert up.Policy().max_bytes == 2147483648
    with release_values(monkeypatch, "adapter_upload_policy", {"max_bytes": 123, "max_bytes_ceiling": 456, "max_header_bytes": 32, "framing_bytes": 55}, "gateway.adapter_upload") as (up,):
        assert up.policy(None).max_bytes == 123
        assert up.policy({"max_bytes":999}).max_bytes == 456
        assert up.MAX_HEADER_BYTES == 32 and up.FRAMING_BYTES == 55
        import pytest
        with pytest.raises(up.Refused):
            up.admit(up.policy({"enabled":True}),size=124,engine_local=True)
