"""独立科学角色的晶体工具；所有模型来源、几何及导出均可追溯。"""
from collections import Counter
import importlib.metadata
import io
import json
import math
from pathlib import Path
import time

from .contract import (MAX_INPUT_ATOMS, MAX_OUTPUT_ATOMS, MAX_RECIPROCAL_POINTS,
                       MAX_SCATTERING_WORK, PROFILE_VERSION, PYMATGEN_CORE_VERSION,
                       PYMATGEN_VERSION, XRD_MAX_OUTPUT_PEAKS, XRD_TWO_THETA_TOLERANCE,
                       GEOMETRY_LATTICE_RELATIVE_TOLERANCE, GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE,
                       GEOMETRY_METRIC_ABSOLUTE_TOLERANCE, GEOMETRY_VERIFICATION,
                       decode_input, extinction_error_recipe, poscar_header,
                       validate_input_bytes, validate_inputs, validate_parameters)


class ToolError(ValueError):
    def __init__(self, message, code='unsupported_structure'):
        super().__init__(message)
        self.code = code


def _progress(path, phase, progress):
    if path is not None:
        target = Path(path)
        temporary = target.with_suffix('.new')
        temporary.write_text(json.dumps({'phase': phase, 'progress': progress}), encoding='utf-8')
        temporary.replace(target)


def _version_check():
    for name, expected in (('pymatgen', PYMATGEN_VERSION), ('pymatgen-core', PYMATGEN_CORE_VERSION)):
        if importlib.metadata.version(name) != expected:
            raise ToolError('计算环境版本未通过绑定核验。', 'environment')


def _scale_check(structure, max_atoms=MAX_INPUT_ATOMS):
    import numpy as np
    matrix = np.asarray(structure.lattice.matrix, dtype=float)
    if (not 1 <= len(structure) <= max_atoms or not structure.is_ordered
            or not np.isfinite(matrix).all() or not np.isfinite(structure.frac_coords).all()):
        raise ToolError('仅支持规模受限的有序满占有结构。', 'unsupported_scale')
    singular = np.linalg.svd(matrix, compute_uv=False)
    if (min(singular) < 0.1 or max(singular) / min(singular) > 100 or structure.volume < 0.1
            or max(structure.lattice.abc) > (800 if max_atoms == MAX_OUTPUT_ATOMS else 200)):
        raise ToolError('晶胞尺寸或倾斜程度超出当前计算范围。', 'unsupported_scale')


def load_structure(content, filename):
    import numpy as np
    from pymatgen.core import Structure
    kind = validate_input_bytes(content, filename)
    if kind == 'cif':
        from highscore_crystal.cif import CrystalError, load_cif
        try:
            atoms, source = load_cif(content, max_atoms=MAX_INPUT_ATOMS, max_raw_sites=MAX_INPUT_ATOMS)
        except CrystalError as exc:
            raise ToolError(str(exc), exc.code) from None
        structure = Structure(np.asarray(atoms.cell), atoms.get_chemical_symbols(),
                              atoms.get_scaled_positions(wrap=False), to_unit_cell=False)
    else:
        from pymatgen.io.vasp.inputs import Poscar
        text = decode_input(content)
        names, counts = poscar_header(text)
        try:
            parsed = Poscar.from_str(text, default_names=names, read_velocities=False)
            structure = parsed.structure
        except (ValueError, IndexError, TypeError) as exc:
            raise ToolError('POSCAR无法完整解析：' + str(exc)[:200]) from None
        if dict(Counter(site.specie.symbol for site in structure)) != dict(Counter({name: sum(count for actual, count in zip(names, counts) if actual == name) for name in names})):
            raise ToolError('POSCAR解析后的元素数量与明确声明不一致。')
        source = {'format': 'poscar', 'input_atom_count': len(structure), 'explicit_elements': names,
                  'explicit_counts': counts, 'original_fields': {}, 'source_spacegroup': None,
                  'symmetry_expansion': {'method': 'explicit_full_coordinate_list', 'operation_count': 1}}
        lines = text.strip().splitlines()
        position_start = 8 + int(lines[7].lstrip().casefold().startswith('s'))
        source['non_geometric_attributes'] = {
            'parsed_site_property_names': sorted(structure.site_properties),
            'selective_dynamics_present': parsed.selective_dynamics is not None,
            'selective_dynamics_applied': False, 'velocities_parsed': False, 'velocities_applied': False,
            'nonempty_trailing_lines_after_coordinates': sum(bool(line.strip()) for line in lines[position_start + sum(counts):]),
            'velocity_or_other_trailing_section_identity': 'not_parsed',
            'other_source_properties_applied': False, 'retention': 'original_input_bytes_only'}
    source['conversion_scope'] = ['elements', 'lattice', 'periodic_fractional_coordinates']
    source['non_geometric_source_properties_transferred_to_derived_structure'] = False
    _scale_check(structure)
    frac = np.asarray(structure.frac_coords, dtype=float)
    if np.max(np.abs(frac)) > 1000000:
        raise ToolError('分数坐标幅度超出可靠周期约化范围。')
    matrix = np.asarray(structure.lattice.matrix, dtype=float).copy()
    if np.linalg.det(matrix) < 0:
        source['original_lattice_matrix_angstrom'] = matrix.tolist()
        source['basis_handedness_transform'] = [-1, 1, 1]
        matrix[0] *= -1
        frac[:, 0] *= -1
    frac %= 1
    frac[np.abs(frac - 1) < 1e-12] = 0
    frac[np.abs(frac) < 1e-12] = 0
    structure = Structure(matrix, [site.specie.symbol for site in structure], frac, to_unit_cell=False)
    # 明确拒绝跨周期边界的重叠原子；不自动合并或更改占有率。
    if len(structure) > 1:
        distances = structure.distance_matrix
        np.fill_diagonal(distances, np.inf)
        if np.min(distances) < 1e-5:
            raise ToolError('结构存在重叠位点，无法确认有序满占有模型。', 'unsupported_disorder')
    return structure, source


