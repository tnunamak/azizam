"""Hermetic scale-selection regression tests; no model loading or inference."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

# numpy ships with the face service, not the gateway suite: the gateway gate skips
# this file; run it where numpy is installed.
np = pytest.importorskip("numpy")


@pytest.fixture
def facecore(monkeypatch):
    package = ModuleType("insightface")
    package.__path__ = []
    app = ModuleType("insightface.app")
    app.__path__ = []
    common = ModuleType("insightface.app.common")

    class UnusedModel:
        def __init__(self, *args, **kwargs):
            pytest.fail("test attempted model construction")

    app.FaceAnalysis = UnusedModel
    common.Face = UnusedModel
    package.app = app
    app.common = common
    ort = ModuleType("onnxruntime")

    class InferenceSession:
        def __init__(self, *args, **kwargs):
            pytest.fail("test attempted inference session")

    class SessionOptions:
        pass

    ort.InferenceSession = InferenceSession
    ort.SessionOptions = SessionOptions
    for name, module in [
        ("insightface", package), ("insightface.app", app),
        ("insightface.app.common", common), ("onnxruntime", ort),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
    try:
        import cv2  # noqa: F401
    except ImportError:
        cv2 = ModuleType("cv2")
        cv2.INTER_CUBIC = 2
        cv2.INTER_AREA = 3

        def resize(frame, dsize, *, fx, fy, interpolation):
            return np.zeros((round(frame.shape[0] * fy),
                             round(frame.shape[1] * fx), frame.shape[2]), dtype=frame.dtype)

        cv2.resize = resize
        monkeypatch.setitem(sys.modules, "cv2", cv2)
    path = Path(__file__).resolve().parents[1] / "services/face-service/facecore.py"
    spec = importlib.util.spec_from_file_location("facecore_scale_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class FakeDetector:
    def __init__(self):
        self.calls = []

    def detect(self, canvas, *, input_size, max_num, metric):
        self.calls.append(input_size)
        assert canvas.shape == (*input_size, 3)
        assert max_num == 0
        assert metric == "default"
        return (np.array([[10., 20., 30., 40., 0.9]]),
                np.array([[[12., 22.], [28., 22.], [20., 30.], [14., 38.], [26., 38.]]]))


def test_large_frame_uses_one_bounded_fit_pass(facecore):
    detector = FakeDetector()
    frame = np.zeros((4320, 7680, 3), dtype=np.uint8)
    found = facecore.detect(SimpleNamespace(det_model=detector), frame, 0.5)
    assert detector.calls == [(1920, 1920)]
    assert max(detector.calls[0]) <= facecore.MAX_CANVAS
    assert len(found) == 1
    np.testing.assert_allclose(found[0].bbox, [40., 80., 120., 160.])
    np.testing.assert_allclose(found[0].kps,
                               [[48., 88.], [112., 88.], [80., 120.], [56., 152.], [104., 152.]])
    assert found[0].score == pytest.approx(0.9)


def test_small_frame_preserves_configured_passes(facecore):
    detector = FakeDetector()
    facecore.detect(SimpleNamespace(det_model=detector),
                    np.zeros((480, 640, 3), dtype=np.uint8), 0.5)
    assert detector.calls == [(640, 640), (320, 320), (1280, 1280), (320, 320)]


def test_large_frame_with_listed_fit_does_not_add_fallback(facecore):
    detector = FakeDetector()
    facecore.detect(SimpleNamespace(det_model=detector),
                    np.zeros((2160, 3840, 3), dtype=np.uint8), 0.5)
    assert detector.calls == [(1920, 1920), (1152, 1152)]
