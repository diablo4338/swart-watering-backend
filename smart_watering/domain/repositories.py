import json
import time
import urllib.parse
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smart_watering.infrastructure.database import (
    CommandQueueRecord, DeviceRecord, DeviceWateringSettingsRecord, OperationEventRecord, OperationRecord,
    PlantWateringEventRecord, UserRecord, UserSessionRecord, record_to_mapping,
)
from .foundation import (
    DEVICE_TYPES,
    OP_ACCEPTED,
    OP_CANCELLED,
    OP_ERROR,
    OP_QUEUED,
    OP_RUNNING,
    OP_SENDING,
    OP_STATUS_RANK,
    OP_SUCCESS,
    OP_TERMINAL_STATUSES,
    OP_TIMEOUT,
    PASSWORD_HASH_ITERATIONS,
    Device,
    DeviceNameConflictError,
    DeviceType,
    QueuedCommand,
    SQLiteStore,
    SmartWateringError,
    User,
    UserSession,
)

class DeviceRegistry:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def normalize_base_url(ip_or_url: str) -> tuple[str, str]:
        parsed = urllib.parse.urlparse(ip_or_url)
        if parsed.scheme:
            host = parsed.hostname or ip_or_url
            return host, ip_or_url.rstrip("/")
        return ip_or_url, f"http://{ip_or_url}"

    def next_name(self, device_type: str) -> str:
        if device_type == DeviceType.TANK:
            return DeviceType.TANK.value

        with self.store.session() as session:
            rows = session.scalars(
                select(DeviceRecord.name).where(DeviceRecord.name.like(f"{DeviceType.PLANT}_%"))
            ).all()

        used = set(rows)
        index = 1
        while f"{DeviceType.PLANT}_{index}" in used:
            index += 1
        return f"{DeviceType.PLANT}_{index}"

    @staticmethod
    def demote_other_tanks(session: Session, tank_name: str, updated_at: float) -> None:
        rows = session.scalars(
            select(DeviceRecord).where(
                DeviceRecord.device_type == DeviceType.TANK,
                DeviceRecord.name != tank_name,
            )
        ).all()
        for row in rows:
            row.device_type = DeviceType.PLANT
            row.updated_at = updated_at

    def add(self, ip_or_url: str, device_type: str, name: str | None) -> Device:
        if device_type not in DEVICE_TYPES:
            raise SmartWateringError(f"unsupported device type: {device_type}")

        ip, base_url = self.normalize_base_url(ip_or_url)
        now = time.time()
        device_name = name or self.next_name(device_type)

        with self.store.session() as session:
            existing = session.scalar(select(DeviceRecord).where(DeviceRecord.name == device_name))
            if existing is not None and device_type == DeviceType.TANK:
                return Device(existing.id, existing.name, existing.ip, existing.base_url, device_type, existing.created_at, now)
            if existing is not None:
                raise DeviceNameConflictError(f"device name already exists: {device_name}")

            session.add(
                DeviceRecord(
                    name=device_name,
                    ip=ip,
                    base_url=base_url,
                    device_type=DeviceType.PLANT,
                    created_at=now,
                    updated_at=now,
                )
            )

        return self.get(device_name)

    def upsert_discovered(self, ip_or_url: str, device_type: str, name: str) -> Device:
        """Store the identity reported by an online controller without changing it."""
        if device_type not in DEVICE_TYPES:
            raise SmartWateringError(f"unsupported device type: {device_type}")
        if not name:
            raise SmartWateringError("device name must not be empty")

        ip, base_url = self.normalize_base_url(ip_or_url)
        now = time.time()
        with self.store.session() as session:
            existing = session.scalar(select(DeviceRecord).where(DeviceRecord.name == name))
            if device_type == DeviceType.TANK:
                self.demote_other_tanks(session, name, now)
            if existing is None:
                session.add(
                    DeviceRecord(
                        name=name,
                        ip=ip,
                        base_url=base_url,
                        device_type=device_type,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created_at = now
            else:
                existing.ip = ip
                existing.base_url = base_url
                existing.device_type = device_type
                existing.updated_at = now
                created_at = existing.created_at

        return self.get(name)

    def validate_config_update(self, current_name: str, config: dict[str, Any]) -> Device:
        device = self.get(current_name)
        new_name = str(config.get("name", device.name))
        new_type = str(config.get("device_type", device.device_type))

        if new_type not in DEVICE_TYPES:
            raise SmartWateringError(f"unsupported device type: {new_type}")

        with self.store.session() as session:
            if new_name != current_name and session.scalar(select(DeviceRecord).where(DeviceRecord.name == new_name)) is not None:
                raise DeviceNameConflictError(f"device name already exists: {new_name}")

        return Device(device.id, new_name, device.ip, device.base_url, new_type, device.created_at, time.time())

    def apply_confirmed_config(self, current_name: str, config: dict[str, Any]) -> Device:
        device = self.get(current_name)
        new_name = str(config.get("name", device.name))
        new_type = str(config.get("device_type", device.device_type))

        if new_type not in DEVICE_TYPES:
            raise SmartWateringError(f"unsupported device type: {new_type}")

        now = time.time()
        with self.store.session() as session:
            if new_name != current_name and session.scalar(select(DeviceRecord).where(DeviceRecord.name == new_name)) is not None:
                raise DeviceNameConflictError(f"device name already exists: {new_name}")

            if new_type == DeviceType.TANK:
                self.demote_other_tanks(session, new_name, now)

            updated = session.get(DeviceRecord, device.id)
            if updated is None:
                raise SmartWateringError(f"unknown device: {current_name}")
            updated.name = new_name
            updated.ip = device.ip
            updated.base_url = device.base_url
            updated.device_type = new_type
            updated.updated_at = now
            try:
                session.flush()
            except IntegrityError as exc:
                raise DeviceNameConflictError(
                    f"device name already exists: {new_name}"
                ) from exc

        return Device(device.id, new_name, device.ip, device.base_url, new_type, device.created_at, now)

    def list(self) -> list[Device]:
        with self.store.session() as session:
            rows = session.scalars(select(DeviceRecord).order_by(DeviceRecord.device_type, DeviceRecord.name)).all()
        return [
            Device(row.id, row.name, row.ip, row.base_url, row.device_type, row.created_at, row.updated_at)
            for row in rows
        ]

    def get(self, name: str) -> Device:
        with self.store.session() as session:
            row = session.scalar(select(DeviceRecord).where(DeviceRecord.name == name))

        if row is None:
            raise SmartWateringError(f"unknown device: {name}")

        return Device(row.id, row.name, row.ip, row.base_url, row.device_type, row.created_at, row.updated_at)

    def remove(self, name: str) -> None:
        with self.store.session() as session:
            result = session.execute(delete(DeviceRecord).where(DeviceRecord.name == name))
            if result.rowcount == 0:
                raise SmartWateringError(f"unknown device: {name}")

    def watering_settings(self, name: str) -> dict[str, float | None]:
        self.get(name)
        with self.store.session() as session:
            row = session.get(DeviceWateringSettingsRecord, self.get(name).id)
        keys = (
            "dry_weight_g", "dry_weight_updated_at", "wet_weight_g",
            "wet_weight_updated_at", "watering_loss_threshold_percent",
            "watering_loss_threshold_updated_at",
        )
        return {key: getattr(row, key) if row is not None else None for key in keys}

    def confirm_watering_settings(
        self, name: str, config: dict[str, Any], changed_at: float
    ) -> None:
        with self.store.session() as session:
            device_id = session.scalar(select(DeviceRecord.id).where(DeviceRecord.name == name))
            if device_id is None:
                raise SmartWateringError(f"unknown device: {name}")
            row = session.get(DeviceWateringSettingsRecord, device_id)
            if row is None:
                row = DeviceWateringSettingsRecord(device_id=device_id)
                session.add(row)
            timestamp_keys = {
                "dry_weight_g": "dry_weight_updated_at",
                "wet_weight_g": "wet_weight_updated_at",
                "watering_loss_threshold_percent": "watering_loss_threshold_updated_at",
            }
            for key, timestamp_key in timestamp_keys.items():
                value = config.get(key)
                if isinstance(value, (int, float)):
                    setattr(row, key, int(value))
                    setattr(row, timestamp_key, changed_at)


class PlantWateringEventStore:
    FIELDS = (
        "id", "device_id", "event_start_at", "occurred_at",
        "weight_before_g", "weight_after_g", "amount_g", "source",
        "fertilized", "invalid", "detected_at", "updated_at",
    )

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _device_id(session: Session, name: str) -> str:
        device_id = session.scalar(select(DeviceRecord.id).where(DeviceRecord.name == name))
        if device_id is None:
            raise SmartWateringError(f"unknown device: {name}")
        return device_id

    @classmethod
    def to_mapping(cls, row: PlantWateringEventRecord) -> dict[str, Any]:
        result = record_to_mapping(row, cls.FIELDS)
        result["fertilized"] = bool(result["fertilized"])
        result["invalid"] = bool(result["invalid"])
        return result

    def upsert_detected(
        self, device_name: str, event: dict[str, float], detected_at: float | None = None
    ) -> tuple[dict[str, Any], bool]:
        now = detected_at if detected_at is not None else time.time()
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            row = session.scalar(
                select(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.device_id == device_id,
                    PlantWateringEventRecord.event_start_at == event["event_start_at"],
                )
            )
            matched_by_start = row is not None
            if row is None:
                row = session.scalar(
                    select(PlantWateringEventRecord).where(
                        PlantWateringEventRecord.device_id == device_id,
                        PlantWateringEventRecord.occurred_at == event["occurred_at"],
                        PlantWateringEventRecord.weight_after_g
                        == event["weight_after_g"],
                    )
                )
            created = row is None
            if row is None:
                row = PlantWateringEventRecord(
                    device_id=device_id,
                    event_start_at=event["event_start_at"],
                    occurred_at=event["occurred_at"],
                    weight_before_g=event["weight_before_g"],
                    weight_after_g=event["weight_after_g"],
                    amount_g=event["amount_g"],
                    source="prometheus",
                    fertilized=0,
                    invalid=0,
                    detected_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            elif (
                matched_by_start
                and not row.invalid
                and event["amount_g"] > row.amount_g
            ):
                row.occurred_at = event["occurred_at"]
                row.weight_after_g = event["weight_after_g"]
                row.amount_g = event["amount_g"]
                row.updated_at = now
        return self.to_mapping(row), created

    def list_valid_page(
        self, device_name: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], bool]:
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            rows = session.scalars(
                select(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.device_id == device_id,
                    PlantWateringEventRecord.invalid == 0,
                ).order_by(
                    PlantWateringEventRecord.occurred_at.desc(),
                    PlantWateringEventRecord.id.desc(),
                ).offset(offset).limit(limit + 1)
            ).all()
        return [self.to_mapping(row) for row in rows[:limit]], len(rows) > limit

    def list_valid(self, device_name: str) -> list[dict[str, Any]]:
        rows, _has_more = self.list_valid_page(device_name, 1_000_000, 0)
        return rows

    def hard_drop(self, device_name: str | None = None) -> int:
        with self.store.session() as session:
            statement = delete(PlantWateringEventRecord)
            if device_name is not None:
                device_id = self._device_id(session, device_name)
                statement = statement.where(
                    PlantWateringEventRecord.device_id == device_id
                )
            result = session.execute(statement)
        return int(result.rowcount or 0)

    def invalidate(self, device_name: str, event_id: int) -> bool:
        now = time.time()
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            result = session.execute(
                update(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.id == event_id,
                    PlantWateringEventRecord.device_id == device_id,
                    PlantWateringEventRecord.invalid == 0,
                ).values(invalid=1, updated_at=now)
            )
        return bool(result.rowcount)

    def set_fertilized(
        self, device_name: str, event_id: int, fertilized: bool
    ) -> dict[str, Any] | None:
        now = time.time()
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            row = session.scalar(
                select(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.id == event_id,
                    PlantWateringEventRecord.device_id == device_id,
                    PlantWateringEventRecord.invalid == 0,
                )
            )
            if row is None:
                return None
            row.fertilized = int(fertilized)
            row.updated_at = now
            session.flush()
            return self.to_mapping(row)

    def invalidate_above_amount(self, device_name: str, max_amount_g: float) -> int:
        now = time.time()
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            result = session.execute(
                update(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.device_id == device_id,
                    PlantWateringEventRecord.invalid == 0,
                    PlantWateringEventRecord.amount_g > max_amount_g,
                ).values(invalid=1, updated_at=now)
            )
        return int(result.rowcount or 0)

    def invalidate_exact_duplicates(self, device_name: str) -> int:
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            rows = session.scalars(
                select(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.device_id == device_id
                ).order_by(PlantWateringEventRecord.id)
            ).all()
            grouped: dict[tuple[float, float, float], list[PlantWateringEventRecord]] = {}
            for row in rows:
                grouped.setdefault(
                    (row.occurred_at, row.weight_before_g, row.weight_after_g), []
                ).append(row)
            changed = 0
            now = time.time()
            for duplicates in grouped.values():
                if len(duplicates) < 2:
                    continue
                # The oldest row is canonical. If it was manually invalidated,
                # keeping it invalid and invalidating later copies prevents a
                # detector rerun from resurrecting the event.
                for row in duplicates[1:]:
                    if not row.invalid:
                        row.invalid = 1
                        row.updated_at = now
                        changed += 1
        return changed

    def invalidate_events_inside(
        self, device_name: str, start: float, end: float, amount_below: float
    ) -> int:
        now = time.time()
        with self.store.session() as session:
            device_id = self._device_id(session, device_name)
            result = session.execute(
                update(PlantWateringEventRecord).where(
                    PlantWateringEventRecord.device_id == device_id,
                    PlantWateringEventRecord.invalid == 0,
                    PlantWateringEventRecord.occurred_at > start,
                    PlantWateringEventRecord.occurred_at < end,
                    PlantWateringEventRecord.amount_g < amount_below,
                ).values(invalid=1, updated_at=now)
            )
        return int(result.rowcount or 0)


class AuthStore:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def validate_username(username: str) -> str:
        username = username.strip()
        if not username:
            raise SmartWateringError("username must not be empty")
        if len(username) > 64:
            raise SmartWateringError("username must be at most 64 characters")
        if any(char.isspace() for char in username):
            raise SmartWateringError("username must not contain whitespace")
        return username

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 8:
            raise SmartWateringError("password must be at least 8 characters")

    @staticmethod
    def encode_salt(salt: bytes) -> str:
        import base64

        return base64.b64encode(salt).decode("ascii")

    @staticmethod
    def decode_salt(salt: str) -> bytes:
        import base64

        return base64.b64decode(salt.encode("ascii"))

    @staticmethod
    def hash_password(password: str, salt: bytes) -> str:
        import base64
        import hashlib

        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
        return base64.b64encode(digest).decode("ascii")

    def add_user(self, username: str, password: str, replace: bool = False) -> User:
        import secrets

        username = self.validate_username(username)
        self.validate_password(password)
        now = time.time()
        salt = secrets.token_bytes(16)
        password_salt = self.encode_salt(salt)
        password_hash = self.hash_password(password, salt)

        with self.store.session() as session:
            existing = session.get(UserRecord, username)
            if existing is not None and not replace:
                raise SmartWateringError(f"user already exists: {username}")
            if existing is None:
                session.add(
                    UserRecord(
                        username=username,
                        password_salt=password_salt,
                        password_hash=password_hash,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created_at = now
            else:
                existing.password_salt = password_salt
                existing.password_hash = password_hash
                existing.updated_at = now
                created_at = existing.created_at

        return User(username, created_at, now)

    def list_users(self) -> list[User]:
        with self.store.session() as session:
            rows = session.scalars(select(UserRecord).order_by(UserRecord.username)).all()
        return [User(row.username, row.created_at, row.updated_at) for row in rows]

    def drop_user(self, username: str) -> None:
        username = self.validate_username(username)
        with self.store.session() as session:
            session.execute(delete(UserSessionRecord).where(UserSessionRecord.username == username))
            result = session.execute(delete(UserRecord).where(UserRecord.username == username))
            if result.rowcount == 0:
                raise SmartWateringError(f"unknown user: {username}")

    def verify_password(self, username: str, password: str) -> bool:
        import hmac

        username = self.validate_username(username)
        with self.store.session() as session:
            row = session.get(UserRecord, username)
        if row is None:
            return False
        salt = self.decode_salt(row.password_salt)
        password_hash = self.hash_password(password, salt)
        return hmac.compare_digest(password_hash, row.password_hash)

    def create_session(self, username: str, password: str, ttl_sec: int) -> UserSession:
        if ttl_sec <= 0:
            raise SmartWateringError("session ttl must be > 0")
        if not self.verify_password(username, password):
            raise SmartWateringError("invalid username or password")
        return self.create_session_for_user(username, ttl_sec)

    @staticmethod
    def external_username(provider: str, subject: str) -> str:
        import hashlib

        provider = provider.strip().lower()
        subject = subject.strip()
        username = f"{provider}:{subject}"
        if len(username) > 64 or any(char.isspace() for char in username):
            username = f"{provider}:{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:32]}"
        return AuthStore.validate_username(username)

    def ensure_external_user(self, provider: str, subject: str) -> User:
        import secrets

        username = self.external_username(provider, subject)
        now = time.time()
        with self.store.session() as session:
            existing = session.get(UserRecord, username)
            if existing is not None:
                return User(existing.username, existing.created_at, existing.updated_at)

            salt = secrets.token_bytes(16)
            session.add(
                UserRecord(
                    username=username,
                    password_salt=self.encode_salt(salt),
                    password_hash=self.hash_password(secrets.token_urlsafe(32), salt),
                    created_at=now,
                    updated_at=now,
                )
            )
        return User(username, now, now)

    def create_external_session(self, provider: str, subject: str, ttl_sec: int) -> UserSession:
        user = self.ensure_external_user(provider, subject)
        return self.create_session_for_user(user.username, ttl_sec)

    def create_session_for_user(self, username: str, ttl_sec: int) -> UserSession:
        if ttl_sec <= 0:
            raise SmartWateringError("session ttl must be > 0")
        username = self.validate_username(username)
        with self.store.session() as session:
            if session.get(UserRecord, username) is None:
                raise SmartWateringError("unknown user")
        now = time.time()
        user_session = UserSession(
            session_id=str(uuid.uuid4()),
            username=username,
            created_at=now,
            expires_at=now + ttl_sec,
            revoked_at=None,
        )
        with self.store.session() as session:
            session.add(
                UserSessionRecord(
                    session_id=user_session.session_id,
                    username=user_session.username,
                    created_at=user_session.created_at,
                    expires_at=user_session.expires_at,
                    revoked_at=None,
                )
            )
        return user_session

    def get_session(self, session_id: str) -> UserSession | None:
        with self.store.session() as session:
            row = session.get(UserSessionRecord, session_id)
        if row is None:
            return None
        return UserSession(row.session_id, row.username, row.created_at, row.expires_at, row.revoked_at)

    def require_active_session(self, session_id: str) -> UserSession:
        user_session = self.get_session(session_id)
        if user_session is None:
            raise SmartWateringError("unknown session")
        if user_session.revoked_at is not None:
            raise SmartWateringError("session revoked")
        if user_session.expires_at < time.time():
            raise SmartWateringError("session expired")
        return user_session

    def revoke_session(self, session_id: str) -> None:
        with self.store.session() as session:
            session.execute(
                update(UserSessionRecord)
                .where(UserSessionRecord.session_id == session_id)
                .values(revoked_at=time.time())
            )


class OperationLog:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def log(message: str) -> None:
        print(f"operations: {message}", flush=True)

    def create(self, device_name: str, operation_type: str, payload: dict[str, Any]) -> str:
        operation_id = str(uuid.uuid4())
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with self.store.session() as session:
            device_id = session.scalar(select(DeviceRecord.id).where(DeviceRecord.name == device_name))
            session.add(
                OperationRecord(
                    operation_id=operation_id,
                    device_id=device_id,
                    target_name=device_name if device_id is None else None,
                    operation_type=operation_type,
                    payload_json=payload_json,
                    status=OP_QUEUED,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                OperationEventRecord(
                    operation_id=operation_id,
                    status=OP_QUEUED,
                    detail="operation queued",
                    created_at=now,
                )
            )

        self.log(
            f"created operation_id={operation_id} device={device_name} "
            f"type={self.public_type(operation_type)} status={OP_QUEUED}"
        )
        return operation_id

    def event(self, operation_id: str, status: str, detail: str) -> None:
        now = time.time()
        with self.store.session() as session:
            row = session.get(OperationRecord, operation_id)
            if row is None:
                self.log(f"event skipped operation_id={operation_id} status={status} reason=not_found")
                return
            if (
                row.operation_type == "watering_stop"
                and status == OP_ERROR
                and detail.strip().lower() == "watering_not_active"
            ):
                status = OP_SUCCESS
                detail = "no active watering"
            if row.status in OP_TERMINAL_STATUSES:
                misclassified_ack = False
                if row.status == OP_ERROR:
                    latest_error = session.scalars(
                        select(OperationEventRecord)
                        .where(
                            OperationEventRecord.operation_id == operation_id,
                            OperationEventRecord.status == OP_ERROR,
                        )
                        .order_by(OperationEventRecord.created_at.desc(), OperationEventRecord.id.desc())
                        .limit(1)
                    ).first()
                    misclassified_ack = (
                        latest_error is not None
                        and latest_error.detail.strip().lower() in {"accepted", "received"}
                    )
                if not misclassified_ack:
                    self.log(
                        f"event skipped operation_id={operation_id} status={status} "
                        f"reason=terminal current_status={row.status}"
                    )
                    return
            if OP_STATUS_RANK.get(status, 0) < OP_STATUS_RANK.get(row.status, 0):
                self.log(
                    f"event skipped operation_id={operation_id} status={status} "
                    f"reason=status_regression current_status={row.status}"
                )
                return
            device_name = row.device_name
            operation_type = row.operation_type
            payload_json = row.payload_json
            session.execute(
                update(OperationRecord)
                .where(OperationRecord.operation_id == operation_id)
                .values(status=status, updated_at=now)
            )
            session.add(
                OperationEventRecord(
                    operation_id=operation_id,
                    status=status,
                    detail=detail,
                    created_at=now,
                )
            )
        self.log(
            f"event operation_id={operation_id} device={device_name} "
            f"type={self.public_type(operation_type)} status={status} detail={detail!r}"
        )
        if operation_type == "config" and status == OP_SUCCESS:
            try:
                payload = json.loads(payload_json or "{}")
                if isinstance(payload, dict):
                    registry = DeviceRegistry(self.store)
                    confirmed_device = registry.apply_confirmed_config(device_name, payload)
                    registry.confirm_watering_settings(confirmed_device.name, payload, now)
            except (json.JSONDecodeError, SmartWateringError) as exc:
                self.log(
                    f"confirmed config apply skipped operation_id={operation_id} "
                    f"device={device_name} reason={exc}"
                )
        elif operation_type == "device_status" and status == OP_SUCCESS:
            try:
                result = json.loads(row.result_json or "{}")
                config = result.get("config") if isinstance(result, dict) else None
                if isinstance(config, dict):
                    DeviceRegistry(self.store).confirm_watering_settings(
                        device_name, config, now
                    )
            except (json.JSONDecodeError, SmartWateringError) as exc:
                self.log(
                    f"status settings recovery skipped operation_id={operation_id} "
                    f"device={device_name} reason={exc}"
                )

    def is_cancelled(self, operation_id: str) -> bool:
        with self.store.session() as session:
            status = session.scalar(
                select(OperationRecord.status).where(OperationRecord.operation_id == operation_id)
            )
        return status == OP_CANCELLED

    def cancel_active_watering_starts(self, device_name: str, detail: str) -> list[str]:
        now = time.time()
        with self.store.session() as session:
            rows = session.scalars(
                select(OperationRecord).where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.operation_type.in_(["fill", "watering_start"]),
                    OperationRecord.status.not_in(OP_TERMINAL_STATUSES),
                )
            ).all()
            for row in rows:
                row.status = OP_CANCELLED
                row.updated_at = now
                session.add(
                    OperationEventRecord(
                        operation_id=row.operation_id,
                        status=OP_CANCELLED,
                        detail=detail,
                        created_at=now,
                    )
                )
        operation_ids = [row.operation_id for row in rows]
        if operation_ids:
            self.log(
                f"cancelled watering_start device={device_name} count={len(operation_ids)} "
                f"operation_ids={','.join(operation_ids)} detail={detail!r}"
            )
        return operation_ids

    def cancel_non_terminal(self, device_name: str, detail: str) -> list[str]:
        now = time.time()
        with self.store.session() as session:
            rows = session.scalars(
                select(OperationRecord).where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.status.not_in(OP_TERMINAL_STATUSES),
                )
            ).all()
            for row in rows:
                row.status = OP_CANCELLED
                row.updated_at = now
                session.add(
                    OperationEventRecord(
                        operation_id=row.operation_id,
                        status=OP_CANCELLED,
                        detail=detail,
                        created_at=now,
                    )
                )
        operation_ids = [row.operation_id for row in rows]
        if operation_ids:
            self.log(
                f"cancelled non-terminal operations device={device_name} "
                f"count={len(operation_ids)} operation_ids={','.join(operation_ids)} "
                f"detail={detail!r}"
            )
        return operation_ids

    def update_payload(self, operation_id: str, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self.store.session() as session:
            session.execute(
                update(OperationRecord)
                .where(OperationRecord.operation_id == operation_id)
                .values(payload_json=payload_json, updated_at=now)
            )

    def update_result(self, operation_id: str, result: dict[str, Any]) -> None:
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self.store.session() as session:
            session.execute(
                update(OperationRecord)
                .where(OperationRecord.operation_id == operation_id)
                .values(result_json=result_json, updated_at=now)
            )

    def get(self, operation_id: str) -> dict[str, Any] | None:
        fields = (
            "operation_id",
            "device_name",
            "operation_type",
            "payload_json",
            "result_json",
            "status",
            "created_at",
            "updated_at",
        )
        with self.store.session() as session:
            row = session.get(OperationRecord, operation_id)
        return record_to_mapping(row, fields) if row is not None else None

    def events(self, operation_id: str) -> list[dict[str, Any]]:
        fields = ("operation_id", "status", "detail", "created_at")
        with self.store.session() as session:
            rows = session.scalars(
                select(OperationEventRecord)
                .where(OperationEventRecord.operation_id == operation_id)
                .order_by(OperationEventRecord.created_at, OperationEventRecord.id)
            ).all()
        return [record_to_mapping(row, fields) for row in rows]

    @staticmethod
    def error_from_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event["status"] in {OP_ERROR, OP_TIMEOUT, OP_CANCELLED}:
                detail = str(event["detail"])
                if event["status"] == OP_ERROR and detail.strip().lower() in {"accepted", "received"}:
                    continue
                return {
                    "code": event["status"],
                    "message": detail,
                    "detail": detail,
                    "retryable": event["status"] in {OP_ERROR, OP_TIMEOUT},
                }
        return None

    @staticmethod
    def payload(operation: dict[str, Any]) -> dict[str, Any]:
        payload_json = operation.get("payload_json")
        if not payload_json:
            return {}
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def result(operation: dict[str, Any]) -> dict[str, Any] | None:
        result_json = operation.get("result_json")
        if not result_json:
            return None
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def public_type(operation_type: str) -> str:
        return {
            "fill": "watering_start",
            "stop": "watering_stop",
            "config": "device_config",
            "watering_start": "watering_start",
            "watering_stop": "watering_stop",
            "sleep_enable": "sleep_enable",
            "sleep_disable": "sleep_disable",
            "sleep_interval": "sleep_interval",
            "device_status": "device_status",
            "zero_capture": "zero_capture",
            "scale_calibration": "scale_calibration",
        }.get(operation_type, operation_type)

    def detail_from_operation(
        self,
        operation: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = self.payload(operation)
        created_at = operation["created_at"]
        updated_at = operation["updated_at"]
        started_at = next(
            (event["created_at"] for event in events if event["status"] in {OP_SENDING, OP_ACCEPTED, OP_RUNNING}),
            None,
        )
        finished_at = next(
            (event["created_at"] for event in reversed(events) if event["status"] in OP_TERMINAL_STATUSES),
            None,
        )
        status = operation["status"]
        terminal_events = [
            event for event in events
            if event["status"] in OP_TERMINAL_STATUSES
        ]
        if terminal_events:
            # A controller callback can race the worker's HTTP acknowledgement.
            # The terminal callback is authoritative even if a concurrent
            # accepted update was the last value persisted on the operation row.
            status = terminal_events[-1]["status"]
        if status == OP_ERROR and any(
            event["status"] == OP_ERROR and str(event["detail"]).strip().lower() in {"accepted", "received"}
            for event in events
        ):
            status = OP_ACCEPTED
        result = self.result(operation)
        result_received_at = None
        if result is not None:
            result_received_at = next(
                (event["created_at"] for event in reversed(events) if event["status"] == OP_SUCCESS),
                operation["updated_at"],
            )

        return {
            "operation_id": operation["operation_id"],
            "device": operation["device_name"],
            "type": self.public_type(operation["operation_type"]),
            "status": status,
            "target_g": payload.get("target_g"),
            "payload": payload,
            "result": result,
            "result_received_at": result_received_at,
            "error": self.error_from_events(events),
            "created_at": created_at,
            "updated_at": updated_at,
            "started_at": started_at,
            "finished_at": finished_at,
        }

    def detail(self, operation_id: str) -> dict[str, Any] | None:
        operation = self.get(operation_id)
        if operation is None:
            return None
        return self.detail_from_operation(operation, self.events(operation_id))

    def details_from_operations(self, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not operations:
            return []
        operation_ids = [operation["operation_id"] for operation in operations]
        event_fields = ("operation_id", "status", "detail", "created_at")
        with self.store.session() as session:
            rows = session.scalars(
                select(OperationEventRecord)
                .where(OperationEventRecord.operation_id.in_(operation_ids))
                .order_by(
                    OperationEventRecord.operation_id,
                    OperationEventRecord.created_at,
                    OperationEventRecord.id,
                )
            ).all()
        events_by_operation = {operation_id: [] for operation_id in operation_ids}
        for row in rows:
            event = record_to_mapping(row, event_fields)
            events_by_operation[event["operation_id"]].append(event)
        return [
            self.detail_from_operation(
                operation,
                events_by_operation[operation["operation_id"]],
            )
            for operation in operations
        ]

    def list_recent(
        self,
        limit: int = 20,
        device_name: str | None = None,
    ) -> list[dict[str, Any]]:
        fields = (
            "operation_id",
            "device_name",
            "operation_type",
            "payload_json",
            "result_json",
            "status",
            "created_at",
            "updated_at",
        )
        query = select(OperationRecord)
        if device_name is not None:
            query = query.where(OperationRecord.device_name == device_name)
        with self.store.session() as session:
            rows = session.scalars(
                query.order_by(OperationRecord.created_at.desc()).limit(limit)
            ).all()
        return [record_to_mapping(row, fields) for row in rows]

    def list_non_terminal(self, device_name: str | None = None) -> list[dict[str, Any]]:
        fields = (
            "operation_id",
            "device_name",
            "operation_type",
            "payload_json",
            "result_json",
            "status",
            "created_at",
            "updated_at",
        )
        conditions = [OperationRecord.status.not_in(OP_TERMINAL_STATUSES)]
        if device_name is not None:
            conditions.append(OperationRecord.device_name == device_name)
        with self.store.session() as session:
            rows = session.scalars(
                select(OperationRecord)
                .where(*conditions)
                .order_by(OperationRecord.created_at.desc())
            ).all()
        return [record_to_mapping(row, fields) for row in rows]

    def devices_with_non_terminal(self, operation_types: set[str]) -> set[str]:
        with self.store.session() as session:
            device_names = session.scalars(
                select(OperationRecord.device_name)
                .where(
                    OperationRecord.operation_type.in_(operation_types),
                    OperationRecord.status.not_in(OP_TERMINAL_STATUSES),
                )
                .distinct()
            ).all()
        return set(device_names)

    def list_recent_watering_starts(self, limit: int = 10, successful_only: bool = False) -> list[dict[str, Any]]:
        fields = (
            "operation_id",
            "device_name",
            "operation_type",
            "payload_json",
            "status",
            "created_at",
            "updated_at",
        )
        conditions = [
            OperationRecord.operation_type.in_(["fill", "watering_start"]),
            OperationRecord.status.in_(OP_TERMINAL_STATUSES),
        ]
        if successful_only:
            conditions.append(OperationRecord.status == OP_SUCCESS)
        with self.store.session() as session:
            rows = session.scalars(
                select(OperationRecord)
                .where(*conditions)
                .order_by(OperationRecord.updated_at.desc())
                .limit(limit)
            ).all()
        return [record_to_mapping(row, fields) for row in rows]

    def latest_successful_result(self, device_name: str, operation_type: str) -> dict[str, Any] | None:
        with self.store.session() as session:
            operation_id = session.scalar(
                select(OperationRecord.operation_id)
                .where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.operation_type == operation_type,
                    OperationRecord.status == OP_SUCCESS,
                    OperationRecord.result_json.is_not(None),
                )
                .order_by(OperationRecord.updated_at.desc())
                .limit(1)
            )
        return self.detail(operation_id) if operation_id is not None else None

    def latest_non_terminal(self, device_name: str, operation_type: str) -> dict[str, Any] | None:
        with self.store.session() as session:
            operation_id = session.scalar(
                select(OperationRecord.operation_id)
                .where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.operation_type == operation_type,
                    OperationRecord.status.not_in(OP_TERMINAL_STATUSES),
                )
                .order_by(OperationRecord.updated_at.desc())
                .limit(1)
            )
        return self.detail(operation_id) if operation_id is not None else None

    def latest_for_device(self, device_name: str, operation_type: str) -> dict[str, Any] | None:
        with self.store.session() as session:
            operation_id = session.scalar(
                select(OperationRecord.operation_id)
                .where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.operation_type == operation_type,
                )
                .order_by(OperationRecord.updated_at.desc())
                .limit(1)
            )
        return self.detail(operation_id) if operation_id is not None else None

    def latest_non_terminal_watering_start(self, device_name: str) -> dict[str, Any] | None:
        with self.store.session() as session:
            operation_id = session.scalar(
                select(OperationRecord.operation_id)
                .where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.operation_type.in_(["fill", "watering_start"]),
                    OperationRecord.status.not_in(OP_TERMINAL_STATUSES),
                )
                .order_by(OperationRecord.updated_at.desc())
                .limit(1)
            )
        return self.detail(operation_id) if operation_id is not None else None

    def latest_terminal_watering_start(self, device_name: str) -> dict[str, Any] | None:
        with self.store.session() as session:
            operation_id = session.scalar(
                select(OperationRecord.operation_id)
                .where(
                    OperationRecord.device_name == device_name,
                    OperationRecord.operation_type.in_(["fill", "watering_start"]),
                    OperationRecord.status.in_(OP_TERMINAL_STATUSES),
                )
                .order_by(OperationRecord.updated_at.desc())
                .limit(1)
            )
        return self.detail(operation_id) if operation_id is not None else None

    def timeout_stale_controller_results(self, max_wait_sec: float) -> int:
        cutoff = time.time() - max_wait_sec
        with self.store.session() as session:
            operation_ids = session.scalars(
                select(OperationRecord.operation_id).where(
                    OperationRecord.status.in_([OP_ACCEPTED, OP_RUNNING]),
                    OperationRecord.updated_at <= cutoff,
                )
            ).all()

        for operation_id in operation_ids:
            self.event(
                operation_id,
                OP_TIMEOUT,
                f"controller result was not received within {max_wait_sec}s",
            )
        return len(operation_ids)


class CommandQueue:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def log(message: str) -> None:
        print(f"queue: {message}", flush=True)

    @staticmethod
    def _row_to_command(row: CommandQueueRecord) -> QueuedCommand:
        payload_json = row.payload_json
        return QueuedCommand(
            id=row.id,
            operation_id=row.operation_id,
            device_id=row.device_id,
            device_name=row.device_name,
            base_url=row.base_url,
            path=row.path,
            method=row.method,
            payload=json.loads(payload_json) if payload_json else None,
            description=row.description,
            created_at=row.created_at,
            started_at=row.started_at,
        )

    @staticmethod
    def dedupe_payload_json(payload_json: str | None) -> str:
        if not payload_json:
            return ""
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return payload_json
        if not isinstance(payload, dict):
            return payload_json
        stable_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"operation_id", "callback_url"}
        }
        return json.dumps(stable_payload, ensure_ascii=False, sort_keys=True)

    def find_duplicate(
        self,
        base_url: str,
        path: str,
        method: str,
        payload: dict[str, Any] | None,
    ) -> str | None:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload is not None else None
        dedupe_payload_json = self.dedupe_payload_json(payload_json)
        with self.store.session() as session:
            duplicate_rows = session.scalars(
                select(CommandQueueRecord).where(
                    CommandQueueRecord.base_url == base_url,
                    CommandQueueRecord.path == path,
                    CommandQueueRecord.method == method,
                )
            ).all()
        for duplicate in duplicate_rows:
            if self.dedupe_payload_json(duplicate.payload_json) == dedupe_payload_json:
                return duplicate.operation_id
        return None

    def enqueue(
        self,
        operation_id: str,
        device_name: str,
        base_url: str,
        path: str,
        method: str,
        payload: dict[str, Any] | None,
        description: str,
        drop_fill_commands: bool = False,
    ) -> str:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload is not None else None
        dedupe_payload_json = self.dedupe_payload_json(payload_json)
        now = time.time()

        with self.store.session() as session:
            device_id = session.scalar(select(DeviceRecord.id).where(DeviceRecord.name == device_name))
            if drop_fill_commands:
                session.execute(delete(CommandQueueRecord).where(CommandQueueRecord.path == "/watering/start"))

            duplicate_rows = session.scalars(
                select(CommandQueueRecord).where(
                    CommandQueueRecord.base_url == base_url,
                    CommandQueueRecord.path == path,
                    CommandQueueRecord.method == method,
                )
            ).all()
            for duplicate in duplicate_rows:
                if self.dedupe_payload_json(duplicate.payload_json) == dedupe_payload_json:
                    self.log(
                        f"enqueue skipped duplicate operation_id={operation_id} "
                        f"existing_operation_id={duplicate.operation_id} "
                        f"device={device_name} method={method} path={path}"
                    )
                    return duplicate.operation_id

            session.add(
                CommandQueueRecord(
                    operation_id=operation_id,
                    device_id=device_id,
                    target_name=device_name if device_id is None else None,
                    base_url=base_url,
                    path=path,
                    method=method,
                    payload_json=payload_json,
                    description=description,
                    created_at=now,
                    started_at=None,
                )
            )
        self.log(
            f"enqueued operation_id={operation_id} device={device_name} "
            f"method={method} path={path} description={description!r}"
        )
        return operation_id

    def peek(self, device_name: str | None = None) -> QueuedCommand | None:
        with self.store.session() as session:
            statement = select(CommandQueueRecord)
            if device_name is not None:
                statement = statement.where(CommandQueueRecord.device_name == device_name)
            row = session.scalars(statement.order_by(CommandQueueRecord.id).limit(1)).first()
        return self._row_to_command(row) if row is not None else None

    def pending_device_names(self) -> list[str]:
        with self.store.session() as session:
            rows = session.scalars(
                select(CommandQueueRecord.device_name)
                .distinct()
                .order_by(CommandQueueRecord.device_name)
            ).all()
        return list(rows)

    def pop(self, command_id: int) -> None:
        with self.store.session() as session:
            session.execute(delete(CommandQueueRecord).where(CommandQueueRecord.id == command_id))
        self.log(f"popped id={command_id}")

    def move_to_tail_if_other(
        self,
        command: QueuedCommand,
        started_at: float,
        device_name: str | None = None,
    ) -> int | None:
        payload_json = (
            json.dumps(command.payload, ensure_ascii=False, sort_keys=True)
            if command.payload is not None
            else None
        )
        with self.store.session() as session:
            statement = select(CommandQueueRecord.id).where(CommandQueueRecord.id != command.id)
            if device_name is not None:
                statement = statement.where(CommandQueueRecord.device_name == device_name)
            if session.scalar(statement.limit(1)) is None:
                return None
            row = session.get(CommandQueueRecord, command.id)
            if row is None:
                return None
            session.delete(row)
            session.flush()
            deferred = CommandQueueRecord(
                operation_id=command.operation_id,
                device_id=command.device_id,
                target_name=command.device_name if command.device_id is None else None,
                base_url=command.base_url,
                path=command.path,
                method=command.method,
                payload_json=payload_json,
                description=command.description,
                created_at=command.created_at,
                started_at=command.started_at or started_at,
            )
            session.add(deferred)
            session.flush()
            new_id = deferred.id
        self.log(f"moved retry to tail old_id={command.id} new_id={new_id}")
        return new_id

    def drop_pending_watering_start(self, device_name: str) -> list[str]:
        with self.store.session() as session:
            rows = session.execute(
                select(CommandQueueRecord.id, CommandQueueRecord.operation_id).where(
                    CommandQueueRecord.device_name == device_name,
                    CommandQueueRecord.path == "/watering/start",
                )
            ).all()
            if rows:
                session.execute(delete(CommandQueueRecord).where(CommandQueueRecord.id.in_([row.id for row in rows])))
        operation_ids = [row.operation_id for row in rows]
        if operation_ids:
            self.log(
                f"dropped watering_start device={device_name} count={len(operation_ids)} "
                f"operation_ids={','.join(operation_ids)}"
            )
        return operation_ids

    def drop_device(self, device_name: str) -> list[str]:
        with self.store.session() as session:
            rows = session.execute(
                select(CommandQueueRecord.id, CommandQueueRecord.operation_id).where(
                    CommandQueueRecord.device_name == device_name,
                )
            ).all()
            if rows:
                session.execute(delete(CommandQueueRecord).where(CommandQueueRecord.id.in_([row.id for row in rows])))
        operation_ids = [row.operation_id for row in rows]
        if operation_ids:
            self.log(
                f"dropped device queue device={device_name} count={len(operation_ids)} "
                f"operation_ids={','.join(operation_ids)}"
            )
        return operation_ids

    def mark_started(self, command_id: int) -> None:
        with self.store.session() as session:
            session.execute(
                update(CommandQueueRecord)
                .where(CommandQueueRecord.id == command_id)
                .values(started_at=func.coalesce(CommandQueueRecord.started_at, time.time()))
            )

    def list(self) -> list[QueuedCommand]:
        with self.store.session() as session:
            rows = session.scalars(select(CommandQueueRecord).order_by(CommandQueueRecord.id)).all()
        return [self._row_to_command(row) for row in rows]

    def format_status(self) -> str:
        commands = self.list()
        if not commands:
            return "pending: none"

        head = commands[0]
        lines = [f"pending: {head.description} (age {head.age_seconds()}s, queued total {len(commands)})"]
        for index, command in enumerate(commands[1:], start=2):
            lines.append(f"{index}. {command.description}")
        return "\n".join(lines)
