"""The retired LLM_PROXY_PORT alias must not affect gateway startup."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _proxy_port(*, environ: dict[str, str], config_port: int = 5000) -> int:
    tree = ast.parse((ROOT / "proxy.py").read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PROXY_PORT" for target in node.targets)
    )
    expression = ast.Expression(assignment.value)
    ast.fix_missing_locations(expression)
    namespace = {
        "int": int,
        "get_env": lambda name, default: environ.get(name, default),
        "os": SimpleNamespace(environ=environ),
        "config": {"proxy": {"port": config_port}},
    }
    return eval(compile(expression, str(ROOT / "proxy.py"), "eval"), namespace)


def test_legacy_port_environment_variable_is_ignored() -> None:
    assert _proxy_port(environ={"LLM_PROXY_PORT": "8123"}) == 5000


def test_yaml_port_default_remains_in_effect_without_new_environment_variable() -> None:
    assert _proxy_port(environ={"LLM_PROXY_PORT": "8123"}, config_port=8124) == 8124


def test_azizam_port_overrides_yaml_and_legacy_environment_variable() -> None:
    assert _proxy_port(
        environ={"AZIZAM_PORT": "8125", "LLM_PROXY_PORT": "8123"},
        config_port=8124,
    ) == 8125
