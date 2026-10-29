"""可读卡片栏目与 RTF 排版；源字段仍由 JSON 独立保留。"""
import math
import re

REPORT_VERSION = 3
REPORT_DIRECTORY = 'report-v3'
PREVIOUS_REPORT_DIRECTORIES = ('report-v2',)
INDEX_DATA_TYPE = 'jade_index_data'
INDEX_REPORT_METHOD = 'jade_index_readonly_report'
INDEX_REPORT_ORIGIN = 'generated_index_report'
STRUCTURE_DATA_TYPE = 'jade_structure_data'
STRUCTURE_REPORT_METHOD = 'jade_structure_readonly_report'
STRUCTURE_REPORT_ORIGIN = 'generated_structure_report'
MISSING = '未提供'
CRYSTAL_SYSTEMS = {'C': '立方', 'H': '六方', 'T': '四方', 'O': '正交',
                   'M': '单斜', 'R': '三方', 'A': '三斜'}
QUALITIES = {'S': 'Star (S)', 'I': 'Indexed (I)'}


def clean_text(value):
    """仅转换已在原生卡片中核对的显示标记，不把数据库文本当作 RTF。"""
    if value is None:
        return ''
    text = str(value).replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\\(?:IT|BF|RG)', '', text)
    text = re.sub(r'(?<=[A-Za-z0-9])#+(?=[A-Za-z0-9])', '', text)
    def formula(match):
        content = match[1]
        if re.fullmatch(r'[A-Za-z0-9+@!().,\-\s]+', content):
            return re.sub(r'\s+', '', content).replace('!', '·').replace('@', '')
        return content
    text = re.sub(r'\\([^\\\n]+)\\', formula, text)
    text = text.replace('@+', '+').replace('@-', '-')
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)
    return '\n'.join(line.strip() for line in text.splitlines()).strip()


def _number(value, places, *, positive=True):
    try:
        number = float(value)
        if not math.isfinite(number) or (positive and number <= 0):
            return None
        return f'{number:.{places}f}'
    except (ValueError, TypeError):
        return None


def report_fields(card):
    if card.get('data_type') in {INDEX_DATA_TYPE, STRUCTURE_DATA_TYPE}:
        return {
            'mineral_name': clean_text(card.get('mineral_name')),
            'compound_name': clean_text(card.get('compound_name')) or card['phase_name'],
            'chemical_formula': clean_text(card.get('chemical_formula')),
        }
    raw = card['database_raw']; indexed = raw['indexed']; texts = raw['text']; stored = raw['stored']
    def code_label(code, labels):
        code = clean_text(code)
        return labels.get(code, f'未识别（{code}）' if code else MISSING)
    def reference(item):
        parts = [clean_text(item.get(key)) for key in
                 ('c3authors', 'c4journal', 'c0volume', 'c1pages')]
        parts = [part.rstrip(' ,;') for part in parts if part]
        year = clean_text(item.get('c2year'))
        if year:
            parts.append(f'({year})')
        kind = item.get('c5referencetype')
        label = {4: '主要文献', 5: '晶胞参数文献'}.get(kind, '文献')
        return {'label': label, 'text': ', '.join(parts) or MISSING}
    return {
        'mineral_name': clean_text(texts.get('c2mineralname')),
        'compound_name': clean_text(texts.get('c1compoundname')) or card['phase_name'],
        'common_name': clean_text(texts.get('c3commonname')),
        'empirical_formula': re.sub(r'\s+', '', clean_text(card.get('empirical_formula'))),
        'chemical_formula': re.sub(r'\s+', '', clean_text(card.get('chemical_formula'))),
        'crystal_system': code_label(indexed.get('XTSLSYS'), CRYSTAL_SYSTEMS),
        'space_group': clean_text(card.get('space_group')),
        'space_group_number': str(indexed['XTLASPECTN']) if indexed.get('XTLASPECTN', 0) and indexed['XTLASPECTN'] > 0 else None,
        'cell': {key: _number(card['cell_parameters'].get(key), 4) for key in ('a', 'b', 'c', 'alpha', 'beta', 'gamma')},
        'density_calculated': _number(indexed.get('DX'), 2),
        'density_measured': _number(indexed.get('DM'), 2),
        'cell_volume': _number(indexed.get('XTLVOL'), 2),
        'z': _number(indexed.get('Z'), 2), 'rir': _number(indexed.get('IIC'), 2),
        'quality': code_label(indexed.get('QUALFINAL'), QUALITIES),
        'subfiles': [clean_text(value) for value in card.get('subfiles', []) if clean_text(value)],
        'color': clean_text(texts.get('c5color')), 'cas': clean_text(stored.get('CAS')),
        'comments': re.sub(r'(?i)(?<!\w)(?:Color|Colour|颜色)\s*[:：]\s*[^.\n。]*(?:[.。]|$)',
                           '',clean_text(card.get('comments'))).strip(),
        'references': [reference(item) for item in card.get('literature', [])],
    }


