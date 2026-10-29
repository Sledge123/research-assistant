"""中文 Tk 界面；后台任务只能通过队列通知主线程。"""
from __future__ import annotations

from datetime import datetime
from dataclasses import replace
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .numbers import parse_code, parse_codes
from .settings import load_settings, resolve_executable, save_settings
from .mail_settings import MailSettings, load_mail_settings, save_mail_settings, validate_email, validate_mail_settings
from .mailer import MailService


_STATUS_LABELS = {
    "pending": "未开始", "queued": "未开始", "running": "处理中",
    "searching": "正在检索", "exporting": "正在导出", "validating": "正在校验",
    "success": "成功", "succeeded": "成功", "complete": "成功", "completed": "成功",
    "skipped": "已跳过", "skip": "已跳过", "failed": "失败", "error": "失败",
    "stopped": "已停止", "cancelled": "已停止", "canceled": "已停止",
    "not_started": "未开始", "not_found": "未找到",
}
_SUCCESS = {"success", "succeeded", "complete", "completed", "成功"}
_SKIPPED = {"skipped", "skip", "已跳过", "跳过"}
_RETRYABLE = {"failed", "error", "not_found", "stopped", "cancelled", "canceled", "pending",
              "queued", "not_started", "失败", "未找到", "已停止", "停止", "未开始"}
_FINISHED = _SUCCESS | _SKIPPED | _RETRYABLE


def _status(value: object) -> str:
    return str(value or "pending").strip().casefold()


def _retry_text(records: list[dict]) -> str:
    """仅从失败、停止、未开始记录构造重试输入。"""
    return "\n".join(str(record["request"]) for record in records
                     if _status(record.get("status")) in _RETRYABLE)


