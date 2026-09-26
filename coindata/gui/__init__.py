"""GUI 진입점 (PRD 10.8).

`python -m coindata gui`, 설치 시 `coindata-gui`, 단일 실행 파일(FR-8.9)이 모두 `main`/`run_gui`로 들어온다.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from coindata.cli import service
from coindata.config import ConfigError
from coindata.gui.worker import QueueLogHandler, Worker


def _setup_logging(worker: Worker, level: str) -> None:
    """로그를 화면 하단 칸으로 보낸다. 콘솔이 있으면 표준 오류에도 쓴다(C-6)."""
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handlers: list[logging.Handler] = [QueueLogHandler(worker)]
    if sys.stderr is not None:  # 콘솔 없는 실행 파일에서는 표준 오류가 없다
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level.upper())


def run_gui(config_path: Path | None) -> int:
    import tkinter as tk
    from tkinter import messagebox

    from coindata.gui.app import App

    root = tk.Tk()
    try:
        config, paths = service.load(config_path)
    except ConfigError as exc:
        root.withdraw()
        messagebox.showerror("설정 오류", str(exc))
        root.destroy()
        return service.EXIT_CANNOT_RUN
    worker = Worker()
    _setup_logging(worker, config.runtime.log_level)
    App(root, config, paths, service.default_runtime(), worker)
    root.mainloop()
    return service.EXIT_OK


def main() -> int:
    """`coindata-gui`와 실행 파일의 진입점. 설정은 실행 폴더의 coindata.toml(없으면 기본값)."""
    return run_gui(None)
