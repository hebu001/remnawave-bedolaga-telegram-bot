"""Do not let the repository-wide legacy F821/F823 ignores hide live-path regressions."""

import subprocess
import sys
from pathlib import Path


def test_changed_runtime_paths_have_no_undefined_or_unbound_names():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed local Ruff command and repository paths, no user input
        [
            sys.executable,
            '-m',
            'ruff',
            'check',
            '--isolated',
            '--select',
            'F821,F823',
            'app/handlers/menu.py',
            'app/database/crud/notification.py',
            'app/services/monitoring_service.py',
            'app/cabinet/routes/subscription_modules/devices.py',
            'app/external/remnawave_api.py',
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
