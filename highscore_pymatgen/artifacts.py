"""纯标准库结果包复验；核对绑定、格式、几何及布拉格关系，不重算散射强度。"""
from collections import Counter, defaultdict, deque
import csv
import hashlib
import io
import json
import math
import struct
import zipfile
import zlib

from .contract import (MAX_INPUT_ATOMS, MAX_OUTPUT_ATOMS, MAX_RECIPROCAL_POINTS, OPERATION_LABELS, PROFILE_VERSION,
                       GEOMETRY_FRACTIONAL_TOLERANCE, GEOMETRY_LATTICE_RELATIVE_TOLERANCE,
                       GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE, GEOMETRY_MAX_CANDIDATE_CHECKS,
                       GEOMETRY_MAX_MATCHING_STEPS, GEOMETRY_VERIFICATION,
                       PYMATGEN_CORE_VERSION, PYMATGEN_VERSION, SYMBOLS,
                       XRD_MAX_OUTPUT_PEAKS, XRD_TWO_THETA_TOLERANCE, extinction_error_recipe,
                       validate_input_bytes, validate_inputs, validate_parameters)

MAX_BUNDLE_BYTES = 12 * 1024 * 1024
MAX_UNPACKED_BYTES = 24 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('结果JSON包含重复字段。')
        result[key] = value
    return result


def _load(content):
    return json.loads(content.decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('结果包含非有限数值。')))


