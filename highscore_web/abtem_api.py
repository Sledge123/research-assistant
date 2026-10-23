"""电镜模拟鉴权接口；HTTP进程只使用标准库复验结果包。"""
import shutil
from typing import Annotated, Literal

from fastapi import Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .abtem_store import AbtemError, AbtemStore, MAX_BUNDLE_BYTES

BASE = '/highscore/api/abtem'
WORKER = '/highscore/api/worker/abtem'


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Upload(Model):
    type: Literal['upload']
    filename: str = Field(min_length=5, max_length=200)
    cif_base64: str = Field(min_length=4, max_length=700000)


class Archive(Model):
    type: Literal['archive']
    job_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    code: str = Field(pattern=r'^ICSD-[0-9]{6}$')


class Submit(Model):
    request_key: str = Field(min_length=1, max_length=100)
    source: Annotated[Upload | Archive, Field(discriminator='type')]
    parameters: dict = Field(default_factory=dict)


class Claim(Model):
    worker_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    available: bool


class Lease(Model):
    lease_token: str = Field(min_length=32, max_length=128)


class Heartbeat(Lease):
    progress: int | None = Field(default=None, ge=0, le=99)
    phase: Literal['preparing', 'calculating', 'packaging', 'verifying'] | None = None


class Complete(Lease):
    state: Literal['succeeded', 'failed', 'cancelled', 'timed_out']
    bundle_base64: str | None = Field(default=None, max_length=(MAX_BUNDLE_BYTES + 2) // 3 * 4)
    message: str | None = Field(default=None, max_length=2000)
    error_code: Literal['unsupported_occupancy', 'unsupported_disorder', 'unsupported_structure',
                        'unsupported_scale', 'unsupported_precision', 'resource', 'compute'] | None = None


def mount_abtem(app, database, root, session, writer, worker, *, enabled=False):
    service = AbtemStore(root, database, enabled=enabled)
    app.state.abtem = service

    @app.exception_handler(AbtemError)
    async def abtem_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail': str(exc)})

    def maintain():
        with database.connection(write=True) as db:
            service.maintain(db)

    @app.get(BASE + '/status')
    def status(user=Depends(session)):
        maintain()
        with database.connection() as db:
            return service.status(db)

    @app.get(BASE + '/sources')
    def sources(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100), user=Depends(session)):
        with database.connection() as db:
            return service.sources(db, user['id'], offset, limit)

    @app.post(BASE + '/jobs')
    def submit(body: Submit, user=Depends(writer)):
        # CIF/原始归档复验在读连接完成，不能长时间阻塞其他业务积分事务。
        maintain()
        with database.connection() as db:
            source = body.source.model_dump()
            previous = service.replay(db, user['id'], body.request_key, source, body.parameters)
            if previous is not None:
                return previous
            prepared = service.input(db, user['id'], source, body.parameters)
        with database.connection(write=True) as db:
            return service.enqueue(db, user['id'], body.request_key, prepared)

    @app.get(BASE + '/jobs')
    def jobs(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100), user=Depends(session)):
        maintain()
        with database.connection() as db:
            rows = db.execute('SELECT * FROM abtem_jobs WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                              (user['id'], limit + 1, offset)).fetchall()
            return {'items': [service.view(row) for row in rows[:limit]],
                    'next_offset': offset + limit if len(rows) > limit else None}

    @app.get(BASE + '/jobs/{job_id}')
    def job(job_id: str, user=Depends(session)):
        maintain()
        with database.connection() as db:
            return service.view(service.own(db, job_id, user['id']))

    @app.post(BASE + '/jobs/{job_id}/cancel')
    def cancel(job_id: str, user=Depends(writer)):
        with database.connection(write=True) as db:
            return service.cancel(db, job_id, user['id'])

    @app.get(BASE + '/jobs/{job_id}/download')
    def download(job_id: str, user=Depends(session)):
        maintain()
        with database.connection() as db:
            row = service.own(db, job_id, user['id'])
            data, _ = service.read_bundle(row)
        return Response(data, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="abtem-{job_id}.zip"',
            'Cache-Control': 'private, no-store'})

    @app.get(BASE + '/jobs/{job_id}/image/{kind}')
    def image(job_id: str, kind: Literal['saed', 'hrtem'], user=Depends(session)):
        maintain()
        with database.connection() as db:
            row = service.own(db, job_id, user['id'])
            _, checked = service.read_bundle(row)
        return Response(checked['files'][kind + '.png'], media_type='image/png',
                        headers={'Cache-Control': 'private, no-store'})

    @app.post(WORKER + '/claim', dependencies=[Depends(worker)])
    def claim(body: Claim):
        with database.connection(write=True) as db:
            return service.claim(db, body.worker_id, body.available)

    @app.post(WORKER + '/jobs/{job_id}/heartbeat', dependencies=[Depends(worker)])
    def heartbeat(job_id: str, body: Heartbeat):
        try:
            with database.connection(write=True) as db:
                return service.heartbeat(db, job_id, body.lease_token, body.progress, body.phase)
        except AbtemError:
            # 失效租约回执不能撤销同次维护已经识别的到期退款。
            maintain()
            raise

    @app.post(WORKER + '/jobs/{job_id}/complete', dependencies=[Depends(worker)])
    def complete(job_id: str, body: Complete):
        maintain()
        prepared = None
        try:
            with database.connection() as db:
                repeated = service.completed(db, job_id, body.lease_token, body.state, body.bundle_base64)
                if repeated is not None:
                    return repeated
                row = service.leased(db, job_id, body.lease_token)
                if row['cancel_requested'] and body.state != 'cancelled':
                    raise AbtemError(409, '任务正在取消，请确认计算已经结束后提交取消结果。')
            if body.state == 'succeeded':
                prepared = service.prepare(row, body.bundle_base64)
            elif body.bundle_base64 is not None:
                raise AbtemError(422, '未完成的模拟任务不能提交成功文件。')
            with database.connection(write=True) as db:
                # 第二个相同完成回调可能已提交；先复查，确保不重复结算。
                current = db.execute('SELECT state FROM abtem_jobs WHERE id=?', (job_id,)).fetchone()
                if current is not None and current['state'] != 'running':
                    repeated = service.completed(db, job_id, body.lease_token, body.state, body.bundle_base64, prepared)
                    if repeated is None:
                        raise AbtemError(409, '模拟任务状态已经变化，不能提交结果。')
                    return repeated
                return service.complete(db, job_id, body.lease_token, body.state, prepared,
                                        body.message or '', body.error_code)
        except AbtemError:
            maintain()
            raise
        finally:
            if prepared is not None and prepared['directory'].exists():
                shutil.rmtree(prepared['directory'])

    return service
