import base64
from fractions import Fraction
import hashlib
import itertools
import json
import struct


_IDENTITY = (1, 0, 0, 0, 1, 0, 0, 0, 1)


def _matrix_product(left, right):
    return tuple(
        sum(left[i * 3 + k] * right[k * 3 + j] for k in range(3))
        for i in range(3) for j in range(3)
    )


def _generated_rotations(*generators):
    """由少量自行定义的整数矩阵生成有限闭合群。"""
    seen, pending = {_IDENTITY}, [_IDENTITY]
    while pending:
        current = pending.pop()
        for generator in generators:
            product = _matrix_product(current, generator)
            if product not in seen:
                seen.add(product)
                pending.append(product)
    return sorted(seen)


def _encode_setting(symbol, number, matrices, multiplicity, *,
                    inversion=0, axis=0, origin=(0, 0, 0),
                    translations=None, setting='synthetic'):
    """手工编码已知布局；平移和原点采用1/24分数坐标单位。

    origin仅用于完整列出的操作集；inversion=1时本夹具固定原点0。
    translations可显式给出螺旋/滑移平移，不借用任何原表字节。
    """
    if len(symbol.encode('ascii')) > 8 or len(setting.encode('ascii')) > 16:
        raise ValueError('合成setting文本超出固定字段宽度')
    if inversion and any(origin):
        raise ValueError('带自动反演的合成例固定采用原点0')
    rows = []
    for index, rotation in enumerate(matrices):
        if translations is None:
            translation = tuple(
                (origin[i] - sum(rotation[i * 3 + j] * origin[j]
                                 for j in range(3))) % 24
                for i in range(3)
            )
        else:
            translation = translations[index]
        row = []
        for i in range(3):
            fraction = Fraction(translation[i] % 24, 24)
            packed = 0 if not fraction else (
                (fraction.denominator << 8) | fraction.numerator
            )
            row.extend((packed, *rotation[i * 3:i * 3 + 3]))
        rows.append(struct.pack('<12h', *row))
    values = [0] * 24
    # source中的SYM记录从offset2开始，文件指针为一基3。
    values[0] = 3
    values[4] = axis
    values[5] = multiplicity
    values[7] = inversion
    values[8] = number
    values[9] = len(rows) - 1
    sgc = (struct.pack('<24h', *values)
           + symbol.encode('ascii').ljust(8, b' ')
           + setting.encode('ascii').ljust(16, b' '))
    sym = struct.pack('<h', len(rows) - 1) + b''.join(rows)
    return sgc, sym


def _synthetic_structure_parts():
    """9个自选Si位点；P21/c、满占有、显式螺旋和反演。"""
    sgc, sym = _encode_setting(
        'P21/c', 14,
        [_IDENTITY, (-1, 0, 0, 0, 1, 0, 0, 0, -1)],
        4, inversion=1, setting='b-unique',
        translations=[(0, 0, 0), (0, 12, 12)],
    )
    csd = bytearray(124)
    struct.pack_into('<4I', csd, 0, 1, 1025, 1025, 9)
    struct.pack_into('<H', csd, 16, 0)
    struct.pack_into('<6f', csd, 40, 4, 5, 6, 90, 100, 90)
    csa = bytearray(struct.pack('<IHH', 1, 1, 9))
    for index in range(9):
        atom = bytearray(108)
        struct.pack_into('<HhHHf', atom, 0, 14, 0, 0, 4, 1)
        struct.pack_into('<3f', atom, 32,
                         (index + 1) / 16,
                         (index + 2) / 17,
                         (index + 3) / 19)
        atom[104:108] = ('Si' + str(index + 1)).encode('ascii').ljust(4, b'\0')
        csa.extend(atom)
    csc = bytearray(struct.pack('<I', 1))
    for text in ('Synthetic nine-site structure', 'Si36',
                 'Synthetic reference', 'Synthetic comment'):
        encoded = text.encode('ascii')
        csc.extend(struct.pack('<H', len(text))
                   + encoded + b'\0' + struct.pack('<I', 1))
    return {'csd': bytes(csd), 'csa': bytes(csa), 'csc': bytes(csc),
            'sgc': sgc, 'sym': sym}


def _synthetic_settings():
    # 旧数字键仅保留测试调用兼容；与任何原数据库行位置无关。
    cubic = []
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            cubic.append(tuple(signs[i] if j == permutation[i] else 0
                               for i in range(3) for j in range(3)))
    c3 = (0, -1, 0, 1, -1, 0, 0, 0, 1)
    c2 = (1, -1, 0, 0, -1, 0, 0, 0, -1)
    pairs = {
        '19': _encode_setting(
            'F2', 5, [_IDENTITY, (-1, 0, 0, 0, -1, 0, 0, 0, 1)],
            8, axis=1, setting='synthetic axis'),
        '151': _encode_setting(
            'P222', 16,
            _generated_rotations((1, 0, 0, 0, -1, 0, 0, 0, -1),
                                 (-1, 0, 0, 0, 1, 0, 0, 0, -1)),
            4),
        '537': _encode_setting(
            'R-3m', 166, _generated_rotations(c3, c2),
            36, inversion=1, setting='1/3,2/3,2/3'),
        '620': _encode_setting(
            'Fm-3m', 225, cubic, 192, setting='origin 0'),
        '622': _encode_setting(
            'Fm-3m', 225, cubic, 192,
            origin=(3, 3, 3), setting='origin 1/8'),
    }
    return {
        key: {'sgc': base64.b64encode(sgc).decode('ascii'),
              'sym': base64.b64encode(sym).decode('ascii')}
        for key, (sgc, sym) in pairs.items()
    }


_SYNTHETIC_PARTS = _synthetic_structure_parts()
FIXTURE = {key + '_base64': base64.b64encode(raw).decode('ascii')
           for key, raw in _SYNTHETIC_PARTS.items()}
SETTING_FIXTURES = _synthetic_settings()


def fixture_source(root):
    root.mkdir(parents=True)
    header = bytearray(1024)
    header[:4] = b'jcsd'
    struct.pack_into('<III', header, 4, 1, 1, 3)
    header[26:30] = b'TEST'
    for extension in ('csa', 'csc', 'csd'):
        content = _SYNTHETIC_PARTS[extension]
        ending = header[26:30] if extension == 'csd' else b''
        (root / ('ICSD-All.' + extension)).write_bytes(header + content + ending)
    (root / 'ICSD-All.csb').write_bytes(header + b'\0' * 8)
    # 一个SGC条目，jade_j=0；SYM指针3对应offset2。
    (root / 'jade9.SGC').write_bytes(b'J6\0\0' + _SYNTHETIC_PARTS['sgc'])
    (root / 'jade9.SYM').write_bytes(b'J6' + _SYNTHETIC_PARTS['sym'])
    files = [
        {'name': path.name, 'bytes': path.stat().st_size,
         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
         'source_relative_path': './fixture/' + path.name}
        for path in sorted(root.iterdir())
    ]
    (root / 'manifest.json').write_text(
        json.dumps({'type': 'JADE_STRUCTURE_SNAPSHOT', 'files': files}),
        encoding='utf8')
    return root
