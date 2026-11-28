from tests.c2d_policy_support import release_values

def test_discovery_policy(monkeypatch):
    with release_values(monkeypatch, "adapter_discovery", {"max_age_s": 2, "header_age_s": 3, "stale_list_s": 1, "failed_read_backoff_s": 4, "header_reads_at_once": 2}, "gateway.adapter_choices") as (choices,):
        assert (choices.MAX_AGE_S, choices.HEADER_AGE_S, choices.STALE_LIST_S, choices.FAILED_READ_BACKOFF_S, choices.HEADER_READS_AT_ONCE) == (2,3,1,4,2)


def test_discovery_clock_uses_release_ttl(monkeypatch):
    import asyncio
    from tests.test_adapter_choices import contract
    with release_values(monkeypatch, 'adapter_discovery', {'max_age_s':2}, 'gateway.adapter_choices') as (choices,):
        decl = choices.declaration(contract(), 'graph')
        calls = []
        def listing(*args):
            calls.append(args)
            return ['a.safetensors']
        async def read(now):
            return await choices.installed(decl, service_id='sample', origin='http://sample.invalid',
                list_files=listing, read_header=lambda *a: {'key':'match'},
                table={'base_a':{'key':['match']}}, now=now)
        asyncio.run(read(100))
        asyncio.run(read(101))
        assert len(calls) == 1
        asyncio.run(read(103))
        assert len(calls) == 2
