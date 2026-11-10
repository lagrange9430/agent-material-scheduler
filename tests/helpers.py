"""测试公共设施：路径引导、临时 runtime、构造 Agent。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scheduler_agent.agent import Agent  # noqa: E402
from scheduler_agent.models import Action, MaterialState  # noqa: E402
from scheduler_agent.policy import MockPolicy  # noqa: E402
from scheduler_agent.store import DataStore, DraftStore  # noqa: E402

DATA_DIR = REPO / "data"


def load_input(name: str) -> Dict[str, Any]:
    return json.loads((REPO / "examples" / f"{name}.input.json").read_text(encoding="utf-8"))


def make_agent(runtime_dir: Path, faults: Optional[list] = None, policy: Any = None) -> Agent:
    return Agent(
        policy or MockPolicy(),
        DataStore(DATA_DIR),
        DraftStore(runtime_dir),
        faults=faults or [],
    )


def run_input(name: str, runtime_dir: Path, faults: Optional[list] = None) -> Dict[str, Any]:
    agent = make_agent(runtime_dir, faults)
    return agent.run(load_input(name)).payload


class TempRuntimeMixin:
    """每个测试一个干净 runtime，避免草稿文件互相污染。"""

    def setUp(self) -> None:  # noqa: D401
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


def make_state(spec: Dict[str, Any]) -> MaterialState:
    return MaterialState(dict(spec))


def tool_action(name: str, **args: Any) -> Action:
    return Action(name, args, "test")
