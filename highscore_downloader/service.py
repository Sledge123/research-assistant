"""串行归档队列：真实软件状态、完整解析和文件校验共同决定成功。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import traceback
import uuid

from .archive import ArchiveStore
from .automation import HighScoreAutomation, OperationStopped, CardNotFound, AutomationError
from .numbers import parse_codes, parse_code
from .parser import parse_rtf, parse_card_text, CardParseError


class BatchRunner:
    def __init__(self, root, *, on_event=None, stop_event=None, choose_source=None,
                 adapter_factory=None):
        self.root = Path(root).resolve()
        self.on_event = on_event or (lambda event: None)
        self.stop_event = stop_event or threading.Event()
        self.choose_source = choose_source
        self.adapter_factory = adapter_factory or HighScoreAutomation
        self.store = ArchiveStore(self.root)
        self.current = None
        self.log = None

    def _check_stop(self):
        if self.stop_event.is_set():
            raise OperationStopped("已停止，保留已经通过校验的归档")

    def _emit(self, **values):
        if self.current is not None:
            self.current.update(values)
            event = dict(self.current)
        else:
            event = values
        if self.log is not None:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event},
                                        ensure_ascii=False) + "\n")
        self.on_event(event)

    @staticmethod
    def _receipt_values(receipt):
        return {"actual_code": receipt["code"], "source": receipt.get("source"),
                "phase_name": receipt["phase_name"], "peak_count": receipt["peak_count"],
                "files": {kind: item["path"] for kind, item in receipt["files"].items()},
                "receipt_path": receipt["receipt_path"]}

    def run(self, text: str, executable: Path | None = None) -> list[dict]:
        requests = parse_codes(text)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        log_dir = self.root / "logs" / "batches"
        cache = self.root / ".cache" / "native" / run_id
        for path in (log_dir, cache):
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("队列输出目录必须位于项目内")
            path.mkdir(parents=True, exist_ok=True)
        self.log = log_dir / f"{run_id}.jsonl"
        records = [{"code": request.query, "input_code": request.original,
                    "status": "pending", "message": "等待处理", "files": {}}
                   for request in requests]
        adapter = None
        for record in records:
            self.current = record
            self._emit()
        for index, (request, record) in enumerate(zip(requests, records)):
            self.current = record
            try:
                self._check_stop()
                existing = self.store.find_success(request)
                if existing:
                    self._emit(status="skipped", message="已有归档重新校验通过，已跳过",
                               **self._receipt_values(existing))
                    continue
                self._emit(status="running", message=f"正在检索（{index + 1}/{len(requests)}）")
                if adapter is None:
                    adapter = self.adapter_factory(
                        executable, stop_event=self.stop_event,
                        progress=lambda message: self._emit(message=message))
                choices = adapter.load_reference(request)
                self._check_stop()
                if not choices:
                    raise AutomationError("检索结果未给出可核对的卡片，已暂停")
                selected = 0
                if len(choices) > 1:
                    if self.choose_source is None:
                        raise AutomationError("同号有多个来源，需要在界面中选择")
                    selected = self.choose_source(choices)
                    self._check_stop()
                    if selected is None:
                        raise OperationStopped("已取消来源选择")
                    if type(selected) is not int or not 0 <= selected < len(choices):
                        raise AutomationError("来源选择无效")
                choice = choices[selected]
                if not request.matches(choice["code"]):
                    raise AutomationError("候选卡片编号与请求不一致")
                selected_code = parse_code(choice["code"])
                if selected_code.canonical_hint != choice["code"]:
                    raise AutomationError("候选卡片未提供完整规范编号")
                adapter.open_reference(row_index=choice.get("index", selected))
                self._check_stop()
                details = adapter.read_detail()
                ui_card = parse_card_text(details["text"], wavelength=details.get("wavelength"),
                                          source=choice.get("source"), expected_code=selected_code)
                native = cache / f"{ui_card['code']}.rtf"
                adapter.export_reference(native)
                self._check_stop()
                self._emit(message="原生导出完成，校验完整峰表和卡片信息")
                card = parse_rtf(native.read_bytes(), wavelength=details.get("wavelength"),
                                 source=choice.get("source"), expected_code=selected_code)
                for field in ("code", "phase_name", "chemical_formula", "cell_parameters", "peaks"):
                    if card[field] != ui_card[field]:
                        raise CardParseError(f"原生 RTF 的 {field} 与软件详情不一致")
                card["verification"] = {
                    "method": "native_detail_and_rtf",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "peak_count": ui_card["peak_count"],
                    "source_count_unverified": bool(choice.get("source_count_unverified")),
                }
                receipt = self.store.save(card, native, request.original)
                self._emit(status="success", message="RTF、TXT、JSON、CSV 全部通过校验",
                           **self._receipt_values(receipt))
                adapter.close_detail()
            except CardNotFound as error:
                self._emit(status="not_found", message=str(error))
                continue
            except OperationStopped as error:
                if record.get("status") == "success":
                    self._emit(message="本项已完成校验归档，已停止后续任务", needs_attention=True)
                else:
                    self._emit(status="stopped", message=str(error))
                for pending in records[index + 1:]:
                    self.current = pending
                    self._emit(message="尚未处理；可重试继续")
                break
            except Exception as error:
                # 未知状态不继续点击下一张；在日志中留下可诊断原因。
                message = str(error).replace(str(self.root), ".")
                if record.get("status") == "success":
                    self._emit(message="归档已成功，但关闭详情时出现异常；后续任务已暂停",
                               needs_attention=True)
                else:
                    self._emit(status="failed", message=message)
                with self.log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"error": message,
                                             "traceback": traceback.format_exc().replace(str(self.root), ".")},
                                            ensure_ascii=False) + "\n")
                for pending in records[index + 1:]:
                    self.current = pending
                    self._emit(message="因前一项异常暂停，尚未处理；修复窗口状态后可重试")
                break
        self.current = None
        return records
