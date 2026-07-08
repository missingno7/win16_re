import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ppython._env  # noqa: E402,F401  (dos_re on sys.path)


# --- microman opt-in --------------------------------------------------------
# SimAnt is the sole target.  The other games (microman/ppython) were only
# preparation to harden the win16 framework on something simple-and-known;
# their tests are NOT collected by default (microman's boot test alone costs
# ~7 min and can break without blocking SimAnt work).  They stay available as
# an occasional cross-game regression check on shared win16/api code:
#     pytest microman/tests --run-microman      (or RUN_MICROMAN=1 pytest ...)
def _microman_enabled(config) -> bool:
    return bool(config.getoption("--run-microman")
                or os.environ.get("RUN_MICROMAN"))


def pytest_addoption(parser):
    parser.addoption("--run-microman", action="store_true", default=False,
                     help="collect the microman (non-SimAnt) game tests")


def pytest_ignore_collect(collection_path, config):
    # collection_path is a pathlib.Path (pytest >= 8).
    if "microman" in collection_path.parts and not _microman_enabled(config):
        return True
    return None
