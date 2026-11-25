import hashlib
import hmac
import ipaddress
import os

import geoip2.database
import geoip2.errors
import h3
import structlog

from central.models import (
    ClientGeo,
    GeoSource,
    NetClass,
    QuantizedClientContext,
    ReferenceData,
)

logger = structlog.get_logger("central.geo")

# Фолбэк-координаты (центр Куала-Лумпура), если у клиента нет GPS и IP отсутствует в GeoLite2-City
DEFAULT_MY_LAT = 3.1390
DEFAULT_MY_LON = 101.6869

_VPN_DC_KEYWORDS = (
    "vpn",
    "proxy",
    "private relay",
    "cloudflare",
    "amazon",
    "aws",
    "google cloud",
    "microsoft",
    "azure",
    "digitalocean",
    "hetzner",
    "ovh",
    "vultr",
    "linode",
    "akamai",
    "m247",
    "datacamp",
    "hosting",
    "datacenter",
    "data center",
    "server",
)


def compute_session_hash(raw_ip: str, daily_salt: bytes) -> bytes:
    """
    PDPA Privacy-by-Design: необратимый суточный хэш (16B = 128 bit) из IP и соли MYT.
    Raw IP никогда не сохраняется в БД и не попадает в логи.
    """
    return hmac.new(daily_salt, raw_ip.encode("utf-8"), hashlib.sha256).digest()[:16]


def subnet_key(raw_ip: str) -> str:
    """Агрегирует IP в подсеть /24 (IPv4) или /64 (IPv6) для CGNAT-safe Rate Limiting."""
    try:
        addr = ipaddress.ip_address(raw_ip.strip())
    except ValueError:
        return "invalid"
    if isinstance(addr, ipaddress.IPv4Address):
        net = ipaddress.ip_network(f"{addr}/24", strict=False)
        return f"v4:{net.network_address}/24"
    net6 = ipaddress.ip_network(f"{addr}/64", strict=False)
    return f"v6:{net6.network_address}/64"


class GeoIPService:
    """Локальный резолвер MaxMind GeoLite2 (MODE_MMAP в RAM) + H3-квантователь."""

    def __init__(self, asn_db_path: str, city_db_path: str) -> None:
        self._asn_db_path = asn_db_path
        self._city_db_path = city_db_path
        self._asn_reader: geoip2.database.Reader | None = None
        self._city_reader: geoip2.database.Reader | None = None

    def load_databases(self) -> None:
        new_asn = self._open_reader(self._asn_db_path, "GeoLite2-ASN")
        new_city = self._open_reader(self._city_db_path, "GeoLite2-City")

        old_asn, old_city = self._asn_reader, self._city_reader
        self._asn_reader = new_asn
        self._city_reader = new_city

        if old_asn is not None:
            old_asn.close()
        if old_city is not None:
            old_city.close()

    def close(self) -> None:
        if self._asn_reader is not None:
            self._asn_reader.close()
            self._asn_reader = None
        if self._city_reader is not None:
            self._city_reader.close()
            self._city_reader = None

    @staticmethod
    def _open_reader(path: str, label: str) -> geoip2.database.Reader | None:
        if not path or not os.path.exists(path):
            logger.warning("geoip_database_missing", db=label, path=path)
            return None
        try:
            reader = geoip2.database.Reader(path, mode=geoip2.database.MODE_MMAP)
            logger.info("geoip_database_loaded", db=label)
            return reader
        except Exception:
            logger.exception("geoip_database_load_failed", db=label, path=path)
            return None

    def _lookup_asn(self, raw_ip: str) -> tuple[int, str]:
        if self._asn_reader is None:
            return 0, "Unknown"
        try:
            resp = self._asn_reader.asn(raw_ip)
            asn = int(resp.autonomous_system_number or 0)
            org = str(resp.autonomous_system_organization or "Unknown")
            return asn, org
        except (geoip2.errors.AddressNotFoundError, ValueError):
            return 0, "Unknown"
        except Exception:
            logger.exception("geoip_asn_lookup_error")
            return 0, "Unknown"

    def _lookup_city_coords(self, raw_ip: str) -> tuple[float, float, str | None]:
        if self._city_reader is None:
            return DEFAULT_MY_LAT, DEFAULT_MY_LON, None
        try:
            resp = self._city_reader.city(raw_ip)
            lat = resp.location.latitude
            lon = resp.location.longitude
            city_name = resp.city.name if resp.city else None
            if lat is not None and lon is not None:
                return float(lat), float(lon), city_name
            return DEFAULT_MY_LAT, DEFAULT_MY_LON, city_name
        except (geoip2.errors.AddressNotFoundError, ValueError):
            return DEFAULT_MY_LAT, DEFAULT_MY_LON, None
        except Exception:
            logger.exception("geoip_city_lookup_error")
            return DEFAULT_MY_LAT, DEFAULT_MY_LON, None

    @staticmethod
    def _classify_network(
        asn: int, org_name: str, ref_data: ReferenceData
    ) -> tuple[int | None, str | None, str, NetClass]:
        mapped = ref_data.asn_map.get(asn)
        if mapped is not None:
            if mapped.carrier_id is not None:
                carrier = ref_data.carriers_by_id.get(mapped.carrier_id)
                if carrier is not None:
                    return carrier.id, carrier.slug, carrier.display_name, mapped.net_class
            return mapped.carrier_id, None, mapped.org_name, mapped.net_class

        org_lower = org_name.lower()
        if any(keyword in org_lower for keyword in _VPN_DC_KEYWORDS):
            return None, None, org_name or "VPN / Datacenter", NetClass.VPN_DATACENTER

        return None, None, org_name or "Unknown ISP", NetClass.UNKNOWN

    def classify_and_quantize(
        self,
        *,
        raw_ip: str,
        client_geo: ClientGeo | None,
        daily_salt: bytes,
        ref_data: ReferenceData,
    ) -> QuantizedClientContext:
        """
        Мгновенно превращает raw_ip и опциональный GPS в PDPA-совместимый контекст:
        ASN/Carrier/NetClass + H3 Res 8/6 + session_hash. Raw IP и GPS не сохраняются.
        """
        asn, org_name = self._lookup_asn(raw_ip)
        carrier_id, carrier_slug, carrier_name, net_class = self._classify_network(
            asn, org_name, ref_data
        )

        geoip_city_name: str | None = None
        if client_geo is not None:
            lat, lon = client_geo.lat, client_geo.lon
            geo_source = GeoSource.BROWSER_GPS
        else:
            lat, lon, geoip_city_name = self._lookup_city_coords(raw_ip)
            geo_source = GeoSource.GEOIP_COARSE

        h3_res8_hex = h3.latlng_to_cell(lat, lon, 8)
        h3_res6_hex = h3.cell_to_parent(h3_res8_hex, 6)
        h3_res8_int = h3.str_to_int(h3_res8_hex)
        h3_res6_int = h3.str_to_int(h3_res6_hex)

        district_name = (
            ref_data.districts_by_h3.get(h3_res6_int)
            or geoip_city_name
            or "Malaysia"
        )
        session_hash = compute_session_hash(raw_ip, daily_salt)

        return QuantizedClientContext(
            asn=asn,
            carrier_id=carrier_id,
            carrier_slug=carrier_slug,
            carrier_name=carrier_name,
            net_class=net_class,
            h3_res8_int=h3_res8_int,
            h3_res6_int=h3_res6_int,
            h3_res8_hex=h3_res8_hex,
            h3_res6_hex=h3_res6_hex,
            district_name=district_name,
            geo_source=geo_source,
            session_hash=session_hash,
        )