from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models

# 多端接力需要的新列；对已存在的库做幂等 ALTER，新库由 create_all 直接建好。
JOB_COLUMNS = {
    'worker_id': 'VARCHAR(80) NULL',
    'attempt': 'INTEGER NOT NULL DEFAULT 0',
    'lease_until': 'DOUBLE NULL',
    'heartbeat_at': 'DOUBLE NULL',
}

def _upgrade():
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if 'jobs' in tables:
        existing = {c['name'] for c in inspector.get_columns('jobs')}
        with engine.begin() as conn:
            for name, ddl in JOB_COLUMNS.items():
                if name not in existing:
                    conn.execute(text(f'ALTER TABLE jobs ADD COLUMN {name} {ddl}'))

def migrate():
    Base.metadata.create_all(engine)
    _upgrade()

if __name__=='__main__':migrate()
