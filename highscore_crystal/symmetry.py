"""使用 CIF 显式完整操作展开，不给已有操作附加默认反演或平移。"""
import ast
from fractions import Fraction
import math


def _linear(expression):
    tree = ast.parse(expression.strip(), mode='eval')
    if len(list(ast.walk(tree))) > 64:
        raise ValueError('对称操作表达式过长。')

    def evaluate(node):
        if isinstance(node, ast.Name) and node.id in ('x', 'y', 'z'):
            return tuple(Fraction(int(index == ('x', 'y', 'z').index(node.id))) for index in range(4))
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not math.isfinite(node.value) or abs(node.value) > 10000:
                raise ValueError('对称操作常数超出支持范围。')
            return (Fraction(0), Fraction(0), Fraction(0), Fraction(str(node.value)))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return tuple(-item for item in value) if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                return tuple(a + b if isinstance(node.op, ast.Add) else a - b for a, b in zip(left, right))
            if isinstance(node.op, ast.Mult):
                if any(left[:3]) and any(right[:3]):
                    raise ValueError('对称操作只能是线性表达式。')
                scalar, vector = (right[3], left) if not any(right[:3]) else (left[3], right)
                return tuple(scalar * item for item in vector)
            if isinstance(node.op, ast.Div) and not any(right[:3]) and right[3] != 0:
                return tuple(item / right[3] for item in left)
        raise ValueError('对称操作包含不支持的表达式。')

    value = evaluate(tree.body)
    if any(item.denominator > 4096 or abs(item) > 10000 for item in value):
        raise ValueError('对称操作分数超出支持范围。')
    return value


def _key(rotation, translation):
    import numpy as np
    values = np.mod(np.asarray(translation), 1.0)
    values[np.isclose(values, 1.0, rtol=0, atol=1e-8)] = 0
    return tuple(np.asarray(rotation, dtype=int).ravel()) + tuple(np.round(values, 8))


def operations_from_block(block):
    import numpy as np
    from ase.spacegroup import Spacegroup

    explicit = block.get('_space_group_symop_operation_xyz') or block.get('_symmetry_equiv_pos_as_xyz')
    number = block.get('_space_group_it_number') or block.get('_symmetry_int_tables_number')
    name = block.get('_space_group_name_h-m_alt') or block.get('_symmetry_space_group_name_h-m')
    setting = block.get('_symmetry_space_group_setting')
    metadata = {'number': str(number) if number is not None else None,
                'hm_symbol': str(name) if name is not None else None,
                'declared_setting': str(setting) if setting is not None else None}
    if explicit:
        expressions = explicit if isinstance(explicit, list) else [explicit]
        if len(expressions) > 192 or any(not isinstance(item, str) or len(item) > 256 for item in expressions):
            raise ValueError('显式对称操作超出支持范围。')
        rotations, translations = [], []
        for expression in expressions:
            parts = expression.casefold().split(',')
            if len(parts) != 3:
                raise ValueError('对称操作须有三个坐标分量。')
            coefficients = [_linear(part) for part in parts]
            if any(item.denominator != 1 or abs(item) > 6 for line in coefficients for item in line[:3]):
                raise ValueError('对称旋转矩阵不是受支持的晶格整数变换。')
            rotation = np.array([[int(item) for item in line[:3]] for line in coefficients], dtype=int)
            if not np.isclose(abs(np.linalg.det(rotation)), 1, rtol=0, atol=1e-8):
                raise ValueError('对称旋转矩阵的行列式无效。')
            rotations.append(rotation)
            translations.append([float(line[3] % 1) for line in coefficients])
        metadata.update({'method': 'explicit_complete_operations', 'input_operations': expressions})
        if number is not None or name:
            declared = []
            for candidate_setting in (1, 2):
                if setting is not None and str(setting).strip() != str(candidate_setting):
                    continue
                try:
                    candidate = Spacegroup(str(name) if name else int(number), setting=candidate_setting)
                except Exception:
                    # 显式操作已给出非标准轴名（如Pnam）时，签名只依赖已声明群号。
                    if number is None:
                        continue
                    try:
                        candidate = Spacegroup(int(number), setting=candidate_setting)
                    except Exception:
                        continue
                if number is not None and int(number) != candidate.no:
                    continue
                declared.append(candidate)
            def signature(values):
                from collections import Counter
                return Counter((int(round(np.linalg.det(r))), int(round(np.trace(r))),
                                int(round(np.trace(np.matmul(r, r))))) for r in values)
            actual = signature(rotations)
            compatible = [candidate for candidate in declared
                          if signature([pair[0] for pair in candidate.get_symop()]) == actual]
            if not compatible:
                raise ValueError('显式对称操作数量或点群与声明空间群不一致，无法确认操作完整性。')
            metadata['declared_group_check'] = 'operation_count_and_point_group_signature'
            metadata['coordinate_authority'] = 'explicit_operations'
            metadata['full_spacegroup_classification_verified'] = False
    else:
        if not name:
            if str(number).strip() != '1':
                raise ValueError('请提供完整显式对称操作或明确的空间群符号；不能只凭群号猜测坐标设置。')
            name = 'P 1'
        candidates = []
        for candidate_setting in (1, 2):
            if setting is not None and str(setting).strip() != str(candidate_setting):
                continue
            try:
                candidate = Spacegroup(str(name), setting=candidate_setting)
            except Exception:
                continue
            if number is not None and int(number) != candidate.no:
                continue
            candidates.append(candidate)
        if len(candidates) != 1:
            raise ValueError('空间群符号与坐标设置不唯一或不一致，请提供完整显式对称操作。')
        group = candidates[0]
        pairs = group.get_symop()
        rotations, translations = zip(*pairs)
        metadata.update({'method': 'exact_hm_symbol_and_setting', 'resolved_setting': group.setting,
                         'resolved_hm_symbol': group.symbol})
    rotations, translations = np.asarray(rotations, dtype=int), np.asarray(translations, dtype=float)
    keys = {_key(rotation, translation) for rotation, translation in zip(rotations, translations)}
    if len(keys) != len(rotations) or _key(np.eye(3, dtype=int), [0, 0, 0]) not in keys:
        raise ValueError('显式对称操作缺少恒等操作或包含重复。')
    for rotation, translation in zip(rotations, translations):
        composed_rotations = np.matmul(rotation, rotations)
        composed_translations = translations @ rotation.T + translation
        if any(_key(r, t) not in keys for r, t in zip(composed_rotations, composed_translations)):
            raise ValueError('对称操作不是完整闭合的晶体对称集合。')
    metadata['operation_count'] = len(rotations)
    return rotations, translations, metadata


