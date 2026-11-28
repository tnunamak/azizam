"""The browser test server names a busy port instead of dying with a generic readiness error."""
import socket

from scripts.ui.serve_ports import busy_port_message, busy_ports


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_a_held_port_is_reported_busy_and_a_free_one_is_not():
    with socket.socket() as holder:
        holder.bind(('127.0.0.1', 0))
        holder.listen()
        held = holder.getsockname()[1]
        free = _free_port()
        assert busy_ports([held, free]) == [held]


def test_the_check_releases_every_port_it_probes():
    port = _free_port()
    assert busy_ports([port]) == []
    with socket.socket() as again:
        again.bind(('127.0.0.1', port))


def test_the_message_names_each_busy_port_and_the_setting():
    message = busy_port_message([18700, 18702])
    assert '18700' in message and '18702' in message
    assert 'UI_E2E_PORT' in message
