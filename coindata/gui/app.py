"""한 화면 GUI (PRD 10.8, FR-8.3 ~ FR-8.6).

모든 동작은 `coindata.cli.service`의 함수로 한다. 계산, 저장소 직접 접근, 외부 요청을 하지 않는다(FR-8.2).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from coindata.cli import service
from coindata.cli.plan import PlanAddOutcome
from coindata.cli.service import CommandError, Overview, Paths, Runtime, SummaryEntry
from coindata.cli.status import plan_state
from coindata.cli.summary import SummaryError, parse_at
from coindata.config import Config
from coindata.gui.paste import PasteError, Prepared, prepare
from coindata.gui.worker import Worker
from coindata.models import AtRegistration, PlanRecord, RunMode

logger = logging.getLogger(__name__)

PAD = 4


def fmt(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def describe_outcomes(outcomes: list[PlanAddOutcome], prepared: Prepared, dry_run: bool) -> str:
    """검증·등록 결과 문구. 붙인 plan_id, 필드별 오류, 등록 시 계산값(FR-7.5)."""
    lines = []
    if prepared.assigned:
        lines.append("자동으로 붙인 plan_id: " + ", ".join(f"plans[{i}] → {pid}" for i, pid in prepared.assigned))
    for o in outcomes:
        if o.errors:
            lines.append(f"✗ {o.plan_key}")
            lines += [f"    - {e}" for e in o.errors]
            continue
        lines.append(("✓ 등록 가능: " if dry_run else "✓ 등록: ") + o.plan_key)
        if o.at_registration is not None:
            lines += ["    " + line for line in describe_registration(o.at_registration)]
    return "\n".join(lines)


def describe_registration(r: AtRegistration) -> list[str]:
    lines = [
        f"risk {_num(r.risk_bp)}bp ({_num(r.risk_atr, 3)} ATR), reward {_num(r.reward_bp)}bp ({_num(r.reward_atr, 3)} ATR)",
        f"activation까지 {_num(r.activation_distance_bp)}bp, 기준 요약 {fmt(r.source_ref_time)} @ {r.source_ref_price}",
    ]
    n = r.nearest_opposing_level
    if n is None:
        lines.append("가장 가까운 반대 레벨: 없음")
    else:
        inside = ", activation이 구간 안" if n.activation_inside_zone else ""
        lines.append(
            f"가장 가까운 반대 레벨 {n.level_id}: 경계 {_num(n.boundary)}, {_num(n.distance_bp)}bp ({_num(n.distance_atr, 3)} ATR){inside}"
        )
    if r.registration_lag_minutes:
        lines.append(f"등록 지연 {r.registration_lag_minutes}분")
    if r.params_changed_since_source:
        lines.append("기준 요약 이후 파라미터가 바뀌었다")
    return lines


@dataclass(frozen=True, slots=True)
class Views:
    overview: Overview | None
    overview_error: str | None
    summaries: list[SummaryEntry]
    plans: list[PlanRecord]


class App:
    def __init__(self, root: tk.Tk, config: Config, paths: Paths, runtime: Runtime, worker: Worker) -> None:
        self.root = root
        self.config = config
        self.paths = paths
        self.runtime = runtime
        self.worker = worker
        self.summaries: list[SummaryEntry] = []
        self.plans: list[PlanRecord] = []
        self.prepared: Prepared | None = None  # 검증을 통과한 입력. 글을 고치면 버린다
        self.action_buttons: list[ttk.Button] = []

        root.title("coindata")
        root.geometry("1280x800")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        root.after(self.config.gui.poll_ms, self._poll)
        self.refresh()

    # ------------------------------------------------------------------ 배치

    def _button(self, parent: tk.Widget, text: str, command: Callable[[], None], action: bool = True) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side=tk.LEFT, padx=PAD)
        if action:
            self.action_buttons.append(button)
        return button

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=PAD)
        top.pack(fill=tk.X)
        config_text = str(self.paths.config_path) if self.paths.config_path else "기본값 (설정 파일 없음)"
        ttk.Label(top, text=f"설정: {config_text}    저장소: {self.paths.db_path}").pack(side=tk.LEFT)
        bar = ttk.Frame(self.root, padding=PAD)
        bar.pack(fill=tk.X)
        self.overview_label = ttk.Label(bar, text="")
        self.overview_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._button(bar, "동기화", self.on_sync)
        self._button(bar, "초기 적재…", self.on_init)
        self._button(bar, "상태 보기", self.on_status)

        bottom = ttk.LabelFrame(self.root, text="진행과 로그", padding=PAD)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)  # 본문보다 먼저 배치해야 창이 작아져도 남는다
        self.log = _scrolled_text(bottom, height=5, state=tk.DISABLED, wrap=tk.NONE)

        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        body.add(self._build_summary(body), weight=1)
        body.add(self._build_plans(body), weight=1)

    def _build_summary(self, parent: tk.Widget) -> ttk.Frame:
        frame = ttk.LabelFrame(parent, text="요약", padding=PAD)
        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        self._button(row, "요약 실행", self.on_summary)
        ttk.Label(row, text="과거 시점(UTC)").pack(side=tk.LEFT, padx=PAD)
        self.at_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.at_var, width=18).pack(side=tk.LEFT)
        self.full_params = tk.BooleanVar()
        self.compact = tk.BooleanVar()
        ttk.Checkbutton(row, text="전체 파라미터", variable=self.full_params).pack(side=tk.LEFT, padx=PAD)
        ttk.Checkbutton(row, text="압축", variable=self.compact).pack(side=tk.LEFT)

        self.summary_tree = ttk.Treeview(frame, columns=("id", "trigger", "ref"), show="headings", height=8)
        for column, title, width in (("id", "요약 ID", 170), ("trigger", "종류", 90), ("ref", "기준 시각(UTC)", 150)):
            self.summary_tree.heading(column, text=title)
            self.summary_tree.column(column, width=width, anchor=tk.W)
        self.summary_tree.pack(fill=tk.X, pady=PAD)
        self.summary_tree.bind("<<TreeviewSelect>>", lambda _e: self.on_summary_selected())

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        self._button(buttons, "복사", lambda: self.on_copy(False), action=False)
        self._button(buttons, "압축 복사", lambda: self.on_copy(True), action=False)
        self._button(buttons, "폴더 열기", self.on_open_folder, action=False)

        self.preview = _scrolled_text(frame, wrap=tk.NONE, state=tk.DISABLED, expand=True)
        return frame

    def _build_plans(self, parent: tk.Widget) -> ttk.Frame:
        frame = ttk.LabelFrame(parent, text="계획", padding=PAD)
        ttk.Label(frame, text="plan/1 JSON 붙여넣기 (판단 모델 답변의 ```json 블록째 붙여넣어도 된다)").pack(anchor=tk.W)
        self.paste = _scrolled_text(frame, height=10, wrap=tk.NONE)
        self.paste.bind("<<Modified>>", self._on_paste_modified)
        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=PAD)
        self._button(row, "검증", self.on_validate)
        self.register_button = self._button(row, "등록", self.on_register)
        self.register_button.state(["disabled"])
        self.result = _scrolled_text(frame, height=9, wrap=tk.WORD, state=tk.DISABLED)

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=PAD)
        self.show_all = tk.BooleanVar()
        ttk.Checkbutton(row, text="종료된 계획 포함", variable=self.show_all, command=self.refresh).pack(side=tk.LEFT)
        self._button(row, "선택 취소", self.on_cancel_plan)
        self.plan_tree = ttk.Treeview(frame, columns=("key", "side", "state", "expires"), show="headings")
        for column, title, width in (
            ("key", "plan_key", 230), ("side", "side", 60), ("state", "상태", 150), ("expires", "만료(UTC)", 130)
        ):
            self.plan_tree.heading(column, text=title)
            self.plan_tree.column(column, width=width, anchor=tk.W)
        self.plan_tree.pack(fill=tk.BOTH, expand=True)
        return frame

    # ------------------------------------------------------------------ 공통

    def _poll(self) -> None:
        self.worker.drain(self.append_log)
        self._sync_buttons()
        self.root.after(self.config.gui.poll_ms, self._poll)

    def _sync_buttons(self) -> None:
        busy = self.worker.busy
        for button in self.action_buttons:
            if button is self.register_button:
                button.state(["disabled"] if busy or self.prepared is None else ["!disabled"])
            else:
                button.state(["disabled"] if busy else ["!disabled"])

    def append_log(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state=tk.DISABLED)

    def run(self, name: str, job: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        """작업 스레드에서 실행한다. 실행 중이면 알린다(FR-8.4)."""

        def on_error(exc: BaseException) -> None:
            text = str(exc) if isinstance(exc, (CommandError, PasteError)) else f"{name} 실패: {exc!r}"
            self.append_log(text)
            messagebox.showerror(name, text, parent=self.root)

        if not self.worker.submit(name, job, on_done, on_error):
            messagebox.showinfo(name, "다른 작업이 실행 중이다.", parent=self.root)
            return
        self.append_log(f"{name} 시작")
        self._sync_buttons()

    # ------------------------------------------------------------------ 목록 갱신

    def refresh(self) -> None:
        show_all = self.show_all.get()

        def job() -> Views:
            try:
                ov: Overview | None = service.overview(self.config, self.paths)
                error = None
            except CommandError as exc:
                ov, error = None, str(exc)
            if ov is None:
                return Views(None, error, [], [])
            return Views(
                ov, None, service.recent_summaries(self.config, self.paths),
                service.stored_plans(self.config, self.paths, show_all),
            )

        if not self.worker.submit("목록 갱신", job, self._show_views, lambda exc: self.append_log(f"목록 갱신 실패: {exc}")):
            self.root.after(self.config.gui.poll_ms, self.refresh)

    def _show_views(self, views: Views) -> None:
        if views.overview is None:
            self.overview_label.configure(text=views.overview_error or "")
        else:
            parts = [f"{d.dataset.value} {fmt(d.last_ms)}" for d in views.overview.datasets]
            self.overview_label.configure(text="마지막 봉: " + " · ".join(parts) + f"   미해소 결측 {views.overview.open_gap_count}건")
        selected = self._selected_summary()
        self.summaries = views.summaries
        self.summary_tree.delete(*self.summary_tree.get_children())
        for i, entry in enumerate(self.summaries):
            name = entry.record.summary_id + ("" if entry.exists else "  (파일 없음)")
            self.summary_tree.insert("", tk.END, iid=str(i), values=(name, entry.record.trigger.value, fmt(entry.record.ref_time)))
            if selected is not None and entry.record.summary_id == selected.record.summary_id:
                self.summary_tree.selection_set(str(i))
        self.plans = views.plans
        self.plan_tree.delete(*self.plan_tree.get_children())
        for i, record in enumerate(self.plans):
            self.plan_tree.insert(
                "", tk.END, iid=str(i),
                values=(record.spec.plan_key, record.spec.side, plan_state(record).value, fmt(record.expires_at)),
            )

    # ------------------------------------------------------------------ 상단

    def on_sync(self) -> None:
        self.run(
            "동기화",
            lambda: service.run_ingest(self.config, self.paths, self.runtime, RunMode.SYNC, self.config.data.init_days),
            self._ingest_done,
        )

    def on_init(self) -> None:
        days = simpledialog.askinteger(
            "초기 적재", "적재 기간(일)", initialvalue=self.config.data.init_days, minvalue=1, parent=self.root
        )
        if days is None:
            return
        self.run(
            "초기 적재",
            lambda: service.run_ingest(self.config, self.paths, self.runtime, RunMode.INIT, days, self.worker.message),
            self._ingest_done,
        )

    def _ingest_done(self, outcome: service.IngestOutcome) -> None:
        changed = ", ".join(f"{name} {item.rows_changed}" for name, item in outcome.report.datasets.items())
        state = "정상" if outcome.exit_code == service.EXIT_OK else f"부분 실패 (실패 {len(outcome.report.failures)}건, 로그 참조)"
        self.append_log(f"{outcome.mode.value} {state}: 적재 {changed}")
        self.refresh()

    def on_status(self) -> None:
        self.run("상태 보기", lambda: service.status_report(self.config, self.paths), self._show_status)

    def _show_status(self, text: str) -> None:
        window = tk.Toplevel(self.root)
        window.title("저장소 상태")
        widget = tk.Text(window, wrap=tk.NONE, width=130, height=40, font=("Courier", 10))
        widget.pack(fill=tk.BOTH, expand=True)
        self._set_text(widget, text)

    # ------------------------------------------------------------------ 요약

    def on_summary(self) -> None:
        at_text = self.at_var.get().strip()
        try:
            at_ms = parse_at(at_text) if at_text else None
        except SummaryError as exc:
            messagebox.showerror("요약", str(exc), parent=self.root)
            return
        full, compact = self.full_params.get(), self.compact.get()
        self.run(
            "요약",
            lambda: service.make_summary(self.config, self.paths, self.runtime, at_ms, full, compact),
            self._summary_done,
        )

    def _summary_done(self, result: Any) -> None:
        note = f" (부분 실패: 취득 실패 {len(result.failures)}건, 요약의 gaps 참조)" if result.partial else ""
        self.append_log(f"요약 저장: {result.path}{note}")
        self._set_text(self.preview, _read_or_error(result.path, False))
        self.refresh()

    def _selected_summary(self) -> SummaryEntry | None:
        selection = self.summary_tree.selection()
        return self.summaries[int(selection[0])] if selection else None

    def on_summary_selected(self) -> None:
        entry = self._selected_summary()
        if entry is not None:
            self._set_text(self.preview, _read_or_error(entry.path, False) if entry.exists else f"파일 없음: {entry.path}")

    def on_copy(self, compact: bool) -> None:
        entry = self._selected_summary()
        if entry is None or not entry.exists:
            messagebox.showinfo("복사", "파일이 있는 요약을 선택하라.", parent=self.root)
            return
        try:
            text = service.summary_text(entry.path, compact)
        except CommandError as exc:
            messagebox.showerror("복사", str(exc), parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.append_log(f"복사했다: {entry.record.summary_id}" + (" (압축)" if compact else ""))

    def on_open_folder(self) -> None:
        folder = self.paths.output_dir
        if not folder.exists():
            messagebox.showinfo("폴더 열기", f"아직 폴더가 없다: {folder}", parent=self.root)
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror("폴더 열기", str(exc), parent=self.root)

    # ------------------------------------------------------------------ 계획

    def _on_paste_modified(self, _event: object) -> None:
        if self.paste.edit_modified():
            self.prepared = None  # 검증한 뒤 글이 바뀌면 다시 검증해야 한다
            self.paste.edit_modified(False)
            self._sync_buttons()

    def on_validate(self) -> None:
        text = self.paste.get("1.0", tk.END)

        def job() -> tuple[Prepared, list[PlanAddOutcome]]:
            prepared = prepare(text, lambda source: service.plan_ids_in_use(self.config, self.paths, source))
            return prepared, service.plan_add(self.config, self.paths, self.runtime.clock, prepared.text, dry_run=True)

        self.prepared = None
        self.run("검증", job, self._validated)

    def _validated(self, value: tuple[Prepared, list[PlanAddOutcome]]) -> None:
        prepared, outcomes = value
        self._set_text(self.result, describe_outcomes(outcomes, prepared, dry_run=True))
        self.prepared = prepared if outcomes and all(not o.errors for o in outcomes) else None

    def on_register(self) -> None:
        prepared = self.prepared
        if prepared is None:
            return

        def job() -> list[PlanAddOutcome]:
            return service.plan_add(self.config, self.paths, self.runtime.clock, prepared.text, dry_run=False)

        def done(outcomes: list[PlanAddOutcome]) -> None:
            self._set_text(self.result, describe_outcomes(outcomes, prepared, dry_run=False))
            self.prepared = None
            self.refresh()

        self.run("등록", job, done)

    def on_cancel_plan(self) -> None:
        selection = self.plan_tree.selection()
        if not selection:
            messagebox.showinfo("취소", "취소할 계획을 선택하라.", parent=self.root)
            return
        key = self.plans[int(selection[0])].spec.plan_key
        if not messagebox.askokcancel("취소", f"{key}를 취소한다. pending 계획만 취소된다.", parent=self.root):
            return

        def done(_value: None) -> None:
            self.append_log(f"취소했다: {key}")
            self.refresh()

        self.run("취소", lambda: service.plan_cancel(self.config, self.paths, self.runtime.clock, key), done)

    # ------------------------------------------------------------------ 종료

    def on_close(self) -> None:
        if self.worker.busy and not messagebox.askokcancel(
            "종료", "작업이 실행 중이다. 지금 닫으면 작업이 중단된다. 적재는 다음 실행에서 이어진다. 닫을까?",
            parent=self.root,
        ):
            return
        self.root.destroy()


def _scrolled_text(parent: tk.Widget, expand: bool = False, **options: Any) -> tk.Text:
    frame = ttk.Frame(parent)
    frame.pack(fill=tk.BOTH if expand else tk.X, expand=expand, pady=PAD)
    bar = ttk.Scrollbar(frame, orient=tk.VERTICAL)
    text = tk.Text(frame, yscrollcommand=bar.set, **options)
    bar.configure(command=text.yview)
    bar.pack(side=tk.RIGHT, fill=tk.Y)
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return text


def _read_or_error(path: Any, compact: bool) -> str:
    try:
        return service.summary_text(path, compact)
    except CommandError as exc:
        return str(exc)
