from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    bot_token: str
    admin_api_key: str
    database_url: str
    public_bot_username: str
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

settings = Settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

class Base(AsyncAttrs, DeclarativeBase):
    pass

class Organization(Base):
    __tablename__ = 'organizations'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)

class Employee(Base):
    __tablename__ = 'employees'
    __table_args__ = (UniqueConstraint('organization_id','external_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(250))
    region: Mapped[str] = mapped_column(String(200), default='')
    position: Mapped[str] = mapped_column(String(200), default='')
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Survey(Base):
    __tablename__ = 'surveys'
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'))
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[str] = mapped_column(Text, default='')
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Candidate(Base):
    __tablename__ = 'candidates'
    __table_args__ = (UniqueConstraint('survey_id','employee_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey('surveys.id'))
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'))

class Channel(Base):
    __tablename__ = 'channels'
    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey('surveys.id'))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    invite_url: Mapped[str] = mapped_column(String(500))

class Vote(Base):
    __tablename__ = 'votes'
    __table_args__ = (UniqueConstraint('survey_id','telegram_user_id', name='uq_vote_per_survey'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey('surveys.id'))
    candidate_id: Mapped[int] = mapped_column(ForeignKey('candidates.id'))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
