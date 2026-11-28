import re
from pathlib import Path


def test_contributing_section_links_project_guidelines():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    section = re.search(r"^## Contributing[ \t]*$\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
    assert section is not None, "README is missing the Contributing section"
    # A Markdown link destination, not the file name in plain text or inline code.
    destinations = set(re.findall(r"\[[^\]`]+\]\(([^)\s]+)\)", section.group(1)))
    assert "CONTRIBUTING.md" in destinations
    assert "CODE_OF_CONDUCT.md" in destinations
