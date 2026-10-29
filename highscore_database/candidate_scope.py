"""候选和历史下载的范围提示；只使用服务端已识别的数据类型。

候选展示名称、卡号及来源文本不能证明读取器类型。历史任务缺少类型时，
调用方应先用服务端保存的来源身份解析配置，再传入 reader_profile。
本函数不补齐、修改或合并任何科学字段，也不影响是否允许归档。
"""


def candidate_scope(candidate, *, reader_profile=None, data_type=None):
    """返回固定中文类型与范围说明，保留原候选及来源记录。

    ``peak_count`` 只用于已识别索引的199峰边界提醒；普通数据库即使
    峰数更多也不能因此被判定为索引截断。HKL以实际峰记录为准。
    """
    profile = reader_profile if reader_profile is not None else candidate.get('reader_profile')
    kind = data_type if data_type is not None else candidate.get('data_type')
    if kind == 'jade_index_data' or profile in ('jade_pdf4_2009_index_v1', 'jade_pdf4_2009_index_v2'):
        count = candidate.get('peak_count')
        truncated = type(count) is int and count >= 199
        v2 = profile == 'jade_pdf4_2009_index_v2' or isinstance(candidate.get('index_metadata'), dict)
        detail = ('含名称、化学式、已读取的晶体学参数及索引峰表；参考文献尚未读取；'
                  if v2 else '含名称、化学式及索引峰表；晶胞、空间群和参考文献尚未读取；')
        detail += '提供百分强度和2θ，另保留原始强度。'
        if truncated:
            detail += '峰表可能截断。'
        return {'report_type_label': '索引报告', 'report_scope_detail': detail,
                'possible_truncation': truncated}
    if (kind == 'jade_structure_data' or profile == 'jade_icsd_structure_v1'
            or candidate.get('content_type') == 'structure'):
        return {'report_type_label': '晶体结构报告',
                'report_scope_detail': '含晶胞、对称操作及原子坐标，另附独立 CIF 文件。',
                'possible_truncation': False}
    return {'report_type_label': '数据库报告',
            'report_scope_detail': '包含原记录已提供的字段和峰表；缺失字段标为“未提供”。',
            'possible_truncation': False}
