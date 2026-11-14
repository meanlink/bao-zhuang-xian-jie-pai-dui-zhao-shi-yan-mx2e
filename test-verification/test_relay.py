"""多端接力任务调度的端到端验证（不依赖 Redis 服务与 numpy/scipy）。

运行：
  PYTHONPATH=/workspace/backend python -I /workspace/test-verification/test_relay.py
"""
import os
import sys
import time
import tempfile
import threading

BACKEND = '/workspace/backend'
sys.path.insert(0, '/tmp/pylibs')
sys.path.insert(0, BACKEND)

_tmp = tempfile.mkdtemp(prefix='relay-test-')
os.environ['DATABASE_URL'] = f'sqlite:///{_tmp}/relay.db'
os.environ['JOB_LEASE_SECONDS'] = '1.2'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'

from sqlalchemy import select  # noqa: E402
from src.config.database import Base, engine, Session  # noqa: E402
from src.models import Job, JobEvent, Dataset, User  # noqa: E402
from src.services import leases, jobs as jobs_service  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src import worker  # noqa: E402

leases.LEASE_SECONDS = 1.2
leases.HEARTBEAT_SECONDS = 0.4
worker.leases.LEASE_SECONDS = 1.2
worker.leases.HEARTBEAT_SECONDS = 0.4

PASS = []


def check(name, cond, extra=''):
    assert cond, f'FAILED: {name} {extra}'
    PASS.append(name)
    print(f'  ok - {name}')


class FakeRedis:
    def __init__(self):
        self.queue = []
        self.lock = threading.Lock()

    def blpop(self, key, timeout=1):
        with self.lock:
            if self.queue:
                return key, str(self.queue.pop(0)).encode()
        time.sleep(0.05)
        return None

    def rpush(self, key, value):
        with self.lock:
            self.queue.append(value)

    def close(self):
        pass

    def ping(self):
        return True


fake_redis = FakeRedis()
worker.Redis = lambda *a, **k: fake_redis
jobs_service.Redis = lambda *a, **k: fake_redis

# 可控的假计算：默认立即成功，可挂起模拟长任务
run_gate = threading.Event()
analyze_calls = []
analyze_lock = threading.Lock()


def fake_analyze(records, params):
    with analyze_lock:
        analyze_calls.append((params, time.time()))
    run_gate.wait(5)
    return {'summary': f"ok-{params.get('shift')}", 'metrics': [], 'chart': {},
            'findings': [], 'warnings': [], 'method': 'm', 'table': {'columns': [], 'rows': []}}


worker.analyze = fake_analyze

Base.metadata.create_all(engine)
with Session() as db:
    u = User(email='a@x.local', name='t', role='admin', password=hash_password('x'))
    db.add(u)
    d = Dataset(name='ds', source='s', records=[{'x': 1}], checksum='c', created_at=time.time())
    db.add(d)
    db.commit()
    uid, did = u.id, d.id


def make_job(name='j'):
    with Session() as db:
        user = db.get(User, uid)
        from src.schemas.job import JobInput
        j = jobs_service.create(db, JobInput(name=name, dataset_id=did, params={}), user)
        db.commit()
        return j.id


def job_row(job_id):
    with Session() as db:
        j = db.get(Job, job_id)
        return dict(status=j.status, attempt=j.attempt, worker_id=j.worker_id,
                    lease_until=j.lease_until, finished_at=j.finished_at,
                    result=dict(j.result or {}))


def event_names(job_id):
    with Session() as db:
        return [e.event for e in leases.events_for(db, job_id)]


# ---------------------------------------------------------------- 1. 正常完成
print('1) 排队通知送达：单端领取并正常完成')
run_gate.set()
jid = make_job('正常比较')
fake_redis.rpush('analysis:queue', jid)
worker.run_notified(jid)
row = job_row(jid)
check('正常比较照常完成', row['status'] == 'completed', str(row['status']))
check('完成时收回租约', row['lease_until'] is None)
check('经过含 queued/claimed/completed',
      event_names(jid) == ['queued', 'claimed', 'completed'], str(event_names(jid)))

# ---------------------------------------------------------------- 2. 通知丢失
print('2) 排队通知丢失：SQL 兜底轮询仍能开算')
jid2 = make_job('通知丢失')  # 故意不 rpush
worker.run_due()
check('兜底轮询领取并完成', job_row(jid2)['status'] == 'completed')
check('兜底领取也有完整经过',
      event_names(jid2) == ['queued', 'claimed', 'completed'])

# ---------------------------------------------------------------- 3. 失联 + 接手
print('3) 计算端失联：租约到期后另一端接手')
run_gate.clear()
jid3 = make_job('失联接手')
t1 = threading.Thread(target=worker.run_notified, args=(jid3,), daemon=True)
t1.start()
time.sleep(0.3)
r = job_row(jid3)
check('w1 已领取并在运行', r['status'] == 'running' and r['worker_id'] == worker.WORKER_ID
      and r['attempt'] == 1, str(r))
