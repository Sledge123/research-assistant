"""显式操作优先的有序满占有CIF加载，不猜坐标设置。"""
import io
import math
import re


class CrystalError(ValueError):
    def __init__(self, message, code='unsupported_structure'):
        super().__init__(message)
        self.code = code


def load_cif(content, *, max_atoms=512, max_raw_sites=2048, max_operation_product=None, max_cell_length=200):
    import numpy as np
    from ase.io.cif import parse_cif
    from .symmetry import expand_block

    if not isinstance(content, bytes) or not content or len(content) > 512 * 1024:
        raise CrystalError('请提供不超过512 KiB的CIF文件。')
    if b'\x00' in content or any(byte < 32 and byte not in (9, 10, 13) for byte in content):
        raise CrystalError('CIF文件包含无效控制字符。')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content.decode('latin-1')
    if not re.search(r'(?im)^\s*data_\S*', text):
        raise CrystalError('文件不是有效的CIF结构文件。')
    try:
        blocks = [block for block in parse_cif(io.StringIO(text)) if block.has_structure()]
        if len(blocks) != 1:
            raise CrystalError('请提供只包含一个完整晶体结构的CIF文件。')
        block = blocks[0]
        symbols = block.get_symbols()
        if not 1 <= len(symbols) <= max_raw_sites:
            raise CrystalError('原始晶体位点数量超出当前支持范围。', 'unsupported_scale')
        occupancy = block.get('_atom_site_occupancy')
        if occupancy is not None and (not isinstance(occupancy, list) or len(occupancy) != len(symbols) or any(
                type(item) not in (int, float) or not math.isfinite(item) or abs(item - 1.0) > 1e-8 for item in occupancy)):
            raise CrystalError('当前仅支持满占有结构，部分占有或未知占有率暂不支持。', 'unsupported_occupancy')
        for key in ('_atom_site_disorder_group', '_atom_site_disorder_assembly'):
            value = block.get(key)
            if value is not None:
                values = value if isinstance(value, list) else [value]
                if len(values) != len(symbols) or any(str(item).strip() not in ('.', '?', '0', '0.0') for item in values):
                    raise CrystalError('当前仅支持有序结构，无序结构暂不支持。', 'unsupported_disorder')
        raw_types = block.get('_atom_site_type_symbol')
        if raw_types is not None and any(not isinstance(item, str) or not re.fullmatch(r'(?:[A-Z][a-z]?|D)(?:\d*[+-]|[+-]\d*)?', item) for item in raw_types):
            raise CrystalError('元素字段包含混合或无法确定的原子类型。')
        operations = block.get('_space_group_symop_operation_xyz') or block.get('_symmetry_equiv_pos_as_xyz')
        group = (block.get('_space_group_it_number') or block.get('_symmetry_int_tables_number')
                 or block.get('_space_group_name_h-m_alt') or block.get('_symmetry_space_group_name_h-m'))
        if not operations and not group:
            raise CrystalError('CIF缺少明确空间群或对称操作，无法确认完整结构。')
        if operations is not None and (not isinstance(operations, list) or len(operations) > 192
                or (max_operation_product is not None and len(operations) * len(symbols) > max_operation_product)):
            raise CrystalError('对称展开规模超出当前支持范围。', 'unsupported_scale')
        atoms, symmetry = expand_block(block, max_atoms)
        if not 1 <= len(atoms) <= max_atoms or atoms.cell.rank != 3 or not np.isfinite(atoms.positions).all():
            raise CrystalError('CIF未得到受支持的完整三维晶体。')
        original_cell = np.asarray(atoms.cell, dtype=float)
        if not np.isfinite(original_cell).all() or abs(np.linalg.det(original_cell)) < 0.1 or max(atoms.cell.lengths()) > max_cell_length:
            raise CrystalError('原始晶胞尺寸或体积不在支持范围内。', 'unsupported_scale')
        source_fields = {key: value for key, value in block.items() if key.startswith(('_space_group_', '_symmetry_', '_atom_type_'))
                         or key in ('_atom_site_type_symbol', '_atom_site_occupancy')
                         or key.startswith(('_atom_site_b_', '_atom_site_u_'))}
        source = {'format': 'cif', 'input_atom_count': len(symbols),
                  'source_spacegroup': str(group) if group else 'explicit_operations',
                  'input_thermal_parameters_present': any(key.startswith(('_atom_site_b_', '_atom_site_u_')) for key in block),
                  'symmetry_expansion': symmetry, 'original_fields': source_fields}
        return atoms, source
    except CrystalError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AssertionError) as exc:
        raise CrystalError('CIF结构无法完整解析：' + str(exc)[:300]) from None
