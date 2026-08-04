import os
import socket
import urllib.parse

from smart_watering.domain import (
    DEFAULT_NODE_PORT,
    NODE_URL_ENV,
    OP_ACCEPTED,
    OP_ERROR,
    OP_QUEUED,
    OP_RUNNING,
    OP_SUCCESS,
    OP_TIMEOUT,
)


def normalize_operation_status(status: str) -> str:
    status = status.strip().lower()
    if status in {OP_QUEUED, "created"}:
        return OP_QUEUED
    if status in {OP_ACCEPTED, "accepted", "received", "send", "sent"}:
        return OP_ACCEPTED
    if status in {OP_RUNNING, "started", "start"}:
        return OP_RUNNING
    if status in {OP_SUCCESS, "completed", "complete", "ok"}:
        return OP_SUCCESS
    if status in {OP_TIMEOUT, "timeout"}:
        return OP_TIMEOUT
    return OP_ERROR


def detect_node_url(port: int = DEFAULT_NODE_PORT) -> str:
    env_value = os.environ.get(NODE_URL_ENV)
    if env_value:
        return env_value.rstrip("/")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
    except OSError:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def is_loopback_node_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
