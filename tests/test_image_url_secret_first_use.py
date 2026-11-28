"""The image URL secret is made once, even when two requests need it first at the same time.

Found by gate 119: the idempotency race test in test_generated_defaults.py failed whenever it was the first test on
its worker to need the secret, because each of its two threads generated its own secret and so derived its own job
id for the same idempotency key."""
import threading

import proxy
from gateway import private_io


def test_two_first_requests_get_one_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "_image_url_signing_secret", None)
    monkeypatch.setattr(proxy, "IMAGE_SIGNED_URL_SECRET_CFG", "")
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    # Hold each caller at the point where it is about to generate, so two callers that both reach it overlap there.
    # With the lock only one caller reaches it; the barrier then times out and that caller goes on alone.
    barrier = threading.Barrier(2, timeout=1)
    real_ensure = private_io.ensure_private_dir

    def overlap(path, *args, **kwargs):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(private_io, "ensure_private_dir", overlap)
    got: list[bytes] = []
    lock = threading.Lock()

    def one() -> None:
        value = proxy._image_signing_secret()
        with lock:
            got.append(value)

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(got) == 2
    assert got[0] == got[1], "both requests use the same secret"
    assert (tmp_path / "images" / ".url_signing_secret").read_bytes().strip() == got[0], "the stored secret is the one in use"
