import math
import time
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from central.models import (
    HeatmapResponse,
    NetClass,
    NodeConfigRow,
    NodeHealthState,
    ProbeModeStr,
    ReferenceData,
    TestInitResponse,
    TestSession,
)

MYT_TZ = ZoneInfo("Asia/Kuala_Lumpur")


def current_myt_date() -> date:
    return datetime.now(MYT_TZ).date()


class SaltCache:
    """5-минутный RAM-кэш суточной соли с автоматической инвалидацией при смене суток в MYT."""

    def __init__(self, ttl_s: int = 300) -> None:
        self._ttl_s = ttl_s
        self._salt_day: date | None = None
        self._salt: bytes | None = None
        self._expires_mono: float = 0.0

    def get(self) -> bytes | None:
        if self._salt is None or self._salt_day is None:
            return None
        if time.monotonic() >= self._expires_mono:
            return None
        if self._salt_day != current_myt_date():
            self.invalidate()
            return None
        return self._salt

    def set(self, salt_day: date, salt: bytes) -> None:
        self._salt_day = salt_day
        self._salt = salt
        self._expires_mono = time.monotonic() + self._ttl_s

    def invalidate(self) -> None:
        self._salt_day = None
        self._salt = None
        self._expires_mono = 0.0


class SessionStore:
    """
    Хранилище активных сессий test_session (TTL 90s) и Idempotency-Key.
    В 1-воркерном Uvicorn pop() выполняется без await -> строго атомарная защита от Replay.
    """

    def __init__(
        self,
        *,
        session_ttl_s: int = 90,
        idempotency_ttl_s: int = 90,
        max_entries: int = 10000,
    ) -> None:
        self._session_ttl_s = session_ttl_s
        self._idempotency_ttl_s = idempotency_ttl_s
        self._max_entries = max_entries
        self._sessions: dict[str, TestSession] = {}
        self._idempotency: dict[str, tuple[float, TestInitResponse]] = {}

    def put(self, session: TestSession) -> None:
        if len(self._sessions) >= self._max_entries:
            self.cleanup_expired()
        self._sessions[session.test_id] = session

    def pop(self, test_id: str) -> TestSession | None:
        session = self._sessions.pop(test_id, None)
        if session is None:
            return None
        if time.monotonic() > session.expires_mono:
            return None
        return session

    def get_idempotent(self, key: str) -> TestInitResponse | None:
        entry = self._idempotency.get(key)
        if entry is None:
            return None
        expires_mono, response = entry
        if time.monotonic() > expires_mono:
            self._idempotency.pop(key, None)
            return None
        return response

    def put_idempotent(self, key: str, response: TestInitResponse) -> None:
        if len(self._idempotency) >= self._max_entries:
            self.cleanup_expired()
        self._idempotency[key] = (time.monotonic() + self._idempotency_ttl_s, response)

    def cleanup_expired(self) -> int:
        now = time.monotonic()
        expired_sessions = [k for k, s in self._sessions.items() if s.expires_mono < now]
        for k in expired_sessions:
            self._sessions.pop(k, None)

        expired_idem = [k for k, (exp, _) in self._idempotency.items() if exp < now]
        for k in expired_idem:
            self._idempotency.pop(k, None)

        return len(expired_sessions) + len(expired_idem)

    @property
    def active_count(self) -> int:
        return len(self._sessions)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refill_mono: float


