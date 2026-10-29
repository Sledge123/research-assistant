"""共用网站身份的文献接口；与 XRD 开关、执行锁及邮件互相独立。"""
from typing import Literal
import time

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .database import dumps
from .literature_store import LiteratureError, LiteratureStore

BASE = '/highscore/api'


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)


class Submit(Model):
    title: str = Field(min_length=6,max_length=1000)
    request_key: str = Field(min_length=1,max_length=100)


class Select(Model):
    candidate_id: str = Field(min_length=1,max_length=100)


class Settings(Model):
    enabled: bool


class Claim(Model):
    worker_id: str = Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    accounts_ready: bool


class Lease(Model):
    lease_token: str = Field(min_length=32,max_length=128)


Stage = Literal['fetching','awaiting_fulltext','renewing','verifying','ready','retrying']


class Heartbeat(Lease):
    stage: Stage | None = None


class ReminderClaim(Model):
    worker_id: str = Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')


class ReminderComplete(Lease):
    state: Literal['submitted','failed','unknown']


class Candidate(Model):
    id: str = Field(min_length=1,max_length=100)
    title: str = Field(min_length=6,max_length=1000)
    doi: str | None = Field(default=None,max_length=300)
    journal: str | None = Field(default=None,max_length=500)
    year: int | None = Field(default=None,ge=1500,le=2200)


