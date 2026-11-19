# Script de Teste Histórico RADAR: 28/01/2026 23h-00h UTC
# Versão com OSGEO/TATHU (Revertido conforme solicitado)

import os
import glob
import re
import math
import requests
import numpy as np
from datetime import datetime, timedelta
from osgeo import gdal, ogr, osr

# TATHU imports
from tathu.tracking import detectors, descriptors
from tathu.tracking.utils import area2degrees

# Configurações
INPUT_URL = 'https://ftp.monitor.geopixel.com.br/rainviewer_radar/'
SERVER_USERNAME = 'monitor'
SERVER_PASSWORD = 'geopx!monitor'

TEMP_DIR = './temp_radar_historico'
OUTPUTS_DIR = './saidas_radar_historico'

# Parâmetros
THRESHOLD_DBZ = 35.0      # Limiar de dBZ para detecção de objetos
MINAREA_KM2 = 20.0        # Área mínima (km²)
MAX_VEL_MPS = 30.0 # Velocidade máxima (m/s)
MATCH_MIN_OVERLAP = 0.1 # Overlap mínimo para considerar dois objetos como o mesmo
VEL_DECAY = {20: 0.9, 40: 0.8, 60: 0.7} # Fator de decaimento de velocidade para diferentes intervalos de tempo
MAX_DISP_KM = {20: 40, 40: 80, 60: 120} # Distância máxima de deslocamento (km) para diferentes intervalos de tempo

# Logging
def log(level, msg): print(f"[{level.upper()}] {msg}")
def info(msg): log("info", msg)
def warn(msg): log("warn", msg)
def error(msg): log("error", msg)

# --------------------------------------------------------------------------------------
# Download (Mantido do script anterior)
# --------------------------------------------------------------------------------------
def _list_remote_tiffs(base_url, username, password):
    try:
        resp = requests.get(base_url, auth=(username, password), timeout=30)
        resp.raise_for_status()
        html = resp.text
        matches = re.findall(r'href=[\'"](rainviewer_(\d{8})_(\d{4})\.tif)[\'"]', html)
        items = []
        for filename, date_part, time_part in matches:
            url = base_url.rstrip('/') + '/' + filename
            ts = datetime.strptime(date_part + time_part, '%Y%m%d%H%M')
            items.append((url, ts, filename))
        items.sort(key=lambda x: x[1])
        return items
    except Exception as e:
        error(f"Erro ao listar arquivos: {e}")
        return []

