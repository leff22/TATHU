#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Avaliação independente das previsões do TATHU.

Compara os polígonos previstos em previsao_<timestamp>.geojson com as
detecções reais em historico_<timestamp_alvo>.geojson. Não altera produtos
nem parâmetros do forecast operacional.

Dependência: GDAL/OGR disponível no mesmo ambiente do TATHU.
"""
import argparse
import csv
import glob
import json
import math
import os
import re
from datetime import datetime, timedelta

TIMESTAMP_RE = re.compile(r"^(\d{12})$")
FORECAST_FILE_RE = re.compile(r"^previsao_(\d{12})\.geojson$")


def parse_timestamp(value):
    if not TIMESTAMP_RE.match(value or ""):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M")
    except ValueError:
        return None


def load_feature_collection(path):
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("type") != "FeatureCollection":
        raise ValueError(f"GeoJSON não é FeatureCollection: {path}")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"Campo 'features' inválido: {path}")
    return features


def make_projector():
    from osgeo import ogr, osr

    source = osr.SpatialReference()
    source.ImportFromEPSG(4326)
    target = osr.SpatialReference()
    target.ImportFromEPSG(3857)
    if hasattr(source, "SetAxisMappingStrategy"):
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return ogr, osr.CoordinateTransformation(source, target)


def prepare_feature(feature, ogr, transform):
    geometry_json = feature.get("geometry")
    if not geometry_json:
        return None
    geometry = ogr.CreateGeometryFromJson(json.dumps(geometry_json))
    if geometry is None or geometry.IsEmpty():
        return None
    geometry = geometry.Clone()
    geometry.Transform(transform)
    if not geometry.IsValid():
        geometry = geometry.Buffer(0)
    if geometry is None or geometry.IsEmpty():
        return None
    centroid = geometry.Centroid()
    return {
        "id": (feature.get("properties") or {}).get("system_id", ""),
        "geometry": geometry,
        "centroid": (centroid.GetX(), centroid.GetY()),
        "area_m2": geometry.GetArea(),
    }


def load_prepared(path, ogr, transform):
    raw_features = load_feature_collection(path)
    prepared = []
    for feature in raw_features:
        item = prepare_feature(feature, ogr, transform)
        if item is not None:
            prepared.append(item)
    return prepared


def distance_m(a, b):
    return math.hypot(a["centroid"][0] - b["centroid"][0],
                      a["centroid"][1] - b["centroid"][1])


def greedy_match(left, right, max_distance_m):
    """Associa centróides por menor distância, sem reutilizar sistemas."""
    candidates = []
    for i, first in enumerate(left):
        for j, second in enumerate(right):
            distance = distance_m(first, second)
            if distance <= max_distance_m:
                candidates.append((distance, i, j))
    candidates.sort(key=lambda item: item[0])

    used_left = set()
    used_right = set()
    matches = []
    for distance, i, j in candidates:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        matches.append((i, j, distance))

    unmatched_left = [i for i in range(len(left)) if i not in used_left]
    unmatched_right = [j for j in range(len(right)) if j not in used_right]
    return matches, unmatched_left, unmatched_right


def intersection_over_union(first, second):
    try:
        intersection = first["geometry"].Intersection(second["geometry"])
        intersection_area = intersection.GetArea() if intersection else 0.0
        union_area = first["area_m2"] + second["area_m2"] - intersection_area
        return intersection_area / union_area if union_area > 0 else None
    except Exception:
        return None


def nearest_source(forecast, sources):
    if not sources:
        return None, None
    index = min(range(len(sources)), key=lambda i: distance_m(forecast, sources[i]))
    return sources[index], distance_m(forecast, sources[index])


def displacement_metrics(source, forecast, observed):
    if source is None:
        return {
            "predicted_displacement_km": None,
            "observed_displacement_km": None,
            "along_track_error_km": None,
            "cross_track_error_km": None,
        }

    sx, sy = source["centroid"]
    px, py = forecast["centroid"]
    ox, oy = observed["centroid"]
    predicted_x, predicted_y = px - sx, py - sy
    observed_x, observed_y = ox - sx, oy - sy
    predicted_length = math.hypot(predicted_x, predicted_y)
    observed_length = math.hypot(observed_x, observed_y)

    if predicted_length > 0:
        unit_x = predicted_x / predicted_length
        unit_y = predicted_y / predicted_length
        error_x, error_y = px - ox, py - oy
        along = (error_x * unit_x + error_y * unit_y) / 1000.0
        cross = (unit_x * error_y - unit_y * error_x) / 1000.0
    else:
        along = None
        cross = None

    return {
        "predicted_displacement_km": predicted_length / 1000.0,
        "observed_displacement_km": observed_length / 1000.0,
        # Positivo: previsão ficou à frente da posição observada no eixo previsto.
        "along_track_error_km": along,
        "cross_track_error_km": cross,
    }


def build_row(base_ts, target_ts, lead, forecast, observed, source,
              match_distance_m, source_distance_m, status):
    row = {
        "base_timestamp": base_ts,
        "target_timestamp": target_ts,
        "lead_min": lead,
        "forecast_system_id": forecast.get("id", "") if forecast else "",
        "observed_system_id": observed.get("id", "") if observed else "",
        "status": status,
        "centroid_error_km": None,
        "iou": None,
        "area_error_pct": None,
        "match_centroid_distance_km": (
            match_distance_m / 1000.0 if match_distance_m is not None else None
        ),
        "source_association_distance_km": (
            source_distance_m / 1000.0 if source_distance_m is not None else None
        ),
        "predicted_displacement_km": None,
        "observed_displacement_km": None,
        "along_track_error_km": None,
        "cross_track_error_km": None,
    }

    if forecast is not None and observed is not None:
        row["centroid_error_km"] = match_distance_m / 1000.0
        row["iou"] = intersection_over_union(forecast, observed)
        if observed["area_m2"] > 0:
            row["area_error_pct"] = (
                (forecast["area_m2"] - observed["area_m2"])
                / observed["area_m2"] * 100.0
            )
        row.update(displacement_metrics(source, forecast, observed))
    return row


def evaluate(output_dir, leads, max_match_distance_km, csv_path):
    ogr, transform = make_projector()
    max_distance_m = max_match_distance_km * 1000.0
    rows = []
    skipped = []

    forecast_paths = sorted(glob.glob(os.path.join(output_dir, "previsao_*.geojson")))
    for forecast_path in forecast_paths:
        filename = os.path.basename(forecast_path)
        match = FORECAST_FILE_RE.match(filename)
        if not match:
            continue

        base_dt = parse_timestamp(match.group(1))
        if base_dt is None:
            continue
        base_ts = base_dt.strftime("%Y%m%d%H%M")
        source_path = os.path.join(output_dir, f"historico_{base_ts}.geojson")
        if not os.path.isfile(source_path):
            skipped.append(f"{base_ts}: sem GeoJSON de detecção da rodada inicial")
            continue

        try:
            forecast_features = load_feature_collection(forecast_path)
            sources = load_prepared(source_path, ogr, transform)
        except Exception as exc:
            skipped.append(f"{base_ts}: falha lendo arquivos da rodada: {exc}")
            continue

        for lead in leads:
            target_dt = base_dt + timedelta(minutes=lead)
            target_ts = target_dt.strftime("%Y%m%d%H%M")
            observed_path = os.path.join(output_dir, f"historico_{target_ts}.geojson")
            if not os.path.isfile(observed_path):
                skipped.append(f"{base_ts} +{lead}: aguardando {os.path.basename(observed_path)}")
                continue

            try:
                forecasts = []
                for feature in forecast_features:
                    props = feature.get("properties") or {}
                    if props.get("stage") != "forecast":
                        continue
                    try:
                        feature_lead = int(props.get("lead_min"))
                    except (TypeError, ValueError):
                        continue
                    if feature_lead != lead:
                        continue
                    item = prepare_feature(feature, ogr, transform)
                    if item is not None:
                        forecasts.append(item)
                observed = load_prepared(observed_path, ogr, transform)
            except Exception as exc:
                skipped.append(f"{base_ts} +{lead}: falha lendo GeoJSON: {exc}")
                continue

            matches, unmatched_forecasts, unmatched_observed = greedy_match(
                forecasts, observed, max_distance_m
            )

            for forecast_index, observed_index, match_distance in matches:
                forecast = forecasts[forecast_index]
                actual = observed[observed_index]
                source, source_distance = nearest_source(forecast, sources)
                rows.append(build_row(
                    base_ts, target_ts, lead, forecast, actual, source,
                    match_distance, source_distance, "matched"
                ))

            for index in unmatched_forecasts:
                forecast = forecasts[index]
                source, source_distance = nearest_source(forecast, sources)
                rows.append(build_row(
                    base_ts, target_ts, lead, forecast, None, source,
                    None, source_distance, "forecast_without_observation"
                ))

            for index in unmatched_observed:
                rows.append(build_row(
                    base_ts, target_ts, lead, None, observed[index], None,
                    None, None, "observation_without_forecast"
                ))

    fieldnames = [
        "base_timestamp", "target_timestamp", "lead_min",
        "forecast_system_id", "observed_system_id", "status",
        "centroid_error_km", "iou", "area_error_pct",
        "match_centroid_distance_km", "source_association_distance_km",
        "predicted_displacement_km", "observed_displacement_km",
        "along_track_error_km", "cross_track_error_km",
    ]
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    matched = [row for row in rows if row["status"] == "matched"]
    print(f"Comparações válidas: {len(matched)}")
    print(f"Previsões sem detecção correspondente: {sum(r['status'] == 'forecast_without_observation' for r in rows)}")
    print(f"Detecções sem previsão correspondente: {sum(r['status'] == 'observation_without_forecast' for r in rows)}")
    if matched:
        for key, label in (
            ("centroid_error_km", "Erro médio de centróide"),
            ("iou", "IoU médio"),
            ("area_error_pct", "Erro médio de área"),
            ("along_track_error_km", "Erro médio ao longo da trajetória"),
            ("cross_track_error_km", "Erro médio transversal"),
        ):
            values = [row[key] for row in matched if row[key] is not None]
            if values:
                mean = sum(values) / len(values)
                print(f"{label}: {mean:.3f}")
    if skipped:
        print(f"Rodadas ainda não avaliadas/ignoradas: {len(skipped)}")
        for message in skipped[:20]:
            print(f"  - {message}")
        if len(skipped) > 20:
            print(f"  ... e mais {len(skipped) - 20}")
    print(f"CSV salvo em: {csv_path}")
    return len(matched)


def main():
    parser = argparse.ArgumentParser(
        description="Avalia previsões TATHU contra detecções reais posteriores."
    )
    parser.add_argument(
        "--input-dir", default="./saidas_detecao_script",
        help="Diretório com previsao_*.geojson e historico_*.geojson."
    )
    parser.add_argument(
        "--leads", nargs="+", type=int, default=[20],
        choices=[20, 40, 60],
        help="Horizontes a avaliar em minutos (padrão: 20)."
    )
    parser.add_argument(
        "--max-match-distance-km", type=float, default=75.0,
        help="Distância máxima entre centróides para aceitar uma associação (padrão: 75 km)."
    )
    parser.add_argument(
        "--output", default=None,
        help="Caminho do CSV (padrão: <input-dir>/avaliacao_previsoes.csv)."
    )
    args = parser.parse_args()

    if args.max_match_distance_km <= 0:
        parser.error("--max-match-distance-km deve ser maior que zero.")
    csv_path = args.output or os.path.join(args.input_dir, "avaliacao_previsoes.csv")
    evaluate(args.input_dir, args.leads, args.max_match_distance_km, csv_path)


if __name__ == "__main__":
    main()