def _number(value, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError('结构结果数值无效。')
    return float(value)


def determinant(a):
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def inverse(a):
    d = determinant(a)
    if abs(d) < 1e-12:
        raise ValueError('晶胞矩阵不可逆。')
    return [[(a[(j + 1) % 3][(i + 1) % 3] * a[(j + 2) % 3][(i + 2) % 3]
              - a[(j + 1) % 3][(i + 2) % 3] * a[(j + 2) % 3][(i + 1) % 3]) / d
             for j in range(3)] for i in range(3)]


def lattice_parameters(matrix):
    lengths = [math.sqrt(sum(item * item for item in row)) for row in matrix]
    angles = [math.degrees(math.acos(max(-1.0, min(1.0,
        sum(matrix[i][axis] * matrix[j][axis] for axis in range(3)) / (lengths[i] * lengths[j])))))
        for i, j in ((1, 2), (0, 2), (0, 1))]
    return lengths + angles


def check_structure(model, max_atoms=MAX_OUTPUT_ATOMS):
    if not isinstance(model, dict):
        raise ValueError('缺少晶体结构结果。')
    matrix = model.get('lattice_matrix_angstrom')
    if not isinstance(matrix, list) or len(matrix) != 3 or any(not isinstance(row, list) or len(row) != 3 for row in matrix):
        raise ValueError('晶胞矩阵格式无效。')
    matrix = [[_number(item) for item in row] for row in matrix]
    volume = abs(determinant(matrix))
    if volume < 0.1 or not math.isclose(volume, _number(model.get('volume_angstrom3'), True), rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError('晶胞体积与矩阵不一致。')
    parameters = model.get('lattice_parameters')
    if (not isinstance(parameters, list) or len(parameters) != 6
            or any(not math.isclose(_number(a), b, rel_tol=1e-8, abs_tol=1e-7)
                   for a, b in zip(parameters, lattice_parameters(matrix)))):
        raise ValueError('晶胞长度或角度与矩阵不一致。')
    symbols, positions = model.get('symbols'), model.get('fractional_positions')
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= max_atoms or any(symbol not in SYMBOLS for symbol in symbols):
        raise ValueError('结构元素或原子数量无效。')
    if not isinstance(positions, list) or len(positions) != len(symbols):
        raise ValueError('结构坐标数量不一致。')
    for point in positions:
        if not isinstance(point, list) or len(point) != 3 or any(not 0 <= _number(value) < 1 for value in point):
            raise ValueError('结构分数坐标超出有效范围。')
    if (type(model.get('atom_count')) is not int or model.get('atom_count') != len(symbols)
            or model.get('composition') != dict(Counter(symbols))
            or any(type(value) is not int for value in model.get('composition', {}).values())
            or model.get('occupancy') != 'full' or not isinstance(model.get('source'), dict)
            or not isinstance(model.get('formula'), str) or not 1 <= len(model['formula']) <= 1024):
        raise ValueError('结构数量、组成或占有率不一致。')
    json_bytes(model)
    return model


def _csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def _f(value):
    return format(value, '.15g')


def structure_files(model, prefix=''):
    check_structure(model)
    cell = model['lattice_parameters']
    lines = ['data_crystal_tool', '# Expanded geometry; source declaration is preserved in original input and JSON.',
             *['_cell_%s %s' % (name, _f(value)) for name, value in zip(
                 ('length_a', 'length_b', 'length_c', 'angle_alpha', 'angle_beta', 'angle_gamma'), cell)],
             "_space_group_name_H-M_alt 'P 1'", '_space_group_IT_number 1',
             'loop_', '_space_group_symop_operation_xyz', "'x,y,z'", 'loop_', '_atom_site_label',
             '_atom_site_type_symbol', '_atom_site_fract_x', '_atom_site_fract_y', '_atom_site_fract_z', '_atom_site_occupancy']
    rows = [('index', 'element', 'fractional_x', 'fractional_y', 'fractional_z')]
    for index, (symbol, point) in enumerate(zip(model['symbols'], model['fractional_positions']), 1):
        lines.append('%s%d %s %s %s %s 1' % (symbol, index, symbol, *map(_f, point)))
        rows.append((index, symbol, *map(_f, point)))
    species = sorted(set(model['symbols']))
    poscar = ['晶体工具导出', '1.0', *[' '.join(map(_f, row)) for row in model['lattice_matrix_angstrom']],
              ' '.join(species), ' '.join(str(model['composition'][symbol]) for symbol in species), 'Direct']
    for symbol in species:
        poscar.extend(' '.join(map(_f, point)) for actual, point in zip(model['symbols'], model['fractional_positions']) if actual == symbol)
    return {prefix + 'structure.cif': ('\n'.join(lines) + '\n').encode('utf-8'),
            prefix + 'structure.poscar': ('\n'.join(poscar) + '\n').encode('utf-8'),
            prefix + 'structure.json': json_bytes(model), prefix + 'sites.csv': _csv_bytes(rows)}


def input_bindings(inputs, operation):
    result = []
    for index, item in enumerate(validate_inputs(inputs, operation)):
        kind = validate_input_bytes(item['content'], item['filename'])
        prefix = ('a-', 'b-')[index] if operation == 'compare' else ''
        result.append({'filename': item['filename'], 'format': kind, 'archive_name': prefix + 'input.' + kind,
                       'size': len(item['content']), 'sha256': hashlib.sha256(item['content']).hexdigest()})
    return result


def make_summary(operation, structures, details):
    summary = {'operation': operation, 'operation_label': OPERATION_LABELS[operation],
               'formula': ' / '.join(model['formula'] for model in structures),
               'atom_count': sum(model['atom_count'] for model in structures)}
    if operation == 'analyze':
        summary.update({'spacegroup_number': details['spacegroup']['number'], 'spacegroup_symbol': details['spacegroup']['symbol']})
    elif operation == 'xrd':
        summary['peak_count'] = len(details['peaks'])
    elif operation == 'supercell':
        summary['supercell'] = details['scaling']
    elif operation == 'compare':
        summary['matched'] = details['matched']
    return summary


def report_text(result):
    operation, details, params = result['operation'], result['details'], result['parameters']
    lines = [OPERATION_LABELS[operation] + '报告', '']
    for index, model in enumerate(result['structures']):
        label = ('结构A', '结构B')[index] if operation == 'compare' else '结构'
        lines.extend([label + '：' + model['formula'], '原子数：%d' % model['atom_count'],
                      '晶胞长度（Å）：' + '，'.join(_f(value) for value in model['lattice_parameters'][:3]),
                      '晶胞角度（°）：' + '，'.join(_f(value) for value in model['lattice_parameters'][3:]),
                      '晶胞体积（Å³）：' + _f(model['volume_angstrom3']), ''])
    if operation == 'analyze':
        sg = details['spacegroup']
        lines.extend(['计算空间群：%s（%d）' % (sg['symbol'], sg['number']), '晶系：' + sg['crystal_system'],
                      '位置容差：%s Å；角度容差：%s°' % (_f(params['symprec']), _f(params['angle_tolerance'])),
                      '原文件的空间群声明和显式对称操作已保留；计算群由当前展开几何及上述容差确定。'])
    elif operation == 'convert':
        lines.extend(['CIF与POSCAR已从同一完整结构导出，并重新解析核对元素、晶胞和周期坐标。',
                      '转换范围为元素、晶胞和周期坐标；选择性动力学、速度及其他非几何属性仅保留在原始文件。'])
    elif operation == 'supercell':
        lines.extend(['对角扩胞：' + ' × '.join(map(str, params['scaling'])), '已核对扩胞体积、元素数和周期坐标。'])
    elif operation == 'compare':
        lines.extend(['匹配结果：' + ('相同结构（在给定容差内）' if details['matched'] else '未匹配'),
                      '按元素和几何比较，先约化为原胞；保留实际尺寸，不进行子集匹配。',
                      '长度相对容差：%s；位点容差：%s；角度容差：%s°' % (_f(params['ltol']), _f(params['stol']), _f(params['angle_tol']))])
        if details['rms_displacement_normalized'] is not None:
            lines.append('无量纲均方根位移：' + _f(details['rms_displacement_normalized']))
        lines.extend(['位移以每原子平均自由长度归一化，原始价态字段单独保留。',
                      '匹配判据使用最大位移；报告的均方根位移另由最小平均位移配对求得，两种最优配对可能不同。'])
    else:
        lines.extend(['波长：%s Å；角度范围：%s°至%s°' % tuple(_f(params[key]) for key in ('wavelength_angstrom', 'two_theta_min', 'two_theta_max')),
                      '衍射峰数：%d' % len(details['peaks']),
                      '采用静态独立原子理论模型及洛伦兹偏振因子，不进行对称精修。',
                      '原始模型强度为任意单位；相对强度单独按最高峰归一至100。',
                      '归一化前按逐反射的浮点误差估算剔除不可分辨的相消残差，不用固定强度或强峰比例删除弱峰。',
                      '原输入的价态与热参数仅作为来源信息保留，未用于本次强度计算。',
                      '图中为离散衍射峰，不包含实验背景、仪器展宽或实验测量噪声。'])
    return ('\n'.join(lines) + '\n').encode('utf-8')


def xrd_csv(details):
    rows = [('peak_index', 'two_theta_degree', 'd_angstrom', 'intensity_raw_au', 'intensity_relative_max100',
             'h', 'k', 'i', 'l', 'multiplicity')]
    for index, peak in enumerate(details['peaks'], 1):
        for family in peak['hkls']:
            values = family['hkl']
            h, k, i, l = values if len(values) == 4 else (values[0], values[1], '', values[2])
            rows.append((index, *[_f(peak[key]) for key in ('two_theta_degree', 'd_angstrom', 'intensity_raw_au', 'intensity_relative_max100')],
                         h, k, i, l, family['multiplicity']))
    return _csv_bytes(rows)


_GLYPHS = {
    '0': ('111', '101', '101', '101', '111'), '1': ('010', '110', '010', '010', '111'),
    '2': ('111', '001', '111', '100', '111'), '3': ('111', '001', '111', '001', '111'),
    '4': ('101', '101', '111', '001', '001'), '5': ('111', '100', '111', '001', '111'),
    '6': ('111', '100', '111', '101', '111'), '7': ('111', '001', '010', '010', '010'),
    '8': ('111', '101', '111', '101', '111'), '9': ('111', '101', '111', '001', '111'),
    '.': ('000', '000', '000', '000', '010'), '-': ('000', '000', '111', '000', '000'),
    ' ': ('000',) * 5, '/': ('001', '001', '010', '100', '100'),
    'I': ('111', '010', '010', '010', '111'), 'm': ('00000', '11011', '10101', '10101', '10101'),
    'a': ('000', '110', '001', '111', '111'), 'x': ('000', '101', '010', '101', '000'),
    '%': ('101', '001', '010', '100', '101'), '(': ('01', '10', '10', '10', '01'),
    ')': ('10', '01', '01', '01', '10'), 'θ': ('010', '101', '111', '101', '010'),
    '°': ('110', '110', '000', '000', '000')}


def xrd_graphs(details, parameters):
    """从实际峰表直接绘制；不使用生图服务或外部字体。"""
    width, height, left, right, top, bottom = 800, 480, 65, 775, 28, 415
    pixels = bytearray(b'\xff' * (width * height * 3))
    def point(x, y, color=(0, 0, 0)):
        x, y = int(x), int(y)
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset:offset + 3] = bytes(color)
    def line(x1, y1, x2, y2, color=(0, 0, 0)):
        count = max(abs(int(x2) - int(x1)), abs(int(y2) - int(y1)), 1)
        for step in range(count + 1):
            point(round(x1 + (x2 - x1) * step / count), round(y1 + (y2 - y1) * step / count), color)
    def text(x, y, value, scale=2):
        for char in value:
            glyph = _GLYPHS[char]
            for row, glyph_row in enumerate(glyph):
                for column, lit in enumerate(glyph_row):
                    if lit == '1':
                        for dx in range(scale):
                            for dy in range(scale):
                                point(x + column * scale + dx, y + row * scale + dy)
            x += (len(glyph[0]) + 1) * scale
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="480" viewBox="0 0 800 480">',
           '<rect width="800" height="480" fill="white"/>',
           '<g fill="black" font-family="sans-serif" font-size="12">']
    line(left, top, left, bottom); line(left, bottom, right, bottom)
    svg.append('<path d="M65 28V415H775" fill="none" stroke="black"/>')
    lower, upper = parameters['two_theta_min'], parameters['two_theta_max']
    for index in range(6):
        value = lower + (upper - lower) * index / 5
        x = left + (right - left) * index / 5
        label = ('%.1f' % value).rstrip('0').rstrip('.')
        line(x, bottom, x, bottom + 5); text(int(x) - len(label) * 4, bottom + 13, label)
        svg.append('<text x="%s" y="440" text-anchor="middle">%s</text>' % (_f(x), label))
    for value in (0, 25, 50, 75, 100):
        y = bottom - (bottom - top) * value / 105
        line(left - 5, y, left, y); text(24, int(y) - 5, str(value))
        svg.append('<text x="51" y="%s" text-anchor="end">%d</text>' % (_f(y + 4), value))
    text(350, 455, '2θ (°)'); text(68, 8, 'I/Imax (%)')
    svg.extend(['<text x="400" y="467" text-anchor="middle">2θ (°)</text>',
                '<text x="68" y="17">I/Imax × 100</text>', '</g>', '<g stroke="#24598a" stroke-width="1.5">'])
    for peak in details['peaks']:
        x = left + (right - left) * (peak['two_theta_degree'] - lower) / (upper - lower)
        y = bottom - (bottom - top) * peak['intensity_relative_max100'] / 105
        line(x, bottom, x, y, (36, 89, 138))
        svg.append('<path d="M%s 415V%s"/>' % (_f(x), _f(y)))
    svg.append('</g></svg>\n')
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data) & 0xffffffff)
    raw = b''.join(b'\x00' + pixels[row * width * 3:(row + 1) * width * 3] for row in range(height))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
    return {'xrd.png': png, 'xrd.svg': ''.join(svg).encode('utf-8')}