def _index_report_lines(card):
    fields = report_fields(card)
    rows = [('heading', '名称与化学式'), ('blank', '')]
    for label, value in (
        ('卡片编号', card['code']), ('化合物名称', fields['compound_name']),
        ('矿物名称', fields['mineral_name']), ('化学式', fields['chemical_formula']),
    ):
        rows.append(('field', label + '：\t' + (value or MISSING)))
    rows.extend([('blank', ''), ('heading', '索引峰表'), ('blank', ''),
                 ('field', '索引峰数：\t' + str(card['peak_count']))])
    peak_header = ' No.    h    k    l       d [Å]    I [原始值]    标尺 [原始值]'
    rows.append(('peak', peak_header))
    for index, peak in enumerate(card['peaks']):
        if index and index % 48 == 0:
            rows.extend([('continuation', '索引峰表（续）'), ('peak', peak_header)])
        value = lambda key: '' if peak.get(key) is None else str(peak[key])
        distance = _number(peak['d_angstrom'], 5)
        rows.append(('peak', f"{value('no'):>4} {value('h'):>4} {value('k'):>4} {value('l'):>4} "
                     f"{distance:>11} {value('intensity_raw'):>13} {value('intensity_scale_raw'):>17}"))
    return rows


def _structure_report_lines(card):
    fields = report_fields(card)
    structure = card['structure']; cell = structure['cell']; group = structure['space_group']
    rows = [('heading', '名称与化学式'), ('blank', '')]
    for label, value in (
        ('结构编号', card['code']), ('化合物名称', fields['compound_name']),
        ('矿物名称', fields['mineral_name']), ('化学式', fields['chemical_formula']),
    ):
        rows.append(('field', label + '：\t' + (value or MISSING)))
    rows.extend([('blank', ''), ('heading', '晶体学参数'), ('blank', '')])
    for key, label in [('a', 'a (Å)'), ('b', 'b (Å)'), ('c', 'c (Å)'),
                       ('alpha', 'α (°)'), ('beta', 'β (°)'), ('gamma', 'γ (°)')]:
        rows.append(('field', label + '：\t' + str(cell[key])))
    for key, label in [('symbol', '空间群'), ('number', '空间群编号'), ('setting', '空间群设置')]:
        value = group.get(key)
        rows.append(('field', label + '：\t' + (str(value) if value not in (None, '') else MISSING)))
    rows.extend([('blank', ''), ('heading', '对称操作'), ('blank', '')])
    rows.extend(('body', operation) for operation in group['symmetry_operations'])
    rows.extend([('blank', ''), ('heading', '原子坐标'), ('blank', ''),
                 ('field', '原子位点数：\t' + str(card['site_count']))])
    header = ' No.  位点  元素  占位率  x  y  z  多重性  电荷'
    rows.append(('peak', header))
    for index, site in enumerate(structure['sites']):
        if index and index % 48 == 0:
            rows.extend([('continuation', '原子坐标（续）'), ('peak', header)])
        values = [str(index + 1)] + [str(site[key]) if site.get(key) is not None else ''
                   for key in ('label', 'element', 'occupancy', 'x', 'y', 'z', 'multiplicity', 'charge')]
        rows.append(('peak', '  '.join(values)))
    return rows


