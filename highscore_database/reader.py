"""只读解析 PDF2 2.3 与显式指纹绑定的 COD 2024 HSRDB。"""
from contextlib import contextmanager
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time

from highscore_downloader.numbers import parse_code, parse_codes


class DatabaseError(ValueError):
    pass


class QueryTooBroad(DatabaseError):
    pass


class CardMissing(DatabaseError):
    pass


REQUIRED = {
    'Properties': {'ID','Key','Value'},
    'general_indexed': {'id','ProductID','XTSLSYS','STATUS','A','B','C','ALPHA','BETA','GAMMA',
                        'A_OVER_C','Z','DM','DX','D','QUALFINAL','IIC','NumberOfElements','XTLASPECTN',
                        'XTLVOL','CTIME','MTIME'},
    'general_text_content': {'docid','c0chemicalformula','c1compoundname','c2mineralname',
                             'c3commonname','c4empiricalformula','c5color'},
    'general_stored': {'pid','CELLED','SPGR','SPGRED','ANX','XTLSG','XTLSGED','Comment','CAS','Lines','HKL'},
    'literature_map': {'doc','pid'},
    'literature_text_content': {'docid','c0volume','c1pages','c2year','c3authors','c4journal','c5referencetype'},
    'Subfiles': {'ID','SubFile'}, 'SubFileRef': {'pid','SubFileID'},
}

PDF2_THOUSAND_INTENSITY_ORIGIN = 'Lines uint16 / 10; 999 denotes 100 percent; raw bytes retained unchanged'


