"""Unit and integration tests with synthetic fixtures. No fabricated records ship as real data."""
import json
from pathlib import Path

import pandas as pd
import pytest

from reconstruct import (CROP_INFO, audit, build, load_grid, nasa_annual,
                         parse_nasa_payload, parse_soil, paths, require_unique,
                         select_fao_pesticide)


def grid(dest, rows):
    columns = list(rows[0])
    data = [[r[c] for c in columns] for r in rows]
    dest.write_text(json.dumps({"headers": [{"header": c} for c in columns], "data": data}),
                    encoding="utf-8")


def test_pesticide_filter_selects_total_only_and_enforces_unique_year():
    df = pd.DataFrame([
        ("India", "Pesticides (total)", "Use per area of cropland", 1990, "kg/ha", 0.20),
        ("India", "Fungicides", "Use per area of cropland", 1990, "kg/ha", 99),
        ("India", "Pesticides (total)", "Agricultural Use", 1990, "t", 100000),
        ("Pakistan", "Pesticides (total)", "Use per area of cropland", 1990, "kg/ha", 1.5),
    ], columns=["Area", "Item", "Element", "Year", "Unit", "Value"])
    out = select_fao_pesticide(df)
    assert out.to_dict("records") == [{"Year": 1990, "Value": 0.2}]
    with pytest.raises(ValueError, match="duplicate year"):
        select_fao_pesticide(pd.concat([df, df.iloc[[0]]], ignore_index=True))


def test_soil_heuristic_is_weighted_and_unknown_is_missing():
    order, ph = parse_soil("VERTISOLS - 70%; ALFISOLS - 30%")
    assert order == "Vertisols"
    assert ph == pytest.approx(7.32)
    assert parse_soil("UNKNOWN SOIL")== (None, None)


def test_nasa_year_13_and_leap_year_precipitation():
    par = {p: {"200013": v, "200113": v} for p, v in [("T2M", 25), ("RH2M", 55), ("PRECTOTCORR", 2)]}
    parsed = parse_nasa_payload({"properties": {"parameter": par}}, 2000, 2001)
    assert parsed["2000"]["rain_mm"] == 732  # leap year
    assert parsed["2001"]["rain_mm"] == 730
    assert nasa_annual({"200013": -999, **{f"2000{i:02d}": 10 for i in range(1, 13)}}, 2000, "T2M") == 10


def test_duplicate_key_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        require_unique(pd.DataFrame({"DIST": [1, 1], "Year": [2000, 2000]}), ["DIST", "Year"], "fixture")


def make_fixture(base: Path):
    raw, _, _ = paths(base)
    (raw / "districts.json").write_text(json.dumps([
        {"DIST": 101, "DISTNAME": "Fixture North", "STNAME": "Test State", "LAT": 19.1, "LONG": 72.2},
        {"DIST": 102, "DISTNAME": "Fixture South", "STNAME": "Test State", "LAT": 20.1, "LONG": 73.2},
    ]), encoding="utf-8")
    rows = []
    for district in (101, 102):
        for year in (1979, 2000):
            row = {"Dist Code": district, "Year": year}
            for crop in CROP_INFO:
                row[f"{crop} AREA"] = 10 if crop == "RICE" else -1
                row[f"{crop} YIELD"] = 1500 if crop == "RICE" else -1
            rows.append(row)
    grid(raw / "area-production-yield.json", rows)
    grid(raw / "fertilizer-consumption.json", [{
        "Dist Code": d, "Year": y, "NITROGEN PER HA OF GCA": 10,
        "PHOSPHATE PER HA OF GCA": 5, "POTASH PER HA OF GCA": 4,
        "TOTAL PER HA OF GCA": 19
    } for d in (101, 102) for y in (1979, 2000)])
    grid(raw / "bio-monthly-rainfall.json", [{
        "Dist Code": d, "Year": y, "ANNUAL RAINFALL": (700 if y == 1979 else -1)
    } for d in (101, 102) for y in (1979, 2000)])
    grid(raw / "bio-normal-rainfall.json", [{"Dist Code": d, "ANNUAL NORMAL RAINFALL": 900}
                                           for d in (101, 102)])
    grid(raw / "bio-soil-type.json", [{"Dist Code": d, "SOIL TYPE PERCENT": "VERTISOLS - 70%; ALFISOLS - 30%"}
                                    for d in (101, 102)])
    weather = {str(d): {"years": {
        "1981": {"temp": 26, "humidity": 66, "rain_mm": 850},
        "1990": {"temp": 28, "humidity": 60, "rain_mm": 950},
        "2000": {"temp": 29, "humidity": 55, "rain_mm": 1010},
    }} for d in (101, 102)}
    (raw / "weather_cache.json").write_text(json.dumps(weather), encoding="utf-8")
    pd.DataFrame({"Year": [1990, 2000], "Value": [0.2, 0.5]}).to_csv(
        raw / "faostat_pesticide_india.csv", index=False)


