#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Processa uma sequência local de TIFFs GOES em modo histórico/lote.

Usa a mesma detecção, tracking diagnóstico e previsão do tathu_producao.py,
mas não acessa o servidor remoto e grava em uma pasta de saída independente.
"""

import argparse
import glob
import os
import re
from datetime import datetime
from pathlib import Path

import tathu_producao as tathu


TIFF_RE = re.compile(r"^S10635346_(\d{12})\.tif$", re.IGNORECASE)


def listar_tiffs(input_dir):
    encontrados = []
    for path in glob.glob(os.path.join(input_dir, "S10635346_*.tif")):
        match = TIFF_RE.match(os.path.basename(path))
        if not match:
            continue
        timestamp = datetime.strptime(match.group(1), "%Y%m%d%H%M")
        encontrados.append((timestamp, os.path.abspath(path)))

    encontrados.sort(key=lambda item: item[0])
    # Um arquivo por timestamp, mantendo o primeiro caminho encontrado.
    unicos = {}
    for timestamp, path in encontrados:
        unicos.setdefault(timestamp, path)
    return [(timestamp, path) for timestamp, path in unicos.items()]


def detectar(path, extent=None, resolution=None):
    """Executa a detecção TATHU para um TIFF e devolve grid, sistemas e timestamp."""
    from tathu.satellite import goes16
    from tathu.tracking import detectors, descriptors
    from tathu.tracking.utils import area2degrees

    timestamp_match = TIFF_RE.match(os.path.basename(path))
    if not timestamp_match:
        raise ValueError(f"Nome de TIFF fora do padrão esperado: {path}")
    timestamp = datetime.strptime(timestamp_match.group(1), "%Y%m%d%H%M")

    if extent is None or resolution is None:
        extent, resolution = tathu.get_tiff_extent_and_resolution_km(path)

    grid = goes16.rect2grid(
        path, extent=extent, resolution=resolution, autoscale=False
    )
    minarea_deg2 = area2degrees(tathu.MINAREA_KM2)
    threshold_c = tathu.THRESHOLD_K - 273.15
    detector = detectors.LessThan(threshold_c, minarea_deg2)
    systems = detector.detect(grid)

    for system in systems:
        system.timestamp = timestamp
    descriptors.StatisticalDescriptor().describe(grid, systems)
    return timestamp, extent, resolution, systems


def processar_sequencia(arquivos, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    # Redireciona os helpers de publicação para uma pasta de teste isolada.
    tathu.OUTPUTS_SHP_DIR = output_dir
    tathu.HISTORY_DIR = os.path.join(output_dir, "historico")

    total = len(arquivos)
    falhas = []

    for index, (timestamp, curr_path) in enumerate(arquivos):
        ts_str = timestamp.strftime("%Y%m%d%H%M")
        print(f"\n[{index + 1}/{total}] Processando {ts_str} | {os.path.basename(curr_path)}")

        detection_tmp = os.path.join(output_dir, f".{ts_str}_deteccao_tmp.tif")
        forecast_tmp = os.path.join(output_dir, f".{ts_str}_previsao_tmp.tif")

        try:
            ts_curr, extent, resolution, systems_curr = detectar(curr_path)
            print(f"[info] Sistemas detectados: {len(systems_curr)}")

            tathu.rasterize_systems_to_tif(
                systems_curr, curr_path, detection_tmp
            )

            forecasts = {20: [], 40: [], 60: []}

            if index >= 2:
                ts_prev2, prev2_path = arquivos[index - 2]
                ts_prev1, prev1_path = arquivos[index - 1]

                _, _, _, systems_prev2 = detectar(
                    prev2_path, extent=extent, resolution=resolution
                )
                _, _, _, systems_prev1 = detectar(
                    prev1_path, extent=extent, resolution=resolution
                )

                print(
                    f"[info] Sistemas prev2/prev1: "
                    f"{len(systems_prev2)}/{len(systems_prev1)}"
                )

                try:
                    tathu.run_uba_tracking_diagnostics(
                        systems_prev2, systems_prev1, systems_curr
                    )
                except Exception as exc:
                    print(f"[aviso] Diagnóstico UBA falhou (não interrompe forecast): {exc}")

                forecasts = tathu.build_motion_forecasts(
                    systems_prev2,
                    systems_prev1,
                    systems_curr,
                    ts_prev2,
                    ts_prev1,
                    ts_curr,
                    [20, 40, 60],
                )
            else:
                print("[info] Sem duas imagens anteriores; previsão não calculada nesta rodada.")

            tathu.rasterize_forecasts_only(
                forecasts,
                curr_path,
                forecast_tmp,
                codes={20: 2, 40: 3, 60: 4},
                earliest_priority=False,
            )
            tathu.publish_run_outputs(
                ts_str, detection_tmp, forecast_tmp, systems_curr, forecasts
            )

        except Exception as exc:
            falhas.append((ts_str, str(exc)))
            print(f"[ERRO] Falha no timestamp {ts_str}: {exc}")
            for temporary in (detection_tmp, forecast_tmp):
                try:
                    if os.path.exists(temporary):
                        os.remove(temporary)
                except OSError:
                    pass

    print("\nResumo do processamento em lote")
    print(f"TIFFs selecionados: {total}")
    print(f"Rodadas com falha: {len(falhas)}")
    print(f"Saídas: {os.path.abspath(output_dir)}")
    if falhas:
        for timestamp, erro in falhas:
            print(f" - {timestamp}: {erro}")
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Processa TIFFs GOES locais em sequência e gera previsões TATHU."
    )
    parser.add_argument(
        "--input-dir",
        default="./temp_goes_data_teste",
        help="Pasta dos TIFFs baixados (padrão: ./temp_goes_data_teste).",
    )
    parser.add_argument(
        "--output-dir",
        default="./saidas_teste_lote",
        help="Pasta isolada para os produtos (padrão: ./saidas_teste_lote).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Processa apenas os N TIFFs mais recentes; 0 significa todos.",
    )
    args = parser.parse_args()

    if args.limit < 0:
        parser.error("--limit não pode ser negativo.")

    arquivos = listar_tiffs(args.input_dir)
    if len(arquivos) < 3:
        raise SystemExit(
            f"Foram encontrados apenas {len(arquivos)} TIFF(s) em {args.input_dir}. "
            "São necessários pelo menos três."
        )

    if args.limit:
        arquivos = arquivos[-args.limit:]

    if len(arquivos) < 3:
        raise SystemExit("A seleção precisa conter pelo menos três TIFFs.")

    print(f"TIFFs encontrados/selecionados: {len(arquivos)}")
    print(f"Primeiro: {arquivos[0][0]:%Y-%m-%d %H:%M}")
    print(f"Último:   {arquivos[-1][0]:%Y-%m-%d %H:%M}")
    print(f"Saída isolada: {os.path.abspath(args.output_dir)}")

    processar_sequencia(arquivos, args.output_dir)


if __name__ == "__main__":
    main()
