"""网站与计算子进程共用的参数和规模约束。"""
import math
import re

PROFILE_VERSION = 'abtem-cpu-v1'
ABTEM_VERSION = '1.0.10'
MAX_CIF_BYTES = 512 * 1024
MAX_GRID = 256
MAX_ATOMS = 12000
MAX_LATERAL_ANGSTROM = 25.6
MAX_RUN_SECONDS = 180
RETENTION_SECONDS = 3 * 86400
DEFAULT_PARAMETERS = {'energy_kev': 200.0, 'zone_axis': [0, 0, 1],
                      'thickness_nm': 2.0, 'defocus_nm': 10.0,
                      'cs_mm': 1.0, 'aperture_mrad': 20.0}
LIMITS = {'max_cif_bytes': MAX_CIF_BYTES, 'energy_kev': [80, 300],
          'zone_axis_component': [-3, 3], 'thickness_nm': [0.5, 10],
          'defocus_nm': [-200, 200], 'cs_mm': [0, 3], 'aperture_mrad': [5, 50],
          'max_atoms': MAX_ATOMS, 'max_grid': MAX_GRID,
          'max_lateral_angstrom': MAX_LATERAL_ANGSTROM,
          'max_run_seconds': MAX_RUN_SECONDS, 'retention_days': 3,
          'profile_version': PROFILE_VERSION}


def validate_parameters(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PARAMETERS):
        raise ValueError('模拟参数包含不支持的字段。')
    result = {}
    for name, default in DEFAULT_PARAMETERS.items():
        item = value.get(name, default)
        if name == 'zone_axis':
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise ValueError('晶带轴须填写三个整数。')
            if any(type(component) is not int or not -3 <= component <= 3 for component in item) or not any(item):
                raise ValueError('晶带轴分量范围为-3到3，且不能全为0。')
            divisor = math.gcd(math.gcd(abs(item[0]), abs(item[1])), abs(item[2]))
            result[name] = [component // divisor for component in item]
        else:
            if type(item) not in (int, float) or not math.isfinite(item):
                raise ValueError('模拟参数须为有限数值。')
            lower, upper = LIMITS[name]
            if not lower <= item <= upper:
                raise ValueError('模拟参数超出当前支持范围。')
            result[name] = float(item)
    return result


def validate_cif_bytes(content):
    if not isinstance(content, bytes) or not content or len(content) > MAX_CIF_BYTES:
        raise ValueError('请提供不超过512 KiB的CIF文件。')
    if b'\x00' in content or any(byte < 32 and byte not in (9, 10, 13) for byte in content):
        raise ValueError('CIF文件包含无效控制字符。')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content.decode('latin-1')
    if not re.search(r'(?im)^\s*data_\S*', text):
        raise ValueError('文件不是有效的CIF结构文件。')
    return text
