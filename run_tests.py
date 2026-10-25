"""Run all companion tests without writing to the real per-user TUI settings.

Each module gets its own interpreter: legacy tests install a deliberately small
fake setup module, while official-family tests inspect the actual installation.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temp-dir', type=Path, help='Parent directory for isolated test homes')
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    settings = Path.home() / '.strata-tui.json'
    original = settings.read_bytes() if settings.exists() else None
    total = 0
    failed = []
    with tempfile.TemporaryDirectory(prefix='strata-tui-tests-', dir=args.temp_dir) as temporary:
        env = dict(os.environ, USERPROFILE=temporary, HOME=temporary,
                   TEMP=temporary, TMP=temporary, TMPDIR=temporary, PYTHONIOENCODING='utf-8')
        commands: list[tuple[str, ...]] = [('-m', 'unittest', name, '-v') for name in
                    ('test_vram', 'test_merged_load', 'test_official_families', 'test_deepseek_settings', 'test_deepseek_bench')]
        commands.append(('test_ui_repaint.py',))
        for command in commands:
            result = subprocess.run([sys.executable, *command], cwd=here, env=env,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace')
            output = result.stdout + result.stderr
            print(output, end='' if output.endswith('\n') else '\n')
            counts = re.findall(r'Ran (\d+) tests?', output)
            total += sum(map(int, counts))
            if command == ('test_ui_repaint.py',) and 'regression: PASS' in output:
                total += 1
            if result.returncode:
                failed.append(command)
    after = settings.read_bytes() if settings.exists() else None
    if after != original:
        print('ERROR: real TUI settings changed during testing; no automatic overwrite was attempted.')
        return 1
    digest = hashlib.sha256(after).hexdigest() if after is not None else 'absent'
    print(f'Tests exercised: {total}; failing commands: {len(failed)}')
    print(f'Real TUI settings unchanged: {digest}')
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
