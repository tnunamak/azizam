"""C10: the startup file's closed schema (registry/startup.schema.json) and the check that refuses a key it lacks."""
from __future__ import annotations

import json

import pytest

from gateway import startup_config


def _problems(raw):
    return {problem.path: problem.message for problem in startup_config.validate(raw)}


def test_an_empty_file_and_a_file_of_startup_keys_are_valid():
    assert startup_config.validate(None) == []
    assert startup_config.validate({}) == []
    assert startup_config.validate({
        "proxy": {"host": "127.0.0.1", "port": 8000},
        "admin": {"db_path": "admin.db", "oidc": {"enabled": False, "cookie": {"secure": False}}},
        "auth": {"public_paths": ["/health"], "trusted_proxies": []},
        "images": {"cache_dir": "cache", "media_jobs": {"payload_dir": "jobs"}},
    }) == []


def test_a_key_that_moved_is_refused_with_its_new_home():
    problems = _problems({
        "backends": {}, "providers": {}, "routing": {}, "roles": {}, "llm": {"pin_loaded_model": True},
        "models": {}, "workflow_execution": {},
        "proxy": {"public_url": "https://example.test"},
        "admin": {"brand": {"product": "x"}, "user_defaults": {}},
        "gateway": {"resource_admission": {"media_jobs": {}}},
    })
    assert "service registry" in problems["backends"]
    assert "service registry" in problems["providers"]
    assert "delete it" in problems["routing"]
    assert "config section `roles`" in problems["roles"]
    assert "config section `llm`" in problems["llm"]
    assert "config section `models`" in problems["models"]
    assert "config section `workflow_execution`" in problems["workflow_execution"]
    assert "/base_url" in problems["proxy.public_url"]
    assert "/name" in problems["admin.brand.product"]
    assert "Access plans" in problems["admin.user_defaults"]
    assert "media-job-admission" in problems["gateway"]


def test_a_parent_of_a_moved_key_names_each_moved_child():
    message = _problems({"gateway": {"resource_admission": {}}})["gateway"]
    assert "gateway.resource_admission.media_jobs is config section `resource_admission`" in message


def test_a_key_under_a_section_names_the_entry_and_the_write_route():
    message = _problems({"instance": {"name": "x"}})["instance"]
    assert "PUT /admin/api/settings/instance" in message
    message = _problems({"llm": {"pin_loaded_model": False}})["llm"]
    assert "PATCH /admin/api/config/changes" in message


def test_an_unknown_key_says_nothing_reads_it():
    assert "Delete it" in _problems({"proxy": {"workers": 4}})["proxy.workers"]
    assert "Delete it" in _problems({"surprise": 1})["surprise"]


def test_a_removed_auth_key_is_named_as_removed():
    problems = _problems({"auth": {"enabled": True, "lan_bypass_enabled": True, "path_tokens": []}})
    for key in ("enabled", "lan_bypass_enabled", "path_tokens"):
        assert "is removed" in problems[f"auth.{key}"]


def test_a_wrong_type_is_refused_at_the_key():
    problems = _problems({"proxy": {"port": "8000"}, "auth": {"cloudflare_mode": "yes"}, "admin": "x"})
    assert "proxy.port" in problems and "auth.cloudflare_mode" in problems
    assert "must be a mapping" in problems["admin"]
    assert startup_config.validate({"proxy": {"port": 70000}})


def test_check_raises_one_line_per_fault():
    with pytest.raises(startup_config.StartupConfigError) as caught:
        startup_config.check({"backends": {}, "proxy": {"workers": 1}}, source="proxy_config.yml")
    text = str(caught.value)
    assert text.startswith("proxy_config.yml is not a valid startup file:")
    assert "`backends`" in text and "`proxy.workers`" in text


def test_a_root_that_is_not_a_mapping_is_refused():
    assert _problems(["x"])["(file)"].startswith("The file must be a mapping")


def test_a_block_that_holds_only_comments_is_valid():
    assert startup_config.validate({"mcp": None, "request_body_limits": None, "admin": {"oidc": None}}) == []


def test_the_schema_is_closed_at_every_object():
    def walk(node, path):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, path
            for name, child in node["properties"].items():
                walk(child, f"{path}.{name}")

    walk(startup_config.load_schema(), "")


