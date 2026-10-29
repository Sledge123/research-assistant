"""网站和科学角色共用的纯标准库输入与参数约束。"""
import copy
import math
from pathlib import PurePosixPath
import re
import sys

PROFILE_VERSION = 'pymatgen-cpu-v1'
PYMATGEN_VERSION = '2026.9.24'
PYMATGEN_CORE_VERSION = '2026.10.16'
MAX_INPUT_BYTES = 512 * 1024
MAX_CIF_BYTES = MAX_INPUT_BYTES
MAX_INPUT_ATOMS = 512
MAX_OUTPUT_ATOMS = 4096
MAX_RUN_SECONDS = 120
MAX_MEMORY_BYTES = 640 * 1024 * 1024
MAX_RECIPROCAL_POINTS = 60000
MAX_SCATTERING_WORK = 2000000
XRD_TWO_THETA_TOLERANCE = 1e-5
XRD_MAX_OUTPUT_PEAKS = 5000
EXTINCTION_ALGORITHM = 'float64_structure_factor_resolution_v1'
GEOMETRY_FRACTIONAL_TOLERANCE = 1e-10
GEOMETRY_LATTICE_RELATIVE_TOLERANCE = 1e-12
GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE = 1e-10
GEOMETRY_METRIC_ABSOLUTE_TOLERANCE = 1e-8
GEOMETRY_MAX_CANDIDATE_CHECKS = MAX_OUTPUT_ATOMS * 64
GEOMETRY_MAX_MATCHING_STEPS = GEOMETRY_MAX_CANDIDATE_CHECKS * 8
GEOMETRY_VERIFICATION = {
    'method': 'element_preserving_periodic_coordinate_bijection',
    'coordinate_distance': 'maximum_component_of_minimum_image_fractional_delta',
    'fractional_coordinate_absolute_tolerance': GEOMETRY_FRACTIONAL_TOLERANCE,
    'lattice_matrix_relative_tolerance': GEOMETRY_LATTICE_RELATIVE_TOLERANCE,
    'lattice_metric_relative_tolerance': GEOMETRY_LATTICE_RELATIVE_TOLERANCE,
    'lattice_matrix_absolute_tolerance_angstrom': GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE,
    'lattice_metric_absolute_tolerance_angstrom2': GEOMETRY_METRIC_ABSOLUTE_TOLERANCE,
    'cif_lattice_comparison': 'metric_tensor_rotation_invariant',
    'derived_cif_parser': 'full_P1_coordinate_list_without_reexpansion_or_decimal_quantization',
    'poscar_lattice_comparison': 'matrix_preserving_cartesian_orientation',
    'candidate_check_limit': GEOMETRY_MAX_CANDIDATE_CHECKS,
    'matching_step_limit': GEOMETRY_MAX_MATCHING_STEPS}
RETENTION_SECONDS = 3 * 86400
OPERATIONS = ('analyze', 'convert', 'xrd', 'supercell', 'compare')
OPERATION_LABELS = {'analyze': '结构分析', 'convert': '格式转换', 'xrd': '理论XRD',
                    'supercell': '生成超胞', 'compare': '结构比较'}
DEFAULT_PARAMETERS = {
    'analyze': {'symprec': 0.01, 'angle_tolerance': 5.0}, 'convert': {},
    'xrd': {'wavelength_angstrom': 1.54056, 'two_theta_min': 5.0, 'two_theta_max': 90.0},
    'supercell': {'scaling': [2, 2, 2]},
    'compare': {'ltol': 0.2, 'stol': 0.3, 'angle_tol': 5.0,
                'primitive_cell': True, 'scale': False, 'allow_subset': False}}
PARAMETER_RANGES = {'symprec': [0.0001, 0.1], 'angle_tolerance': [0.1, 10.0],
                    'wavelength_angstrom': [0.1, 5.0], 'two_theta_min': [0.1, 170.0],
                    'two_theta_max': [0.1, 170.0], 'ltol': [0.01, 0.5],
                    'stol': [0.01, 0.8], 'angle_tol': [0.1, 10.0]}
LIMITS = {'profile_version': PROFILE_VERSION, 'max_input_bytes': MAX_INPUT_BYTES,
          'max_input_atoms': MAX_INPUT_ATOMS, 'max_output_atoms': MAX_OUTPUT_ATOMS,
          'max_run_seconds': MAX_RUN_SECONDS, 'retention_days': 3,
          'operations': list(OPERATIONS), 'defaults': DEFAULT_PARAMETERS,
          'parameter_ranges': PARAMETER_RANGES, 'scaling_component': [1, 4], 'cost_points': 0}