def structure_record(structure, source, name='structure'):
    import numpy as np
    fractional = np.mod(np.asarray(structure.frac_coords, dtype=float), 1)
    fractional[np.abs(fractional - 1) < 1e-12] = 0
    fractional[np.abs(fractional) < 1e-12] = 0
    symbols = [site.specie.symbol for site in structure]
    return {'name': name, 'lattice_matrix_angstrom': structure.lattice.matrix.tolist(),
            'lattice_parameters': list(structure.lattice.abc) + list(structure.lattice.angles),
            'volume_angstrom3': float(structure.volume), 'symbols': symbols,
            'fractional_positions': fractional.tolist(), 'atom_count': len(structure),
            'composition': dict(Counter(symbols)), 'formula': structure.composition.reduced_formula,
            'occupancy': 'full', 'source': source}


def _verify_roundtrip(original, files, prefix=''):
    """CIF保留晶胞度量及分数坐标，POSCAR另保留笛卡尔晶胞方向。"""
    import numpy as np
    from ase.io.cif import parse_cif
    from pymatgen.core import Structure
    from pymatgen.io.vasp.inputs import Poscar
    from .artifacts import periodic_sites_equal
    # 导出CIF是完整P1坐标列表，无须再次展开；公共输入展开的去重尺度不应用于格式往返。
    blocks = [block for block in parse_cif(io.StringIO(files[prefix + 'structure.cif'].decode('utf-8'))) if block.has_structure()]
    if (len(blocks) != 1 or blocks[0].get('_space_group_it_number') != 1
            or blocks[0].get('_space_group_symop_operation_xyz') != ['x,y,z']):
        raise ToolError('派生CIF未保留完整P1坐标列表。', 'verification')
    atoms = blocks[0].get_unsymmetrized_structure()
    cif = Structure(np.asarray(atoms.cell), atoms.get_chemical_symbols(), atoms.get_scaled_positions())
    poscar = Poscar.from_str(files[prefix + 'structure.poscar'].decode('utf-8'), read_velocities=False).structure
    original_symbols = [site.specie.symbol for site in original]
    original_positions = original.frac_coords.tolist()
    metric = original.lattice.matrix @ original.lattice.matrix.T
    for candidate in (cif, poscar):
        if (not np.allclose(candidate.lattice.matrix @ candidate.lattice.matrix.T, metric,
                            rtol=GEOMETRY_LATTICE_RELATIVE_TOLERANCE, atol=GEOMETRY_METRIC_ABSOLUTE_TOLERANCE)
                or not periodic_sites_equal(original_symbols, original_positions,
                                            [site.specie.symbol for site in candidate], candidate.frac_coords.tolist())):
            raise ToolError('转换文件重新解析后的元素、晶胞或周期坐标不一致。', 'verification')
    if not np.allclose(poscar.lattice.matrix, original.lattice.matrix,
                       rtol=GEOMETRY_LATTICE_RELATIVE_TOLERANCE, atol=GEOMETRY_LATTICE_ABSOLUTE_TOLERANCE):
        raise ToolError('POSCAR转换未保留原晶胞方向。', 'verification')


