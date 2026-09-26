"""진입점. `python -m coindata`와 설치 명령 `coindata`가 여기로 들어온다.

`cli`는 `gui`를 참조하지 않으므로(7.2) GUI 실행기는 여기서 넘긴다.
"""

import sys
from pathlib import Path

from coindata.cli import main


def _run_gui(config: Path | None) -> int:
    from coindata.gui import run_gui  # tkinter는 GUI를 쓸 때만 읽는다

    return run_gui(config)


def run() -> int:
    return main(gui_runner=_run_gui)


if __name__ == "__main__":
    sys.exit(run())
