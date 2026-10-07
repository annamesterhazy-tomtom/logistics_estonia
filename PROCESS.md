# LOGISTICS - Estonia: DATEX II logistics extractor

## What this is
A local Streamlit app that extracts logistics-relevant vehicle restrictions
from the Estonian MNT DATEX II `SituationPublication` XML feed
(`response_sample.xml`) and exports them as a GeoPackage (EPSG:4326 /
WGS84).

## Source data
`response_sample.xml` is a DATEX II feed from the Estonian Road
Administration (MNT) containing traffic restriction situation records of
several types (RoadOrCarriagewayOrLaneManagement, MaintenanceWorks,
SpeedManagement, PublicEvent, ReroutingManagement, GeneralNetworkManagement,
GeneralObstruction, EnvironmentalObstruction). Only records that carry a
vehicle dimension/weight restriction are kept:
- `grossWeightCharacteristic` → gross vehicle weight (tons)
- `heaviestAxleWeightCharacteristic` → heaviest axle weight (tons)
- `heightCharacteristic` → vehicle height (metres)
- `widthCharacteristic` → vehicle width (metres)
- `lengthCharacteristic` → vehicle length (metres)

## Files
- `datex_extract.py` — pure-Python parsing/extraction module (no Streamlit
  dependency, testable standalone):
  - `parse_logistics_records(xml_bytes, progress_callback=None)` — parses
    the XML, filters to logistics restriction records, builds Point/
    LineString geometries in EPSG:4326, returns an `ExtractionResult`
    (points GeoDataFrame, lines GeoDataFrame, counts).
  - `write_gpkg(result, progress_callback=None)` — writes `points`/`lines`
    layers to an in-memory GeoPackage and returns its bytes.
  - `creation_time_bounds(result)` — min/max `situationRecordCreationTime`
    across kept features.
  - `filter_by_creation_time(result, start, end)` — returns a new
    `ExtractionResult` restricted to a creation-time window.
- `app.py` — Streamlit UI: upload XML → Run (shows step-by-step progress via
  `st.status`: Parsing XML, Scanning situation records, Filtering logistics
  restrictions, Building point/line features, Writing GeoPackage) →
  time-range filter (Full data / From-until based on creation time) →
  summary metrics + restriction-type breakdown → download `.gpkg`.
- `requirements.txt` — streamlit, geopandas, shapely, lxml, pyogrio.

## How to run
```powershell
pip install -r requirements.txt
streamlit run app.py
```

## Verified
- Extraction tested directly against `response_sample.xml`: 1625 situation
  records scanned, 928 kept as logistics restrictions (811 gross weight,
  108 height, 11 width, 4 axle weight, 1 length), all as `LineString`
  features, output GeoPackage confirmed readable with correct CRS
  (EPSG:4326) and attributes.
- `filter_by_creation_time` / `creation_time_bounds` verified directly in a
  standalone script (narrowing the creation-time range correctly reduces
  the kept feature count; full range reproduces the unfiltered count).
- App runs locally (`streamlit run app.py`) and responds on
  `http://localhost:8511`.

## Known issue — NOT yet confirmed working end-to-end
**The date/time-range filtering UI in the running app has not been verified
in the browser.** The underlying functions are unit-tested and correct in
isolation, but:
- The app hit a stale-module `ImportError` for `creation_time_bounds` right
  after this feature was added, because Streamlit does not hot-reload
  locally-imported modules (only the main script) — this required a full
  process restart to pick up changes in `datex_extract.py`.
- After restarting, the import error was resolved and the server responds,
  but the filter widgets (radio choice, date pickers) and their effect on
  the summary/download have **not yet been clicked through in the UI** to
  confirm the full interactive flow works as expected.

**Next step when resuming:** open the app, upload `response_sample.xml`,
click Run, switch to "From - until (situation creation time)", pick a
narrower range, and confirm the summary counts and downloaded `.gpkg`
reflect the filtered subset.
