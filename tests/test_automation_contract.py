"""RPA 不依赖屏幕坐标的契约检查。"""
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from highscore_downloader.automation import AutomationError, CardNotFound, HighScoreAutomation, ROOT


class Control:
    def __init__(self, handle, class_name="TInplaceEdit", text="", descendants=(), control_id=0):
        self.handle = handle
        self._class = class_name
        self._text = text
        self._descendants = list(descendants)
        self._id = control_id
        self.set_focus = Mock()
        self.set_keyboard_focus = Mock()
        self.type_keys = Mock()
        self.send_message_timeout = Mock(return_value=0)
        self.post_message = Mock()

    def class_name(self):
        return self._class

    def window_text(self):
        return self._text

    def descendants(self):
        return self._descendants

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def process_id(self):
        return 42

    def control_id(self):
        return self._id

    def client_rect(self):
        return SimpleNamespace(width=lambda: 200, height=lambda: 60)

    def client_to_screen(self, coords):
        return tuple(coords)

    def rectangle(self):
        return SimpleNamespace(left=0, top=0, right=200, bottom=60,
                               width=lambda: 200, height=lambda: 60)


class AutomationContractTests(unittest.TestCase):
    def guard(self, executable, title):
        try:
            from highscore_downloader.automation import is_highscore_window
        except ImportError:
            self.fail("尚未实现 HighScore 目标窗口校验")
        return is_highscore_window(executable, title)

    def test_unrelated_process_cannot_impersonate_title(self):
        self.assertFalse(self.guard("notepad.exe", "HighScore Plus"))

    def test_child_document_title_is_supported(self):
        self.assertTrue(self.guard("HighScorePlus.EXE", "HighScore Plus - [Untitled1]"))

    def test_similar_program_name_is_rejected(self):
        self.assertFalse(self.guard("FakeHighScorePlus.exe", "HighScore Plus"))

    def test_relative_executable_is_resolved_from_project_root(self):
        with patch("os.getcwd", return_value=str(ROOT / ".cache")):
            adapter = HighScoreAutomation("bin/HighScorePlus.exe")
        self.assertEqual(adapter.executable, (ROOT / "bin" / "HighScorePlus.exe").resolve())


