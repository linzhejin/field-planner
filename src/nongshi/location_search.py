"""Offline-first location matching for Hubei farm weather lookups."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files
from typing import Any

from nongshi.schema_v3 import GeocodingResultV3


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum() or "\u4e00" <= char <= "\u9fff")


@lru_cache(maxsize=1)
def _gazetteer() -> tuple[dict[str, Any], ...]:
    data_path = files("nongshi").joinpath("data/hubei_gazetteer.json")
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    if payload.get("province_code") != "12" or not isinstance(payload.get("locations"), list):
        raise ValueError("湖北地名索引格式无效")
    return tuple(payload["locations"])


def search_hubei_locations(query: str, *, count: int = 10) -> list[tuple[int, GeocodingResultV3]]:
    """Return ranked local candidates and their confidence rank."""
    normalized = _normalize(query)
    if len(normalized) < 2:
        return []
    context = _query_components(query)
    scored: list[tuple[int, GeocodingResultV3]] = []
    gazetteer = _gazetteer()
    by_id = {str(item["geoname_id"]): item for item in gazetteer}
    for item in gazetteer:
        aliases = [str(value) for value in item.get("aliases", [])]
        names = [_normalize(value) for value in aliases if value]
        display = _normalize(str(item.get("display_name", "")))
        if context and any(_normalize(token) not in display for token in context):
            continue
        score = 0
        if normalized == display or normalized in names:
            score = 1200
        elif display and normalized in display:
            score = 1100
        elif any(name and normalized in name for name in names):
            score = 700 + min(len(normalized), 100)
        elif any(name and name in normalized for name in names):
            score = 650 + min(max(map(len, names), default=0), 100)
        if score == 0:
            continue
        scored.append(
            (
                score,
                GeocodingResultV3(
                    id=f"geonames:{item['geoname_id']}",
                    name=str(item["name"]),
                    latitude=float(item["latitude"]),
                    longitude=float(item["longitude"]),
                    timezone="Asia/Shanghai",
                    country="中国",
                    country_code="CN",
                    admin1=str(item.get("admin1") or "湖北省"),
                    admin2=item.get("admin2"),
                    admin3=item.get("admin3"),
                    admin4=item.get("admin4"),
                    feature_code=str(item.get("feature_code", "")),
                    population=(int(item["population"]) if item.get("population") else None),
                    query=query,
                    match_source="local_gazetteer",
                    resolution=str(item.get("resolution", "unknown")),
                    match_reason=(
                        "县/区行政区查询点"
                        if item.get("feature_code") == "ADM3"
                        else "乡镇行政中心"
                        if item.get("feature_code") == "PPLA4"
                        else ""
                    ),
                ),
            )
        )
        parent_id = str(item.get("admin3_code") or "")
        parent = by_id.get(parent_id)
        if item.get("resolution") == "unknown" and parent and parent.get("resolution") == "county":
            parent_result = GeocodingResultV3(
                id=f"geonames:{parent['geoname_id']}",
                name=str(parent["name"]),
                latitude=float(parent["latitude"]),
                longitude=float(parent["longitude"]),
                timezone="Asia/Shanghai",
                country="中国",
                country_code="CN",
                admin1=str(parent.get("admin1") or "湖北省"),
                admin2=parent.get("admin2"),
                feature_code=str(parent.get("feature_code", "")),
                population=int(parent["population"]) if parent.get("population") else None,
                query=query,
                match_source="local_gazetteer",
                resolution="county",
                match_reason="乡镇或村级名称未确认，所属县区查询点",
            )
            scored.append((score - 100, parent_result))
    unique: dict[str, tuple[int, GeocodingResultV3]] = {}
    for score, result in scored:
        previous = unique.get(result.id)
        if previous is None or score > previous[0]:
            unique[result.id] = (score, result)
    ranked = sorted(unique.values(), key=lambda pair: (-pair[0], pair[1].label, pair[1].id))
    return ranked[:count]


def normalize_online_query(query: str) -> str:
    """Turn a long Chinese address into the most specific provider-friendly query."""
    pattern = r"[\u4e00-\u9fff]+?(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)"
    matches = list(re.finditer(pattern, query))
    parts = [match.group() for match in matches]
    if len(parts) < 2:
        return query
    trailing = query[matches[-1].end() :].strip()
    if trailing:
        place = trailing
        context = re.sub(r"(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)$", "", parts[-1])
    else:
        place = re.sub(r"(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)$", "", parts[-1])
        context = re.sub(r"(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)$", "", parts[-2])
    if not place:
        return query
    return f"{place}, {context}, China" if context else f"{place}, China"


def _query_components(query: str) -> list[str]:
    pattern = r"[\u4e00-\u9fff]+?(?:特别行政区|自治区|自治州|地区|省|市|区|县|旗|镇|乡)"
    return [match.group() for match in re.finditer(pattern, query)]
