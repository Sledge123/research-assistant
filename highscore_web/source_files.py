"""鉴权下载附加原始 BLOB 与经登记的原生文件，不改变原归档和邮件包。"""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import zipfile

from highscore_database.reader import DatabaseReader,PDF2_THOUSAND_INTENSITY_ORIGIN
from highscore_downloader.archive import ArchiveError
from .artifacts import checked_zip,validate_directory

MAX_NATIVE_BYTES=8*1024*1024


def _json_bytes(value):
    return (json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode('utf-8')


def _sqlite_snapshot(records,metadata):
    """无列亲和性的单卡快照保持SQLite值类型，源DDL仅作为元数据保存。"""
    def decoded(value):
        if not isinstance(value,dict):return value
        if set(value)!={'blob_base64','bytes','sha256'}:raise ValueError()
        data=base64.b64decode(value['blob_base64'],validate=True)
        if len(data)!=value['bytes'] or hashlib.sha256(data).hexdigest()!=value['sha256']:raise ValueError()
        return data
    quote=lambda name:'"'+name.replace('"','""')+'"'
    db=sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE __export_info (key,value)')
        db.executemany('INSERT INTO __export_info VALUES (?,?)',[(key,json.dumps(value,ensure_ascii=False)) for key,value in metadata.items()])
        for name,table in records['tables'].items():
            if not isinstance(name,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',name):raise ValueError()
            columns=[column['name'] for column in table['columns']]
            if not columns or any(not isinstance(name,str) for name in columns):raise ValueError()
            db.execute('CREATE TABLE '+quote(name)+' ('+','.join(quote(column) for column in columns)+')')
            rows=[]
            for row in table['rows']:
                if set(row)!=set(columns):raise ValueError()
                rows.append(tuple(decoded(row[column]) for column in columns))
            db.executemany('INSERT INTO '+quote(name)+' VALUES ('+','.join('?' for _ in columns)+')',rows)
            actual=db.execute('SELECT * FROM '+quote(name)+' ORDER BY rowid').fetchall()
            if actual!=rows or any(type(a)!=type(b) for old,new in zip(rows,actual) for a,b in zip(old,new)):raise ValueError()
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError()
        return db.serialize()
    finally:db.close()


def original_data(card):
    if card.get('data_type') == 'jade_index_data':
        return _index_original_data(card)
    if card.get('data_type') == 'jade_structure_data':
        return _structure_original_data(card)
    raw=card.get('database_raw')
    if raw is None:return {}
    try:
        sha=card['database_sha256'];identity=card['database_id'];code=card['code']
        if not re.fullmatch('[a-f0-9]{64}',sha) or identity!='hsrdb-'+sha[:48]:raise ValueError()
        lines=base64.b64decode(raw['lines_base64'],validate=True)
        hkl=base64.b64decode(raw['hkl_base64'],validate=True) if raw['hkl_base64'] is not None else None
        lines_i=None
        scale=raw.get('intensity_scale',100)
        if scale==999:
            if (raw.get('reader_profile')!='pdf2_2.3'
                    or raw.get('intensity_origin')!=PDF2_THOUSAND_INTENSITY_ORIGIN
                    or 'lines_i_base64' in raw):raise ValueError()
        elif scale!=100:
            if scale!=1000 or raw.get('reader_profile')!='cod2024_hs4x':raise ValueError()
            lines_i=base64.b64decode(raw['lines_i_base64'],validate=True)
        peaks=DatabaseReader._peaks(lines,hkl,intensity_scale=scale,lines_i=lines_i)
        if len(peaks)!=len(card['peaks']):raise ValueError()
        for original,current in zip(peaks,card['peaks']):
            if any(current[key]!=value for key,value in original.items() if key!='two_theta_deg'):raise ValueError()
        prefix=f'original-data/{code}/'
        payloads={prefix+'Lines.bin':lines}
        if hkl is not None:payloads[prefix+'HKL.bin']=hkl
        if lines_i is not None:payloads[prefix+'LinesI.bin']=lines_i
        record={'representation':'database_record_extraction','independent_rtf_in_database':False,
                'code':code,'database_id':identity,'database_sha256':sha,'source':card['source'],
                'general_indexed':raw['indexed'],'general_text_content':raw['text'],
                'general_stored':raw['stored'],'literature':card['literature'],'subfiles':card['subfiles'],
                'binary_fields':{name.rsplit('/',1)[1]:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                                 for name,data in payloads.items()}}
        if 'source_records' in raw:
            records=raw['source_records']
            if records['format']!='sqlite_card_rows_v1' or records['product_id']!=raw['indexed']['ProductID']:raise ValueError()
            if lines_i is not None:
                stored=records['tables']['general_stored']['rows']
                if len(stored)!=1 or base64.b64decode(stored[0]['LinesI']['blob_base64'],validate=True)!=lines_i:raise ValueError()
            record['source_records']=records
            payloads[prefix+'source-records.sqlite3']=_sqlite_snapshot(records,{
                'representation':'single_card_record_snapshot','code':code,'database_sha256':sha,
                'source_schema':{name:value['create_sql'] for name,value in records['tables'].items()}})
        payloads[prefix+'record.json']=_json_bytes(record)
        return payloads
    except (ValueError,KeyError,TypeError,sqlite3.Error):
        raise ArchiveError('原始数据库字段或二进制峰表复验失败。') from None


def _index_original_data(card):
    """保留单卡各子集索引记录，不将派生 SQLite 称为 PDF-4+ 原库。"""
    from highscore_database.jade_index import validate_index_card
    try:
        validate_index_card(card)
        code = card['code']; raw = card['database_raw']; prefix = f'original-data/{code}/'
        payloads = {prefix + 'index-snapshot-manifest.json':
                    base64.b64decode(raw['index_snapshot']['manifest_base64'], validate=True)}
        for record in raw['records']:
            for kind in ('xid', 'sid', 'rid') + tuple(kind for kind in ('zid', 'cid', 'lid') if kind in record):
                name = prefix + record['subset'] + '-' + kind.upper() + '.bin'
                if name in payloads:
                    raise ValueError('重复的索引记录')
                payloads[name] = base64.b64decode(record[kind]['base64'], validate=True)
        payloads[prefix + 'record.json'] = _json_bytes({
            'representation': 'jade_index_record_extraction', 'code': code,
            'database_id': card['database_id'], 'database_sha256': card['database_sha256'],
            'source': card['source'], 'complete_index_peak_table': True,
            'complete_original_peak_table': False, 'possible_truncation': card['possible_truncation'],
            'database_raw': raw,
            'binary_fields': {name.rsplit('/', 1)[1]: {'bytes': len(data),
                             'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in payloads.items()},
        })
        return payloads
    except Exception as error:
        raise ArchiveError('原始索引记录或峰表复验失败。') from error


def _structure_original_data(card):
    """单结构的原始记录与设置字节；不补造粉末峰表或整库快照。"""
    from highscore_database.jade_structure import validate_structure_card
    try:
        validate_structure_card(card)
        code = card['code']; raw = card['database_raw']; prefix = f'original-data/{code}/'
        payloads = {prefix + 'structure-snapshot-manifest.json':
                    base64.b64decode(raw['snapshot']['manifest_base64'], validate=True)}
        for kind in ('csd', 'csa', 'csc', 'sgc', 'sym'):
            payloads[prefix + kind.upper() + '.bin'] = base64.b64decode(raw['records'][kind]['base64'], validate=True)
        payloads[prefix + 'record.json'] = _json_bytes({
            'representation': 'jade_structure_record_extraction', 'code': code,
            'database_id': card['database_id'], 'database_sha256': card['database_sha256'],
            'source': card['source'], 'site_count': card['site_count'],
            'structure': card['structure'], 'database_raw': raw,
            'binary_fields': {name.rsplit('/', 1)[1]: {'bytes': len(data),
                             'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in payloads.items()},
        })
        return payloads
    except Exception as error:
        raise ArchiveError('原始晶体结构、原子坐标或对称操作复验失败。') from error


def native_file(root,card):
    """仅接受运维私有登记的附件，绑定库指纹、卡片和原生解析核心数据。"""
    try:
        identity=card.get('database_id');code=card['code']
        if not isinstance(identity,str) or re.fullmatch(r'hsrdb-[a-f0-9]{48}',identity) is None:return None
        root=Path(root).resolve();base=root/'output/web/native-cards'/identity/code
        for path in (base,*base.parents):
            if path==root:break
            if path.is_symlink():raise ValueError()
        manifest_path=base/'manifest.json';target=base/(code+'.rtf')
        if not manifest_path.exists() and not target.exists():return None
        if manifest_path.is_symlink() or target.is_symlink() or manifest_path.stat().st_size>1024*1024:raise ValueError()
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if (manifest['kind']!='highscore_native_export' or manifest['database_id']!=identity
                or manifest['database_sha256']!=card['database_sha256'] or manifest['code']!=code
                or not 0<target.stat().st_size<=MAX_NATIVE_BYTES):raise ValueError()
        payload=target.read_bytes()
        if len(payload)>MAX_NATIVE_BYTES or not payload.startswith(b'{\\rtf') or hashlib.sha256(payload).hexdigest()!=manifest['rtf_sha256']:raise ValueError()
        native=manifest['native_core']
        compact=lambda value:re.sub(r'\s+','',value or '')
        if (native['code']!=code or native['phase_name']!=card['phase_name']
                or compact(native['chemical_formula'])!=compact(card['chemical_formula'])
                or native['space_group']!=card['space_group'] or len(native['peaks'])!=card['peak_count']):raise ValueError()
        for original,current in zip(native['peaks'],card['peaks']):
            if (format(float(current['d_angstrom']),'.5f')!=original['d_angstrom']
                    or float(current['intensity_percent'])!=float(original['intensity_percent'])
                    or any(current[key]!=original[key] for key in ('h','k','l'))):raise ValueError()
            if card.get('wavelength_angstrom')==manifest.get('wavelength_angstrom') and current.get('two_theta_deg') is not None:
                if current['two_theta_deg']!=original['two_theta_deg']:raise ValueError()
        return payload
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise ArchiveError('原生附件的来源、内容摘要或卡片身份复验失败。') from None


def _current_report(card, payloads, receipt, *, root=None):
    """仅从v3单卡原字节生成新的下载副本，原归档与邮件文件始终保留。"""
    from highscore_database.reporting import REPORT_VERSION, report_fields, report_text, report_rtf
    verification = card.get('verification', {})
    if (not isinstance(verification, dict) or verification.get('report_version') not in (3, 4, 5)
            or verification.get('method') not in ('sqlite_readonly_database_report',
                'jade_index_readonly_report', 'jade_structure_readonly_report')):
        return card, None
    if (card.get('data_type') not in ('jade_index_data', 'jade_structure_data')
            and not card.get('database_raw', {}).get('source_records')):
        return card, None  # 更早缺少物理表快照的旧档保留，不能伪造完整来源。
    try:
        from highscore_database.validation import refresh_card
        from highscore_downloader.archive import _check_card, _csv_bytes
        from_version = verification['report_version']
        settings = None
        if card.get('data_type') == 'jade_index_data':
            from highscore_database.exporter import load_report_settings
            settings = load_report_settings(root or Path(__file__).resolve().parents[1])
        current = refresh_card(card, report_settings=settings)
        current['verification'] = {**verification, 'report_version': REPORT_VERSION}
        current['report_fields'] = report_fields(current)
        current['raw_text'] = report_text(current)
        current['report_upgrade'] = {'from_version': from_version,
            'original_json_sha256': hashlib.sha256(payloads[card['code'] + '.json']).hexdigest(),
            'source_archive_unchanged': True}
        _check_card(current)
        code = current['code']
        original = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
        payloads.update({code + '.json': _json_bytes(current), code + '.txt': current['raw_text'].encode('utf-8'),
                         code + '.rtf': report_rtf(current), code + '.csv': _csv_bytes(current)})
        if code + '.cif' in payloads:
            from highscore_database.jade_structure import structure_cif
            if payloads[code + '.cif'] != structure_cif(current).encode('utf-8'):
                raise ValueError()
        proof = {'code': code, 'source': current['source'], 'from_version': from_version, 'to_version': REPORT_VERSION,
                 'source_archive_unchanged': True, 'original_files_sha256': original,
                 'rendered_files_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
                 'archive_receipt_sha256': hashlib.sha256(_json_bytes(receipt)).hexdigest()}
        return current, proof
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ArchiveError('历史报告原始记录重建或新格式复验失败，原归档保留。') from None


def download_zip(root,relative,expected,*,upgrades=None,job_id=None):
    content=checked_zip(root,relative,expected)
    receipts=validate_directory(root/relative,expected)
    output=io.BytesIO();total=0
    with zipfile.ZipFile(io.BytesIO(content)) as source,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for receipt in receipts:
            code=receipt['code'];prefix='' if len(receipts)==1 else code+'/'
            payloads={code+'.'+kind:source.read(item['path']) for kind,item in receipt['files'].items()}
            card=json.loads(payloads[code+'.json'].decode('utf-8'))
            correction = None
            if job_id is not None and card.get('data_type') == 'jade_index_data':
                from .index_corrections import corrected_report
                correction = corrected_report(root, job_id, card, payloads, source.read(receipt['receipt_path']))
            if correction is not None:
                card, payloads, rendering = correction
                # 已审计的ZID纠正仍保留来源证明，下载副本继续应用当前峰表标准。
                card, peak_rendering = _current_report(card, payloads, receipt, root=root)
                if peak_rendering is not None:
                    peak_rendering['source_correction'] = rendering
                    rendering = peak_rendering
            else:
                card, rendering = _current_report(card, payloads, receipt, root=root)
            raw_names={f'original-data/{code}/'+name:code+'-'+label for name,label in (
                ('Lines.bin','Lines.bin'),('HKL.bin','HKL.bin'),
                ('LinesI.bin','LinesI.bin'),
                ('record.json','原始记录.json'),('source-records.sqlite3','原始记录.sqlite3'))}
            if card.get('data_type') == 'jade_index_data':
                raw_names = {
                    f'original-data/{code}/index-snapshot-manifest.json': code + '-索引快照清单.json',
                    f'original-data/{code}/record.json': code + '-原始索引记录.json',
                }
                for record in card['database_raw']['records']:
                    for kind in ('XID', 'SID', 'RID') + tuple(kind.upper() for kind in ('zid', 'cid', 'lid') if kind in record):
                        name = record['subset'] + '-' + kind + '.bin'
                        raw_names[f'original-data/{code}/' + name] = code + '-' + name
            if card.get('data_type') == 'jade_structure_data':
                raw_names = {
                    f'original-data/{code}/structure-snapshot-manifest.json': code + '-结构快照清单.json',
                    f'original-data/{code}/record.json': code + '-原始结构记录.json',
                    **{f'original-data/{code}/{kind}.bin': code + '-' + kind + '.bin'
                       for kind in ('CSD', 'CSA', 'CSC', 'SGC', 'SYM')},
                }
            for name,payload in original_data(card).items():
                if name not in raw_names:raise ArchiveError('原始记录包含尚未支持的下载文件。')
                payloads[raw_names[name]]=payload
            native=native_file(root,card)
            if native is not None:payloads[code+'-HighScore-original.rtf']=native
            manifest={'download_layout_version':1,'code':code,'source':receipt['source'],
                      'peak_count':receipt['peak_count'],'files':{
                          name:{'path':prefix+name,'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
                          for name,payload in payloads.items()},
                      'archive_receipt_sha256':hashlib.sha256(source.read(receipt['receipt_path'])).hexdigest(),
                      'archive_receipt':receipt}
            if rendering is not None:
                # 原始回执实际字节摘要仍采用归档本身，不以重新排版的JSON代替。
                rendering['archive_receipt_sha256'] = manifest['archive_receipt_sha256']
                manifest['report_rendering'] = rendering
                if upgrades is not None:
                    upgrades.append(rendering)
            payloads[code+'-回执.json']=_json_bytes(manifest)
            total+=sum(len(value) for value in payloads.values())
            if total>100*1024*1024:raise ArchiveError('下载包超过100MB。')
            for name,payload in sorted(payloads.items()):archive.writestr(prefix+name,payload)
    return output.getvalue()


def selected_native(root,relative,expected,code):
    receipts=validate_directory(root/relative,expected)
    for receipt in receipts:
        if receipt['code']==code:
            card=json.loads((root/relative/receipt['files']['json']['path']).read_text(encoding='utf-8'))
            return native_file(root,card)
    return None


def selected_cif(root,relative,expected,code):
    root = Path(root).resolve()
    receipts = validate_directory(root / relative, expected)
    for receipt in receipts:
        if receipt['code'] == code:
            if (receipt.get('export_method') != 'jade_structure_readonly_report'
                    or 'cif' not in receipt['files']):
                return None
            card = json.loads((root / relative / receipt['files']['json']['path']).read_text(encoding='utf-8'))
            if card.get('data_type') != 'jade_structure_data':
                raise ArchiveError('CIF 文件与晶体结构类型不一致。')
            return (root / relative / receipt['files']['cif']['path']).read_bytes()
    return None
