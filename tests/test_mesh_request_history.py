"""A kept request names what it ran: the run facts a summary keeps (workflow, title, capability).

The 3D rows run over the run core, which writes no run facts into the kept request yet, so the end-to-end case that the
deleted 3D route had (title and library item of a kept 3D request) is gone with it; see the builder D4 report.
"""
from __future__ import annotations

from gateway import request_history




def test_run_facts_keep_short_text_and_fill_the_capability_label():
    facts = request_history.run_facts({"workflow": "w1", "title": "  Name  ", "capability": "mesh", "extra": "x"})
    assert facts == {"workflow": "w1", "title": "Name", "capability": "mesh", "capability_label": "3D models"}
    assert request_history.run_facts(None) == {}
    assert request_history.run_facts({"title": 3, "capability": "unlisted-kind"}) == {"capability": "unlisted-kind"}


def test_a_summary_without_run_facts_reads_as_it_always_did():
    summary = request_history.summarize("/v1/chat/completions", {"model": "m"}, 200, 5)
    assert "run" not in summary
