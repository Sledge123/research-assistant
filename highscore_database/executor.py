"""Linux 数据库执行器：持久归档复验、HTTPS 租约和独立邮件状态。"""
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import zipfile

from highscore_downloader.archive import ArchiveStore,ArchiveError
from highscore_downloader.numbers import parse_code,parse_codes
from highscore_downloader.mail_settings import validate_email
from highscore_worker.client import WorkerClient
from highscore_worker.config import WorkerError
from highscore_worker.verification import VerificationMailer,atomic_json
from .reader import load_reader,DatabaseError,CardMissing,QueryTooBroad
from .exporter import DatabaseExporter,METHOD,ORIGIN,load_report_settings
from .reporting import REPORT_DIRECTORY,PREVIOUS_REPORT_DIRECTORIES

SELECTED_SOURCE_DIRECTORY='sources'


def isolation_root(root,user_id,database_id,*,create=True):
    # 使用 128 位目录指纹，避免 Windows 离线测试超过旧式 MAX_PATH。
    digest=lambda text:hashlib.sha256(str(text).encode('utf-8')).hexdigest()[:32]
    path=Path(root).resolve()/'output/worker/users'/digest(user_id)/digest(database_id)
    for part in (path,*path.parents):
        if part==Path(root).resolve():break
        if part.is_symlink():raise WorkerError('用户输出目录不允许符号链接。')
    if not path.resolve().is_relative_to(Path(root).resolve()):raise WorkerError('用户输出目录超出项目。')
    if create:path.mkdir(parents=True,exist_ok=True)
    return path


def report_root(base):
    target=Path(base)/REPORT_DIRECTORY
    if target.is_symlink():raise WorkerError('报告版本目录不允许符号链接。')
    target.mkdir(parents=True,exist_ok=True)
    return target


def selected_report_root(root,reader,selected):
    """同编号可跨任务选择不同来源；整个批次按明确的编号/来源绑定隔离。"""
    bindings=[]
    for item in selected:
        code=item.get('code')
        if parse_code(code).canonical_hint!=code:
            raise DatabaseError('来源隔离要求完整候选编号。')
        source_reader=reader.for_source(item['source']) if hasattr(reader,'for_source') else reader
        if source_reader.source!=item['source']:
            raise DatabaseError('来源隔离身份与候选不一致。')
        bindings.append({'code':code,'source':item['source'],'database_id':source_reader.database_id})
    bindings.sort(key=lambda item:(item['code'],item['source'],item['database_id']))
    identity=hashlib.sha256(json.dumps(bindings,ensure_ascii=False,sort_keys=True,
                                      separators=(',',':')).encode('utf-8')).hexdigest()[:32]
    directory=Path(root)/SELECTED_SOURCE_DIRECTORY;target=directory/identity
    if directory.is_symlink() or target.is_symlink():
        raise WorkerError('来源隔离目录不允许符号链接。')
    target.mkdir(parents=True,exist_ok=True)
    return target


def archive_roots(base):
    """只读列出既有版本及新版来源绑定根；旧路径、原始映射均保留。"""
    roots=[Path(base),*[Path(base)/name for name in PREVIOUS_REPORT_DIRECTORIES],Path(base)/REPORT_DIRECTORY]
    for root in list(roots):
        directory=root/SELECTED_SOURCE_DIRECTORY
        if root.is_symlink() or directory.is_symlink():
            raise WorkerError('历史报告目录不允许符号链接。')
        if not directory.exists():continue
        for target in sorted(directory.iterdir()):
            if not re.fullmatch(r'[a-f0-9]{32}',target.name):continue
            if target.is_symlink():raise WorkerError('来源隔离目录不允许符号链接。')
            if target.is_dir():roots.append(target)
    return roots


def package_archive(root,results):
    root=Path(root).resolve();store=ArchiveStore(root);stream=io.BytesIO();seen=set()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
        for result in results:
            if result.get('status') not in {'success','skipped'}:
                raise ArchiveError('完整上传不允许混入失败或待处理卡片。')
            receipt=store.validate(result['receipt_path'])
            if receipt['code'] in seen:raise ArchiveError('归档上传不允许重复卡片。')
            seen.add(receipt['code'])
            for name in [receipt['receipt_path'],*(item['path'] for item in receipt['files'].values())]:
                archive.writestr(name,(root/name).read_bytes())
            # 防止打包读取期间源产物变化。
            if store.validate(result['receipt_path'])!=receipt:
                raise ArchiveError('归档在上传打包时发生变化。')
    if not seen:raise ArchiveError('无已校验卡片可上传。')
    if len(stream.getbuffer())>40*1024*1024:raise ArchiveError('归档上传包超过 40 MB。')
    return base64.b64encode(stream.getvalue()).decode('ascii')