class TokenBucketLimiter:
    """
    CGNAT-safe In-Memory Token Bucket:
    - Fixed Broadband / VPN / Unknown: 15 тестов / 10 мин на /24 (IPv4) или /64 (IPv6)
    - Mobile Carrier (CGNAT): 120 тестов / 10 мин на /24 (IPv4) или /64 (IPv6)
    """

    def __init__(
        self,
        *,
        window_s: int = 600,
        fixed_max: int = 15,
        mobile_max: int = 120,
        max_buckets: int = 20000,
    ) -> None:
        self._window_s = float(window_s)
        self._fixed_max = float(fixed_max)
        self._mobile_max = float(mobile_max)
        self._max_buckets = max_buckets
        self._buckets: dict[str, _Bucket] = {}

    def _capacity_for(self, net_class: NetClass) -> float:
        if net_class == NetClass.MOBILE_CARRIER:
            return self._mobile_max
        return self._fixed_max

    def check_and_consume(self, bucket_key: str, net_class: NetClass) -> tuple[bool, int]:
        now = time.monotonic()
        if len(self._buckets) >= self._max_buckets:
            self._evict_stale(now)

        capacity = self._capacity_for(net_class)
        refill_rate = capacity / self._window_s
        full_key = f"{int(net_class)}:{bucket_key}"

        bucket = self._buckets.get(full_key)
        if bucket is None:
            self._buckets[full_key] = _Bucket(tokens=capacity - 1.0, last_refill_mono=now)
            return True, 0

        elapsed = max(0.0, now - bucket.last_refill_mono)
        bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_rate)
        bucket.last_refill_mono = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0

        missing = 1.0 - bucket.tokens
        retry_after = max(1, math.ceil(missing / refill_rate))
        return False, retry_after

    def _evict_stale(self, now: float) -> None:
        cutoff = now - self._window_s
        stale_keys = [k for k, b in self._buckets.items() if b.last_refill_mono < cutoff]
        for k in stale_keys:
            self._buckets.pop(k, None)


class HeatmapCache:
    """RAM-кэш 12–24 страновых ключей тепловых карт (TTL 30s)."""

    def __init__(self, ttl_s: int = 30) -> None:
        self._ttl_s = ttl_s
        self._store: dict[tuple[str, str, int], tuple[float, HeatmapResponse]] = {}

    def get(self, key: tuple[str, str, int]) -> HeatmapResponse | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_mono, response = entry
        if time.monotonic() >= expires_mono:
            self._store.pop(key, None)
            return None
        return response

    def set(self, key: tuple[str, str, int], value: HeatmapResponse) -> None:
        self._store[key] = (time.monotonic() + self._ttl_s, value)

    def clear(self) -> None:
        self._store.clear()


def _endpoint_to_wss_url(endpoint_url: str) -> str:
    clean = endpoint_url.rstrip("/")
    if clean.startswith("https://"):
        base = "wss://" + clean[len("https://") :]
    elif clean.startswith("http://"):
        base = "ws://" + clean[len("http://") :]
    else:
        base = clean
    return f"{base}/probe/ws"


class NodeRegistry:
    """Реестр Probe-нод в памяти, обновляемый Pull-Healthcheck каждые 5с."""

    def __init__(self, *, fail_threshold: int = 3) -> None:
        self._fail_threshold = fail_threshold
        self._nodes: dict[int, NodeHealthState] = {}

    def load_nodes(self, rows: list[NodeConfigRow]) -> None:
        existing = self._nodes
        updated: dict[int, NodeHealthState] = {}
        for row in rows:
            prev = existing.get(row.id)
            updated[row.id] = NodeHealthState(
                node_id=row.id,
                region_code=row.region_code,
                endpoint_url=row.endpoint_url.rstrip("/"),
                wss_url=_endpoint_to_wss_url(row.endpoint_url),
                is_local=row.is_local,
                is_active=row.is_active,
                is_healthy=prev.is_healthy if prev else True,
                mode=prev.mode if prev else ("FULL_PROBE" if row.is_local else "PULSE_ONLY"),
                active_sessions=prev.active_sessions if prev else 0,
                consecutive_failures=prev.consecutive_failures if prev else 0,
                last_clock_drift_s=prev.last_clock_drift_s if prev else 0.0,
                last_checked_mono=prev.last_checked_mono if prev else 0.0,
            )
        self._nodes = updated

    def all_nodes(self) -> list[NodeHealthState]:
        return list(self._nodes.values())

    def get_node(self, node_id: int) -> NodeHealthState | None:
        return self._nodes.get(node_id)

    def is_node_healthy(self, node_id: int) -> bool:
        node = self._nodes.get(node_id)
        return bool(node and node.is_active and node.is_healthy)

    def record_success(
        self,
        node_id: int,
        *,
        mode: ProbeModeStr,
        active_sessions: int,
        clock_drift_s: float,
    ) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.is_healthy = True
        node.consecutive_failures = 0
        node.mode = mode
        node.active_sessions = active_sessions
        node.last_clock_drift_s = clock_drift_s
        node.last_checked_mono = time.monotonic()

    def record_failure(self, node_id: int) -> tuple[bool, int]:
        """Возвращает (transitioned_to_down, consecutive_failures)."""
        node = self._nodes.get(node_id)
        if node is None:
            return False, 0
        node.consecutive_failures += 1
        node.last_checked_mono = time.monotonic()
        if node.is_healthy and node.consecutive_failures >= self._fail_threshold:
            node.is_healthy = False
            return True, node.consecutive_failures
        return False, node.consecutive_failures

    def get_healthy_plan_nodes(self) -> tuple[NodeHealthState | None, list[NodeHealthState]]:
        """Возвращает лучшую здоровую локальную ноду (min active_sessions) и список зарубежных."""
        healthy_local = [n for n in self._nodes.values() if n.is_active and n.is_healthy and n.is_local]
        healthy_intl = [n for n in self._nodes.values() if n.is_active and n.is_healthy and not n.is_local]
        healthy_local.sort(key=lambda n: (n.active_sessions, n.node_id))
        healthy_intl.sort(key=lambda n: n.node_id)
        best_local = healthy_local[0] if healthy_local else None
        return best_local, healthy_intl


