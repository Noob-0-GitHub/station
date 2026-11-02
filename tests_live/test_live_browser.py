"""The seeded ~/bin/browser wrapper, end to end: open a page, read it, screenshot, close."""

import os
import subprocess

import pytest

from conftest import need


def test_browser_wrapper_opens_reads_and_closes(runtime):
    from station.config import DEFAULTS
    from station.loop import Life

    need(DEFAULTS["browser"]["binary"])
    life = Life(runtime, echo=lambda *_: None)
    life.init_layout()
    life.seed_home()
    wrapper = life.home / "bin" / "browser"
    assert os.access(wrapper, os.X_OK)
    env = {"HOME": str(life.home), "PATH": f"{life.home}/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}

    def browser(*args, timeout=120):
        r = subprocess.run(["browser", *args], cwd=life.home, env=env, capture_output=True, text=True, timeout=timeout)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    try:
        browser("open", "https://example.com/")
        assert "Example Domain" in browser("get", "title")
        snap = browser("snapshot", "-i")
        assert '"Learn more" [ref=e1]' in snap  # refs print as [ref=eN]; commands take them as @eN
        assert "documentation examples" in browser("get", "text", "body")
        # Forms: fill and click by ref, on a local page (no third-party site involved).
        browser("open", "data:text/html,<input name=q><button type=button "
                "onclick=\"document.title=document.querySelector('input').value\">Go</button>")
        refs = browser("snapshot", "-i")
        assert "textbox [ref=e1]" in refs and '"Go" [ref=e2]' in refs
        browser("fill", "@e1", "hello station")
        browser("click", "@e2")
        assert browser("get", "title").strip() == "hello station"
        shot = life.home / "shot.png"
        browser("screenshot", str(shot))
        assert shot.stat().st_size > 1000
        assert (life.home / ".browser-profile").is_dir()  # its own profile, inside the home
    finally:
        subprocess.run(["browser", "close"], cwd=life.home, env=env, capture_output=True, timeout=60)
