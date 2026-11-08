import pytest
from tests.test_s7_parity_images import *  # noqa
from tests.test_s7_parity_images import pair, EDIT, form, edit_ok, media_jobs
import json

def test_probe(tmp_path):
    old, row = pair(tmp_path, EDIT, form(), edit_ok)
    print("PROBE", json.dumps(media_jobs(row), default=str)[:3500])
    assert 0
