import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ppython._env  # noqa: E402,F401  (dos_re on sys.path)