def download_history_interval(start_ts, end_ts):
    items = _list_remote_tiffs(INPUT_URL, SERVER_USERNAME, SERVER_PASSWORD)
    selection = [item for item in items if start_ts <= item[1] <= end_ts]
    
    if not selection:
        warn(f"Nenhum arquivo encontrado entre {start_ts} e {end_ts}.")
        return []
    
    info(f"Encontrados {len(selection)} arquivos no intervalo.")
    os.makedirs(TEMP_DIR, exist_ok=True)
    local_files = []
    
    for url, ts, fname in selection:
        local_path = os.path.join(TEMP_DIR, fname)
        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            info(f"Baixando: {fname}")
            try:
                with requests.get(url, auth=(SERVER_USERNAME, SERVER_PASSWORD), stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(local_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk: f.write(chunk)
            except Exception as ex:
                error(f"Falha ao baixar {fname}: {ex}")
                continue
        if os.path.exists(local_path):
            local_files.append(local_path)
    return local_files

# --------------------------------------------------------------------------------------
# Funções de Tracking (Motion Forecast) - Trazido do tempestades_radar.py
# --------------------------------------------------------------------------------------
class SysWrap:
    def __init__(self, geom, timestamp=None, event=""):
        self.geom = geom
        self.timestamp = timestamp
        self.event = event

def _centroid_xy_m(g, transform_to_meters):
    cg = g.Centroid()
    cg.Transform(transform_to_meters)
    return cg.GetX(), cg.GetY()

def _translate_geom_m(geom, dx_m, dy_m, to_meters, to_src):
    g = geom.Clone()
    g.Transform(to_meters)
    
    # Translação manual nos pontos
    def shift_ring(ring):
        for i in range(ring.GetPointCount()):
            x, y, z = ring.GetPoint(i)
            ring.SetPoint(i, x + dx_m, y + dy_m)
            
    if g.GetGeometryType() == ogr.wkbPolygon:
        shift_ring(g.GetGeometryRef(0)) # Exterior
        for i in range(1, g.GetGeometryCount()):
            shift_ring(g.GetGeometryRef(i))
    elif g.GetGeometryType() == ogr.wkbMultiPolygon:
        for i in range(g.GetGeometryCount()):
            poly = g.GetGeometryRef(i)
            shift_ring(poly.GetGeometryRef(0))
            for j in range(1, poly.GetGeometryCount()):
                shift_ring(poly.GetGeometryRef(j))
                
    g.Transform(to_src)
    return g

def build_motion_forecasts(systems_prev, systems_curr, ts_prev, ts_curr, intervals=[20, 40, 60]):
    if not (systems_prev and systems_curr and ts_prev and ts_curr):
        return {t: [] for t in intervals}
        
    dt_min = (ts_curr - ts_prev).total_seconds() / 60.0
    if dt_min <= 0: return {t: [] for t in intervals}

    # Projeções para cálculo métrico
    src_srs = osr.SpatialReference(); src_srs.ImportFromEPSG(4326) # Assumindo WGS84
    dst_srs = osr.SpatialReference(); dst_srs.ImportFromEPSG(3857) # Web Mercator
    to_meters = osr.CoordinateTransformation(src_srs, dst_srs)
    to_latlon = osr.CoordinateTransformation(dst_srs, src_srs)
    
    forecasts = {t: [] for t in intervals}
    
    for curr in systems_curr:
        if not getattr(curr, 'geom', None): continue
        
        xc, yc = _centroid_xy_m(curr.geom, to_meters)
        allowed_dist_m = MAX_VEL_MPS * 60.0 * dt_min
        
        best_prev = None
        best_score = -1.0
        
        for prev in systems_prev:
            if not getattr(prev, 'geom', None): continue
            xp, yp = _centroid_xy_m(prev.geom, to_meters)
            dist = math.hypot(xc - xp, yc - yp)
            
            if dist > allowed_dist_m: continue
            
            inter = curr.geom.Intersection(prev.geom)
            
            inter_area = 0.0
            if inter and not inter.IsEmpty():
                # Check if it is a surface geometry (Polygon=3, MultiPolygon=6)
                # Or GeometryCollection=7 (might contain surfaces)
                gtype = inter.GetGeometryType()
                if gtype in (ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
                    inter_area = inter.GetArea()

            overlap = inter_area / max(1e-9, min(curr.geom.GetArea(), prev.geom.GetArea()))
            
            if overlap < MATCH_MIN_OVERLAP: continue
            
            score = overlap * (1.0 - dist/(allowed_dist_m + 1.0))
            if score > best_score:
                best_score = score
                best_prev = prev
                
        if best_prev:
            xp, yp = _centroid_xy_m(best_prev.geom, to_meters)
            vx = (xc - xp) / dt_min
            vy = (yc - yp) / dt_min
            
            for t in intervals:
                decay = VEL_DECAY.get(t, 1.0)
                dx = vx * t * decay
                dy = vy * t * decay
                
                disp = math.hypot(dx, dy)
                max_d = MAX_DISP_KM.get(t, 9999) * 1000.0
                if disp > max_d:
                    scale = max_d / disp
                    dx *= scale
                    dy *= scale
                    
                moved_geom = _translate_geom_m(curr.geom, dx, dy, to_meters, to_latlon)
                forecasts[t].append(SysWrap(moved_geom, ts_curr + timedelta(minutes=t), f"FCST+{t}"))
                
    return forecasts

def rasterize_forecasts_combined(systems_det, forecasts_dict, reference_tiff_path, out_tif_path):
    # Codes: det=1, +20=2, +40=3, +60=4
    codes = {'det': 1, 20: 2, 40: 3, 60: 4}
    
    ref = gdal.Open(reference_tiff_path)
    drv = gdal.GetDriverByName('GTiff')
    xsize = ref.RasterXSize; ysize = ref.RasterYSize
    gt = ref.GetGeoTransform(); proj = ref.GetProjection()
    
    # Cria output UInt8 (para valores 0-4)
    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_Byte, options=['TILED=YES','COMPRESS=LZW'])
    ds_out.SetGeoTransform(gt); ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1); band.SetNoDataValue(0); band.Fill(0)
    
    mem_drv = ogr.GetDriverByName('Memory')
    srs = osr.SpatialReference(); srs.ImportFromWkt(proj)
    
    # Ordem de plotagem: detecção por último para ficar "por cima" (conforme tempestades_local.py que usa earliest_priority=False por padrão)
    # Mas no script tempestades_local.py, a ordem padrão é ['det', 20, 40, 60] e ele chama burn nessa ordem.
    # O burn usa MERGE_ALG=REPLACE, então o último sobrescreve o anterior.
    # Se queremos detecção "visível", ela deve ser queimada por ÚLTIMO se ela estiver "acima" das previsões, OU
    # Se queremos ver a previsão MAIS LONGE (60min) "acima" das outras, queimamos ela por último.
    # No script local: order = ['det', 20, 40, 60]. 60 é o último, então 60 fica "em cima" de 40, que fica em cima de 20, que fica em cima da detecção.
    # Isso faz sentido para ver onde VAI estar.
    order = ['det', 20, 40, 60]
    
    def burn(systems, val):
        if not systems: return
        ds_mem = mem_drv.CreateDataSource('mem')
        layer = ds_mem.CreateLayer('layer', srs, geom_type=ogr.wkbPolygon)
        for s in systems:
            if getattr(s, 'geom', None):
                f = ogr.Feature(layer.GetLayerDefn())
                f.SetGeometry(s.geom)
                layer.CreateFeature(f)
        # ALL_TOUCHED=TRUE para garantir que pequenos polígonos sejam rasterizados
        gdal.RasterizeLayer(ds_out, [1], layer, burn_values=[val], options=['ALL_TOUCHED=TRUE'])
    
    for k in order:
        if k == 'det': burn(systems_det, codes['det'])
        else: burn(forecasts_dict.get(k, []), codes.get(k, 0))
        
    ds_out = None

# --------------------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------------------
def main():
    info("Iniciando script de teste histórico RADAR (OSGEO/TATHU)...")
    
    # Intervalo (UTC): 2026-01-28 23:00 - 00:00 (dia 29)
    start_ts = datetime(2026, 1, 28, 22, 45) 
    end_ts = datetime(2026, 1, 29, 0, 15)
    
    tiffs = download_history_interval(start_ts, end_ts)
    if not tiffs: return
    tiffs.sort()
    
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    
    # Process Loop
    prev_systems = None
    prev_ts = None
    
    # Prepara detector
    # minarea em graus quadrados (conversão aproximada para latlon)
    minarea_deg = area2degrees(MINAREA_KM2)
    detector = detectors.GreaterThan(THRESHOLD_DBZ, minarea_deg)
    
    for curr_path in tiffs:
        bn = os.path.basename(curr_path)
        m = re.search(r'(\d{8})_(\d{4})', bn)
        if not m: continue
        ts_curr = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M')
        
        info(f"Processando: {bn} ({ts_curr})")
        
        # Detecção
        ds_curr = gdal.Open(curr_path)
        if not ds_curr:
            error(f"Falha ao abrir {curr_path}")
            continue
            
        systems_curr = detector.detect(ds_curr)
        # Adiciona timestamp
        for s in systems_curr: s.timestamp = ts_curr
            
        info(f"  > Detectados: {len(systems_curr)}")
        
        # Tracking
        forecasts = {}
        if prev_systems and prev_ts:
            forecasts = build_motion_forecasts(prev_systems, systems_curr, prev_ts, ts_curr, [20, 40, 60])
            count = sum(len(v) for v in forecasts.values())
            info(f"  > Previsões: {count}")
            
        # Export only if in target range
        target_start = datetime(2026, 1, 28, 23, 0)
        target_end = datetime(2026, 1, 29, 0, 0)
        
        if target_start <= ts_curr <= target_end:
            out_name = f"radar_hist_{ts_curr.strftime('%Y%m%d%H%M')}.tif"
            out_path = os.path.join(OUTPUTS_DIR, out_name)
            
            try:
                rasterize_forecasts_combined(systems_curr, forecasts, curr_path, out_path)
                info(f"  > Salvo: {out_name}")
            except Exception as e:
                error(f"  > Erro ao salvar: {e}")
            
        # Update prev
        prev_systems = systems_curr
        prev_ts = ts_curr

if __name__ == '__main__':
    main()
