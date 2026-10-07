"""Extract logistics vehicle restrictions from a DATEX II XML feed.

Parses a DATEX II (SituationPublication) feed such as the Estonian MNT
traffic restriction export and keeps only the situation records that carry
a vehicle-dimension restriction expressed in metres (height / width /
length) or tons (gross weight / heaviest axle weight). Each kept record is
turned into a point or line GIS feature in EPSG:4326.

This module has no dependency on Streamlit so it can be tested and reused
standalone.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import geopandas as gpd
import pandas as pd
from lxml import etree
from shapely.geometry import LineString, Point

NS = {
    "d2": "http://datex2.eu/schema/2/2_0",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

WGS84 = "EPSG:4326"

RESTRICTION_COLUMNS = (
    "gross_weight_t",
    "axle_weight_t",
    "vehicle_height_m",
    "vehicle_width_m",
    "vehicle_length_m",
)

ProgressCallback = Optional[Callable[[str], None]]


def _report(callback: ProgressCallback, message: str) -> None:
    if callback is not None:
        callback(message)


@dataclass
class ExtractionResult:
    """Result of running the extraction over a DATEX II feed."""

    points: gpd.GeoDataFrame
    lines: gpd.GeoDataFrame
    total_records_scanned: int = 0
    logistics_records_kept: int = 0
    skipped_no_geometry: int = 0
    restriction_type_counts: dict = field(default_factory=dict)


def _text(element: etree._Element, xpath: str) -> Optional[str]:
    node = element.find(xpath, NS)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _float(element: etree._Element, xpath: str) -> Optional[float]:
    value = _text(element, xpath)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _localized_values(element: Optional[etree._Element]) -> dict:
    """Collect {lang: text} for a DATEX II `values/value[@lang]` block."""
    result: dict = {}
    if element is None:
        return result
    for value in element.findall("d2:values/d2:value", NS):
        lang = value.get("lang", "")
        if value.text:
            result[lang] = value.text.strip()
    return result


def _parse_linear_geometry(group: etree._Element) -> Optional[LineString]:
    coord_text = _text(
        group,
        "d2:linearExtension/d2:linearLineStringExtension/d2:gmlLineString/d2:coordinates",
    )
    if not coord_text:
        return None
    points = []
    for pair in coord_text.split():
        try:
            lon_str, lat_str = pair.split(",")
            points.append((float(lon_str), float(lat_str)))
        except ValueError:
            continue
    if len(points) < 2:
        return None
    return LineString(points)


def _parse_point_geometry(group: etree._Element) -> Optional[Point]:
    coords = group.find("d2:pointByCoordinates/d2:pointCoordinates", NS)
    if coords is None:
        return None
    lat = _float(coords, "d2:latitude")
    lon = _float(coords, "d2:longitude")
    if lat is None or lon is None:
        return None
    return Point(lon, lat)


def _extract_geometry(record: etree._Element):
    group = record.find("d2:groupOfLocations", NS)
    if group is None:
        return None, None
    group_type = group.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
    if group_type == "Linear":
        return "line", _parse_linear_geometry(group)
    if group_type == "Point":
        return "point", _parse_point_geometry(group)
    # Fall back: try both shapes regardless of the declared type.
    linear = _parse_linear_geometry(group)
    if linear is not None:
        return "line", linear
    point = _parse_point_geometry(group)
    if point is not None:
        return "point", point
    return None, None


def _extract_vehicle_restrictions(record: etree._Element) -> dict:
    """Collect vehicle dimension/weight restrictions across all
    `forVehiclesWithCharacteristicsOf` blocks on a record."""
    restrictions: dict = {}
    vehicle_types: list[str] = []

    characteristics = [
        ("grossWeightCharacteristic", "grossVehicleWeight", "gross_weight_t", "gross_weight_op"),
        (
            "heaviestAxleWeightCharacteristic",
            "heaviestAxleWeight",
            "axle_weight_t",
            "axle_weight_op",
        ),
        ("heightCharacteristic", "vehicleHeight", "vehicle_height_m", "vehicle_height_op"),
        ("widthCharacteristic", "vehicleWidth", "vehicle_width_m", "vehicle_width_op"),
        ("lengthCharacteristic", "vehicleLength", "vehicle_length_m", "vehicle_length_op"),
    ]

    for block in record.findall("d2:forVehiclesWithCharacteristicsOf", NS):
        vehicle_type = _text(block, "d2:vehicleType")
        if vehicle_type and vehicle_type not in vehicle_types:
            vehicle_types.append(vehicle_type)

        for tag, value_tag, value_col, op_col in characteristics:
            char = block.find(f"d2:{tag}", NS)
            if char is None:
                continue
            value = _float(char, f"d2:{value_tag}")
            if value is None:
                continue
            if value_col not in restrictions:
                restrictions[value_col] = value
                restrictions[op_col] = _text(char, "d2:comparisonOperator")

    restrictions["vehicle_types"] = ", ".join(vehicle_types) if vehicle_types else None
    return restrictions


def _record_type_detail(record: etree._Element, record_type: str) -> Optional[str]:
    """Best-effort type-specific classifier field, e.g.
    `roadOrCarriagewayOrLaneManagementType` for RoadOrCarriagewayOrLaneManagement."""
    # Lowercase first letter of the xsi:type to build the conventional field name.
    if not record_type:
        return None
    candidate = record_type[0].lower() + record_type[1:] + "Type"
    return _text(record, f"d2:{candidate}")


def _parse_record(
    situation: etree._Element,
    record: etree._Element,
    feed_type: Optional[str],
    publication_time: Optional[str],
) -> Optional[dict]:
    restrictions = _extract_vehicle_restrictions(record)
    has_restriction = any(
        restrictions.get(col) is not None
        for col in (
            "gross_weight_t",
            "axle_weight_t",
            "vehicle_height_m",
            "vehicle_width_m",
            "vehicle_length_m",
        )
    )
    if not has_restriction:
        return None

    kind, geometry = _extract_geometry(record)
    if geometry is None:
        return None

    record_type = record.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")

    validity = record.find("d2:validity", NS)
    valid_from = _text(validity, "d2:validityTimeSpecification/d2:overallStartTime") if validity is not None else None
    valid_to = _text(validity, "d2:validityTimeSpecification/d2:overallEndTime") if validity is not None else None
    validity_status = _text(validity, "d2:validityStatus") if validity is not None else None

    source = record.find("d2:source", NS)
    source_country = _text(source, "d2:sourceCountry") if source is not None else None
    source_id = _text(source, "d2:sourceIdentification") if source is not None else None
    source_name_values = _localized_values(source.find("d2:sourceName", NS)) if source is not None else {}

    comment_values = _localized_values(record.find("d2:generalPublicComment/d2:comment", NS))

    linear_element = record.find(
        "d2:groupOfLocations/d2:linearWithinLinearElement/d2:linearElement", NS
    )
    road_name_values = _localized_values(linear_element.find("d2:roadName", NS)) if linear_element is not None else {}
    road_number = _text(linear_element, "d2:roadNumber") if linear_element is not None else None

    header = situation.find("d2:headerInformation", NS)

    row = {
        "situation_id": situation.get("id"),
        "situation_version": situation.get("version"),
        "record_id": record.get("id"),
        "record_version": record.get("version"),
        "record_type": record_type,
        "record_subtype": _record_type_detail(record, record_type),
        "feed_type": feed_type,
        "publication_time": publication_time,
        "creation_time": _text(record, "d2:situationRecordCreationTime"),
        "version_time": _text(record, "d2:situationRecordVersionTime"),
        "probability": _text(record, "d2:probabilityOfOccurrence"),
        "severity": _text(record, "d2:severity"),
        "confidentiality": _text(header, "d2:confidentiality") if header is not None else None,
        "information_status": _text(header, "d2:informationStatus") if header is not None else None,
        "urgency": _text(header, "d2:urgency") if header is not None else None,
        "validity_status": validity_status,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "source_country": source_country,
        "source_id": source_id,
        "source_name": source_name_values.get("en") or next(iter(source_name_values.values()), None),
        "comment_et": comment_values.get("et"),
        "comment_en": comment_values.get("en"),
        "road_name": road_name_values.get("et") or next(iter(road_name_values.values()), None),
        "road_number": road_number,
        "compliance_option": _text(record, "d2:complianceOption"),
        "alertc_event_code": _text(
            record,
            "d2:roadOrCarriagewayOrLaneManagementExtension/d2:alertCEventCode",
        ),
        **restrictions,
        "geometry": geometry,
    }
    return {"kind": kind, "row": row}


def parse_logistics_records(
    xml_source, progress_callback: ProgressCallback = None
) -> ExtractionResult:
    """Parse a DATEX II XML feed and return point/line GeoDataFrames of the
    situation records that carry a logistics vehicle restriction.

    Parameters
    ----------
    xml_source:
        A path, file-like object, or raw bytes containing the DATEX II XML.
    progress_callback:
        Optional callable invoked with a short human-readable message as
        each major processing step starts, e.g. for surfacing progress in a
        UI.
    """
    _report(progress_callback, "Parsing XML")
    if isinstance(xml_source, (bytes, bytearray)):
        root = etree.fromstring(xml_source)
    else:
        root = etree.parse(xml_source).getroot()

    payload = root.find("d2:payloadPublication", NS)
    feed_type = _text(payload, "d2:feedType") if payload is not None else None
    publication_time = _text(payload, "d2:publicationTime") if payload is not None else None

    _report(progress_callback, "Scanning situation records")
    situations = root.findall("d2:payloadPublication/d2:situation", NS)

    _report(progress_callback, "Filtering logistics restrictions")
    point_rows: list[dict] = []
    line_rows: list[dict] = []
    total_records = 0
    skipped_no_geometry = 0
    restriction_type_counts = {
        "gross_weight_t": 0,
        "axle_weight_t": 0,
        "vehicle_height_m": 0,
        "vehicle_width_m": 0,
        "vehicle_length_m": 0,
    }

    for situation in situations:
        for record in situation.findall("d2:situationRecord", NS):
            total_records += 1
            parsed = _parse_record(situation, record, feed_type, publication_time)
            if parsed is None:
                restrictions = _extract_vehicle_restrictions(record)
                has_restriction = any(
                    restrictions.get(col) is not None for col in restriction_type_counts
                )
                if has_restriction:
                    skipped_no_geometry += 1
                continue
            for col in restriction_type_counts:
                if parsed["row"].get(col) is not None:
                    restriction_type_counts[col] += 1
            if parsed["kind"] == "point":
                point_rows.append(parsed["row"])
            else:
                line_rows.append(parsed["row"])

    _report(progress_callback, "Building point features")
    points = gpd.GeoDataFrame(point_rows, geometry="geometry", crs=WGS84) if point_rows else gpd.GeoDataFrame(
        geometry=gpd.GeoSeries([], crs=WGS84)
    )

    _report(progress_callback, "Building line features")
    lines = gpd.GeoDataFrame(line_rows, geometry="geometry", crs=WGS84) if line_rows else gpd.GeoDataFrame(
        geometry=gpd.GeoSeries([], crs=WGS84)
    )

    return ExtractionResult(
        points=points,
        lines=lines,
        total_records_scanned=total_records,
        logistics_records_kept=len(point_rows) + len(line_rows),
        skipped_no_geometry=skipped_no_geometry,
        restriction_type_counts=restriction_type_counts,
    )


def write_gpkg(result: ExtractionResult, progress_callback: ProgressCallback = None) -> bytes:
    """Write the extraction result to an in-memory GeoPackage and return
    its raw bytes. Layers without features are skipped."""
    _report(progress_callback, "Writing GeoPackage")
    with tempfile.TemporaryDirectory() as tmp_dir:
        gpkg_path = Path(tmp_dir) / "logistics_restrictions.gpkg"
        wrote_any = False
        if len(result.points):
            result.points.to_file(gpkg_path, layer="points", driver="GPKG")
            wrote_any = True
        if len(result.lines):
            result.lines.to_file(
                gpkg_path,
                layer="lines",
                driver="GPKG",
                mode="a" if wrote_any else "w",
            )
            wrote_any = True
        if not wrote_any:
            # Still produce an (empty) valid GeoPackage so the download works.
            result.points.to_file(gpkg_path, layer="points", driver="GPKG")
        return gpkg_path.read_bytes()


def creation_time_bounds(result: ExtractionResult):
    """Return the (min, max) `creation_time` across all kept features as
    timezone-aware pandas Timestamps, or (None, None) if unavailable."""
    series = []
    for gdf in (result.points, result.lines):
        if len(gdf) and "creation_time" in gdf.columns:
            series.append(pd.to_datetime(gdf["creation_time"], utc=True, errors="coerce"))
    if not series:
        return None, None
    combined = pd.concat(series).dropna()
    if combined.empty:
        return None, None
    return combined.min(), combined.max()


def filter_by_creation_time(
    result: ExtractionResult,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> ExtractionResult:
    """Return a new ExtractionResult keeping only features whose
    `situationRecordCreationTime` falls within [start, end] (inclusive).
    `start`/`end` of None leaves that side of the range unbounded. Naive
    datetimes are assumed to be UTC; tz-aware datetimes are converted."""

    def _to_utc_timestamp(value):
        if value is None:
            return None
        ts = pd.Timestamp(value)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    start_ts = _to_utc_timestamp(start)
    end_ts = _to_utc_timestamp(end)

    def _filter(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if len(gdf) == 0 or "creation_time" not in gdf.columns:
            return gdf
        times = pd.to_datetime(gdf["creation_time"], utc=True, errors="coerce")
        mask = pd.Series(True, index=gdf.index)
        if start_ts is not None:
            mask &= times >= start_ts
        if end_ts is not None:
            mask &= times <= end_ts
        return gdf[mask].reset_index(drop=True)

    points = _filter(result.points)
    lines = _filter(result.lines)

    restriction_type_counts = {}
    for col in RESTRICTION_COLUMNS:
        count = 0
        if col in points.columns:
            count += int(points[col].notna().sum())
        if col in lines.columns:
            count += int(lines[col].notna().sum())
        restriction_type_counts[col] = count

    return ExtractionResult(
        points=points,
        lines=lines,
        total_records_scanned=result.total_records_scanned,
        logistics_records_kept=len(points) + len(lines),
        skipped_no_geometry=result.skipped_no_geometry,
        restriction_type_counts=restriction_type_counts,
    )