def build_bundle(inputs, operation, parameters, result, files):
    parameters = validate_parameters(operation, parameters)
    bindings = input_bindings(inputs, operation)
    result = {**result, 'input_bindings': bindings,
              'methods': {**result.get('methods', {}), 'geometry_verification': dict(GEOMETRY_VERIFICATION)}}
    output_files = dict(files)
    for item, binding in zip(inputs, bindings):
        output_files[binding['archive_name']] = item['content']
    output_files['parameters.json'] = json_bytes(parameters)
    output_files['result.json'] = json_bytes(result)
    receipt = {'profile_version': PROFILE_VERSION, 'operation': operation, 'parameters': parameters,
               'input_bindings': bindings,
               'files': {name: {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                         for name, content in output_files.items()},
               'verification_scope': 'input_binding_cross_format_geometry_bragg_and_plot_consistency',
               'geometry_verification': dict(GEOMETRY_VERIFICATION),
               'independently_recomputed_intensities': False}
    output_files['receipt.json'] = json_bytes(receipt)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in sorted(output_files.items()):
            archive.writestr(name, content)
    return output.getvalue()


def periodic_sites_equal(symbols_a, positions_a, symbols_b, positions_b):
    """同元素周期位点的容差内双射；空间索引与增广路径均有固定工作上限。"""
    if (not 1 <= len(symbols_a) <= MAX_OUTPUT_ATOMS or len(symbols_a) != len(symbols_b)
            or len(positions_a) != len(symbols_a) or len(positions_b) != len(symbols_b)
            or Counter(symbols_a) != Counter(symbols_b)):
        return False
    def normalized(points):
        result = []
        for point in points:
            if len(point) != 3:
                raise ValueError('周期位点坐标维度无效。')
            result.append(tuple(_number(value) % 1.0 for value in point))
        return result
    a, b = normalized(positions_a), normalized(positions_b)
    # 完全相同或只重排的常见记录线性完成；不能以小数舍入桶决定是否相等。
    if Counter(zip(symbols_a, a)) == Counter(zip(symbols_b, b)):
        return True
    tolerance = GEOMETRY_FRACTIONAL_TOLERANCE
    divisions = max(1, int(1 / (2 * tolerance)))
    def bin_coordinate(point):
        return tuple(min(divisions - 1, int(value * divisions)) for value in point)
    buckets = defaultdict(list)
    for index, (symbol, point) in enumerate(zip(symbols_b, b)):
        buckets[(symbol, *bin_coordinate(point))].append(index)
    adjacency, candidate_checks = [], 0
    for symbol, point in zip(symbols_a, a):
        location = bin_coordinate(point)
        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    nearby = buckets.get((symbol, *((location[axis] + delta) % divisions
                                                    for axis, delta in enumerate((dx, dy, dz)))), ())
                    candidate_checks += len(nearby)
                    if candidate_checks > GEOMETRY_MAX_CANDIDATE_CHECKS:
                        raise ValueError('周期位点候选过密，超出几何复验工作预算。')
                    for index in nearby:
                        if all(min(abs(x - y), 1 - abs(x - y)) <= tolerance for x, y in zip(point, b[index])):
                            candidates.append(index)
        if not candidates:
            return False
        adjacency.append(candidates)
    size = len(a)
    left_match, right_match = [-1] * size, [-1] * size
    matching_steps = 0
    # 增广路径使有多个容差邻居时仍保证一一对应，不使用可能误配的贪心或全排列。
    for start in sorted(range(size), key=lambda index: len(adjacency[index])):
        queue, seen_left, parent_right = deque([start]), {start}, {}
        end = None
        while queue and end is None:
            left = queue.popleft()
            for right in adjacency[left]:
                matching_steps += 1
                if matching_steps > GEOMETRY_MAX_MATCHING_STEPS:
                    raise ValueError('周期位点一一匹配超出几何复验工作预算。')
                if right in parent_right:
                    continue
                parent_right[right] = left
                previous_left = right_match[right]
                if previous_left == -1:
                    end = right
                    break
                if previous_left not in seen_left:
                    seen_left.add(previous_left)
                    queue.append(previous_left)
        if end is None:
            return False
        while end != -1:
            left = parent_right[end]
            previous_right = left_match[left]
            left_match[left], right_match[end] = end, left
            end = previous_right
    return True


def _same_geometry(a, b):
    return (a['composition'] == b['composition']
            and periodic_sites_equal(a['symbols'], a['fractional_positions'], b['symbols'], b['fractional_positions'])
            and all(math.isclose(x, y, rel_tol=GEOMETRY_LATTICE_RELATIVE_TOLERANCE, abs_tol=GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE)
                    for ar, br in zip(a['lattice_matrix_angstrom'], b['lattice_matrix_angstrom']) for x, y in zip(ar, br)))


def _check_xrd(details, model, parameters):
    peaks = details.get('peaks')
    if not isinstance(peaks, list) or len(peaks) > XRD_MAX_OUTPUT_PEAKS:
        raise ValueError('理论衍射峰表无效。')
    audit = details.get('numerical_extinction_filter')
    if (not isinstance(audit, dict) or audit.get('recipe') != extinction_error_recipe(model['atom_count'])
            or audit.get('applied_relative_cutoff_percent') != 0.0
            or details.get('peak_retention_relative_to_range_maximum_percent') != 0.0):
        raise ValueError('消光残差处理方法或误差假设不一致。')
    count, considered, discarded = audit.get('checked_reflections'), audit.get('considered_peaks'), audit.get('discarded_peak_count')
    if (any(type(value) is not int or value < 0 for value in (count, considered, discarded))
            or considered != discarded + len(peaks) or considered > count
            or count > MAX_RECIPROCAL_POINTS or audit.get('discarded_peak_details_limit') != 256):
        raise ValueError('消光残差处理的反射或峰数量不一致。')
    removed = audit.get('discarded_peaks')
    if (not isinstance(removed, list) or len(removed) != min(discarded, 256)
            or audit.get('discarded_peak_details_truncated') is not (discarded > 256)):
        raise ValueError('消光残差回执数量不一致。')
    minimum, maximum_error = [_number(audit.get(key)) for key in ('minimum_amplitude_error_bound', 'maximum_amplitude_error_bound')]
    if (not 0 <= minimum <= maximum_error or (count > 0 and minimum == 0)
            or (count == 0 and maximum_error != 0)):
        raise ValueError('消光残差振幅误差估算无效。')
    def check_resolution(peak, *, removed):
        resolution = peak.get('numerical_resolution')
        if not isinstance(resolution, dict):
            raise ValueError('缺少逐峰数值分辨率回执。')
        checked, resolved = resolution.get('checked_reflections'), resolution.get('resolved_reflections')
        if (type(checked) is not int or not 1 <= checked <= count or type(resolved) is not int
                or not 0 <= resolved <= checked or (removed and resolved != 0) or (not removed and resolved == 0)):
            raise ValueError('逐峰相消判据或反射数量无效。')
        low, high = [_number(resolution.get(key), True) for key in ('minimum_amplitude_error_bound', 'maximum_amplitude_error_bound')]
        ratio = _number(resolution.get('maximum_amplitude_to_error_bound_ratio'))
        ceiling = _number(resolution.get('intensity_envelope_upper_bound_au'), True)
        if not minimum <= low <= high <= maximum_error or ratio < 0 or (removed and ratio > 1) or (not removed and ratio <= 1):
            raise ValueError('逐峰振幅误差阈值或分辨率无效。')
        if removed and _number(peak.get('intensity_raw_au'), True) > ceiling * (1 + 1e-10):
            raise ValueError('删除峰的原始强度超出回执中的强度包络估算。')
    for peak in removed:
        check_resolution(peak, removed=True)
    inverse_cell = inverse(model['lattice_matrix_angstrom'])
    maximum = max((_number(peak.get('intensity_raw_au'), True) for peak in peaks), default=0.0)
    previous = -1.0
    for peak in peaks:
        check_resolution(peak, removed=False)
        angle = _number(peak.get('two_theta_degree'), True)
        d = _number(peak.get('d_angstrom'), True)
        raw, relative = _number(peak.get('intensity_raw_au'), True), _number(peak.get('intensity_relative_max100'), True)
        if (not parameters['two_theta_min'] - 1e-7 <= angle <= parameters['two_theta_max'] + 1e-7
                or angle <= previous or not math.isclose(relative, raw / maximum * 100, rel_tol=1e-8, abs_tol=1e-7)
                or not math.isclose(2 * d * math.sin(math.radians(angle / 2)), parameters['wavelength_angstrom'], rel_tol=2e-7, abs_tol=1e-8)):
            raise ValueError('衍射角、晶面距、归一强度或布拉格关系不一致。')
        previous = angle
        families = peak.get('hkls')
        if not isinstance(families, list) or not 1 <= len(families) <= 1000:
            raise ValueError('衍射晶面指标缺失。')
        for family in families:
            hkl = family.get('hkl')
            if (not isinstance(hkl, list) or len(hkl) not in (3, 4) or not any(hkl)
                    or any(type(value) is not int or abs(value) > 10000 for value in hkl)
                    or type(family.get('multiplicity')) is not int or not 1 <= family['multiplicity'] <= 10000):
                raise ValueError('晶面指标或多重性无效。')
            if len(hkl) == 4:
                if hkl[2] != -hkl[0] - hkl[1]:
                    raise ValueError('六方四指数不满足约束。')
                hkl = [hkl[0], hkl[1], hkl[3]]
            vector = [sum(hkl[i] * inverse_cell[j][i] for i in range(3)) for j in range(3)]
            expected = 1 / math.sqrt(sum(value * value for value in vector))
            argument = parameters['wavelength_angstrom'] / (2 * expected)
            if argument > 1 + 1e-10:
                raise ValueError('晶面指标在当前波长下不满足布拉格条件。')
            family_angle = math.degrees(2 * math.asin(min(1.0, argument)))
            # 锁定版引擎按1e-5度角桶合并近简并反射，桶内各hkl的d不必完全相等。
            if abs(family_angle - angle) > XRD_TWO_THETA_TOLERANCE * 1.1:
                raise ValueError('晶面指标与原晶胞的晶面距不一致。')
    if (details.get('symprec') != 0 or details.get('intensity_model') != 'static_independent_atoms_lorentz_polarization'
            or details.get('two_theta_merge_tolerance_degree') != XRD_TWO_THETA_TOLERANCE):
        raise ValueError('理论衍射计算模型不一致。')


def _check_comparison(details, structures, parameters):
    if type(details.get('matched')) is not bool:
        raise ValueError('结构比较结果无效。')
    rms, maximum = details.get('rms_displacement_normalized'), details.get('max_displacement_normalized')
    if details['matched']:
        rms, maximum = _number(rms), _number(maximum)
        # get_rms_dist优化平均RMS，fit优化最大位移；最大值不强制使用另一配对的stol判据。
        maximum_atoms = min(model['atom_count'] for model in structures)
        if (not 0 <= rms < parameters['stol'] + 1e-8 or maximum < rms - 1e-8
                or maximum > rms * math.sqrt(maximum_atoms) + 1e-7):
            raise ValueError('结构比较的无量纲均方根或最大位移无效。')
    elif rms is not None or maximum is not None:
        raise ValueError('未匹配结构不应带有匹配位移。')


def validate_bundle(bundle, inputs, operation, parameters):
    parameters = validate_parameters(operation, parameters)
    bindings = input_bindings(inputs, operation)
    if not isinstance(bundle, bytes) or not 1 <= len(bundle) <= MAX_BUNDLE_BYTES:
        raise ValueError('晶体结果包大小无效。')
    try:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            items = archive.infolist()
            if (not 1 <= len(items) <= 32 or len({item.filename for item in items}) != len(items)
                    or any(item.is_dir() or item.file_size <= 0 or item.file_size > MAX_FILE_BYTES
                           or '/' in item.filename or '\\' in item.filename or ':' in item.filename or item.flag_bits & 1
                           or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for item in items)
                    or sum(item.file_size for item in items) > MAX_UNPACKED_BYTES):
                raise ValueError('结果包目录或大小无效。')
            files = {item.filename: archive.read(item) for item in items}
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ValueError('结果包无法完整读取。') from exc
    required = {'receipt.json', 'parameters.json', 'result.json', '报告.txt'} | {item['archive_name'] for item in bindings}
    prefixes = ('a-', 'b-') if operation == 'compare' else ('',)
    for prefix in prefixes:
        required |= {prefix + suffix for suffix in ('structure.cif', 'structure.poscar', 'structure.json', 'sites.csv')}
    if operation == 'xrd':
        required |= {'xrd.json', 'xrd.csv', 'xrd.png', 'xrd.svg'}
    if set(files) != required:
        raise ValueError('结果文件集合不完整或包含未许可文件。')
    receipt, result = _load(files['receipt.json']), _load(files['result.json'])
    for value in (receipt, result):
        if (not isinstance(value, dict) or value.get('profile_version') != PROFILE_VERSION
                or value.get('operation') != operation or json_bytes(value.get('parameters')) != json_bytes(parameters)
                or value.get('input_bindings') != bindings):
            raise ValueError('结果输入绑定、操作或参数不一致。')
        json_bytes(value)
    if (receipt.get('independently_recomputed_intensities') is not False
            or receipt.get('verification_scope') != 'input_binding_cross_format_geometry_bragg_and_plot_consistency'):
        raise ValueError('结果复验范围声明无效。')
    if (receipt.get('geometry_verification') != GEOMETRY_VERIFICATION
            or result.get('methods', {}).get('geometry_verification') != GEOMETRY_VERIFICATION):
        raise ValueError('几何复验方法或容差记录不一致。')
    expected_digests = {name: {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                        for name, content in files.items() if name != 'receipt.json'}
    if receipt.get('files') != expected_digests or files['parameters.json'] != json_bytes(parameters):
        raise ValueError('结果文件摘要或参数不一致。')
    for item, binding in zip(inputs, bindings):
        if files[binding['archive_name']] != item['content']:
            raise ValueError('原始结构文件未被原样保存。')
    versions = result.get('versions', {})
    if versions.get('pymatgen') != PYMATGEN_VERSION or versions.get('pymatgen-core') != PYMATGEN_CORE_VERSION:
        raise ValueError('计算环境版本未通过绑定核验。')
    originals, structures, details = result.get('input_structures'), result.get('structures'), result.get('details')
    if (not isinstance(originals, list) or len(originals) != len(inputs) or not isinstance(structures, list)
            or len(structures) != len(inputs) or not isinstance(details, dict)):
        raise ValueError('结构模型数量不一致。')
    for original in originals:
        check_structure(original, MAX_INPUT_ATOMS)
    for model, prefix in zip(structures, prefixes):
        check_structure(model)
        for name, content in structure_files(model, prefix).items():
            if files[name] != content:
                raise ValueError('结构JSON、CSV、CIF或POSCAR交叉复验失败。')
    if operation == 'supercell':
        scaling, source, target = parameters['scaling'], originals[0], structures[0]
        factor = math.prod(scaling)
        if (details.get('scaling') != scaling or target['composition'] != {key: value * factor for key, value in source['composition'].items()}
                or not math.isclose(target['volume_angstrom3'], source['volume_angstrom3'] * factor, rel_tol=1e-8)
                or any(not math.isclose(b, a * scaling[index], rel_tol=GEOMETRY_LATTICE_RELATIVE_TOLERANCE,
                                        abs_tol=GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE)
                       for index, (ar, br) in enumerate(zip(source['lattice_matrix_angstrom'], target['lattice_matrix_angstrom'])) for a, b in zip(ar, br))):
            raise ValueError('超胞体积、晶胞或元素数复验失败。')
        expected_symbols, expected_positions = [], []
        for symbol, point in zip(source['symbols'], source['fractional_positions']):
            for i in range(scaling[0]):
                for j in range(scaling[1]):
                    for k in range(scaling[2]):
                        delta = (i, j, k)
                        expected_symbols.append(symbol)
                        expected_positions.append([(point[axis] + delta[axis]) / scaling[axis] for axis in range(3)])
        if not periodic_sites_equal(expected_symbols, expected_positions, target['symbols'], target['fractional_positions']):
            raise ValueError('超胞坐标复验失败。')
    elif any(not _same_geometry(a, b) for a, b in zip(originals, structures)):
        raise ValueError('导出几何与原始展开结构不一致。')
    if operation == 'xrd':
        _check_xrd(details, structures[0], parameters)
        expected = {'xrd.json': json_bytes(details), 'xrd.csv': xrd_csv(details), **xrd_graphs(details, parameters)}
        if any(files[name] != content for name, content in expected.items()):
            raise ValueError('衍射峰表JSON、CSV或数值图不一致。')
    elif operation == 'compare':
        _check_comparison(details, structures, parameters)
    elif operation == 'analyze':
        sg = details.get('spacegroup', {})
        if (type(sg.get('number')) is not int or not 1 <= sg['number'] <= 230
                or not isinstance(sg.get('symbol'), str) or not isinstance(sg.get('crystal_system'), str)):
            raise ValueError('计算空间群信息无效。')
    if details.get('roundtrip_verified') is not True:
        raise ValueError('转换结构未经科学角色往返核验。')
    if result.get('summary') != make_summary(operation, structures, details) or files['报告.txt'] != report_text(result):
        raise ValueError('结果摘要或中文报告与科学数据不一致。')
    return {'files': files, 'summary': result['summary'], 'receipt': receipt, 'result': result}