SYMBOLS = set(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
               'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce '
               'Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn '
               'Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split())


def extinction_error_recipe(atom_count):
    """数值分辨率的可追溯假设，不把误差估算称为区间算术认证。"""
    if type(atom_count) is not int or not 1 <= atom_count <= MAX_INPUT_ATOMS:
        raise ValueError('消光误差分析的原子数量无效。')
    return {'algorithm': EXTINCTION_ALGORITHM, 'precision': 'IEEE754_binary64',
            'unit_roundoff': sys.float_info.epsilon / 2, 'atom_count': atom_count,
            'gamma_definition': 'gamma_n=n*u/(1-n*u)', 'dot_product_gamma_n': 5,
            'phase_multiplication_gamma_n': 3, 'summation_gamma_n': 2 * atom_count + 2,
            'scattering_factor_gamma_n': '16+8*number_of_scattering_coefficients',
            'trigonometric_absolute_error_assumption_in_units_u': 8,
            'amplitude_bound': '(sum(abs(f_j)*(sqrt(2)*phase_error_j+2*u)+factor_error_j)+sqrt(2)*gamma_(2*N+2)*sum(abs(f_j)))/(1-gamma_(2*N+2))',
            'phase_error_bound': '2*pi*(gamma_5+gamma_3)/(1-gamma_5-gamma_3)*sum(abs(h_i*r_ji))+2*pi*u+8*u',
            'intensity_envelope_upper_bound': 'sum_halfspace(2*LP*(abs(F_recomputed)+2*amplitude_bound)^2)',
            'decision': 'discard_only_if_every_actual_reflection_in_same_engine_angular_bin_np_round_angle_over_tolerance_has_abs_F_le_bound',
            'phase_reduction': 'modulo_one_cycle_before_diagnostic_trigonometry',
            'summation': 'math_fsum_of_weighted_real_and_imaginary_terms',
            'classification': 'numerically_unresolved_cancellation',
            'interval_arithmetic_certification': False,
            'coordinate_and_physical_model_uncertainty_included': False,
            'error_analysis_reference': 'https://nhigham.com/wp-content/uploads/2023/10/high93s.pdf',
            'floating_point_reference': 'https://numpy.org/doc/stable/reference/generated/numpy.finfo.html'}


def validate_parameters(operation, value):
    if operation not in OPERATIONS:
        raise ValueError('请选择受支持的晶体工具。')
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PARAMETERS[operation]):
        raise ValueError('工具参数包含不支持的字段。')
    result = copy.deepcopy(DEFAULT_PARAMETERS[operation])
    for name in result:
        item = value.get(name, result[name])
        if name == 'scaling':
            if (not isinstance(item, (list, tuple)) or len(item) != 3
                    or any(type(component) is not int or not 1 <= component <= 4 for component in item)):
                raise ValueError('超胞三个分量须为1到4的整数。')
            result[name] = list(item)
        elif name in ('primitive_cell', 'scale', 'allow_subset'):
            if type(item) is not bool or item != result[name]:
                raise ValueError('结构比较固定使用原胞、保留实际尺寸并禁止子集匹配。')
        else:
            if type(item) not in (int, float) or not math.isfinite(item):
                raise ValueError('工具参数须为有限数值。')
            lower, upper = PARAMETER_RANGES[name]
            if not lower <= item <= upper:
                raise ValueError('工具参数超出当前支持范围。')
            result[name] = float(item)
    if operation == 'xrd' and result['two_theta_min'] >= result['two_theta_max']:
        raise ValueError('衍射角起点须小于终点。')
    return result


def decode_input(content):
    if not isinstance(content, bytes) or not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError('每个结构文件须非空且不超过512 KiB。')
    if b'\x00' in content or any(byte < 32 and byte not in (9, 10, 13) for byte in content):
        raise ValueError('结构文件包含无效控制字符。')
    try:
        return content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return content.decode('latin-1')


def poscar_header(text, max_atoms=MAX_INPUT_ATOMS):
    lines = text.strip().splitlines()
    if len(lines) < 9:
        raise ValueError('POSCAR结构不完整。')
    names = lines[5].split()
    if not names or any(name not in SYMBOLS for name in names):
        raise ValueError('POSCAR须在第六行明确给出有效元素，不能猜测元素。')
    counts = lines[6].split()
    if len(counts) != len(names) or any(not re.fullmatch(r'\d+', item) for item in counts):
        raise ValueError('POSCAR元素数量格式无效。')
    counts = [int(item) for item in counts]
    if any(item <= 0 for item in counts) or not 1 <= sum(counts) <= max_atoms:
        raise ValueError('POSCAR原子数超出当前支持范围。')
    start = 7
    if lines[start].lstrip().casefold().startswith('s'):
        start += 1
    if start >= len(lines) or not lines[start].strip().casefold().startswith(('d', 'c', 'k')):
        raise ValueError('POSCAR须明确使用分数坐标或笛卡尔坐标。')
    if len(lines) < start + 1 + sum(counts):
        raise ValueError('POSCAR坐标数量不完整。')
    return names, counts


def validate_input_bytes(content, filename):
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 180 or filename.strip() != filename
            or filename in ('.', '..') or any(ord(char) < 32 for char in filename)
            or any(char in filename for char in ('/', '\\', ':'))):
        raise ValueError('结构文件名无效。')
    text = decode_input(content)
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix == '.cif':
        if not re.search(r'(?im)^\s*data_\S*', text):
            raise ValueError('文件不是有效的CIF结构文件。')
        return 'cif'
    if suffix in ('.poscar', '.vasp') or filename.upper() in ('POSCAR', 'CONTCAR'):
        poscar_header(text)
        return 'poscar'
    raise ValueError('仅支持CIF或明确包含元素的POSCAR文件。')


def validate_inputs(inputs, operation):
    if operation not in OPERATIONS:
        raise ValueError('请选择受支持的晶体工具。')
    if not isinstance(inputs, (list, tuple)) or len(inputs) != (2 if operation == 'compare' else 1):
        raise ValueError('结构比较需要两个文件，其他工具需要一个文件。')
    result = []
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {'filename', 'content'}:
            raise ValueError('结构输入字段无效。')
        validate_input_bytes(item['content'], item['filename'])
        result.append({'filename': item['filename'], 'content': item['content']})
    return result
