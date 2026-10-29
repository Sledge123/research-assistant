"""从已绑定 CIF 构建有序静态 IAM 模型并生成可复核的数值结果。"""
import importlib.metadata
import io
import json
import math
from pathlib import Path
import re
import time

from .contract import (ABTEM_VERSION, MAX_ATOMS, MAX_GRID, MAX_LATERAL_ANGSTROM,
                       PROFILE_VERSION, validate_cif_bytes, validate_parameters)


class SimulationError(ValueError):
    def __init__(self, message, code='unsupported_structure'):
        super().__init__(message)
        self.code = code


def _progress(path, phase, progress):
    if path is not None:
        target = Path(path)
        temporary = target.with_suffix('.new')
        temporary.write_text(json.dumps({'phase': phase, 'progress': progress}), encoding='utf-8')
        temporary.replace(target)


def prepare_model(input_cif, parameters):
    import numpy as np
    from abtem.atoms import best_orthogonal_cell, orthogonalize_cell
    from highscore_crystal.cif import CrystalError, load_cif
    try:
        atoms, source = load_cif(input_cif, max_atoms=MAX_ATOMS, max_operation_product=MAX_ATOMS * 2)
    except CrystalError as exc:
        raise SimulationError(str(exc), exc.code) from None
    symmetry = source['symmetry_expansion']
    original_cell = np.array(atoms.cell, dtype=float)
    # 原始 [uvw] 乘以原始实空间晶胞，明确区别于晶面指数。
    beam = np.asarray(parameters['zone_axis'], dtype=float) @ original_cell
    beam /= np.linalg.norm(beam)
    candidates = [vector - np.dot(vector, beam) * beam for vector in original_cell]
    x_axis = max(candidates, key=np.linalg.norm)
    x_axis /= np.linalg.norm(x_axis)
    basis = np.array((x_axis, np.cross(beam, x_axis), beam))
    atoms.positions[:] = atoms.positions @ basis.T
    atoms.set_cell(original_cell @ basis.T, scale_atoms=False)
    atoms.pbc = True
    box = np.asarray(best_orthogonal_cell(atoms.cell, max_repetitions=5), dtype=float)
    if not np.isfinite(box).all() or min(box) <= 0 or max(box[:2]) > MAX_LATERAL_ANGSTROM:
        raise SimulationError('该方向的周期横向晶胞超出当前小模型范围。', 'unsupported_scale')
    estimate = len(atoms) * np.prod(box) / abs(np.linalg.det(atoms.cell))
    if estimate > MAX_ATOMS:
        raise SimulationError('该方向的周期超胞原子数超出当前支持范围。', 'unsupported_scale')
    atoms, transform = orthogonalize_cell(atoms, max_repetitions=5, return_transform=True, tolerance=1e-5)
    rotation, scale, shear = [np.asarray(value, dtype=float) for value in transform]
    if (np.max(np.abs(rotation)) > 1e-4 or np.max(np.abs(scale - 1)) > 1e-4
            or np.max(np.abs(shear)) > 1e-4):
        raise SimulationError('该方向需要超出允许误差的晶胞变形，当前无法无损构建周期模型。')
    lengths = atoms.cell.lengths()
    if not np.allclose(atoms.cell.array, np.diag(lengths), rtol=1e-9, atol=1e-6):
        raise SimulationError('该结构无法构建已核验的正交周期模型。')
    thickness = parameters['thickness_nm'] * 10
    repeats = [max(1, int(math.ceil(8.0 / length))) for length in lengths[:2]]
    repeats.append(max(1, int(math.ceil(thickness / lengths[2]))))
    predicted = len(atoms) * math.prod(repeats)
    if predicted > MAX_ATOMS or max(lengths[:2] * repeats[:2]) > MAX_LATERAL_ANGSTROM + 1e-7:
        raise SimulationError('展开后的原子模型超出当前计算规模。', 'unsupported_scale')
    # ASE默认eps会把接近上边界的真实原子移至负坐标；有限厚度必须采用[0,L)。
    atoms.wrap(eps=0.0)
    base_fractional = atoms.get_scaled_positions(wrap=False)
    base_fractional[np.abs(base_fractional) < 1e-12] = 0.0
    base_fractional[np.abs(base_fractional - 1.0) < 1e-12] = 0.0
    atoms.set_scaled_positions(base_fractional)
    atoms = atoms.repeat(tuple(repeats))
    atoms = atoms[(atoms.positions[:, 2] >= 0) & (atoms.positions[:, 2] < thickness)]
    if not 1 <= len(atoms) <= MAX_ATOMS:
        raise SimulationError('当前取向与厚度没有形成受支持的原子模型。')
    atoms.set_cell([float(lengths[0] * repeats[0]), float(lengths[1] * repeats[1]), thickness])
    atoms.pbc = (True, True, False)
    atoms.wrap(eps=0.0)
    fractional = atoms.get_scaled_positions(wrap=False)
    fractional[np.abs(fractional) < 1e-12] = 0.0
    for axis in (0, 1):
        fractional[np.abs(fractional[:, axis] - 1.0) < 1e-12, axis] = 0.0
    atoms.set_scaled_positions(fractional)
    if not np.isfinite(fractional).all() or np.min(fractional) < 0 or np.max(fractional) >= 1:
        raise SimulationError('展开后的原子坐标无效。')
    grid = tuple(max(64, 2 ** math.ceil(math.log2(length / 0.1))) for length in atoms.cell.lengths()[:2])
    if max(grid) > MAX_GRID:
        raise SimulationError('当前晶胞需要更大采样网格，超出轻量计算范围。', 'unsupported_scale')
    model = {'cell_angstrom': atoms.cell.lengths().tolist(), 'symbols': atoms.get_chemical_symbols(),
             'fractional_positions': fractional.tolist(), 'pbc': [True, True, False],
             'zone_axis': parameters['zone_axis'], 'occupancy': 'full', 'thermal_model': 'static',
             'original_cell_angstrom': original_cell.tolist(), 'input_atom_count': source['input_atom_count'],
             'orientation_basis_cartesian': basis.tolist(), 'orthogonalization_rotation_rad': rotation.tolist(),
             'orthogonalization_scale': scale.tolist(), 'orthogonalization_shear': shear.tolist(),
             'repeat_counts': repeats, 'thickness_handling': 'finite_slab_cut_to_requested_thickness',
             'source_spacegroup': source['source_spacegroup'],
             'input_thermal_parameters_present': source['input_thermal_parameters_present'],
             'symmetry_expansion': symmetry, 'periodic_boundary_canonicalization_tolerance': 1e-12}
    return atoms, model, grid