def test_every_moved_entry_names_a_real_place():
    moved = startup_config.load_schema()["x-azizam-moved"]
    sections = startup_config.section_ids()
    for head, where in moved.items():
        assert not startup_config.startup_paths() & {head}, f"{head} is both a startup key and a moved key"
        if "config section `" in where:
            name = where.split("config section `")[1].split("`")[0]
            assert name in sections, (head, name)
        assert where.strip(), head


def test_no_section_name_is_also_a_startup_top_level_key_except_the_listener_style_ones():
    """A top-level name belongs to one home: a section of admin.db or the startup schema, never both."""
    top = set(startup_config.load_schema()["properties"])
    overlap = top & startup_config.section_ids()
    assert overlap <= {"images", "voices", "audio", "model_routing"}, overlap  # sections that keep a storage root here


def test_every_env_name_is_declared_once():
    names = [key.env for key in startup_config.iter_keys() if key.env]
    assert len(names) == len(set(names))
    assert startup_config.startup_env_names() == frozenset(names)


def test_secrets_are_marked():
    secrets = {key.path for key in startup_config.iter_keys() if key.secret}
    assert {"admin.session_signing_key", "admin.oidc.client_secret", "admin.oidc.signing_key",
            "images.signed_url_secret"} <= secrets


def test_where_it_lives_prefers_the_longest_moved_entry():
    assert "instance" in startup_config.where_it_lives("admin.brand.product")
    assert startup_config.where_it_lives("nothing.here").startswith("nowhere")


def test_the_schema_file_is_valid_json_schema():
    json.loads(startup_config.SCHEMA_PATH.read_text(encoding="utf-8"))
    startup_config.load_schema()


def test_every_startup_only_name_of_a_section_is_a_startup_key_or_a_deliberate_move():
    """A section's ``x-azizam-startup-only`` names the keys its old file block held that the section never owns. Each
    must still have a home: a key of the startup schema, or an ``x-azizam-moved`` entry that says where it went. A name
    in neither would be refused at start with no place to put it."""
    from gateway import config_runtime

    keys = {key.path for key in startup_config.iter_keys()}
    moved = startup_config.load_schema()["x-azizam-moved"]
    homeless = []
    for section_id, section in config_runtime.load_sections().items():
        for name in section.schema.get("x-azizam-startup-only") or []:
            path = f"{section_id}.{name}"
            if path not in keys and path not in moved:
                homeless.append(path)
    assert homeless == []


def test_the_tracked_startup_file_is_valid_and_is_the_first_run_file():
    """A fresh install starts from the tracked file: local only, no sign-in provider, setup page claims the first owner.
    (The fresh-install journey, scripts/e2e/fresh_instance.sh, boots a gateway from it with no admin.db.)"""
    from pathlib import Path

    import yaml

    raw = yaml.safe_load((Path(__file__).resolve().parent.parent / "proxy_config.yml").read_text(encoding="utf-8"))
    startup_config.check(raw, source="proxy_config.yml")
    assert raw["proxy"]["host"] == "127.0.0.1"
    assert raw["admin"]["oidc"]["cookie"]["secure"] is False
    assert not (raw["admin"]["oidc"].keys() - {"cookie"}), "no sign-in provider ships in the tracked file"
    assert not (raw.keys() - {"proxy", "admin"}), "the tracked file holds only the listener and the cookie rule"


REMOVED_ENV_OVERRIDES = (
    "UPSTREAM_READ_TIMEOUT", "EMBEDDINGS_TIMEOUT", "AZIZAM_MODEL_COMPAT_ENABLED", "OPENAI_MODEL_ALIASES_FILE",
    "OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE", "IMAGE_MEDIA_JOB_RESUME_ON_START", "AZIZAM_PUBLIC_URL", "AZIZAM_BRAND",
    "COMFY_ENABLED", "COMFY_HOST", "AZIZAM_USER_SELF_SERVICE", "AZIZAM_AUTO_CREATE_PERSONAL_APP",
    "TTS_ENABLED", "TTS_HOST",
)


