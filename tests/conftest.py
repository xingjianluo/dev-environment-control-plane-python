"""Shared fixtures: temp state file, controllable clock, capture log handler."""
from __future__ import annotations

import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from controlplane.logging_util import configure_logging
from controlplane.manager import ControlPlaneManager
from controlplane.service import create_app
from controlplane.store import Store


class FakeClock:
    """Thread-safe monotonic-ish clock advanced explicitly by tests."""

    def __init__(self, start: float = 1_000_000.0):
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def set(self, value: float) -> None:
        with self._lock:
            self._now = value


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            pass


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state_path(tmp_path) -> str:
    return str(tmp_path / "state.json")


@pytest.fixture
def store(state_path) -> Store:
    return Store(state_path)


@pytest.fixture
def log_capture() -> tuple[logging.Logger, ListHandler]:
    logger = configure_logging()
    handler = ListHandler()
    logger.addHandler(handler)
    yield logger, handler
    logger.removeHandler(handler)


@pytest.fixture
def manager(store, clock, log_capture) -> ControlPlaneManager:
    logger, _handler = log_capture
    return ControlPlaneManager(
        store, global_limit=2, per_owner_limit=1, clock=clock, logger=logger
    )


@pytest.fixture
def client(state_path, clock) -> TestClient:
    app = create_app(
        state_path=state_path,
        global_limit=2,
        per_owner_limit=1,
        clock=clock,
    )
    with TestClient(app) as c:
        yield c