class DatabaseReader:
    content_type = 'diffraction'

    def __init__(self, path, *, timeout=5, stop_event=None, profile=None, cache_directory=None):
        self.path = Path(path).resolve()
        self.cache_directory = cache_directory
        self.stop_event = stop_event or threading.Event()
        self.timeout = float(timeout)
        if not 0 < self.timeout <= 60 or not self.path.is_file() or self.path.is_symlink():
            raise DatabaseError('数据库路径或查询超时配置无效。')
        for suffix in ('-wal','-journal'):
            if Path(str(self.path)+suffix).exists():
                raise DatabaseError('数据库存在未合并的事务文件，禁止忽略事务只读打开。')
        self.signature = self._signature()
        digest = hashlib.sha256()
        with self.path.open('rb') as stream:
            header = stream.read(16)
            if header != b'SQLite format 3\x00':
                raise DatabaseError('只支持已核验的 SQLite HSRDB 文件，不读取未知或加密格式。')
            digest.update(header)
            while chunk := stream.read(1024 * 1024):
                self._check_stop()
                digest.update(chunk)
        self.sha256 = digest.hexdigest()
        self.reader_profile = 'pdf2_2.3'
        self.intensity_scale = 100
        if profile is not None:
            if (not isinstance(profile,dict) or set(profile)!={'format','source','sha256'}
                    or profile.get('format')!='cod2024_hs4x' or profile.get('sha256')!=self.sha256
                    or not isinstance(profile.get('source'),str) or not profile['source'].strip()
                    or len(profile['source'])>300):
                raise DatabaseError('COD 配置必须明确格式、来源，并绑定已核验文件 SHA256。')
            self.reader_profile = profile['format']
            self.intensity_scale = 1000
        with self.connection() as connection:
            for table, columns in REQUIRED.items():
                kind = connection.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
                actual = {row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if kind is None or kind['type'] != 'table' or not columns.issubset(actual):
                    raise DatabaseError('数据库表结构尚未核验，已拒绝解析。')
            rows = connection.execute('SELECT Key,Value FROM Properties').fetchall()
            self.properties = {row['Key']: row['Value'] for row in rows}
            if len(self.properties) != len(rows):
                raise DatabaseError('数据库版本或来源描述尚未核验。')
            if profile is None:
                if (self.properties.get('FormatVersion') != '2.3' or self.properties.get('ID')!='PDF2'
                        or not isinstance(self.properties.get('Description'),str) or not self.properties['Description'].strip()):
                    raise DatabaseError('数据库版本或来源描述尚未核验。')
            elif (self.properties.get('ID')!='CIFDB'
                    or 'LinesI' not in {r['name'] for r in connection.execute('PRAGMA table_info(general_stored)')}):
                raise DatabaseError('COD 元数据或 LinesI 表结构尚未核验。')
            self.pattern_count = connection.execute('SELECT count(*) AS n FROM general_indexed').fetchone()['n']
            if str(self.pattern_count) != self.properties.get('NoOfPatterns'):
                raise DatabaseError('数据库记录数与来源元数据不一致。')
        self.source = profile['source'].strip() if profile is not None else self.properties['Description']
        self.database_id = 'hsrdb-' + self.sha256[:48]

    def _signature(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _check_stop(self):
        if self.stop_event.is_set():
            raise DatabaseError('任务已停止，未继续读取数据库。')

    @contextmanager
    def connection(self):
        self._check_stop()
        if self._signature() != self.signature:
            raise DatabaseError('数据库文件在运行期间发生变化，必须重新核验指纹。')
        connection = None
        deadline = time.monotonic() + self.timeout
        def progress():
            return int(self.stop_event.is_set() or time.monotonic() > deadline)
        try:
            connection = sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=self.timeout)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.set_progress_handler(progress,1000)
            yield connection
            self._check_stop()
            if self._signature() != self.signature:
                raise DatabaseError('数据库读取期间发生变化，结果已丢弃。')
        except sqlite3.Error:
            raise DatabaseError('数据库只读查询失败、已停止或超过时间限制。') from None
        finally:
            if connection is not None:
                connection.close()

    def metadata(self):
        return {'database_id': self.database_id,'sha256':self.sha256,'source':self.source,
                'format_version':self.properties.get('FormatVersion'),'pattern_count':self.pattern_count,
                'reader_profile':self.reader_profile}

    @staticmethod
    def _code(product):
        if not isinstance(product,str) or not re.fullmatch(r'[0-9]{9}',product):
            raise DatabaseError('数据库卡片编号格式未知。')
        return parse_code(product).canonical_hint

    def _candidate(self,row):
        name = row['c1compoundname'] or row['c2mineralname'] or row['c3commonname']
        if not isinstance(name,str) or not name.strip():
            raise DatabaseError('候选缺少物相名称，不能报告有效卡片。')
        formula=row['c0chemicalformula'] or row['c4empiricalformula'] or None
        if formula is not None and not isinstance(formula,str):
            raise DatabaseError('候选化学式的字段类型尚未核验。')
        return {'code':self._code(row['ProductID']), 'phase_name':name,
                'chemical_formula':formula,
                'source':self.source}

    def search(self, mode, query):
        conditions = {'compound_name':'t.c1compoundname = ? COLLATE BINARY',
                      'mineral_name':'t.c2mineralname = ? COLLATE BINARY',
                      'formula':'(t.c0chemicalformula = ? COLLATE BINARY OR t.c4empiricalformula = ? COLLATE BINARY)'}
        if mode not in conditions or not isinstance(query,str) or not query.strip() or len(query)>200 or '\0' in query:
            raise DatabaseError('请输入不超过 200 字的原文物相、矿物名或化学式。')
        query = query.strip()
        values = (query,query) if mode=='formula' else (query,)
        with self.connection() as connection:
            rows = connection.execute('SELECT i.ProductID,t.c1compoundname,t.c2mineralname,t.c3commonname,'
                 't.c0chemicalformula,t.c4empiricalformula FROM general_indexed i '
                 'JOIN general_text_content t ON i.id=t.docid WHERE '+conditions[mode]+' ORDER BY i.ProductID LIMIT 101',values).fetchall()
        if len(rows)>100:
            raise QueryTooBroad('检索结果超过 100 条，请细化条件；未截断为完整结果。')
        candidates=[self._candidate(row) for row in rows]
        if len({(item['code'],item['source']) for item in candidates})!=len(candidates):
            raise DatabaseError('数据库结果包含重复身份或来源歧义，未返回候选。')
        return candidates

    def _formula_batches(self):
        """分批关闭只读连接，避免正规化的CPU时间占用源SQL查询时限。"""
        last = None
        while True:
            with self.connection() as connection:
                condition, values = ('', ()) if last is None else (' WHERE i.id>?', (last,))
                rows = connection.execute('SELECT i.id,t.c0chemicalformula,t.c4empiricalformula FROM general_indexed i '
                    'JOIN general_text_content t ON i.id=t.docid' + condition + ' ORDER BY i.id LIMIT 2000', values).fetchall()
            if not rows:
                return
            yield [(row['id'], (row['c0chemicalformula'], row['c4empiricalformula'])) for row in rows]
            last = rows[-1]['id']

    def _browse_rows(self, condition, values):
        with self.connection() as connection:
            return connection.execute('SELECT i.id,i.ProductID,i.A,i.B,i.C,i.ALPHA,i.BETA,i.GAMMA,'
                't.c1compoundname,t.c2mineralname,t.c3commonname,t.c0chemicalformula,t.c4empiricalformula,'
                's.SPGR,length(s.Lines) AS line_bytes,length(s.HKL) AS hkl_bytes FROM general_indexed i '
                'JOIN general_text_content t ON i.id=t.docid LEFT JOIN general_stored s ON s.pid=i.id WHERE '
                + condition + ' ORDER BY i.ProductID', values).fetchall()

    def browse(self, mode, query):
        """新候选列表接口：全部匹配元数据，旧search原文精确行为保持。"""
        from .formula import chunks, formula_record_ids
        rows = []
        if mode == 'codes':
            try:
                requests = parse_codes(query)
            except ValueError as error:
                raise DatabaseError(str(error)) from None
            for request in requests:
                if request.canonical_hint and not re.fullmatch(r'[0-9]{2}-[0-9]{3}-[0-9]{4}', request.canonical_hint):
                    continue
                condition = 'i.ProductID=?' if request.canonical_hint else 'substr(i.ProductID,3)=?'
                value = (request.canonical_hint or request.query).replace('-', '')
                rows.extend(self._browse_rows(condition, (value,)))
        elif mode == 'formula':
            for batch in chunks(formula_record_ids(self, query)):
                rows.extend(self._browse_rows('i.id IN (' + ','.join('?' for _ in batch) + ')', batch))
        else:
            raise DatabaseError('检索方式必须为编号或化学式。')
        candidates = {}
        seen_ids = set()
        for row in rows:
            if row['id'] in seen_ids:
                continue
            seen_ids.add(row['id'])
            item = self._candidate(row)
            identity = (item['code'], item['source'])
            if identity in candidates:
                raise DatabaseError('数据库候选存在重复卡片身份。')
            valid = isinstance(row['line_bytes'], int) and row['line_bytes'] > 0 and row['line_bytes'] % 6 == 0
            valid = valid and row['hkl_bytes'] in (None, row['line_bytes'])
            item.update(mineral_name=row['c2mineralname'], space_group=row['SPGR'],
                cell_parameters={key.lower(): None if row[key] is None else str(row[key]) for key in ('A','B','C','ALPHA','BETA','GAMMA')},
                peak_count=row['line_bytes'] // 6 if valid else None, site_count=None,
                content_type='diffraction', exportable=valid,
                support_message=None if valid else '源记录缺少完整峰表，暂不能归档。')
            candidates[identity] = item
        return [candidates[key] for key in sorted(candidates)]

    def card(self, code):
        request = parse_code(code)
        with self.connection() as connection:
            condition = 'i.ProductID=?' if request.canonical_hint else 'substr(i.ProductID,3)=?'
            value = request.canonical_hint.replace('-','') if request.canonical_hint else request.query.replace('-','')
            indexed = connection.execute('SELECT i.* FROM general_indexed i WHERE '+condition+' LIMIT 2',(value,)).fetchall()
            if not indexed:
                raise CardMissing('数据库未找到该卡片编号。')
            if len(indexed)!=1:
                raise DatabaseError('编号对应多个来源或完整编号，请明确选择后重试。')
            indexed = dict(indexed[0]); pid=indexed['id']
            texts = connection.execute('SELECT * FROM general_text_content WHERE docid=?',(pid,)).fetchall()
            stored = connection.execute('SELECT * FROM general_stored WHERE pid=?',(pid,)).fetchall()
            if len(texts)!=1 or len(stored)!=1:
                raise DatabaseError('数据库卡片文本、峰表或来源关联不唯一。')
            texts,stored = dict(texts[0]),dict(stored[0])
            literature = [dict(row) for row in connection.execute('SELECT t.* FROM literature_map m '
                         'JOIN literature_text_content t ON m.doc=t.docid WHERE m.pid=? ORDER BY t.docid',(pid,))]
            subfiles = [row['SubFile'] for row in connection.execute('SELECT s.SubFile FROM SubFileRef r '
                       'JOIN Subfiles s ON s.ID=r.SubFileID WHERE r.pid=? ORDER BY s.ID',(pid,))]
            source_records=self._source_records(connection,pid,indexed['ProductID'])
        candidate=self._candidate({**indexed,**texts})
        lines,hkl=stored.pop('Lines'),stored.pop('HKL')
        lines_i=stored.pop('LinesI',None)
        intensity_scale=self._pdf2_intensity_scale(lines) if self.reader_profile=='pdf2_2.3' else self.intensity_scale
        peaks=self._peaks(lines,hkl,intensity_scale=intensity_scale,lines_i=lines_i)
        return {**candidate, 'database_id':self.database_id, 'database_sha256':self.sha256,
                'complete':True,'peak_count':len(peaks),'peaks':peaks,'wavelength_angstrom':None,
                'cell_parameters':{name.lower():None if indexed[name] is None else str(indexed[name])
                                   for name in ('A','B','C','ALPHA','BETA','GAMMA')},
                'space_group':stored['SPGR'],'comments':stored['Comment'],'literature':literature,'subfiles':subfiles,
                'empirical_formula':texts['c4empiricalformula'],
                'database_raw':{'indexed':indexed,'text':texts,'stored':stored,
                  'source_records':source_records,
                  'lines_base64':base64.b64encode(lines).decode('ascii'),
                  'hkl_base64':base64.b64encode(hkl).decode('ascii') if hkl is not None else None,
                  **({'lines_i_base64':base64.b64encode(lines_i).decode('ascii'),
                      'intensity_scale':1000,'reader_profile':self.reader_profile,
                      'source_label_origin':'administrator_config_bound_to_database_sha256',
                      'intensity_origin':'LinesI little-endian float32 / 10; Lines uint16 retained unchanged'}
                     if self.intensity_scale==1000 else {}),
                  **({'intensity_scale':999,'reader_profile':self.reader_profile,
                      'intensity_origin':PDF2_THOUSAND_INTENSITY_ORIGIN}
                     if intensity_scale==999 else {}),
                  'lines_encoding':'little-endian float32 d_angstrom + uint16 intensity; 6 bytes per peak',
                  'hkl_encoding':'little-endian int16 h,k,l; 6 bytes per peak',
                  'peak_order':'report sorted by d descending; database_index preserves original order'}}

    @staticmethod
    def _source_records(connection,pid,product):
        """完整提取卡片关联物理记录；不把全库FTS索引当成该卡片的数据。"""
        canonical=DatabaseReader._code(product)
        def encoded(value):
            if isinstance(value,bytes):
                return {'blob_base64':base64.b64encode(value).decode('ascii'),
                        'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()}
            return value
        queries={
            'Properties':('1=1',()),
            'general_indexed':('id=?',(pid,)),
            'general_text_content':('docid=?',(pid,)),
            'general_stored':('pid=?',(pid,)),
            'literature_map':('pid=?',(pid,)),
            'literature_text_content':('docid IN (SELECT doc FROM literature_map WHERE pid=?)',(pid,)),
            'SubFileRef':('pid=?',(pid,)),
            'Subfiles':('ID IN (SELECT SubFileID FROM SubFileRef WHERE pid=?)',(pid,)),
            'pattern_elements':('pid=?',(pid,)),
            'pattern_strongestlines':('pid=?',(pid,)),
            'general_used':('pid=?',(pid,)),
            'PatternPhase':('ReferenceCode IN (?,?)',(product,canonical)),
            'Phases':('Name IN (SELECT PhaseName FROM PatternPhase WHERE ReferenceCode IN (?,?))',(product,canonical)),
            'PatternScan':('ReferenceCode IN (?,?)',(product,canonical)),
            'Scans':('Name IN (SELECT ScanName FROM PatternScan WHERE ReferenceCode IN (?,?))',(product,canonical)),
            'DeprecatedPatterns':('OldReferenceCode IN (?,?) OR NewReferenceCode IN (?,?)',(product,canonical,product,canonical)),
        }
        tables={};absent=[]
        for table,(condition,values) in queries.items():
            schema=connection.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(table,)).fetchone()
            if schema is None:
                absent.append(table);continue
            if schema['type']!='table':raise DatabaseError('原始记录的关联表类型异常，未导出。')
            # 表名来自固定白名单；值全部参数化。保存原始DDL但绝不执行源DDL。
            rows=connection.execute(f'SELECT * FROM "{table}" WHERE '+condition+' ORDER BY rowid',values).fetchall()
            tables[table]={'create_sql':schema['sql'],
                           'columns':[dict(row) for row in connection.execute(f'PRAGMA table_info("{table}")')],
                           'rows':[{key:encoded(row[key]) for key in row.keys()} for row in rows]}
        return {'format':'sqlite_card_rows_v1','product_id':product,'tables':tables,'absent_tables':absent,
                'excluded_global_storage':['FTS index segments','sqlite_sequence','sqlite_stat1']}

    @staticmethod
    def _pdf2_intensity_scale(lines):
        # 同一 PDF-2 库同时存在百分制和千分编码。仅最大值999是已核验的后者标记。
        # 01-073-2422 的33峰已与原生详情及RTF核对：999=>100%，其余除10。
        if not isinstance(lines,bytes) or not lines or len(lines)%6 or len(lines)>6*100000:
            raise DatabaseError('峰表 BLOB 长度不符合已核验格式。')
        return 999 if max(value for _,value in struct.iter_unpack('<fH',lines))==999 else 100

    @staticmethod
    def _peaks(lines,hkl,*,intensity_scale=100,lines_i=None):
        if (not isinstance(lines,bytes) or not lines or len(lines)%6 or len(lines)>6*100000
                or (hkl is not None and (not isinstance(hkl,bytes) or len(hkl)!=len(lines)))):
            raise DatabaseError('峰表 BLOB 长度或 HKL 对应关系不符合已核验格式。')
        if intensity_scale not in (100,999,1000) or (intensity_scale==1000 and
                (not isinstance(lines_i,bytes) or len(lines_i)!=len(lines)//6*4)):
            raise DatabaseError('COD 原始强度 BLOB 长度或强度刻度无效。')
        if intensity_scale==999 and (lines_i is not None or DatabaseReader._pdf2_intensity_scale(lines)!=999):
            raise DatabaseError('PDF-2 千分强度编码与原始峰表不一致。')
        result=[]
        for index,(distance,intensity) in enumerate(struct.iter_unpack('<fH',lines)):
            if not math.isfinite(distance) or distance<=0 or not 0<=intensity<=intensity_scale:
                raise DatabaseError('峰表 d 间距或相对强度不符合已核验范围，未生成报告。')
            percent=str(intensity)
            if intensity_scale==999:
                normalized=100.0 if intensity==999 else intensity/10
                percent=str(int(normalized)) if normalized.is_integer() else repr(normalized)
            if intensity_scale==1000:
                raw_intensity=struct.unpack_from('<f',lines_i,index*4)[0]
                if (not math.isfinite(raw_intensity) or not 0<=raw_intensity<=1000
                        or abs(raw_intensity-intensity)>1):
                    raise DatabaseError('COD LinesI 与 Lines 的原始强度对应关系无效。')
                normalized=raw_intensity/10
                percent=str(int(normalized)) if normalized.is_integer() else repr(normalized)
            indices=struct.unpack_from('<hhh',hkl,index*6) if hkl is not None else (None,None,None)
            # 在 00-030-0820 原生详情及 RTF 中实测：三项 -128 表示缺失 HKL。
            # 原始 BLOB 仍完整保留，不能把这个缺失标记当成晶面指数。
            if indices==(-128,-128,-128):indices=(None,None,None)
            result.append({'no':None,'h':str(indices[0]) if indices[0] is not None else None,
                           'k':str(indices[1]) if indices[1] is not None else None,
                           'l':str(indices[2]) if indices[2] is not None else None,
                           'd_angstrom':repr(distance),'two_theta_deg':None,
                           'intensity_percent':percent,'database_index':index,
                           'd_float32_le_hex':lines[index*6:index*6+4].hex()})
        result.sort(key=lambda row:float(row['d_angstrom']),reverse=True)
        for index,row in enumerate(result,1):row['no']=str(index)
        return result


class OrderedDatabaseReader:
    """按配置顺序接续；只有明确查无记录才进入下一库。"""
    def __init__(self,readers):
        self.readers=list(readers)
        self.legacy_roots=[]
        self.legacy_database_ids=[]
        if (not 1<=len(self.readers)<=20
                or len({reader.database_id for reader in self.readers})!=len(self.readers)
                or len({reader.source for reader in self.readers})!=len(self.readers)):
            raise DatabaseError('自动检索数据库必须为1至20个不重复的已核验来源。')
        identity=[{'sha256':reader.sha256,'source':reader.source} for reader in self.readers]
        self.sha256=hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        self.database_id='hsauto-'+self.sha256[:48]
        self.source='XRD 自动检索'
        self.stop_event=self.readers[0].stop_event

    @property
    def stop_event(self):return self._stop_event

    @stop_event.setter
    def stop_event(self,value):
        self._stop_event=value
        for reader in self.readers:reader.stop_event=value

    def metadata(self):
        return {'database_id':self.database_id,'sha256':self.sha256,'source':self.source,
                'selection':'first_match','databases':[reader.metadata() for reader in self.readers]}

    def search(self,mode,query):
        return self.search_content(mode,query,'diffraction')

    def search_content(self,mode,query,content):
        if content not in ('diffraction','structure'):
            raise DatabaseError('检索内容类型无效。')
        readers=[reader for reader in self.readers if getattr(reader,'content_type','diffraction')==content]
        if not readers:raise DatabaseError('所选内容的检索数据尚未配置。')
        for reader in readers:
            candidates=reader.search(mode,query)
            if candidates:return candidates
        return []

    def browse(self,mode,query):
        return self.browse_content(mode,query,'diffraction')

    def browse_content(self,mode,query,content):
        if content not in ('diffraction','structure'):
            raise DatabaseError('检索内容类型无效。')
        readers=[reader for reader in self.readers if getattr(reader,'content_type','diffraction')==content]
        if not readers:
            raise DatabaseError('所选内容的检索数据尚未配置。')
        if mode == 'formula':
            from .formula import resolved_formula_query
            query = resolved_formula_query(readers, query)
        result=[];seen=set()
        for reader in readers:
            candidates=reader.browse(mode,query)
            for item in sorted(candidates,key=lambda value:value['code']):
                identity=(item['code'],item['source'])
                if identity in seen:
                    raise DatabaseError('候选列表存在重复卡片身份。')
                if item['source']!=reader.source:
                    raise DatabaseError('候选来源与当前数据库不一致。')
                seen.add(identity);result.append(item)
        return result

    def card(self,code):
        for reader in self.readers:
            try:return reader.card(code)
            except CardMissing:continue
        raise CardMissing('未发现该卡片信息')

    def for_source(self,source):
        for reader in self.readers:
            if reader.source==source:return reader
        raise DatabaseError('候选来源不在当前已核验数据库中。')


def load_reader(root, *, stop_event=None, config_relative='database.local.json', cache_directory=None):
    from highscore_worker.config import project_path
    try:
        data=json.loads(project_path(root,config_relative).read_text(encoding='utf-8-sig'))
        formula_cache=project_path(root,'.cache/formula-index') if cache_directory is None else project_path(root,cache_directory)
        def single(item):
            if not isinstance(item,dict) or set(item) not in ({'path'},{'path','profile'}):
                raise DatabaseError('数据库配置只能包含项目内相对 path 及可选格式 profile。')
            if isinstance(item.get('profile'),dict) and item['profile'].get('format') in {'jade_pdf4_2009_index_v1', 'jade_pdf4_2009_index_v2'}:
                from .jade_index import JadeIndexReader
                return JadeIndexReader(project_path(root,item['path']),stop_event=stop_event,profile=item['profile'],cache_directory=formula_cache)
            if isinstance(item.get('profile'),dict) and item['profile'].get('format')=='jade_icsd_structure_v1':
                from .jade_structure import JadeStructureReader
                return JadeStructureReader(project_path(root,item['path']),stop_event=stop_event,profile=item['profile'],cache_directory=formula_cache)
            return DatabaseReader(project_path(root,item['path']),stop_event=stop_event,profile=item.get('profile'),cache_directory=formula_cache)
        if isinstance(data,dict) and 'databases' in data and set(data).issubset({'databases','legacy_output_roots','legacy_database_ids'}):
            if not isinstance(data['databases'],list) or not 1<=len(data['databases'])<=20:
                raise DatabaseError('自动检索配置必须为有序数据库列表。')
            reader=OrderedDatabaseReader([single(item) for item in data['databases']])
            legacy=data.get('legacy_output_roots',[])
            if not isinstance(legacy,list) or len(legacy)>20:raise DatabaseError('历史输出目录配置无效。')
            reader.legacy_roots=[project_path(root,item) for item in legacy]
            if any(not path.is_dir() for path in reader.legacy_roots):raise DatabaseError('历史输出目录不可读取。')
            identities=data.get('legacy_database_ids',[])
            if (not isinstance(identities,list) or len(identities)>20
                    or any(not isinstance(item,str) or re.fullmatch(r'(?:hsrdb|hsauto|jadeidx|jadestr)-[a-f0-9]{48}',item) is None for item in identities)
                    or len(set(identities))!=len(identities)):
                raise DatabaseError('历史数据库身份配置无效。')
            reader.legacy_database_ids=identities
            return reader
        return single(data)
    except DatabaseError:raise
    except (OSError,TypeError,ValueError):
        raise DatabaseError('数据库配置无法读取或指向项目外路径。') from None
