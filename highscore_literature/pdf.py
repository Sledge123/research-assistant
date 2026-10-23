"""从实际 PDF 内容核对论文身份；不以文件扩展名代替核验。"""
from difflib import SequenceMatcher
import hashlib
import io
import math
import re
import unicodedata

MAX_PDF_BYTES = 36 * 1024 * 1024
DOI_PATTERN = re.compile(r'(?i)(?<![a-z0-9])10\.\d{4,9}/[^\s<>"\'\x00-\x1f]+')
PUBLICATION_DOI = re.compile(r'(?i)^\s*(?:(?:doi|digital object identifier)\s*[:：]?\s*|https?://(?:dx\.)?doi\.org/|10\.\d{4,9}/|article \| article in press |(?:article|research article|communication|review)\s+https?://(?:dx\.)?doi\.org/)')
REFERENCES = re.compile(r'(?im)(?:^|\n)\s*(?:\d+[.\s]+)?(?:references(?:\s+and\s+notes)?|bibliography|参考文献)\s*(?:[:.\n]|$)')


def extract_dois(value):
    """完整提取标识，不能把 a2 的前缀当作 a；保留 DOI 内平衡的括号。"""
    result=set()
    for match in DOI_PATTERN.finditer(unicodedata.normalize('NFKC',value)):
        token=match.group(0).casefold().rstrip('.,;:')
        while token.endswith(')') and token.count(')')>token.count('('):token=token[:-1]
        result.add(token)
    return result


def publication_dois(line):
    """出版标识必须占据整行，摘要叙述不能仅凭行首 DOI 文字获得身份。"""
    if not PUBLICATION_DOI.match(line):return set()
    value=unicodedata.normalize('NFKC',line).strip()
    value=re.sub(r'(?i)^(?:(?:doi|digital object identifier)\s*[:：]?\s*|article \| article in press |(?:article|research article|communication|review)\s+)','',value)
    value=re.sub(r'(?i)^https?://(?:dx\.)?doi\.org/','',value).casefold()
    tokens=extract_dois(value)
    return tokens if DOI_PATTERN.fullmatch(value) and len(tokens)==1 else set()


def normalize_title(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def title_matches(requested, actual):
    requested, actual = normalize_title(requested), normalize_title(actual)
    return bool(requested and actual and (requested == actual or SequenceMatcher(None, requested, actual).ratio() >= .94))


def validate_pdf(data, title, doi=None):
    if not isinstance(data, bytes) or not 200 <= len(data) <= MAX_PDF_BYTES or not data.startswith(b'%PDF-'):
        raise ValueError('下载结果不是完整且大小合规的 PDF。')
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 5000:
            raise ValueError()
        page=reader.pages[0]
        footer_dois=set()
        reference_seen=False
        bottom=float(page.cropbox.bottom)
        height=float(page.cropbox.height)
        def visit(text,cm,tm,font,size):
            nonlocal reference_seen
            if reference_seen:return
            parts=REFERENCES.split(text,maxsplit=1)
            reference_seen=len(parts)>1
            y=float(tm[4])*float(cm[1])+float(tm[5])*float(cm[3])+float(cm[5])-bottom
            # 部分期刊的独立 DOI 页脚位于 72 pt 以上；保留页高 12% 和 108 pt 双上限。
            if page.rotation==0 and math.isfinite(y) and 0<=y<=min(108,height*.12):
                for line in parts[0].splitlines():footer_dois.update(publication_dois(line))
        pages = [page.extract_text(visitor_text=visit) or '']
    except Exception:
        raise ValueError('PDF 无法完整读取或受密码保护。') from None
    # 论文题名必须位于第一页正文题名区域；参考文献中的题名不是本文身份。
    first_page = re.split(r'(?im)(?:^|\n)\s*(?:\d+[.\s]+)?(?:abstract|introduction|keywords|references(?:\s+and\s+notes)?|bibliography|contents|摘要|引言|参考文献)\s*[:.\n]',pages[0],maxsplit=1)[0]
    lines=[normalize_title(line) for line in first_page[:4000].splitlines() if line.strip()][:40]
    expected = normalize_title(title)
    # 题名可跨行，但必须占据完整连续文本行，不能只是另一篇摘要/正文中的一句引用。
    matched=any(''.join(lines[start:end])==expected for start in range(len(lines)) for end in range(start+1,min(start+12,len(lines))+1))
    if len(expected) < 6 or not matched:
        raise ValueError('PDF 首页题名区域未能确认所求论文标题，未提供下载或扣分。')
    if doi:
        normalized_doi=re.sub(r'(?i)^https?://(?:dx\.)?doi\.org/','',unicodedata.normalize('NFKC',doi).strip()).casefold()
        expected_dois=extract_dois(normalized_doi)
        # 只认可题名/出版元数据中的完整 DOI 行，或实际位于首页底部的独立出版页脚。
        # 摘要、正文叙述和参考文献中出现该 DOI 都不能证明本文身份。
        own_dois=set(footer_dois)
        for line in first_page[:4000].splitlines()[:40]:own_dois.update(publication_dois(line))
        if not DOI_PATTERN.fullmatch(normalized_doi) or expected_dois!={normalized_doi} or own_dois!=expected_dois:
            raise ValueError('PDF 首页出版信息未能唯一确认所求完整 DOI，未提供下载或扣分。')
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'pages': len(reader.pages)}
