"""The CLI entry point: defaults and refusal surface (no hardware involved).

`fm1-switch web` binds to loopback by default (audit SEC-02) — the panel's own path —
and `--host` is the documented opt-in to the LAN.
"""

from __future__ import annotations

import fm1_switch.__main__ as cli


def _capture_serve(monkeypatch) -> dict:
    seen = {}

    def fake_serve(host, port):
        seen.update(host=host, port=port)
        return 0

    monkeypatch.setattr("fm1_switch.web.serve", fake_serve)
    return seen


def test_web_defaults_to_loopback(monkeypatch):
    seen = _capture_serve(monkeypatch)
    assert cli.cmd_web([]) == 0
    assert seen == {"host": "127.0.0.1", "port": 8080}


def test_web_host_and_port_opt_in_pass_through(monkeypatch):
    seen = _capture_serve(monkeypatch)
    assert cli.cmd_web(["--host", "0.0.0.0", "--port", "9090"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 9090}


def test_web_rejects_unknown_options():
    assert cli.cmd_web(["--nonsense"]) == 2


def test_install_is_gone(monkeypatch, capsys):
    """SAFE-01: the port-addressed write path must not exist (audit §3.7).

    `install` resolved /dev/<slot> and called mflasher.install_firmware directly: no
    flash-id match, no backup, no lock (it could run beside the web server's write), no
    audit log, no sha check, no confirmation. The panel path through guided.flash is the
    only writer now, and the command refuses like any other unknown one.
    """
    import fm1_switch.mflasher as mflasher

    def boom(*_a, **_k):
        raise AssertionError("the unguarded install write path ran")

    monkeypatch.setattr(mflasher, "install_firmware", boom)
    assert cli.main(["install", "fm1-a", "x.fwsc"]) == 2
    assert "unknown command" in capsys.readouterr().err
    # ...and no docstring or usage text tells anyone to try it
    for text in (cli.USAGE, cli.__doc__ or ""):
        assert "fm1-switch install" not in text
