"""Fixture translator code for extension package validation tests."""


def request_to_canonical(body: dict) -> dict:
    return {"model": body.get("model", "default"), "messages": body.get("messages", [])}


def response_from_canonical(body: dict) -> dict:
    return {"response": body}
