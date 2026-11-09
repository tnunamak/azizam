"""S7 section 6.5 and 6.8: the node-graph protocol declares each run operation's poll cadence, and a binding declares the
reviewed result-processing stages it runs. Declarations only; the runner that reads them is built on top of these names."""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path

from gateway import binding_descriptors
from gateway.engine_protocol import description

PROTOCOL = Path(__file__).resolve().parent.parent / "registry" / "protocols" / "node-graph.yml"


def _doc():
    return yaml.safe_load(PROTOCOL.read_text(encoding="utf-8"))


def _compile(doc):
    return description.parse_description(doc, source="test")


def test_shipped_cadences_follow_the_old_routes():
    ops = _compile(_doc()).operations
    image = ops["image.generate"].poll
    assert (image.ramp_s, image.interval_s, image.max_polls) == ((0.0, 0.05, 0.1, 0.2, 0.4), 0.5, None)
    assert ops["image.edit"].poll == image
    assert (ops["audio.generate"].poll.ramp_s, ops["audio.generate"].poll.interval_s, ops["audio.generate"].poll.max_polls) == ((), 2.0, 600)
    assert (ops["video.generate"].poll.ramp_s, ops["video.generate"].poll.interval_s, ops["video.generate"].poll.max_polls) == ((), 5.0, None)
    assert ops["graph.submit"].poll is None


def test_a_run_operation_without_poll_does_not_load():
    doc = _doc()
    del doc["operations"]["audio.generate"]["poll"]
    with pytest.raises(description.DescriptionError, match="must declare 'poll'"):
        _compile(doc)


def test_poll_on_a_wire_operation_does_not_load():
    doc = _doc()
    doc["operations"]["graph.queue"]["poll"] = {"ramp_s": [], "interval_s": 1.0}
    with pytest.raises(description.DescriptionError, match="belongs to a run operation"):
        _compile(doc)


@pytest.mark.parametrize("poll", [{"ramp_s": [], "interval_s": 0}, {"ramp_s": [-1], "interval_s": 1}, {"interval_s": 1},
                                  {"ramp_s": [], "interval_s": 1, "max_polls": 0}, {"ramp_s": [], "interval_s": 1, "extra": 1}])
def test_a_malformed_poll_does_not_load(poll):
    doc = _doc()
    doc["operations"]["image.generate"]["poll"] = poll
    with pytest.raises(Exception):
        _compile(doc)


def test_result_processing_is_a_closed_list_of_stages():
    descriptor = _compile(_doc()).binding_descriptor
    spec = descriptor.fields["result_processing"]
    assert spec.kind == "enum_list"
    assert set(spec.values) == {"source_alpha_mask", "post_composite_mask", "background_fallback", "offset_inpaint", "strip_audio",
                              "attach_provenance"}


def _problems(value):
    from gateway.manifests import ErrorCollector
    spec = _compile(_doc()).binding_descriptor.fields["result_processing"]
    errors = ErrorCollector()
    binding_descriptors._check_field(spec, value, ("result_processing",), None, None, errors)
    return errors.errors


def test_an_unknown_or_repeated_stage_is_refused():
    assert not _problems(["source_alpha_mask", "background_fallback"])
    assert _problems(["no_such_stage"])
    assert _problems(["source_alpha_mask", "source_alpha_mask"])
    assert _problems("source_alpha_mask")
