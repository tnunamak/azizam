from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _section(text, title):
    return text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def test_owner_docs_name_the_current_key_actions():
    for path in ("README.md", "docs/guides/OPERATOR.md"):
        guide = (ROOT / path).read_text(encoding="utf-8")
        assert "Add key" not in guide and "Create key for" not in guide, path
        assert "Apps & keys" in guide and "Create key" in guide, path


def test_owner_docs_name_the_people_and_engine_controls():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operator = (ROOT / "docs/guides/OPERATOR.md").read_text(encoding="utf-8")
    for people in (
        _section(readme, "Share with family or friends"),
        _section(operator, "Manage people and access plans"),
    ):
        for label in ("Gateway", "People", "Invite someone", "Access plan"):
            assert label in people, label
    assert "Link expires after" in _section(operator, "Manage people and access plans")
    engines = _section(operator, "Add local engines")
    assert "Add engine" in engines and "Continue" in engines
    assert "Add a service record" not in engines


def test_readme_says_the_loopback_base_url_is_only_for_the_gateway_computer():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    step = readme.split("6. Connect your app", 1)[1].split("\n## ", 1)[0]
    assert "only on the computer that runs the gateway" in step
    assert "docs/guides/FOR_MEMBERS.md" in step


def test_operator_invite_steps_open_the_dialog_before_choosing_its_plan():
    operator = (ROOT / "docs/guides/OPERATOR.md").read_text(encoding="utf-8")
    people = _section(operator, "Manage people and access plans")
    assert people.index("Invite someone") < people.index("**Access plan**")
