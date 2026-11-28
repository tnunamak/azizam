from contextlib import contextmanager
import importlib
import importlib.util
import sys
from gateway import config_runtime

@contextmanager
def release_values(monkeypatch, section, changes, *modules):
    from gateway import release_policy
    original_policy = release_policy.load
    original = config_runtime.load_sections
    def load(*args, **kwargs):
        from dataclasses import replace
        sections = original(*args, **kwargs)
        row = sections[section]
        sections[section] = replace(row, defaults={**row.defaults, **changes})
        return sections
    with monkeypatch.context() as patch:
        if section in {"adapter_upload_policy", "adapter_discovery", "workflow_retention"}:
            patch.setattr(release_policy, "load", lambda name: {**original_policy(name), **changes}
                          if name == section else original_policy(name))
        else:
            patch.setattr(config_runtime, "load_sections", load)
        loaded = []
        for name in modules:
            source = importlib.import_module(name)
            clone_name = name + "_c2d_test"
            spec = importlib.util.spec_from_file_location(clone_name, source.__file__)
            clone = importlib.util.module_from_spec(spec)
            sys.modules[clone_name] = clone
            spec.loader.exec_module(clone)
            loaded.append(clone)
        try:
            yield loaded
        finally:
            patch.undo()
            for module in loaded:
                sys.modules.pop(module.__name__, None)