def report_lines(card):
    """所有格式共用同一组段落，保证 RTF 读回与 TXT 一致。"""
    if card.get('data_type') == INDEX_DATA_TYPE:
        return _index_report_lines(card)
    if card.get('data_type') == STRUCTURE_DATA_TYPE:
        return _structure_report_lines(card)
    fields = report_fields(card); rows = []
    def heading(text):
        if rows:
            rows.append(('blank', ''))
        rows.append(('heading', text))
        rows.append(('blank', ''))
    def field(label, value, kind='field'):
        rows.append((kind, label + '：\t' + (str(value) if value not in (None, '') else MISSING)))
    heading('名称与化学式')
    field('卡片编号', card['code'])
    field('矿物名称', fields['mineral_name']); field('化合物名称', fields['compound_name'])
    if fields['common_name']:
        field('通用名称', fields['common_name'])
    field('经验式', fields['empirical_formula'], 'formula')
    field('化学式', fields['chemical_formula'], 'formula')
    if fields['cas']:
        field('CAS', fields['cas'])
    heading('晶体学参数')
    field('晶系', fields['crystal_system']); field('空间群', fields['space_group'])
    field('空间群编号', fields['space_group_number'])
    for key, label in [('a', 'a (Å)'), ('b', 'b (Å)'), ('c', 'c (Å)'),
                       ('alpha', 'α (°)'), ('beta', 'β (°)'), ('gamma', 'γ (°)')]:
        field(label, fields['cell'][key])
    field('计算密度 (g/cm³)', fields['density_calculated'])
    field('实测密度 (g/cm³)', fields['density_measured'])
    field('晶胞体积 (Å³)', fields['cell_volume']); field('Z', fields['z']); field('RIR', fields['rir'])
    heading('子库与质量')
    field('子库', '; '.join(fields['subfiles'])); field('质量', fields['quality'])
    heading('注释')
    rows.extend(('body', value) for value in (fields['comments'] or MISSING).splitlines())
    heading('参考文献')
    if fields['references']:
        for reference in fields['references']:
            field(reference['label'], reference['text'])
    else:
        rows.append(('body', MISSING))
    heading('峰表')
    field('峰数', card['peak_count'])
    conversion = card.get('angular_conversion')
    if conversion:
        field('波长 (Å)', card['wavelength_angstrom'])
    peak_header=' No.    h    k    l       d [Å] '+('2Theta(calc) [°]' if conversion else '    2Theta [°]')+'    I [%]'
    rows.append(('peak', peak_header))
    for index,peak in enumerate(card['peaks']):
        if index and index%48==0:
            rows.extend([('continuation','峰表（续）'),('peak',peak_header)])
        value = lambda key: '' if peak.get(key) is None else str(peak[key])
        distance = _number(peak['d_angstrom'], 5)
        angle = _number(peak.get('two_theta_deg'), 3, positive=False) or ''
        intensity = _number(peak['intensity_percent'], 1, positive=False)
        rows.append(('peak', f"{value('no'):>4} {value('h'):>4} {value('k'):>4} {value('l'):>4} {distance:>11} {angle:>13} {intensity:>8}"))
    return rows


def report_text(card):
    return '\n'.join(text for _, text in report_lines(card)) + '\n'


def escape_rtf(text):
    fragments = []
    for character in text:
        if character in '\\{}':
            fragments.append('\\' + character)
        elif character == '\n':
            fragments.append('\\par\n')
        elif character == '\t':
            fragments.append('\\tab ')
        elif 32 <= ord(character) < 127:
            fragments.append(character)
        else:
            encoded = character.encode('utf-16-le')
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index:index+2], 'little')
                fragments.append('\\u' + str(unit if unit < 32768 else unit - 65536) + '?')
    return ''.join(fragments)


def report_rtf(card):
    header = (r'{\rtf1\ansi\deff0{\fonttbl{\f0\fnil Tahoma;}'
              r'{\f1\fmodern Courier New;}{\f2\fnil\fcharset134 SimSun;}}'
              r'\uc1\paperw11906\paperh16838\margl1134\margr1134\margt850\margb850'
              r'\widowctrl\deflang2052' + '\n')
    output = [header]
    for kind, text in report_lines(card):
        controls = r'\pard\plain\f0\lang2052\fs20\sa30\sl240\slmult1'
        if kind == 'heading':
            controls += r'\keepn\sb160\sa40\b\ul\fs24'
            if text=='峰表':controls += r'\pagebb'
        elif kind=='continuation':
            controls += r'\keepn\pagebb\b\ul\fs24'
        elif kind in {'field', 'formula'}:
            controls += r'\fi-3000\li3000\tx3000'
        elif kind == 'peak':
            controls += r'\f1\fs18\sa0\sl220\slmult1'
        elif kind == 'note':
            controls += r'\fs17\sa30'
        elif kind == 'blank':
            controls += r'\fs8\sa0\sl100\slmult1'
        content = escape_rtf(text)
        if kind == 'formula':
            label, value = text.split('\t', 1)
            # 仅将元素符号后的计量数字下标化；不改变读回文本或电荷数字。
            fragments = []; start = 0
            for match in re.finditer(r'(?<=[A-Za-z)])(\d+)', value):
                fragments.extend([escape_rtf(value[start:match.start()]),
                                  '{\\dn6\\fs16 ' + match[1] + '}'])
                start = match.end()
            fragments.append(escape_rtf(value[start:]))
            content = escape_rtf(label) + r'\tab ' + ''.join(fragments)
        output.append(controls + ' ' + content + '\\par\n')
    output.append('}')
    return ''.join(output).encode('ascii')
