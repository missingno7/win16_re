import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import win16._env  # noqa: E402,F401  (puts the dos_re submodule on sys.path)