class FailedNodeTracker:
    """
    Отслеживает failed_nodes от пользователей для детекции обрыва магистрали/пиринга оператора:
    если >= 30 уникальных пользователей одного оператора за 5 минут сообщили об ошибке к живой ноде.
    """

    def __init__(self, *, window_s: int = 300) -> None:
        self._window_s = float(window_s)
        self._events: dict[tuple[int, int], dict[bytes, float]] = {}

    def record_failure(self, carrier_id: int, node_id: int, session_hash: bytes) -> int:
        now = time.monotonic()
        cutoff = now - self._window_s
        key = (carrier_id, node_id)
        bucket = self._events.setdefault(key, {})

        stale = [sh for sh, ts in bucket.items() if ts < cutoff]
        for sh in stale:
            bucket.pop(sh, None)

        bucket[session_hash] = now
        return len(bucket)

    def cleanup_expired(self) -> None:
        now = time.monotonic()
        cutoff = now - self._window_s
        empty_keys: list[tuple[int, int]] = []
        for key, bucket in self._events.items():
            stale = [sh for sh, ts in bucket.items() if ts < cutoff]
            for sh in stale:
                bucket.pop(sh, None)
            if not bucket:
                empty_keys.append(key)
        for key in empty_keys:
            self._events.pop(key, None)


class InMemoryState:
    """Контейнер всего in-memory состояния 1-воркерного Central API (< 15 MB RAM)."""

    def __init__(
        self,
        *,
        session_ttl_s: int = 90,
        idempotency_ttl_s: int = 90,
        salt_cache_ttl_s: int = 300,
        heatmap_cache_ttl_s: int = 30,
        rate_limit_window_s: int = 600,
        rate_limit_fixed_max: int = 15,
        rate_limit_mobile_max: int = 120,
        healthcheck_fail_threshold: int = 3,
        peering_alert_window_s: int = 300,
    ) -> None:
        self.salts = SaltCache(ttl_s=salt_cache_ttl_s)
        self.sessions = SessionStore(
            session_ttl_s=session_ttl_s,
            idempotency_ttl_s=idempotency_ttl_s,
        )
        self.rate_limiter = TokenBucketLimiter(
            window_s=rate_limit_window_s,
            fixed_max=rate_limit_fixed_max,
            mobile_max=rate_limit_mobile_max,
        )
        self.heatmap_cache = HeatmapCache(ttl_s=heatmap_cache_ttl_s)
        self.node_registry = NodeRegistry(fail_threshold=healthcheck_fail_threshold)
        self.failed_nodes = FailedNodeTracker(window_s=peering_alert_window_s)
        self.ref_data = ReferenceData()