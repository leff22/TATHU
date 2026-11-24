#!/usr/bin/env python3
"""Baixa os TIFFs GOES mais recentes do servidor do TATHU, sem processá-los."""

import argparse
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

BASE_URL = "https://ftp.monitor.geopixel.com.br/goes_temp/"
FILE_PATTERN = re.compile(r"^S10635346_(\d{12})\.tif$", re.IGNORECASE)


def listar_tiffs(session):
    response = session.get(BASE_URL, timeout=30)
    response.raise_for_status()

    encontrados = {}
    # Extrai os nomes tanto do texto da página quanto dos links href.
    for timestamp in re.findall(r"S10635346_(\d{12})\.tif", response.text, re.IGNORECASE):
        nome = f"S10635346_{timestamp}.tif"
        encontrados[nome] = datetime.strptime(timestamp, "%Y%m%d%H%M")

    for href in re.findall(r'href=[\'"]([^\'"]+\.tif)[\'"]', response.text, re.IGNORECASE):
        url = urljoin(BASE_URL, href)
        nome = Path(urlparse(url).path).name
        match = FILE_PATTERN.match(nome)
        if match:
            encontrados[nome] = datetime.strptime(match.group(1), "%Y%m%d%H%M")

    if not encontrados:
        raise RuntimeError(
            "Nenhum arquivo S10635346_YYYYMMDDHHMM.tif foi encontrado na listagem remota."
        )

    return sorted(encontrados.items(), key=lambda item: item[1])


def baixar(session, nome, destino, sobrescrever=False):
    arquivo = destino / nome
    if arquivo.exists() and arquivo.stat().st_size > 0 and not sobrescrever:
        print(f"[já existe] {nome} ({arquivo.stat().st_size:,} bytes)")
        return arquivo

    url = urljoin(BASE_URL, nome)
    temporario = arquivo.with_suffix(arquivo.suffix + ".part")

    try:
        with session.get(url, stream=True, timeout=(30, 180)) as response:
            response.raise_for_status()
            with temporario.open("wb") as saida:
                for bloco in response.iter_content(chunk_size=1024 * 1024):
                    if bloco:
                        saida.write(bloco)

        if not temporario.exists() or temporario.stat().st_size == 0:
            raise RuntimeError(f"Download vazio: {nome}")

        temporario.replace(arquivo)
        print(f"[baixado] {nome} ({arquivo.stat().st_size:,} bytes)")
        return arquivo
    except Exception:
        temporario.unlink(missing_ok=True)
        raise


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Baixa os N TIFFs GOES mais recentes, sem executar o TATHU."
    )
    parser.add_argument("--limit", type=int, default=10, help="Quantidade de TIFFs (padrão: 10).")
    parser.add_argument(
        "--output-dir",
        default="./temp_goes_data_teste",
        help="Pasta de destino (padrão: ./temp_goes_data_teste).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Baixa novamente mesmo que o arquivo já exista.",
    )
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit precisa ser maior que zero.")

    username = os.getenv("SERVER_USERNAME")
    password = os.getenv("SERVER_PASSWORD")
    if not username or not password:
        raise SystemExit(
            "Credenciais ausentes. Configure SERVER_USERNAME e SERVER_PASSWORD no .env "
            "ou nas variáveis de ambiente."
        )

    destino = Path(args.output_dir)
    destino.mkdir(parents=True, exist_ok=True)

    with requests.Session() as session:
        session.auth = (username, password)
        arquivos = listar_tiffs(session)
        selecionados = arquivos[-args.limit:]

        print(f"Arquivos encontrados no servidor: {len(arquivos)}")
        print(f"Selecionados para download: {len(selecionados)}")
        print(f"Destino: {destino.resolve()}")

        falhas = []
        for nome, timestamp in selecionados:
            print(f"\n{timestamp:%Y-%m-%d %H:%M} | {nome}")
            try:
                baixar(session, nome, destino, args.overwrite)
            except Exception as exc:
                falhas.append((nome, str(exc)))
                print(f"[ERRO] {nome}: {exc}")

    baixados = sorted(destino.glob("S10635346_*.tif"))
    print("\nResumo")
    print(f"TIFFs presentes na pasta: {len(baixados)}")
    print(f"Falhas nesta execução: {len(falhas)}")
    if falhas:
        for nome, erro in falhas:
            print(f" - {nome}: {erro}")
        raise SystemExit(1)

    print("Download concluído.")


if __name__ == "__main__":
    main()