first_lease = r['lease_until']
time.sleep(0.6)
check('心跳持续续租', job_row(jid3)['lease_until'] > first_lease)

# 模拟 w1 彻底失联：停掉心跳（线程仍卡在 analyze，但不再续租）
with Session() as db:
    j = db.get(Job, jid3)
    j.lease_until = time.time() - 0.01
    db.commit()
# 找到 w1 的心跳线程并停掉，防止它把租约续回来
for th in threading.enumerate():
    if isinstance(th, worker.Heartbeat) and th.job_id == jid3:
        th.stop()
time.sleep(0.5)
with Session() as db:
    n_lost = leases.sweep_lost(db)
check('失联被扫描登记', n_lost == 1)
check('经过含 lost', 'lost' in event_names(jid3), str(event_names(jid3)))
check('失联任务未被重置为 queued（仍是 running 等接手）',
      job_row(jid3)['status'] == 'running')

# 另一端 w2 接手
worker.WORKER_ID = 'worker-w2'
t2 = threading.Thread(target=worker.run_due, daemon=True)
t2.start()
time.sleep(0.4)
r = job_row(jid3)
check('w2 成功接手，attempt 递增',
      r['status'] == 'running' and r['worker_id'] == 'worker-w2' and r['attempt'] == 2, str(r))
check('经过含 reclaimed', 'reclaimed' in event_names(jid3))
run_gate.set()
t1.join(3); t2.join(3)
r = job_row(jid3)
check('接手后任务正常完成', r['status'] == 'completed' and r['worker_id'] == 'worker-w2')
with Session() as db:
    completes = [e for e in leases.events_for(db, jid3) if e.event == 'completed']
check('完成事件归属第 2 次领取（w2）',
      len(completes) == 1 and completes[0].attempt == 2 and completes[0].actor == 'worker-w2',
      str([(e.event, e.attempt, e.actor) for e in completes]))
check('旧端迟交在接手后被拒收', 'stale_result_rejected' in event_names(jid3))

# ---------------------------------------------------------------- 4. 旧领取者迟交
print('4) 旧领取者迟交结果：栅栏令牌拒绝，不能覆盖接手后状态')
worker.WORKER_ID = 'worker-w3'
run_gate.clear()
jid4 = make_job('迟交拒收')
t3 = threading.Thread(target=worker.run_notified, args=(jid4,), daemon=True)
t3.start()
time.sleep(0.3)
check('w3 领取（attempt=1）', job_row(jid4)['attempt'] == 1)
for th in threading.enumerate():
    if isinstance(th, worker.Heartbeat) and th.job_id == jid4:
        th.stop()  # w3 失联
with Session() as db:
    db.get(Job, jid4).lease_until = time.time() - 0.01
    db.commit()
time.sleep(1.3)
worker.WORKER_ID = 'worker-w4'
with Session() as db:
    job4, took = leases.claim_next(db, worker_id='worker-w4')
    check('w4 接手 attempt=2', took and job4.attempt == 2)
# w3 的心跳尝试续租：令牌/归属不匹配，必须失败
with Session() as db:
    ok = leases.heartbeat(db, jid4, 1, 'worker-w3')
check('旧领取者续租被拒', ok is False)
# w3 迟交完成结果
with Session() as db:
    stale_job = db.get(Job, jid4)
    accepted = leases.complete(db, stale_job, 1, {'summary': 'STALE'}, 'worker-w3')
check('旧领取者迟交结果被拒收', accepted is False)
r = job_row(jid4)
check('接手后状态未被覆盖（仍 running，无 stale 结果）',
      r['status'] == 'running' and r['result'].get('summary') != 'STALE', str(r))
check('拒收事件已留痕', 'stale_result_rejected' in event_names(jid4))
# w4 正常交结果
with Session() as db:
    cur = db.get(Job, jid4)
    leases.complete(db, cur, 2, {'summary': 'FRESH'}, 'worker-w4')
r = job_row(jid4)
check('当前领取者结果生效', r['status'] == 'completed' and r['result'].get('summary') == 'FRESH')
run_gate.set(); t3.join(3)
# t3 醒来后 _run 也会尝试 complete(attempt=1)，应再次被拒且不影响 FRESH
check('线程迟交同样不覆盖', job_row(jid4)['result'].get('summary') == 'FRESH')

# ---------------------------------------------------------------- 5. 取消排队
print('5) 取消排队任务：终态、不复活、不产结果')
jid5 = make_job('取消排队')
with Session() as db:
    j5 = db.get(Job, jid5)
    check('排队任务可取消', leases.cancel(db, j5, 'a@x.local') is True)
