"""The gateway never logs an upstream response body or an upstream error text (decision D43).

A credential reaches a log by being sent upstream: a server echoes what it was sent, in a body or an error, and an
``httpx`` error text names the URL (which can carry a credential). Scrubbing the line afterwards is the backup
(``gateway/log_scrub.py``); the rule is that such text is not put in a log call at all. A log line about an upstream says a
status, a content type, a byte count, the class of an error and the origin it was sent to, and a reason of our own.

This test finds every ``print``/``_say``/logger/``sys.stderr.write``/``traceback`` call in ``gateway/`` and ``proxy.py``
(a logger is any name or attribute assigned from ``getLogger(...)``, any ``getLogger(...).level(...)`` call and any
``logging`` module alias; ``traceback`` helpers are found under any import alias) whose arguments are derived from

* the body of a response: ``.text``, ``.content``, ``.json()``, ``.read()``, ``.aread()``, ``.aiter_*()``, and any name
  assigned from them (so ``response_text[-300:]`` is found);
* the output of a subprocess: ``.stdout``, ``.stderr``, ``.communicate()``;
* the text of an exception caught around a network call (``str(exc)``, ``f"{exc}"``, ``exc.args``, ``repr(exc)``), or any
  exception caught as an ``httpx`` error;
* a traceback (``traceback.print_exc()``, ``format_exc()``): it holds the same text.

An argument passed through a formatter of ``gateway/log_scrub.py`` (``describe_response``, ``describe_bytes``,
``describe_error``) is allowed: they keep a status, a size and a class name. So is ``type(exc).__name__``. A call that is
found and is listed in ``ALLOWED`` below must say why the text cannot carry a credential.

This is the narrow, syntactic half. The boundary that does not depend on finding each call is the runtime egress
(``gateway/log_egress.py``, tested by ``tests/test_log_egress.py``): every record is cut down to scrubbed text and
the type and location of its exception whoever made it. The shared checker is ``tests/support/upstream_taint.py``.
"""

from __future__ import annotations

from tests.support import upstream_taint as taint

# "file::function": why the logged text cannot carry a credential an upstream was sent.
ALLOWED: dict[str, str] = {
    "gateway/log_egress.py::exception_text": "the egress's own renderer: it blanks the text of every exception it renders",
}


def discover() -> list[tuple[str, int, str]]:
    return taint.discover(taint.is_log_call, traceback_is_sink=True)


def test_no_log_call_takes_an_upstream_body_or_an_upstream_error_text():
    bare = sorted({(key, line, what) for key, line, what in discover() if key not in ALLOWED})
    assert not bare, "log calls that take text an upstream or a subprocess produced (use log_scrub.describe_*):\n" + "\n".join(
        f"  {key} line {line}: {what}" for key, line, what in bare)


def test_the_allowed_list_holds_no_stale_entry():
    keys = {key for key, _, _ in discover()}
    assert not [key for key in ALLOWED if key not in keys]


def test_the_checker_finds_what_it_is_meant_to_find():
    source = '''
import httpx
import logging
import logging as lg
import traceback
import traceback as tb
from traceback import format_exc as fe
LOG = logging.getLogger("x")
class K:
    def __init__(self):
        self.log = logging.getLogger("y")
async def a(client, url):
    resp = await client.get(url)
    body = resp.text
    print(f"failed {body[:200]}")
async def b(client, url):
    try:
        await client.get(url)
    except Exception as e:
        print(f"error {e}")
async def c(client, url):
    try:
        await client.get(url)
    except Exception as e:
        print(type(e).__name__)
        print(log_scrub.describe_error(e))
async def d(client, url):
    resp = await client.get(url)
    print(log_scrub.describe_response(resp, "refused"))
def e(proc):
    print(proc.stderr)
async def f(resp):
    text = ""
    async for chunk in resp.aiter_bytes():
        text += chunk.decode()
    print(text)
def g():
    traceback.print_exc()
async def h(client, url):
    resp = await client.get(url)
    logging.getLogger("z").warning("bad %s", resp.text)
async def i(client, url):
    resp = await client.get(url)
    LOG.warning("bad %s", resp.text)
async def j(client, url):
    try:
        await client.get(url)
    except Exception as err:
        logging.exception("failed %s", err)
async def k(client, url):
    try:
        await client.get(url)
    except Exception:
        LOG.error("failed %s", tb.format_exc())
async def l(client, url):
    try:
        await client.get(url)
    except Exception:
        LOG.error("failed %s", fe())
async def m(client, url):
    resp = await client.get(url)
    lg.error("bad %s", resp.content)
async def n(self, client, url):
    resp = await client.get(url)
    self.log.warning("bad %s", resp.text)
async def o(client, url):
    resp = await client.get(url)
    LOG.warning("status %s", resp.status_code)
    LOG.warning("%s", log_scrub.describe_response(resp))
'''
    assert taint.scan_source(source, taint.is_log_call) == {"a", "b", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n"}