class ConnectionSafetyTests(unittest.TestCase):
    class ProcessNotFoundError(Exception):
        pass

    def setUp(self):
        cache = ROOT / ".cache" / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.directory.cleanup)
        self.executable = Path(self.directory.name) / "HighScorePlus.exe"
        self.executable.write_bytes(b"unit test only")
        main = Control(1, "TfrmMain", "HighScore Plus")
        self.spec = SimpleNamespace(wait=Mock(), wrapper_object=lambda: main)
        self.application = SimpleNamespace(connect=Mock(), start=Mock(), window=Mock(return_value=self.spec))
        self.application.start.return_value = self.application
        self.application.connect.return_value = self.application
        self.factory = Mock(return_value=self.application)
        module = SimpleNamespace(Application=self.factory, ProcessNotFoundError=self.ProcessNotFoundError)
        self.modules = patch.dict("sys.modules", {"pywinauto.application": module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_permission_failure_never_starts_duplicate_process(self):
        self.application.connect.side_effect = PermissionError("access denied")
        with self.assertRaisesRegex(AutomationError, "连接.*失败"):
            HighScoreAutomation(self.executable).connect()
        self.application.start.assert_not_called()

    def test_only_explicit_process_not_found_can_start_application(self):
        self.application.connect.side_effect = self.ProcessNotFoundError("not running")
        HighScoreAutomation(self.executable).connect()
        self.application.start.assert_called_once_with(
            f'"{self.executable}"', work_dir=str(self.executable.parent), wait_for_idle=False)
        self.assertEqual(self.application.window.call_args.kwargs["class_name"], "TfrmMain")

    def test_attach_only_never_starts_missing_application(self):
        self.application.connect.side_effect = self.ProcessNotFoundError("not running")
        with self.assertRaises(AutomationError):
            HighScoreAutomation(self.executable).connect(launch_if_missing=False)
        self.application.start.assert_not_called()


class InputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HighScoreAutomation(timeout=0.01)
        self.adapter.app = SimpleNamespace(process=42)
        self.adapter.main = Control(1, "TfrmMain")
        self.edit = Control(5)
        self.state = {"foreground": 2, "foreground_pid": 42, "active": 2, "focus": 5}
        self.adapter._input_state = Mock(side_effect=lambda: dict(self.state))
        self.adapter._popup_menus = Mock(return_value=[])
        self.gui = SimpleNamespace(
            GetForegroundWindow=lambda: self.state["foreground"],
            GetAncestor=lambda handle, flag: 2 if handle in (2, 5, 6) else 1,
            IsChild=lambda parent, child: parent == 2 and child in (5, 6),
            IsWindow=lambda handle: True,
        )
        self.process = SimpleNamespace(GetWindowThreadProcessId=lambda handle: (9, 42))
        self.modules = patch.dict("sys.modules", {"win32gui": self.gui, "win32process": self.process})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.sender = patch.object(self.adapter, "_send_keys", create=True)
        self.send_keys = self.sender.start()
        self.addCleanup(self.sender.stop)

    def test_same_process_wrong_keyboard_focus_is_rejected(self):
        self.state["focus"] = 6
        with self.assertRaisesRegex(AutomationError, "键盘焦点"):
            self.adapter._press(self.edit, "19-0629")
        self.send_keys.assert_not_called()
        self.edit.type_keys.assert_not_called()

    def test_child_keyboard_focus_is_set_and_verified(self):
        self.state["focus"] = 6
        self.edit.set_keyboard_focus.side_effect = lambda: self.state.update(focus=5)
        self.adapter._press(self.edit, "19-0629")
        self.edit.set_keyboard_focus.assert_called_once()
        self.edit.set_focus.assert_not_called()
        self.edit.type_keys.assert_not_called()
        self.send_keys.assert_called_once()

    def test_foreign_foreground_pauses_without_stealing_focus(self):
        self.state["foreground_pid"] = 99
        with self.assertRaisesRegex(AutomationError, "前台"):
            self.adapter._press(self.edit, "19-0629")
        self.edit.set_keyboard_focus.assert_not_called()
        self.edit.set_focus.assert_not_called()
        self.send_keys.assert_not_called()

    def test_foreign_control_is_rejected(self):
        self.edit.process_id = lambda: 99
        with self.assertRaisesRegex(AutomationError, "目标进程"):
            self.adapter._press(self.edit, "19-0629")
        self.send_keys.assert_not_called()

    def test_button_uses_bound_bm_click(self):
        button = Control(6, "TButton", "&Load")
        dialog = Control(2, "TfrmSearchByNumber", descendants=[button])
        fake_buttons = SimpleNamespace(ButtonWrapper=Mock(return_value=SimpleNamespace(click=Mock())))
        with patch.dict("sys.modules", {"pywinauto.controls.win32_controls": fake_buttons}):
            self.adapter._button(dialog, "Load")
        button.send_message_timeout.assert_called_once_with(0x00F5, timeout=2)

    def test_button_is_not_submitted_after_foreground_changes(self):
        button = Control(6, "TButton", "Load")
        dialog = Control(2, "TfrmSearchByNumber", descendants=[button])
        self.state["foreground_pid"] = 99
        fake_buttons = SimpleNamespace(ButtonWrapper=Mock(return_value=SimpleNamespace(click=Mock())))
        with patch.dict("sys.modules", {"pywinauto.controls.win32_controls": fake_buttons}):
            with self.assertRaisesRegex(AutomationError, "前台"):
                self.adapter._button(dialog, "Load")
        button.send_message_timeout.assert_not_called()

    def test_resume_search_uses_existing_dialog_and_active_document(self):
        self.assertTrue(callable(getattr(self.adapter, "resume_search", None)), "必须支持接续当前检索框")
        dialog = Control(2, "TfrmSearchByNumber")
        document = Control(3, "TfrmChild")
        self.adapter._window = Mock(return_value=dialog)
        self.adapter._bind_current_document = Mock(return_value=document)
        self.adapter._submit_search = Mock(return_value=[{"code": "00-019-0629"}])
        result = self.adapter.resume_search("19-0629")
        self.assertEqual(result, [{"code": "00-019-0629"}])
        self.assertIs(self.adapter.document, document)
        self.adapter._submit_search.assert_called_once()
        self.adapter.main.set_focus.assert_not_called()

    def test_resume_search_activates_existing_modal_before_input(self):
        dialog = Control(2, "TfrmSearchByNumber")
        self.state.update(foreground=20, foreground_pid=99, active=0, focus=0)
        dialog.set_focus.side_effect = lambda: self.state.update(
            foreground=2, foreground_pid=42, active=2, focus=5)
        self.adapter._window = Mock(return_value=dialog)
        self.adapter._bind_current_document = Mock(return_value=Control(3, "TfrmChild"))
        self.adapter._submit_search = Mock(return_value=[])
        self.adapter.resume_search("19-0629")
        dialog.set_focus.assert_called_once()
        self.adapter._submit_search.assert_called_once()

    def test_attached_document_cannot_start_new_search(self):
        self.assertTrue(callable(getattr(self.adapter, "attach_current_document", None)), "必须支持仅绑定当前文档")
        document = Control(3, "TfrmChild", "00-019-0629")
        self.adapter._bind_current_document = Mock(return_value=document)
        self.adapter.attach_current_document("19-0629")
        with self.assertRaisesRegex(AutomationError, "当前文档.*只读"):
            self.adapter.load_reference("19-0629")

    def test_same_process_sibling_cannot_receive_click(self):
        self.gui.WindowFromPoint = lambda point: 6
        mouse = SimpleNamespace(click=Mock(), double_click=Mock())
        with patch.dict("sys.modules", {"pywinauto": SimpleNamespace(mouse=mouse)}):
            with self.assertRaisesRegex(AutomationError, "遮挡"):
                self.adapter._click(self.edit, (20, 20))
        mouse.click.assert_not_called()

    def test_search_text_must_pass_readback_before_tab(self):
        from highscore_downloader.numbers import parse_code

        grid = Control(7, "TStringGrid")
        dialog = Control(2, "TfrmSearchByNumber", descendants=[grid, self.edit])
        self.adapter._press = Mock()
        self.adapter._click = Mock()
        fake_edit = SimpleNamespace(set_edit_text=Mock())
        wrappers = SimpleNamespace(EditWrapper=Mock(return_value=fake_edit))
        with patch.dict("sys.modules", {"pywinauto.controls.win32_controls": wrappers}):
            with self.assertRaisesRegex(AutomationError, "读回校验"):
                self.adapter._fill_search_number(dialog, parse_code("19-0629"))
        self.assertNotIn(unittest.mock.call(self.edit, "{TAB}"), self.adapter._press.call_args_list)

    def test_search_first_row_is_activated_without_forcing_grid_keyboard_focus(self):
        from highscore_downloader.numbers import parse_code

        grid = Control(7, "TStringGrid")
        dialog = Control(2, "TfrmSearchByNumber", descendants=[grid, self.edit])
        self.adapter._press = Mock()
        self.adapter._click = Mock()
        fake_edit = SimpleNamespace(set_edit_text=lambda text: setattr(self.edit, "_text", text))
        wrappers = SimpleNamespace(EditWrapper=Mock(return_value=fake_edit))
        with patch.dict("sys.modules", {"pywinauto.controls.win32_controls": wrappers}):
            self.adapter._fill_search_number(dialog, parse_code("19-0629"))
        self.adapter._click.assert_called_once_with(grid, (40, 30), double=True)
        self.adapter._press.assert_called_once_with(self.edit, "{TAB}")

    def test_loaded_document_identity_does_not_require_clipboard(self):
        from highscore_downloader.numbers import parse_code

        self.adapter.document = Control(3, "TfrmChild", "00-019-0629")
        self.adapter._fill_search_number = Mock()
        self.adapter._button = Mock()
        self.adapter._window = Mock(return_value=None)
        self.adapter._accepted_grid = Mock(return_value=Control(7, "TcxTreeList"))
        self.adapter._reference_source_count = Mock(return_value=1)
        self.adapter._copy_list = Mock(side_effect=AssertionError("不能访问剪贴板"))
        rows = self.adapter._submit_search(Control(2), parse_code("19-0629"))
        self.assertEqual(rows[0]["code"], "00-019-0629")
        self.assertEqual(rows[0]["verification"], "active_document_title")
        self.adapter._copy_list.assert_not_called()

    def test_unverified_result_is_not_reported_as_not_found(self):
        from highscore_downloader.automation import CardNotFound
        from highscore_downloader.numbers import parse_code

        self.adapter.document = Control(3, "TfrmChild", "Untitled1")
        self.adapter._fill_search_number = Mock()
        self.adapter._button = Mock()
        self.adapter._window = Mock(return_value=None)
        self.adapter._accepted_grid = Mock(return_value=Control(7, "TcxTreeList"))
        self.adapter._reference_source_count = Mock(return_value=None)
        self.adapter._copy_list = Mock(return_value="")
        with self.assertRaises(AutomationError) as result:
            self.adapter._submit_search(Control(2), parse_code("19-0629"))
        self.assertNotIsInstance(result.exception, CardNotFound)

    def test_known_multiple_sources_require_explicit_row_selection(self):
        self.adapter.reference_source_count = 2
        self.adapter._window = Mock(return_value=None)
        self.adapter._accepted_grid = Mock(return_value=Control(7, "TcxTreeList"))
        self.adapter._press = Mock()
        self.adapter._click = Mock()
        self.adapter.app.windows = Mock(return_value=[])
        with self.assertRaisesRegex(AutomationError, "来源"):
            self.adapter.open_reference()
        self.adapter._click.assert_not_called()


class SearchNotFoundTests(unittest.TestCase):
    def setUp(self):
        from highscore_downloader.numbers import parse_code

        self.request = parse_code("99-999-9999")
        self.adapter = HighScoreAutomation(timeout=0.01)
        self.adapter.main = Control(1, "TfrmMain")
        self.adapter.document = Control(3, "TfrmChild", "Untitled1")
        self.adapter._fill_search_number = Mock()
        self.adapter._accepted_grid = Mock(return_value=Control(8, "TcxTreeList"))
        self.load = Control(6, "TButton", "Load")
        self.close = Control(7, "TButton", "Close")
        self.dialog = Control(2, "TfrmSearchByNumber", descendants=[self.load, self.close])
        self.notice = Control(9, "#32770", "Information")
        self.content = Control(None, "Text", 'Pattern: "99-999-9999" could not be found in the current database(es).')
        self.content.element_info = SimpleNamespace(control_type="Text", automation_id="ContentText")
        self.uia_ok = Control(10, "Button", "OK")
        self.uia_ok.element_info = SimpleNamespace(control_type="Button", automation_id="CommandButton_1")
        self.uia_notice = Control(9, "#32770", "Information", [self.content, self.uia_ok])
        self.native_ok = Control(10, "Button", "OK", control_id=0)
        self.windows = [self.dialog]
        self.adapter.app = SimpleNamespace(process=42, windows=lambda **kwargs: list(self.windows))
        self.state = {"foreground": 2, "foreground_pid": 42, "active": 2, "focus": 6}
        self.adapter._input_state = Mock(side_effect=lambda: dict(self.state))
        self.owner = 2
        self.gui = SimpleNamespace(
            IsWindow=lambda handle: True,
            GetWindow=lambda handle, flag: self.owner if handle == 9 and flag == 4 else 0,
            GetAncestor=lambda handle, flag: 9 if handle in (9, 10) else 2,
            IsChild=lambda parent, child: parent == 9 and child == 10,
        )
        self.process = SimpleNamespace(GetWindowThreadProcessId=lambda handle: (1, 42))
        self.desktop = SimpleNamespace(window=Mock(return_value=SimpleNamespace(wrapper_object=lambda: self.uia_notice)))
        self.wrapper = Mock(return_value=self.native_ok)
        modules = {
            "win32gui": self.gui,
            "win32process": self.process,
            "pywinauto": SimpleNamespace(Desktop=Mock(return_value=self.desktop)),
            "pywinauto.controls.hwndwrapper": SimpleNamespace(HwndWrapper=self.wrapper),
        }
        self.modules = patch.dict("sys.modules", modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.load.send_message_timeout.side_effect = self.show_notice
        self.native_ok.send_message_timeout.side_effect = self.dismiss_notice
        self.close.send_message_timeout.side_effect = self.close_search
        self.dialog.is_enabled = lambda: self.notice not in self.windows

    def show_notice(self, *args, **kwargs):
        self.windows.append(self.notice)
        self.state.update(foreground=9, active=9, focus=10)
        return "SendMessageTimeout: timeout"

    def dismiss_notice(self, *args, **kwargs):
        self.windows.remove(self.notice)
        self.state.update(foreground=2, active=2, focus=6)
        return 0

    def close_search(self, *args, **kwargs):
        self.windows.remove(self.dialog)
        self.state.update(foreground=1, active=1, focus=3)
        return 0

    def submit_error(self):
        with self.assertRaises(AutomationError) as result:
            self.adapter._submit_search(self.dialog, self.request)
        return result.exception

    def assert_paused_without_acknowledgement(self):
        self.assertNotIsInstance(self.submit_error(), CardNotFound)
        self.native_ok.send_message_timeout.assert_not_called()
        self.close.send_message_timeout.assert_not_called()
        self.assertIn(self.notice, self.windows)

    def test_exact_owned_not_found_prompt_is_acknowledged_and_search_closed(self):
        self.assertIsInstance(self.submit_error(), CardNotFound)
        self.assertNotIn(self.notice, self.windows)
        self.assertNotIn(self.dialog, self.windows)
        self.load.send_message_timeout.assert_called_once_with(0x00F5, timeout=2)
        self.native_ok.send_message_timeout.assert_called_once_with(0x00F5, timeout=2)
        self.close.send_message_timeout.assert_called_once_with(0x00F5, timeout=2)
        self.adapter._accepted_grid.assert_not_called()

    def test_title_bar_close_button_is_not_a_task_confirmation_button(self):
        title_close = Control(None, "Button", "关闭")
        title_close.element_info = SimpleNamespace(control_type="Button", automation_id="")
        title_bar = Control(None, "TitleBar", "Information", [title_close])
        title_bar.element_info = SimpleNamespace(control_type="TitleBar", automation_id="")
        self.uia_notice._descendants.extend([title_bar, title_close])
        self.assertIsInstance(self.submit_error(), CardNotFound)
        self.native_ok.send_message_timeout.assert_called_once_with(0x00F5, timeout=2)
        self.assertNotIn(self.dialog, self.windows)

    def test_different_reference_code_is_never_acknowledged(self):
        self.content._text = self.content._text.replace("99-999-9999", "00-019-0629")
        self.assert_paused_without_acknowledgement()

    def test_other_message_template_is_never_acknowledged(self):
        self.content._text = "The current database could not be opened."
        self.assert_paused_without_acknowledgement()

    def test_prompt_owned_by_other_window_is_never_acknowledged(self):
        self.owner = 1
        self.assert_paused_without_acknowledgement()

    def test_foreign_process_prompt_is_never_acknowledged(self):
        self.notice.process_id = lambda: 99
        self.assert_paused_without_acknowledgement()

    def test_ambiguous_native_prompts_are_never_acknowledged(self):
        self.windows.append(Control(11, "#32770", "Information"))
        self.assert_paused_without_acknowledgement()

    def test_content_text_must_belong_to_bound_process(self):
        self.content.process_id = lambda: 99
        self.assert_paused_without_acknowledgement()

    def test_duplicate_content_text_is_never_acknowledged(self):
        self.uia_notice._descendants.append(self.content)
        self.assert_paused_without_acknowledgement()

    def test_ok_button_requires_verified_automation_identity(self):
        self.uia_ok.element_info.automation_id = "OtherButton"
        self.assert_paused_without_acknowledgement()

    def test_foreground_change_does_not_acknowledge_prompt(self):
        def lost_focus(*args, **kwargs):
            self.show_notice()
            self.state.update(foreground=20, foreground_pid=99)
            return "SendMessageTimeout: timeout"

        self.load.send_message_timeout.side_effect = lost_focus
        self.assert_paused_without_acknowledgement()

    def test_timeout_without_explicit_prompt_stays_automation_error(self):
        self.load.send_message_timeout.side_effect = None
        self.load.send_message_timeout.return_value = "SendMessageTimeout: timeout"
        error = self.submit_error()
        self.assertNotIsInstance(error, CardNotFound)
        self.native_ok.send_message_timeout.assert_not_called()
        self.close.send_message_timeout.assert_not_called()


class KeySequenceSafetyTests(unittest.TestCase):
    def test_focus_loss_releases_held_modifier_without_typing_next_key(self):
        adapter = HighScoreAutomation()
        control = Control(5)
        ctrl_down = SimpleNamespace(key=17, down=True, up=False, run=Mock())
        next_key = SimpleNamespace(key=65, down=True, up=True, run=Mock())
        release = SimpleNamespace(run=Mock())
        keyboard = SimpleNamespace(parse_keys=Mock(return_value=[ctrl_down, next_key]),
                                   VirtualKeyAction=Mock(return_value=release))
        adapter._assert_keyboard_focus = Mock(side_effect=[{}, AutomationError("已失去前台")])
        with patch.dict("sys.modules", {"pywinauto": SimpleNamespace(keyboard=keyboard)}):
            with self.assertRaisesRegex(AutomationError, "前台"):
                adapter._send_keys(control, "^a")
        ctrl_down.run.assert_called_once()
        next_key.run.assert_not_called()
        keyboard.VirtualKeyAction.assert_called_once_with(17, down=False, up=True)
        release.run.assert_called_once()


class MenuNavigationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HighScoreAutomation(timeout=0.01)
        self.adapter.app = SimpleNamespace(process=42)
        self.menu_bar = Control(4, "TdxBarControl", "Main Menu")
        self.adapter.main = Control(1, "TfrmMain", descendants=[self.menu_bar])
        self.adapter.document = Control(3, "TfrmChild")
        self.adapter._activate_window = Mock()
        self.adapter._trace_menu = Mock(return_value={})
        self.adapter._assert_menu_foreground = Mock()
        self.menus = []
        self.dialog = None
        self.adapter._popup_menus = Mock(side_effect=lambda: list(self.menus))
        self.adapter._window = Mock(side_effect=lambda name: self.dialog if name == "TfrmSearchByNumber" else None)

        def key_effect(keys):
            if keys == "%p":
                self.menus.append(Control(10, "TdxBarSubMenuControl"))
            elif keys == "{RIGHT}":
                self.menus.append(Control(11, "TdxBarSubMenuControl"))
            elif keys == "{ENTER}":
                self.menus.clear()
                self.dialog = Control(12, "TfrmSearchByNumber")

        self.adapter._menu_keys = Mock(side_effect=key_effect)

        def click_effect(control, coords):
            if control.handle == self.menu_bar.handle:
                popup = Control(10, "TdxBarSubMenuControl")
                popup.rectangle = lambda: SimpleNamespace(left=194, top=46, right=482, bottom=370,
                                                          width=lambda: 288, height=lambda: 324)
                self.menus.append(popup)
            elif control.handle == 10:
                self.menus.append(Control(11, "TdxBarSubMenuControl"))

        self.adapter._click = Mock(side_effect=click_effect)

    def test_number_search_menu_is_navigated_by_three_guarded_keyboard_phases(self):
        self.assertTrue(callable(getattr(self.adapter, "open_reference_search", None)), "必须提供分段菜单入口")
        result = self.adapter.open_reference_search()
        self.assertEqual(result.class_name(), "TfrmSearchByNumber")
        self.assertEqual([call.args[0] for call in self.adapter._menu_keys.call_args_list],
                         ["{END}", "{ENTER}"])
        self.assertEqual([call.args[1] for call in self.adapter._click.call_args_list],
                         [(260, 13), (100, 15)])
        self.adapter._activate_window.assert_called_once_with(self.adapter.main)

    def test_submenu_step_rejects_missing_parent_menu_without_sending_keys(self):
        self.assertTrue(callable(getattr(self.adapter, "reference_menu_step", None)), "必须提供逐阶段探测")
        with self.assertRaisesRegex(AutomationError, "菜单状态"):
            self.adapter.reference_menu_step("retrieve")
        self.adapter._menu_keys.assert_not_called()
        self.adapter._click.assert_not_called()

    def test_menu_foreground_guard_does_not_require_main_keyboard_focus(self):
        adapter = HighScoreAutomation()
        self.assertTrue(callable(getattr(adapter, "_assert_menu_foreground", None)), "必须使用菜单专用前台校验")
        adapter.app = SimpleNamespace(process=42)
        adapter.main = Control(1, "TfrmMain")
        adapter._assert_target = Mock()
        adapter._input_state = Mock(return_value={"foreground": 1, "foreground_pid": 42, "focus": 99})
        adapter._assert_menu_foreground()
        adapter.main.set_focus.assert_not_called()
        adapter.main.set_keyboard_focus.assert_not_called()

    def test_menu_step_allows_vcl_to_process_asynchronous_key_events(self):
        adapter = HighScoreAutomation()
        adapter.main = Control(1, "TfrmMain")
        adapter._assert_menu_foreground = Mock()
        adapter._send_keys = Mock()
        adapter.stop_event = SimpleNamespace(wait=Mock(), is_set=lambda: False)
        adapter._menu_keys("{RIGHT}")
        adapter.stop_event.wait.assert_called_once_with(0.15)

    def test_retrieve_accepts_submenu_that_vcl_opened_automatically(self):
        self.menus.extend([Control(10, "TdxBarSubMenuControl"), Control(11, "TdxBarSubMenuControl")])
        try:
            self.adapter.reference_menu_step("retrieve")
        except AutomationError as error:
            self.fail(f"合法提前展开不应暂停：{error}")
        self.adapter._menu_keys.assert_not_called()

    def test_open_accepts_click_automatically_expanding_retrieve_submenu(self):
        original = self.adapter._click.side_effect

        def auto_expand(control, coords):
            original(control, coords)
            if control.handle == self.menu_bar.handle:
                self.menus.append(Control(11, "TdxBarSubMenuControl"))

        self.adapter._click.side_effect = auto_expand
        try:
            self.adapter.reference_menu_step("open")
        except AutomationError as error:
            self.fail(f"点击后提前展开不应暂停：{error}")
        self.assertEqual(len(self.menus), 2)

    def test_folded_menu_is_expanded_before_retrieve_can_click(self):
        self.assertTrue(callable(getattr(self.adapter, "_ensure_reference_menu_expanded", None)),
                        "不能点击仍在重建的折叠菜单")
        folded = Control(10, "TdxBarSubMenuControl")
        folded.rectangle = lambda: SimpleNamespace(width=lambda: 196, height=lambda: 53)
        expanded = Control(10, "TdxBarSubMenuControl")
        self.menus.append(folded)
        self.adapter._wait_expanded_reference_menu = Mock(side_effect=[None, expanded])
        self.adapter._click = Mock()
        result = self.adapter._ensure_reference_menu_expanded()
        self.assertIs(result, expanded)
        self.adapter._click.assert_called_once_with(folded, (98, 40))

    def test_open_must_wait_for_actual_expanded_menu_geometry(self):
        expanded = Control(10, "TdxBarSubMenuControl")
        self.adapter._ensure_reference_menu_expanded = Mock(return_value=expanded)
        self.adapter.reference_menu_step("open")
        self.adapter._ensure_reference_menu_expanded.assert_called_once()


class DocumentReadinessTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HighScoreAutomation()
        self.adapter.app = SimpleNamespace(process=42, wait_cpu_usage_lower=Mock())
        self.adapter.main = Control(1, "TfrmMain")
        self.adapter.document = Control(3, "TfrmChild")
        self.adapter._assert_foreground = Mock()
        self.adapter._assert_target = Mock()
        self.adapter._bind_current_document = Mock(return_value=self.adapter.document)
        self.adapter.stop_event = SimpleNamespace(is_set=lambda: False, wait=Mock())
        gui = SimpleNamespace(IsChild=lambda parent, child: parent == 3 and child in (5, 6),
                              IsWindow=lambda handle: True)
        process = SimpleNamespace(GetWindowThreadProcessId=lambda handle: (9, 42))
        self.modules = patch.dict("sys.modules", {"win32gui": gui, "win32process": process})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_new_document_requires_two_idle_samples_with_stable_descendant_focus(self):
        self.assertTrue(callable(getattr(self.adapter, "_wait_document_ready", None)), "新文档必须等待初始化完成")
        self.adapter._input_state = Mock(return_value={"foreground": 1, "foreground_pid": 42, "focus": 5})
        self.adapter._wait_document_ready()
        self.assertEqual(self.adapter.app.wait_cpu_usage_lower.call_count, 2)
        for call in self.adapter.app.wait_cpu_usage_lower.call_args_list:
            self.assertEqual(call.kwargs["threshold"], 5)
            self.assertEqual(call.kwargs["usage_interval"], 0.5)
            self.assertLessEqual(call.kwargs["timeout"], 10)

    def test_document_focus_change_restarts_stable_idle_sample_count(self):
        self.assertTrue(callable(getattr(self.adapter, "_wait_document_ready", None)), "新文档必须等待初始化完成")
        states = [{"foreground": 1, "foreground_pid": 42, "focus": focus}
                  for focus in (5, 6, 6, 6, 6, 6)]
        self.adapter._input_state = Mock(side_effect=states)
        self.adapter._wait_document_ready()
        self.assertEqual(self.adapter.app.wait_cpu_usage_lower.call_count, 3)

    def test_load_waits_for_document_ready_before_opening_any_menu(self):
        adapter = self.adapter
        events = []
        toolbar = Control(4, "TdxBarControl", "Standard Toolbar")
        document = adapter.document
        adapter.main._descendants = [toolbar]
        adapter._activate_window = Mock()
        adapter._popup_menus = Mock(return_value=[])
        adapter._window = Mock(return_value=None)
        adapter._menu_keys = Mock(side_effect=lambda *args, **kwargs: adapter.main._descendants.append(document))
        adapter._wait_document_ready = Mock(side_effect=lambda: events.append("ready"))
        adapter.open_reference_search = Mock(side_effect=lambda: events.append("menu") or Control(2))
        adapter._submit_search = Mock(return_value=[])
        adapter.load_reference("25-1376")
        self.assertEqual(events, ["ready", "menu"])

    def test_new_document_uses_native_shortcut_when_toolbar_click_does_not_create_document(self):
        adapter = self.adapter
        adapter.timeout = 0.02
        document = adapter.document
        toolbar = Control(4, "TdxBarControl", "Standard Toolbar")
        adapter.main._descendants = [toolbar]
        adapter._activate_window = Mock()
        adapter._popup_menus = Mock(return_value=[])
        adapter._window = Mock(return_value=None)
        adapter._click = Mock()  # 现场工具栏只高亮，未生成新的 MDI 文档。
        adapter._menu_keys = Mock(side_effect=lambda keys: adapter.main._descendants.append(document)
                                  if keys == "^n" else None)
        adapter._wait_document_ready = Mock()
        adapter.open_reference_search = Mock(return_value=Control(2))
        adapter._submit_search = Mock(return_value=[{"code": "00-030-0820"}])
        result = adapter.load_reference("30-0820")
        self.assertEqual(result[0]["code"], "00-030-0820")
        adapter._menu_keys.assert_called_once_with("^n")
        adapter._click.assert_not_called()
        self.assertIn(document.handle, adapter.owned_document_handles)

    def test_ignored_new_shortcut_never_reuses_existing_document(self):
        adapter = self.adapter
        adapter.timeout = 0.02
        existing_document = adapter.document
        adapter.main._descendants = [existing_document]
        adapter._activate_window = Mock()
        adapter._popup_menus = Mock(return_value=[])
        adapter._window = Mock(return_value=None)
        adapter._menu_keys = Mock()
        adapter.open_reference_search = Mock()
        adapter._wait_document_ready = Mock()
        with self.assertRaisesRegex(AutomationError, "未能创建专用检索文档"):
            adapter.load_reference("30-0820")
        adapter.open_reference_search.assert_not_called()
        adapter._wait_document_ready.assert_not_called()
        self.assertNotIn(existing_document.handle, adapter.owned_document_handles)

    def test_new_document_initially_hidden_is_observed_until_ready(self):
        self.assertTrue(callable(getattr(self.adapter, "_wait_document_ready", None)), "新文档必须等待初始化完成")
        self.adapter._assert_target = HighScoreAutomation._assert_target.__get__(self.adapter)
        states = [False, True]
        self.adapter.document.is_visible = Mock(side_effect=lambda: states.pop(0) if len(states) > 1 else states[0])
        self.adapter._input_state = Mock(return_value={"foreground": 1, "foreground_pid": 42, "focus": 5})
        try:
            self.adapter._wait_document_ready()
        except AutomationError as error:
            self.fail(f"新建文档的短暂隐藏不应直接拒绝：{error}")
        self.assertEqual(self.adapter.app.wait_cpu_usage_lower.call_count, 2)


class NativeExportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HighScoreAutomation(timeout=0.01)
        self.adapter.app = SimpleNamespace(process=42)
        self.adapter.main = Control(1, "TfrmMain")

    def test_detail_is_read_directly_from_native_controls(self):
        self.assertTrue(callable(getattr(self.adapter, "read_detail", None)), "必须支持读取原生详情")
        text = Control(5, "TcxRichInnerEdit", "Reference code: 00-019-0629\nIron Oxide")
        wavelength = Control(6, "TRealEdit", "1.54060")
        detail = Control(2, "TfrmShowReferencePattern", descendants=[text, wavelength])
        self.adapter._window = Mock(return_value=detail)
        self.assertEqual(self.adapter.read_detail(), {"text": text.window_text(), "wavelength": "1.54060"})

    def test_detail_must_match_requested_reference_code(self):
        from highscore_downloader.numbers import parse_code

        self.adapter.expected_request = parse_code("19-0630")
        text = Control(5, "TcxRichInnerEdit", "Reference code: 00-019-0629\nIron Oxide")
        wavelength = Control(6, "TRealEdit", "1.54060")
        detail = Control(2, "TfrmShowReferencePattern", descendants=[text, wavelength])
        self.adapter._window = Mock(return_value=detail)
        with self.assertRaisesRegex(AutomationError, "编号.*不符"):
            self.adapter.read_detail()

    def test_only_bound_native_detail_is_closed(self):
        self.assertTrue(callable(getattr(self.adapter, "close_detail", None)), "必须支持关闭已绑定详情")
        detail = Control(2, "TfrmShowReferencePattern")
        self.adapter.detail = detail
        self.adapter._window = Mock(return_value=detail)
        self.adapter._activate_window = Mock()
        self.adapter._assert_foreground = Mock()
        self.adapter.app.windows = Mock(return_value=[])
        self.adapter.close_detail()
        detail.post_message.assert_called_once_with(0x0010)
        self.assertIsNone(self.adapter.detail)

    def test_export_rejects_path_outside_project_before_ui(self):
        self.assertTrue(callable(getattr(self.adapter, "export_reference", None)), "必须支持接续原生导出")
        self.adapter._window = Mock()
        with self.assertRaisesRegex(AutomationError, "项目目录"):
            self.adapter.export_reference(ROOT.parent / "outside.rtf")
        self.adapter._window.assert_not_called()

    def test_export_never_overwrites_existing_file(self):
        self.assertTrue(callable(getattr(self.adapter, "export_reference", None)), "必须支持接续原生导出")
        cache = ROOT / ".cache" / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            target = Path(directory) / "existing.rtf"
            target.write_bytes(b"original")
            self.adapter._window = Mock()
            with self.assertRaisesRegex(AutomationError, "已存在"):
                self.adapter.export_reference(target)
            self.assertEqual(target.read_bytes(), b"original")
            self.adapter._window.assert_not_called()

    def test_existing_save_dialog_uses_filename_id_instead_of_address_edit(self):
        cache = ROOT / ".cache" / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            target = Path(directory) / "new.rtf"
            filename = Control(5, "Edit", control_id=1001)
            address = Control(6, "Edit", "原文件夹", control_id=41477)
            filetype = Control(7, "ComboBox")
            save = Control(8, "Button", "保存", control_id=1)
            dialog = Control(2, "#32770", "另存为", [filename, address, filetype, save])
            self.adapter._save_dialog = Mock(return_value=dialog)
            self.adapter._assert_target = Mock()
            self.adapter._assert_foreground = Mock()
            self.adapter._input_state = Mock(return_value={"foreground": 2})
            self.adapter.app.windows = Mock(return_value=[])
            edit = SimpleNamespace(set_edit_text=lambda text: setattr(filename, "_text", text))
            combo = SimpleNamespace(item_texts=lambda: ["Rich text format (*.rtf)"],
                                    selected_text=lambda: "Rich text format (*.rtf)")
            wrappers = SimpleNamespace(EditWrapper=Mock(return_value=edit), ComboBoxWrapper=Mock(return_value=combo))

            def save_file(*args, **kwargs):
                target.write_bytes(b"{\\rtf1 original card}")
                return 0

            save.send_message_timeout.side_effect = save_file
            with patch.dict("sys.modules", {"pywinauto.controls.win32_controls": wrappers}):
                result = self.adapter.export_reference(target)
            self.assertEqual(result, target.relative_to(ROOT))
            self.assertEqual(filename.window_text(), str(target))
            self.assertEqual(address.window_text(), "原文件夹")
            save.send_message_timeout.assert_called_once_with(0x00F5, timeout=2)

    def test_export_opens_save_as_at_verified_toolbar_center_without_remote_button_rect(self):
        cache = ROOT / ".cache" / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            target = Path(directory) / "new.rtf"
            toolbar = Control(4, "TToolBar", "tlbButins")
            detail = Control(2, "TfrmShowReferencePattern", descendants=[toolbar])
            filename = Control(5, "Edit", control_id=1001)
            filetype = Control(7, "ComboBox")
            save = Control(8, "Button", "保存", control_id=1)
            dialog = Control(9, "#32770", "另存为", [filename, filetype, save])
            self.adapter.detail = detail
            self.adapter.read_detail = Mock()
            self.adapter._save_dialog = Mock(side_effect=[None, dialog])
            self.adapter._assert_target = Mock()
            self.adapter._assert_foreground = Mock()
            state = {"foreground": 2}
            self.adapter._input_state = Mock(side_effect=lambda: state)
            self.adapter._click = Mock(side_effect=lambda *args, **kwargs: state.update(foreground=9))
            self.adapter.app.windows = Mock(return_value=[])
            edit = SimpleNamespace(set_edit_text=lambda text: setattr(filename, "_text", text))
            combo = SimpleNamespace(item_texts=lambda: ["Rich text format (*.rtf)"],
                                    selected_text=lambda: "Rich text format (*.rtf)")
            wrappers = SimpleNamespace(EditWrapper=Mock(return_value=edit), ComboBoxWrapper=Mock(return_value=combo))
            remote = SimpleNamespace(ToolbarWrapper=Mock(side_effect=AssertionError("禁止使用跨位数button矩形")))
            fake_ctypes = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(GetDpiForWindow=lambda handle: 96)))

            def save_file(*args, **kwargs):
                target.write_bytes(b"{\\rtf1 original card}")
                return 0

            save.send_message_timeout.side_effect = save_file
            with patch.dict("sys.modules", {"pywinauto.controls.win32_controls": wrappers,
                                            "pywinauto.controls.common_controls": remote,
                                            "ctypes": fake_ctypes}):
                self.adapter.export_reference(target)
            self.adapter._click.assert_called_once_with(toolbar, (36, 14))


if __name__ == "__main__":
    unittest.main()
