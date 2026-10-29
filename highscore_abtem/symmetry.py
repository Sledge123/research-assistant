"""兼容原模拟接口；严格CIF对称操作实现在公共晶体模块。"""
from highscore_crystal.symmetry import expand_block, operations_from_block

__all__ = ['expand_block', 'operations_from_block']