def reciprocal_cost_bound(structure, parameters):
    """h_i=g·a_i，按三方向Cauchy界包住球，枚举前拒绝过大分配。"""
    radius = 2 * math.sin(math.radians(parameters['two_theta_max'] / 2)) / parameters['wavelength_angstrom']
    bounds = [int(math.ceil(radius * length)) + 2 for length in structure.lattice.abc]
    count = math.prod(2 * bound + 1 for bound in bounds)
    work = count * len(structure)
    if count > MAX_RECIPROCAL_POINTS or work > MAX_SCATTERING_WORK:
        raise ToolError('倒易空间枚举规模超出当前计算预算，请减小晶胞或衍射角范围。', 'unsupported_scale')
    return {'maximum_reciprocal_radius_inverse_angstrom': radius, 'hkl_absolute_bounds': bounds,
            'bounding_box_points': count, 'atom_point_work_upper_bound': work}


def _comparison_budget(a, b, parameters):
    import numpy as np
    maximum = max(max(a.lattice.abc), max(b.lattice.abc)) * (1 + parameters['ltol'])
    for model in (a, b):
        reciprocal = np.linalg.inv(model.lattice.matrix).T
        bound = math.prod(2 * (int(math.ceil(maximum * float(np.linalg.norm(row)))) + 1) + 1 for row in reciprocal)
        if bound > 20000 or bound * max(len(a), len(b)) > 2000000:
            raise ToolError('结构比较的晶格候选规模超出当前计算预算。', 'unsupported_scale')


