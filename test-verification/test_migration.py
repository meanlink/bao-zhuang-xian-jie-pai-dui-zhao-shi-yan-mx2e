"""旧库迁移幂等 + 真实统计分析全链路（不 mock analyze）。"""
import os, sys, tempfile, time

sys.path.insert(0, '/tmp/pylibs')
sys.path.insert(0, '/workspace/backend')

_tmp = tempfile.mkdtemp(prefix='relay-mig-')
db_path = f'{_tmp}/mig.db'
os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
os.environ['SEED_DEMO'] = 'false'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
os.environ['JOB_LEASE_SECONDS'] = '5'

from sqlalchemy import text, inspect  # noqa: E402
from src.config.database import engine, Session  # noqa: E402
from src.models import User, Dataset  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.services.business import seed_records  # noqa: E402
from src.services import jobs as jobs_service, leases  # noqa: E402
from src.schemas.job import JobInput  # noqa: E402
import src.worker as worker  # noqa: E402

# 1) 先建一张“旧版” jobs 表（无租约列）
with engine.begin() as conn:
    conn.execute(text(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, name VARCHAR(160), dataset_id INTEGER, "
        "params JSON, status VARCHAR(20), result JSON, error VARCHAR(1000), approved BOOLEAN, "
        "created_by INTEGER, created_at DOUBLE, finished_at DOUBLE)"))
    conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(150), "
                      "name VARCHAR(150), role VARCHAR(20), password VARCHAR(300))"))
    conn.execute(text("CREATE TABLE datasets (id INTEGER PRIMARY KEY, name VARCHAR(160), "
                      "source VARCHAR(255), records JSON, checksum VARCHAR(64), created_at DOUBLE)"))

from migrations.initial import migrate  # noqa: E402
migrate()
cols = {c['name'] for c in inspect(engine).get_columns('jobs')}
assert {'worker_id', 'attempt', 'lease_until', 'heartbeat_at'} <= cols, cols
assert 'job_events' in inspect(engine).get_table_names()
print('ok - 旧库迁移补齐租约列与事件表')
# 再跑一次迁移必须幂等无报错
migrate()
print('ok - 迁移可重复执行（幂等）')

# 2) 旧数据 attempt 默认 0，且仍可被新 worker 领取
with Session() as db:
    u = User(email='a@x.local', name='t', role='admin', password=hash_password('x'))
    db.add(u)
    d = Dataset(name='ds', source='s', records=seed_records(), checksum='c', created_at=time.time())
    db.add(u); db.flush(); db.add(d); db.flush()
    # 模拟旧版遗留的 running 任务：重启后不应被重置，靠租约过期接手
    from src.models import Job
    stuck = Job(name='旧端遗留', dataset_id=d.id, params={'confidence': '0.95', 'shift': '全部'},
                status='running', result={}, created_by=u.id, created_at=time.time() - 100)
    db.add(stuck); db.flush()
    stuck_id = stuck.id
    jid = jobs_service.create(db, JobInput(name='真实分析', dataset_id=d.id, params={}), u).id
    db.commit()

# 遗留 running 租约为 NULL → 立即可被接手（NULL 视为未持约）
with Session() as db:
    j, took = leases.claim_next(db, worker_id='worker-new')
    assert j.id == stuck_id and took and j.attempt == 1, (j.id, took, j.attempt)
print('ok - 旧版遗留 running 任务由新端接手（不重置、不重建，记 reclaimed）')

# 真实 analyze 全链路（worker._run，含心跳线程）
class FakeRedis:
    def blpop(self, *a, **k): return None
    def rpush(self, *a, **k): pass
    def close(self): pass
worker.Redis = lambda *a, **k: FakeRedis()
worker.WORKER_ID = 'worker-real'
from src.models import Job as _J
# worker-new 接手后立刻“失联”：租约到期由 worker-real 第二次接手
with Session() as db:
    cur = db.get(_J, stuck_id)
    assert cur.status == 'running' and cur.attempt == 1 and cur.worker_id == 'worker-new'
    cur.lease_until = time.time() - 0.01
    db.commit()
time.sleep(0.05)
with Session() as db:
    cur, took = leases.claim_next(db, worker_id='worker-real')
    assert cur.id == stuck_id and took and cur.attempt == 2 and cur.worker_id == 'worker-real'
    cid, att, did, par = cur.id, cur.attempt, cur.dataset_id, dict(cur.params)
# 带着 worker-real 的领取代号跑真实分析（含后台心跳）
worker._run(cid, att, did, par)
with Session() as db:
    cur = db.get(_J, stuck_id)
    assert cur.status == 'completed' and cur.result and cur.result.get('summary'), cur.status
    assert cur.worker_id == 'worker-real' and cur.attempt == 2
print('ok - 二次接手后真实 Welch 分析完成，结果非空')

# 排队任务走 run_due 兜底全链路
worker.WORKER_ID = 'worker-due'
worker.run_due()
with Session() as db:
    cur = db.get(_J, jid)
    assert cur.status == 'completed', cur.status
    assert cur.result.get('metrics') and cur.worker_id == 'worker-due'
print('ok - 兜底轮询驱动真实分析完成')

# 事件链完整
with Session() as db:
    ev = [e.event for e in leases.events_for(db, stuck_id)]
assert ev == ['reclaimed', 'reclaimed', 'completed'], ev
print('ok - 全链路事件链完整（两次接手留痕）')

print('\nMIGRATION + REAL ANALYZE ALL PASSED')
