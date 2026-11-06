"""Synthetic 10k-row comparison with main 52408b26; no operator stores.

Timing is reported for acceptance, not asserted against a noisy shared host.
Predicates below are copied from main, independently of the new policy code.
"""
from timeit import repeat

import gateway.accounts  # match gateway initialization order
from gateway import me_media
from gateway.content_access import CreationRecord, ReceiptRecord, Viewer, decide, project
from tests.test_owner_projection import seeded_record, NAMES


def _main_receipt_in_scope(receipt, scope):
    if scope.app_ids is None:
        return True
    if receipt is None:
        return False
    if receipt.get("app_id"):
        return str(receipt["app_id"]) in set(scope.app_ids)
    owner_key = receipt.get("owner_key_hash")
    return isinstance(owner_key, str) and owner_key in (scope.owner_keys or frozenset())


def _ms(call, number=10):
    return min(repeat(call, number=number, repeat=7)) * 1000 / number


def test_principal_scope_10k_main_comparison():
    rows = [{"app_id": f"app{i % 7}"} for i in range(5000)] + [
        {"owner_key_hash": "k1" if i % 2 else "zz"} for i in range(5000)]
    for napps in (5, 24):
        scope = me_media._MediaScope(app_ids=[f"app{i}" for i in range(napps)],
                                     viewer_person_id="p", owner_keys=frozenset({"k1", "k2"}))
        main = lambda: [_main_receipt_in_scope(row, scope) for row in rows]
        current = lambda: [me_media._receipt_in_scope(row, scope) for row in rows]
        assert current() == main()
        old, new = _ms(main), _ms(current)
        print(f"PERF receipt 10k apps={napps}: main={old:.3f} ms current={new:.3f} ms ratio={new / old:.3f}")
    # owns_record decides by maker (D51). Owner-key parity with main is the rule this change removes,
    # so its decisions are pinned in tests/test_content_access_maker.py, not compared here.


def test_policy_and_projection_ceilings():
    viewer = Viewer(auth_kind="app_key", owner_keys=frozenset({"app:a"}))
    record = ReceiptRecord(frozenset({"app:a"}))
    creation = CreationRecord("asset:1", record.owner_keys)
    raw = seeded_record()
    print(f"CEILING decide receipt: {_ms(lambda: decide(viewer, record, 'read'), 10000) * 1000:.3f} us")
    print(f"CEILING decide creation: {_ms(lambda: decide(viewer, creation, 'read'), 10000) * 1000:.3f} us")
    print(f"CEILING receipt projection: {_ms(lambda: project('receipt', raw, names=NAMES), 1000) * 1000:.3f} us")