def _xrd(structure, parameters):
    import numpy as np
    from pymatgen.analysis.diffraction.core import AbstractDiffractionPatternCalculator
    from pymatgen.analysis.diffraction.xrd import XRDCalculator
    budget = reciprocal_cost_bound(structure, parameters)
    hkl, angles, reciprocal_lengths = _reciprocal_points(structure, parameters, budget)
    peaks = []
    previous_cutoff = AbstractDiffractionPatternCalculator.SCALED_INTENSITY_TOL
    audit = {'recipe': extinction_error_recipe(len(structure)), 'checked_reflections': int(len(hkl)),
             'original_engine_relative_cutoff_percent': float(previous_cutoff),
             'applied_relative_cutoff_percent': 0.0, 'considered_peaks': 0, 'discarded_peak_count': 0,
             'angular_grouping': 'numpy_round_two_theta_divided_by_engine_tolerance',
             'reciprocal_point_source': 'pymatgen_get_points_in_sphere',
             'discarded_peak_details_limit': 256, 'discarded_peak_details_truncated': False,
             'discarded_peaks': [], 'minimum_amplitude_error_bound': 0.0, 'maximum_amplitude_error_bound': 0.0}
    if len(hkl):
        calculator = XRDCalculator(wavelength=parameters['wavelength_angstrom'], symprec=0)
        # 子进程每次只处理一项任务且只有一个计算线程。关闭相对显示截断，
        # 先剔除有误差分析依据的相消残差，再归一化，避免噪声max吞掉真实弱峰。
        try:
            AbstractDiffractionPatternCalculator.SCALED_INTENSITY_TOL = 0.0
            pattern = calculator.get_pattern(structure, scaled=False,
                                             two_theta_range=(parameters['two_theta_min'], parameters['two_theta_max']))
        finally:
            AbstractDiffractionPatternCalculator.SCALED_INTENSITY_TOL = previous_cutoff
        amplitudes, error_bounds = _structure_factor_resolution(structure, hkl, reciprocal_lengths)
        theta = np.radians(angles / 2)
        lorentz = (1 + np.cos(2 * theta) ** 2) / (np.sin(theta) ** 2 * np.cos(theta))
        audit.update({'considered_peaks': len(pattern.x), 'minimum_amplitude_error_bound': float(np.min(error_bounds)),
                      'maximum_amplitude_error_bound': float(np.max(error_bounds))})
        # 与锁定引擎使用相同的np.round角桶；相邻桶即使角差更小也不能互相救活残差。
        bin_keys = np.round(angles / XRD_TWO_THETA_TOLERANCE).astype(np.int64)
        order = np.argsort(bin_keys)
        ordered_keys = bin_keys[order]
        for angle, raw, d, hkls in zip(pattern.x, pattern.y, pattern.d_hkls, pattern.hkls):
            bin_key = int(np.round(float(angle) / XRD_TWO_THETA_TOLERANCE))
            lower = np.searchsorted(ordered_keys, bin_key, side='left')
            upper = np.searchsorted(ordered_keys, bin_key, side='right')
            selected = order[lower:upper]
            if not len(selected):
                raise ToolError('衍射角桶未找到可复核的倒易反射。', 'verification')
            resolved = amplitudes[selected] > error_bounds[selected]
            envelope_upper = math.fsum(float(2 * lorentz[index] * (amplitudes[index] + 2 * error_bounds[index]) ** 2) for index in selected)
            resolution = {'angular_bin_key': bin_key, 'checked_reflections': int(len(selected)),
                          'resolved_reflections': int(np.count_nonzero(resolved)),
                          'minimum_amplitude_error_bound': float(np.min(error_bounds[selected])),
                          'maximum_amplitude_error_bound': float(np.max(error_bounds[selected])),
                          'maximum_amplitude_to_error_bound_ratio': float(np.max(amplitudes[selected] / error_bounds[selected])),
                          'intensity_envelope_upper_bound_au': envelope_upper}
            peak = {'two_theta_degree': float(angle), 'd_angstrom': float(d), 'intensity_raw_au': float(raw),
                    'hkls': [{'hkl': [int(value) for value in family['hkl']], 'multiplicity': int(family['multiplicity'])}
                             for family in hkls], 'numerical_resolution': resolution}
            if not np.any(resolved):
                if float(raw) > envelope_upper * (1 + 1e-10):
                    raise ToolError('相消反射的引擎强度超出已记录的浮点误差估算，当前无法确认精度。', 'unsupported_precision')
                audit['discarded_peak_count'] += 1
                if len(audit['discarded_peaks']) < audit['discarded_peak_details_limit']:
                    audit['discarded_peaks'].append(peak)
            else:
                peaks.append(peak)
        if len(peaks) > XRD_MAX_OUTPUT_PEAKS:
            raise ToolError('可分辨衍射峰数量超出当前导出预算，请缩小角度范围。', 'unsupported_scale')
        maximum = max((peak['intensity_raw_au'] for peak in peaks), default=0.0)
        for peak in peaks:
            peak['intensity_relative_max100'] = peak['intensity_raw_au'] / maximum * 100
        audit['discarded_peak_details_truncated'] = audit['discarded_peak_count'] > len(audit['discarded_peaks'])
    return {'peaks': peaks, 'symprec': 0, 'intensity_model': 'static_independent_atoms_lorentz_polarization',
            'raw_intensity_unit': 'arbitrary_model_unit', 'relative_intensity_scale': 'maximum_100',
            'two_theta_merge_tolerance_degree': XRD_TWO_THETA_TOLERANCE,
            'peak_retention_relative_to_range_maximum_percent': 0.0, 'numerical_extinction_filter': audit,
            'thermal_parameters_applied': False, 'oxidation_states_applied': False, 'enumeration_budget': budget}


def _reciprocal_points(structure, parameters, budget):
    """预算先通过，再沿用官方倒易点长度，避免桶边界的独立范数舍入差异。"""
    import numpy as np
    low, high = [2 * math.sin(math.radians(parameters[name] / 2)) / parameters['wavelength_angstrom']
                 for name in ('two_theta_min', 'two_theta_max')]
    points = structure.lattice.reciprocal_lattice_crystallographic.get_points_in_sphere(
        [[0, 0, 0]], [0, 0, 0], high)
    if len(points) > budget['bounding_box_points']:
        raise ToolError('实际倒易点数量超过预先核验的枚举上界。', 'unsupported_scale')
    points = [point for point in points if point[1] >= low]
    if not points:
        return np.empty((0, 3), dtype=np.int64), np.empty(0), np.empty(0)
    hkl = np.round([point[0] for point in points]).astype(np.int64)
    lengths = np.asarray([point[1] for point in points], dtype=np.float64)
    h, k, l = hkl.T
    selected = (h > 0) | ((h == 0) & (k > 0)) | ((h == 0) & (k == 0) & (l > 0))
    hkl, lengths = hkl[selected], lengths[selected]
    angles = np.degrees(2 * np.arcsin(np.clip(parameters['wavelength_angstrom'] * lengths / 2, -1, 1)))
    return hkl, angles, lengths


