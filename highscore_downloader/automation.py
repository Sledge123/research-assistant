"""本机 HighScore 的进程绑定、控件探测和原生卡片导出。"""
from __future__ import annotations

import os
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


class AutomationError(RuntimeError):
    """窗口或控件状态不符合预期，调用方必须暂停。"""


class CardNotFound(AutomationError):
    """软件已经明确完成查询但没有命中。"""


class OperationStopped(AutomationError):
    """用户请求停止。"""


class HighScoreNotRunning(AutomationError):
    """目标进程没有运行，退出操作无需启动它。"""


def is_highscore_window(executable: str, title: str) -> bool:
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name == "highscoreplus.exe" and bool(
        re.fullmatch(r"HighScore Plus(?:\s*-\s*.*)?", title)
    )


def discover_highscore() -> Path | None:
    """从软件自己注册的安装信息发现位置，不扫描整盘。"""
    if os.name != "nt":
        return None
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for uninstall in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ):
            try:
                with winreg.OpenKey(hive, uninstall) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                                if "HighScore" not in name:
                                    continue
                                location = str(winreg.QueryValueEx(key, "InstallLocation")[0])
                                candidate = Path(location) / "HighScorePlus.exe"
                                if candidate.is_file():
                                    return candidate.resolve()
                        except OSError:
                            continue
            except OSError:
                continue
    return None