@contextmanager
def process_lock(root):
    target=Path(root)/'output/worker/.lock';target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('a+b') as stream:
        if not stream.tell():stream.write(b'0');stream.flush()
        try:
            if os.name=='nt':
                import msvcrt
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise WorkerError('同一项目的另一执行器正在运行，未重复领取任务。') from None
        try:yield
        finally:
            if os.name=='nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def _mail_status(mail):
    return {'sent':'submitted','unknown':'unknown','failed':'failed','stopped':'not_started',
            'prepared':'pending','skipped':'not_started'}.get(mail.get('status'),'unknown')


class JobHandler:
    def __init__(self,config,*,reader_factory=None,mail_settings_loader=None,mail_factory=None,
                 verification_factory=None,runner_factory=None,reader=None):
        self.config=config
        self._reader=reader
        self.reader_factory=reader_factory or self._verified_reader
        self.mail_settings_loader=mail_settings_loader
        self.mail_factory=mail_factory
        self.verification_factory=verification_factory or VerificationMailer

    def _verified_reader(self,stop):
        # 大库只在启动时完整计算指纹；每次只读连接仍核对文件大小和 mtime。
        if self._reader is None:self._reader=load_reader(self.config.root,stop_event=stop)
        self._reader.stop_event=stop
        return self._reader

    def _settings(self):
        if self.mail_settings_loader:return self.mail_settings_loader(self.config.root)
        from .mail_config import load_settings
        return load_settings(self.config.root)

    def _mailer(self,root,stop,before_submit):
        if self.mail_factory:
            service=self.mail_factory(root,self._settings(),stop_event=stop)
        else:
            from highscore_downloader.mailer import MailService
            service=MailService(root,self._settings(),stop_event=stop)
        service.submission_guard=before_submit
        return service

    @staticmethod
    def _matching_mail(service,results,recipient):
        """只接续完整指纹相同的投递；unknown 不会被自动重发。"""
        desired={service._snapshot(item['receipt_path'],recipient)[0]['fingerprint'] for item in results}
        with service._locked():
            jobs=service._jobs()
        matches=[item for item in jobs if item['recipient']==recipient and
                 {card['fingerprint'] for card in item['cards']}==desired]
        return max(matches,key=lambda item:item['created_at']) if matches else None

    @staticmethod
    def _mapping(root,job_id):
        digest=hashlib.sha256(job_id.encode('utf-8')).hexdigest()
        # 新来源子根加深目录，用 128 位任务摘要保持 Windows 路径兼容；旧映射名不改。
        if root.parent.name==SELECTED_SOURCE_DIRECTORY:digest=digest[:32]
        return root/'output/cloud-jobs'/(digest+'.json')

    def _original_root(self,base,job_id):
        if not isinstance(job_id,str) or not job_id:raise WorkerError('原任务编号无效。')
        roots=archive_roots(base)
        if any(path.is_symlink() for path in roots):raise WorkerError('报告目录不允许符号链接。')
        matches=[root for root in roots if self._mapping(root,job_id).is_file()]
        if len(matches)>1:
            current=json.loads(self._mapping(matches[-1],job_id).read_text(encoding='utf-8'))
            proof=current.get('supersedes_mappings',{})
            if not isinstance(proof,dict):raise WorkerError('报告迁移映射无效。')
            verified=True
            for previous in matches[:-1]:
                name='legacy' if previous==base else previous.name
                expected=proof.get(name)
                if len(matches)==2 and previous==base:expected=expected or current.get('supersedes_mapping_sha256')
                verified=verified and expected==hashlib.sha256(self._mapping(previous,job_id).read_bytes()).hexdigest()
            if verified:return matches[-1]
        if len(matches)!=1:raise WorkerError('原任务归档版本缺失或不唯一，禁止发信。')
        return matches[0]

    def _uncertain_legacy_mail(self,root,results,recipient,stop,*,reader=None,user_id=None,include_submitted=False):
        versions=(*PREVIOUS_REPORT_DIRECTORIES,REPORT_DIRECTORY)
        version_root=root.parent.parent if root.parent.name==SELECTED_SOURCE_DIRECTORY else root
        base=version_root.parent if version_root.name in versions else version_root
        codes={item['actual_code'] for item in results}
        bases=[base]
        if reader is not None and user_id is not None:
            identities=[item.database_id for item in getattr(reader,'readers',[])]
            identities.extend(getattr(reader,'legacy_database_ids',[]))
            bases.extend(isolation_root(self.config.root,user_id,item.database_id)
                         for item in getattr(reader,'readers',[]))
            bases.extend(isolation_root(legacy,user_id,identity,create=False)
                         for legacy in getattr(reader,'legacy_roots',[]) for identity in identities)
        blocked={'unknown','sending'}|({'sent','prepared'} if include_submitted else set())
        for previous in [path for item in bases for path in archive_roots(item)]:
            if previous==root or not (previous/'output/mail').exists():continue
            if previous.is_symlink():raise WorkerError('历史报告目录不允许符号链接。')
            service=self._mailer(previous,stop,lambda:False)
            # 历史输出以只读挂载访问；原账本使用原子文件，不能在此创建锁或恢复 sending。
            jobs=[]
            for directory in sorted(service._path('output/mail').iterdir()):
                if directory.name.startswith('.') or not directory.is_dir():continue
                if not service._path(f'output/mail/{directory.name}/job.json').is_file():continue
                job=service._read_job(directory.name)
                if job['status'] in {'sent','unknown','sending'} or job['status'] in blocked:
                    service._validate_package(job,check_source=False)
                jobs.append(job)
            if any(item['recipient']==recipient and item['status'] in blocked
                   and codes.intersection(card['code'] for card in item['cards']) for item in jobs):return True
        return False

    def __call__(self,job,stop_event,progress):
        kind=job.get('kind');payload=job.get('payload')
        if not isinstance(payload,dict):return self.failure('任务内容格式无效。',kind=kind)
        if kind!='verification_mail' and job.get('database_id')!=self.config.database_id:
            return self.failure('任务数据库指纹与本机数据库不一致，未执行。',kind=kind)
        try:
            before_submit=getattr(progress,'before_submit',lambda:None)
            if kind=='verification_mail':
                if validate_email(payload.get('to'))!=validate_email(job.get('email')):
                    raise WorkerError('验证邮件收件人与任务账号不一致。')
                mail=self.verification_factory(self.config.root,self._settings(),self.config.public_url,
                     submission_guard=before_submit).send(
                     job['id'],payload,stop_event)
                status=_mail_status(mail)
                return {'state':'cancelled' if stop_event.is_set() and status!='submitted' else
                        'succeeded' if status=='submitted' else 'needs_attention',
                        'result':{'mail_status':status,'message':mail['message']}}
            reader=self.reader_factory(stop_event)
            if reader.database_id!=self.config.database_id:
                raise DatabaseError('本机数据库内容指纹与执行器配置不一致，未执行。')
            if kind=='search':
                progress('正在只读检索数据库原文字段')
                content=payload.get('content','diffraction')
                if content not in ('diffraction','structure'):raise DatabaseError('检索内容类型无效。')
                if payload.get('search_version') == 2:
                    if callable(getattr(reader, 'browse_content', None)):
                        candidates=reader.browse_content(payload.get('mode'),payload.get('query'),content)
                    elif callable(getattr(reader, 'browse', None)) and getattr(reader,'content_type','diffraction')==content:
                        candidates=reader.browse(payload.get('mode'),payload.get('query'))
                    else:
                        raise DatabaseError('当前执行器尚未支持完整候选检索，请联系管理员。')
                    # 预留完整回执、租约字段及 JSON 标点；超出真实上传限制时明确失败，不截断。
                    if len(json.dumps(candidates, ensure_ascii=True, allow_nan=False).encode('utf-8')) > 56*1024*1024-4096:
                        raise QueryTooBroad('完整候选快照超出上传限制，请细化检索条件；未截断候选。')
                elif hasattr(reader,'search_content'):
                    candidates=reader.search_content(payload.get('mode'),payload.get('query'),content)
                elif getattr(reader,'content_type','diffraction')==content:
                    candidates=reader.search(payload.get('mode'),payload.get('query'))
                else:raise DatabaseError('所选内容的检索数据尚未配置。')
                return {'state':'succeeded','result':{'candidates':candidates,
                        'message':f'检索完成，共 {len(candidates)} 个候选。'}}
            root=isolation_root(self.config.root,job['user_id'],reader.database_id)
            if kind=='archive':return self.archive(job,reader,report_root(root),stop_event,progress)
            if kind=='mail_retry':return self.mail_retry(job,reader,self._original_root(root,payload.get('original_job_id','')),
                                                         stop_event,before_submit)
            raise WorkerError('任务类型尚未支持。')
        except (ValueError,ArchiveError,OSError) as error:
            # 不向接口透出数据库绝对路径、远端回应或任何凭据。
            text=str(error) if isinstance(error,(DatabaseError,WorkerError)) else '任务未完成，请检查项目内配置和归档回执。'
            return self.failure(text,kind=kind,cancelled=stop_event.is_set())

    @staticmethod
    def failure(message,*,kind=None,cancelled=False):
        result={'message':message[:1000]}
        if kind in {'mail_retry','verification_mail'}:result['mail_status']='not_started'
        return {'state':'cancelled' if cancelled else 'needs_attention','result':result}

    def archive(self,job,reader,root,stop,progress):
        payload=job['payload'];requests=parse_codes(payload.get('codes',''))
        selected=payload.get('selected')
        if selected:
            sources={item.source for item in getattr(reader,'readers',[reader])}
            if len(selected)!=len(requests) or any(item.get('source') not in sources for item in selected):
                raise DatabaseError('选中候选的数据库来源或数量不一致。')
            if payload.get('search_version')==2:
                root=selected_report_root(root,reader,selected)
        exporter=DatabaseExporter(root,reader,report_settings=load_report_settings(self.config.root));results=[]
        for index,request in enumerate(requests,1):
            if stop.is_set():break
            progress(f'正在生成归档报告（{index}/{len(requests)}）')
            try:
                active_exporter=exporter
                if selected and hasattr(reader,'for_source'):
                    matches=[item for item in selected if request.matches(item.get('code',''))]
                    if len(matches)!=1:raise DatabaseError('候选快照与请求编号不一致。')
                    active_exporter=DatabaseExporter(root,reader.for_source(matches[0]['source']),
                                                    report_settings=load_report_settings(self.config.root))
                receipt=active_exporter.export(request.original)
                if selected:
                    expected=[item for item in selected if item.get('code')==receipt['code']]
                    if len(expected)!=1 or expected[0].get('source')!=receipt.get('source'):
                        raise DatabaseError('报告身份与候选快照不一致。')
                message=({'jade_structure_readonly_report':'晶体结构五格式、原子坐标和对称操作复验通过。',
                          'jade_index_readonly_report':'索引报告四格式和索引内峰记录复验通过。'}
                         .get(receipt.get('export_method'),'数据库报告四格式和峰表复验通过。'))
                results.append({'code':request.query,'input_code':request.original,'actual_code':receipt['code'],
                                'source':receipt['source'],'status':'success','message':message,
                                'receipt_path':receipt['receipt_path'],'peak_count':receipt['peak_count']})
            except CardMissing:
                results.append({'code':request.query,'status':'not_found','message':'未发现该卡片信息'})
            except (DatabaseError,ArchiveError):
                results.append({'code':request.query,'status':'failed','message':'数据库字段或报告数据校验失败，已暂停。'})
                break
        complete=len(results)==len(requests) and all(item['status']=='success' for item in results)
        successes=[item for item in results if item['status']=='success']
        if not successes:
            return {'state':'cancelled' if stop.is_set() else 'needs_attention','result':{'results':results,
                    'archive_status':'failed',
                    'mail_status':'not_started' if payload.get('send_mail') else 'not_requested',
                    'message':'本批没有通过完整校验的卡片；未提交邮件。'}}
        encoded=package_archive(root,successes)
        mapping={'database_id':reader.database_id,'results':successes,'mail_job_id':None,
                 'recipient_sha256':hashlib.sha256(validate_email(job['email']).encode('utf-8')).hexdigest()}
        target=self._mapping(root,job['id']);atomic_json(target,mapping)
        result={'results':results,'archive_status':'success' if complete else 'partial','mail_status':'not_requested',
                'message':'数据库报告完整归档成功（非 HighScore 原生导出）。' if complete else
                           '本批部分卡片通过完整校验，失败项未附；数据库报告已保留。'}
        if payload.get('send_mail'):
            if stop.is_set():result.update(mail_status='not_started',message='归档完成；任务已停止，未开始邮件。')
            else:
                attempted=False
                try:
                    if self._uncertain_legacy_mail(root,successes,validate_email(job['email']),stop,
                                                   reader=reader,user_id=job['user_id']):
                        raise WorkerError('历史报告存在状态不确定的投递，禁止因版式升级而自动重发。')
                    if payload.get('search_version')==2 and self._uncertain_legacy_mail(root,successes,
                            validate_email(job['email']),stop,reader=reader,user_id=job['user_id'],include_submitted=True):
                        result.update(mail_status='not_started',message='历史已有相关投递记录；新报告已保留，请在任务中明确发信。')
                    else:
                        service=self._mailer(root,stop,getattr(progress,'before_submit',lambda:None))
                        mail=service.prepare(successes,job['email'])
                        if mail.get('status')=='skipped':
                            mail=self._matching_mail(service,successes,validate_email(job['email'])) or mail
                        if mail.get('job_id'):
                            mapping['mail_job_id']=mail['job_id'];atomic_json(target,mapping)
                            attempted=True
                            mail=service.send(mail['job_id'])
                        result.update(mail_status=_mail_status(mail),message=mail.get('message','邮件尚未提交。'))
                        if not complete:result['message']='本批部分归档，失败项未附；'+result['message']
                except WorkerError:
                    result.update(mail_status='unknown',message='历史报告邮件状态不确定；新版已归档，请核对邮箱后明确重发。')
                except Exception:
                    result.update(mail_status='unknown' if attempted else 'failed',
                                  message='数据库报告已归档；邮件提交状态不确定，禁止自动重发。' if attempted
                                  else '数据库报告已归档；邮件配置或投递未完成。')
        return {'state':'cancelled' if stop.is_set() and result['mail_status']!='submitted' else
                'succeeded' if complete and result['mail_status'] in
                {'not_requested','submitted'} else 'needs_attention',
                'result':result,'artifact_zip_base64':encoded}

    def mail_retry(self,job,reader,root,stop,before_submit=lambda:None):
        payload=job['payload'];original=payload.get('original_job_id')
        if not isinstance(original,str) or payload.get('force_new_delivery') is not True:
            raise WorkerError('重新发信必须显式创建新的投递记录。')
        try:
            mapping=json.loads(self._mapping(root,original).read_text(encoding='utf-8'))
            recipient=validate_email(job['email'])
            if (mapping['database_id']!=reader.database_id or mapping['recipient_sha256']!=hashlib.sha256(recipient.encode('utf-8')).hexdigest()
                    or not isinstance(mapping['results'],list)):raise ValueError()
            results=mapping['results'];package_archive(root,results)
            requests=parse_codes(payload.get('codes',''))
            actual=[item['actual_code'] for item in results]
            if (len(actual)>len(requests) or any(sum(request.matches(code) for request in requests)!=1
                                                  for code in actual)):
                raise ValueError()
        except (OSError,ValueError,TypeError,KeyError):
            raise WorkerError('原任务的拥有者、数据库或归档账本无法复验，禁止发信。') from None
        service=self._mailer(root,stop,before_submit)
        if (self._uncertain_legacy_mail(root,results,recipient,stop,reader=reader,user_id=job['user_id'])
                and payload.get('confirm_unknown') is not True):
            return {'state':'needs_attention','result':{'mail_status':'unknown',
                    'message':'历史报告邮件状态不确定，需要明确确认后创建新投递。'}}
        previous=None
        with service._locked():
            # MailService 本身复验 package 与源归档，并把中断 sending 恢复为 unknown。
            jobs=service._jobs()
            if mapping['mail_job_id']:
                previous=next((item for item in jobs if item['job_id']==mapping['mail_job_id']),None)
                if previous is None:raise WorkerError('原投递记录缺失，禁止发信。')
        # 进程可能在新投递持久化后、映射更新前退出；此时仍以最新相同指纹账本为准。
        latest=self._matching_mail(service,results,recipient)
        if latest is not None:previous=latest
        if previous is None:
            previous=service.prepare(results,recipient)
            if not previous.get('job_id'):raise WorkerError('没有可创建的新投递记录，请检查原归档账本。')
            mapping['mail_job_id']=previous['job_id'];atomic_json(self._mapping(root,original),mapping)
            mail=service.send(previous['job_id'])
            status=_mail_status(mail)
            return {'state':'cancelled' if stop.is_set() and status!='submitted' else
                    'succeeded' if status=='submitted' else 'needs_attention',
                    'result':{'mail_status':status,'message':mail.get('message','邮件尚未提交。')}}
        if previous['status'] in {'unknown','sending'} and payload.get('confirm_unknown') is not True:
            return {'state':'needs_attention','result':{'mail_status':'unknown','message':'原投递状态不确定，需要明确确认后创建新投递。'}}
        mail=service.send(previous['job_id'],force=True)
        if mail.get('job_id') and mail['job_id']!=mapping['mail_job_id']:
            mapping['mail_job_id']=mail['job_id'];atomic_json(self._mapping(root,original),mapping)
        status=_mail_status(mail)
        return {'state':'cancelled' if stop.is_set() and status!='submitted' else
                'succeeded' if status=='submitted' else 'needs_attention',
                'result':{'mail_status':status,'message':mail.get('message','邮件尚未提交。')}}


