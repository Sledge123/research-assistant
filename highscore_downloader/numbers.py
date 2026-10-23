"""解析卡片编号；仅对已核验的旧式编号范围推导数据库。"""

from dataclasses import dataclass
import re
import unicodedata


_DASH_CHARACTERS = "-\u2010\u2011\u2012\u2013\u2014\u2212\uff0d"
_DASH_TRANSLATION = str.maketrans({character: "-" for character in _DASH_CHARACTERS})
_DASH_SPACING = re.compile(r"\s*([" + re.escape(_DASH_CHARACTERS) + r"])\s*")
_LEGACY = re.compile(r"([0-9]{1,3})-([0-9]{1,4})")
_FULL = re.compile(r"([0-9]{1,2})-([0-9]{1,3})-([0-9]{1,4})")
_COMPACT = re.compile(r"[0-9]{9}")
_ICSD = re.compile(r"ICSD-([0-9]{1,6})",re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s,，;；]+")


@dataclass(frozen=True)
class ReferenceCode:
    """用户输入、检索编号及可确定的完整编号。"""

    original: str
    query: str
    canonical_hint: str | None

    @property
    def dedupe_key(self) -> str:
        """未知数据库的旧式编号拥有独立的去重命名空间。"""
        return self.canonical_hint or f"legacy:{self.query}"

    def matches(self, actual: str) -> bool:
        """核对检索结果编号；无法解析的结果不匹配。"""
        try:
            parsed = parse_code(actual)
        except ValueError:
            return False
        if self.canonical_hint is not None:
            return self.canonical_hint == parsed.canonical_hint
        return self.query.split("-")[-2:] == parsed.query.split("-")[-2:]


def parse_code(text: str) -> ReferenceCode:
    """接受旧式、完整或九位数字编号，拒绝多余字符和超宽字段。"""
    if not isinstance(text, str):
        raise ValueError(f"非法卡片编号 {text!r}：必须输入文本。")
    normalized = unicodedata.normalize("NFKC", text).translate(_DASH_TRANSLATION)
    normalized = _DASH_SPACING.sub(r"\1", normalized.strip())

    structure_match = _ICSD.fullmatch(normalized)
    if structure_match is not None:
        accession=int(structure_match[1])
        if accession<1:raise ValueError('ICSD 结构编号必须为正整数。')
        canonical=f'ICSD-{accession:06d}'
        return ReferenceCode(text.strip(),canonical,canonical)

    if _COMPACT.fullmatch(normalized):
        canonical = f"{normalized[:2]}-{normalized[2:5]}-{normalized[5:]}"
        return ReferenceCode(text.strip(), canonical, canonical)

    full_match = _FULL.fullmatch(normalized)
    if full_match is not None:
        database, set_number, entry = map(int, full_match.groups())
        canonical = f"{database:02d}-{set_number:03d}-{entry:04d}"
        return ReferenceCode(text.strip(), canonical, canonical)

    legacy_match = _LEGACY.fullmatch(normalized)
    if legacy_match is not None:
        set_number, entry = map(int, legacy_match.groups())
        query = f"{set_number:03d}-{entry:04d}"
        database = None
        if 1 <= set_number <= 53:
            database = "00"
        elif 70 <= set_number <= 89:
            database = "01"
        if database is not None:
            canonical = f"{database}-{query}"
            return ReferenceCode(text.strip(), canonical, canonical)
        return ReferenceCode(text.strip(), query, None)

    raise ValueError(
        f"非法卡片编号 {text!r}：请输入旧式 19-0629、完整 00-019-0629 "
        "或九位数字 000190629，也可输入 ICSD-12345 结构编号；"
        "数据库、组号和条目最多为 2、3、4 位数字，ICSD 编号为 1 至 6 位正整数。"
    )


def parse_codes(text: str, limit: int = 20) -> list[ReferenceCode]:
    """解析批量输入，按首次出现顺序去重，再检查数量上限。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"编号数量上限 {limit!r}：必须为正整数。")
    if not isinstance(text, str):
        raise ValueError(f"非法卡片编号输入 {text!r}：必须输入文本。")
    compact_spacing = _DASH_SPACING.sub(r"\1", text)
    tokens = [token for token in _SEPARATORS.split(compact_spacing) if token]
    if not tokens:
        raise ValueError(f"非法卡片编号输入 {text!r}：至少需要一个卡片编号。")

    codes = []
    seen = set()
    for token in tokens:
        code = parse_code(token)
        if code.dedupe_key not in seen:
            codes.append(code)
            seen.add(code.dedupe_key)
    if len(codes) > limit:
        raise ValueError(f"卡片编号数量超过上限 {limit}，去重后为 {len(codes)} 个。")
    return codes
