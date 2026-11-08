from tests.test_s7_parity_images import *
from tests.test_s7_parity_images import _half_mask
def test_dbg(tmp_path):
    o = observe(tmp_path, EDIT, form(files={"image": IMG, "mask": _half_mask()}), edit_ok, row=False)
    print("STATUS", status(o), failure(o), headers_with(o,"x-image"), o.extra)