def test_no_gateway_code_reads_an_environment_override_of_a_setting_that_lives_in_admin_db():
    """F4 (docs/design/CONFIG_HOMES.md): an env var that overrides an owner setting is a second writable home. The
    variables listed in docs/CONFIG.md as removed must stay out of the gateway and out of scripts/*.py. (The shell script
    ``scripts/safe-restart`` still reads ``AZIZAM_PUBLIC_URL`` from the operator's shell; it is not scanned.)"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sources = [root / "proxy.py", *sorted((root / "gateway").glob("*.py")), *sorted((root / "scripts").glob("*.py"))]
    found = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for name in REMOVED_ENV_OVERRIDES:
            for quote in ('"', "'"):
                if f"environ.get({quote}{name}{quote}" in text or f"getenv({quote}{name}{quote}" in text \
                        or f"get_env({quote}{name}{quote}" in text or f"environ[{quote}{name}{quote}]" in text:
                    found.append(f"{path.name}: {name}")
    assert found == []
    documented = (root / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    assert [name for name in REMOVED_ENV_OVERRIDES if f"`{name}`" not in documented] == [], "list each removed variable in docs/CONFIG.md"


def test_no_unit_file_or_shell_script_sets_a_removed_environment_variable():
    """A unit file or script that still sets a removed variable tells an operator it does something. ``scripts/safe-restart``
    reads ``AZIZAM_PUBLIC_URL`` from the operator's shell on purpose and is left out."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    files = [root / "azizam.service", *sorted((root / "scripts").glob("*.sh")), *sorted((root / "docs" / "ops").glob("*.service"))]
    setter = re.compile(r"(?<![\w])(?:" + "|".join(REMOVED_ENV_OVERRIDES) + r")=")
    found = [f"{path.name}: {match.group(0)}" for path in files if path.is_file()
             for match in setter.finditer(path.read_text(encoding="utf-8"))]
    assert found == []


# --- done gate item 9: the schema's top-level keys are the keys the code reads -------------------------------------------

class _Scope:
    """One module or function body: the bindings made in it. A binding is (value, simple); ``value`` is the right-hand side
    of ``name = value`` (or None when the name is bound some other way: a parameter, a loop, an import, an unpacking);
    ``simple`` is true only for ``name = value`` and ``name: T = value`` with that single target."""

    def __init__(self, parent: "_Scope | None") -> None:
        self.parent = parent
        self.bindings: dict[str, list[tuple[object, bool]]] = {}

    def lookup(self, name: str) -> "tuple[_Scope, list[tuple[object, bool]]] | None":
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.bindings:
                return scope, scope.bindings[name]
            scope = scope.parent
        return None