class SourceReset(Model):
    previous_request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    source_revision: int = Field(ge=0)
    reason: Literal['expired','supplement_only','finished_without_file']
    evidence_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Update(Lease):
    state: Literal['waiting','needs_selection','succeeded','failed','needs_attention']
    message: str | None = Field(default=None,max_length=1000)
    canonical_title: str | None = Field(default=None,min_length=6,max_length=1000)
    doi: str | None = Field(default=None,max_length=300)
    external_request_id: str | None = Field(default=None,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    account_ref: str | None = Field(default=None,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    candidates: list[Candidate] | None = Field(default=None,max_length=10)
    pdf_base64: str | None = Field(default=None,max_length=50*1024*1024)
    source_revision: int | None = Field(default=None,ge=0)
    source_reset: SourceReset | None = None
    stage: Stage | None = None


def mount_literature(app,database,root,session,writer,admin,admin_writer,worker,mail_worker):
    service = LiteratureStore(root,database)
    app.state.literature = service

    @app.exception_handler(LiteratureError)
    async def literature_error(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status,content={'detail':str(exc)})

    def account(db,user):
        service.maintain(db)
        service.grant(db,user['id'])
        return service.wallet(db,user['id'])

    @app.get(BASE+'/credits')
    def credits(user=Depends(session)):
        with database.connection(write=True) as db:
            return account(db,user)

    @app.post(BASE+'/credits/checkin')
    def checkin(user=Depends(writer)):
        with database.connection(write=True) as db:
            account(db,user)
            service.entry(db,user['id'],'checkin',1,0,'checkin:'+service.today())
            return service.wallet(db,user['id'])

    @app.get(BASE+'/literature/status')
    def status():
        with database.connection() as db:
            return service.status(db)

    @app.post(BASE+'/literature/jobs')
    def submit(body: Submit,user=Depends(writer)):
        with database.connection(write=True) as db:
            return service.enqueue(db,user['id'],body.title.strip(),body.request_key)

    @app.get(BASE+'/literature/jobs')
    def jobs(user=Depends(session)):
        with database.connection(write=True) as db:
            service.maintain(db)
            return {'items':[service.view(row) for row in db.execute('SELECT * FROM literature_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 200', (user['id'],))]}

    @app.get(BASE+'/literature/jobs/{job_id}')
    def job(job_id: str,user=Depends(session)):
        with database.connection(write=True) as db:
            service.maintain(db)
            return service.view(service.own(db,job_id,user['id']))

    @app.post(BASE+'/literature/jobs/{job_id}/cancel')
    def cancel(job_id: str,user=Depends(writer)):
        with database.connection(write=True) as db:
            service.maintain(db)
            row = service.own(db,job_id,user['id'])
            if row['state'] not in ('succeeded','failed','cancelled'):
                service.settle(db,row,'cancelled','已取消，冻结积分已退回。')
            return service.view(service.own(db,job_id,user['id']))

    @app.post(BASE+'/literature/jobs/{job_id}/select')
    def select(job_id: str,body: Select,user=Depends(writer)):
        import json
        from highscore_literature.pdf import normalize_title
        with database.connection(write=True) as db:
            service.maintain(db)
            row = service.own(db,job_id,user['id'])
            if row['state']!='needs_selection':
                raise LiteratureError(409,'任务当前不需要选择候选。')
            candidate = next((value for value in json.loads(row['candidates']) if value['id']==body.candidate_id),None)
            if candidate is None:
                raise LiteratureError(404,'候选不属于此任务。')
            db.execute("UPDATE literature_jobs SET canonical_title=?,doi=?,selected_candidate=?,state='queued',message='已选择论文，等待处理',next_poll=0,candidates='[]' WHERE id=?", (candidate['title'],candidate.get('doi'),dumps(candidate),job_id))
            return service.view(service.own(db,job_id,user['id']))

    @app.get(BASE+'/literature/jobs/{job_id}/download')
    def download(job_id: str,user=Depends(session)):
        with database.connection(write=True) as db:
            service.maintain(db)
            row = service.own(db,job_id,user['id'])
            data = service.read_pdf(row)
        return Response(content=data,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="paper-{job_id}.pdf"','Cache-Control':'private, no-store'})

    @app.get(BASE+'/admin/literature')
    def admin_status(user=Depends(admin)):
        with database.connection(write=True) as db:
            service.maintain(db)
            values = service.status(db)
            return {**values,'queue_length':db.execute("SELECT COUNT(*) FROM literature_jobs WHERE state IN ('queued','waiting','needs_selection')").fetchone()[0],
                    'active_jobs':db.execute("SELECT COUNT(*) FROM literature_jobs WHERE state='running'").fetchone()[0],
                    'needs_attention':db.execute("SELECT COUNT(*) FROM literature_jobs WHERE state='needs_attention'").fetchone()[0],
                    'accounts_ready':values['worker_online']}

    @app.patch(BASE+'/admin/literature')
    def admin_settings(body: Settings,user=Depends(admin_writer)):
        with database.connection(write=True) as db:
            db.execute("UPDATE settings SET value=? WHERE key='literature_enabled'", (dumps(body.enabled),))
            db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,'literature_settings',?,?)", (user['id'],dumps({'enabled':body.enabled}),time.time()))
            return service.status(db)

    @app.post(BASE+'/worker/literature/claim',dependencies=[Depends(worker)])
    def claim(body: Claim):
        with database.connection(write=True) as db:
            return service.claim(db,body.worker_id,body.accounts_ready)

    @app.post(BASE+'/worker/literature/jobs/{job_id}/heartbeat',dependencies=[Depends(worker)])
    def heartbeat(job_id: str,body: Heartbeat):
        try:
            with database.connection(write=True) as db:
                service.maintain(db)
                row=service.leased(db,job_id,body.lease_token)
                if body.stage == 'ready':
                    raise LiteratureError(409,'全文须通过服务器核验后才可下载。')
                now = time.time()
                db.execute('UPDATE literature_jobs SET lease_until=?,last_checked_at=?,stage=COALESCE(?,stage) WHERE id=?',
                           (now+180,now,body.stage,job_id))
                db.execute('UPDATE literature_workers SET last_seen=? WHERE id=?',(now,row['worker_id']))
        except LiteratureError:
            with database.connection(write=True) as db:service.maintain(db)
            raise
        return {'cancel_requested':False}

    @app.post(BASE+'/worker/literature/jobs/{job_id}/update',dependencies=[Depends(worker)])
    def update(job_id: str,body: Update):
        prepared=None
        try:
            if body.state=='succeeded':
                with database.connection() as db:
                    row=service.leased(db,job_id,body.lease_token)
                prepared=service.prepare_pdf(row,body)
            with database.connection(write=True) as db:
                return service.update(db,job_id,body,prepared)
        except LiteratureError:
            # 原事务全部回滚，随后独立提交已到期结算；拒绝迟到回调不应撤销退款。
            with database.connection(write=True) as db:service.maintain(db)
            raise
        finally:
            if prepared is not None:prepared['path'].unlink(missing_ok=True)

    @app.post(BASE+'/worker/literature/reminders/claim',dependencies=[Depends(mail_worker)])
    def claim_reminder(body: ReminderClaim):
        with database.connection(write=True) as db:
            return service.claim_reminder(db,body.worker_id)

    @app.post(BASE+'/worker/literature/reminders/{reminder_id}/complete',dependencies=[Depends(mail_worker)])
    def complete_reminder(reminder_id: str,body: ReminderComplete):
        with database.connection(write=True) as db:
            return service.complete_reminder(db,reminder_id,body.lease_token,body.state)

    return service