def _structure_factor_resolution(structure, hkl, reciprocal_lengths):
    """对原float64坐标的相位求和给出逐反射误差估算；不是物理模型不确定度。"""
    import numpy as np
    from pymatgen.analysis.diffraction.xrd import ATOMIC_SCATTERING_PARAMS
    recipe = extinction_error_recipe(len(structure))
    u = recipe['unit_roundoff']
    def gamma(n):
        return n * u / (1 - n * u)
    coords = np.asarray(structure.frac_coords, dtype=np.float64)
    absolute_dot_sum = np.abs(hkl) @ np.abs(coords).T
    gphase = gamma(5) + gamma(3)
    phase_error = 2 * math.pi * gphase / (1 - gphase) * absolute_dot_sum + 2 * math.pi * u + 8 * u
    phase = 2 * math.pi * np.remainder(hkl @ coords.T, 1.0)
    real, imaginary = np.cos(phase), np.sin(phase)
    sum_absolute_weights = np.zeros(len(hkl), dtype=np.float64)
    propagated_error = np.zeros(len(hkl), dtype=np.float64)
    symbols = [site.specie.symbol for site in structure]
    s2 = (reciprocal_lengths / 2) ** 2
    for symbol in sorted(set(symbols)):
        indices = [index for index, actual in enumerate(symbols) if actual == symbol]
        coeff = np.asarray(ATOMIC_SCATTERING_PARAMS[symbol], dtype=np.float64)
        z = structure[indices[0]].specie.Z
        terms = coeff[:, 0] * np.exp(-coeff[:, 1] * s2[:, None])
        correction = 41.78214 * s2 * np.sum(terms, axis=1)
        factor = z - correction
        factor_gamma = gamma(16 + 8 * len(coeff))
        factor_error = factor_gamma / (1 - factor_gamma) * (abs(z) + 41.78214 * s2 * np.sum(np.abs(terms), axis=1))
        magnitude = np.abs(factor)
        real[:, indices] *= factor[:, None]
        imaginary[:, indices] *= factor[:, None]
        sum_absolute_weights += len(indices) * magnitude
        propagated_error += magnitude * np.sum(math.sqrt(2) * phase_error[:, indices] + 2 * u, axis=1) + len(indices) * factor_error
    amplitudes = np.hypot(np.fromiter((math.fsum(row) for row in real), dtype=np.float64, count=len(hkl)),
                          np.fromiter((math.fsum(row) for row in imaginary), dtype=np.float64, count=len(hkl)))
    gsum = gamma(2 * len(structure) + 2)
    error_bounds = np.nextafter((propagated_error + math.sqrt(2) * gsum * sum_absolute_weights) / (1 - gsum), np.inf)
    return amplitudes, error_bounds


