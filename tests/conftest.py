"""Every test runs without the network, whatever the environment.

Station's defaults point at the real key directory (`secrets.dir`), and the loop now calls
paid APIs by itself (memory indexing on sleep, recall on `focus new`). Without this, a test
run outside `unshare -rn` could spend real money with real keys. Tests that need an API
replace `urllib.request.urlopen` with a fake (monkeypatch), which takes precedence.
"""

import socket
import urllib.request

import pytest


class NetworkBlocked(OSError):
    pass


def _refuse(*args, **kwargs):
    raise NetworkBlocked("tests never use the network (tests/conftest.py)")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    real_connect = socket.socket.connect

    def connect(self, address):
        if self.family == socket.AF_UNIX:  # local sockets (e.g. the browser wrapper) are fine
            return real_connect(self, address)
        _refuse()

    monkeypatch.setattr(socket.socket, "connect", connect)
    # The production pause between rounds (pace.gap_seconds, 60 s) would make every loop test slow.
    from station import config as config_mod

    monkeypatch.setitem(config_mod.DEFAULTS["pace"], "gap_seconds", 0)
    # Never start the real Claude Code CLI (it would use a real subscription login): only an
    # explicit fake given by absolute path in claude_code.command may run.
    from station import claude_code

    real_resolve = claude_code.resolve_command
    monkeypatch.setattr(claude_code, "resolve_command",
                        lambda command, env: real_resolve(command, env) if command and command.startswith("/")
                        and "/.local/bin/" not in command else None)
