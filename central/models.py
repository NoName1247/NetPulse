from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NetClass(IntEnum):
    MOBILE_CARRIER = 1
    FIXED_BROADBAND = 2
    VPN_DATACENTER = 3
    UNKNOWN = 4

    def to_api_str(self) -> str:
        return self.name


class GeoSource(IntEnum):
    BROWSER_GPS = 1
    GEOIP_COARSE = 2

    def to_api_str(self) -> str:
        return self.name


class ProbeModeDB(IntEnum):
    FULL_PROBE = 1
    PULSE_ONLY = 2

    @classmethod
    def from_str(cls, mode: str) -> "ProbeModeDB":
        if mode == "FULL_PROBE":
            return cls.FULL_PROBE
        if mode == "PULSE_ONLY":
            return cls.PULSE_ONLY
        raise ValueError(f"Unsupported probe_mode: {mode}")


ProbeModeStr = Literal["FULL_PROBE", "PULSE_ONLY"]
HeatmapWindow = Literal["1h", "24h", "7d"]
HeatmapResolution = Literal[6, 8]
ConfidenceLevel = Literal["LOW", "MED", "HIGH"]
HealthStatus = Literal["HEALTHY", "DEGRADED", "MAJOR_OUTAGE"]
IncidentSeverity = Literal["DEGRADED", "MAJOR_OUTAGE"]


class VerdictCode(str, Enum):
    VPN_ACTIVE = "VPN_ACTIVE"
    INTL_ROUTE_DEGRADED = "INTL_ROUTE_DEGRADED"
    DISTRICT_CARRIER_OUTAGE = "DISTRICT_CARRIER_OUTAGE"
    LOCAL_DEVICE_OR_INDOOR = "LOCAL_DEVICE_OR_INDOOR"
    DEGRADED_LOW_SAMPLE = "DEGRADED_LOW_SAMPLE"
    NETWORK_HEALTHY = "NETWORK_HEALTHY"
    LOCAL_PROBE_UNAVAILABLE = "LOCAL_PROBE_UNAVAILABLE"


# --- Доменные исключения ---


class AppError(Exception):
    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, status_code: int | None = None, error_code: str | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code


class RateLimitError(AppError):
    status_code = 429
    error_code = "RATE_LIMIT_EXCEEDED"

    def __init__(self, retry_after_s: int):
        super().__init__("Too many diagnostic requests. Please retry later.")
        self.retry_after_s = max(1, retry_after_s)


class TurnstileError(AppError):
    status_code = 403
    error_code = "TURNSTILE_FAILED"


class InvalidSessionError(AppError):
    status_code = 403
    error_code = "INVALID_OR_EXPIRED_SESSION"


class TamperedReceiptError(AppError):
    status_code = 403
    error_code = "TAMPERED_RECEIPT"


class BadRequestError(AppError):
    status_code = 400
    error_code = "BAD_REQUEST"


class ServiceUnavailableError(AppError):
    status_code = 503
    error_code = "SERVICE_UNAVAILABLE"


# --- REST API DTO: POST /v1/tests/init ---


