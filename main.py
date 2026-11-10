#!/usr/bin/env python3
"""入口垫片：把 src/ 加入 sys.path，然后交给 scheduler_agent.cli。

这样评审不需要安装包、不需要设 PYTHONPATH：
    python main.py --input examples/a1-normal.input.json
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from scheduler_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
