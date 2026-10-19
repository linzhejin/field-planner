"""Build the packaged Hubei location index from the GeoNames China dump."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

SOURCE_URL = "https://download.geonames.org/export/dump/CN.zip"
DEFAULT_OUTPUT = Path("src/nongshi/data/hubei_gazetteer.json")


def _record(line: str) -> dict[str, str] | None:
    columns = line.rstrip("\n").split("\t")
    if len(columns) < 19 or columns[8] != "CN" or columns[10] != "12":
        return None
    if columns[6] not in {"A", "P"}:
        return None
    return {
        "geoname_id": columns[0],
        "name": columns[1],
        "ascii_name": columns[2],
        "aliases": columns[3].split(",") if columns[3] else [],
        "latitude": columns[4],
        "longitude": columns[5],
        "feature_class": columns[6],
        "feature_code": columns[7],
        "admin1_code": columns[10],
        "admin2_code": columns[11],
        "admin3_code": columns[12],
        "admin4_code": columns[13],
        "population": columns[14],
    }


def _chinese_name(record: dict[str, str], *, prefer: tuple[str, ...] = ()) -> str:
    names = [record["name"], *record["aliases"]]
    for suffix in prefer:
        for name in names:
            if name.endswith(suffix):
                return name
    for name in names:
        if any("\u4e00" <= char <= "\u9fff" for char in name):
            return name
    return record["name"]


def build(source_bytes: bytes, output: Path) -> int:
    with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
        file_name = next(name for name in archive.namelist() if Path(name).name == "CN.txt")
        lines = archive.read(file_name).decode("utf-8").splitlines()
    records = [record for line in lines if (record := _record(line)) is not None]
    admin2_names: dict[str, str] = {}
    admin3_names: dict[str, str] = {}
    for record in records:
        if record["feature_class"] != "A":
            continue
        if record["feature_code"] == "ADM2" and record["admin2_code"]:
            admin2_names[record["admin2_code"]] = _chinese_name(
                record, prefer=("市", "自治州", "地区", "盟")
            )
        if record["feature_code"] == "ADM3" and record["admin3_code"]:
            admin3_names[record["admin3_code"]] = _chinese_name(
                record, prefer=("区", "县", "市", "旗")
            )
    locations = []
    for record in records:
        name = _chinese_name(
            record,
            prefer=("区", "县", "市", "旗") if record["feature_code"] == "ADM3" else (),
        )
        resolution = {
            "ADM4": "township",
            "ADM3": "county",
            "ADM2": "city",
            "PPLA": "city",
            "PPLA1": "city",
            "PPLA2": "county",
            "PPLA3": "township",
            "PPLA4": "township",
        }.get(record["feature_code"], "unknown")
        aliases = list(
            dict.fromkeys(
                value
                for value in [name, record["name"], record["ascii_name"], *record["aliases"]]
                if value
            )
        )
        locations.append(
            {
                "geoname_id": record["geoname_id"],
                "name": name,
                "aliases": aliases,
                "latitude": float(record["latitude"]),
                "longitude": float(record["longitude"]),
                "feature_code": record["feature_code"],
                "admin1": "湖北省",
                "admin2": admin2_names.get(record["admin2_code"]),
                "admin3": (
                    None
                    if record["feature_code"] == "ADM3"
                    and record["admin3_code"] == record["geoname_id"]
                    else admin3_names.get(record["admin3_code"])
                ),
                "admin4": record["admin4_code"] or None,
                "admin2_code": record["admin2_code"],
                "admin3_code": record["admin3_code"],
                "admin4_code": record["admin4_code"],
                "population": int(record["population"] or 0),
                "resolution": resolution,
                "display_name": "".join(
                    [
                        "湖北省",
                        ""
                        if record["feature_code"] == "ADM2"
                        else admin2_names.get(record["admin2_code"], ""),
                        ""
                        if record["feature_code"] == "ADM3"
                        else admin3_names.get(record["admin3_code"], ""),
                        name,
                    ]
                ),
            }
        )
    payload = {
        "source": "GeoNames.org",
        "source_url": SOURCE_URL,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
        "province_code": "12",
        "location_count": len(locations),
        "locations": locations,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return len(locations)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, help="Use a downloaded GeoNames CN.zip")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    source = (
        args.source.read_bytes()
        if args.source
        else urllib.request.urlopen(SOURCE_URL, timeout=30).read()
    )
    print(f"Built {build(source, args.output)} Hubei locations at {args.output}")


if __name__ == "__main__":
    main()
