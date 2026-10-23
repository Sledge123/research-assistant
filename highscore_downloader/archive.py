"""完整卡片归档、相对路径回执与逐文件读回校验。"""

import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile

from .numbers import ReferenceCode, parse_code


PEAK_FIELDS = ("no", "h", "k", "l", "d_angstrom", "two_theta_deg", "intensity_percent")
INDEX_PEAK_FIELDS = PEAK_FIELDS + ("intensity_raw", "intensity_scale_raw")
_KINDS = ("rtf", "txt", "json", "csv")
_STRUCTURE_KINDS = _KINDS + ("cif",)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class ArchiveError(Exception):
    """归档不完整、已损坏、来源冲突或文件操作失败。"""


def _canonical_code(value):
    try:
        parsed = parse_code(value)
    except ValueError as error:
        raise ArchiveError(f"归档卡片编号非法：{value!r}") from error
    if parsed.canonical_hint != value:
        raise ArchiveError(f"归档必须使用规范完整编号：{value!r}")
    return value


def _numeric(value, field, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or _NUMBER.fullmatch(value) is None:
        raise ArchiveError(f"峰表字段 {field} 必须为有效数字字符串：{value!r}")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ArchiveError(f"峰表字段 {field} 无效：{value!r}") from error
    if not number.is_finite():
        raise ArchiveError(f"峰表字段 {field} 必须为有限数值。")
    return number


def _report_contract(card, receipt=None):
    """先识别来源身份再分派版本，数据库卡不得退回原生归档分支。"""
    methods = {
        'sqlite_readonly_database_report': ('generated_database_report', {2, 3, 4, 5, 6}),
        'jade_index_readonly_report': ('generated_index_report', {3, 4, 5, 6}),
        'jade_structure_readonly_report': ('generated_structure_report', {3, 4, 5, 6}),
    }
    data_methods = {'jade_index_data': 'jade_index_readonly_report',
                    'jade_structure_data': 'jade_structure_readonly_report'}
    verification = card.get('verification')
    declared = verification.get('method') if isinstance(verification, dict) else None
    recorded = (receipt or {}).get('export_method')
    expected = data_methods.get(card.get('data_type'))
    database_marked = ('database_raw' in card or 'database_sha256' in card
                       or str(card.get('database_id', '')).startswith(('hsrdb-', 'jadeidx-', 'jadestr-')))
    if expected is None and database_marked:
        expected = 'sqlite_readonly_database_report'
    if expected is None:
        expected = declared if declared in methods else recorded if recorded in methods else None
    report_marked = (expected is not None
                     or isinstance(verification, dict) and 'report_version' in verification
                     or receipt is not None and any(key in receipt for key in ('export_method', 'report_version', 'rtf_origin')))
    if not report_marked:
        if verification is not None and (not isinstance(verification, dict)
                                         or declared != 'native_detail_and_rtf'):
            raise ArchiveError('卡片核验方法无法识别。')
        return None, None
    if expected is None or not isinstance(verification, dict) or declared != expected:
        raise ArchiveError('数据库报告缺少可识别且一致的核验方法。')
    origin, versions = methods[expected]
    version = verification.get('report_version')
    if type(version) is not int or version not in versions:
        raise ArchiveError('数据库报告版本缺失或不受支持。')
    if receipt is not None:
        for key, value in (('export_method', expected), ('rtf_origin', origin), ('report_version', version)):
            if key in receipt and (type(receipt[key]) is not type(value) or receipt[key] != value):
                raise ArchiveError('数据库报告与归档回执的核验方法或版本不一致。')
    return expected, version


def _check_card(card):
    if not isinstance(card, dict) or card.get("complete") is not True:
        raise ArchiveError("仅允许归档 complete=True 的完整卡片。")
    index_report = card.get("data_type") == "jade_index_data"
    structure_report = card.get("data_type") == "jade_structure_data"
    _report_contract(card)
    verification = card.get("verification")
    if (isinstance(verification, dict) and verification.get('report_version') == 6
            and not structure_report and not isinstance(card.get('angular_conversion'), dict)):
        raise ArchiveError('新版衍射报告必须提供明确波长与可复验的2θ换算。')
    if (isinstance(verification, dict) and verification.get('method') == 'sqlite_readonly_database_report'
            and verification.get('report_version') >= 4):
        from highscore_database.validation import validate_database_card
        try:
            validate_database_card(card)
        except Exception as error:
            raise ArchiveError('数据库报告科学字段、原始记录或完整峰表复验失败。') from error
    if (isinstance(verification, dict) and verification.get("method") == "jade_index_readonly_report"
            and not index_report):
        raise ArchiveError("索引报告的类型标识不一致。")
    if (isinstance(verification, dict) and verification.get("method") == "jade_structure_readonly_report"
            and not structure_report):
        raise ArchiveError("晶体结构报告的类型标识不一致。")
    if index_report:
        from highscore_database.jade_index import validate_index_card
        try:
            validate_index_card(card)
        except Exception as error:
            raise ArchiveError("索引原始记录、峰表或完整性标记复验失败。") from error
        if (verification.get('report_version') != 6 and (card.get("wavelength_angstrom") is not None or card.get("angular_conversion") is not None
                or any(peak.get("two_theta_deg") is not None for peak in card["peaks"]))):
            raise ArchiveError("索引报告必须保留缺失角度，不接受未经本类型核验的角度换算。")
    if structure_report:
        from highscore_database.jade_structure import validate_structure_card
        try:
            validate_structure_card(card)
        except Exception as error:
            raise ArchiveError("晶体结构原始记录、原子坐标或对称操作复验失败。") from error
        if (card.get("wavelength_angstrom") is not None or card.get("angular_conversion") is not None
                or card.get("peaks") != [] or type(card.get("peak_count")) is not int or card["peak_count"] != 0):
            raise ArchiveError("晶体结构报告不包含衍射峰表或角度换算。")
    code = _canonical_code(card.get("code"))
    if not isinstance(card.get("phase_name"), str) or not card["phase_name"].strip():
        raise ArchiveError("卡片物相名称不能为空。")
    if not isinstance(card.get("raw_text"), str) or not card["raw_text"].strip():
        raise ArchiveError("卡片完整原文不能为空。")
    for field in ("source", "chemical_formula"):
        if card.get(field) is not None and not isinstance(card[field], str):
            raise ArchiveError(f"卡片字段 {field} 必须为文本或空值。")
    if structure_report:
        return code
    peaks = card.get("peaks")
    if not isinstance(peaks, list) or not peaks:
        raise ArchiveError("卡片峰表不能为空。")
    if type(card.get("peak_count")) is not int or card["peak_count"] != len(peaks):
        raise ArchiveError("卡片峰数与实际峰表不一致。")
    for index, peak in enumerate(peaks, 1):
        if not isinstance(peak, dict):
            raise ArchiveError(f"第 {index} 个峰必须为结构化记录。")
        for field in PEAK_FIELDS:
            number = _numeric(peak.get(field), f"{index}.{field}",
                              required=field == "d_angstrom" or field == "intensity_percent" and (
                                  not index_report or verification.get('report_version') == 6))
            if field == "d_angstrom" and number <= 0:
                raise ArchiveError(f"第 {index} 个峰的 d 间距必须大于零。")
            if field == "intensity_percent" and number is not None and not 0 <= number <= 100:
                raise ArchiveError(f"第 {index} 个峰的强度百分数必须在 0 到 100 之间。")
    wavelength = _numeric(card.get("wavelength_angstrom"), "wavelength_angstrom")
    if wavelength is not None and wavelength <= 0:
        raise ArchiveError("波长必须大于零。")
    return code


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _csv_rows(card):
    if card.get("data_type") == "jade_structure_data":
        from highscore_database.jade_structure import structure_csv
        return list(csv.reader(io.StringIO(structure_csv(card), newline="")))
    fields = INDEX_PEAK_FIELDS if card.get("data_type") == "jade_index_data" else PEAK_FIELDS
    return [list(fields)] + [
        ["" if peak.get(field) is None else peak[field] for field in fields]
        for peak in card["peaks"]
    ]


def _csv_bytes(card):
    if card.get("data_type") == "jade_structure_data":
        from highscore_database.jade_structure import structure_csv
        return structure_csv(card).encode("utf-8-sig")
    output = io.StringIO(newline="")
    csv.writer(output).writerows(_csv_rows(card))
    return output.getvalue().encode("utf-8-sig")


def _atomic_write(path, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ArchiveStore:
    """root 内的归档存储；损坏归档会抛错，不会被视为可跳过的成功。"""

    def __init__(self, root):
        try:
            self.root = Path(root).resolve()
            if not self.root.is_dir():
                raise ArchiveError("归档项目根目录不存在或不是目录。")
        except (OSError, TypeError, ValueError) as error:
            raise ArchiveError(f"归档项目根目录无效：{error}") from error

    def _safe_path(self, path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ArchiveError(f"归档路径超出项目根目录：{path}")
        return resolved

    @staticmethod
    def _directory(code):
        return f"output/cards/{code}"

    def _validate_files(self, receipt):
        code = _canonical_code(receipt.get("code"))
        if receipt.get("status") != "success" or receipt.get("schema_version") != 1:
            raise ArchiveError("归档回执不是受支持的成功回执。")
        if receipt.get("receipt_path") != f"{self._directory(code)}/receipt.json":
            raise ArchiveError("回执路径与卡片编号不一致。")
        files = receipt.get("files")
        if not isinstance(files, dict) or set(files) not in (set(_KINDS), set(_STRUCTURE_KINDS)):
            raise ArchiveError("归档须包含 RTF、TXT、JSON 和 CSV，晶体结构报告还须包含 CIF。")
        payloads = {}
        for kind in (_STRUCTURE_KINDS if "cif" in files else _KINDS):
            item = files[kind]
            expected_path = f"{self._directory(code)}/{code}.{kind}"
            if not isinstance(item, dict) or item.get("path") != expected_path:
                raise ArchiveError(f"{kind} 文件路径不符合卡片目录约定。")
            payload = self._safe_path(item["path"]).read_bytes()
            if not payload or type(item.get("size")) is not int or len(payload) != item["size"]:
                raise ArchiveError(f"{kind} 文件为空或大小与回执不符。")
            if hashlib.sha256(payload).hexdigest() != item.get("sha256"):
                raise ArchiveError(f"{kind} 文件 SHA256 摘要不匹配。")
            payloads[kind] = payload
        if not payloads["rtf"].startswith(b"{\\rtf"):
            raise ArchiveError("原生卡片必须为以 {\\rtf 开头的非空 RTF 文件。")
        card = json.loads(payloads["json"].decode("utf-8"))
        _report_contract(card, receipt)
        _check_card(card)
        structure_report = card.get("data_type") == "jade_structure_data"
        if set(files) != set(_STRUCTURE_KINDS if structure_report else _KINDS):
            raise ArchiveError("归档文件类型与晶体结构或衍射卡片类型不一致。")
        if card.get("data_type") == "jade_index_data":
            from highscore_database.reporting import (
                INDEX_REPORT_METHOD, INDEX_REPORT_ORIGIN, report_fields, report_text, report_rtf,
            )
            verification = card.get("verification")
            if (not isinstance(verification, dict)
                    or verification.get("method") != INDEX_REPORT_METHOD
                    or verification.get("rtf_origin") != INDEX_REPORT_ORIGIN
                    or verification.get("native_highscore_export") is not False
                    or verification.get("completeness_scope") != "verified_index_records"
                    or receipt.get("export_method", INDEX_REPORT_METHOD) != INDEX_REPORT_METHOD
                    or receipt.get("rtf_origin", INDEX_REPORT_ORIGIN) != INDEX_REPORT_ORIGIN):
                raise ArchiveError("索引报告的来源或核验范围标记不一致。")
            if (card.get("report_fields") != report_fields(card)
                    or card["raw_text"] != report_text(card)
                    or payloads["rtf"] != report_rtf(card)):
                raise ArchiveError("索引报告的字段、TXT 或 RTF 与原始索引记录不一致。")
        if structure_report:
            from highscore_database.reporting import (
                STRUCTURE_REPORT_METHOD, STRUCTURE_REPORT_ORIGIN, report_fields, report_text, report_rtf,
            )
            from highscore_database.jade_structure import structure_cif
            verification = card.get("verification")
            if (not isinstance(verification, dict)
                    or verification.get("method") != STRUCTURE_REPORT_METHOD
                    or verification.get("rtf_origin") != STRUCTURE_REPORT_ORIGIN
                    or verification.get("native_highscore_export") is not False
                    or verification.get("completeness_scope") != "verified_structure_records"
                    or receipt.get("export_method", STRUCTURE_REPORT_METHOD) != STRUCTURE_REPORT_METHOD
                    or receipt.get("rtf_origin", STRUCTURE_REPORT_ORIGIN) != STRUCTURE_REPORT_ORIGIN):
                raise ArchiveError("晶体结构报告的来源或核验范围标记不一致。")
            if (card.get("report_fields") != report_fields(card)
                    or card["raw_text"] != report_text(card)
                    or payloads["rtf"] != report_rtf(card)):
                raise ArchiveError("晶体结构报告的字段、TXT 或 RTF 与原始记录不一致。")
            if payloads["cif"] != structure_cif(card).encode("utf-8"):
                raise ArchiveError("CIF 的晶胞、原子坐标或对称操作与原始结构记录不一致。")
        verification = card.get('verification')
        if not isinstance(verification, dict): verification = {}
        if (verification.get('method') == 'sqlite_readonly_database_report'
                and verification.get('report_version') >= 4):
            from highscore_database.reporting import report_fields, report_text, report_rtf
            if (verification.get('rtf_origin') != 'generated_database_report'
                    or verification.get('native_highscore_export') is not False
                    or verification.get('database_sha256') != card.get('database_sha256')
                    or verification.get('completeness_scope') != 'available_database_fields'
                    or receipt.get('export_method', 'sqlite_readonly_database_report') != 'sqlite_readonly_database_report'
                    or receipt.get('rtf_origin', 'generated_database_report') != 'generated_database_report'
                    or receipt.get('report_version', verification['report_version']) != verification['report_version']):
                raise ArchiveError('数据库报告来源、版本或核验范围标记不一致。')
            if (card.get('report_fields') != report_fields(card)
                    or card['raw_text'] != report_text(card)
                    or payloads['rtf'] != report_rtf(card)):
                raise ArchiveError('数据库报告的字段、TXT 或 RTF 与原始记录不一致。')
        for field in ("code", "source", "phase_name", "peak_count"):
            if receipt.get(field) != card.get(field):
                raise ArchiveError(f"JSON 字段 {field} 与回执不一致。")
        if payloads["txt"].decode("utf-8") != card["raw_text"]:
            raise ArchiveError("TXT 完整原文与 JSON 不一致。")
        if not payloads["csv"].startswith(b"\xef\xbb\xbf"):
            raise ArchiveError("CSV 必须使用 utf-8-sig 编码。")
        rows = list(csv.reader(io.StringIO(payloads["csv"].decode("utf-8-sig"), newline="")))
        if rows != _csv_rows(card):
            raise ArchiveError("CSV 峰数、字段或精确数值与 JSON 不一致。")
        if structure_report and payloads["csv"] != _csv_bytes(card):
            raise ArchiveError("原子坐标 CSV 与原始结构记录不一致。")
        try:
            matches_input = parse_code(receipt.get("input_code")).matches(code)
        except ValueError as error:
            raise ArchiveError("回执中的输入编号非法。") from error
        if not matches_input:
            raise ArchiveError("回执中的输入编号与归档卡片不匹配。")
        return receipt

    def validate(self, receipt_path: Path) -> dict:
        """校验路径、大小、摘要、卡片身份及文本和峰表内容。"""
        try:
            path = self._safe_path(receipt_path)
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ArchiveError("归档回执必须为 JSON 对象。")
            if path.relative_to(self.root).as_posix() != receipt.get("receipt_path"):
                raise ArchiveError("回执实际路径与记录的相对路径不一致。")
            return self._validate_files(receipt)
        except (OSError, TypeError, ValueError, csv.Error) as error:
            raise ArchiveError(f"归档读回校验失败：{error}") from error

    def find_success(self, request: ReferenceCode) -> dict | None:
        """按请求编号匹配；匹配目录存在损坏时明确报错。"""
        if not isinstance(request, ReferenceCode):
            raise ArchiveError("查找归档需要 ReferenceCode 编号请求。")
        try:
            cards = self._safe_path("output/cards")
            if not cards.exists():
                return None
            if not cards.is_dir():
                raise ArchiveError("卡片归档根路径不是目录。")
            matches = []
            for directory in sorted(cards.iterdir()):
                if request.matches(directory.name):
                    matches.append(self.validate(directory / "receipt.json"))
            if len({receipt["code"] for receipt in matches}) > 1:
                codes = "、".join(receipt["code"] for receipt in matches)
                raise ArchiveError(f"编号 {request.original} 匹配多个完整编号，存在来源歧义：{codes}")
            return matches[0] if matches else None
        except (OSError, TypeError, ValueError) as error:
            raise ArchiveError(f"查找归档失败：{error}") from error

    def save(self, card: dict, rtf_path: Path, input_code: str) -> dict:
        """保存对应类型的文件并完整读回后，最后原子写入成功回执。"""
        try:
            code = _check_card(card)
            if not parse_code(input_code).matches(code):
                raise ArchiveError("输入编号与待归档卡片不匹配。")
            source_path = self._safe_path(rtf_path)
            directory = self._safe_path(self._directory(code))
            receipt_path = directory / "receipt.json"
            native_continuation = False
            if directory.exists():
                if receipt_path.exists():
                    existing = self.validate(receipt_path)
                    if existing.get("source") != card.get("source"):
                        raise ArchiveError(f"卡片 {code} 已存在不同来源的成功归档，禁止覆盖。")
                    return existing
                native = directory / f"{code}.rtf"
                if (directory.is_dir() and {entry.name for entry in directory.iterdir()} == {native.name}
                        and native.is_file() and source_path == self._safe_path(native)):
                    native_continuation = True
                else:
                    raise ArchiveError(f"卡片 {code} 目录缺少成功回执且不符合原生 RTF 安全接续条件。")
            rtf = source_path.read_bytes()
            if not rtf.startswith(b"{\\rtf"):
                raise ArchiveError("原生卡片必须为以 {\\rtf 开头的非空 RTF 文件。")
            payloads = {"rtf": rtf, "txt": card["raw_text"].encode("utf-8"),
                        "json": _json_bytes(card), "csv": _csv_bytes(card)}
            if card.get("data_type") == "jade_structure_data":
                from highscore_database.jade_structure import structure_cif
                payloads["cif"] = structure_cif(card).encode("utf-8")
            directory.parent.mkdir(parents=True, exist_ok=True)
            if not native_continuation:
                directory.mkdir()
            receipt = {"schema_version": 1, "status": "success", "code": code,
                       "source": card.get("source"), "phase_name": card["phase_name"],
                       "peak_count": card["peak_count"], "input_code": input_code,
                       "receipt_path": f"{self._directory(code)}/receipt.json", "files": {}}
            for kind, payload in payloads.items():
                relative = f"{self._directory(code)}/{code}.{kind}"
                if kind != "rtf" or not native_continuation:
                    _atomic_write(self._safe_path(relative), payload)
                receipt["files"][kind] = {"path": relative, "size": len(payload),
                                          "sha256": hashlib.sha256(payload).hexdigest()}
            self._validate_files(receipt)
            _atomic_write(receipt_path, _json_bytes(receipt))
            return self.validate(receipt_path)
        except (OSError, TypeError, ValueError, csv.Error) as error:
            raise ArchiveError(f"保存归档失败：{error}") from error
