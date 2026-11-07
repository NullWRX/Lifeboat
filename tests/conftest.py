"""Point every Lifeboat path at a throwaway folder before the package is imported."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(tempfile.mkdtemp(prefix="lifeboat-tests-"))
for var, sub in (("LIFEBOAT_CONFIG_DIR", "etc"), ("LIFEBOAT_STATE_DIR", "state"), ("LIFEBOAT_RUN_DIR", "run"),
                 ("LIFEBOAT_LOG_DIR", "log"), ("LIFEBOAT_SYSTEMD_DIR", "systemd")):
    os.environ[var] = str(ROOT / sub)
    (ROOT / sub).mkdir(parents=True, exist_ok=True)
os.environ["LIFEBOAT_UDEV_RULES"] = str(ROOT / "udev" / "90-lifeboat.rules")
(ROOT / "udev").mkdir(exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="needs root (ownership and /etc access)")
needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")


@pytest.fixture
def root_dir():
    return ROOT


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(ROOT, ignore_errors=True)
