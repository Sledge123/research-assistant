"""晶体工具鉴权接口，结果复验仅使用标准库。"""
import shutil
from typing import Annotated, Literal

from fastapi import Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .pymatgen_store import PymatgenError, PymatgenStore, MAX_BUNDLE_BYTES

BASE = '/highscore/api/pymatgen'
WORKER = '/highscore/api/worker/pymatgen'


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Upload(Model):
    type: Literal['upload']
    filename: str = Field(min_length=1, max_length=180)
    content_base64: str = Field(min_length=4, max_length=699052)


class Archive(Model):
    type: Literal['archive']
    job_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    code: str = Field(pattern=r'^ICSD-[0-9]{6}$')


class Submit(Model):
    request_key: str = Field(min_length=1, max_length=100)
    operation: Literal['analyze', 'convert', 'xrd', 'supercell', 'compare']
    inputs: list[Annotated[Upload | Archive, Field(discriminator='type')]] = Field(min_length=1, max_length=2)
    parameters: dict = Field(default_factory=dict)
    pricing_version: Literal[1] | None = None


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


def mount_pymatgen(app, database, root, session, writer, worker, *, enabled=False):
    provider = getattr(app.state, 'abtem', None)
    service = PymatgenStore(root, database, enabled=enabled, archive_provider=provider)
    app.state.pymatgen = service
    if provider is not None:
        service.science_peers, provider.science_peers = (provider,), (service,)

    @app.exception_handler(PymatgenError)
    async def pymatgen_error(request, exc):
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
        maintain()
        with database.connection() as db:
            inputs = [source.model_dump() for source in body.inputs]
            previous = service.replay(db, user['id'], body.request_key, inputs, body.operation, body.parameters)
            if previous is not None:
                return previous
            if body.pricing_version != 1:
                raise PymatgenError(409, '材料工具计费规则已更新，请刷新页面后重新提交。')
            prepared = service.resolve_inputs(db, user['id'], inputs, body.operation, body.parameters)
        with database.connection(write=True) as db:
            return service.enqueue(db, user['id'], body.request_key, prepared)

    @app.get(BASE + '/jobs')
    def jobs(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100), user=Depends(session)):
        maintain()
        with database.connection() as db:
            rows = db.execute('SELECT * FROM pymatgen_jobs WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
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
            data, _ = service.read_bundle(service.own(db, job_id, user['id']), db=db)
        return Response(data, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="pymatgen-{job_id}.zip"', 'Cache-Control': 'private, no-store'})

    @app.get(BASE + '/jobs/{job_id}/result')
    def result(job_id: str, user=Depends(session)):
        maintain()
        with database.connection() as db:
            _, checked = service.read_bundle(service.own(db, job_id, user['id']), db=db)
        return JSONResponse(content={'result': checked['result'], 'parameters': checked['result']['parameters']},
                            headers={'Cache-Control': 'private, no-store'})

    @app.get(BASE + '/jobs/{job_id}/image/xrd')
    def image(job_id: str, user=Depends(session)):
        maintain()
        with database.connection() as db:
            row = service.own(db, job_id, user['id'])
            if row['operation'] != 'xrd':
                raise PymatgenError(404, '该任务没有理论XRD图。')
            _, checked = service.read_bundle(row, db=db)
        return Response(checked['files']['xrd.png'], media_type='image/png', headers={'Cache-Control': 'private, no-store'})

    @app.post(WORKER + '/claim', dependencies=[Depends(worker)])
    def claim(body: Claim):
        with database.connection(write=True) as db:
            return service.claim(db, body.worker_id, body.available)

    @app.post(WORKER + '/jobs/{job_id}/heartbeat', dependencies=[Depends(worker)])
    def heartbeat(job_id: str, body: Heartbeat):
        try:
            with database.connection(write=True) as db:
                return service.heartbeat(db, job_id, body.lease_token, body.progress, body.phase)
        except PymatgenError:
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
                    raise PymatgenError(409, '任务正在取消，请确认计算已经结束后提交取消结果。')
                if body.state == 'succeeded':
                    prepared = service.prepare(db, row, body.bundle_base64)
            if body.state != 'succeeded' and body.bundle_base64 is not None:
                raise PymatgenError(422, '未完成的任务不能提交成功文件。')
            with database.connection(write=True) as db:
                current = db.execute('SELECT state FROM pymatgen_jobs WHERE id=?', (job_id,)).fetchone()
                if current is not None and current['state'] != 'running':
                    repeated = service.completed(db, job_id, body.lease_token, body.state, body.bundle_base64, prepared)
                    if repeated is None:
                        raise PymatgenError(409, '工具任务状态已经变化，不能提交结果。')
                    return repeated
                return service.complete(db, job_id, body.lease_token, body.state, prepared, body.message or '', body.error_code)
        except PymatgenError:
            maintain()
            raise
        finally:
            if prepared is not None and prepared['directory'].exists():
                shutil.rmtree(prepared['directory'])

    return service
