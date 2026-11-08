from tests.test_workflow_registry_store import owner_contract, _with_inline, _template
from gateway.workflow_registry_store import contract_problems
def test_probe():
    c = _with_inline(owner_contract(), _template(2,1))
    out = [str(list(c["bindings"]))] + [f"{p.path} {p.code} {p.message}" for p in contract_problems(c)] + [str(c["bindings"])[:600]]
    raise AssertionError("\n".join(out))
