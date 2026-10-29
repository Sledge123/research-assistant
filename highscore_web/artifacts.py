"""上传包的路径隔离、限量解压和现有 ArchiveStore 全量复验。"""
import base64
import binascii
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

from highscore_downloader.archive import ArchiveError, ArchiveStore
from highscore_downloader.numbers import parse_code

MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
ENTRY = re.compile(r"output/cards/([0-9]{2}-[0-9]{3}-[0-9]{4}|ICSD-[0-9]{6})/(?:receipt\.json|\1\.(?:rtf|txt|json|csv|cif))\Z")


def validate_directory(directory, expected, *, allow_subset=False):
    """expected 为实际请求的完整编号及可选来源，不能接纳包中多余卡片。"""
    store = ArchiveStore(directory)
    receipts = []
    actual = sorted(path.parent.name for path in (directory / "output/cards").glob("*/receipt.json"))
    if not actual or len(actual) > len(expected) or not allow_subset and len(actual) != len(expected):
        raise ArchiveError("归档数量与任务请求不一致。")
    matched = set()
    for code in actual:
        receipt = store.validate(Path(f"output/cards/{code}/receipt.json"))
        matches = [index for index, item in enumerate(expected) if parse_code(item["code"]).matches(code)]
        if len(matches) != 1 or matches[0] in matched:
            raise ArchiveError("归档编号与请求不一致或来源不唯一。")
        item = expected[matches[0]]
        if item.get("source") and item["source"] != receipt.get("source"):
            raise ArchiveError("候选来源与归档来源不一致。")
        matched.add(matches[0])
        receipts.append(receipt)
    return receipts


def accept_zip(root, job_id, encoded, expected, *, allow_subset=False):
    if not isinstance(encoded, str) or len(encoded) > MAX_ZIP_BYTES * 4 // 3 + 8:
        raise ArchiveError("归档上传包超过允许大小。")
    destination = root / f"output/web/artifacts/{job_id}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 缓存和归档可为两个独立容器挂载；在目标目录暂存才能原子发布。
    staging = Path(tempfile.mkdtemp(prefix="upload-", dir=destination.parent))
    try:
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ArchiveError("归档上传编码无效。") from error
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 120 or sum(item.file_size for item in entries) > MAX_UNPACKED:
                raise ArchiveError("归档上传内容超过数量或解压大小限制。")
            names = set()
            for item in entries:
                name = item.filename
                match = ENTRY.fullmatch(name)
                mode = item.external_attr >> 16
                if (match is None or name in names or stat.S_ISLNK(mode) or item.is_dir()
                        or PurePosixPath(name).is_absolute() or "\\" in name
                        or item.flag_bits & 1 or item.file_size > MAX_UNPACKED):
                    raise ArchiveError("归档包含不允许的路径、重复文件或链接。")
                names.add(name)
                path = staging.joinpath(*PurePosixPath(name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                # 流式限量读取，避免伪造 central directory 导致内存耗尽。
                with archive.open(item) as source, path.open("xb") as target:
                    copied = 0
                    while chunk := source.read(64 * 1024):
                        copied += len(chunk)
                        if copied > item.file_size or copied > MAX_UNPACKED:
                            raise ArchiveError("归档文件实际大小超出声明。")
                        target.write(chunk)
            receipts = validate_directory(staging, expected, allow_subset=allow_subset)
            required = {r["receipt_path"] for r in receipts} | {f["path"] for r in receipts for f in r["files"].values()}
            if names != required:
                raise ArchiveError("归档文件集合与已验证回执不一致。")
        if destination.exists():
            # 允许服务在文件落盘后、SQLite 提交前异常退出的同内容安全接续。
            existing = validate_directory(destination, expected, allow_subset=allow_subset)
            if existing != receipts:
                raise ArchiveError("任务已存在不同归档，禁止覆盖。")
            return destination.relative_to(root).as_posix(), existing
        (staging / "artifact.zip").write_bytes(payload)
        staging.rename(destination)
        return destination.relative_to(root).as_posix(), receipts
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ArchiveError("归档上传无效或无法保存。") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def checked_zip(root, relative, expected):
    directory = (root / relative).resolve()
    if not directory.is_relative_to((root / "output/web/artifacts").resolve()):
        raise ArchiveError("下载路径无效。")
    receipts = validate_directory(directory, expected)
    # 每次下载重新打包已复验文件，磁盘上旧 ZIP 不是可信依据。
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for receipt in receipts:
            for name in [receipt["receipt_path"], *(item["path"] for item in receipt["files"].values())]:
                archive.write(directory / name, name)
    return stream.getvalue()


def public_results(root, relative, receipts):
    """输出类型来自已复验 JSON 的来源声明，绝不把数据库报告称作原生导出。"""
    results = []
    directory = root / relative
    for receipt in receipts:
        card = json.loads((directory / receipt["files"]["json"]["path"]).read_text(encoding="utf-8"))
        method = card.get("verification", {}).get("method") if isinstance(card.get("verification"), dict) else None
        database_report = method == "sqlite_readonly_database_report"
        index_report = method == "jade_index_readonly_report"
        structure_report = method == "jade_structure_readonly_report"
        results.append({"code": receipt["code"], "phase_name": receipt["phase_name"],
                        "source": receipt["source"], "peak_count": receipt["peak_count"],
                        "status": "success",
                        "archive_type": "structure_report" if structure_report else "index_report" if index_report else "database_report" if database_report else "verified_archive",
                        "archive_label": "晶体结构报告" if structure_report else "索引数据报告" if index_report else "数据库报告（非 HighScore 原生导出）" if database_report else "完整文件归档（已校验）"})
    return results