def expand_block(block, max_atoms):
    import numpy as np
    from ase import Atoms

    base = block.get_unsymmetrized_structure()
    if base.cell.rank != 3:
        raise ValueError('结构没有完整三维晶胞。')
    rotations, translations, metadata = operations_from_block(block)
    metric = np.asarray(base.cell) @ np.asarray(base.cell).T
    if any(not np.allclose(rotation.T @ metric @ rotation, metric, rtol=1e-4, atol=1e-5)
           for rotation in rotations):
        raise ValueError('对称旋转与原始晶胞度量不一致，无法确认坐标设置。')
    scaled = base.get_scaled_positions(wrap=False)
    if not np.isfinite(scaled).all():
        raise ValueError('原始原子坐标不是有限数值。')
    multiplicities = block.get('_atom_site_symmetry_multiplicity') or block.get('_atom_site_multiplicity')
    if multiplicities is not None and (not isinstance(multiplicities, list) or len(multiplicities) != len(base)):
        raise ValueError('原始位点重数数量不一致。')
    positions, symbols, masses, seen = [], [], [], set()
    for index, point in enumerate(scaled):
        expanded = np.einsum('ijk,k->ij', rotations, point) + translations
        expanded %= 1.0
        expanded[np.isclose(expanded, 1.0, rtol=0, atol=1e-8)] = 0
        sites = {}
        for coordinate in expanded:
            sites.setdefault(tuple(np.round(coordinate, 8)), coordinate)
        if multiplicities is not None:
            value = multiplicities[index]
            if type(value) not in (int, float) or not float(value).is_integer() or int(value) != len(sites):
                raise ValueError('位点重数与实际对称操作展开不一致。')
        for key, coordinate in sites.items():
            if key in seen:
                raise ValueError('不同原子位点展开后重合，无法确认有序满占有模型。')
            seen.add(key); positions.append(coordinate)
            symbols.append(base[index].symbol); masses.append(float(base[index].mass))
            if len(positions) > max_atoms:
                raise ValueError('对称展开原子数超出当前小模型范围。')
    atoms = Atoms(symbols=symbols, scaled_positions=positions, cell=base.cell, masses=masses, pbc=True)
    return atoms, metadata
