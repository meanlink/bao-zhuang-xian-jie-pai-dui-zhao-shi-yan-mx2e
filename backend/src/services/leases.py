import os, time, threading
from sqlalchemy import select, update
from src.config.database import Session, engine
from src.models import Job, JobEvent
from src.services.audit import record

# 租约时长与心跳节奏：心跳间隔固定为租约的三分之一，
# 租约到期即视为领取端失联，其他计算端可接手。
LEASE_SECONDS = float(os.getenv('JOB_LEASE_SECONDS', '30'))
HEARTBEAT_SECONDS = LEASE_SECONDS / 3
WORKER_ID = os.getenv('WORKER_ID', f'worker-{os.getpid()}')

ACTIVE_STATUSES = ('queued', 'running')
TERMINAL_STATUSES = ('completed', 'failed', 'cancelled')

# 仅 SQLite（本地/测试）使用的进程内领取互斥锁；MySQL 依赖行级 SKIP LOCKED。
_CLAIM_LOCK = threading.Lock()


def log_event(db, job_id, event, actor, attempt, detail=None):
    db.add(JobEvent(job_id=job_id, event=event, actor=actor, attempt=attempt,
                    detail=detail or {}, at=time.time()))


def claim_next(db, job_id=None, worker_id=WORKER_ID, now=None):
    '''领取一个可执行任务（排队中，或租约过期的失联任务）。

    以条件 UPDATE 做原子领取：只有仍可领取的任务会被更新，
    多个计算端并发抢同一任务时只有一个成功。job_id 给出时优先领取该任务
    （Redis 通知路径）；通知指向的任务已被领取则回退到任意可执行任务。
    返回 (job, took_over)；没有可领任务时返回 (None, False)。
    '''
    now = time.time() if now is None else now
    # MySQL 用 SKIP LOCKED 支持多端并发抢领；SQLite 无行锁语法，
    # 测试进程内用 _CLAIM_LOCK 模拟行级互斥（生产 MySQL 路径不使用该锁）。
    is_mysql = engine.dialect.name == 'mysql'
    locked = (lambda stmt: stmt.with_for_update(skip_locked=True)) \
        if is_mysql else (lambda stmt: stmt)
    if not is_mysql:
        _CLAIM_LOCK.acquire()
    try:
        base = (
            select(Job)
            .where(Job.status.in_(ACTIVE_STATUSES))
            .where((Job.lease_until.is_(None)) | (Job.lease_until < now))
        )
        candidate = None
        if job_id is not None:
            candidate = db.scalar(
                locked(base.where(Job.id == job_id).limit(1))
            )
        if candidate is None:
            candidate = db.scalar(
                locked(base.order_by(Job.id).limit(1))
            )
        if candidate is None:
            return None, False
        # 是否为接手：原状态已是 running（失联端留下），或已有领取代号
        took_over = candidate.status == 'running' or candidate.attempt > 0
        previous_worker = candidate.worker_id
        candidate.attempt += 1
        attempt = candidate.attempt
        candidate.status = 'running'
        candidate.worker_id = worker_id
        candidate.lease_until = now + LEASE_SECONDS
        candidate.heartbeat_at = now
        log_event(db, candidate.id, 'reclaimed' if took_over else 'claimed',
                  worker_id, attempt,
                  {'previous_worker': previous_worker} if took_over else {})
        if took_over:
            record(db, worker_id, 'job.reclaimed',
                   {'id': candidate.id, 'attempt': attempt, 'previous_worker': previous_worker})
        db.commit()
        db.refresh(candidate)
        return candidate, took_over
    finally:
        if not is_mysql:
            _CLAIM_LOCK.release()


def heartbeat(db, job_id, attempt, worker_id=WORKER_ID, now=None):
    '''续租。只有当前领取者（worker_id + attempt 都匹配）能续。'''
    now = time.time() if now is None else now
    updated = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.attempt == attempt,
               Job.worker_id == worker_id, Job.status == 'running')
        .values(lease_until=now + LEASE_SECONDS, heartbeat_at=now)
    ).rowcount
    db.commit()
    return updated == 1


def _finish(db, job, attempt, worker_id, status, changes, event_detail):
    '''终态提交的统一闸门：任务只认当前领取者。

    旧领取者在失联后迟交结果时，其 attempt 已落后于当前领取者，
    条件更新命中 0 行，结果无法覆盖接手后的状态。
    '''
    now = time.time()
    updated = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.attempt == attempt,
               Job.worker_id == worker_id, Job.status == 'running')
        .values(status=status, lease_until=None, finished_at=now, **changes)
    ).rowcount
    if updated == 0:
        db.rollback()
        current = db.get(Job, job.id)
        log_event(db, job.id, 'stale_result_rejected', worker_id, attempt,
                  {'current_status': current.status if current else 'missing',
                   'current_attempt': current.attempt if current else None,
                   'current_worker': current.worker_id if current else None})
        db.commit()
        return False
    log_event(db, job.id, status, worker_id, attempt, event_detail)
    record(db, worker_id, f'job.{status}',
           {'id': job.id, 'attempt': attempt, **event_detail})
    db.commit()
    return True


def complete(db, job, attempt, result, worker_id=WORKER_ID):
    return _finish(db, job, attempt, worker_id, 'completed',
                   {'result': result, 'error': ''}, {})


def fail(db, job, attempt, error, worker_id=WORKER_ID):
    return _finish(db, job, attempt, worker_id, 'failed',
                   {'error': error[:990]}, {'error': error[:990]})


def cancel(db, job, actor, now=None):
    '''工作台取消排队或运行中的比较；取消是终态，重启也不复活。

    运行中任务只取消当前仍被持有（租约未过期）的领取；租约已过期说明
    原端失联、随时可能被接手，留给接手流程处理，避免与接手并发冲突。
    '''
    now = time.time() if now is None else now
    if job.status not in ACTIVE_STATUSES:
        return False
    if job.status == 'running' and job.lease_until is not None and job.lease_until < now:
        return False
    previous = {'worker_id': job.worker_id, 'attempt': job.attempt}
    job.status = 'cancelled'
    job.lease_until = None
    job.finished_at = now
    log_event(db, job.id, 'cancelled', actor, job.attempt, previous)
    record(db, actor, 'job.cancelled', {'id': job.id, **previous})
    db.commit()
    return True


def sweep_lost(db, now=None):
    '''把失联领取者留下的过期租约登记为 lost 事件（不改变任务归属，
    任务仍可被 claim_next 接手）。幂等：同一 attempt 只登记一次。'''
    now = time.time() if now is None else now
    lost = list(db.scalars(
        select(Job).where(Job.status == 'running', Job.lease_until < now)
    ))
    marked = 0
    for job in lost:
        exists = db.scalar(
            select(JobEvent.id).where(
                JobEvent.job_id == job.id, JobEvent.attempt == job.attempt,
                JobEvent.event.in_(('lost', 'reclaimed', 'cancelled',
                                    'completed', 'failed')))
        )
        if exists:
            continue
        log_event(db, job.id, 'lost', job.worker_id or 'unknown', job.attempt,
                  {'lease_until': job.lease_until, 'heartbeat_at': job.heartbeat_at})
        record(db, 'scheduler', 'job.lost',
               {'id': job.id, 'attempt': job.attempt, 'worker_id': job.worker_id})
        marked += 1
    if marked:
        db.commit()
    return marked


def events_for(db, job_id):
    return list(db.scalars(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
    ))
