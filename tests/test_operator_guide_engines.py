import re
from pathlib import Path


def test_operator_guide_explains_host_managed_workflow_engines():
    guide = (
        Path(__file__).resolve().parents[1] / "docs" / "guides" / "OPERATOR.md"
    ).read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", guide)
    engines = next(
        (section for section in sections if section.splitlines()[0] == "Add local engines"),
        None,
    )
    assert engines is not None, "Missing Add local engines section"

    subsections = re.split(r"(?m)^### ", engines)
    workflow = next(
        (
            subsection
            for subsection in subsections
            if subsection.splitlines()[0] == "Add a workflow engine Azizam starts"
        ),
        None,
    )
    assert workflow is not None, "Missing workflow engine subsection"
    for term in ("Engine folders", "preview", ".venv", "confirm"):
        assert term in workflow, f"Workflow engine subsection must mention {term}"