class HighScoreAutomation:
    def __init__(
        self,
        executable: Path | str | None = None,
        *,
        timeout: float = 30,
        stop_event: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        candidate = Path(executable) if executable else None
        self.executable = ((candidate if candidate.is_absolute() else ROOT / candidate).resolve()
                           if candidate is not None else None)
        self.timeout = timeout
        self.stop_event = stop_event or threading.Event()
        self.progress = progress or (lambda message: None)
        self.app = None
        self.main = None
        self.document = None
        self.detail = None
        self.current_document_read_only = False
        self.last_input_state = None
        self.expected_request = None
        self.reference_source_count = None
        self.detail_code = None
        self.owned_document_handles = set()
        self.menu_trace = []
        self.document_ready_state = None

    def check_stop(self):
        if self.stop_event.is_set():
            raise OperationStopped("已停止，保留已经完成的归档")

    def connect(self, *, launch_if_missing: bool = True):
        self.check_stop()
        if os.name != "nt":
            raise AutomationError("RPA 需要 Windows 桌面")
        from pywinauto.application import Application, ProcessNotFoundError

        executable = self.executable or discover_highscore()
        if not executable or not executable.is_file():
            raise AutomationError("未发现 HighScorePlus.exe，请选择本机程序位置")
        if executable.name.casefold() != "highscoreplus.exe":
            raise AutomationError("所选程序不是 HighScorePlus.exe")
        self.executable = executable
        try:
            app = Application(backend="win32").connect(path=str(executable), timeout=2)
        except ProcessNotFoundError as error:
            if not launch_if_missing:
                raise HighScoreNotRunning("未连接到正在运行的 HighScore，未启动新的进程") from error
            self.progress("正在启动 HighScore Plus")
            app = Application(backend="win32").start(
                f'"{executable}"', work_dir=str(executable.parent), wait_for_idle=False
            )
        except Exception as error:
            raise AutomationError("连接 HighScore 失败，未启动重复进程") from error
        main_spec = app.window(class_name="TfrmMain", title_re=r"^HighScore Plus(?:\s*-.*)?$")
        main_spec.wait("exists visible", timeout=self.timeout)
        self.app = app
        self.main = main_spec.wrapper_object()
        if not is_highscore_window(executable.name, self.main.window_text()):
            raise AutomationError("HighScore 主窗口身份不匹配")
        self.progress("已连接 HighScore Plus")
        return self

    def close_application(self) -> dict:
        """请求正常退出 Plus；保存提示保留给用户，禁止强制结束进程。"""
        self.check_stop()
        if self.app is None:
            try:
                self.connect(launch_if_missing=False)
            except HighScoreNotRunning:
                return {"status": "not_running", "message": "HighScore Plus 当前未运行。"}
        if not self.app.is_process_running():
            return {"status": "not_running", "message": "HighScore Plus 当前未运行。"}
        attention = {"status": "needs_attention", "message": (
            "HighScore Plus 正在等待保存或关闭对话框；请先在软件中处理提示，再退出 HighScore Plus。")}
        if not self.main.is_enabled():
            return attention
        self._activate_window(self.main, "HighScore Plus 主窗口未能激活，未请求退出")
        self.main.post_message(0x0010)  # WM_CLOSE，让软件正常执行保存提示和退出流程。
        self.progress("已请求退出 HighScore Plus，等待软件正常结束。")
        deadline = time.monotonic() + min(self.timeout, 10)
        while time.monotonic() < deadline:
            self.check_stop()
            if not self.app.is_process_running():
                return {"status": "closed", "message": "HighScore Plus 已退出，归档结果保留在当前窗口。"}
            try:
                enabled = self.main.is_enabled()
            except Exception:
                enabled = None  # 主窗口可能已销毁，但仍须等待进程实际结束。
            if enabled is False:
                return attention
            self.stop_event.wait(min(0.15, max(0, deadline - time.monotonic())))
        return {"status": "needs_attention", "message": (
            "HighScore Plus 尚未退出；请检查软件的保存提示或当前状态。程序未强制结束进程。")}

    def _wait(self, predicate, message: str, timeout: float | None = None):
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        last_error = None
        while time.monotonic() < deadline:
            self.check_stop()
            try:
                result = predicate()
                if result:
                    return result
            except AutomationError:
                raise
            except Exception as error:
                last_error = error
            self.stop_event.wait(0.15)
        raise AutomationError(message) from last_error

    def _window(self, class_name: str):
        found = [window for window in self.app.windows(visible_only=True)
                 if window.class_name() == class_name]
        if len(found) > 1:
            raise AutomationError(f"目标窗口不唯一：{class_name}")
        return found[0] if found else None

    def _input_state(self) -> dict:
        """只读查询目标 GUI 线程；不绑定线程，也不改变前台或焦点。"""
        import ctypes
        import win32gui
        import win32process
        from pywinauto import win32functions, win32structures

        foreground = win32gui.GetForegroundWindow()
        foreground_pid = (win32process.GetWindowThreadProcessId(foreground)[1]
                          if foreground else 0)
        thread_id, _ = win32process.GetWindowThreadProcessId(self.main.handle)
        info = win32structures.GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not win32functions.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            raise AutomationError("无法读取 HighScore 键盘焦点，已暂停")
        state = {"foreground": foreground, "foreground_pid": foreground_pid,
                 "active": int(info.hwndActive or 0), "focus": int(info.hwndFocus or 0),
                 "menu_owner": int(info.hwndMenuOwner or 0), "flags": int(info.flags)}
        self.last_input_state = state
        return state

    def _assert_target(self, control):
        import win32gui
        import win32process

        self.check_stop()
        if (self.app is None or not win32gui.IsWindow(control.handle)
                or control.process_id() != self.app.process
                or win32process.GetWindowThreadProcessId(control.handle)[1] != self.app.process):
            raise AutomationError("目标控件不属于已绑定的目标进程，已暂停")
        if not control.is_visible() or not control.is_enabled():
            raise AutomationError("目标控件不可操作，已暂停")

    def _assert_foreground(self, control) -> dict:
        import win32gui

        self._assert_target(control)
        state = self._input_state()
        if state["foreground_pid"] != self.app.process:
            raise AutomationError("HighScore 已失去前台，已暂停；请激活目标窗口后接续")
        # 弹出菜单由主窗拥有，不能把菜单自身设为前台，否则 VCL 会收起菜单。
        root = (self.main.handle if control.class_name() == "TdxBarSubMenuControl"
                else win32gui.GetAncestor(control.handle, 2))  # GA_ROOT
        if state["foreground"] != root:
            raise AutomationError("当前前台是另一个窗口，已暂停以防输入到错误对话框")
        return state

    def _assert_keyboard_focus(self, control):
        state = self._assert_foreground(control)
        if state["focus"] != control.handle:
            raise AutomationError("目标控件未取得键盘焦点，已暂停")
        return state

    def _activate_window(self, window, message="HighScore 目标窗口未能切换到前台"):
        """只在独立操作开始时激活已绑定窗口，后续输入仍逐步核对。"""
        self._assert_target(window)
        try:
            window.set_focus()
        except Exception as error:
            raise AutomationError(message) from error
        self._wait(lambda: self._input_state()["foreground"] == window.handle,
                   message, timeout=5)
        self._assert_foreground(window)

    def _send_keys(self, control, keys: str, *, menu: bool = False):
        """逐键检查后发送；不使用会 AttachThreadInput 的 wrapper.type_keys。"""
        from pywinauto import keyboard

        held = []
        try:
            for action in keyboard.parse_keys(keys, vk_packet=False):
                self.check_stop()
                if menu:
                    self._assert_menu_foreground()
                else:
                    self._assert_keyboard_focus(control)
                action.run()
                if action.down and not action.up:
                    held.append(action.key)
                elif action.up and not action.down and action.key in held:
                    held.remove(action.key)
                self.stop_event.wait(0.05)
        finally:
            # 焦点被用户切走时也只释放本次持有的修饰键，不继续输入内容。
            for key in reversed(held):
                keyboard.VirtualKeyAction(key, down=False, up=True).run()

    def _press(self, control, keys: str):
        self._assert_foreground(control)
        if self._popup_menus():
            raise AutomationError("菜单仍处于打开状态，暂停键盘输入")
        if self._input_state()["focus"] != control.handle:
            control.set_keyboard_focus()
            self._wait(lambda: self._assert_keyboard_focus(control),
                       "目标控件未取得键盘焦点，已暂停", timeout=2)
        self._assert_keyboard_focus(control)
        self._send_keys(control, keys)

    def _assert_menu_foreground(self):
        """菜单允许主窗的子控件持焦，但绝不切换前台或重设焦点。"""
        self._assert_target(self.main)
        state = self._input_state()
        if state["foreground_pid"] != self.app.process:
            raise AutomationError("菜单导航时 HighScore 已失去前台，已暂停")
        return state

    def _menu_keys(self, keys: str):
        self._assert_menu_foreground()
        self._send_keys(self.main, keys, menu=True)
        # keybd_event 入队后 VCL 需要处理消息；菜单选择并不总改变 HWND。
        self.stop_event.wait(0.15)
        self.check_stop()

    def _trace_menu(self, stage: str) -> dict:
        menus = []
        for item in self._popup_menus():
            try:
                rect = item.rectangle()
                menus.append({"handle": item.handle, "class": item.class_name(),
                              "title": item.window_text(),
                              "rect": [rect.left, rect.top, rect.right, rect.bottom]})
            except Exception:
                menus.append({"handle": item.handle, "stale": True})
        record = {"stage": stage, "input_state": self._input_state(),
                  "menus": menus}
        self.menu_trace.append(record)
        self.menu_trace = self.menu_trace[-24:]
        directory = ROOT / ".cache" / "automation"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "menu_trace.json").write_text(
            json.dumps(self.menu_trace, ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    def _wait_expanded_reference_menu(self, timeout: float = 3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_stop()
            self._assert_menu_foreground()
            menus = self._popup_menus()
            if len(menus) > 2:
                raise AutomationError("参考菜单层级不唯一，已暂停")
            expanded = []
            for menu in menus:
                try:
                    if menu.rectangle().height() >= 100:
                        expanded.append(menu)
                except Exception:
                    # 智能菜单正在重建时只继续观察，不点击旧句柄。
                    continue
            if len(expanded) > 1:
                raise AutomationError("完整参考菜单不唯一，已暂停")
            if expanded:
                return expanded[0]
            self.stop_event.wait(0.15)
        return None

    def _ensure_reference_menu_expanded(self):
        """本机折叠高 53、完整高 324；点击前等待真实完整菜单，避免穿透。"""
        expanded = self._wait_expanded_reference_menu()
        if expanded is not None:
            return expanded
        menus = self._popup_menus()
        if len(menus) != 1:
            raise AutomationError("参考菜单未保持打开，未点击检索项")
        folded = menus[0]
        rect = folded.rectangle()
        if rect.height() >= 100:
            return folded
        if rect.height() < 40:
            raise AutomationError("参考菜单尺寸异常，未点击展开箭头")
        self.progress("展开软件的完整参考菜单")
        self._click(folded, (rect.width() // 2, rect.height() - 13))
        expanded = self._wait_expanded_reference_menu()
        if expanded is None:
            raise AutomationError("参考菜单尚未完成展开，未点击检索项")
        return expanded

    def reference_menu_step(self, stage: str) -> dict:
        """逐阶段打开编号框：控件内点击主菜单/首条，最后 End/Enter。"""
        if stage not in ("open", "retrieve", "submit"):
            raise AutomationError("未知菜单阶段；只支持 open、retrieve、submit")
        if self.app is None:
            self.connect(launch_if_missing=False)
        if stage == "open":
            if self._popup_menus():
                raise AutomationError("菜单状态不是关闭状态，未重新打开主菜单")
            self._activate_window(self.main)
        self._assert_menu_foreground()
        self._trace_menu(f"{stage}:before")
        allowed_before = {"open": (0,), "retrieve": (1, 2), "submit": (2,)}[stage]
        if len(self._popup_menus()) not in allowed_before:
            raise AutomationError(f"菜单状态不符合 {stage} 阶段，已暂停")
        try:
            if stage == "open":
                self.progress("菜单阶段 1/3：打开 Reference Patterns")
                bars = [item for item in self.main.descendants()
                        if item.class_name() == "TdxBarControl" and item.window_text() == "Main Menu"
                        and item.is_visible()]
                if len(bars) != 1:
                    raise AutomationError("未能唯一绑定 HighScore 主菜单控件")
                self._click(bars[0], (260, 13))
                self.stop_event.wait(0.15)
                self.check_stop()
                self._wait(lambda: len(self._popup_menus()) in (1, 2),
                           "Reference Patterns 菜单未打开；查看 .cache/automation/menu_trace.json", timeout=3)
                self._ensure_reference_menu_expanded()
            elif stage == "retrieve":
                self.progress("菜单阶段 2/3：打开 Retrieve Pattern by 子菜单")
                menus = self._popup_menus()
                if len(menus) == 1:
                    popup = self._ensure_reference_menu_expanded()
                    if len(self._popup_menus()) == 1:
                        self._click(popup, (100, 15))
                        self.stop_event.wait(0.15)
                        self.check_stop()
                self._wait(lambda: len(self._popup_menus()) == 2,
                           "Retrieve Pattern by 子菜单未打开；查看 .cache/automation/menu_trace.json", timeout=3)
            else:
                self.progress("菜单阶段 3/3：确认 Reference Code")
                # End 的选择行为已经本机逐步核验，Down 会落到智能展开箭头。
                self._menu_keys("{END}")
                self._wait(lambda: len(self._popup_menus()) == 2,
                           "Reference Code 子菜单未保持打开", timeout=3)
                self._menu_keys("{ENTER}")

                def search_dialog():
                    dialog = self._window("TfrmSearchByNumber")
                    if dialog is not None:
                        return dialog
                    if not self.main.is_enabled():
                        other = [item for item in self.app.windows(visible_only=True)
                                 if item.handle != self.main.handle and item.class_name() != "TdxBarSubMenuControl"]
                        description = "、".join(f"{item.class_name()} {item.window_text()}" for item in other)
                        raise AutomationError(f"菜单后出现其他窗口，未输入编号：{description}")
                    return None

                self._wait(search_dialog, "编号检索框未打开；查看 .cache/automation/menu_trace.json", timeout=3)
        except Exception:
            self._trace_menu(f"{stage}:failed")
            raise
        return self._trace_menu(f"{stage}:ready")

    def open_reference_search(self):
        """正常流程和分段探测使用同一条已核验的菜单键盘链。"""
        for stage in ("open", "retrieve", "submit"):
            self.reference_menu_step(stage)
        dialog = self._window("TfrmSearchByNumber")
        if dialog is None:
            raise AutomationError("编号检索框未保持打开，已暂停")
        return dialog

    def _button(self, window, title: str):
        matches = [control for control in window.descendants()
                   if control.class_name() == "TButton"
                   and control.window_text().replace("&", "") == title
                   and control.is_visible()]
        if len(matches) != 1:
            raise AutomationError(f"无法唯一定位按钮：{title}")
        self.check_stop()
        if not matches[0].is_enabled():
            raise AutomationError(f"按钮未就绪：{title}")
        self._assert_foreground(matches[0])
        # BM_CLICK 绑定具体 TButton；没有全局鼠标、默认按钮或焦点歧义。
        result = matches[0].send_message_timeout(0x00F5, timeout=2)
        if not isinstance(result, int):
            raise AutomationError(f"按钮未能响应：{title}")

    def _click(self, control, coords, *, button="left", double=False):
        """弹出菜单不能 SetForegroundWindow；这会先关闭 VCL 菜单。"""
        import win32gui
        import win32process
        from pywinauto import mouse

        self._assert_foreground(control)
        rect = control.client_rect()
        if not (0 <= coords[0] < rect.width() and 0 <= coords[1] < rect.height()):
            raise AutomationError("目标位置超出已绑定控件范围，已暂停")
        point = control.client_to_screen(coords)
        hit = win32gui.WindowFromPoint(point)
        _, process = win32process.GetWindowThreadProcessId(hit)
        if (process != self.app.process
                or (hit != control.handle and not win32gui.IsChild(control.handle, hit))):
            raise AutomationError("目标位置被其他窗口遮挡，暂停操作")
        self._assert_foreground(control)
        if double:
            mouse.double_click(button=button, coords=point)
        else:
            mouse.click(button=button, coords=point)

    def _accepted_grid(self):
        if self.document is None or not self.document.is_visible():
            raise AutomationError("专用检索文档不可用")
        panels = [item for item in self.document.descendants()
                  if item.class_name() == "TPanel" and item.window_text() == "pnlIdentPatterns"]
        if len(panels) != 1:
            raise AutomationError("无法定位当前文档的参考卡片列表")
        grids = [item for item in panels[0].descendants()
                 if item.class_name() == "TcxTreeList" and item.is_visible()]
        if len(grids) != 1:
            raise AutomationError("参考卡片列表不唯一")
        return grids[0]

    def _bind_current_document(self):
        """用只读 WM_MDIGETACTIVE 绑定实际活动文档，不切换或创建文档。"""
        clients = [item for item in self.main.descendants()
                   if item.class_name().casefold() == "mdiclient"]
        if len(clients) != 1:
            raise AutomationError("无法唯一定位 HighScore 的文档容器")
        handle = clients[0].send_message_timeout(0x0229, timeout=2)  # WM_MDIGETACTIVE
        documents = [item for item in self.main.descendants()
                     if item.class_name() == "TfrmChild" and item.handle == handle]
        if len(documents) != 1:
            raise AutomationError("无法确认当前活动文档，未切换或创建文档")
        return documents[0]

    def _wait_document_ready(self):
        """新 MDI 文档完成延迟激活且连续空闲后，才能打开菜单。"""
        import win32gui
        import win32process

        self.check_stop()
        if (self.app is None or not win32gui.IsWindow(self.document.handle)
                or self.document.process_id() != self.app.process
                or win32process.GetWindowThreadProcessId(self.document.handle)[1] != self.app.process):
            raise AutomationError("新文档句柄不属于已绑定的目标进程")
        self.progress("等待专用文档初始化完成")
        deadline = time.monotonic() + 10
        last_focus = None
        stable_samples = 0
        cpu_samples = 0
        self.document_ready_state = {"document": self.document.handle, "stable_samples": 0,
                                     "cpu_samples": 0, "ready": False}
        while time.monotonic() < deadline:
            self.check_stop()
            visible, enabled = self.document.is_visible(), self.document.is_enabled()
            main_ready = self.main.is_visible() and self.main.is_enabled()
            self.document_ready_state.update(visible=visible, enabled=enabled, main_ready=main_ready)
            if not visible or not enabled or not main_ready:
                stable_samples = 0
                last_focus = None
                self.stop_event.wait(0.15)
                continue
            self._assert_foreground(self.main)
            if self._bind_current_document().handle != self.document.handle:
                raise AutomationError("专用文档已被切换，未打开检索菜单")
            before = self._input_state()
            focus = before["focus"]
            belongs = bool(focus and (focus == self.document.handle
                                     or win32gui.IsChild(self.document.handle, focus)))
            self.document_ready_state = {"document": self.document.handle,
                                         "focus_before": focus, "belongs_to_document": belongs,
                                         "stable_samples": stable_samples, "cpu_samples": cpu_samples,
                                         "ready": False}
            if not belongs:
                stable_samples = 0
                last_focus = None
                self.stop_event.wait(0.15)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.app.wait_cpu_usage_lower(threshold=5, timeout=min(10, remaining), usage_interval=0.5)
            except Exception as error:
                raise AutomationError("专用文档尚未进入 CPU 空闲状态，未打开检索菜单") from error
            self.check_stop()
            cpu_samples += 1
            self._assert_foreground(self.main)
            after = self._input_state()
            if self._bind_current_document().handle != self.document.handle:
                raise AutomationError("专用文档初始化时被切换，未打开检索菜单")
            after_focus = after["focus"]
            after_belongs = bool(after_focus and (after_focus == self.document.handle
                                                 or win32gui.IsChild(self.document.handle, after_focus)))
            if after_belongs and after_focus == focus:
                stable_samples = stable_samples + 1 if last_focus == after_focus else 1
                last_focus = after_focus
            else:
                stable_samples = 0
                last_focus = None
            self.document_ready_state.update(focus_after=after_focus, cpu_samples=cpu_samples,
                                             stable_samples=stable_samples,
                                             belongs_to_document=after_belongs,
                                             ready=stable_samples >= 2)
            if stable_samples >= 2:
                self._assert_target(self.document)
                return self.document_ready_state
            self.stop_event.wait(0.15)
        raise AutomationError(f"专用文档焦点尚未稳定，未打开检索菜单：{self.document_ready_state}")

    def attach_current_document(self, request=None):
        """只读绑定已加载文档；此适配器不再允许发起检索。"""
        from .numbers import parse_code

        if self.app is None:
            self.connect(launch_if_missing=False)
        document = self._bind_current_document()
        if request is not None:
            request = parse_code(request) if isinstance(request, str) else request
            codes = re.findall(r"(?<!\d)\d{2}-\d{3}-\d{4}(?!\d)", document.window_text())
            if len(codes) != 1 or not request.matches(codes[0]):
                raise AutomationError("当前文档标题无法核对请求编号，已暂停")
        self.document = document
        self.expected_request = request
        self.current_document_read_only = True
        return self

    def _fill_search_number(self, dialog, request):
        """只在编号框首行输入，并在提交前核对真实 Edit 文字。"""
        from pywinauto.controls.win32_controls import EditWrapper

        grids = [item for item in dialog.descendants()
                 if item.class_name() == "TStringGrid" and item.is_visible()]
        if len(grids) != 1:
            raise AutomationError("编号检索表格不唯一，已暂停")
        # VCL 会把 TStringGrid 的键盘焦点立即转给 TInplaceEdit；
        # 不对 grid 强制 SetFocus，而在已核验的首个数据单元格内激活编辑。
        self._click(grids[0], (40, 30), double=True)

        def first_edit():
            found = [item for item in dialog.descendants()
                     if item.class_name() == "TInplaceEdit" and item.is_visible()]
            if len(found) > 1:
                raise AutomationError("编号输入控件不唯一，已暂停")
            return found[0] if found else None

        edit = self._wait(first_edit, "编号首行未进入编辑状态", timeout=3)
        self._assert_foreground(edit)
        if self._input_state()["focus"] != edit.handle:
            edit.set_keyboard_focus()
        self._assert_keyboard_focus(edit)
        EditWrapper(edit.handle).set_edit_text(request.query)
        self._wait(lambda: edit.window_text().strip() == request.query,
                   "编号写入未通过读回校验，未提交检索", timeout=3)
        self._press(edit, "{TAB}")

    def _submit_search(self, dialog, request):
        self.progress("编号检索框已绑定，输入并核对首行编号")
        self._fill_search_number(dialog, request)
        try:
            self._button(dialog, "Load")
        except AutomationError:
            # 同步 BM_CLICK 会停在软件自己的模态循环里；超时后不能重复 Load。
            self._handle_search_not_found(dialog, request)
            raise
        self.progress("软件检索已返回，核对结果列表")
        self._button(dialog, "Close")
        self._wait(lambda: self._window("TfrmSearchByNumber") is None,
                   "编号检索框未关闭，可能存在软件提示")
        self._wait(lambda: self.main.is_enabled(), "检索完成状态异常，可能有软件提示")
        grid = self._accepted_grid()
        title = self.document.window_text()
        codes = re.findall(r"(?<!\d)\d{2}-\d{3}-\d{4}(?!\d)", title)
        if len(codes) != 1:
            raise AutomationError("检索结果尚未取得可核对的编号，未判断为没有命中")
        if not request.matches(codes[0]):
            raise AutomationError("软件返回的文档编号与请求不符，已停止以防误存")
        self.expected_request = request
        self.reference_source_count = self._reference_source_count(grid)
        if self.reference_source_count == 0:
            raise AutomationError("结果列表与已加载的文档编号不一致，已暂停")
        self.progress(f"已核对 {codes[0]}，下一步核对原生详情")
        return [{"code": codes[0], "index": 0, "source": None,
                 "document_title": title, "verification": "active_document_title",
                 "source_count": self.reference_source_count,
                 "source_count_unverified": self.reference_source_count is None}]

    def _handle_search_not_found(self, dialog, request):
        """只确认本次编号的已实测原生提示；其他提示保留现场。"""
        import win32gui

        notices = [item for item in self.app.windows(visible_only=True)
                   if item.class_name() == "#32770"]
        if not notices:
            return
        if len(notices) != 1:
            raise AutomationError("检索后出现多个原生提示，保留现场并暂停")
        notice = notices[0]
        self._assert_target(notice)
        if (notice.window_text() != "Information"
                or dialog.class_name() != "TfrmSearchByNumber"
                or dialog.process_id() != self.app.process
                or win32gui.GetWindow(notice.handle, 4) != dialog.handle):  # GW_OWNER
            raise AutomationError("原生提示不属于本次编号检索，保留现场并暂停")
        try:
            from pywinauto import Desktop
            from pywinauto.controls.hwndwrapper import HwndWrapper

            uia_notice = Desktop(backend="uia").window(handle=notice.handle).wrapper_object()
            if uia_notice.handle != notice.handle or uia_notice.process_id() != self.app.process:
                raise AutomationError("原生检索提示的 UIA 身份不匹配，已暂停")
            children = [item for item in uia_notice.descendants() if item.is_visible()]
            contents = [item for item in children
                        if item.element_info.control_type == "Text"
                        and item.element_info.automation_id == "ContentText"]
            # UIA 也暴露标题栏关闭按钮；只统计原生任务提示的命令按钮。
            buttons = [item for item in children if item.element_info.control_type == "Button"
                       and (item.element_info.automation_id or "").startswith("CommandButton_")]
            if len(contents) != 1 or len(buttons) != 1:
                raise AutomationError("原生检索提示的正文或按钮不唯一，保留现场并暂停")
            content, button = contents[0], buttons[0]
            expected = f'Pattern: "{request.query}" could not be found in the current database(es).'
            if (content.process_id() != self.app.process
                    or content.window_text().strip() != expected):
                raise AutomationError("原生检索提示未精确匹配本次编号的未找到信息，保留现场并暂停")
            if (button.process_id() != self.app.process
                    or button.element_info.automation_id != "CommandButton_1"
                    or button.window_text() != "OK" or not button.is_enabled()
                    or not button.handle or not win32gui.IsChild(notice.handle, button.handle)):
                raise AutomationError("原生检索提示的确认按钮身份不匹配，保留现场并暂停")
            native_ok = HwndWrapper(button.handle)
            if (native_ok.class_name() != "Button" or native_ok.window_text() != "OK"
                    or native_ok.control_id() != 0):
                raise AutomationError("原生检索提示的确认按钮未通过 Win32 核对，已暂停")
            self._assert_foreground(notice)
            self._assert_foreground(native_ok)
            if win32gui.GetWindow(notice.handle, 4) != dialog.handle:
                raise AutomationError("原生检索提示的归属发生变化，已暂停")
            self.progress(f"软件明确报告未找到 {request.query}，确认提示并关闭检索框")
            result = native_ok.send_message_timeout(0x00F5, timeout=2)
            if not isinstance(result, int):
                raise AutomationError("原生未找到提示的确认按钮未响应，已暂停")
        except AutomationError:
            raise
        except Exception as error:
            raise AutomationError("无法读取或确认原生检索提示，保留现场并暂停") from error
        self._wait(lambda: all(item.handle != notice.handle
                               for item in self.app.windows(visible_only=True)),
                   "原生未找到提示未关闭，已暂停", timeout=3)
        self._wait(lambda: dialog.is_enabled(), "编号检索框尚未恢复，已暂停", timeout=3)
        self._button(dialog, "Close")
        self._wait(lambda: self._window("TfrmSearchByNumber") is None,
                   "未找到后的编号检索框未关闭，已暂停", timeout=3)
        self._wait(lambda: self.main.is_enabled(), "未找到后的主窗口尚未恢复，已暂停", timeout=3)
        raise CardNotFound(f"本机已启用数据库未找到编号 {request.query}")

    def _reference_source_count(self, grid):
        """本机旧版 TcxTreeList 未公开行数；保持未知，不以高度或标题猜测。"""
        return None

    def resume_search(self, request):
        """接续当前已打开的编号框，不新建文档、不重新经过菜单。"""
        from .numbers import parse_code

        if self.current_document_read_only:
            raise AutomationError("当前文档为只读绑定模式，不能发起检索")
        request = parse_code(request) if isinstance(request, str) else request
        if self.app is None:
            self.connect(launch_if_missing=False)
        dialog = self._window("TfrmSearchByNumber")
        if dialog is None:
            raise AutomationError("当前未打开编号检索框，未创建新的检索")
        self._assert_target(dialog)
        # 接续操作的首步可以激活已绑定模态框；输入开始后仍逐键检查。
        try:
            dialog.set_focus()
        except Exception as error:
            raise AutomationError("编号检索框未能激活到前台，已暂停") from error
        self._wait(lambda: self._input_state()["foreground"] == dialog.handle,
                   "编号检索框未能切换到前台，已暂停", timeout=5)
        self._assert_foreground(dialog)
        self.document = self._bind_current_document()
        self.expected_request = request
        return self._submit_search(dialog, request)

    def load_reference(self, request, *, resume_open_search: bool = False):
        """在新建的独立文档中检索，并通过列表文本确认真正命中。"""
        from .numbers import parse_code

        if self.current_document_read_only:
            raise AutomationError("当前文档为只读绑定模式，不能发起检索")
        if resume_open_search:
            return self.resume_search(request)
        if isinstance(request, str):
            request = parse_code(request)
        if self.app is None:
            self.connect()
        if not self.main.is_enabled():
            raise AutomationError("HighScore 有未关闭的对话框，请关闭后重试")
        if self._window("TfrmSearchByNumber") is not None:
            raise AutomationError("编号框已打开，请接续当前检索，未创建新文档")
        if self._popup_menus():
            raise AutomationError("当前菜单仍处于打开状态，请关闭后再开始检索")
        # 只在序列开始时尝试一次激活；输入阶段发现抢焦点时立即暂停。
        self._activate_window(self.main)
        before = {item.handle for item in self.main.descendants()
                  if item.class_name() == "TfrmChild"}
        self.progress(f"创建专用文档，检索 {request.original}")
        # File → New 的原生快捷键；旧式工具栏可能只高亮而不执行点击。
        # 仍逐键核对目标进程和前台，且仅接受本次新建的唯一 MDI 文档。
        self._menu_keys("^n")

        def new_document():
            children = [item for item in self.main.descendants()
                        if item.class_name() == "TfrmChild" and item.handle not in before]
            if len(children) > 1:
                raise AutomationError("新建文档不唯一，已停止以防操作现有文档")
            return children[0] if len(children) == 1 else None

        self.document = self._wait(new_document, "未能创建专用检索文档")
        self.owned_document_handles.add(self.document.handle)
        self.expected_request = request
        if self._bind_current_document().handle != self.document.handle:
            raise AutomationError("新建文档不是当前活动文档，已暂停")
        self._wait_document_ready()
        self.progress("已创建专用文档，打开编号检索")
        dialog = self.open_reference_search()
        return self._submit_search(dialog, request)

    def _popup_menus(self):
        return [item for item in self.app.windows(visible_only=True)
                if item.class_name() == "TdxBarSubMenuControl"]

    def read_detail(self) -> dict:
        """直接读取原生详情文字与波长，不占用剪贴板、不改变文档。"""
        if self.app is None:
            self.connect(launch_if_missing=False)
        detail = self._window("TfrmShowReferencePattern")
        if detail is None:
            raise AutomationError("当前未打开原生卡片详情")
        if detail.process_id() != self.app.process:
            raise AutomationError("卡片详情不属于目标进程")
        texts = [item for item in detail.descendants()
                 if item.class_name() == "TcxRichInnerEdit" and item.is_visible()]
        waves = [item for item in detail.descendants()
                 if item.class_name() == "TRealEdit" and item.is_visible()]
        if len(texts) != 1 or len(waves) != 1:
            raise AutomationError("原生卡片文字或波长控件不唯一，已暂停")
        text = texts[0].window_text()
        wavelength = waves[0].window_text().strip()
        if not text.strip() or not re.fullmatch(r"\d+(?:\.\d+)?", wavelength) or float(wavelength) <= 0:
            raise AutomationError("原生卡片文字或波长为空或格式异常")
        codes = re.findall(r"^\s*Reference\s+code\s*:\s*(\d{2}-\d{3}-\d{4})(?!\d)",
                           text, re.I | re.M)
        if len(codes) != 1:
            raise AutomationError("原生详情中没有唯一可核对的卡片编号")
        if self.expected_request is not None and not self.expected_request.matches(codes[0]):
            raise AutomationError("原生详情编号与请求不符，已暂停以防误存")
        self.detail = detail
        self.detail_code = codes[0]
        return {"text": text, "wavelength": wavelength}

    def _save_dialog(self):
        dialog = self._window("#32770")
        if dialog is not None and dialog.window_text().strip().casefold() not in ("另存为", "save as"):
            raise AutomationError("当前存在其他软件提示，未操作保存按钮")
        return dialog

    def export_reference(self, path: Path | str) -> Path:
        """将当前详情原生另存为新 RTF；可接续已经打开的保存框。"""
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else ROOT / candidate).resolve()
        if not target.is_relative_to(ROOT.resolve()):
            raise AutomationError("原生卡片只能保存到项目目录内")
        if target.suffix.casefold() != ".rtf":
            raise AutomationError("原生卡片输出必须使用 .rtf 扩展名")
        if target.exists():
            raise AutomationError("目标文件已存在，未覆盖原有归档")
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.app is None:
            self.connect(launch_if_missing=False)
        dialog = self._save_dialog()
        if dialog is None:
            import ctypes

            self.read_detail()
            self.detail.set_focus()
            self._wait(lambda: self._input_state()["foreground"] == self.detail.handle,
                       "卡片详情未能切换到前台", timeout=5)
            toolbars = [item for item in self.detail.descendants()
                        if item.class_name() == "TToolBar" and item.is_visible()]
            if len(toolbars) != 1:
                raise AutomationError("卡片详情保存工具栏不唯一")
            toolbar = toolbars[0]
            try:
                scale = ctypes.windll.user32.GetDpiForWindow(toolbar.handle) / 96
                if scale <= 0:
                    scale = 1
            except Exception:
                scale = 1
            # VCL 工具栏跨 32/64 位返回的远程 button RECT 已实测不准。
            # 本机 SaveAs 为首个 72x29 按钮，使用已核验中心并交给 _click 核对边界。
            self._click(toolbar, (int(round(36 * scale)), int(round(14 * scale))))
            dialog = self._wait(self._save_dialog, "原生卡片另存为窗口未打开")
        self._assert_target(dialog)
        dialog.set_focus()
        self._wait(lambda: self._input_state()["foreground"] == dialog.handle,
                   "原生卡片另存为窗口未能切换到前台", timeout=5)
        self._assert_foreground(dialog)
        controls = dialog.descendants()
        filenames = [item for item in controls if item.class_name() == "Edit"
                     and item.control_id() == 1001 and item.is_visible()]
        buttons = [item for item in controls if item.class_name() == "Button"
                   and item.control_id() == 1 and item.is_visible()]
        if len(filenames) != 1 or len(buttons) != 1:
            raise AutomationError("另存为文件名或保存按钮不唯一，已暂停")
        from pywinauto.controls.win32_controls import ComboBoxWrapper, EditWrapper

        rtf_formats = []
        for item in controls:
            if item.class_name() != "ComboBox" or not item.is_visible():
                continue
            combo = ComboBoxWrapper(item.handle)
            for value in combo.item_texts():
                if re.search(r"\*\.rtf", value, re.I):
                    rtf_formats.append((item, combo, value))
        if len(rtf_formats) != 1:
            raise AutomationError("未能唯一确认原生 RTF 保存格式")
        combo_control, combo, rtf_value = rtf_formats[0]
        self._assert_foreground(combo_control)
        if combo.selected_text() != rtf_value:
            combo.select(rtf_value)
        if combo.selected_text() != rtf_value:
            raise AutomationError("RTF 保存格式未通过读回校验")
        filename = filenames[0]
        self._assert_foreground(filename)
        EditWrapper(filename.handle).set_edit_text(str(target))
        self._wait(lambda: filename.window_text() == str(target),
                   "另存为文件名未通过读回校验，未提交保存", timeout=3)
        self._assert_foreground(buttons[0])
        if target.exists():
            raise AutomationError("目标文件已存在，未覆盖原有归档")
        result = buttons[0].send_message_timeout(0x00F5, timeout=2)
        if not isinstance(result, int):
            raise AutomationError("原生卡片保存按钮未响应")

        def saved():
            old_dialog = next((item for item in self.app.windows(visible_only=True)
                               if item.handle == dialog.handle), None)
            return (old_dialog is None and target.is_file() and target.stat().st_size > 0)

        self._wait(saved, "原生卡片保存未完成；请保留当前窗口核对")
        with target.open("rb") as stream:
            if not stream.read(16).lstrip().startswith(b"{\\rtf"):
                raise AutomationError("已生成文件不是原生 RTF，未报告导出成功")
        self.progress("原生 RTF 已保存，等待归档内容校验")
        return target.relative_to(ROOT)

    def open_reference(self, row_index: int | None = None):
        existing = self._window("TfrmShowReferencePattern")
        if existing is not None:
            self.read_detail()
            self._activate_window(existing, "原生卡片详情未能切换到前台")
            return self.detail
        if row_index is None:
            if self.reference_source_count is not None and self.reference_source_count > 1:
                raise AutomationError("软件存在多个来源，请明确选择来源行后继续")
            row_index = 0
        if not isinstance(row_index, int) or isinstance(row_index, bool) or row_index < 0:
            raise AutomationError("来源行必须为从 0 开始的非负整数")
        if self.reference_source_count is not None and row_index >= self.reference_source_count:
            raise AutomationError("所选来源行超出已核对的列表范围")
        if self.reference_source_count is None:
            self.progress("来源数量尚未核实，当前仅核对选定卡片")
        self._activate_window(self.main)
        grid = self._accepted_grid()
        self.check_stop()
        rect = grid.rectangle()
        if rect.width() < 120 or rect.height() < 40:
            raise AutomationError("参考卡片列表过小，请恢复正常窗口布局")
        # 相对于已定位的参考列表；首行位置不依赖屏幕分辨率。
        import ctypes
        try:
            scale = ctypes.windll.user32.GetDpiForWindow(grid.handle) / 96
            if scale <= 0:
                scale = 1
        except Exception:
            scale = 1
        self._click(grid, (min(int(180 * scale), rect.width() - 5),
                           int((32 + 20 * row_index) * scale)), double=True)

        self.detail = self._wait(lambda: self._window("TfrmShowReferencePattern"),
                                 "卡片详情未打开")
        self.read_detail()
        self.progress("已打开卡片详情")
        return self.detail

    def close_detail(self):
        """只关闭此适配器已经绑定的原生详情，不关闭样品文档。"""
        if self.detail is None:
            return False
        current = self._window("TfrmShowReferencePattern")
        if current is None:
            self.detail = None
            self.detail_code = None
            return False
        if current.handle != self.detail.handle:
            raise AutomationError("当前卡片详情与已绑定窗口不一致，未关闭")
        self._activate_window(current, "原生卡片详情未能切换到前台")
        self._assert_foreground(current)
        current.post_message(0x0010)  # WM_CLOSE，仅向已绑定的详情窗口发送
        self._wait(lambda: not any(item.handle == current.handle
                                   for item in self.app.windows(visible_only=True)),
                   "原生卡片详情未关闭，请检查软件提示")
        self.detail = None
        self.detail_code = None
        return True

    def inspect(self) -> dict:
        """仅读取目标进程的控件，用于迁移和版本差异诊断。"""
        if self.app is None:
            self.connect()
        windows = []
        for window in self.app.windows(visible_only=True):
            controls = []
            for control in window.descendants()[:500]:
                try:
                    rect = control.rectangle()
                    controls.append({
                        "handle": control.handle,
                        "class": control.class_name(),
                        "text": control.window_text()[:1200],
                        "control_id": control.control_id(),
                        "visible": control.is_visible(),
                        "enabled": control.is_enabled(),
                        "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    })
                except Exception:
                    continue
            windows.append({
                "handle": window.handle,
                "class": window.class_name(),
                "title": window.window_text(),
                "rect": [window.rectangle().left, window.rectangle().top,
                         window.rectangle().right, window.rectangle().bottom],
                "controls": controls,
            })
        try:
            executable = os.path.relpath(self.executable, ROOT)
        except ValueError:
            executable = "由 Windows 安装信息发现"
        return {"process_id": self.app.process, "executable": executable, "windows": windows,
                "input_state": self._input_state(),
                "document_ready_state": self.document_ready_state,
                "menu_trace": self.menu_trace,
                "reference_source_count": self.reference_source_count,
                "source_count_unverified": self.reference_source_count is None}
