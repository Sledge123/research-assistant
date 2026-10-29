"""从数据库字段生成报告，绝不标记为 HighScore 原生导出。"""
from datetime import datetime,timezone
import json
import math
from pathlib import Path

from highscore_downloader.archive import ArchiveStore,ArchiveError
from .reader import DatabaseError
from highscore_worker.verification import atomic_json
from .reporting import (REPORT_VERSION, report_fields, report_text, report_rtf, escape_rtf,
                        INDEX_DATA_TYPE, INDEX_REPORT_METHOD, INDEX_REPORT_ORIGIN,
                        STRUCTURE_DATA_TYPE, STRUCTURE_REPORT_METHOD, STRUCTURE_REPORT_ORIGIN)

METHOD='sqlite_readonly_database_report'
ORIGIN='generated_database_report'


def rtf_bytes(text):
    return ('{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\\uc1\\f0\n'+escape_rtf(text)+'}').encode('ascii')


def load_report_settings(root):
    from highscore_worker.config import project_path
    path=project_path(root,'database-report.local.json')
    if not path.exists():path=Path(__file__).with_name('peak_standard.json')
    try:
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(data,dict) or set(data)!={'wavelength_angstrom','source'}
                or not isinstance(data['wavelength_angstrom'],str)
                or not isinstance(data['source'],str) or not data['source'].strip()
                or len(data['source'])>500):raise ValueError()
        wave=float(data['wavelength_angstrom'])
        if not math.isfinite(wave) or wave<=0 or wave>100:raise ValueError()
        return data
    except (OSError,ValueError,TypeError,KeyError):
        raise DatabaseError('报告波长配置无效，必须使用明确的波长与来源。') from None


def prepare_card(card,settings=None,*,standardize_index=True):
    if card.get('data_type') == STRUCTURE_DATA_TYPE:
        return card
    if card.get('data_type') == INDEX_DATA_TYPE:
        if not standardize_index:return card
        # 这是当前索引峰表内I/Imax的派生值，不猜测RID头部标尺的物理含义。
        values=[int(row['intensity_raw']) for row in card['peaks']]
        maximum=max(values)
        if maximum<=0:raise DatabaseError('索引峰表没有正强度，无法计算百分强度。')
        card={**card,'intensity_normalization':{
            'method':'index_table_maximum','formula':'I_percent = 100 * I_raw / max(I_raw)',
            'maximum_intensity_raw':str(maximum),'scope':'available_index_peak_table',
            'intensity_origin':'calculated','original_header_scale_used':False},
            'peaks':[{**row,'intensity_percent':format(100*value/maximum,'.6f').rstrip('0').rstrip('.')}
                     for row,value in zip(card['peaks'],values)]}
    if not settings:return card
    wave=float(settings['wavelength_angstrom'])
    if not math.isfinite(wave) or wave<=0:raise DatabaseError('报告波长无效。')
    original_peaks=card['peaks']
    card={**card,'wavelength_angstrom':settings['wavelength_angstrom'],
          'angular_conversion':{'method':'bragg_first_order','formula':'2theta = 2 asin(lambda / (2 d))',
                                'wavelength_angstrom':settings['wavelength_angstrom'],
                                'wavelength_source':settings['source'],'two_theta_origin':'calculated',
                                'inaccessible_peak_policy':'null; original d/intensity/HKL retained'},'peaks':[]}
    for row in original_peaks:
        ratio=wave/(2*float(row['d_angstrom']))
        angle=f'{math.degrees(2*math.asin(ratio)):.3f}' if ratio<=1 else None
        card['peaks'].append({**row,'two_theta_deg':angle})
    return card


class DatabaseExporter:
    def __init__(self,root,reader,*,report_settings=None):
        self.root,self.reader=Path(root).resolve(),reader
        self.report_settings=report_settings if report_settings is not None else load_report_settings(self.root)
        self.store=ArchiveStore(self.root)

    @staticmethod
    def _verify_rtf(path,expected):
        from striprtf.striprtf import rtf_to_text
        try:
            decoded=rtf_to_text(path.read_bytes().decode('ascii'))
            # RTF 使用 UTF-16 的有符号 \u 单元；解析器保留的代理对在此规范化。
            decoded=decoded.encode('utf-16','surrogatepass').decode('utf-16')
            if decoded!=expected:raise ValueError()
        except (OSError,UnicodeError,ValueError):
            raise DatabaseError('数据库报告 RTF 读回文本与源报告不一致，禁止归档或发信。') from None

    def export(self,code):
        source=self.reader.card(code)
        from .validation import refresh_card
        card=refresh_card(source,report_settings=self.report_settings)
        index_report = card.get('data_type') == INDEX_DATA_TYPE
        structure_report = card.get('data_type') == STRUCTURE_DATA_TYPE
        method = INDEX_REPORT_METHOD if index_report else STRUCTURE_REPORT_METHOD if structure_report else METHOD
        origin = INDEX_REPORT_ORIGIN if index_report else STRUCTURE_REPORT_ORIGIN if structure_report else ORIGIN
        card['report_fields']=report_fields(card)
        receipt_path=f"output/cards/{card['code']}/receipt.json"
        if (self.root/receipt_path).exists():
            receipt=self.store.validate(receipt_path)
            existing=json.loads((self.root/receipt['files']['json']['path']).read_text(encoding='utf-8'))
            if (existing.get('database_id')!=card['database_id']
                    or existing.get('verification',{}).get('method')!=method
                    or receipt.get('report_version')!=REPORT_VERSION
                    or receipt.get('export_method')!=method or receipt.get('rtf_origin')!=origin):
                raise DatabaseError('已有归档的来源、数据库指纹或报告类型不一致，禁止覆盖。')
            if (any(existing.get(key)!=value for key,value in card.items())
                    or existing.get('raw_text')!=report_text(card)
                    or existing.get('verification',{}).get('database_sha256')!=card['database_sha256']):
                raise DatabaseError('已有报告与当前只读数据库源字段或完整峰表不一致，禁止复用。')
            self._verify_rtf(self.root/receipt['files']['rtf']['path'],existing['raw_text'])
            if (self.root/receipt['files']['rtf']['path']).read_bytes()!=report_rtf(card):
                raise DatabaseError('已有报告版式与当前报告版本不一致，禁止复用。')
            return receipt
        card['verification']={'method':method,'checked_at':datetime.now(timezone.utc).isoformat(),
                              'peak_count':card['peak_count'],'database_sha256':card['database_sha256'],
                              'rtf_origin':origin,'native_highscore_export':False,
                              'report_version':REPORT_VERSION,'completeness_scope':
                              'verified_index_records' if index_report else
                              'verified_structure_records' if structure_report else 'available_database_fields'}
        card['raw_text']=report_text(card)
        cache=self.root/'.cache/database-reports';cache.mkdir(parents=True,exist_ok=True)
        native=cache/(card['code']+'.rtf')
        native.write_bytes(report_rtf(card))
        self._verify_rtf(native,card['raw_text'])
        receipt=self.store.save(card,native,code)
        self._verify_rtf(self.root/receipt['files']['rtf']['path'],card['raw_text'])
        receipt.update(export_method=method,rtf_origin=origin,database_id=card['database_id'],report_version=REPORT_VERSION)
        atomic_json(self.root/receipt['receipt_path'],receipt)
        return self.store.validate(receipt['receipt_path'])
