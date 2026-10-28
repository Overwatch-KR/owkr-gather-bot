"""Exercise real shell orchestration with a fixed clock and no Docker daemon."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize(('clock', 'is_open'), [
    ('1 00', True), ('1 01', True), ('1 02', False),
    ('1 19', False), ('1 20', True), ('1 23', True),
    ('2 02', False), ('3 19', False), ('4 02', False),
    ('4 20', True), ('5 02', True), ('5 12', True),
    ('6 12', True), ('7 23', True),
])
@pytest.mark.parametrize('running', [False, True])
def test_reconciles_hours_without_duplicate_start_or_stop(tmp_path, clock, is_open, running):
    calls, result = run_schedule(tmp_path, clock, running)
    assert result.returncode == 0, result.stderr
    expected = ['compose ps --status running -q bot']
    if is_open and not running:
        expected.append('compose up -d --no-build bot')
    elif not is_open and running:
        expected.append('compose stop bot')
    assert calls == expected


@pytest.mark.parametrize('clock', ['', '8 20', '1 24', 'garbage', '1 20 extra'])
def test_invalid_clock_preserves_current_state(tmp_path, clock):
    calls, result = run_schedule(tmp_path, clock, True)
    assert result.returncode == 2
    assert calls == []


def run_schedule(tmp_path, clock, running):
    repo = tmp_path / 'repo'
    scripts = repo / 'scripts'
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / 'scripts/operating-hours.sh'
    shutil.copy2(source, scripts / source.name)
    bin_path = tmp_path / 'bin'
    bin_path.mkdir()
    date = bin_path / 'date'
    date.write_text('#!/bin/sh\n[ "$TZ" = Asia/Seoul ] || exit 1\nprintf "%s\\n" "$OWKR_TEST_CLOCK"\n')
    docker = bin_path / 'docker'
    docker.write_text('''#!/bin/sh
[ "$PWD" = "$OWKR_TEST_REPO" ] || exit 1
printf '%s\\n' "$*" >> "$OWKR_TEST_CALLS"
if [ "$*" = 'compose ps --status running -q bot' ] && [ "$OWKR_TEST_RUNNING" = yes ]; then
    echo existing-container
fi
exit 0
''')
    date.chmod(0o755)
    docker.chmod(0o755)
    log = tmp_path / 'calls'
    env = dict(os.environ, PATH=f'{bin_path}:{os.environ["PATH"]}',
               OWKR_TEST_CLOCK=clock, OWKR_TEST_REPO=str(repo),
               OWKR_TEST_CALLS=str(log), OWKR_TEST_RUNNING='yes' if running else 'no')
    result = subprocess.run(['/bin/sh', str(scripts / source.name)], env=env,
                            capture_output=True, text=True, check=False)
    return log.read_text().splitlines() if log.exists() else [], result