def test_reconstruction_preserves_all_yields_and_exposes_proxy_years(tmp_path):
    make_fixture(tmp_path)
    research_path, legacy_path, prov_path = build(tmp_path, start=1979, end=2000)
    research = pd.read_csv(research_path)
    legacy = pd.read_csv(legacy_path)
    provenance = pd.read_csv(prov_path)
    assert len(research) == len(legacy) == len(provenance) == 4
    assert list(legacy.columns) == ["Year", "Region", "Crop", "Season", "Temperature", "Rainfall", "Humidity",
                                    "Soil Type", "pH", "Nitrogen", "Phosphorus", "Potassium", "Fertilizer", "Pesticide", "Yield"]
    assert legacy.isna().sum().sum() == 0
    old = research.loc[research.Year == 1979]
    assert old["Temperature"].isna().all()
    assert old["Pesticide"].isna().all()
    assert set(old["rain_src"]) == {"ICRISAT_gauge"}
    old_prov = provenance.loc[provenance.Year == 1979]
    assert set(old_prov["temp_src"]) == {"NASA_1981_1990_future_climatic_normal"}
    assert set(old_prov["pest_src"]) == {"FAOSTAT_future_first_year_backfill"}
    current = provenance.loc[provenance.Year == 2000]
    assert set(current["rain_src"]) == {"NASA_POWER"}
    assert current["Rainfall"].iloc[0] == 1010
    report_path = audit(tmp_path)
    report = json.loads(report_path.read_text())
    assert report["crop_yield_dataset.csv"]["duplicates_year_region_crop_season"] == 0
    assert report["build_summary"]["yield_observations"] == 4


def test_invalid_apy_omits_target_but_not_temporal_imputation(tmp_path):
    make_fixture(tmp_path)
    raw, _, _ = paths(tmp_path)
    df = load_grid(raw / "area-production-yield.json")
    df.loc[0, "RICE YIELD"] = -1
    grid(raw / "area-production-yield.json", df.to_dict("records"))
    research_path, legacy_path, _ = build(tmp_path, start=1979, end=2000)
    assert len(pd.read_csv(research_path)) == 3
    assert len(pd.read_csv(legacy_path)) == 3


def test_auto_fetch_falls_back_and_records_source(tmp_path, monkeypatch):
    """If ICRISAT API cannot be reached, upstream mirror is explicitly recorded."""
    import reconstruct as r

    def fake_download(_session, url, dest, timeout=180):
        if "data.icrisat.org" in url:
            raise RuntimeError("simulated endpoint outage")
        if url.endswith("/districts.json"):
            dest.write_text(json.dumps([{"DIST": 1, "LAT": 20, "LONG": 75}]))
        else:
            dest.write_text(json.dumps({"headers": [{"header": "Dist Code"}], "data": [[1]]}))

    monkeypatch.setattr(r, "save_response_stream", fake_download)
    r.fetch_icrisat(tmp_path, source="auto")
    manifest = json.loads((tmp_path / "reports" / "source_manifest.json").read_text())
    assert manifest["districts.json"]["url"].startswith(r.UPSTREAM)
    assert len(manifest["districts.json"]["sha256"]) == 64
    assert (tmp_path / "raw" / "bio-soil-type.json").exists()
