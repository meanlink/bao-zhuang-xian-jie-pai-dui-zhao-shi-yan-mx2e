from sqlalchemy import String, Double, JSON, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class JobEvent(Base):
    '''单个任务的领取/失联/接手/取消经过，按任务可复查。'''
    __tablename__ = 'job_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'), index=True)
    event: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(150))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)
