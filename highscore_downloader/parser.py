"""解析 HighScore 原生卡片；数值按软件文字保存，未知内容保留原文。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .numbers import parse_code


class CardParseError(ValueError):
    """原生卡片不能确认完整；保留原件但不得记成功。"""


SECTIONS = ("Name and formula", "Crystallographic parameters", "Subfiles and quality",
            "Comments", "References", "Peak list", "Structure data", "Stick Pattern", "Scan")
PEAK_KEYS = ("no", "h", "k", "l", "d_angstrom", "two_theta_deg", "intensity_percent")


def _columns(line: str) -> list[str]:
    if "|" in line:
        parts = line.strip().split("|")
        if parts[-1] == "":
            parts.pop()
        return [part.strip() for part in parts]
    return [part.strip() for part in line.split("\t")]


def _field(section: str, label: str) -> str | None:
    match = re.search(r"^[ \t]*" + re.escape(label) + r"[ \t]*:[ \t|]*(.*?)[ \t|]*$",
                      section, re.MULTILINE)
    return match.group(1).strip().strip("|").strip() or None if match else None


def _decimal(value: str | None, label: str, *, positive=False):
    try:
        number = Decimal(value) if value else None
        if number is None or not number.is_finite() or (positive and number <= 0):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise CardParseError(f"{label} 不是有效数值：{value!r}") from None


def parse_card_text(text: str, *, wavelength: str | None = None,
                    source: str | None = None, expected_code=None) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise CardParseError("卡片原文为空")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sections = {name: "" for name in SECTIONS}
    marks = []
    for name in SECTIONS:
        match = re.search(r"^\s*" + re.escape(name) + r"\s*$", text, re.MULTILINE | re.IGNORECASE)
        if not match:
            # 本机帮助第452页明确：只显示该卡片已有的章节；唯独峰表可隐藏。
            if name in ("Name and formula", "Peak list"):
                raise CardParseError(f"缺少完整卡片章节：{name}；请在详情中显示 Peak List")
            continue
        marks.append((name, match.start(), match.end()))
    if [item[1] for item in marks] != sorted(item[1] for item in marks):
        raise CardParseError("卡片章节顺序异常")
    for index, (name, _, end) in enumerate(marks):
        next_start = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        sections[name] = text[end:next_start].strip()
    identity = sections["Name and formula"]
    actual = _field(identity, "Reference code")
    try:
        code = parse_code(actual).canonical_hint
    except ValueError:
        raise CardParseError("原生卡片缺少有效完整编号") from None
    if not code or actual != code:
        raise CardParseError("原生卡片未提供完整规范编号")
    if expected_code:
        request = parse_code(expected_code) if isinstance(expected_code, str) else expected_code
        if not request.matches(code):
            raise CardParseError(f"原生卡片 {code} 与请求 {request.original} 不一致")
    phase_name = _field(identity, "Compound name") or _field(identity, "Mineral name")
    if not phase_name:
        raise CardParseError("原生卡片缺少物相名称")
    peak_text = re.split(r"^\s*(?:Stick Pattern|Structure|Scan)\s*$", sections["Peak list"],
                         maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)[0]
    lines = [line for line in peak_text.splitlines() if line.strip()]
    if not lines:
        raise CardParseError("完整峰表为空")
    fixed_bounds = None
    if "\t" not in lines[0] and "|" not in lines[0]:
        # HighScore 3.0.5 原生 RTF 使用等宽字符串，不是 RTF 表格。
        fixed_header = re.fullmatch(r"No\.\s+h\s+k\s+l\s+d\s*\[[^]]+\]\s+2Theta\[deg\]\s+I\s*\[%\]\s*", lines[0])
        if not fixed_header:
            raise CardParseError("不能识别原生等宽峰表表头")
        positions = [re.search(r"\b" + key + r"\b", lines[0]).start() for key in ("h", "k", "l")]
        width = positions[1] - positions[0]
        if width < 3 or positions[2] - positions[1] != width:
            raise CardParseError("等宽峰表晶面指数列无法确定")
        first = positions[0] - width // 2
        theta = lines[0].index("2Theta") - 1
        intensity = re.search(r"\bI\s*\[%\]", lines[0]).start()
        fixed_bounds = (0, first, first + width, first + 2 * width, first + 3 * width,
                        theta, intensity, None)
        if list(fixed_bounds[:-1]) != sorted(fixed_bounds[:-1]):
            raise CardParseError("等宽峰表列宽异常")
        header = ["No.", "h", "k", "l", "d [A]", "2Theta[deg]", "I [%]"]
    else:
        header = _columns(lines[0])
    normalized_header = [re.sub(r"\s", "", part).casefold() for part in header]
    aliases = {
        "no.": "no", "no": "no", "h": "h", "k": "k", "l": "l",
        "d[a]": "d_angstrom", "d[å]": "d_angstrom", "d[angstrom]": "d_angstrom",
        "2theta[deg]": "two_theta_deg", "2theta[°]": "two_theta_deg",
        "i[%]": "intensity_percent", "i": "intensity_percent",
    }
    try:
        mapping = [aliases[value] for value in normalized_header]
    except KeyError:
        raise CardParseError(f"无法识别峰表列：{header!r}") from None
    if len(set(mapping)) != len(mapping) or not {"no", "d_angstrom", "intensity_percent"}.issubset(mapping):
        raise CardParseError("峰表必需列缺失或重复")
    peaks = []
    for line in lines[1:]:
        fields = ([line[start:end].strip() for start, end in zip(fixed_bounds, fixed_bounds[1:])]
                  if fixed_bounds else _columns(line))
        if len(fields) != len(mapping):
            raise CardParseError(f"峰表行列数不一致：{line[:100]}")
        peak = {key: None for key in PEAK_KEYS}
        peak.update({key: value or None for key, value in zip(mapping, fields)})
        if peak["no"] != str(len(peaks) + 1):
            raise CardParseError("峰表行号不连续，不能确认已导出完整峰表")
        _decimal(peak["d_angstrom"], "d 值", positive=True)
        if _decimal(peak["intensity_percent"], "相对强度") < 0:
            raise CardParseError("峰表强度不能为负数")
        if peak["two_theta_deg"]:
            _decimal(peak["two_theta_deg"], "2θ")
        for key in ("h", "k", "l"):
            if peak[key] is not None and not re.fullmatch(r"[+-]?[0-9]+", peak[key]):
                raise CardParseError(f"不能识别晶面指数 {key}={peak[key]}")
        peaks.append(peak)
    if not peaks:
        raise CardParseError("完整峰表没有数据行")
    if wavelength is not None:
        _decimal(wavelength, "软件波长", positive=True)
    crystallography = sections["Crystallographic parameters"]
    cells = {}
    for key, label in (("a_angstrom", "a"), ("b_angstrom", "b"), ("c_angstrom", "c"),
                       ("alpha_deg", "Alpha"), ("beta_deg", "Beta"), ("gamma_deg", "Gamma")):
        match = re.search(r"^[ \t]*" + label + r"[ \t]*\([^\n)]*\)[ \t]*:[ |\t]*([^|\n]*)", crystallography, re.MULTILINE)
        cells[key] = match.group(1).strip() or None if match else None
    return {
        "code": code, "source": source, "phase_name": phase_name,
        "mineral_name": _field(identity, "Mineral name"),
        "chemical_formula": _field(identity, "Chemical formula"),
        "empirical_formula": _field(identity, "Empirical formula"),
        "crystal_system": _field(crystallography, "Crystal system"),
        "space_group": _field(crystallography, "Space group"),
        "space_group_number": _field(crystallography, "Space group number"),
        "cell_parameters": cells, "crystallographic_text": crystallography,
        "quality": _field(sections["Subfiles and quality"], "Quality"),
        "subfiles_text": sections["Subfiles and quality"],
        "comments": sections["Comments"], "references": sections["References"],
        "structure_text": sections["Structure data"],
        "peaks": peaks, "peak_count": len(peaks), "wavelength_angstrom": wavelength,
        "raw_text": text, "complete": True,
        "source_note": "数据库来源未在原生导出中提供" if not source else None,
    }


def parse_rtf(data: bytes, **kwargs) -> dict:
    from striprtf.striprtf import rtf_to_text

    if not isinstance(data, bytes) or not data.lstrip().startswith(b"{\\rtf"):
        raise CardParseError("文件不是 HighScore 原生 RTF")
    try:
        text = rtf_to_text(data.decode("latin-1"), errors="strict")
    except (UnicodeError, ValueError, IndexError) as error:
        raise CardParseError(f"RTF 解码失败：{error}") from error
    return parse_card_text(text, **kwargs)
