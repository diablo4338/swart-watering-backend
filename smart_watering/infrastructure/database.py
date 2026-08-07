import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint, Float, ForeignKey, Index, Integer, String, UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool


class DatabaseError(RuntimeError):
    pass


class Base(DeclarativeBase):
    pass


class DeviceRecord(Base):
    __tablename__ = "devices"
    __table_args__ = (CheckConstraint("device_type IN ('plant', 'tank')", name="ck_devices_device_type"),)

    name: Mapped[str] = mapped_column(String, primary_key=True)
    ip: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    device_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class OperationRecord(Base):
    __tablename__ = "operations"

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_name: Mapped[str] = mapped_column(String, nullable=False)
    operation_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(String, nullable=True)
    result_json: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class OperationEventRecord(Base):
    __tablename__ = "operation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("operations.operation_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class CommandQueueRecord(Base):
    __tablename__ = "command_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("operations.operation_id", ondelete="CASCADE"),
        nullable=False,
    )
    device_name: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class UserRecord(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    password_salt: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class UserSessionRecord(Base):
    __tablename__ = "user_sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.username", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class PlantWateringEventRecord(Base):
    __tablename__ = "plant_watering_events"
    __table_args__ = (
        UniqueConstraint(
            "device_name", "event_start_at",
            name="uq_plant_watering_events_device_start",
        ),
        Index(
            "ix_plant_watering_events_device_occurred",
            "device_name", "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        nullable=False,
    )
    event_start_at: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[float] = mapped_column(Float, nullable=False)
    weight_before_g: Mapped[float] = mapped_column(Float, nullable=False)
    weight_after_g: Mapped[float] = mapped_column(Float, nullable=False)
    amount_g: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    fertilized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detected_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class DatabaseStore:
    def __init__(self, db_path: str, migrations_dir: str | None = None) -> None:
        self.db_path = db_path
        package_dir = os.path.dirname(os.path.dirname(__file__))
        self.migrations_dir = migrations_dir or os.path.join(package_dir, "migrations")
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    def ensure_dir(self) -> None:
        directory = os.path.dirname(self.db_path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(f"cannot create database directory {directory}: {exc}") from exc

    def validate_path(self) -> None:
        if not self.db_path:
            raise DatabaseError("database path must not be empty")

        self.ensure_dir()
        directory = os.path.dirname(self.db_path) or "."

        if not os.path.isdir(directory):
            raise DatabaseError(f"database directory does not exist: {directory}")

        if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
            raise DatabaseError(f"database directory is not accessible: {directory}")

        if os.path.exists(self.db_path):
            if not os.path.isfile(self.db_path):
                raise DatabaseError(f"database path is not a file: {self.db_path}")
            if not os.access(self.db_path, os.R_OK | os.W_OK):
                raise DatabaseError(f"database file is not readable and writable: {self.db_path}")
            return

        try:
            with open(self.db_path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise DatabaseError(f"cannot create database file {self.db_path}: {exc}") from exc

        if not os.access(self.db_path, os.R_OK | os.W_OK):
            raise DatabaseError(f"database file is not readable and writable: {self.db_path}")

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self.validate_path()
            try:
                self._engine = create_engine(
                    f"sqlite:///{self.db_path}",
                    connect_args={"check_same_thread": False},
                    poolclass=NullPool,
                    future=True,
                )
            except SQLAlchemyError as exc:
                raise DatabaseError(f"cannot open database {self.db_path}: {exc}") from exc
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            self._session_factory = sessionmaker(self.engine, expire_on_commit=False, future=True)
        return self._session_factory

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise DatabaseError(str(exc)) from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def migrate(self) -> None:
        self.validate_path()
        config = Config()
        config.set_main_option("script_location", self.migrations_dir)
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")
        try:
            command.upgrade(config, "head")
        except Exception as exc:
            raise DatabaseError(f"cannot migrate database {self.db_path}: {exc}") from exc


def record_to_mapping(record: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(record, field) for field in fields}
