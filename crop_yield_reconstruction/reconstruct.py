#!/usr/bin/env python3
"""Reproducible reconstruction of sid09008mane/crop-yield (ICRISAT/NASA/FAO).

This program deliberately does *not* treat historical proxy values as observations.
Run `python reconstruct.py --help` for full CLI documentation.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("reconstruct")
ROOT = Path(__file__).resolve().parent
UPSTREAM = "https://raw.githubusercontent.com/sid09008mane/crop-yield/main"
ICRISAT_BASES = ["https://data.icrisat.org/dldAPI/apportioned", "http://data.icrisat.org/dldAPI/apportioned"]
NASA_URL = "https://power.larc.nasa.gov/api/temporal/monthly/point"
FAO_URL = "https://bulks-faostat.fao.org/production/Inputs_Pesticides_Use_E_All_Data_(Normalized).zip"
GRID_FILES = {
    "area-production-yield": "area-production-yield.json",
    "fertilizer-consumption": "fertilizer-consumption.json",
    "soil-type": "bio-soil-type.json",
    "normal-rainfall": "bio-normal-rainfall.json",
    "monthly-rainfall": "bio-monthly-rainfall.json",
}
CROP_INFO = {
    "RICE": ("Rice", "Kharif"), "WHEAT": ("Wheat", "Rabi"),
    "KHARIF SORGHUM": ("Sorghum (Kharif)", "Kharif"),
    "RABI SORGHUM": ("Sorghum (Rabi)", "Rabi"),
    "PEARL MILLET": ("Pearl Millet", "Kharif"), "MAIZE": ("Maize", "Kharif"),
    "FINGER MILLET": ("Finger Millet", "Kharif"), "BARLEY": ("Barley", "Rabi"),
    "CHICKPEA": ("Chickpea", "Rabi"), "PIGEONPEA": ("Pigeonpea", "Kharif"),
    "MINOR PULSES": ("Minor Pulses", "Whole Year"),
    "GROUNDNUT": ("Groundnut", "Kharif"), "SESAMUM": ("Sesamum", "Kharif"),
    "RAPESEED AND MUSTARD": ("Rapeseed & Mustard", "Rabi"),
    "SAFFLOWER": ("Safflower", "Rabi"), "CASTOR": ("Castor", "Kharif"),
    "LINSEED": ("Linseed", "Rabi"), "SUNFLOWER": ("Sunflower", "Whole Year"),
    "SOYABEAN": ("Soybean", "Kharif"),
    "SUGARCANE": ("Sugarcane", "Whole Year"), "COTTON": ("Cotton", "Kharif"),
}
SCHEMA = ["Year", "Region", "Crop", "Season", "Temperature", "Rainfall", "Humidity",
          "Soil Type", "pH", "Nitrogen", "Phosphorus", "Potassium", "Fertilizer", "Pesticide", "Yield"]
NUMERIC = ["Temperature", "Rainfall", "Humidity", "pH", "Nitrogen", "Phosphorus",
           "Potassium", "Fertilizer", "Pesticide", "Yield"]
# Heuristic soil-order pH proxies from upstream; NOT measurements or universally valid soil pH.
SOIL_RULES = [
    ("VERTISOL", "Vertisols", 7.8), ("VERRTISOL", "Vertisols", 7.8),
    ("USTERT", "Vertisols", 7.8), ("VERTIC", "Vertisols", 7.6),
    ("VRTIC", "Vertisols", 7.6), ("VERTI", "Vertisols", 7.6),
    ("ARIDISOL", "Aridisols", 8.0), ("ORTHID", "Aridisols", 8.0),
    ("MOLLISOL", "Mollisols", 7.0), ("USTOLL", "Mollisols", 7.2),
    ("UDOLL", "Mollisols", 6.8),
    ("ALFISOL", "Alfisols", 6.2), ("USTALF", "Alfisols", 6.2), ("UDALF", "Alfisols", 6.0),
    ("INNCEPTISOL", "Inceptisols", 6.8), ("INCEPTISOL", "Inceptisols", 6.8),
    ("USTOCHREPT", "Inceptisols", 6.8), ("OCHREPT", "Inceptisols", 6.8),
    ("DYSTROPEPT", "Inceptisols", 5.6), ("TROPEPT", "Inceptisols", 6.2),
    ("UDUPT", "Inceptisols", 6.4), ("UDEPT", "Inceptisols", 6.4),
    ("AQUEPT", "Inceptisols", 6.5),
    ("PSSAMNET", "Entisols", 6.5), ("PSSAMENT", "Entisols", 6.5),
    ("PSAMMENT", "Entisols", 6.5), ("FLUVENT", "Entisols", 7.4),
    ("ORTHENT", "Entisols", 7.0), ("ENTISOL", "Entisols", 7.0),
    ("ULTISOL", "Ultisols", 5.2), ("OXISOL", "Oxisols", 5.0),
    ("HISTOSOL", "Histosols", 4.8),
]


def paths(base: Path) -> tuple[Path, Path, Path]:
    raw, processed, reports = (base / "raw", base / "processed", base / "reports")
    for d in (raw, processed, reports):
        d.mkdir(parents=True, exist_ok=True)
    return raw, processed, reports


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, allow_nan=False)
        tmp = Path(f.name)
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def provenance_entry(path: Path, url: str) -> dict:
    return {"url": url, "downloaded_utc": datetime.now(timezone.utc).isoformat(),
            "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def save_manifest(base: Path, entries: dict) -> None:
    path = base / "reports" / "source_manifest.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    old.update(entries)
    write_json_atomic(path, old)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "AgricultureProject-Reconstruction/1.0 (academic research)"})
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(408, 429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def save_response_stream(s: requests.Session, url: str, dest: Path, timeout=180) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with s.get(url, stream=True, timeout=(15, timeout)) as response:
        response.raise_for_status()
        with tempfile.NamedTemporaryFile("wb", dir=dest.parent, delete=False) as f:
            tmp = Path(f.name)
            try:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
    tmp.replace(dest)


def validate_grid_payload(value: Any, source: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("headers"), list) or not isinstance(value.get("data"), list):
        raise ValueError(f"Unexpected ICRISAT grid structure from {source}")
    if not value["data"]:
        raise ValueError(f"ICRISAT grid contains no rows: {source}")


def fetch_icrisat(base: Path, source: str = "auto", force: bool = False) -> None:
    raw, _, _ = paths(base)
    s = session()
    entries = {}
    specs = {"districts": "districts.json", **GRID_FILES}
    for category, filename in specs.items():
        dest = raw / filename
        if dest.exists() and not force:
            LOG.info("Using existing %s", dest)
            continue
        urls = []
        if source in ("api", "auto"):
            urls.extend(f"{baseurl}/{category}" for baseurl in ICRISAT_BASES)
        if source in ("mirror", "auto"):
            urls.append(f"{UPSTREAM}/raw_icrisat/{filename}")
        problems = []
        for url in urls:
            try:
                LOG.info("Fetching %s -> %s", category, url)
                save_response_stream(s, url, dest)
                data = json.loads(dest.read_text(encoding="utf-8"))
                if category == "districts":
                    if not isinstance(data, list) or not data or not {"DIST", "LAT", "LONG"}.issubset(data[0]):
                        raise ValueError("Invalid districts structure")
                else:
                    validate_grid_payload(data, url)
                entries[filename] = provenance_entry(dest, url)
                break
            except Exception as exc:
                LOG.warning("Source unavailable: %s (%s)", url, exc)
                problems.append(f"{url}: {exc}")
                dest.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Could not retrieve {filename}. Attempts:\n" + "\n".join(problems))
    save_manifest(base, entries)


def clean_number(value: Any) -> float | None:
    try:
        number = float(value)
        if not math.isfinite(number) or number <= -900:
            return None
        return number
    except (ValueError, TypeError):
        return None


def nasa_annual(parameter: dict, year: int, name: str) -> float | None:
    """POWER monthly annual field YYYY13 or day-weighted monthly aggregation."""
    val = clean_number(parameter.get(f"{year}13"))
    if val is not None:
        return val
    months = [clean_number(parameter.get(f"{year}{m:02d}")) for m in range(1, 13)]
    if any(v is None for v in months):
        return None
    days = [calendar.monthrange(year, m)[1] for m in range(1, 13)]
    return sum(v * d for v, d in zip(months, days)) / sum(days)


def parse_nasa_payload(payload: dict, start: int, end: int) -> dict:
    par = payload["properties"]["parameter"]
    required = ("T2M", "RH2M", "PRECTOTCORR")
    if not all(k in par for k in required):
        raise ValueError(f"Missing NASA parameters: {required}")
    records = {}
    for y in range(start, end + 1):
        t = nasa_annual(par["T2M"], y, "T2M")
        h = nasa_annual(par["RH2M"], y, "RH2M")
        p = nasa_annual(par["PRECTOTCORR"], y, "PRECTOTCORR")
        if not (t is None or -25 <= t <= 60):
            t = None
        if not (h is None or 0 <= h <= 100):
            h = None
        if not (p is None or p >= 0):
            p = None
        records[str(y)] = {"temp": round(t, 3) if t is not None else None,
                           "humidity": round(h, 3) if h is not None else None,
                           # POWER precipitation is mm/day -> annual total, with leap year accounted for.
                           "rain_mm": round(p * (366 if calendar.isleap(y) else 365), 2) if p is not None else None}
    if not any(v["temp"] is not None and v["humidity"] is not None for v in records.values()):
        raise ValueError("NASA response contains no usable temperature/humidity observations")
    return records


def fetch_weather(base: Path, start: int = 1981, end: int = 2017, delay: float = 0.35,
                  force: bool = False, max_districts: int | None = None) -> None:
    raw, _, _ = paths(base)
    district_path = raw / "districts.json"
    if not district_path.exists():
        raise FileNotFoundError("Missing raw/districts.json. Run fetch-icrisat first.")
    districts = json.loads(district_path.read_text(encoding="utf-8"))
    if max_districts is not None:
        districts = districts[:max_districts]  # debug-only, deliberately partial
    cache_path = raw / "weather_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    s = session()
    failures = []
    for n, d in enumerate(districts, 1):
        key = str(int(d["DIST"]))
        if not force and key in cache and all(str(y) in cache[key].get("years", {}) for y in range(start, end + 1)):
            continue
        lat, lon = float(d["LAT"]), float(d["LONG"])
        if not (5 <= lat <= 38 and 67 <= lon <= 99):
            failures.append(f"{key}: district centroid outside India bounds ({lat}, {lon})")
            continue
        params = {"parameters": "T2M,RH2M,PRECTOTCORR", "community": "AG", "longitude": lon,
                  "latitude": lat, "start": start, "end": end, "format": "JSON"}
        try:
            response = s.get(NASA_URL, params=params, timeout=(15, 120))
            response.raise_for_status()
            years = parse_nasa_payload(response.json(), start, end)
            if key in cache and not force:
                years = {**cache[key].get("years", {}), **years}
            cache[key] = {"dist": d.get("DISTNAME"), "state": d.get("STNAME"),
                          "lat": lat, "lon": lon, "years": years}
            write_json_atomic(cache_path, cache)  # always resumable, no progress loss
            LOG.info("Weather fetched for district %s (%d/%d)", key, n, len(districts))
        except Exception as exc:
            LOG.error("NASA POWER error for district %s: %s", key, exc)
            failures.append(f"{key}: {exc}")
        time.sleep(delay)
    save_manifest(base, {"weather_cache.json": provenance_entry(cache_path, NASA_URL)}) if cache_path.exists() else None
    if failures:
        write_json_atomic(base / "reports" / "weather_failures.json", failures)
        raise RuntimeError(f"Weather fetch incomplete ({len(failures)} failed districts). Rerun to resume. See reports/weather_failures.json")
    LOG.info("Cached %d district weather records", len(cache))


def select_fao_pesticide(df: pd.DataFrame) -> pd.DataFrame:
    required = {"Area", "Item", "Element", "Year", "Unit", "Value"}
    if missing := required - set(df.columns):
        raise ValueError(f"FAOSTAT CSV missing required fields: {sorted(missing)}")
    m = (df["Area"].astype(str).str.strip().eq("India") &
         df["Item"].astype(str).str.strip().eq("Pesticides (total)") &
         df["Element"].astype(str).str.strip().eq("Use per area of cropland") &
         df["Unit"].astype(str).str.strip().eq("kg/ha"))
    sub = df.loc[m, ["Year", "Value"]].copy()
    sub["Year"] = pd.to_numeric(sub["Year"], errors="coerce")
    sub["Value"] = pd.to_numeric(sub["Value"], errors="coerce")
    sub = sub.dropna().sort_values("Year")
    sub = sub[sub["Value"] >= 0]
    if sub.empty:
        raise ValueError("No FAOSTAT India / Pesticides (total) / Use per area of cropland / kg/ha rows")
    if sub["Year"].duplicated().any():
        raise ValueError("FAOSTAT India total pesticide has duplicate year values; inspect item filters")
    sub["Year"] = sub["Year"].astype(int)
    return sub.reset_index(drop=True)


def fetch_pesticide(base: Path, source: str = "auto", force: bool = False) -> None:
    raw, _, _ = paths(base)
    dest = raw / "faostat_pesticide_india.csv"
    if dest.exists() and not force:
        LOG.info("Using existing %s", dest)
        return
    s = session()
    errors = []
    if source in ("api", "auto"):
        archive = raw / "faostat_pesticides_original.zip"
        try:
            save_response_stream(s, FAO_URL, archive, timeout=300)
            with zipfile.ZipFile(archive) as z:
                csvs = [n for n in z.namelist() if n.lower().endswith(".csv") and "flag" not in n.lower()]
                if not csvs:
                    raise ValueError("FAO archive has no data CSV")
                with z.open(csvs[0]) as f:
                    frame = pd.read_csv(f, encoding="latin-1", low_memory=False)
            clean = select_fao_pesticide(frame)
            clean.to_csv(dest, index=False)
            save_manifest(base, {"faostat_pesticide_india.csv": provenance_entry(dest, FAO_URL),
                                 "faostat_pesticides_original.zip": provenance_entry(archive, FAO_URL)})
            return
        except Exception as exc:
            errors.append(f"FAOSTAT: {exc}")
            LOG.warning("FAOSTAT bulk download failed: %s", exc)
    if source in ("mirror", "auto"):
        url = f"{UPSTREAM}/raw_icrisat/faostat_pesticide_india.csv"
        try:
            original = raw / "faostat_pesticide_upstream_raw.csv"
            save_response_stream(s, url, original)
            clean = select_fao_pesticide(pd.read_csv(original, encoding="latin-1"))
            clean.to_csv(dest, index=False)
            save_manifest(base, {"faostat_pesticide_india.csv": provenance_entry(dest, url),
                                 "faostat_pesticide_upstream_raw.csv": provenance_entry(original, url)})
            return
        except Exception as exc:
            errors.append(f"Upstream mirror: {exc}")
    raise RuntimeError("Unable to fetch pesticide series:\n" + "\n".join(errors))


def fetch_reference(base: Path, force: bool = False) -> None:
    reference = base / "reference"
    reference.mkdir(parents=True, exist_ok=True)
    s = session()
    records = {}
    for name in ["crop_yield_dataset.csv", "crop_yield_dataset_with_provenance.csv"]:
        output = reference / name
        url = f"{UPSTREAM}/{name}"
        if force or not output.exists():
            save_response_stream(s, url, output, timeout=300)
        records[f"reference/{name}"] = provenance_entry(output, url)
    save_manifest(base, records)


def load_grid(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_grid_payload(payload, str(path))
    names = [v["header"] if isinstance(v, dict) else str(v) for v in payload["headers"]]
    data = pd.DataFrame(payload["data"], columns=names)
    if data.columns.duplicated().any():
        raise ValueError(f"Duplicate ICRISAT headers in {path}")
    return data


def require_unique(df: pd.DataFrame, keys: list[str], label: str) -> None:
    duplicates = df.duplicated(keys, keep=False)
    if duplicates.any():
        examples = df.loc[duplicates, keys].head(5).to_dict("records")
        raise ValueError(f"{label}: duplicate keys {keys}. Examples: {examples}")


def normalize_codes(frame: pd.DataFrame, label: str, annual: bool = True) -> pd.DataFrame:
    if "Dist Code" not in frame:
        raise ValueError(f"{label} requires `Dist Code` column")
    out = frame.copy()
    out["DIST"] = pd.to_numeric(out["Dist Code"], errors="coerce").astype("Int64")
    if annual:
        if "Year" not in out:
            raise ValueError(f"{label} requires `Year` column")
        out["Year"] = pd.to_numeric(out["Year"], errors="coerce").astype("Int64")
    return out.dropna(subset=["DIST"] + (["Year"] if annual else []))


def nonnegative(v: pd.Series) -> pd.Series:
    n = pd.to_numeric(v, errors="coerce")
    return n.where((n >= 0) & np.isfinite(n))


def parse_soil(text: Any) -> tuple[str | None, float | None]:
    """Parse ICRISAT soil-order mix; only identified categories contribute to proxy."""
    if not isinstance(text, str) or not text.strip() or text.strip().upper() == "NA":
        return None, None
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if not parts:
        return None, None
    ph_sum = weight_sum = 0.0
    best_weight = -1.0
    dominant = None
    for part in parts:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", part)
        weight = float(match.group(1)) if match else 100.0 / len(parts)
        if weight <= 0 or weight > 100:
            continue
        for token, label, ph in SOIL_RULES:
            if token in part.upper():
                ph_sum += ph * weight
                weight_sum += weight
                if weight > best_weight:
                    dominant, best_weight = label, weight
                break
    return (dominant, round(ph_sum / weight_sum, 2)) if weight_sum else (None, None)


def read_weather(raw: Path) -> pd.DataFrame:
    cache = json.loads((raw / "weather_cache.json").read_text(encoding="utf-8"))
    rows = []
    for district_code, item in cache.items():
        for year, values in item.get("years", {}).items():
            rows.append((int(district_code), int(year), values.get("temp"),
                         values.get("humidity"), values.get("rain_mm")))
    out = pd.DataFrame(rows, columns=["DIST", "Year", "T_nasa", "H_nasa", "Rain_nasa"])
    for col in ["T_nasa", "H_nasa", "Rain_nasa"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    require_unique(out, ["DIST", "Year"], "NASA weather cache")
    return out


def build(base: Path, start: int = 1966, end: int = 2017) -> tuple[Path, Path, Path]:
    raw, processed, reports = paths(base)
    expected = [raw / name for name in ["districts.json", *GRID_FILES.values(),
                                         "weather_cache.json", "faostat_pesticide_india.csv"]]
    missing = [str(p) for p in expected if not p.is_file()]
    if missing:
        raise FileNotFoundError("Missing inputs (run `all` / `fetch-*` first):\n" + "\n".join(missing))
    districts = json.loads((raw / "districts.json").read_text(encoding="utf-8"))
    master = pd.DataFrame([{
        "DIST": int(d["DIST"]), "District": str(d["DISTNAME"]).strip(),
        "State": str(d["STNAME"]).strip(), "Latitude": d.get("LAT"),
        "Longitude": d.get("LONG")
    } for d in districts])
    require_unique(master, ["DIST"], "District master")
    apy = normalize_codes(load_grid(raw / GRID_FILES["area-production-yield"]), "APY")
    required_crops = [v for crop in CROP_INFO for v in (f"{crop} AREA", f"{crop} YIELD")]
    if absent := set(required_crops) - set(apy.columns):
        raise ValueError(f"ICRISAT APY schema changed. Missing columns: {sorted(absent)}")
    pieces = []
    for crop, (name, season) in CROP_INFO.items():
        pieces.append(pd.DataFrame({"DIST": apy["DIST"], "Year": apy["Year"],
                                    "Crop": name, "Season": season,
                                    "ICRISAT_Area_raw": nonnegative(apy[f"{crop} AREA"]),
                                    "Yield": nonnegative(apy[f"{crop} YIELD"]) }))
    yield_panel = pd.concat(pieces, ignore_index=True)
    yield_panel = yield_panel.loc[(yield_panel["Year"].between(start, end)) &
                                  (yield_panel["ICRISAT_Area_raw"] > 0) & (yield_panel["Yield"] > 0)].copy()
    require_unique(yield_panel, ["DIST", "Year", "Crop"], "Crop yield panel")
    raw_yield_rows = len(yield_panel)

    fert = normalize_codes(load_grid(raw / GRID_FILES["fertilizer-consumption"]), "Fertilizer")
    fcols = {"NITROGEN PER HA OF GCA": "Nitrogen", "PHOSPHATE PER HA OF GCA": "Phosphorus",
             "POTASH PER HA OF GCA": "Potassium", "TOTAL PER HA OF GCA": "Fertilizer"}
    if absent := set(fcols) - set(fert.columns):
        raise ValueError(f"ICRISAT fertilizer schema changed: {sorted(absent)}")
    fdf = fert[["DIST", "Year"]].copy()
    for source_name, target in fcols.items():
        fdf[target] = nonnegative(fert[source_name])
    require_unique(fdf, ["DIST", "Year"], "Fertilizer")

    rain = normalize_codes(load_grid(raw / GRID_FILES["monthly-rainfall"]), "Rainfall")
    rdf = rain[["DIST", "Year"]].copy()
    rdf["Rain_ICRISAT"] = nonnegative(rain["ANNUAL RAINFALL"])
    require_unique(rdf, ["DIST", "Year"], "ICRISAT rainfall")
    nrain = normalize_codes(load_grid(raw / GRID_FILES["normal-rainfall"]), "Normal rainfall", annual=False)
    nrain["Rain_normal"] = nonnegative(nrain["ANNUAL NORMAL RAINFALL"])
    nrain = nrain[["DIST", "Rain_normal"]].copy()
    require_unique(nrain, ["DIST"], "ICRISAT normal rainfall")

    soil = normalize_codes(load_grid(raw / GRID_FILES["soil-type"]), "Soil", annual=False)
    soil[["Soil Type", "pH"]] = pd.DataFrame(
        soil["SOIL TYPE PERCENT"].map(parse_soil).tolist(), index=soil.index)
    sdf = soil[["DIST", "Soil Type", "pH", "SOIL TYPE PERCENT"]].copy()
    require_unique(sdf, ["DIST"], "Soil profiles")

    wdf = read_weather(raw)
    norm = (wdf.loc[wdf["Year"].between(1981, 1990)]
            .groupby("DIST")[["T_nasa", "H_nasa"]].mean()
            .rename(columns={"T_nasa": "T_normal", "H_nasa": "H_normal"}).reset_index())
    pesticide_raw = pd.read_csv(raw / "faostat_pesticide_india.csv")
    pest = (select_fao_pesticide(pesticide_raw) if {"Area", "Item", "Element"}.issubset(pesticide_raw.columns)
            else pesticide_raw[["Year", "Value"]].copy())
    pest["Year"] = pd.to_numeric(pest["Year"], errors="raise").astype(int)
    pest["Value"] = pd.to_numeric(pest["Value"], errors="raise")
    require_unique(pest, ["Year"], "FAOSTAT year")
    pesticide_min = int(pest["Year"].min())
    pesticide_map = dict(zip(pest["Year"], pest["Value"]))

    panel = yield_panel.merge(master, on="DIST", how="left", validate="many_to_one")
    for label, table, join_keys, validate in [
        ("fert", fdf, ["DIST", "Year"], "many_to_one"),
        ("rain", rdf, ["DIST", "Year"], "many_to_one"),
        ("normal", nrain, ["DIST"], "many_to_one"),
        ("soil", sdf, ["DIST"], "many_to_one"),
        ("NASA", wdf, ["DIST", "Year"], "many_to_one"),
        ("weather normal", norm, ["DIST"], "many_to_one")]:
        before = len(panel)
        panel = panel.merge(table, on=join_keys, how="left", validate=validate)
        if len(panel) != before:
            raise AssertionError(f"{label} altered yield panel row count (join multiplication)")
    if panel["District"].isna().any():
        raise ValueError("Yield panel contains district codes not found in district master")
    panel["Region"] = panel["District"] + ", " + panel["State"]

    # Research panel: no future climatology or pesticide backfill; unknowns remain NA.
    panel["Temperature"] = panel["T_nasa"].where(panel["T_nasa"].between(-25, 60))
    panel["Humidity"] = panel["H_nasa"].where(panel["H_nasa"].between(0, 100))
    panel["temp_src"] = np.where(panel["Temperature"].notna(), "NASA_POWER", "missing")
    panel["Rainfall"] = panel["Rain_ICRISAT"].combine_first(panel["Rain_nasa"])
    panel["rain_src"] = np.select([panel["Rain_ICRISAT"].notna(), panel["Rain_nasa"].notna()],
                                  ["ICRISAT_gauge", "NASA_POWER"], default="missing")
    panel["Pesticide"] = panel["Year"].map(pesticide_map)
    panel["pest_src"] = np.where(panel["Pesticide"].notna(), "FAOSTAT_India_national", "missing")
    panel["ph_src"] = np.where(panel["pH"].notna(), "ICRISAT_soil_taxonomy_proxy", "missing")
    panel["season_src"] = "crop_lookup_dominant_season"
    panel["yield_src"] = "ICRISAT_APY"
    # Weather/source-year flags used to protect temporal prediction experiments.
    panel["weather_observed_year"] = panel["Temperature"].notna() & panel["Humidity"].notna()
    panel["rain_observed_year"] = panel["rain_src"].isin(["ICRISAT_gauge", "NASA_POWER"])
    panel["pesticide_observed_year"] = panel["Pesticide"].notna()
    panel["pH_is_proxy"] = panel["pH"].notna()
    panel["fertilizer_data_complete"] = panel[list(fcols.values())].notna().all(axis=1)
    research_cols = SCHEMA + ["DIST", "State", "District", "Latitude", "Longitude", "ICRISAT_Area_raw",
                              "SOIL TYPE PERCENT", "temp_src", "rain_src", "pest_src", "ph_src", "season_src",
                              "yield_src", "weather_observed_year", "rain_observed_year", "pesticide_observed_year",
                              "pH_is_proxy", "fertilizer_data_complete"]
    research = panel[research_cols].copy().sort_values(["State", "District", "Year", "Crop"])

    # Upstream-compatible *imputed* copy; IMPORTANT: not safe as-of-year for historical forecasting.
    legacy = panel.copy()
    before1981 = legacy["Year"].lt(1981)
    temp_fill = before1981 & legacy["Temperature"].isna() & legacy["T_normal"].notna()
    hum_fill = before1981 & legacy["Humidity"].isna() & legacy["H_normal"].notna()
    legacy.loc[temp_fill, "Temperature"] = legacy.loc[temp_fill, "T_normal"]
    legacy.loc[hum_fill, "Humidity"] = legacy.loc[hum_fill, "H_normal"]
    legacy.loc[temp_fill | hum_fill, "temp_src"] = "NASA_1981_1990_future_climatic_normal"
    rain_fill = legacy["Rainfall"].isna() & legacy["Rain_normal"].notna()
    legacy.loc[rain_fill, "Rainfall"] = legacy.loc[rain_fill, "Rain_normal"]
    legacy.loc[rain_fill, "rain_src"] = "ICRISAT_30yr_climatic_normal"
    pesticide_fill = legacy["Year"].lt(pesticide_min) & legacy["Pesticide"].isna()
    legacy.loc[pesticide_fill, "Pesticide"] = pesticide_map[pesticide_min]
    legacy.loc[pesticide_fill, "pest_src"] = "FAOSTAT_future_first_year_backfill"
    for col, decimals in {"Temperature": 2, "Humidity": 1, "Rainfall": 1, "pH": 2, "Pesticide": 4}.items():
        legacy[col] = legacy[col].round(decimals)
    legacy_complete = legacy.dropna(subset=SCHEMA).copy()
    legacy_complete = legacy_complete.sort_values(["State", "District", "Year", "Crop"])
    research_path = processed / "crop_yield_research_panel.csv"
    legacy_path = processed / "crop_yield_dataset.csv"
    provenance_path = processed / "crop_yield_dataset_with_provenance.csv"
    research.to_csv(research_path, index=False)
    legacy_complete[SCHEMA].to_csv(legacy_path, index=False)
    legacy_complete[SCHEMA + ["DIST", "State", "District", "temp_src", "rain_src", "pest_src", "ph_src"]].to_csv(provenance_path, index=False)
    LOG.info("Yield observations before covariate filtering: %d", raw_yield_rows)
    LOG.info("Research panel: %s, %d rows; legacy complete: %s, %d rows", research_path,
             len(research), legacy_path, len(legacy_complete))
    write_json_atomic(reports / "build_summary.json", {
        "yield_observations": raw_yield_rows, "research_panel_rows": len(research),
        "legacy_complete_rows": len(legacy_complete),
        "removed_from_complete": int(raw_yield_rows - len(legacy_complete)),
        "period": [start, end], "fao_first_year": pesticide_min,
        "quality_warning": "Historical climatology and pesticide backfills in legacy CSV are future-informed proxies; use research_panel for temporal experiments."})
    # Record exactly which local bytes were consumed, even for manually supplied/cached files.
    write_json_atomic(reports / "reconstruction_inputs.json", {
        "build_utc": datetime.now(timezone.utc).isoformat(),
        "script": "reconstruct.py", "start_year": start, "end_year": end,
        "input_sha256": {p.name: sha256_file(p) for p in expected},
        "output_sha256": {p.name: sha256_file(p) for p in [research_path, legacy_path, provenance_path]},
    })
    return research_path, legacy_path, provenance_path


def audit(base: Path, strict: bool = False) -> Path:
    _, processed, reports = paths(base)
    result = {}
    problems = []
    for name in ("crop_yield_research_panel.csv", "crop_yield_dataset.csv", "crop_yield_dataset_with_provenance.csv"):
        path = processed / name
        frame = pd.read_csv(path, low_memory=False)
        numeric = [c for c in NUMERIC if c in frame]
        keys = ["Year", "Region", "Crop", "Season"]
        dups = int(frame.duplicated(keys).sum())
        invalid = {}
        for col, low, high in [("Temperature", -25, 60), ("Humidity", 0, 100),
                               ("Rainfall", 0, 12000), ("pH", 0, 14),
                               ("Pesticide", 0, 100), ("Yield", 0, 1000000)]:
            if col in frame:
                x = pd.to_numeric(frame[col], errors="coerce").dropna()
                invalid[col] = int((~x.between(low, high)).sum())
        for col in ("Nitrogen", "Phosphorus", "Potassium", "Fertilizer"):
            if col in frame:
                invalid[col] = int((pd.to_numeric(frame[col], errors="coerce") < 0).sum())
        meta = {"rows": len(frame), "columns": list(frame.columns),
                "year_min": int(frame["Year"].min()) if len(frame) else None,
                "year_max": int(frame["Year"].max()) if len(frame) else None,
                "distinct_years": int(frame["Year"].nunique()),
                "district_region_count": int(frame["Region"].nunique()),
                "crop_count": int(frame["Crop"].nunique()),
                "missing_by_column": {k: int(v) for k, v in frame.isna().sum().items()},
                "duplicates_year_region_crop_season": dups,
                "out_of_range_counts": invalid,
                "records_by_year": {str(k): int(v) for k, v in frame.groupby("Year").size().items()},
                "numeric_summary": {col: {
                    "min": float(frame[col].min()) if frame[col].notna().any() else None,
                    "p01": float(frame[col].quantile(.01)) if frame[col].notna().any() else None,
                    "median": float(frame[col].median()) if frame[col].notna().any() else None,
                    "p99": float(frame[col].quantile(.99)) if frame[col].notna().any() else None,
                    "max": float(frame[col].max()) if frame[col].notna().any() else None} for col in numeric}}
        for col in ("temp_src", "rain_src", "pest_src", "ph_src"):
            if col in frame:
                meta[col + "_counts"] = {str(k): int(v) for k, v in frame[col].value_counts(dropna=False).items()}
        if dups:
            problems.append(f"{name}: {dups} duplicate keys")
        if sum(invalid.values()):
            problems.append(f"{name}: out-of-range numeric fields {invalid}")
        if name == "crop_yield_dataset.csv" and any(meta["missing_by_column"].values()):
            problems.append("Legacy main CSV has missing values (should be complete-case)")
        result[name] = meta
    summary = json.loads((reports / "build_summary.json").read_text(encoding="utf-8"))
    result["build_summary"] = summary
    # Inspect time-structured missingness and source assignment *as CSVs* for EDA.
    frame = pd.read_csv(processed / "crop_yield_research_panel.csv")
    coverage = (frame.groupby("Year", observed=True).agg(
        total_rows=("Yield", "size"), districts=("DIST", "nunique"),
        crops=("Crop", "nunique"), measured_weather=("weather_observed_year", "sum"),
        measured_rain=("rain_observed_year", "sum"),
        measured_pesticide_year=("pesticide_observed_year", "sum"),
        complete_fertilizer=("fertilizer_data_complete", "sum")
    ).reset_index())
    coverage.to_csv(reports / "coverage_by_year.csv", index=False)
    missing_cols = ["Temperature", "Humidity", "Rainfall", "Soil Type", "pH",
                    "Nitrogen", "Phosphorus", "Potassium", "Fertilizer", "Pesticide"]
    missing_year = frame.groupby("Year")[missing_cols].agg(lambda col: float(col.isna().mean()))
    missing_year.add_suffix("_missing_fraction").reset_index().to_csv(
        reports / "missingness_by_year.csv", index=False)
    sources = pd.read_csv(processed / "crop_yield_dataset_with_provenance.csv")
    source_counts = []
    for col in ("temp_src", "rain_src", "pest_src", "ph_src"):
        for val, count in sources[col].value_counts(dropna=False).items():
            source_counts.append({"column": col, "source": str(val), "rows": int(count)})
    pd.DataFrame(source_counts).to_csv(reports / "source_distribution.csv", index=False)
    # Fertilizer consistency sanity check. Do not assume exact equality in underlying raw data.
    good = frame[["Nitrogen", "Phosphorus", "Potassium", "Fertilizer"]].dropna().copy()
    diff = (good["Nitrogen"] + good["Phosphorus"] + good["Potassium"] - good["Fertilizer"]).abs()
    result["fertilizer_consistency"] = {"checked_rows": len(good),
                                         "gt_1kg_per_ha": int((diff > 1).sum()),
                                         "gt_10kg_per_ha": int((diff > 10).sum())}
    if len(good) and (diff > 10).mean() > .05:
        problems.append("Over 5% of fertilizer input rows differ from N+P+K by more than 10 kg/ha: verify source interpretation")
    result["warnings"] = problems
    output = reports / "data_quality_report.json"
    write_json_atomic(output, result)
    if strict and problems:
        raise ValueError(f"Audit raised {len(problems)} warnings; see {output}: " + "; ".join(problems))
    LOG.info("Audited CSVs and wrote %s; warnings: %d", output, len(problems))
    return output


def compare_reference(base: Path) -> Path:
    reference = base / "reference" / "crop_yield_dataset.csv"
    built = base / "processed" / "crop_yield_dataset.csv"
    if not reference.exists() or not built.exists():
        raise FileNotFoundError("Need reference/crop_yield_dataset.csv and processed/crop_yield_dataset.csv")
    ref = pd.read_csv(reference)
    out = pd.read_csv(built)
    keys = ["Year", "Region", "Crop", "Season"]
    require_unique(ref, keys, "Upstream reference")
    require_unique(out, keys, "New reconstruction")
    both = ref.merge(out, on=keys, suffixes=("_reference", "_new"), how="outer", indicator=True,
                     validate="one_to_one")
    numeric = [c for c in NUMERIC if c in ref and c in out]
    comparison = {"reference_rows": len(ref), "reconstructed_rows": len(out),
                  "reference_only": int((both["_merge"] == "left_only").sum()),
                  "reconstructed_only": int((both["_merge"] == "right_only").sum()),
                  "matched_keys": int((both["_merge"] == "both").sum()),
                  "max_absolute_diff": {col: float((both[f"{col}_reference"] - both[f"{col}_new"]).abs().max())
                                        if (both["_merge"] == "both").any() else None for col in numeric},
                  "note": "Nonzero differences can be expected from refreshed FAOSTAT/NASA vintages and leap-year-correct precipitation."}
    path = base / "reports" / "reference_comparison.json"
    write_json_atomic(path, comparison)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["all", "fetch-icrisat", "fetch-weather", "fetch-pesticide",
                                         "fetch-reference", "build", "audit", "compare-reference"])
    p.add_argument("--data-dir", type=Path, default=ROOT / "data")
    p.add_argument("--source", choices=["api", "mirror", "auto"], default="auto",
                   help="For ICRISAT / pesticide: source preference. auto = primary then repository mirror")
    p.add_argument("--force", action="store_true", help="Refresh even if raw files already exist")
    p.add_argument("--start", type=int, default=1966, help="First crop-yield year")
    p.add_argument("--end", type=int, default=2017, help="Last crop-yield year")
    p.add_argument("--weather-start", type=int, default=1981)
    p.add_argument("--delay", type=float, default=0.35, help="Seconds between NASA requests")
    p.add_argument("--max-districts", type=int, default=None, help="Partial NASA fetch for debugging only")
    p.add_argument("--strict-audit", action="store_true", help="Exit nonzero on audit warnings")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.start > args.end or args.weather_start > args.end:
        p.error("Start year must be <= end year")
    base = args.data_dir.resolve()
    paths(base)
    if args.command in ("all", "fetch-icrisat"):
        fetch_icrisat(base, source=args.source, force=args.force)
    if args.command in ("all", "fetch-pesticide"):
        fetch_pesticide(base, source=args.source, force=args.force)
    if args.command in ("all", "fetch-weather"):
        fetch_weather(base, start=args.weather_start, end=args.end, delay=args.delay,
                      force=args.force, max_districts=args.max_districts)
    if args.command == "fetch-reference":
        fetch_reference(base, force=args.force)
    if args.command in ("all", "build"):
        build(base, start=args.start, end=args.end)
    if args.command in ("all", "audit"):
        audit(base, strict=args.strict_audit)
    if args.command == "compare-reference":
        print(compare_reference(base))


if __name__ == "__main__":
    main()
