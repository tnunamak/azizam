import re
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



_CREATES_A_KEY = re.compile(r"\b(mints?|creates?|issues?|makes?)\b[^.;]*\bkeys?\b")


def _flat(text):
    return " ".join(text.split())


def _sentence_with(text, phrase):
    flat = _flat(text)
    at = flat.index(phrase)
    start = flat.rfind(". ", 0, at)
    end = flat.find(". ", at)
    return flat[start + 2 if start >= 0 else 0:end if end >= 0 else len(flat)]


def _paragraph_with(text, phrase):
    return next(_flat(block) for block in text.split("\n\n") if phrase in _flat(block))


def _subsection(text, title):
    return text.split(f"### {title}\n", 1)[1].split("\n#", 1)[0]


def _steps(section):
    """The numbered steps of a section, each flattened to one line, in order."""
    steps, current = [], None
    for line in section.splitlines():
        head = line.split(". ", 1)
        if head[0].isdigit() and len(head) == 2:
            current = [head[1]]
            steps.append(current)
        elif current is not None and line.startswith("   "):
            current.append(line.strip())
        else:
            current = None
    return [" ".join(step) for step in steps]


def test_owner_docs_name_the_choices_that_turn_on_the_create_buttons():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operator = (ROOT / "docs/guides/OPERATOR.md").read_text(encoding="utf-8")
    key_step = readme.split("4. Open **Apps & keys**", 1)[1].split("\n5. ", 1)[0]
    for text in (key_step, _paragraph_with(operator, "**A service**")):
        choice = _sentence_with(text, "**A service**")
        assert "acts as" in choice and "**You**" in choice, choice
        assert "until you" in _sentence_with(text, "**Create app and key** stays off")
        assert _flat(text).index("**A service**") < _flat(text).index("**Create app and key** stays off")
        assert "**Create key**" in _sentence_with(text, "**Create another key**")
    for people in (
        _section(readme, "Share with family or friends"),
        _section(operator, "Manage people and access plans"),
    ):
        either = _sentence_with(people, "**Allow anyone with the link to create an account and receive an API key**")
        assert "mail" in either and " or tick " in either, either
        assert "one of the two" in _sentence_with(people, "**Create invite link** stays off until")
        assert "replace" in _sentence_with(people, "`http://127.0.0.1`")


def test_member_guide_names_only_controls_the_screens_have():
    guide = (ROOT / "docs/guides/FOR_MEMBERS.md").read_text(encoding="utf-8")
    web = _web_text()
    for gone in ("Gateway check", "Continue to Home"):
        assert gone not in guide, gone
        assert f">{gone}<" not in web and f"\"{gone}\"" not in web, gone
    accept = _steps(_section(guide, "Accept an invitation"))
    assert "**I’ve saved it**" in accept[-1], accept
    connect = _steps(_section(guide, "Connect an app from Home"))
    assert "**Send**" in connect[0] and "Home" in connect[0], connect
    assert "card" in connect[1] and "**Apps & keys**" in connect[1], connect
    assert "**Set up**" in connect[2], connect
    assert connect[-1] == "Send a message from your app.", connect
    # Each control is checked on the screen that renders it, not anywhere in the web source.
    screens = {
        "I’ve saved it": "web/src/components/product/secret-reveal.tsx",
        "Set up": "web/src/app/apps/page.tsx",
    }
    for label, screen in screens.items():
        assert label in (ROOT / screen).read_text(encoding="utf-8"), (label, screen)


def test_setup_guide_gets_the_first_key_from_apps_and_covers_a_docker_engine():
    setup = (ROOT / "docs/FIRST_RUN_SETUP.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    flat = _flat(setup)
    # No sentence about the setup page credits it with a key; the key steps name Apps & keys. The console's own
    # form route (/setup/first-app) does mint one, so only the page path itself is matched.
    for sentence in flat.split(". "):
        if "/setup/`" in sentence:
            assert not _CREATES_A_KEY.search(sentence.lower()) and "opk_" not in sentence, sentence
    for phrase in ("then create the first App and its", "to claim the first owner account", "Create the first key in"):
        assert "**Apps & keys**" in _paragraph_with(setup, phrase), phrase
    docker = _subsection(setup, "Engine in its own Docker container on the same computer")
    for step in ("ip addr show docker0", "-p <docker0-address>:11434:11434", "`http://host.docker.internal:11434`"):
        assert step in docker, step
    assert "`litellm`" in _paragraph_with(setup, "maps `host.docker.internal` to `host-gateway`")
    for text in (setup, readme):
        overlap = _paragraph_with(text, "Pool overlaps with other one on this address space")
        assert "AZIZAM_DOCKER_SUBNET" in overlap and "AZIZAM_DOCKER_GATEWAY" in overlap, overlap
        # The 2026-11-19 re-walk hit the overlap: "unused values" needs a way to see the used ones.
        assert "docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}'" in overlap, overlap


def test_readme_names_the_setup_field_and_the_usage_receipt_steps():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    owner_step = readme.split("3. Create the owner account.", 1)[1].split("\n4. ", 1)[0]
    assert "**Email or username**" in owner_step
    receipt = _sentence_with(readme.split("6. Connect your app", 1)[1], "**View recent requests**")
    assert "**Usage**" in receipt and "newest row" in receipt, receipt