r = job_row(jid5)
check('排队取消后为 cancelled 终态', r['status'] == 'cancelled' and r['finished_at'])
check('取消经过已记录', event_names(jid5)[-1] == 'cancelled')
worker.run_due()
check('取消后兜底轮询不再领取', job_row(jid5)['status'] == 'cancelled')
fake_redis.rpush('analysis:queue', jid5)
worker.run_notified(jid5)
check('即使迟到通知到达也不复活', job_row(jid5)['status'] == 'cancelled')
check('取消后无结果产出', job_row(jid5)['result'] == {})
with Session() as db:
    check('终态任务不能再次取消', leases.cancel(db, db.get(Job, jid5), 'a@x.local') is False)

# ---------------------------------------------------------------- 6. 取消运行中
print('6) 取消运行中任务：计算端心跳/结果提交被挡下')
worker.WORKER_ID = 'worker-w5'
run_gate.clear()
jid6 = make_job('取消运行')
t6 = threading.Thread(target=worker.run_notified, args=(jid6,), daemon=True)
t6.start()
time.sleep(0.3)
with Session() as db:
    leases.cancel(db, db.get(Job, jid6), 'a@x.local')
time.sleep(0.6)
with Session() as db:
    ok = leases.heartbeat(db, jid6, 1, 'worker-w5')
check('取消后续租被拒', ok is False)
run_gate.set(); t6.join(3)
r = job_row(jid6)
check('运行中取消后保持 cancelled，不产出结果',
      r['status'] == 'cancelled' and r['result'] == {}, str(r))

# ---------------------------------------------------------------- 7. 重启恢复
print('7) 计算端重启：失联任务可恢复重跑，不重复建任务，取消不复活')
run_gate.set()
jid7 = make_job('重启恢复')
with Session() as db:
    j7, _ = leases.claim_next(db, worker_id='worker-old')
    check('旧端领取 attempt=1', j7.attempt == 1 and j7.status == 'running')
# 模拟旧端崩溃（不做任何重置），新端启动 main 的初始化扫描
worker.WORKER_ID = 'worker-restart'
with Session() as db:
    leases.sweep_lost(db)  # 租约未过期时先不应判失联
check('租约未过期不误判失联', 'lost' not in event_names(jid7))
with Session() as db:
    db.get(Job, jid7).lease_until = time.time() - 0.01
    db.commit()
with Session() as db:
    leases.sweep_lost(db)
# 新端主循环：先 blpop（空），再 run_due 接手
worker.run_due()
r = job_row(jid7)
check('重启后接手重跑并完成', r['status'] == 'completed' and r['worker_id'] == 'worker-restart'
      and r['attempt'] == 2, str(r))
check('恢复重跑不重复建任务（仍是同一 job id，仅一次 queued）',
      event_names(jid7).count('queued') == 1, str(event_names(jid7)))
# 重启不复活已取消任务
worker.run_due()
check('已取消任务重启后依然取消', job_row(jid5)['status'] == 'cancelled')

# ---------------------------------------------------------------- 8. 并发抢领
print('8) 多端抢领同一任务：只有一个领取者，其余端落空')
worker.WORKER_ID = 'worker-race'
jid8 = make_job('并发抢领')
winners = []
# MySQL 生产路径用 SELECT ... FOR UPDATE SKIP LOCKED 做行级互斥；
# SQLite 无行锁语法，这里以屏障并发发起领取，验证任务只会被成功领取一次。
barrier = threading.Barrier(5)


def race(worker_name):
    barrier.wait()
    with Session() as db:
        j, _ = leases.claim_next(db, job_id=jid8, worker_id=worker_name)
        if j:
            winners.append((worker_name, j.attempt))


threads = [threading.Thread(target=race, args=(f'worker-r{i}',)) for i in range(5)]
for th in threads:
    th.start()
for th in threads:
    th.join(5)
r8 = job_row(jid8)
check('五个端只有一个抢到（attempt=1，单一领取者）',
      len(winners) == 1 and winners[0][1] == 1, str(winners))
check('抢到者记录在任务上', r8['worker_id'] == winners[0][0])
# 已被领走且租约未过期时，其他端的定向领取必须落空
with Session() as db:
    j, _ = leases.claim_next(db, job_id=jid8, worker_id='worker-late')
    check('租约有效时其他端领取落空', j is None)

# ---------------------------------------------------------------- 汇总
count_jobs = None
with Session() as db:
    count_jobs = len(list(db.scalars(select(Job))))
check('全部场景只建了 8 个任务', count_jobs == 8, str(count_jobs))
print(f'\nALL {len(PASS)} CHECKS PASSED')
