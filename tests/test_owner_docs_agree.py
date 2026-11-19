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
    # README teaches the same flow in quick start step 5; both must name the same controls.
    readme_engines = readme.split("5. Add your engine.", 1)[1].split("\n6. ", 1)[0]
    for label in ("Gateway → Engines", "Add engine", "Continue"):
        assert label in readme_engines, label
    assert "Gateway → Engines" in engines
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


def _web_text():
    files = (ROOT / "web/src").rglob("*.tsx")
    return "\n".join(p.read_text(encoding="utf-8") for p in files if ".spec." not in p.name)


def test_owner_docs_name_the_choices_that_turn_on_the_create_buttons():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operator = (ROOT / "docs/guides/OPERATOR.md").read_text(encoding="utf-8")
    key_step = readme.split("4. Open **Apps & keys**", 1)[1].split("\n5. ", 1)[0]
    for text in (key_step, operator):
        for label in ("**You**", "**A service**", "Create app and key", "Create another key"):
            assert label in text, label
    for people in (
        _section(readme, "Share with family or friends"),
        _section(operator, "Manage people and access plans"),
    ):
        assert "Allow anyone" in people and "Create invite link" in people
        assert "127.0.0.1" in people


def test_member_guide_names_only_controls_the_screens_have():
    guide = (ROOT / "docs/guides/FOR_MEMBERS.md").read_text(encoding="utf-8")
    web = _web_text()
    for gone in ("Gateway check", "Continue to Home"):
        assert gone not in guide, gone
        assert f">{gone}<" not in web and f"\"{gone}\"" not in web, gone
    for label in ("I’ve saved it", "Set up", "Apps & keys"):
        assert label in guide and label in web, label


def test_setup_guide_gets_the_first_key_from_apps_and_covers_a_docker_engine():
    setup = (ROOT / "docs/FIRST_RUN_SETUP.md").read_text(encoding="utf-8")
    assert "mint the first App" not in setup and "mint a runtime key" not in setup
    assert "Mint the first" not in setup
    assert "### Engine in its own Docker container on the same computer" in setup
    assert "`litellm`" in setup and "AZIZAM_DOCKER_GATEWAY" in setup


def test_the_subnet_overlap_advice_shows_how_to_find_the_subnets_in_use():
    # The 2026-11-19 re-walk hit the overlap: the advice said "unused values" and gave no way to see the used ones.
    for name in ("README.md", "docs/FIRST_RUN_SETUP.md"):
        text = " ".join((ROOT / name).read_text(encoding="utf-8").split())
        overlap = text.index("Pool overlaps with other one on this address space")
        assert "docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}'" in text[overlap:overlap + 900], name
