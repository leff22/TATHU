#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnóstico de grades GOES para preparar o experimento de Optical Flow.

Não gera previsões nem altera qualquer saída do TATHU. Apenas lê dois TIFFs
consecutivos e informa a estrutura das grades retificadas e metadados raster.
"""

import argparse
import glob
import os
import re
from datetime import datetime

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
    encontrados.sort(key=lambda item: item[0])
    return encontrados


def descrever_array(nome, value):
    print(f"  {nome}: tipo={type(value).__module__}.{type(value).__name__}")

    # goes16.rect2grid retorna um GDAL Dataset; ler a banda explicitamente.
    if hasattr(value, "RasterXSize") and hasattr(value, "GetRasterBand"):
        print(
            f"    raster={value.RasterXSize} x {value.RasterYSize}; "
            f"bandas={value.RasterCount}"
        )
        print(f"    geotransform={value.GetGeoTransform()}")
        print(f"    projection={value.GetProjection()[:350]}")
        for band_index in range(1, value.RasterCount + 1):
            band = value.GetRasterBand(band_index)
            arr = band.ReadAsArray()
            nodata = band.GetNoDataValue()
            print(
                f"    banda {band_index}: tipo={gdal.GetDataTypeName(band.DataType)}; "
                f"nodata={nodata}; escala={band.GetScale()}; offset={band.GetOffset()}"
            )
            if arr is None:
                print("      ReadAsArray retornou None")
                continue
            valid = np.isfinite(arr)
            if nodata is not None:
                valid &= arr != nodata
            count = int(valid.sum())
            print(
                f"      shape={arr.shape}; dtype={arr.dtype}; "
                f"validos={count}/{arr.size}; invalidos={arr.size-count}"
            )
            if count:
                vals = arr[valid].astype(np.float64, copy=False)
                p = np.percentile(vals, [1, 5, 25, 50, 75, 95, 99])
                print(
                    f"      min={vals.min():.6g}; max={vals.max():.6g}; "
                    f"media={vals.mean():.6g}; desvio={vals.std():.6g}"
                )
                print(
                    "      percentis 1/5/25/50/75/95/99%="
                    + ", ".join(f"{x:.6g}" for x in p)
                )
        return

    try:
        arr = np.asarray(value)
        print(f"    shape={arr.shape}; ndim={arr.ndim}; dtype={arr.dtype}")
        if arr.size and np.issubdtype(arr.dtype, np.number):
            finite = np.isfinite(arr)
            count = int(finite.sum())
            print(f"    finitos={count}/{arr.size}")
            if count:
                vals = arr[finite]
                print(
                    f"    min={float(vals.min()):.6g}; "
                    f"max={float(vals.max()):.6g}; "
                    f"media={float(vals.mean()):.6g}; "
                    f"desvio={float(vals.std()):.6g}"
                )
    except Exception as exc:
        print(f"    conversão para ndarray indisponível: {exc}")


def descrever_tiff(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL não conseguiu abrir: {path}")
    print(f"\nTIFF: {os.path.basename(path)}")
    print(f"  raster: {ds.RasterXSize} x {ds.RasterYSize}; bandas={ds.RasterCount}")
    print(f"  geotransform={ds.GetGeoTransform()}")
    print(f"  projection={ds.GetProjection()[:500]}")
    for band_index in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(band_index)
        print(
            f"  banda {band_index}: tipo={gdal.GetDataTypeName(band.DataType)}; "
            f"nodata={band.GetNoDataValue()}; escala={band.GetScale()}; "
            f"offset={band.GetOffset()}"
        )
    ds = None


def main():
    parser = argparse.ArgumentParser(
        description="Inspeciona duas grades GOES retificadas para Optical Flow."
    )
    parser.add_argument("--input-dir", default="./temp_goes_data_teste")
    parser.add_argument("--index", type=int, default=-1,
                        help="Índice da imagem mais recente (padrão: -1).")
    args = parser.parse_args()

    arquivos = listar_tiffs(args.input_dir)
    if len(arquivos) < 2:
        raise SystemExit(f"São necessários pelo menos dois TIFFs em {args.input_dir}.")

    idx = args.index
    if idx < 0:
        idx = len(arquivos) - 1
    if idx <= 0 or idx >= len(arquivos):
        raise SystemExit(f"Índice inválido: {args.index}; intervalo válido: 1 a {len(arquivos)-1}.")

    ts_prev, prev_path = arquivos[idx - 1]
    ts_curr, curr_path = arquivos[idx]
    print(f"Par selecionado: {ts_prev:%Y-%m-%d %H:%M} -> {ts_curr:%Y-%m-%d %H:%M}")
    print(f"Intervalo real: {(ts_curr - ts_prev).total_seconds() / 60.0:.1f} min")
    descrever_tiff(prev_path)
    descrever_tiff(curr_path)

    extent, resolution = tathu.get_tiff_extent_and_resolution_km(curr_path)
    print(f"\nRetificação TATHU compartilhada:")
    print(f"  extent={extent}")
    print(f"  resolution={resolution}")

    grid_prev = goes16.rect2grid(
        prev_path, extent=extent, resolution=resolution, autoscale=False
    )
    grid_curr = goes16.rect2grid(
        curr_path, extent=extent, resolution=resolution, autoscale=False
    )
    descrever_array("grid_prev", grid_prev)
    descrever_array("grid_curr", grid_curr)

    try:
        import cv2
        print(f"\nOpenCV: {cv2.__version__}; Farneback disponível={hasattr(cv2, 'calcOpticalFlowFarneback')}")
    except Exception as exc:
        print(f"\nOpenCV indisponível: {exc}")

    try:
        import skimage
        from skimage.registration import optical_flow_tvl1
        print(f"scikit-image: {skimage.__version__}; TV-L1 disponível={callable(optical_flow_tvl1)}")
    except Exception as exc:
        print(f"scikit-image/TV-L1 indisponível: {exc}")


if __name__ == "__main__":
    main()