class Worker:
    def __init__(self,config,*,client=None,handler=None,mutex_factory=None):
        self.config=config;self.client=client or WorkerClient(config);self.handler=handler or JobHandler(config)
        self.mutex_factory=mutex_factory or (lambda:process_lock(config.root))
        self.stop_event=threading.Event()

    def run_once(self):
        with self.mutex_factory():
            claimed=self.client.claim();job=claimed.get('job')
            if job is None:return False
            job_id=job.get('id');token=claimed.get('lease_token');seconds=claimed.get('lease_seconds')
            if (not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',job_id)
                    or not isinstance(token,str) or not token or type(seconds) not in (int,float)
                    or not math.isfinite(seconds) or seconds<=0):
                raise WorkerError('任务租约格式无效，未执行。')
            stop=threading.Event();done=threading.Event();lost=threading.Event();message=['正在执行任务']
            lease_lock=threading.Lock()
            def progress(value):message[0]=' '.join(str(value).split())[:500]
            def checkpoint():
                if self.stop_event.is_set():stop.set()
                if lost.is_set():return False
                try:
                    with lease_lock:
                        response=self.client.heartbeat(job_id,token,message[0])
                    if response.get('cancel_requested'):stop.set()
                except Exception:
                    lost.set();stop.set();return False
                return not stop.is_set()
            def before_submit():
                return checkpoint()
            progress.before_submit=before_submit
            if not checkpoint():
                if lost.is_set():return False
                result={'message':'任务已取消，未开始执行或提交邮件。'}
                if job.get('kind') in {'verification_mail','mail_retry'} or job.get('payload',{}).get('send_mail'):
                    result['mail_status']='not_started'
                self.client.complete(job_id,{'lease_token':token,'state':'cancelled','result':result})
                return True
            def heartbeat():
                while not done.wait(min(15,seconds/4)):
                    if not checkpoint() and lost.is_set():return
            thread=threading.Thread(target=heartbeat,name='highscore-lease',daemon=True);thread.start()
            try:
                if self.stop_event.is_set():stop.set()
                result=self.handler(job,stop,progress)
                done.set();thread.join(timeout=11)
                if lost.is_set():return False
                # completion 前再续租，以服务器确认作为最后一道有效性门槛。
                response=self.client.heartbeat(job_id,token,message[0])
                if response.get('cancel_requested') and result.get('result',{}).get('mail_status')!='submitted':
                    result['state']='cancelled';result['result']['message']='任务已取消；保留已核验归档和实际邮件回执。'
                if lost.is_set():return False
                self.client.complete(job_id,{'lease_token':token,**result})
                return True
            except Exception:
                stop.set()
                raise
            finally:
                done.set();thread.join(timeout=11)

    def run(self):
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(self.config.poll_seconds)
