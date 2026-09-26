import sys
from pathlib import Path

from coindata.cli import main


def _run_gui(config: Path | None) -> int:
    from coindata.gui import run_gui  # tkinter는 GUI를 쓸 때만 읽는다

    return run_gui(config)


sys.exit(main(gui_runner=_run_gui))
