"""Location matching combines a local Hubei gazetteer with online results."""

from __future__ import annotations

import httpx

from nongshi.location_search import _gazetteer, normalize_online_query
from nongshi.weather import OpenMeteoService


def empty_geocoder(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"results": []})


def test_full_hongshan_address_matches_local_admin_area_offline() -> None:
    service = OpenMeteoService(transport=httpx.MockTransport(empty_geocoder))

    results = service.search_locations("湖北省武汉市洪山区")

    hongshan = next(result for result in results if result.id == "geonames:6642321")
    assert hongshan.label == "湖北省 · 武汉市 · 洪山区"
    assert hongshan.match_source == "local_gazetteer"
    assert hongshan.query == "湖北省武汉市洪山区"
    assert hongshan.resolution == "county"


def test_city_alias_matches_local_name_when_online_search_returns_nothing() -> None:
    service = OpenMeteoService(transport=httpx.MockTransport(empty_geocoder))

    results = service.search_locations("钟祥市")

    zhongxiang = next(result for result in results if result.id == "geonames:1784277")
    assert zhongxiang.label == "湖北省 · 荆门市 · 钟祥市"
    assert zhongxiang.match_source == "local_gazetteer"
    assert zhongxiang.resolution == "county"


def test_local_hubei_match_is_ranked_before_unrelated_online_prefix_matches() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 23,
                        "name": "洪山镇",
                        "latitude": 26.0,
                        "longitude": 119.0,
                        "country": "中国",
                        "country_code": "CN",
                        "admin1": "福建省",
                    }
                ]
            },
        )

    service = OpenMeteoService(transport=httpx.MockTransport(respond))

    results = service.search_locations("湖北省武汉市洪山区")

    assert results[0].id == "geonames:6642321"
    assert results[0].admin2 == "武汉市"
    assert all(result.admin1 != "福建省" for result in results)


def test_location_selection_preserves_user_query_and_uses_matched_resolution() -> None:
    service = OpenMeteoService(transport=httpx.MockTransport(empty_geocoder))
    result = next(
        item for item in service.search_locations("钟祥") if item.id == "geonames:1784277"
    )

    selected = service.select_location([result], result.id)

    assert selected.query == "钟祥"
    assert selected.display_name == "湖北省 · 荆门市 · 钟祥市"
    assert selected.resolution == "county"


def test_unavailable_geocoder_still_returns_local_candidate() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    service = OpenMeteoService(transport=httpx.MockTransport(fail))

    results = service.search_locations("钟祥")

    assert any(result.id == "geonames:1784277" for result in results)


def test_full_address_is_reduced_to_place_and_parent_for_online_query() -> None:
    assert normalize_online_query("湖北省武汉市洪山镇") == "洪山, 武汉, China"


def test_county_query_point_is_labeled_when_locality_search_falls_back_to_its_parent() -> None:
    _gazetteer()
    service = OpenMeteoService(transport=httpx.MockTransport(empty_geocoder))

    results = service.search_locations("老河口村")

    assert any(
        result.resolution == "county"
        and "县/区行政区查询点" in result.match_reason
        and result.admin1 == "湖北省"
        and result.id == "geonames:1804385"
        for result in results
    )
