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

    # Segundo método independente para comparação. TV-L1 recebe imagens float
    # no intervalo [0, 1] e retorna (v, u); reorganizamos para (u, v).
    from skimage.registration import optical_flow_tvl1
    tvl1_raw = optical_flow_tvl1(
        prev_img.astype(np.float32) / 255.0,
        curr_img.astype(np.float32) / 255.0,
        attachment=15,
        tightness=0.3,
        num_warp=5,
        num_iter=10,
        tol=0.0001,
        prefilter=False,
    )
    flow_tvl1 = np.stack((tvl1_raw[1], tvl1_raw[0]), axis=-1).astype(np.float32)

    rows, cols = np.indices(prev.shape, dtype=np.float32)
    lat = gt_prev[3] + (rows + 0.5) * gt_prev[5]
    # Conversão local aproximada: graus de longitude variam com cos(latitude).
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * np.cos(np.deg2rad(lat))
    dx_km = flow[..., 0] * gt_prev[1] * km_per_deg_lon
    dy_km = flow[..., 1] * gt_prev[5] * km_per_deg_lat
    displacement_km = np.hypot(dx_km, dy_km)
    speed_kmh = displacement_km / (interval_min / 60.0)

    dx_tvl1_km = flow_tvl1[..., 0] * gt_prev[1] * km_per_deg_lon
    dy_tvl1_km = flow_tvl1[..., 1] * gt_prev[5] * km_per_deg_lat
    displacement_tvl1_km = np.hypot(dx_tvl1_km, dy_tvl1_km)
    speed_tvl1_kmh = displacement_tvl1_km / (interval_min / 60.0)

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

    # Diagnósticos adicionais para separar fluxo sobre nuvens de fluxo sobre
    # superfície/textura quente. Os limiares são exploratórios em °C e não
    # representam uma classificação operacional de nuvens.
    cloud_masks = {
        "bt_le_0C_both": interior & (prev <= 0.0) & (curr <= 0.0),
        "bt_le_m20C_both": interior & (prev <= -20.0) & (curr <= -20.0),
        "bt_le_m40C_both": interior & (prev <= -40.0) & (curr <= -40.0),
    }
    flow_magnitude_px = np.linalg.norm(flow, axis=2)
    flow_magnitude_tvl1_px = np.linalg.norm(flow_tvl1, axis=2)
    flow_stats = estatisticas("magnitude_pixels", flow_magnitude_px[interior])
    tvl1_stats = estatisticas("magnitude_pixels", flow_magnitude_tvl1_px[interior])
    speed_tvl1_stats = estatisticas("speed_kmh", speed_tvl1_kmh[interior])

    # Concordância direcional entre métodos, excluindo vetores quase nulos.
    mag_fb = flow_magnitude_px
    mag_tv = flow_magnitude_tvl1_px
    direction_mask = interior & (mag_fb > 0.5) & (mag_tv > 0.5)
    if np.any(direction_mask):
        dot = (
            flow[..., 0] * flow_tvl1[..., 0]
            + flow[..., 1] * flow_tvl1[..., 1]
        )
        cosine = np.clip(dot / np.maximum(mag_fb * mag_tv, 1e-6), -1.0, 1.0)
        direction_difference_deg = np.degrees(np.arccos(cosine[direction_mask]))
        direction_agreement = {
            "pixel_count": int(direction_mask.sum()),
            "median_angle_difference_deg": float(np.median(direction_difference_deg)),
            "p90_angle_difference_deg": float(np.percentile(direction_difference_deg, 90)),
            "fraction_within_30deg_percent": float(np.mean(direction_difference_deg <= 30) * 100.0),
        }
    else:
        direction_agreement = {"pixel_count": 0}
    dx_stats = estatisticas("dx_km", dx_km[interior])
    dy_stats = estatisticas("dy_km", dy_km[interior])
    speed_stats = estatisticas("speed_kmh", speed_kmh[interior])
    speed_exceedance = {
        str(limit): float(np.mean(speed_kmh[interior] > limit) * 100.0)
        for limit in (60, 80, 100, 120, 150, 200)
    }
    cloud_stats = {}
    for label, mask in cloud_masks.items():
        cloud_stats[label] = {
            "pixel_count": int(mask.sum()),
            "fraction_of_grid_percent": float(mask.mean() * 100.0),
            "flow_pixels": estatisticas("magnitude_pixels", flow_magnitude_px[mask]),
            "speed_kmh": estatisticas("speed_kmh", speed_kmh[mask]),
            "fraction_speed_over_100_kmh_percent": (
                float(np.mean(speed_kmh[mask] > 100.0) * 100.0) if np.any(mask) else None
            ),
        }

    # Grade 4x4: ajuda a localizar numericamente regiões com fluxo extremo
    # quando o usuário não consegue compartilhar a visualização PNG.
    tile_diagnostics = []
    nrows, ncols = prev.shape
    for iy in range(4):
        for ix in range(4):
            y0, y1 = iy * nrows // 4, (iy + 1) * nrows // 4
            x0, x1 = ix * ncols // 4, (ix + 1) * ncols // 4
            mask = interior[y0:y1, x0:x1]
            vals = speed_kmh[y0:y1, x0:x1][mask]
            if vals.size:
                tile_diagnostics.append({
                    "tile_row": iy,
                    "tile_col": ix,
                    "valid_pixels": int(vals.size),
                    "median_speed_kmh": float(np.median(vals)),
                    "p90_speed_kmh": float(np.percentile(vals, 90)),
                    "fraction_over_100_kmh_percent": float(np.mean(vals > 100.0) * 100.0),
                })

    np.savez_compressed(
        npz_path,
        flow_px=flow,
        flow_tvl1_px=flow_tvl1,
        speed_tvl1_kmh=speed_tvl1_kmh.astype(np.float32),
        dx_km=dx_km.astype(np.float32),
        dy_km=dy_km.astype(np.float32),
        displacement_km=displacement_km.astype(np.float32),
        speed_kmh=speed_kmh.astype(np.float32),
        valid_mask=interior,
        cloud_mask_bt_le_0C=cloud_masks["bt_le_0C_both"],
        cloud_mask_bt_le_m20C=cloud_masks["bt_le_m20C_both"],
        cloud_mask_bt_le_m40C=cloud_masks["bt_le_m40C_both"],
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
        "flow_pixels": flow_stats,
        "tvl1_flow_pixels": tvl1_stats,
        "tvl1_speed_kmh": speed_tvl1_stats,
        "method_comparison": {
            "direction_agreement": direction_agreement,
            "farneback_speed_kmh": speed_stats,
            "tvl1_speed_kmh": speed_tvl1_stats,
        },
        "dx_km": dx_stats,
        "dy_km": dy_stats,
        "displacement_km": estatisticas("displacement_km", displacement_km[interior]),
        "speed_kmh": speed_stats,
        "speed_exceedance_percent": speed_exceedance,
        "cloud_temperature_masks": cloud_stats,
        "spatial_tiles_4x4": tile_diagnostics,
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
    print(
        f"TV-L1: fluxo mediano={tvl1_stats.get('median', float('nan')):.3f} px; "
        f"velocidade mediana={speed_tvl1_stats.get('median', float('nan')):.3f} km/h; "
        f"p90={speed_tvl1_stats.get('p90', float('nan')):.3f}; "
        f"p95={speed_tvl1_stats.get('p95', float('nan')):.3f}; "
        f"máximo={speed_tvl1_stats.get('max', float('nan')):.3f}"
    )
    print(
        "Concordância direcional Farneback x TV-L1: "
        f"diferença angular mediana={direction_agreement.get('median_angle_difference_deg', float('nan')):.1f}°; "
        f"p90={direction_agreement.get('p90_angle_difference_deg', float('nan')):.1f}°; "
        f"vetores até 30°={direction_agreement.get('fraction_within_30deg_percent', float('nan')):.1f}%"
    )
    print("Percentual de pixels acima de cada velocidade (km/h) - Farneback:")
    for limit, pct in speed_exceedance.items():
        print(f"  >{limit}: {pct:.2f}%")
    print("Diagnóstico por limiar de temperatura (pixels nas duas imagens):")
    for label, item in cloud_stats.items():
        speed = item["speed_kmh"]
        if speed["count"]:
            print(
                f"  {label}: n={item['pixel_count']}; "
                f"mediana={speed['median']:.1f} km/h; "
                f"p90={speed['p90']:.1f} km/h; "
                f">100 km/h={item['fraction_speed_over_100_kmh_percent']:.1f}%"
            )
        else:
            print(f"  {label}: sem pixels")
    print("Grade espacial 4x4 (mediana/p90 km/h):")
    for tile in tile_diagnostics:
        print(
            f"  [{tile['tile_row']},{tile['tile_col']}]: "
            f"{tile['median_speed_kmh']:.1f}/{tile['p90_speed_kmh']:.1f} "
            f"(>{100} km/h: {tile['fraction_over_100_kmh_percent']:.1f}%)"
        )
    print(f"NPZ: {npz_path}")
    print(f"PNG: {png_path}")
    print(f"JSON: {json_path}")
    print("ATENÇÃO: resultado experimental; não integrado ao forecast operacional.")


if __name__ == "__main__":
    main()
