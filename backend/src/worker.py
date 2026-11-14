import time, json, logging, threading
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report
from src.services.business import analyze
from src.services.reports import deliver
from src.services.audit import record
from src.services import leases

logging.basicConfig(level=logging.INFO)
WORKER_ID = leases.WORKER_ID


class Heartbeat(threading.Thread):
    '''后台续租线程：停跳超过租约时长即被判定失联。'''
    def __init__(self, job_id, attempt):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.job_id = job_id
        self.attempt = attempt
        self.alive = True

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(leases.HEARTBEAT_SECONDS):
            try:
                with Session() as db:
                    if not leases.heartbeat(db, self.job_id, self.attempt, WORKER_ID):
                        # 已被接手或取消，续租被拒，本端不再是领取者
                        self.alive = False
                        return
            except Exception:
                logging.exception('Heartbeat failed; retrying next interval')


def _run(job_id, attempt, dataset_id, params):
    '''带着领取代号执行计算；终态写入由 leases 统一校验归属。'''
    hb = Heartbeat(job_id, attempt)
    hb.start()
    try:
        try:
            with Session() as db:
                dataset = db.get(Dataset, dataset_id)
                result = analyze(dataset.records, params)
                json.dumps(result, allow_nan=False)
                job = db.get(Job, job_id)
                if not leases.complete(db, job, attempt, result, WORKER_ID):
                    logging.warning('Job %s attempt %s result rejected (no longer owner)',
                                    job_id, attempt)
        except Exception as e:
            logging.exception('Analysis failed')
            with Session() as db:
                job = db.get(Job, job_id)
                if job and not leases.fail(db, job, attempt, str(e), WORKER_ID):
                    logging.warning('Job %s attempt %s failure rejected (no longer owner)',
                                    job_id, attempt)
    finally:
        hb.stop()


def run_notified(job_id):
    '''Redis 通知路径：先尝试领通知中的任务，失败再领任意可执行任务。'''
    with Session() as db:
        job, _ = leases.claim_next(db, job_id=job_id, worker_id=WORKER_ID)
        if job is None:
            job, _ = leases.claim_next(db, worker_id=WORKER_ID)
        if job is None:
            return
        claimed = (job.id, job.attempt, job.dataset_id, dict(job.params))
    _run(*claimed)


def run_due():
    '''SQL 兜底路径：通知丢失时，直接从库里领取排队/失联任务。'''
    with Session() as db:
        leases.sweep_lost(db)
        job, _ = leases.claim_next(db, worker_id=WORKER_ID)
        if job is None:
            return
        claimed = (job.id, job.attempt, job.dataset_id, dict(job.params))
    _run(*claimed)


def tick():
    with Session() as db:
        leases.sweep_lost(db)
        for r in db.scalars(select(Report).where(Report.enabled == True, Report.next_run <= time.time())):
            d = deliver(db, r)
            if d:
                record(db, WORKER_ID, 'report.delivered', {'id': r.id, 'delivery': d.id})
        db.commit()


def main():
    r = Redis.from_url(REDIS_URL, socket_timeout=3)
    logging.info('Worker %s starting; lease=%ss heartbeat=%ss',
                 WORKER_ID, leases.LEASE_SECONDS, leases.HEARTBEAT_SECONDS)
    # 多端安全：不再把 running 整体重置为 queued。
    # 失联任务靠租约过期被其他端接手，取消的任务保持终态。
    with Session() as db:
        leases.sweep_lost(db)
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item = r.blpop('analysis:queue', timeout=1)
            if item:
                run_notified(int(item[1]))
            else:
                run_due()
            tick()
        except Exception:
            logging.exception('Worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