def run(inputs, operation, parameters, progress_path=None):
    start = time.monotonic()
    inputs, parameters = validate_inputs(inputs, operation), validate_parameters(operation, parameters)
    _progress(progress_path, 'preparing', 5)
    _version_check()
    from .artifacts import (build_bundle, json_bytes, make_summary, report_text,
                            structure_files, validate_bundle, xrd_csv, xrd_graphs)
    models_and_sources = [load_structure(item['content'], item['filename']) for item in inputs]
    originals = [structure_record(model, source, ('a', 'b')[index] if operation == 'compare' else 'input-structure')
                 for index, (model, source) in enumerate(models_and_sources)]
    models = [model for model, _ in models_and_sources]
    sources = [source for _, source in models_and_sources]
    _progress(progress_path, 'calculating', 25)
    if operation == 'analyze':
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
        analyzer = SpacegroupAnalyzer(models[0], **parameters)
        systems = {'triclinic': '三斜晶系', 'monoclinic': '单斜晶系', 'orthorhombic': '正交晶系',
                   'tetragonal': '四方晶系', 'trigonal': '三方晶系', 'hexagonal': '六方晶系', 'cubic': '立方晶系'}
        crystal_system = analyzer.get_crystal_system()
        details = {'spacegroup': {'number': int(analyzer.get_space_group_number()),
                                  'symbol': analyzer.get_space_group_symbol(), 'hall': analyzer.get_hall(),
                                  'point_group': analyzer.get_point_group_symbol(),
                                  'crystal_system': systems.get(crystal_system, crystal_system),
                                  'crystal_system_identifier': crystal_system},
                   'calculated_from_expanded_geometry': True, 'input_declaration_replaced': False}
    elif operation == 'xrd':
        details = _xrd(models[0], parameters)
    elif operation == 'supercell':
        factor = math.prod(parameters['scaling'])
        if len(models[0]) * factor > MAX_OUTPUT_ATOMS:
            raise ToolError('超胞原子数超过4096，请减小扩胞分量。', 'unsupported_scale')
        models[0] = models[0].copy()
        models[0].make_supercell(parameters['scaling'], to_unit_cell=True)
        _scale_check(models[0], MAX_OUTPUT_ATOMS)
        details = {'scaling': parameters['scaling'], 'multiplicity': factor}
    elif operation == 'compare':
        from pymatgen.core.structure_matcher import ElementComparator, StructureMatcher
        _comparison_budget(models[0], models[1], parameters)
        matcher = StructureMatcher(**parameters, comparator=ElementComparator(), attempt_supercell=False)
        matched = bool(matcher.fit(models[0], models[1]))
        distances = matcher.get_rms_dist(models[0], models[1]) if matched else None
        if matched and distances is None:
            raise ToolError('结构匹配未产生可核验的位移。', 'verification')
        details = {'matched': matched, 'rms_displacement_normalized': float(distances[0]) if matched else None,
                   'max_displacement_normalized': float(distances[1]) if matched else None,
                   'displacement_normalization': 'mean_free_length_per_atom', 'comparator': 'element_and_geometry',
                   'fit_criterion': 'minimum_maximum_displacement', 'rms_criterion': 'minimum_average_rms_displacement',
                   'source_oxidation_information_preserved': True}
    else:
        details = {}
    _progress(progress_path, 'packaging', 70)
    records = [structure_record(model, source, ('a', 'b')[index] if operation == 'compare' else 'structure')
               for index, (model, source) in enumerate(zip(models, sources))]
    files = {}
    for index, (model, record) in enumerate(zip(models, records)):
        prefix = ('a-', 'b-')[index] if operation == 'compare' else ''
        exports = structure_files(record, prefix)
        _verify_roundtrip(model, exports, prefix)
        files.update(exports)
    details['roundtrip_verified'] = True
    if operation == 'xrd':
        files.update({'xrd.json': json_bytes(details), 'xrd.csv': xrd_csv(details), **xrd_graphs(details, parameters)})
    try:
        import resource
        peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
    except ImportError:
        peak_rss = None
    versions = {name: importlib.metadata.version(name) for name in ('pymatgen', 'pymatgen-core', 'numpy', 'scipy', 'ase', 'spglib')}
    result = {'profile_version': PROFILE_VERSION, 'operation': operation, 'parameters': parameters,
              'input_structures': originals, 'structures': records, 'details': details,
              'summary': make_summary(operation, records, details), 'versions': versions,
              'runtime': {'seconds': time.monotonic() - start, 'peak_rss_bytes': peak_rss,
                          'measurement_scope': 'through_roundtrip_before_packaging'},
              'methods': {'source_geometry': 'explicit_operations_authoritative_without_setting_guess',
                          'ordered_full_occupancy_only': True, 'derived_cif_setting': 'P1_expanded_geometry',
                          'source_declarations_and_bytes_preserved': True,
                          'conversion_scope': ['elements', 'lattice', 'periodic_fractional_coordinates'],
                          'geometry_verification': dict(GEOMETRY_VERIFICATION),
                          'selective_dynamics_applied': False, 'velocities_applied': False,
                          'other_non_geometric_source_properties_transferred': False,
                          'non_geometric_attribute_retention': 'original_input_bytes_only',
                          'server_verification': 'cross_format_geometry_bragg_and_plot_consistency',
                          'server_independently_recomputes_intensity': False}}
    files['报告.txt'] = report_text(result)
    bundle = build_bundle(inputs, operation, parameters, result, files)
    validate_bundle(bundle, inputs, operation, parameters)
    _progress(progress_path, 'packaging', 95)
    return bundle
