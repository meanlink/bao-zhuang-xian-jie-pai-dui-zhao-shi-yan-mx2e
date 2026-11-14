"""HTTP 层集成：登录、建任务、取消、事件查询与权限。"""
import os, sys, tempfile, time

sys.path.insert(0, '/tmp/pylibs')
sys.path.insert(0, '/workspace/backend')

_tmp = tempfile.mkdtemp(prefix='relay-http-')
os.environ['DATABASE_URL'] = f'sqlite:///{_tmp}/http.db'
os.environ['SEED_DEMO'] = 'false'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
os.environ['JOB_LEASE_SECONDS'] = '1'

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from src.main import app  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.models import User, Dataset  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from migrations.initial import migrate  # noqa: E402

migrate()
with Session() as db:
    db.add(User(email='analyst@demo.local', name='a', role='analyst', password=hash_password('Demo123!')))
    db.add(User(email='viewer@demo.local', name='v', role='viewer', password=hash_password('Demo123!')))
    db.add(Dataset(name='ds', source='s', records=[{'run': 'r', 'arm': 'A', 'shift': '早班', 'seconds': 1.0},
                                                   {'run': 'r2', 'arm': 'B', 'shift': '早班', 'seconds': 2.0}],
                   checksum='c', created_at=time.time()))
    db.commit()

with TestClient(app) as client:
    # 鉴权
    r = client.post('/auth/login', json={'email': 'analyst@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    token = r.json()['access_token']
    H = {'Authorization': f'Bearer {token}'}
    rv = client.post('/auth/login', json={'email': 'viewer@demo.local', 'password': 'Demo123!'})
    HV = {'Authorization': f'Bearer {rv.json()["access_token"]}'}

    # 建任务
    r = client.post('/jobs', headers=H, json={'name': 'HTTP 集成任务', 'dataset_id': 1, 'params': {}})
    assert r.status_code == 201, r.text
    job = r.json()
    jid = job['id']
    assert job['status'] == 'queued' and job['attempt'] == 0 and job['worker_id'] is None
    print('ok - 建任务返回排队状态与租约字段')

    # 事件接口：queued 已落库（通知丢失也能被轮询领到）
    r = client.get(f'/jobs/{jid}/events', headers=H)
    assert r.status_code == 200
    events = r.json()
    assert [e['event'] for e in events] == ['queued'], events
    print('ok - 任务页可查排队经过')

    # viewer 不能取消
    r = client.post(f'/jobs/{jid}/cancel', headers=HV)
    assert r.status_code == 403, r.text
    print('ok - 观察员无取消权限')

    # analyst 取消排队任务
    r = client.post(f'/jobs/{jid}/cancel', headers=H)
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'cancelled'
    print('ok - 排队任务可取消')

    # 重复取消 409
    r = client.post(f'/jobs/{jid}/cancel', headers=H)
    assert r.status_code == 409, r.text
    print('ok - 终态重复取消返回 409')

    # 取消经过
    events = client.get(f'/jobs/{jid}/events', headers=H).json()
    assert [e['event'] for e in events] == ['queued', 'cancelled'], events
    assert events[-1]['actor'] == 'analyst@demo.local'
    print('ok - 取消经过可查且记录操作人')

    # 任务详情含 cancelled
    r = client.get(f'/jobs/{jid}', headers=H)
    assert r.json()['status'] == 'cancelled' and r.json()['finished_at']
    print('ok - 详情接口返回取消终态')

    # viewer 不能建任务
    r = client.post('/jobs', headers=HV, json={'name': '被拒任务', 'dataset_id': 1, 'params': {}})
    assert r.status_code == 403
    print('ok - 观察员不能创建任务')

    # 未知任务 404
    assert client.get('/jobs/9999/events', headers=H).status_code == 404
    assert client.post('/jobs/9999/cancel', headers=H).status_code == 404
    print('ok - 未知任务返回 404')

    # 再建一个运行态任务，验证运行中取消链路
    j2 = client.post('/jobs', headers=H, json={'name': '运行中取消', 'dataset_id': 1, 'params': {}}).json()
    from src.services import leases
    with Session() as db:
        claimed, _ = leases.claim_next(db, job_id=j2['id'], worker_id='worker-http')
        assert claimed.attempt == 1
    r = client.post(f"/jobs/{j2['id']}/cancel", headers=H)
    assert r.status_code == 200 and r.json()['status'] == 'cancelled'
    ev = [e['event'] for e in client.get(f"/jobs/{j2['id']}/events", headers=H).json()]
    assert ev == ['queued', 'claimed', 'cancelled'], ev
    print('ok - 运行中任务可取消，经过完整')

print('\nHTTP INTEGRATION ALL PASSED')
