import re
from pathlib import Path


def _workflow_subsection() -> str:
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
    return workflow


def _words(text: str) -> str:
    """The text on one line, lower case, so a check does not depend on where a line breaks."""
    return re.sub(r"\s+", " ", text).lower()


def test_operator_guide_names_the_settings_and_the_dialog_the_owner_uses():
    workflow = _words(_workflow_subsection())
    for term in ("engine folders", "add engine", "main.py", "computer commands"):
        assert term in workflow, f"Workflow engine subsection must mention {term}"


def test_operator_guide_says_folders_are_saved_by_actual_location_and_never_the_whole_computer():
    workflow = _words(_workflow_subsection())
    assert "saves the folder's actual location" in workflow
    assert "whole computer" in workflow and "refuses" in workflow


def test_operator_guide_lists_the_python_choices_in_the_order_azizam_tries_them():
    workflow = _workflow_subsection()
    assert "`.venv`" in workflow and "`venv`" in workflow
    assert workflow.index("`.venv`") < workflow.index("`venv`")


def test_operator_guide_says_the_port_is_free_and_the_preview_comes_before_saving():
    workflow = _words(_workflow_subsection())
    assert "free port" in workflow
    assert re.search(r"before you save, [^.]*preview shows the exact command", workflow)
    assert workflow.index("preview") < workflow.index("saving does not start")


def test_operator_guide_does_not_promise_what_a_removed_folder_does_to_a_saved_engine():
    workflow = _words(_workflow_subsection())
    assert "keeps running" not in workflow
    assert "does not change an engine you already saved" in workflow


def test_operator_guide_says_workflows_you_add_yourself_are_not_checked_and_overlap_needs_confirming():
    workflow = _words(_workflow_subsection())
    assert "workflows you add yourself are not checked" in workflow
    assert "another engine already handles that work" in workflow
    assert "confirm" in workflow


def test_operator_guide_says_health_check_not_status_path():
    workflow = _words(_workflow_subsection())
    assert "health check" in workflow
    assert "status path" not in workflow