class HighScoreWindow:
    def __init__(self, root: Path):
        self.project_root = Path(root).resolve()
        self.window = tk.Tk()
        self.window.title("HighScore 离线卡片归档器")
        self.window.geometry("1040x780")
        self.window.minsize(860, 650)
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.closing = False
        self.executable: Path | None = None
        self.records: dict[str, dict] = {}
        self.active_keys: list[str] = []
        self.source_request: dict | None = None
        self.source_dialog: tk.Toplevel | None = None
        self.log_path = self.project_root / "logs" / "gui.log"
        self.local_settings = load_settings(self.project_root)
        self.mail_settings = load_mail_settings(self.project_root)
        self.latest_mail = None
        self.mail_dialog = None
        self._build()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        if self.local_settings.message:
            self.notice_var.set(self.local_settings.message)
            self._append_log(self.local_settings.message)
        if self.mail_settings.message:
            self.mail_status_var.set(self.mail_settings.message)
        try:
            latest = MailService(self.project_root, self.mail_settings).latest_job()
            if latest:
                self._apply_mail_result(latest)
                self._set_running_controls()
        except ValueError:
            self.mail_status_var.set("最近邮件任务读取失败，已禁止自动重试；请保留任务目录排查。")
        configured = resolve_executable(self.project_root, self.local_settings)
        if configured is not None and configured.is_file():
            self._set_executable(configured, persist=False)
        else:
            if configured is not None:
                self._append_log("已保存的程序位置不存在，将尝试自动发现，也可重新选择。")
            self.window.after(50, lambda: self._discover(persist=False, report=False))
        self.window.after(80, self._drain_events)

    def _build(self):
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)
        outer = ttk.Frame(self.window, padding=14)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=3)
        outer.rowconfigure(7, weight=1)

        program = ttk.LabelFrame(outer, text="HighScore 程序", padding=10)
        program.grid(row=0, column=0, sticky="ew")
        program.columnconfigure(0, weight=1)
        self.program_var = tk.StringVar(value="尚未发现程序，请自动发现或浏览选择")
        ttk.Entry(program, textvariable=self.program_var, state="readonly").grid(
            row=0, column=0, sticky="ew", padx=(0, 8))
        self.discover_button = ttk.Button(program, text="自动发现", command=self._discover)
        self.discover_button.grid(row=0, column=1, padx=(0, 8))
        self.browse_button = ttk.Button(program, text="浏览程序…", command=self._browse)
        self.browse_button.grid(row=0, column=2)
        self.notice_var = tk.StringVar(value="程序位置按项目相对路径保存；跨盘位置将在下次自动发现或重新选择。")
        ttk.Label(program, textvariable=self.notice_var, wraplength=930).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))

        inputs = ttk.LabelFrame(outer, text="卡片编号（每批 1–20 个，支持换行、逗号、分号或空白分隔）", padding=10)
        inputs.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        inputs.columnconfigure(0, weight=1)
        self.input_text = ScrolledText(inputs, height=4, wrap="word", undo=True)
        self.input_text.grid(row=0, column=0, sticky="ew")
        self.input_text.insert("1.0", "19-0629")
        mail_frame = ttk.LabelFrame(inputs, text="批次邮件", padding=8)
        mail_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        mail_frame.columnconfigure(1, weight=1)
        ttk.Label(mail_frame, text="收件邮箱").grid(row=0, column=0, padx=(0, 6))
        self.mail_recipient_var = tk.StringVar(value=self.mail_settings.recipient)
        self.mail_recipient_entry = ttk.Entry(mail_frame, textvariable=self.mail_recipient_var, width=28)
        self.mail_recipient_entry.grid(row=0, column=1, sticky="ew")
        configured = bool(self.mail_settings.username and not self.mail_settings.message)
        self.mail_enabled_var = tk.BooleanVar(value=configured and self.mail_settings.enabled)
        self.mail_enable_check = ttk.Checkbutton(mail_frame, text="归档完成后发送", variable=self.mail_enabled_var)
        self.mail_enable_check.grid(row=0, column=2, padx=8)
        self.mail_settings_button = ttk.Button(mail_frame, text="邮件设置…", command=self._show_mail_settings)
        self.mail_settings_button.grid(row=0, column=3, padx=(0, 6))
        self.mail_dry_run_button = ttk.Button(mail_frame, text="离线验证", command=self._mail_dry_run)
        self.mail_dry_run_button.grid(row=0, column=4, padx=(0, 6))
        self.mail_retry_button = ttk.Button(mail_frame, text="邮件重试", command=self._retry_mail, state="disabled")
        self.mail_retry_button.grid(row=0, column=5)
        self.mail_status_var = tk.StringVar(value="邮件尚未配置。" if not configured else "邮件已配置，等待归档批次。")
        ttk.Label(mail_frame, textvariable=self.mail_status_var, wraplength=930).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, sticky="ew", pady=10)
        self.start_button = ttk.Button(actions, text="开始归档", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.retry_button = ttk.Button(actions, text="重试失败／停止／未开始项", command=self._retry, state="disabled")
        self.retry_button.pack(side="left")
        self.auto_exit_var = tk.BooleanVar(master=self.window, value=False)
        ttk.Checkbutton(actions, text="完成后退出 HighScore Plus", variable=self.auto_exit_var).pack(
            side="left", padx=12)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        self.status_var = tk.StringVar(value="就绪。开始后请保持 HighScore 可操作，避免切换目标窗口。")
        ttk.Label(outer, textvariable=self.status_var, wraplength=980).grid(
            row=3, column=0, sticky="w", pady=(0, 7))

        results = ttk.Frame(outer)
        results.grid(row=4, column=0, sticky="nsew")
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=1)
        columns = ("code", "status", "phase_name", "peak_count", "files")
        self.table = ttk.Treeview(results, columns=columns, show="headings", height=8, selectmode="browse")
        for key, title, width in (
            ("code", "卡片编号", 150), ("status", "状态", 85), ("phase_name", "物相", 180),
            ("peak_count", "峰数", 60), ("files", "输出文件", 350),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=50, stretch=key in {"phase_name", "files"})
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<<TreeviewSelect>>", self._show_selection)
        self.detail_var = tk.StringVar(value="选择结果可查看状态说明和相对输出路径。")
        ttk.Label(results, textvariable=self.detail_var, wraplength=970).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        output = ttk.Frame(outer)
        output.grid(row=5, column=0, sticky="ew", pady=10)
        ttk.Label(output, text="输出目录：.\\output\\cards\\").pack(side="left")
        ttk.Button(output, text="打开输出目录", command=self._open_output).pack(side="left", padx=10)
        ttk.Button(output, text="打开所选归档目录", command=self._open_selected).pack(side="left")
        ttk.Button(output, text="关闭归档器", command=self._close).pack(side="right")
        self.exit_highscore_button = ttk.Button(output, text="退出 HighScore Plus", command=self._exit_highscore)
        self.exit_highscore_button.pack(side="right", padx=8)
        ttk.Label(outer, text="运行日志（同时保存到 .\\logs\\gui.log）").grid(row=6, column=0, sticky="w")
        self.log_text = ScrolledText(outer, height=7, state="disabled", wrap="word")
        self.log_text.grid(row=7, column=0, sticky="nsew", pady=(5, 0))

    def _mail_snapshot(self, *, require_credentials=True, ignore_enabled=False):
        toggle = getattr(self, "mail_enabled_var", None)
        if not ignore_enabled and (toggle is None or not toggle.get()):
            return None
        settings = replace(getattr(self, "mail_settings", MailSettings()))
        recipient = validate_email(self.mail_recipient_var.get())
        settings = validate_mail_settings(settings, recipient, require_credentials=require_credentials)
        return settings, recipient

    def _show_mail_settings(self):
        if self.running or self.closing:
            return
        dialog = self.mail_dialog = tk.Toplevel(self.window)
        dialog.title("邮件设置")
        dialog.transient(self.window)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=16)
        frame.grid(sticky="nsew")
        settings = self.mail_settings
        values = {
            "username": tk.StringVar(value=settings.username),
            "authorization_code": tk.StringVar(value=settings.authorization_code),
            "smtp_host": tk.StringVar(value=settings.smtp_host),
            "smtp_port": tk.StringVar(value=str(settings.smtp_port)),
            "smtp_ssl": tk.BooleanVar(value=settings.smtp_ssl),
        }
        for row, (name, label) in enumerate((("username", "SMTP 用户名（邮箱）"), ("authorization_code", "SMTP 授权码"),
                                             ("smtp_host", "SMTP 主机"), ("smtp_port", "SMTP 端口"))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 12))
            ttk.Entry(frame, textvariable=values[name], width=38, show="●" if name == "authorization_code" else "").grid(
                row=row, column=1, sticky="ew", pady=5)
        ttk.Checkbutton(frame, text="SSL（必须开启，自动校验证书）", variable=values["smtp_ssl"]).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=7)
        ttk.Label(frame, text="授权码使用当前 Windows 用户的 DPAPI 加密，保存在 .\\mail.local.json。\n保存只验证本地设置；连接服务器在发送时进行。",
                  wraplength=470).grid(row=5, column=0, columnspan=2, sticky="w", pady=8)

        def close():
            values["authorization_code"].set("")
            self.mail_dialog = None
            dialog.grab_release()
            dialog.destroy()

        def save():
            try:
                try:
                    port = int(values["smtp_port"].get())
                except ValueError:
                    raise ValueError("SMTP 端口必须为整数。") from None
                enabled = self.mail_enabled_var.get() if settings.username else True
                candidate = MailSettings(username=values["username"].get(), authorization_code=values["authorization_code"].get(),
                                         smtp_host=values["smtp_host"].get(), smtp_port=port, smtp_ssl=values["smtp_ssl"].get(),
                                         recipient=self.mail_recipient_var.get(), enabled=bool(enabled))
                self.mail_settings = save_mail_settings(self.project_root, candidate)
            except ValueError as error:
                messagebox.showerror("无法保存邮件设置", str(error), parent=dialog)
                return
            self.mail_recipient_var.set(self.mail_settings.recipient)
            self.mail_enabled_var.set(self.mail_settings.enabled)
            self.mail_status_var.set("邮件设置已加密保存，归档批次将使用开始时的设置快照。")
            self._append_log("邮件设置已加密保存。")
            close()

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="保存配置", command=save).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="取消", command=close).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.grab_set()

    def _apply_mail_result(self, result):
        if result.get("job_id"):
            self.latest_mail = dict(result)
        labels = {"prepared": "已准备", "sending": "正在投递", "sent": "服务器已接受", "failed": "失败",
                  "unknown": "状态不确定", "skipped": "已跳过", "stopped": "已停止"}
        text = f"邮件：{labels.get(result.get('status'), '待检查')}。{result.get('message', '')}"
        if result.get("job_path"):
            text += f" 回执：{result['job_path']}"
        variable = getattr(self, "mail_status_var", None)
        if variable is not None:
            variable.set(text)
        self._append_log(text)

    def _existing_mail_results(self):
        """只读当前输入对应的归档，离线验证不触发 HighScore。"""
        from .archive import ArchiveError, ArchiveStore
        store = ArchiveStore(self.project_root)
        records = []
        for request in parse_codes(self.input_text.get("1.0", "end")):
            try:
                receipt = store.find_success(request)
            except ArchiveError:
                receipt = None
            record = {"code": request.query, "input_code": request.original, "status": "failed", "message": "现有归档缺失或校验失败。"}
            if receipt:
                record.update(status="skipped", actual_code=receipt["code"], receipt_path=receipt["receipt_path"],
                              message="离线读取的现有归档已通过校验。")
            records.append(record)
        return records

    def _mail_dry_run(self):
        if self.running or self.closing:
            return
        try:
            settings, recipient = self._mail_snapshot(require_credentials=False, ignore_enabled=True)
            results = self._existing_mail_results()
        except ValueError as error:
            messagebox.showerror("无法离线验证", str(error), parent=self.window)
            return
        if not any(result["status"] == "skipped" for result in results):
            self._apply_mail_result({"status": "skipped", "message": "当前输入没有通过校验的现有归档；离线验证不会操作 HighScore。"})
            return
        self._launch_mail_only(settings, recipient=recipient, results=results, dry_run=True)

    def _retry_mail(self):
        if self.running or self.closing:
            return
        job = getattr(self, "latest_mail", None)
        if not job or not job.get("job_id"):
            return
        force = job.get("status") == "unknown"
        if force and not messagebox.askyesno(
                "明确重新发送", "上次投递状态不确定，可能已被服务器接受。请先核对收件箱和已发送邮件。\n重新发送会生成新任务和新 Message-ID，可能产生重复邮件。\n是否明确重新发送？",
                parent=self.window, default="no"):
            return
        try:
            settings = validate_mail_settings(replace(self.mail_settings), job.get("recipient"))
        except ValueError as error:
            messagebox.showerror("邮件配置有误", str(error), parent=self.window)
            return
        self._launch_mail_only(settings, job_id=job["job_id"], force=force)

    def _launch_mail_only(self, settings, *, recipient=None, results=None, dry_run=False, job_id=None, force=False):
        self.running = True
        self.stop_event = threading.Event()
        stop_event = self.stop_event
        self._set_running_controls()
        self.mail_status_var.set("正在离线生成并校验邮件…" if dry_run else "正在重试邮件投递…")

        def work():
            try:
                service = MailService(self.project_root, settings, stop_event=stop_event)
                result = (service.send(job_id, force=force) if job_id else
                          service.deliver(results, recipient, dry_run=dry_run))
            except Exception:
                result = {"status": "failed", "message": "邮件任务未能完成；请检查本地设置和任务产物，归档已保留。"}
            self.events.put(("mail_result", result))
            self.events.put(("mail_only_done", None))

        self.worker = threading.Thread(target=work, name="highscore-mail", daemon=True)
        self.worker.start()

    def _relative_display(self, path: Path) -> str:
        try:
            return ".\\" + os.path.relpath(path, self.project_root).replace("/", "\\")
        except ValueError:
            return "HighScorePlus.exe（位于其他盘符，本次会话使用）"

    def _set_executable(self, executable: Path, *, persist: bool):
        self.executable = Path(executable).resolve()
        self.program_var.set(self._relative_display(self.executable))
        if persist:
            try:
                self.local_settings = save_settings(self.project_root, self.executable)
                self.notice_var.set(self.local_settings.message or "程序位置已保存为项目相对路径。")
            except ValueError as error:
                self.notice_var.set(str(error))
                self._append_log(str(error))
        self._append_log(f"已选择程序：{self._relative_display(self.executable)}")

    def _discover(self, *, persist: bool = True, report: bool = True):
        if self.running or self.closing:
            return
        try:
            from .automation import discover_highscore
            executable = discover_highscore()
        except Exception as error:
            executable = None
            self._append_log(f"自动发现程序失败：{error}")
        if executable is not None and executable.is_file():
            self._set_executable(executable, persist=persist)
            return
        message = "未发现 HighScorePlus.exe，请使用“浏览程序”选择本机安装位置。"
        self._append_log(message)
        if report:
            self.notice_var.set(message)

    def _browse(self):
        if self.running:
            return
        selected = filedialog.askopenfilename(
            parent=self.window, title="选择 HighScorePlus.exe",
            initialdir=str(self.executable.parent if self.executable else self.project_root),
            filetypes=[("HighScore 程序", "HighScorePlus.exe"), ("可执行程序", "*.exe")],
        )
        if not selected:
            return
        executable = Path(selected)
        if executable.name.casefold() != "highscoreplus.exe" or not executable.is_file():
            messagebox.showerror("程序位置无效", "请选择实际存在的 HighScorePlus.exe。", parent=self.window)
            return
        self._set_executable(executable, persist=True)

    def _start(self):
        self._begin(self.input_text.get("1.0", "end"), retry=False)

    def _retry(self):
        text = _retry_text(list(self.records.values()))
        if text:
            self._begin(text, retry=True)

    def _begin(self, text: str, *, retry: bool):
        if self.running or self.closing:
            return
        try:
            requests = parse_codes(text)
        except ValueError as error:
            messagebox.showerror("编号输入有误", str(error), parent=self.window)
            return
        try:
            mail_snapshot = self._mail_snapshot()
        except ValueError as error:
            messagebox.showerror("邮件设置有误", str(error), parent=self.window)
            return
        if self.executable is None or not self.executable.is_file():
            self._discover(persist=False, report=True)
        if self.executable is None or not self.executable.is_file():
            messagebox.showerror("无法开始", "请先选择本机 HighScorePlus.exe。", parent=self.window)
            return
        if not retry:
            self.table.delete(*self.table.get_children())
            self.records.clear()
        self.active_keys = []
        for request in requests:
            key = request.dedupe_key
            self.active_keys.append(key)
            record = self.records.setdefault(key, {})
            record.update({"request": request.original, "code": request.query, "status": "pending", "message": "等待开始"})
            self._update_row(key)
        self.stop_event = threading.Event()
        self.running = True
        self._set_running_controls()
        self.status_var.set(f"正在处理 {len(requests)} 个编号…")
        self._append_log(f"{'重试' if retry else '开始'}：{len(requests)} 个编号。")
        executable = self.executable
        stop_event = self.stop_event

        def work():
            try:
                from .service import BatchRunner
                runner = BatchRunner(
                    self.project_root, on_event=lambda event: self.events.put(("event", dict(event))),
                    stop_event=stop_event, choose_source=self._choose_source,
                )
                result = runner.run("\n".join(request.original for request in requests), executable=executable)
                if mail_snapshot is not None:
                    settings, recipient = mail_snapshot
                    try:
                        mail_result = MailService(self.project_root, settings, stop_event=stop_event).deliver(result, recipient)
                    except Exception:
                        mail_result = {"status": "failed", "message": "邮件任务未能完成；归档结果已保留，请检查邮件设置和产物。"}
                    self.events.put(("mail_result", mail_result))
                self.events.put(("done", result))
            except Exception as error:
                self.events.put(("failed", str(error)))

        self.worker = threading.Thread(target=work, name="highscore-batch", daemon=True)
        self.worker.start()

    def _set_running_controls(self):
        state = "disabled" if self.running else "normal"
        for button in (self.start_button, self.discover_button, self.browse_button,
                       self.exit_highscore_button):
            button.configure(state=state)
        self.input_text.configure(state=state)
        can_retry = bool(_retry_text(list(self.records.values())))
        self.retry_button.configure(state="normal" if not self.running and can_retry else "disabled")
        self.stop_button.configure(state="normal" if self.running else "disabled")
        for name in ("mail_recipient_entry", "mail_enable_check", "mail_settings_button", "mail_dry_run_button"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state=state)
        retry_mail = getattr(self, "mail_retry_button", None)
        if retry_mail is not None:
            recent = getattr(self, "latest_mail", None) or {}
            retry_mail.configure(state="normal" if not self.running and recent.get("status") in {
                "prepared", "failed", "unknown", "stopped"} else "disabled",
                text="重新发送…" if recent.get("status") == "unknown" else "邮件重试")
        if self.running:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _stop(self):
        if not self.running:
            return
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止，等待当前步骤退出；已经完成的归档会保留。")
        self._append_log("已请求停止。")
        self._answer_source(None)

    def _exit_highscore(self):
        """在后台正常关闭 Plus，归档器继续显示结果或保存提示原因。"""
        if self.running or self.closing:
            return
        self.running = True
        self.stop_event = threading.Event()
        stop_event, executable = self.stop_event, self.executable
        self._set_running_controls()
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在请求退出 HighScore Plus…")
        self._append_log("正在请求退出 HighScore Plus。")

        def work():
            try:
                from .automation import HighScoreAutomation
                adapter = HighScoreAutomation(
                    executable, timeout=10, stop_event=stop_event,
                    progress=lambda message: self.events.put(("highscore_exit_progress", message)))
                result = adapter.close_application()
            except Exception as error:
                result = {"status": "needs_attention", "message": f"退出 HighScore Plus 暂停：{error}"}
            self.events.put(("highscore_exit_done", result))

        self.worker = threading.Thread(target=work, name="highscore-exit", daemon=True)
        self.worker.start()

    def _event_key(self, event: dict) -> str | None:
        value = event.get("code")
        if value is None:
            return None
        try:
            parsed = parse_code(str(value))
        except ValueError:
            return None
        if parsed.dedupe_key in self.records:
            return parsed.dedupe_key
        matches = [key for key in self.active_keys
                   if parse_code(self.records[key]["request"]).matches(str(value))]
        return matches[0] if len(matches) == 1 else None

    def _apply_event(self, event: dict):
        key = self._event_key(event)
        if key is not None:
            self.records[key].update({name: value for name, value in event.items() if value is not None})
            self._update_row(key)
        message = str(event.get("message") or "")
        if message:
            prefix = f"{event['code']}：" if event.get("code") else ""
            self.status_var.set(prefix + message)
            self._append_log(prefix + message)

    def _update_row(self, key: str):
        record = self.records[key]
        files = record.get("files") or {}
        paths = "；".join(str(value) for value in files.values()) if isinstance(files, dict) else ""
        status = _status(record.get("status"))
        values = (record["code"], _STATUS_LABELS.get(status, str(record.get("status", "未开始"))),
                  record.get("phase_name", ""), record.get("peak_count", ""), paths)
        if self.table.exists(key):
            self.table.item(key, values=values)
        else:
            self.table.insert("", "end", iid=key, values=values)
        if self.table.selection() == (key,):
            self._show_selection()

    def _show_selection(self, _event=None):
        selected = self.table.selection()
        if not selected:
            return
        record = self.records.get(selected[0], {})
        details = str(record.get("message") or "")
        files = record.get("files") or {}
        if isinstance(files, dict) and files:
            details += "\n" + "；".join(str(value) for value in files.values())
        self.detail_var.set(details or "暂无结果说明。")

    def _choose_source(self, choices: list[dict]) -> int | None:
        request = {"choices": choices, "ready": threading.Event(), "selection": None}
        self.events.put(("source", request))
        while not request["ready"].wait(0.1):
            if self.stop_event.is_set():
                return None
        return request["selection"]

    def _show_source(self, request: dict):
        if self.stop_event.is_set() or self.closing:
            request["ready"].set()
            return
        if self.source_request is not None:
            request["ready"].set()
            self._append_log("出现重复来源选择请求，已取消后来的请求。")
            return
        self.source_request = request
        dialog = self.source_dialog = tk.Toplevel(self.window)
        dialog.title("选择本机数据库来源")
        dialog.geometry("780x340")
        dialog.transient(self.window)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="同一编号有多个来源，请核对来源信息后选择。", padding=12).grid(sticky="w")
        table = ttk.Treeview(dialog, columns=("code", "source"), show="headings", selectmode="browse")
        table.heading("code", text="卡片编号")
        table.heading("source", text="来源及软件信息")
        table.column("code", width=150, stretch=False)
        table.column("source", width=580)
        table.grid(row=1, column=0, sticky="nsew", padx=12)
        for index, source in enumerate(request["choices"]):
            description = source.get("row_text") or source.get("source") or source.get("database") or str(source)
            table.insert("", "end", iid=str(index), values=(source.get("code", ""), description))
        controls = ttk.Frame(dialog, padding=12)
        controls.grid(row=2, column=0, sticky="e")

        def accept():
            selected = table.selection()
            if not selected:
                messagebox.showinfo("尚未选择", "请先选择一个来源。", parent=dialog)
                return
            self._answer_source(int(selected[0]))

        ttk.Button(controls, text="使用所选来源", command=accept).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="取消选择", command=lambda: self._answer_source(None)).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="停止任务", command=self._stop).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._answer_source(None))
        dialog.grab_set()
        dialog.lift()

    def _answer_source(self, selection: int | None):
        request, dialog = self.source_request, self.source_dialog
        self.source_request = self.source_dialog = None
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
        if request is not None:
            request["selection"] = selection
            request["ready"].set()

    def _drain_events(self):
        for _ in range(200):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                self._apply_event(payload)
            elif kind == "source":
                self._show_source(payload)
            elif kind == "mail_result":
                self._apply_mail_result(payload)
            elif kind == "mail_only_done":
                self.running = False
                self._set_running_controls()
                if self.closing:
                    self.window.destroy()
                    return
            elif kind == "highscore_exit_progress":
                self.status_var.set(payload)
                self._append_log(payload)
            elif kind == "highscore_exit_done":
                self.running = False
                self._set_running_controls()
                self.status_var.set(payload["message"])
                self._append_log(payload["message"])
                if self.closing:
                    self.window.destroy()
                    return
            elif kind in {"done", "failed"}:
                self._finished(payload if kind == "done" else [], payload if kind == "failed" else None)
                if self.closing:
                    self.window.destroy()
                    return
        self.window.after(80, self._drain_events)

    def _finished(self, results: list[dict], error: str | None):
        for result in results or []:
            if isinstance(result, dict):
                self._apply_event(result)
        for key in self.active_keys:
            status = _status(self.records[key].get("status"))
            if status not in _FINISHED:
                self.records[key].update({"status": "stopped" if self.stop_event.is_set() else "failed",
                                          "message": error or "任务未给出完整结果，请重试。"})
                self._update_row(key)
        self._answer_source(None)
        self.running = False
        self._set_running_controls()
        if error:
            self.status_var.set(f"任务已结束：{error}")
            self._append_log(f"任务异常：{error}")
        else:
            statuses = [_status(self.records[key].get("status")) for key in self.active_keys]
            successful = sum(status in _SUCCESS for status in statuses)
            skipped = sum(status in _SKIPPED for status in statuses)
            remaining = len(statuses) - successful - skipped
            message = f"本次任务结束：成功 {successful}，已跳过 {skipped}，失败／停止／未开始 {remaining}。"
            self.status_var.set(message)
            self._append_log(message)
            if (self.auto_exit_var.get() and statuses and not self.closing
                    and not self.stop_event.is_set()
                    and all(status in _SUCCESS | _SKIPPED for status in statuses)
                    and not any(self.records[key].get("needs_attention") for key in self.active_keys)):
                self._exit_highscore()

    def _append_log(self, message: str):
        message = str(message).replace(str(self.project_root), ".")
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        try:
            directory = self.log_path.parent.resolve()
            if not directory.is_relative_to(self.project_root):
                raise ValueError("日志目录位于项目外")
            directory.mkdir(parents=True, exist_ok=True)
            if self.log_path.is_symlink():
                raise ValueError("日志文件不能为符号链接")
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except (OSError, ValueError):
            self.notice_var.set("无法写入项目日志，当前运行信息仍显示在界面中。")

    def _open_directory(self, directory: Path):
        try:
            directory = directory.resolve()
            if not directory.is_relative_to(self.project_root):
                raise ValueError("输出路径超出项目目录。")
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except (OSError, ValueError) as error:
            messagebox.showerror("无法打开目录", str(error), parent=self.window)

    def _open_output(self):
        self._open_directory(self.project_root / "output" / "cards")

    def _open_selected(self):
        selected = self.table.selection()
        if not selected:
            return
        files = self.records.get(selected[0], {}).get("files") or {}
        if not isinstance(files, dict) or not files:
            messagebox.showinfo("暂无归档", "所选条目尚未生成归档文件。", parent=self.window)
            return
        relative = Path(str(next(iter(files.values()))))
        if relative.is_absolute():
            messagebox.showerror("输出路径无效", "归档路径必须相对于项目目录。", parent=self.window)
            return
        self._open_directory((self.project_root / relative).parent)

    def _close(self):
        if self.running:
            self.closing = True
            self._stop()
            self.status_var.set("正在停止，后台任务退出后将自动关闭窗口。")
        else:
            self.window.destroy()


def launch_gui(root: Path) -> None:
    """以项目根目录启动中文界面；仅开始归档后才调用 BatchRunner。"""
    application = HighScoreWindow(Path(root))
    application.window.mainloop()
