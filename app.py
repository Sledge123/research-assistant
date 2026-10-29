"""源码入口：中文界面、命令行归档和控件诊断。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="HighScore 离线卡片归档器")
    parser.add_argument("--inspect", action="store_true", help="只读打印 HighScore 控件诊断")
    parser.add_argument("--executable", help="HighScorePlus.exe 的位置，可使用项目相对路径")
    parser.add_argument("--probe-code", help="检索并打开单张卡片，输出诊断（开发核验用）")
    parser.add_argument("--resume-search", action="store_true", help="接续当前已经打开的编号检索框")
    parser.add_argument("--export-current", help="将当前卡片详情保存为项目内 RTF（相对项目根目录）")
    parser.add_argument("--codes", help="命令行归档编号，多个编号用逗号、空白或换行分隔")
    parser.add_argument("--input-file", help="读取项目内 UTF-8 编号文件")
    parser.add_argument("--validate", action="store_true", help="重新校验所有已有归档，不操作软件")
    parser.add_argument("--email-to", help="本批归档后发送至一个 ASCII 邮箱，须已保存邮件设置")
    parser.add_argument("--mail-dry-run", action="store_true", help="仅生成并校验邮件 EML 和 ZIP，不连接 SMTP")
    parser.add_argument("--menu-stage", choices=("open", "retrieve", "submit"), help="分步核验原生菜单（开发诊断）")
    options = parser.parse_args(argv)
    if options.mail_dry_run and not options.email_to:
        parser.error("--mail-dry-run 必须同时指定 --email-to")
    mail_settings = None
    if options.email_to is not None:
        if (not (options.codes or options.input_file) or options.inspect or options.validate
                or options.probe_code or options.export_current or options.menu_stage):
            parser.error("--email-to 仅用于 --codes 或 --input-file 归档批次")
        from highscore_downloader.mail_settings import load_mail_settings, validate_email, validate_mail_settings
        try:
            options.email_to = validate_email(options.email_to)
            loaded_mail_settings = load_mail_settings(ROOT)
            if loaded_mail_settings.message:
                raise ValueError(loaded_mail_settings.message)
            mail_settings = validate_mail_settings(loaded_mail_settings, options.email_to,
                                                   require_credentials=not options.mail_dry_run)
        except ValueError as error:
            parser.error(str(error))
    cache = ROOT / ".cache" / "tmp"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(cache)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    if options.inspect:
        from highscore_downloader.automation import HighScoreAutomation
        adapter = HighScoreAutomation(options.executable)
        print(json.dumps(adapter.inspect(), ensure_ascii=False, indent=2))
        return 0
    if options.menu_stage:
        from highscore_downloader.automation import HighScoreAutomation
        adapter = HighScoreAutomation(options.executable, progress=lambda message: print(message, file=sys.stderr, flush=True))
        print(json.dumps(adapter.reference_menu_step(options.menu_stage), ensure_ascii=False, indent=2))
        return 0
    if options.export_current:
        from highscore_downloader.automation import HighScoreAutomation
        target = (ROOT / options.export_current).resolve()
        if not target.is_relative_to(ROOT):
            raise ValueError("导出位置必须在项目目录内")
        adapter = HighScoreAutomation(options.executable, progress=lambda message: print(message, file=sys.stderr, flush=True))
        adapter.connect(launch_if_missing=False)
        details = adapter.read_detail()
        result = adapter.export_reference(target)
        (ROOT / ".cache" / "last_detail.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"file": str(result), "wavelength": details.get("wavelength")}, ensure_ascii=False, indent=2))
        return 0
    if options.probe_code:
        from highscore_downloader.automation import HighScoreAutomation
        adapter = HighScoreAutomation(options.executable, progress=lambda message: print(message, file=sys.stderr, flush=True))
        try:
            rows = adapter.load_reference(options.probe_code, resume_open_search=options.resume_search)
            adapter.open_reference()
        except Exception:
            import traceback
            log_path = ROOT / ".cache" / "last_probe.json"
            try:
                log_path.write_text(json.dumps(adapter.inspect(), ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            traceback.print_exc()
            raise
        print(json.dumps({"matches": rows, "state": adapter.inspect()}, ensure_ascii=False, indent=2))
        return 0
    if options.validate:
        from highscore_downloader.archive import ArchiveStore, ArchiveError
        store = ArchiveStore(ROOT)
        cards = ROOT / "output" / "cards"
        directories = list(cards.iterdir()) if cards.exists() else []
        failures = 0
        for directory in directories:
            try:
                verified = store.validate(directory / "receipt.json")
                print(f"PASS {verified['code']}，峰数 {verified['peak_count']}")
            except ArchiveError as error:
                failures += 1
                print(f"FAIL {directory.name}：{error}")
        print(f"已检查 {len(directories)} 个卡片目录，失败 {failures}")
        return 1 if failures else 0
    if options.codes or options.input_file:
        from highscore_downloader.service import BatchRunner
        text = options.codes or ""
        if options.input_file:
            inputs = (ROOT / options.input_file).resolve()
            if not inputs.is_relative_to(ROOT):
                raise ValueError("编号文件必须在项目目录内")
            text += "\n" + inputs.read_text(encoding="utf-8-sig")
        runner = BatchRunner(ROOT, on_event=lambda event: print(
            f"{event.get('code', '')}：{event.get('message', '')}", file=sys.stderr, flush=True))
        result = runner.run(text, executable=options.executable)
        archive_ok = all(row["status"] in ("success", "skipped") for row in result)
        if options.email_to:
            from highscore_downloader.mailer import MailService
            try:
                mail = MailService(ROOT, mail_settings, stop_event=runner.stop_event).deliver(
                    result, options.email_to, dry_run=options.mail_dry_run)
            except Exception:
                mail = {"status": "failed", "message": "邮件任务未能完成，请检查项目内邮件设置和任务产物。"}
            print(json.dumps({"results": result, "mail": mail}, ensure_ascii=False, indent=2))
            mail_ok = mail.get("status") in {"sent", "skipped"} or (
                options.mail_dry_run and mail.get("status") == "prepared")
            return 0 if archive_ok and mail_ok else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if archive_ok else 1
    from highscore_downloader.gui import launch_gui
    launch_gui(ROOT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
