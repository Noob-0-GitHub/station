"""Codex token file (production) and the root-side sync. No network, no Hermes, fake tokens only."""

import base64
import importlib.util
import json
import os
import pwd
import time
from pathlib import Path

import pytest

from station import codex_auth
from station.codex_auth import CodexAuthError, FileCodexCredential, make_credential, parse_token_file
from station.llm import Codex, LLMError

BASE = "https://chatgpt.com/backend-api/codex"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "codex-token-sync.py"


def fake_jwt(exp: float) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': int(exp)})}.sig"


def cred(hours=8.0, base=BASE, headers=None):
    exp = time.time() + hours * 3600
    return {"token": fake_jwt(exp), "exp": exp, "base_url": base, "headers": headers or {"originator": "test"}}


def load_sync():
    spec = importlib.util.spec_from_file_location("codex_token_sync", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def me():
    return pwd.getpwuid(os.geteuid()).pw_name


@pytest.fixture
def auth(tmp_path):
    d = tmp_path / "auth"
    d.mkdir(mode=0o700)
    return d


def test_file_roundtrip_and_reload(auth, me):
    sync = load_sync()
    first = cred()
    assert sync.sync(auth / "codex.json", me, first) == "updated"
    assert oct(os.stat(auth / "codex.json").st_mode & 0o777) == "0o600"
    reader = FileCodexCredential(auth / "codex.json")
    got = reader.get()
    assert got["token"] == first["token"] and got["base_url"] == BASE and got["headers"] == {"originator": "test"}
    assert sync.sync(auth / "codex.json", me, first) == "unchanged"
    second = cred(hours=9)
    assert sync.sync(auth / "codex.json", me, second, check=True) == "would update"
    assert reader.get()["token"] == first["token"]  # check wrote nothing
    assert sync.sync(auth / "codex.json", me, second) == "updated"
    assert reader.get()["token"] == second["token"]  # picked up without a restart
    assert not [p for p in auth.iterdir() if p.name.startswith(".")]  # no temp files left


def test_reader_refuses_unsafe_or_bad_files(auth, tmp_path):
    path = auth / "codex.json"
    reader = FileCodexCredential(path)
    with pytest.raises(CodexAuthError, match="does not exist"):
        reader.get()
    good = load_sync().render(cred())
    path.write_text(good)
    os.chmod(path, 0o644)
    with pytest.raises(CodexAuthError, match="chmod 600"):
        reader.get()
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(good)
    os.chmod(target, 0o600)
    path.symlink_to(target)
    with pytest.raises(CodexAuthError, match="cannot open"):
        reader.get()


@pytest.mark.parametrize("change, error", [
    ({"base_url": "https://evil.example/backend-api/codex"}, "official"),
    ({"base_url": "http://chatgpt.com/backend-api/codex"}, "official"),
    ({"headers": {"Authorization": "Bearer x"}}, "Authorization"),
    ({"version": 2}, "version"),
    ({"access_token": ""}, "access_token"),
])
def test_parse_rejects(change, error):
    body = json.loads(load_sync().render(cred()))
    body.update(change)
    with pytest.raises(CodexAuthError, match=error):
        parse_token_file(json.dumps(body))


def test_expired_token_is_refused_and_errors_hide_the_token():
    c = cred(hours=-1)
    text = load_sync().render({**c, "exp": c["exp"]})
    with pytest.raises(CodexAuthError) as e:
        parse_token_file(text)
    assert "expired" in str(e.value) and c["token"] not in str(e.value)
    with pytest.raises(CodexAuthError) as e:
        parse_token_file(text.replace('"version": 1', '"version": "x"'))
    assert c["token"] not in str(e.value)


def test_sync_refuses_to_publish_bad_credentials(auth, me):
    sync = load_sync()
    with pytest.raises(CodexAuthError, match="official"):
        sync.sync(auth / "codex.json", me, cred(base="https://proxy.example/v1"))
    assert not (auth / "codex.json").exists()


def test_sync_refuses_symlinked_or_open_directories(tmp_path, me):
    sync = load_sync()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SystemExit, match="without following links"):
        sync.sync(link / "codex.json", me, cred())
    os.chmod(real, 0o755)
    with pytest.raises(SystemExit, match="mode 700"):
        sync.sync(real / "codex.json", me, cred())
    assert not list(real.iterdir())


def test_sync_replaces_a_planted_symlink_without_touching_its_target(auth, tmp_path, me):
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (auth / "codex.json").symlink_to(victim)
    assert load_sync().sync(auth / "codex.json", me, cred()) == "updated"
    assert victim.read_text() == "keep\n"
    assert not (auth / "codex.json").is_symlink()


def test_config_selects_the_source(tmp_path):
    assert isinstance(make_credential({"codex": {"token_file": str(tmp_path / "c.json")}}, "m"), FileCodexCredential)
    assert isinstance(make_credential({"codex": {"hermes_root": "/x", "hermes_home": "/y"}}, "m"),
                      codex_auth.HermesCodexCredential)


def test_missing_file_surfaces_as_llm_error(tmp_path):
    client = Codex({"model": "gpt-test", "codex": {"token_file": str(tmp_path / "none.json")}})
    with pytest.raises(LLMError, match="codex auth: .*does not exist"):
        client.call([{"type": "text", "text": "s"}], [], [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])


def test_init_points_production_at_the_token_file(tmp_path):
    import yaml

    from station.cli import main

    root = tmp_path / ".station"
    main(["--root", str(root), "init", "--home", str(tmp_path)])
    cfg = yaml.safe_load((root / "station.yaml").read_text())
    assert cfg["codex"]["token_file"] == str(root.resolve() / "auth" / "codex.json")