class ClientGeo(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class ConnectionHint(BaseModel):
    effective_type: str | None = Field(default=None, max_length=32)


class TestInitRequest(BaseModel):
    client_geo: ClientGeo | None = None
    connection_hint: ConnectionHint | None = None


class DetectedNetwork(BaseModel):
    asn: int
    carrier_slug: str | None
    carrier_name: str
    net_class: str
    exclude_from_heatmap: bool
    h3_res8: str
    h3_res6: str
    district_name: str
    geo_source: str


class ProbePlanItem(BaseModel):
    node_id: int
    region: str
    wss_url: str
    probe_mode: ProbeModeStr
    probe_token: str


class TestInitResponse(BaseModel):
    test_id: str
    expires_at: datetime
    detected_network: DetectedNetwork
    probe_plan: list[ProbePlanItem]


# --- REST API DTO: POST /v1/tests/complete ---


class ReceiptMetrics(BaseModel):
    rtt_p50_ms: float = Field(ge=0.0, le=60000.0)
    rtt_p95_ms: float = Field(ge=0.0, le=60000.0)
    jitter_ms: float = Field(ge=0.0, le=60000.0)
    stall_ratio: float = Field(ge=0.0, le=1.0)
    dl_kbps: int | None = Field(default=None, ge=0, le=10_000_000)
    ul_kbps: int | None = Field(default=None, ge=0, le=10_000_000)


class ReceiptIn(BaseModel):
    node_id: int = Field(ge=1)
    probe_mode: ProbeModeStr
    measured_at: int = Field(ge=0)
    metrics: ReceiptMetrics
    signature_hmac: str = Field(min_length=64, max_length=64)


class FailedNodeIn(BaseModel):
    node_id: int = Field(ge=1)
    error: str = Field(min_length=1, max_length=64)


class TestCompleteRequest(BaseModel):
    test_id: str
    receipts: list[ReceiptIn] = Field(default_factory=list, max_length=8)
    failed_nodes: list[FailedNodeIn] = Field(default_factory=list, max_length=8)

    @field_validator("test_id")
    @classmethod
    def _validate_uuid(cls, value: str) -> str:
        UUID(value)
        return value


class VerdictMetrics(BaseModel):
    local_rtt_p50_ms: float | None
    intl_rtt_p50_ms: float | None
    dl_mbps: float | None
    ul_mbps: float | None
    stall_ratio: float | None


class DistrictComparison(BaseModel):
    district_health_score: int | None
    status: HealthStatus | None
    samples_24h: int


class TestCompleteResponse(BaseModel):
    test_id: str
    carrier: str
    district: str
    verdict_code: VerdictCode
    verdict_text_ru: str
    verdict_text_en: str
    verdict_text_bm: str
    local_probe_unavailable: bool = False
    metrics: VerdictMetrics
    district_comparison: DistrictComparison


# --- REST API DTO: GET /v1/heatmap ---


class HeatmapCell(BaseModel):
    h3: str
    district: str
    lat: float | None = None
    lon: float | None = None
    samples: int
    reports: int | None = None
    confidence: ConfidenceLevel
    avg_stall_ratio: float
    p95_rtt_ms: float
    median_dl_kbps: int | None
    health_score: int
    raw_score: int | None = None
    status: HealthStatus


class HeatmapResponse(BaseModel):
    window: HeatmapWindow
    carrier: str
    resolution: HeatmapResolution
    min_samples: int
    cells: list[HeatmapCell]


# --- REST API DTO: GET /v1/incidents/active ---


class ActiveIncidentItem(BaseModel):
    id: int
    carrier_slug: str
    carrier_name: str
    district_name: str
    h3_res6: str
    severity: IncidentSeverity
    verdict_code: str
    unique_reports: int
    avg_stall_ratio: float
    p95_rtt_ms: float
    started_at: datetime
    last_seen_at: datetime


class ActiveIncidentsResponse(BaseModel):
    active_incidents: list[ActiveIncidentItem]


# --- Внутренние структуры (RAM / Domain / DB) ---


@dataclass(slots=True)
class CarrierRecord:
    id: int
    slug: str
    display_name: str
    carrier_type: int


@dataclass(slots=True)
class AsnRecord:
    asn: int
    carrier_id: int | None
    net_class: NetClass
    org_name: str


@dataclass(slots=True)
class NodeConfigRow:
    id: int
    region_code: str
    endpoint_url: str
    is_local: bool
    is_active: bool


@dataclass(slots=True)
class ReferenceData:
    carriers_by_id: dict[int, CarrierRecord] = field(default_factory=dict)
    carriers_by_slug: dict[str, CarrierRecord] = field(default_factory=dict)
    asn_map: dict[int, AsnRecord] = field(default_factory=dict)
    nodes: list[NodeConfigRow] = field(default_factory=list)
    districts_by_h3: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class QuantizedClientContext:
    asn: int
    carrier_id: int | None
    carrier_slug: str | None
    carrier_name: str
    net_class: NetClass
    h3_res8_int: int
    h3_res6_int: int
    h3_res8_hex: str
    h3_res6_hex: str
    district_name: str
    geo_source: GeoSource
    session_hash: bytes


@dataclass(slots=True)
class TestSession:
    test_id: str
    expires_mono: float
    carrier_id: int | None
    carrier_slug: str | None
    carrier_name: str
    asn: int
    net_class: NetClass
    h3_res8_int: int
    h3_res6_int: int
    h3_res8_hex: str
    h3_res6_hex: str
    district_name: str
    geo_source: GeoSource
    session_hash: bytes
    expected_nodes: dict[int, ProbeModeStr]
    local_node_ids: set[int]
    local_probe_unavailable: bool


@dataclass(slots=True)
class NodeHealthState:
    node_id: int
    region_code: str
    endpoint_url: str
    wss_url: str
    is_local: bool
    is_active: bool
    is_healthy: bool = True
    mode: ProbeModeStr = "FULL_PROBE"
    active_sessions: int = 0
    consecutive_failures: int = 0
    last_clock_drift_s: float = 0.0
    last_checked_mono: float = 0.0


@dataclass(slots=True)
class MeasurementRow:
    test_id: UUID
    node_id: int
    carrier_id: int | None
    asn: int
    net_class: int
    h3_res8: int
    h3_res6: int
    geo_source: int
    probe_mode: int
    rtt_p50_ms: float
    rtt_p95_ms: float
    jitter_ms: float
    stall_ratio: float
    dl_kbps: int | None
    ul_kbps: int | None
    session_hash: bytes


@dataclass(slots=True)
class DistrictStats:
    unique_sessions_recent: int
    avg_stall_ratio: float | None
    p95_rtt_ms: float | None
    median_dl_kbps: float | None
    health_score: int | None
    status: HealthStatus | None
    samples_24h: int


@dataclass(slots=True)
class VerdictResult:
    verdict_code: VerdictCode
    verdict_text_ru: str
    verdict_text_en: str
    verdict_text_bm: str