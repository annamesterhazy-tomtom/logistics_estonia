"""Streamlit app: extract logistics vehicle restrictions from a DATEX II
XML feed (e.g. the Estonian MNT traffic restriction export) and download
the result as a GeoPackage (EPSG:4326)."""

from __future__ import annotations

import datetime as dt

import streamlit as st

from datex_extract import (
    creation_time_bounds,
    filter_by_creation_time,
    parse_logistics_records,
    write_gpkg,
)

st.set_page_config(page_title="LOGISTICS- Estonia", page_icon=":material/local_shipping:")

st.title("LOGISTICS- Estonia")
st.markdown(
    "Upload a DATEX II `SituationPublication` XML feed. The app keeps only "
    "situation records with a vehicle restriction expressed in **metres** "
    "(height, width, length) or **tons** (gross weight, axle weight), and "
    "converts them into point/line GIS features (CRS: EPSG:4326 / WGS84) "
    "that you can download as a GeoPackage."
)

with st.container(key="file_uploader"):
    st.html(
        "<style>"
        ".st-key-file_uploader [data-testid='stFileUploaderDropzoneInstructions'] small"
        " { display: none; }"
        "</style>"
    )
    uploaded_file = st.file_uploader("DATEX II XML file", type=["xml"], accept_multiple_files=False)

run_clicked = st.button(
    "Run",
    icon=":material/play_arrow:",
    disabled=uploaded_file is None,
    width="content",
)

if run_clicked and uploaded_file is not None:
    xml_bytes = uploaded_file.getvalue()

    with st.status("Processing DATEX II feed...", expanded=True) as status:

        def report(message: str) -> None:
            status.update(label=message)
            st.write(f":material/check_circle: {message}")

        try:
            result = parse_logistics_records(xml_bytes, progress_callback=report)
        except Exception as exc:  # noqa: BLE001 - surface parsing errors to the user
            status.update(label="Failed", state="error")
            st.error(f"Could not process the file: {exc}")
        else:
            status.update(label="Done", state="complete")
            st.session_state["raw_result"] = result

if "raw_result" in st.session_state:
    raw_result = st.session_state["raw_result"]

    st.subheader("Filter")
    filter_mode = st.radio(
        "Time range",
        options=["Full data", "From - until (situation creation time)"],
        horizontal=True,
        label_visibility="collapsed",
    )

    min_time, max_time = creation_time_bounds(raw_result)

    if filter_mode == "Full data" or min_time is None:
        if filter_mode != "Full data" and min_time is None:
            st.caption(":material/info: No creation time found in the data; showing full data.")
        result = raw_result
    else:
        col1, col2 = st.columns(2)
        from_date = col1.date_input(
            "From",
            value=min_time.date(),
            min_value=min_time.date(),
            max_value=max_time.date(),
        )
        until_date = col2.date_input(
            "Until",
            value=max_time.date(),
            min_value=min_time.date(),
            max_value=max_time.date(),
        )
        start = dt.datetime.combine(from_date, dt.time.min)
        end = dt.datetime.combine(until_date, dt.time.max)
        result = filter_by_creation_time(raw_result, start=start, end=end)

    gpkg_bytes = write_gpkg(result)

    st.subheader("Summary")
    col1, col2, col3 = st.columns(3)
    col1.metric("Situation records scanned", result.total_records_scanned)
    col2.metric("Logistics features kept", result.logistics_records_kept)
    col3.metric("Points / lines", f"{len(result.points)} / {len(result.lines)}")

    if result.skipped_no_geometry:
        st.caption(
            f":material/info: {result.skipped_no_geometry} record(s) had a logistics "
            "restriction but no usable geometry and were skipped."
        )

    st.markdown("**Restriction types found**")
    counts_table = {
        "Gross weight (t)": result.restriction_type_counts.get("gross_weight_t", 0),
        "Heaviest axle weight (t)": result.restriction_type_counts.get("axle_weight_t", 0),
        "Vehicle height (m)": result.restriction_type_counts.get("vehicle_height_m", 0),
        "Vehicle width (m)": result.restriction_type_counts.get("vehicle_width_m", 0),
        "Vehicle length (m)": result.restriction_type_counts.get("vehicle_length_m", 0),
    }
    st.table(counts_table)

    if result.logistics_records_kept:
        st.download_button(
            "Download GeoPackage",
            data=gpkg_bytes,
            file_name="logistics_restrictions.gpkg",
            mime="application/geopackage+sqlite3",
            icon=":material/download:",
        )
    else:
        st.warning("No logistics restrictions (height/width/length/weight) match the current filter.")
