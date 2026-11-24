#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Experimento isolado de Optical Flow Farneback para grades GOES retificadas.

Este script NÃO altera o forecast operacional nem os produtos do TATHU.
Calcula o fluxo entre duas imagens consecutivas, converte o deslocamento de
pixels para km usando o geotransform e salva diagnóstico em diretório próprio.
"""

import argparse
import glob
import json
import os
import re
from datetime import datetime

import cv2
import numpy as np
from osgeo import gdal
from tathu.satellite import goes16
import tathu_producao as tathu


TIFF_RE = re.compile(r"^S10635346_(\d{12})\.tif$", re.IGNORECASE)


def listar_tiffs(input_dir):
    encontrados = []
    for path in glob.glob(os.path.join(input_dir, "S10635346_*.tif")):
        match = TIFF_RE.match(os.path.basename(path))
        if match:
            ts = datetime.strptime(match.group(1), "%Y%m%d%H%M")
            encontrados.append((ts, os.path.abspath(path)))
    return sorted(encontrados, key=lambda item: item[0])


def ler_grade(dataset):
    if dataset is None or dataset.RasterCount < 1:
        raise RuntimeError("Grade GDAL inválida ou sem bandas.")
    band = dataset.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    valid = np.isfinite(arr)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= arr != nodata
    return arr, valid, dataset.GetGeoTransform()


def normalizar_par(prev, curr, valid_prev, valid_curr):
    """Usa os mesmos limites robustos para as duas imagens."""
    values = np.concatenate((prev[valid_prev], curr[valid_curr]))
    if values.size == 0:
        raise RuntimeError("Não há pixels válidos para normalização.")
    low, high = np.percentile(values, [1, 99])
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        raise RuntimeError(f"Intervalo de normalização inválido: {low}, {high}")
    def convert(arr, valid):
        scaled = np.clip((arr - low) / (high - low), 0.0, 1.0)
        image = np.round(scaled * 255).astype(np.uint8)
        image[~valid] = 0
        return image
    return convert(prev, valid_prev), convert(curr, valid_curr), float(low), float(high)


def estatisticas(name, values):
    values = np.asarray(values)
    values = values[np.isfinite(values)]
    if not values.size:
        return {"name": name, "count": 0}
    p = np.percentile(values, [50, 75, 90, 95, 99])
    return {
        "name": name,
        "count": int(values.size),
        "mean": float(np.mean(values)),
        "median": float(p[0]),
        "p75": float(p[1]),
        "p90": float(p[2]),
        "p95": float(p[3]),
        "p99": float(p[4]),
        "max": float(np.max(values)),
    }


def salvar_visualizacao(flow, curr_img, valid):
    mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1], angleInDegrees=True)
    hsv = np.zeros((*curr_img.shape, 3), dtype=np.uint8)
    hsv[..., 0] = np.mod(ang / 2.0, 180).astype(np.uint8)
    hsv[..., 1] = 255
    scale = np.percentile(mag[valid], 99) if np.any(valid) else 1.0
    if not np.isfinite(scale) or scale <= 0:
        scale = 1.0
    hsv[..., 2] = np.clip(mag / scale * 255, 0, 255).astype(np.uint8)
    hsv[~valid] = 0
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def main():
    parser = argparse.ArgumentParser(
        description="Calcula Optical Flow Farneback experimental em duas grades GOES."
    )
    parser.add_argument("--input-dir", default="./temp_goes_data_teste")
    parser.add_argument("--output-dir", default="./saidas_optical_flow_experimental")
    parser.add_argument("--index", type=int, default=-1,
                        help="Índice da imagem mais recente (padrão: -1).")
    parser.add_argument("--winsize", type=int, default=21)
    parser.add_argument("--levels", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()

    arquivos = listar_tiffs(args.input_dir)
    if len(arquivos) < 2:
        raise SystemExit(f"São necessários pelo menos dois TIFFs em {args.input_dir}.")
    idx = args.index if args.index >= 0 else len(arquivos) - 1
    if idx <= 0 or idx >= len(arquivos):
        raise SystemExit(f"Índice inválido: {args.index}; intervalo válido: 1 a {len(arquivos)-1}.")

    ts_prev, prev_path = arquivos[idx - 1]
    ts_curr, curr_path = arquivos[idx]
    interval_min = (ts_curr - ts_prev).total_seconds() / 60.0
    if interval_min <= 0:
        raise RuntimeError("O intervalo temporal precisa ser positivo.")

    extent, resolution = tathu.get_tiff_extent_and_resolution_km(curr_path)
    ds_prev = goes16.rect2grid(prev_path, extent=extent, resolution=resolution, autoscale=False)
    ds_curr = goes16.rect2grid(curr_path, extent=extent, resolution=resolution, autoscale=False)
    prev, valid_prev, gt_prev = ler_grade(ds_prev)
    curr, valid_curr, gt_curr = ler_grade(ds_curr)

    if prev.shape != curr.shape or not np.allclose(gt_prev, gt_curr, rtol=0, atol=1e-10):
        raise RuntimeError("As grades retificadas não possuem geometria idêntica.")
    valid = valid_prev & valid_curr
    if valid.mean() < 0.5:
        raise RuntimeError(f"Cobertura válida insuficiente: {valid.mean():.1%}")

    prev_img, curr_img, norm_low, norm_high = normalizar_par(
        prev, curr, valid_prev, valid_curr
    )
    # Zera pixels inválidos nas duas imagens para evitar artefatos nas bordas.
    prev_img[~valid] = 0
    curr_img[~valid] = 0

    flow = cv2.calcOpticalFlowFarneback(
        prev_img, curr_img, None,
        pyr_scale=0.5,
        levels=args.levels,
        winsize=args.winsize,
        iterations=args.iterations,
        poly_n=7,
        poly_sigma=1.5,
        flags=0,
    )

    rows, cols = np.indices(prev.shape, dtype=np.float32)
    lat = gt_prev[3] + (rows + 0.5) * gt_prev[5]
    # Conversão local aproximada: graus de longitude variam com cos(latitude).
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * np.cos(np.deg2rad(lat))
    dx_km = flow[..., 0] * gt_prev[1] * km_per_deg_lon
    dy_km = flow[..., 1] * gt_prev[5] * km_per_deg_lat
    displacement_km = np.hypot(dx_km, dy_km)
    speed_kmh = displacement_km / (interval_min / 60.0)

    # Evita bordas da imagem, onde o algoritmo tende a produzir fluxo instável.
    interior = valid.copy()
    border = max(3, args.winsize // 2)
    interior[:border, :] = False
    interior[-border:, :] = False
    interior[:, :border] = False
    interior[:, -border:] = False
    if not np.any(interior):
        raise RuntimeError("Não restaram pixels válidos após excluir as bordas.")

    os.makedirs(args.output_dir, exist_ok=True)
    stamp = ts_curr.strftime("%Y%m%d%H%M")
    npz_path = os.path.join(args.output_dir, f"optical_flow_{stamp}.npz")
    png_path = os.path.join(args.output_dir, f"optical_flow_{stamp}.png")
    json_path = os.path.join(args.output_dir, f"optical_flow_{stamp}.json")

    np.savez_compressed(
        npz_path,
        flow_px=flow,
        dx_km=dx_km.astype(np.float32),
        dy_km=dy_km.astype(np.float32),
        displacement_km=displacement_km.astype(np.float32),
        speed_kmh=speed_kmh.astype(np.float32),
        valid_mask=interior,
        geotransform=np.asarray(gt_prev),
    )
    visualization = salvar_visualizacao(flow, curr_img, interior)
    if not cv2.imwrite(png_path, visualization):
        raise RuntimeError(f"Falha ao salvar visualização: {png_path}")

    summary = {
        "method": "Farneback",
        "previous_timestamp": ts_prev.isoformat(),
        "current_timestamp": ts_curr.isoformat(),
        "interval_minutes": interval_min,
        "grid_shape_rows_cols": list(prev.shape),
        "geotransform": list(gt_prev),
        "normalization_percentiles": [1, 99],
        "normalization_limits_source_units": [norm_low, norm_high],
        "valid_fraction_after_border": float(interior.mean()),
        "parameters": {
            "pyr_scale": 0.5,
            "levels": args.levels,
            "winsize": args.winsize,
            "iterations": args.iterations,
            "poly_n": 7,
            "poly_sigma": 1.5,
        },
        "flow_pixels": estatisticas("magnitude_pixels", np.linalg.norm(flow, axis=2)[interior]),
        "displacement_km": estatisticas("displacement_km", displacement_km[interior]),
        "speed_kmh": estatisticas("speed_kmh", speed_kmh[interior]),
        "warning": (
            "Diagnóstico exploratório: fluxo de textura radiométrica não equivale "
            "automaticamente ao movimento de células convectivas. Validar vetores "
            "e associação com sistemas antes de qualquer uso em previsão."
        ),
        "outputs": {
            "npz": os.path.basename(npz_path),
            "png": os.path.basename(png_path),
        },
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"Par: {ts_prev:%Y-%m-%d %H:%M} -> {ts_curr:%Y-%m-%d %H:%M}")
    print(f"Intervalo: {interval_min:.1f} min")
    print(f"Grade: {prev.shape[1]} x {prev.shape[0]}; cobertura válida: {interior.mean():.1%}")
    print(f"Normalização conjunta p1-p99: {norm_low:.4f} a {norm_high:.4f}")
    for key in ("flow_pixels", "displacement_km", "speed_kmh"):
        item = summary[key]
        print(
            f"{key}: mediana={item.get('median', float('nan')):.3f}; "
            f"p90={item.get('p90', float('nan')):.3f}; "
            f"p95={item.get('p95', float('nan')):.3f}; "
            f"máximo={item.get('max', float('nan')):.3f}"
        )
    print(f"NPZ: {npz_path}")
    print(f"PNG: {png_path}")
    print(f"JSON: {json_path}")
    print("ATENÇÃO: resultado experimental; não integrado ao forecast operacional.")


if __name__ == "__main__":
    main()
