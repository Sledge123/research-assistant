"""从单卡原始记录重建科学数据；不依赖卡号白名单或服务器挂载私库。"""
import base64
import hashlib
import re
from types import SimpleNamespace

from .reader import DatabaseError, DatabaseReader, PDF2_THOUSAND_INTENSITY_ORIGIN


def same_values(first, second):
    """数值相同但SQLite类型不同也不是相同源字段。"""
    if type(first) is not type(second):
        return False
    if isinstance(first, dict):
        return first.keys() == second.keys() and all(same_values(first[key], second[key]) for key in first)
    if isinstance(first, list):
        return len(first) == len(second) and all(same_values(a, b) for a, b in zip(first, second))
    return first == second


def _blob(value):
    if (not isinstance(value, dict) or set(value) != {'blob_base64', 'bytes', 'sha256'}
            or type(value['bytes']) is not int):
        raise ValueError()
    payload = base64.b64decode(value['blob_base64'], validate=True)
    if len(payload) != value['bytes'] or hashlib.sha256(payload).hexdigest() != value['sha256']:
        raise ValueError()
    return payload


def _table_rows(records, name):
    table = records['tables'][name]
    columns = [column['name'] for column in table['columns']]
    if not columns or len(set(columns)) != len(columns):
        raise ValueError()
    rows = table['rows']
    if not isinstance(rows, list):
        raise ValueError()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(columns):
            raise ValueError()
    return rows


def _hsrdb_card(card):
    raw = card['database_raw']
    sha = card['database_sha256']
    if (not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)
            or card['database_id'] != 'hsrdb-' + sha[:48]
            or not isinstance(card['source'], str) or not card['source'].strip()):
        raise ValueError()
    indexed, text, stored = (raw[key] for key in ('indexed', 'text', 'stored'))
    pid = indexed['id']
    if type(pid) is not int or text['docid'] != pid or stored['pid'] != pid:
        raise ValueError()
    records = raw['source_records']
    if records['format'] != 'sqlite_card_rows_v1' or records['product_id'] != indexed['ProductID']:
        raise ValueError()
    # 单卡物理快照和用于排版的字段必须完全同型同值；不接受只重新计算摘要的删改。
    if (not same_values(_table_rows(records, 'general_indexed'), [indexed])
            or not same_values(_table_rows(records, 'general_text_content'), [text])):
        raise ValueError()
    stored_rows = _table_rows(records, 'general_stored')
    if len(stored_rows) != 1:
        raise ValueError()
    original_stored = stored_rows[0]
    scalar_stored = {key: value for key, value in original_stored.items() if key not in ('Lines', 'HKL', 'LinesI')}
    if not same_values(scalar_stored, stored):
        raise ValueError()
    lines = base64.b64decode(raw['lines_base64'], validate=True)
    hkl = base64.b64decode(raw['hkl_base64'], validate=True) if raw['hkl_base64'] is not None else None
    if _blob(original_stored['Lines']) != lines:
        raise ValueError()
    if (None if original_stored['HKL'] is None else _blob(original_stored['HKL'])) != hkl:
        raise ValueError()
    intensity_scale = raw.get('intensity_scale', 100)
    lines_i = None
    if intensity_scale == 999:
        if (raw.get('reader_profile') != 'pdf2_2.3'
                or raw.get('intensity_origin') != PDF2_THOUSAND_INTENSITY_ORIGIN
                or 'LinesI' in original_stored or 'lines_i_base64' in raw):
            raise ValueError()
    elif intensity_scale != 100:
        if intensity_scale != 1000 or raw.get('reader_profile') != 'cod2024_hs4x':
            raise ValueError()
        lines_i = base64.b64decode(raw['lines_i_base64'], validate=True)
        if _blob(original_stored['LinesI']) != lines_i:
            raise ValueError()
    elif 'LinesI' in original_stored or 'lines_i_base64' in raw:
        raise ValueError()
    literature_rows = sorted(_table_rows(records, 'literature_text_content'), key=lambda item: item['docid'])
    mapping = _table_rows(records, 'literature_map')
    if (any(item['pid'] != pid for item in mapping)
            or {item['doc'] for item in mapping} != {item['docid'] for item in literature_rows}):
        raise ValueError()
    literature = [item for item in literature_rows for relation in mapping if relation['doc'] == item['docid']]
    subfile_rows = sorted(_table_rows(records, 'Subfiles'), key=lambda item: item['ID'])
    subfile_mapping = _table_rows(records, 'SubFileRef')
    if (any(item['pid'] != pid for item in subfile_mapping)
            or {item['SubFileID'] for item in subfile_mapping} != {item['ID'] for item in subfile_rows}):
        raise ValueError()
    peaks = DatabaseReader._peaks(lines, hkl, intensity_scale=intensity_scale, lines_i=lines_i)
    candidate = DatabaseReader._candidate(SimpleNamespace(source=card['source'], _code=DatabaseReader._code),
                                         {**indexed, **text})
    return {**candidate, 'database_id': card['database_id'], 'database_sha256': sha,
            'complete': True, 'peak_count': len(peaks), 'peaks': peaks, 'wavelength_angstrom': None,
            'cell_parameters': {key.lower(): None if indexed[key] is None else str(indexed[key])
                                for key in ('A', 'B', 'C', 'ALPHA', 'BETA', 'GAMMA')},
            'space_group': stored['SPGR'], 'comments': stored['Comment'], 'literature': literature,
            'subfiles': [item['SubFile'] for item in subfile_rows for relation in subfile_mapping
                         if relation['SubFileID'] == item['ID']], 'empirical_formula': text['c4empiricalformula'],
            'database_raw': raw}


def refresh_card(card, *, report_settings=None, standardize_index=True):
    """只读重建单卡，返回新报告科学数据；旧回执和原字节保持。

    未另传波长时沿用旧报告明确记录的换算设置；没有来源时不生成角度。
    此函数不把管理员绑定的整库摘要当作单卡切片的密码学归属证明。
    """
    try:
        if card.get('data_type') == 'jade_index_data':
            from .jade_index import validate_index_card
            source = validate_index_card(card)
        elif card.get('data_type') == 'jade_structure_data':
            from .jade_structure import validate_structure_card
            source = validate_structure_card(card)
        else:
            source = _hsrdb_card(card)
        if report_settings is None and card.get('angular_conversion'):
            conversion = card['angular_conversion']
            if (conversion.get('method') != 'bragg_first_order'
                    or conversion.get('two_theta_origin') != 'calculated'
                    or conversion.get('wavelength_angstrom') != card.get('wavelength_angstrom')
                    or not isinstance(conversion.get('wavelength_source'), str)
                    or not conversion['wavelength_source'].strip()):
                raise ValueError()
            report_settings = {'wavelength_angstrom': conversion['wavelength_angstrom'],
                               'source': conversion['wavelength_source']}
        from .exporter import prepare_card
        return prepare_card(source, report_settings, standardize_index=standardize_index)
    except DatabaseError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise DatabaseError('单卡原始记录、科学字段或完整峰表复验失败。') from None


def validate_database_card(card):
    """v4 HSRDB报告全字段复验，覆盖名称/晶胞/注释/文献及完整峰表。"""
    expected = refresh_card(card)
    for key, value in expected.items():
        if not same_values(card.get(key), value):
            raise DatabaseError('数据库报告科学字段与单卡原始记录不一致。')
    return expected
