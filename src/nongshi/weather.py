"""Open-Meteo geocoding and forecast adapter with an auditable six-hour cache."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import httpx

from nongshi.location_search import normalize_online_query, search_hubei_locations
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v3 import (
    GeocodingResultV3,
    WeatherDayV3,
    WeatherLocationV3,
    WeatherSnapshotV3,
)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "rain_sum",
    "showers_sum",
    "wind_speed_10m_max",
)


class WeatherServiceError(RuntimeError):
    def __init__(
        self,
        code: Literal["timeout", "connection", "http", "invalid", "missing_dates"],
        message: str,
    ):
        super().__init__(message)
        self.code = code


class OpenMeteoService:
    def __init__(
        self,
        *,
        store: WorkbenchStoreV3 | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
        cache_hours: float = 6.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if cache_hours <= 0:
            raise ValueError("天气缓存时长必须大于零")
        self.store = store
        self.cache_ttl = timedelta(hours=cache_hours)
        self._now = now or (lambda: datetime.now(UTC))
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenMeteoService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search_locations(self, query: str, *, count: int = 10) -> list[GeocodingResultV3]:
        name = query.strip()
        if len(name) < 2:
            raise ValueError("地点至少输入两个字")
        if not 1 <= count <= 10:
            raise ValueError("地点候选最多显示十条")
        local_results = search_hubei_locations(name, count=count)
        if (
            local_results
            and local_results[0][0] >= 1000
            and any(suffix in name for suffix in ("省", "市", "区", "县", "旗", "镇", "乡"))
        ):
            return [result for _, result in local_results]
        local_results = local_results[: max(1, count - 2)]
        try:
            online_results = []
            for attempt, (provider_query, context_query) in enumerate(
                self._geocoding_queries(name)
            ):
                candidates = self._request_geocoding(provider_query, name)
                online_results = self._filter_by_address_context(context_query, candidates)
                if online_results:
                    if attempt:
                        online_results = [
                            result.model_copy(
                                update={
                                    "match_reason": (
                                        "已放宽行政区查询范围，请核对具体查询点"
                                        if not result.match_reason
                                        else (
                                            f"{result.match_reason}；已放宽查询范围，请核对具体查询点"
                                        )
                                    )
                                }
                            )
                            for result in online_results
                        ]
                    break
        except WeatherServiceError:
            if local_results:
                return [result for _, result in local_results]
            raise
        merged: dict[str, tuple[int, GeocodingResultV3]] = {}
        for score, result in local_results:
            merged[result.id] = (score, result)
        for index, result in enumerate(online_results):
            key = self._location_key(result)
            normalized_query = normalize_online_query(name).split(",", 1)[0].strip()
            score = (
                700 - index if normalized_query and normalized_query in result.name else 400 - index
            )
            previous = merged.get(key)
            if previous is None or previous[0] < score:
                merged[key] = (score, result)
        return [result for _, result in sorted(merged.values(), key=lambda item: -item[0])[:count]]

    def _request_geocoding(
        self, provider_query: str, original_query: str
    ) -> list[GeocodingResultV3]:
        try:
            response = self._client.get(
                GEOCODING_URL,
                params={
                    "name": provider_query,
                    "count": 100,
                    "language": "zh",
                    "format": "json",
                    "countryCode": "CN",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as error:
            raise WeatherServiceError("timeout", "地点查询超时，请稍后重试") from error
        except httpx.HTTPStatusError as error:
            raise WeatherServiceError(
                "http", f"地点服务返回 HTTP {error.response.status_code}"
            ) from error
        except httpx.RequestError as error:
            raise WeatherServiceError("connection", "地点服务暂不可用") from error
        except ValueError as error:
            raise WeatherServiceError("invalid", "地点服务返回的数据无法读取") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise WeatherServiceError("invalid", "地点服务返回的数据格式错误")
        try:
            return [
                GeocodingResultV3(
                    id=str(item["id"]),
                    name=str(item["name"]),
                    latitude=float(item["latitude"]),
                    longitude=float(item["longitude"]),
                    timezone=str(item.get("timezone") or "Asia/Shanghai"),
                    country=str(item.get("country", "")),
                    country_code=str(item.get("country_code", "")),
                    admin1=item.get("admin1"),
                    admin2=item.get("admin2"),
                    admin3=item.get("admin3"),
                    admin4=item.get("admin4"),
                    feature_code=str(item.get("feature_code", "")),
                    population=(
                        int(item["population"]) if item.get("population") is not None else None
                    ),
                    query=original_query,
                    match_source="open_meteo",
                    resolution=self._resolution(str(item.get("feature_code", ""))),
                )
                for item in payload.get("results", [])
            ]
        except (KeyError, TypeError, ValueError) as error:
            raise WeatherServiceError("invalid", "地点服务返回了无效候选") from error

    @staticmethod
    def _geocoding_queries(query: str) -> list[tuple[str, str]]:
        pattern = r"[\u4e00-\u9fff]+?(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)"
        matches = list(re.finditer(pattern, query))
        parts = [match.group() for match in matches]
        trailing = query[matches[-1].end() :].strip() if matches else ""
        queries = [(normalize_online_query(query), query)]

        start_index = len(parts) - 1 if trailing else len(parts) - 2
        for index in range(start_index, 0, -1):
            place = OpenMeteoService._strip_admin_suffix(parts[index])
            parent = OpenMeteoService._strip_admin_suffix(parts[index - 1])
            queries.append((f"{place}, {parent}, China", "".join(parts[index - 1 : index + 1])))
        if len(parts) > 1:
            city = OpenMeteoService._strip_admin_suffix(parts[1])
            queries.append((f"{city}, China", "".join(parts[:2])))

        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for provider_query, context_query in queries:
            if provider_query and provider_query not in seen:
                unique.append((provider_query, context_query))
                seen.add(provider_query)
        return unique

    @staticmethod
    def _strip_admin_suffix(value: str) -> str:
        return re.sub(r"(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)$", "", value)

    @staticmethod
    def _resolution(feature_code: str) -> Literal["township", "county", "city", "unknown"]:
        if feature_code in {"ADM4", "PPLA3", "PPLA4"}:
            return "township"
        if feature_code in {"ADM2", "ADM3"}:
            return "county"
        if feature_code in {"ADM1", "PPLC", "PPLA", "PPLA1"}:
            return "city"
        return "unknown"

    @staticmethod
    def _location_key(result: GeocodingResultV3) -> str:
        if result.match_source == "local_gazetteer":
            return result.id
        return f"{result.latitude:.4f}:{result.longitude:.4f}"

    @staticmethod
    def _filter_by_address_context(
        query: str, results: list[GeocodingResultV3]
    ) -> list[GeocodingResultV3]:
        normalized = re.sub(r"\s+", "", query).casefold()
        tokens = re.findall(
            r"[\u4e00-\u9fff]+?(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)",
            normalized,
        )
        if not tokens:
            return results
        tokens = [OpenMeteoService._strip_admin_suffix(token).casefold() for token in tokens]
        filtered = []
        for result in results:
            areas = [
                OpenMeteoService._strip_admin_suffix(area).casefold()
                for area in (
                    result.admin1,
                    result.admin2,
                    result.admin3,
                    result.admin4,
                    result.name,
                )
                if area
            ]
            if all(any(token == area or token in area for area in areas) for token in tokens):
                filtered.append(result)
        return filtered

    @staticmethod
    def select_location(
        results: list[GeocodingResultV3],
        result_id: str,
        *,
        resolution: Literal["township", "county", "city", "unknown"] = "unknown",
    ) -> WeatherLocationV3:
        matches = [result for result in results if result.id == str(result_id)]
        if len(matches) != 1:
            raise ValueError("请从地点候选中明确选择一个查询点")
        result = matches[0]
        selected_resolution = result.resolution if resolution == "unknown" else resolution
        return WeatherLocationV3(
            query=result.query or result.label,
            display_name=result.label,
            latitude=result.latitude,
            longitude=result.longitude,
            timezone="Asia/Shanghai",
            resolution=selected_resolution,
            geocoding_source=(
                "GeoNames.org 湖北离线地名索引"
                if result.match_source == "local_gazetteer"
                else "Open-Meteo Geocoding API"
            ),
            administrative_area=[
                area for area in (result.admin1, result.admin2, result.admin3) if area
            ],
        )

    def fetch_forecast(
        self,
        field_id: str,
        location: WeatherLocationV3,
        start_date: date,
        end_date: date,
        *,
        force_refresh: bool = False,
    ) -> WeatherSnapshotV3:
        if end_date - start_date != timedelta(days=6):
            raise ValueError("天气查询范围需要覆盖完整七天")
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("天气缓存时间需要包含时区")
        now = now.astimezone(UTC)
        if self.store and not force_refresh:
            cached = self.store.cached_weather(
                field_id,
                latitude=location.latitude,
                longitude=location.longitude,
                start_date=start_date,
                end_date=end_date,
                now=now,
            )
            if cached:
                return cached
        try:
            response = self._client.get(
                FORECAST_URL,
                params={
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "daily": ",".join(DAILY_FIELDS),
                    "temperature_unit": "celsius",
                    "wind_speed_unit": "kmh",
                    "precipitation_unit": "mm",
                    "timezone": "Asia/Shanghai",
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as error:
            raise WeatherServiceError("timeout", "天气查询超时，已保留当前地块资料") from error
        except httpx.HTTPStatusError as error:
            raise WeatherServiceError(
                "http", f"天气服务返回 HTTP {error.response.status_code}"
            ) from error
        except httpx.RequestError as error:
            raise WeatherServiceError(
                "connection", "天气服务暂不可用，已保留当前地块资料"
            ) from error
        except ValueError as error:
            raise WeatherServiceError("invalid", "天气服务返回的数据无法读取") from error
        snapshot = self._parse_snapshot(
            field_id, location, start_date, end_date, now, payload, self.cache_ttl
        )
        if self.store:
            self.store.save_weather_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _parse_snapshot(
        field_id: str,
        location: WeatherLocationV3,
        start_date: date,
        end_date: date,
        now: datetime,
        payload: object,
        cache_ttl: timedelta = timedelta(hours=6),
    ) -> WeatherSnapshotV3:
        if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
            raise WeatherServiceError("invalid", "天气响应中缺少 daily 数据")
        daily = payload["daily"]
        if payload.get("timezone") != "Asia/Shanghai":
            raise WeatherServiceError("invalid", "天气响应时区不是 Asia/Shanghai")
        times = daily.get("time")
        expected_days = [start_date + timedelta(days=index) for index in range(7)]
        if times != [day.isoformat() for day in expected_days]:
            raise WeatherServiceError("missing_dates", "天气预报缺少日期，未用估算值填补")
        for key in DAILY_FIELDS:
            if not isinstance(daily.get(key), list) or len(daily[key]) != len(expected_days):
                raise WeatherServiceError("invalid", f"天气响应中的 {key} 数据不完整")
        try:
            days = [
                WeatherDayV3(
                    day=expected_days[index],
                    weather_code=daily["weather_code"][index],
                    temperature_max=daily["temperature_2m_max"][index],
                    temperature_min=daily["temperature_2m_min"][index],
                    precipitation_sum=daily["precipitation_sum"][index],
                    rain_sum=daily["rain_sum"][index],
                    showers_sum=daily["showers_sum"][index],
                    wind_speed_max=daily["wind_speed_10m_max"][index],
                )
                for index in range(7)
            ]
            return WeatherSnapshotV3(
                field_id=field_id,
                location=location,
                requested_latitude=location.latitude,
                requested_longitude=location.longitude,
                grid_latitude=float(payload["latitude"]),
                grid_longitude=float(payload["longitude"]),
                timezone="Asia/Shanghai",
                fetched_at=now,
                coverage_start=start_date,
                coverage_end=end_date,
                units=dict(payload.get("daily_units", {})),
                days=days,
                cache_expires_at=now + cache_ttl,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WeatherServiceError("invalid", "天气响应字段缺失或数值无效") from error

    def latest_snapshot(
        self,
        field_id: str,
        *,
        location: WeatherLocationV3 | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> WeatherSnapshotV3 | None:
        if self.store is None:
            return None
        snapshot = self.store.latest_weather(field_id)
        if snapshot is None:
            return None
        mismatch = bool(
            location
            and (
                snapshot.requested_latitude != location.latitude
                or snapshot.requested_longitude != location.longitude
            )
        ) or bool(
            start_date
            and end_date
            and (snapshot.coverage_start != start_date or snapshot.coverage_end != end_date)
        )
        stale = snapshot.is_stale or snapshot.cache_expires_at <= self._now() or mismatch
        return snapshot.model_copy(update={"is_stale": stale})
