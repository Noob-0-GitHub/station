"""Secrets: one dotenv per service, environment first, re-read on every lookup, unsafe files refused."""

import os

import pytest

from station import secrets, web


def write(path, text, mode=0o600):
    path.write_text(text)
    os.chmod(path, mode)
    return path


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    for name in ("TAVILY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    d = tmp_path / "auth"
    d.mkdir(mode=0o700)
    return d


def test_one_file_per_service(keydir, monkeypatch):
    write(keydir / "tavily.env", "# comment\nTAVILY_API_KEY=tvly-file\n")
    write(keydir / "openai.env", "OPENAI_API_KEY=\n")
    cfg = {"secrets": {"dir": str(keydir)}}
    assert secrets.get(cfg, "TAVILY_API_KEY") == "tvly-file"
    with pytest.raises(secrets.SecretMissing):
        secrets.get(cfg, "OPENAI_API_KEY")  # empty value counts as missing
    write(keydir / "openai.env", "export OPENAI_API_KEY='sk-proj-x'\n")
    assert secrets.get(cfg, "OPENAI_API_KEY") == "sk-proj-x"  # no restart needed
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env")
    assert secrets.get(cfg, "TAVILY_API_KEY") == "tvly-env"


def test_unsafe_files_and_duplicates_are_refused(keydir, tmp_path):
    write(keydir / "tavily.env", "TAVILY_API_KEY=a\n", mode=0o644)
    with pytest.raises(secrets.SecretError, match="chmod 600"):
        secrets.load(keydir)
    os.chmod(keydir / "tavily.env", 0o600)
    write(keydir / "other.env", "TAVILY_API_KEY=b\n")
    with pytest.raises(secrets.SecretError, match="both"):
        secrets.load(keydir)
    (keydir / "other.env").unlink()
    target = write(tmp_path / "elsewhere", "OPENAI_API_KEY=c\n")
    os.symlink(target, keydir / "openai.env")
    with pytest.raises(secrets.SecretError, match="regular file"):
        secrets.load(keydir)


def test_web_without_key(keydir, tmp_path):
    from station.config import DEFAULTS
    from station.ledger import Ledger

    cfg = {**DEFAULTS, "secrets": {"dir": str(keydir)},
           "credits": {**DEFAULTS["credits"], "allowances": {"daily": 3, "weekly": 20}}}
    with pytest.raises(web.WebError, match="not configured"):
        web.search(cfg, Ledger(tmp_path / "ledger.db"), "q")