def simulate(input_cif, parameters, progress_path=None):
    import resource
    start = time.monotonic()
    _progress(progress_path, 'preparing', 5)
    parameters = validate_parameters(parameters)
    if importlib.metadata.version('abtem') != ABTEM_VERSION:
        raise SimulationError('模拟环境版本未通过绑定核验。')
    import numpy as np
    import abtem
    from .artifacts import build_bundle

    abtem.config.set({'device': 'cpu', 'fft': 'numpy', 'precision': 'float32',
                      'dask.lazy': True, 'dask.chunk-size': '16 MB'})
    atoms, model, grid = prepare_model(input_cif, parameters)
    _progress(progress_path, 'calculating', 30)
    potential = abtem.Potential(atoms, gpts=grid, slice_thickness=0.5,
                               parametrization='lobato', projection='infinite', device='cpu')
    wave = abtem.PlaneWave(energy=parameters['energy_kev'] * 1000, device='cpu').multislice(potential, max_batch=1)
    wave = wave.compute(scheduler='single-threaded')
    mean_intensity = float(np.mean(np.abs(np.asarray(wave.array)) ** 2))
    if not 0.95 <= mean_intensity <= 1.02:
        raise SimulationError('当前轻量采样的传播精度不足，请减小厚度或调整结构方向。', 'unsupported_precision')
    diffraction = wave.diffraction_patterns(max_angle='full').compute(scheduler='single-threaded')
    image = wave.apply_ctf(abtem.CTF(energy=parameters['energy_kev'] * 1000,
                                    defocus=parameters['defocus_nm'] * 10,
                                    Cs=parameters['cs_mm'] * 10000000,
                                    semiangle_cutoff=parameters['aperture_mrad'])).intensity().compute(scheduler='single-threaded')
    _progress(progress_path, 'packaging', 85)
    measurements = {}
    for kind, measurement in (('saed', diffraction), ('hrtem', image)):
        array = np.ascontiguousarray(np.asarray(measurement.array).T, dtype='<f4')
        if array.ndim != 2 or not np.isfinite(array).all() or np.any(array < 0):
            raise SimulationError('模拟未产生有效的非负强度数组。')
        rows, cols = array.shape
        x_step, y_step = [float(value) for value in measurement.sampling]
        measurements[kind] = {'shape': [rows, cols], 'data': array.tobytes(),
                              'x_start': -(cols // 2) * x_step if kind == 'saed' else 0.0,
                              'y_start': -(rows // 2) * y_step if kind == 'saed' else 0.0,
                              'x_step': x_step, 'y_step': y_step,
                              'coordinate_unit': '1/angstrom' if kind == 'saed' else 'angstrom'}
    runtime = {'seconds': time.monotonic() - start,
               'peak_rss_bytes': int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
               'measurement_scope': 'through_array_generation_before_export',
               'grid': list(grid), 'slice_thickness_angstrom': 0.5,
               'wave_mean_intensity': mean_intensity, 'per_task_convergence': False,
               'profile_validation': 'isolated_silicon_sampling_and_slice_comparison',
               'saed_display': 'log1p_relative_to_max', 'hrtem_display': 'linear_min_max'}
    versions = {name: importlib.metadata.version(name) for name in ('abtem', 'numpy', 'scipy', 'numba', 'dask', 'ase')}
    bundle = build_bundle(input_cif, parameters, model, measurements, versions, runtime)
    _progress(progress_path, 'packaging', 95)
    return bundle
