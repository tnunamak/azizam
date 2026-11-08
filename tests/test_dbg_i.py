from tests.test_s7_parity_images import *
from tests.test_s7_parity_images import _half_mask
def test_dbg(tmp_path):
    lines = []
    for model in (None, "krea2-turbo", "qwen-image-2.1-edit", "image-identity"):
        extra = {"model": model} if model else {}
        o = observe(tmp_path / (model or "d"), EDIT, form(files={"image": IMG, "mask": _half_mask()}, **extra), edit_ok, row=False)
        lines.append(f"DBG {model} {status(o)} {(failure(o) or {}).get('message')} {headers_with(o,'x-image-mask')}")
    assert False, "\n".join(lines)