def _collect(tree) -> "tuple[list[tuple[object, _Scope]], _Scope]":
    """(reads, module scope): every ``x.get(k)`` and ``x[k]`` load with the scope it sits in, and the bindings of every scope."""
    import ast

    reads: list[tuple[object, _Scope]] = []

    def bind(scope: _Scope, name: str, value: object = None, simple: bool = False) -> None:
        scope.bindings.setdefault(name, []).append((value, simple))

    def visit(node, scope: _Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner = _Scope(scope)
            if not isinstance(node, ast.Lambda):
                bind(scope, node.name)
                for outer in (*node.decorator_list, node.returns):
                    if outer is not None:
                        visit(outer, scope)
            arguments = node.args
            for default in (*arguments.defaults, *arguments.kw_defaults):
                if default is not None:
                    visit(default, scope)
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg):
                if argument is not None:
                    bind(inner, argument.arg)
            for child in (node.body if isinstance(node.body, list) else [node.body]):
                visit(child, inner)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None:
                for target in targets:
                    if isinstance(target, ast.Name):
                        bind(scope, target.id, node.value, simple=len(targets) == 1)
                    else:
                        visit(target, scope)
                visit(node.value, scope)
                return
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bind(scope, (alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                bind(scope, name)
        elif isinstance(node, ast.ClassDef):
            bind(scope, node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(scope, node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bind(scope, node.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            reads.append((node, scope))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):  # a write or a delete is not a read
            reads.append((node, scope))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    module = _Scope(None)
    for statement in tree.body:
        visit(statement, module)
    return reads, module


def startup_keys_read(sources: dict[str, str], section_ids: set[str]) -> tuple[set[str], list[str]]:
    """(keys, unresolved): top-level keys that code reads from the loaded startup file, and the reads the scan could not resolve.

    Three objects hold it: ``_bootstrap_config`` (the raw file), ``config`` and ``runtime().config`` (the file merged with
    the sections and the registry's ``backends``). Every literal key read (``x.get("k")`` or ``x["k"]``) is collected.

    A name (an alias of one of those objects, or a string constant used as the key) is followed only when it has exactly
    one binding in its scope (the module, or the function that holds the read, or one around it) and that binding is a
    plain ``name = value``. A name bound twice, rebound, a parameter, or a target of ``a = b = value`` is not followed: a
    read through it that could reach the file, and a read whose key is not a literal or such a constant, come back in
    ``unresolved`` as ``file:line: expression`` and the test fails on them. One pass, no fixed point: a name is
    resolved by looking up its single binding, and following aliases of aliases stops at a name seen twice.

    A key that is a section id, or ``backends``, is read from the merged config unless the read is on
    ``_bootstrap_config``: a section id that the schema also lists (``images``) is a startup key only through its raw
    reads. Limit: a local variable that is also called ``config`` and is not the loaded file would add a key.
    """
    import ast

    tracked = {"config", "_bootstrap_config", "runtime().config"}
    ambiguous = "(ambiguous)"
    found: set[str] = set()
    unresolved: list[str] = []

    def single(scope: _Scope, name: str):
        """(binding scope, value) of a name with one plain binding; (binding scope, None) when it has other bindings; None when unbound."""
        looked = scope.lookup(name)
        if looked is None:
            return None
        owner, bindings = looked
        return (owner, bindings[0][0]) if len(bindings) == 1 and bindings[0][1] else (owner, None)

    def object_of(expression, scope: _Scope, seen: frozenset = frozenset()) -> str | None:
        """A tracked object name, ``ambiguous`` when a name that may hold one has several bindings, or None."""
        text = ast.unparse(expression)
        if text in tracked:
            return text
        if not isinstance(expression, ast.Name) or (scope, text) in seen:
            return None
        seen = seen | {(scope, text)}
        looked = scope.lookup(text)
        if looked is None:
            return None
        owner, bindings = looked
        if len(bindings) == 1 and bindings[0][1]:
            return object_of(bindings[0][0], owner, seen)
        return ambiguous if any(value is not None and object_of(value, owner, seen) for value, _ in bindings) else None

    for filename, source in sources.items():
        reads, module = _collect(ast.parse(source))
        for node, scope in reads:
            if isinstance(node, ast.Call):
                key_node, receiver = node.args[0], node.func.value
            else:
                key_node, receiver = node.slice, node.value
            holder = object_of(receiver, scope)
            if holder is None:
                continue
            key = None
            if holder != ambiguous:
                if isinstance(key_node, ast.Constant):
                    key = key_node.value
                elif isinstance(key_node, ast.Name) and (named := single(scope, key_node.id)) and isinstance(named[1], ast.Constant):
                    key = named[1].value
            if not isinstance(key, str):
                unresolved.append(f"{filename}:{node.lineno}: {ast.unparse(node)}")
                continue
            if holder == "_bootstrap_config" or (key not in section_ids and key != "backends"):
                found.add(key)
    return found, unresolved


def test_the_top_level_keys_of_the_startup_schema_are_the_keys_the_code_reads():
    """Both ways: a key the schema lists that nothing reads is dead, a key the code reads that the schema lacks is refused
    at start (the closed schema) and would break the gateway."""
    from pathlib import Path

    from gateway import startup_config

    root = Path(__file__).resolve().parent.parent
    sources = {name: (root / name).read_text(encoding="utf-8")
               for name in ("proxy.py", "gateway/bootstrap_config.py", "gateway/request_body_limits.py")}
    sections = {path.name.removesuffix(".schema.json") for path in (root / "registry" / "defaults").glob("*.schema.json")}
    read, unresolved = startup_keys_read(sources, sections)
    assert unresolved == [], "a read of the startup file whose key the scan cannot resolve; use a literal or a module constant:\n" + "\n".join(unresolved)
    declared = set(startup_config.load_schema()["properties"])
    assert read - declared == set(), f"the code reads startup keys the schema does not list: {sorted(read - declared)}"
    assert declared - read == set(), f"the schema lists startup keys no code reads: {sorted(declared - read)}"


def test_the_startup_key_scan_sees_a_new_read_and_ignores_a_section_read():
    source = ('SECTION = "audio"\n_bootstrap_config.get(SECTION)\nconfig.get("zzz")\nruntime().config["images"]\n'
              'config["backends"]\nother.get("nope")\n')
    assert startup_keys_read({"x.py": source}, {"images", "audio"}) == ({"audio", "zzz"}, [])


def test_the_startup_key_scan_does_not_count_a_write_or_a_delete_as_a_read():
    source = '_bootstrap_config["unused"] = 1\ndel config["gone"]\nconfig["read"]\n'
    assert startup_keys_read({"x.py": source}, set()) == ({"read"}, [])


def test_the_startup_key_scan_resolves_an_annotated_constant():
    source = 'KEY: str = "annotated"\n_bootstrap_config.get(KEY)\nconfig[KEY]\n'
    assert startup_keys_read({"x.py": source}, set()) == ({"annotated"}, [])


def test_the_startup_key_scan_follows_a_simple_alias():
    source = ('cfg = _bootstrap_config\n'
              'def f():\n    raw: dict = _bootstrap_config\n    return raw.get("viaannotated")\n'
              'other = cfg\ncfg.get("viaalias")\nother["viachain"]\nnot_config = {}\nnot_config.get("nope")\n')
    assert startup_keys_read({"x.py": source}, {"viaalias"}) == ({"viaannotated", "viaalias", "viachain"}, [])  # raw alias: section ids count


def test_the_startup_key_scan_reports_a_read_it_cannot_resolve_with_its_location():
    source = 'name = compute()\nconfig.get(name)\n_bootstrap_config[f"x{name}"]\nconfig.get("fine")\n'
    found, unresolved = startup_keys_read({"x.py": source}, set())
    assert found == {"fine"}
    assert unresolved == ["x.py:2: config.get(name)", "x.py:3: _bootstrap_config[f'x{name}']"]


def _bounded(call, *args, seconds: int = 5):
    """Run the scan with a time limit, so a loop that never ends fails the test instead of hanging the run."""
    import signal

    def give_up(*_):
        raise TimeoutError(f"the scan did not finish in {seconds} s")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.alarm(seconds)
    try:
        return call(*args)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_name_bound_twice_is_not_followed_and_the_scan_ends():
    source = 'cfg = _bootstrap_config\ncfg = config\ncfg.get("x")\n'
    found, unresolved = _bounded(startup_keys_read, {"x.py": source}, set())
    assert found == set() and unresolved == ["x.py:3: cfg.get('x')"]


def test_an_alias_name_used_in_two_functions_resolves_in_each_function_alone():
    source = ('def one():\n    raw = _bootstrap_config\n    return raw.get("one")\n'
              'def two():\n    raw = {}\n    return raw.get("two")\n'
              'def three():\n    raw = config\n    return raw.get("sect")\n')
    assert _bounded(startup_keys_read, {"x.py": source}, {"sect"}) == ({"one"}, [])


def test_a_constant_bound_twice_or_shadowed_by_a_parameter_is_unresolved():
    twice = 'KEY = "proxy"\nKEY = compute()\nconfig.get(KEY)\n'
    assert _bounded(startup_keys_read, {"x.py": twice}, set()) == (set(), ["x.py:3: config.get(KEY)"])
    shadowed = 'KEY = "proxy"\ndef f(KEY):\n    return config.get(KEY)\ndef g():\n    return config.get(KEY)\n'
    assert _bounded(startup_keys_read, {"x.py": shadowed}, set()) == ({"proxy"}, ["x.py:3: config.get(KEY)"])


def test_an_alias_made_by_a_multiple_assignment_is_unresolved():
    source = 'a = b = _bootstrap_config\na.get(name)\nb.get("lit")\n'
    assert _bounded(startup_keys_read, {"x.py": source}, set()) == (set(), ["x.py:2: a.get(name)", "x.py:3: b.get('lit')"])


def test_an_alias_cycle_ends():
    source = 'a = b\nb = a\na.get("k")\n'
    assert _bounded(startup_keys_read, {"x.py": source}, set()) == (set(), [])
