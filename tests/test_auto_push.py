"""Exercise the opt-in hook only against local bare repositories, never GitHub."""

import os
from pathlib import Path
import shutil
import subprocess


HOOK = Path(__file__).resolve().parents[1] / '.githooks/post-commit'


def test_post_commit_push_and_failed_push_keep_local_commit(tmp_path):
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0'}
    def run(*args, cwd=None, check=True):
        return subprocess.run(['git', *args], cwd=cwd, env=env, check=check,
                              capture_output=True, text=True, timeout=30)
    remote, work = tmp_path / 'remote.git', tmp_path / 'work'
    run('init', '--bare', str(remote))
    run('init', '-b', 'master', str(work))
    run('config', 'user.name', 'Offline Hook Test', cwd=work)
    run('config', 'user.email', 'test@localhost', cwd=work)
    # Raw origin URL is checked by the hook. Push-only rewrite keeps this test fully local.
    run('remote', 'add', 'origin', 'https://github.com/Noob-0-GitHub/station.git', cwd=work)
    rewrite = f'url.{remote.as_uri()}.pushInsteadOf'
    run('config', rewrite, 'https://github.com/Noob-0-GitHub/station.git', cwd=work)
    hooks = work / '.githooks'
    hooks.mkdir()
    shutil.copy2(HOOK, hooks / 'post-commit')
    (hooks / 'post-commit').chmod(0o755)
    run('config', 'core.hooksPath', '.githooks', cwd=work)
    (work / 'a.txt').write_text('first\n')
    run('add', 'a.txt', cwd=work)
    run('commit', '-m', 'first', cwd=work)
    head = run('rev-parse', 'HEAD', cwd=work).stdout.strip()
    assert run('--git-dir', str(remote), 'rev-parse', 'refs/heads/master').stdout.strip() == head
    assert not (work / '.git/station-push-pending').exists()
    # A missing local destination guarantees failure without network or auth.
    run('config', '--unset', rewrite, cwd=work)
    run('config', f'url.{(tmp_path / "missing.git").as_uri()}.pushInsteadOf',
        'https://github.com/Noob-0-GitHub/station.git', cwd=work)
    (work / 'a.txt').write_text('second\n')
    run('add', 'a.txt', cwd=work)
    result = run('commit', '-m', 'second', cwd=work)
    new = run('rev-parse', 'HEAD', cwd=work).stdout.strip()
    assert new != head and 'FAILED' in result.stderr
    assert new in (work / '.git/station-push-pending').read_text()
    assert run('--git-dir', str(remote), 'rev-parse', 'refs/heads/master').stdout.strip() == head
    # Restore local target: a third commit syncs all pending ancestry; no force-push is needed.
    run('config', '--unset', f'url.{(tmp_path / "missing.git").as_uri()}.pushInsteadOf', cwd=work)
    run('config', rewrite, 'https://github.com/Noob-0-GitHub/station.git', cwd=work)
    (work / 'a.txt').write_text('third\n')
    run('add', 'a.txt', cwd=work)
    run('commit', '-m', 'third', cwd=work)
    final = run('rev-parse', 'HEAD', cwd=work).stdout.strip()
    assert run('--git-dir', str(remote), 'rev-parse', 'refs/heads/master').stdout.strip() == final
    assert not (work / '.git/station-push-pending').exists()
