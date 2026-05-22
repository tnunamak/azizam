"""Fixture adapter code for extension package validation tests."""


class DemoAdapter:
    """Tiny inert adapter used only to prove package code is hashed."""

    def health(self) -> dict[str, str]:
        return {"status": "ok"}
