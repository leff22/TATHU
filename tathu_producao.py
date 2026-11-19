# imports 
import os
import glob
import re
from datetime import datetime, timedelta
from tathu.satellite import goes16
from tathu.tracking import detectors, descriptors, forecasters, trackers
from tathu.tracking.utils import area2degrees
import requests
from dotenv import load_dotenv

# Carrega variáveis do arquivo .env (quando disponível)
load_dotenv()

# DIRETÓRIOS
# Diretório de entrada
INPUT_DIR = 'https://ftp.monitor.geopixel.com.br/goes_temp/'

# Credenciais do diretório ambiental (definidas no arquivo .env ou no ambiente)
SERVER_USERNAME = os.getenv('SERVER_USERNAME')
SERVER_PASSWORD = os.getenv('SERVER_PASSWORD')

#Diretório de saída
TEMP_DIR = './temp_goes_data'       #Alterar para o diretório ambiental
OUTPUTS_SHP_DIR = './saidas_detecao_script'     #Alterar para o diretório ambiental

#CONSTANTES
# Parâmetros de detecção P/ CÉLULAS CONVECTIVAS INTENSAS
THRESHOLD_K = 228            # limiar em K p/ topos frios (~ -45 °C)
MINAREA_KM2 = 3000           # área mínima do sistema (km²)
SUL_BRASIL_EXTENT = (-62.0, -34.0, -40.0, -18.0)  # extent padrão (lon/lat)
RESOLUTION_KM = 2.0          # resolução da grade quando usa extent (km)
SMOOTH_ITERATIONS = 8        # iterações de suavização de contorno (Chaikin)
MAX_VEL_MPS = 45             # velocidade máxima de deslocamento (m/s)
MATCH_MIN_OVERLAP = 0.1      # sobreposição mínima p/ casar sistemas
MATCH_MAX_ANGLE_DIFF_DEG = 90  # tolerância de ângulo entre passos (°)
VEL_DECAY = {20: 0.85, 40: 0.75, 60: 0.65}  # redução da velocidade por horizonte
MAX_DISP_KM = {20: 30, 40: 60, 60: 90}      # teto de deslocamento por horizonte (km)

# --- Listagem e download remotos dos TIFFs ---
def _list_remote_tiffs(base_url: str, username: str, password: str):
    resp = requests.get(base_url, auth=(username, password), timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f'Falha ao acessar {base_url}: {resp.status_code}')
    html = resp.text

    # 1) Captura nomes S10635346_YYYYMMDDHHMM.tif em qualquer lugar do HTML
    names = re.findall(r'S10635346_(\d{12})\.tif', html, flags=re.IGNORECASE)
    items = []
    for ts_str in set(names):
        fname = f'S10635346_{ts_str}.tif'
        url = base_url.rstrip('/') + '/' + fname
        ts = datetime.strptime(ts_str, '%Y%m%d%H%M')
        items.append((url, ts))

    # 2) Fallback: também varre atributos href com .tif
    hrefs = re.findall(r'href=[\'"]([^\'"]+\.tif)[\'"]', html, flags=re.IGNORECASE)
    for href in hrefs:
        url = href if href.lower().startswith('http') else base_url.rstrip('/') + '/' + href.lstrip('/')
        fname = os.path.basename(url)
        m = re.match(r'^S10635346_(\d{12})\.tif$', fname, flags=re.IGNORECASE)
        if m:
            ts = datetime.strptime(m.group(1), '%Y%m%d%H%M')
            items.append((url, ts))

    # Remove duplicados e ordena por timestamp
    unique = {}
    for url, ts in items:
        unique[url] = ts
    sorted_items = sorted([(u, t) for u, t in unique.items()], key=lambda x: x[1])
    return sorted_items

def find_last_three_tiff_remote(base_url: str, username: str, password: str):
    items = _list_remote_tiffs(base_url, username, password)
    if len(items) < 3:
        raise RuntimeError('Menos de três arquivos TIFF encontrados no diretório remoto.')
    return items[-3][0], items[-2][0], items[-1][0]

def download_file(url: str, dst_path: str, username: str, password: str):
    with requests.get(url, auth=(username, password), stream=True, timeout=60) as r:
        r.raise_for_status()
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with open(dst_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return dst_path

def main():
    if not SERVER_USERNAME or not SERVER_PASSWORD:
        raise RuntimeError(
            'Credenciais ausentes. Configure SERVER_USERNAME e SERVER_PASSWORD no arquivo .env.'
        )

    # Baixa últimos 3 TIFFs do servidor (tempo real)
    try:
        prev2_url, prev1_url, curr_url = find_last_three_tiff_remote(INPUT_DIR, SERVER_USERNAME, SERVER_PASSWORD)
        os.makedirs(TEMP_DIR, exist_ok=True)
        prev2_local = os.path.join(TEMP_DIR, os.path.basename(prev2_url))
        prev1_local = os.path.join(TEMP_DIR, os.path.basename(prev1_url))
        curr_local = os.path.join(TEMP_DIR, os.path.basename(curr_url))
        download_file(prev2_url, prev2_local, SERVER_USERNAME, SERVER_PASSWORD)
        download_file(prev1_url, prev1_local, SERVER_USERNAME, SERVER_PASSWORD)
        download_file(curr_url, curr_local, SERVER_USERNAME, SERVER_PASSWORD)
        prev2_path, prev1_path, curr_path = prev2_local, prev1_local, curr_local
        info(f"Baixado tempo real: {os.path.basename(prev2_local)}, {os.path.basename(prev1_local)}, {os.path.basename(curr_local)}")

        # Exemplo fixo para testes (07/11) — manter comentado:
        # prev_url, curr_url = find_last_two_tiff_remote_by_date(
        #     INPUT_DIR, SERVER_USERNAME, SERVER_PASSWORD, '20251107'
        # )
        # prev_local = os.path.join(TEMP_DIR, os.path.basename(prev_url))
        # curr_local = os.path.join(TEMP_DIR, os.path.basename(curr_url))
        # download_file(prev_url, prev_local, SERVER_USERNAME, SERVER_PASSWORD)
        # download_file(curr_url, curr_local, SERVER_USERNAME, SERVER_PASSWORD)
        # curr_path = curr_local

    except Exception as ex:
        warn(f"Falha em tempo real, usando local: {ex}")
        curr_path = None

    # Seleciona o último TIFF local (fallback)
    local_dir = TEMP_DIR if os.path.isdir(TEMP_DIR) else os.path.join(os.getcwd(), 'temp_goes_data')
    tiffs = glob.glob(os.path.join(local_dir, 'S10635346_*.tif'))
    if not tiffs and not curr_path:
        error(f"Nenhum TIFF encontrado em: {local_dir}")
        return

    def parse_ts(p):
        m = re.search(r'_(\d{12})\.tif$', os.path.basename(p))
        return datetime.strptime(m.group(1), '%Y%m%d%H%M') if m else datetime.fromtimestamp(os.path.getmtime(p))

    tiffs = sorted(tiffs, key=parse_ts)
    if curr_path is None:
        curr_path = tiffs[-1]
    info(f"Usando imagem: {os.path.basename(curr_path)}")

    # Detecção t0
    extent_curr, res_km_curr = get_tiff_extent_and_resolution_km(curr_path)
    grid_det = goes16.rect2grid(curr_path, extent=extent_curr, resolution=res_km_curr, autoscale=False)
    minarea_deg2 = area2degrees(MINAREA_KM2)
    threshold_c = THRESHOLD_K-273.15
    detector = detectors.LessThan(threshold_c, minarea_deg2)
    systems_curr = detector.detect(grid_det)

    mfn = re.search(r'_(\d{12})\.tif$', os.path.basename(curr_path))
    ts = datetime.strptime(mfn.group(1), '%Y%m%d%H%M') if mfn else None
    for s in systems_curr:
        s.timestamp = ts

    descriptors.StatisticalDescriptor().describe(grid_det, systems_curr)
    info(f"Sistemas detectados (curr): {len(systems_curr)}")

    # Export shapefile t0
    os.makedirs(OUTPUTS_SHP_DIR, exist_ok=True)
    m_ts = re.search(r'_(\d{12})', os.path.basename(curr_path))
    ts_str = m_ts.group(1) if m_ts else os.path.splitext(os.path.basename(curr_path))[0]
    if systems_curr:
        try:
            out_tif_det = os.path.join(OUTPUTS_SHP_DIR, f"{ts_str}.tif")
            rasterize_systems_to_tif(systems_curr, curr_path, out_tif_det)
            info(f"GeoTIFF detecção gravado: {out_tif_det}")
        except Exception as ex:
            warn(f"Falha ao rasterizar detecção: {ex}")
    else:
        warn("Nenhum sistema detectado; GeoTIFF não será criado.")

    # Forecast usando os últimos 3 arquivos
    # Definição dos caminhos prev2, prev1 e curr
    local_three = sorted(glob.glob(os.path.join(TEMP_DIR, '*.tif')), key=lambda p: datetime.strptime(re.search(r'_(\d{12})', os.path.basename(p)).group(1), '%Y%m%d%H%M') if re.search(r'_(\d{12})', os.path.basename(p)) else datetime.fromtimestamp(os.path.getmtime(p)))
    prev2_path = local_three[-3] if len(local_three) >= 3 else None
    prev1_path = local_three[-2] if len(local_three) >= 2 else None
    curr_path = local_three[-1] if len(local_three) >= 1 else curr_path

    if not (prev1_path and curr_path):
        warn("Menos de dois TIFFs disponíveis para previsão de deslocamento.")
    else:
        # Detecta prev2 e prev1 separadamente
        minarea_deg2 = area2degrees(MINAREA_KM2)
        systems_prev2 = []
        ts_prev2 = None
        if prev2_path:
            grid_prev2 = goes16.rect2grid(prev2_path, extent=extent_curr, resolution=res_km_curr, autoscale=False)
            det_prev2 = detectors.LessThan(threshold_c, minarea_deg2)
            systems_prev2 = det_prev2.detect(grid_prev2)
            descriptors.StatisticalDescriptor().describe(grid_prev2, systems_prev2)
            m_prev2 = re.search(r'_(\d{12})\.tif$', os.path.basename(prev2_path))
            ts_prev2 = datetime.strptime(m_prev2.group(1), '%Y%m%d%H%M') if m_prev2 else None
            for s in systems_prev2:
                s.timestamp = ts_prev2
            info(f"Sistemas detectados (prev2 {os.path.basename(prev2_path)}): {len(systems_prev2)}")

        grid_prev1 = goes16.rect2grid(prev1_path, extent=extent_curr, resolution=res_km_curr, autoscale=False)
        det_prev1 = detectors.LessThan(threshold_c, minarea_deg2)
        systems_prev1 = det_prev1.detect(grid_prev1)
        descriptors.StatisticalDescriptor().describe(grid_prev1, systems_prev1)
        m_prev1 = re.search(r'_(\d{12})\.tif$', os.path.basename(prev1_path))
        ts_prev1 = datetime.strptime(m_prev1.group(1), '%Y%m%d%H%M') if m_prev1 else None
        for s in systems_prev1:
            s.timestamp = ts_prev1
        info(f"Sistemas detectados (prev1 {os.path.basename(prev1_path)}): {len(systems_prev1)}")

        # Construção de previsão por deslocamento usando prev2->prev1->curr
        intervals = [20, 40, 60]
        m_ts = re.search(r'_(\d{12})', os.path.basename(curr_path))
        ts_str = m_ts.group(1) if m_ts else os.path.splitext(os.path.basename(curr_path))[0]
        m_curr = re.search(r'_(\d{12})\.tif$', os.path.basename(curr_path))
        ts_curr = datetime.strptime(m_curr.group(1), '%Y%m%d%H%M') if m_curr else None

        forecasts_motion = build_motion_forecasts(systems_prev2, systems_prev1, systems_curr, ts_prev2, ts_prev1, ts_curr, intervals)

        try:
            out_comb = os.path.join(OUTPUTS_SHP_DIR, f"{ts_str}_forecast.tif")
            rasterize_forecasts_combined(systems_curr, {20: forecasts_motion.get(20, []), 40: forecasts_motion.get(40, []), 60: forecasts_motion.get(60, [])}, curr_path, out_comb, codes={'det': 1, 20: 2, 40: 3, 60: 4}, earliest_priority=False)
            info(f"GeoTIFF forecast deslocamento (3 imagens) gravado: {out_comb}")
        except Exception as ex:
            warn(f"Falha ao rasterizar forecast deslocamento: {ex}")

    # Merge (sem atributos de cor/lead); overlay opcional com cor única
    info("Merge desativado: previsão gravada em único GeoTIFF por timestamp.")

    return

def smooth_polygon_chaikin(geom, iterations=5):
    from osgeo import ogr

    def _ring_to_points(ring):
        pts = []
        for i in range(ring.GetPointCount()):
            x, y, _ = ring.GetPoint(i)
            pts.append((x, y))
        # garante fechamento
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    def _chaikin(points, iters):
        if not points or len(points) < 3:
            return points
        pts = points[:]
        for _ in range(iters):
            new_pts = []
            for i in range(len(pts) - 1):
                x0, y0 = pts[i]
                x1, y1 = pts[i + 1]
                qx = 0.75 * x0 + 0.25 * x1
                qy = 0.75 * y0 + 0.25 * y1
                rx = 0.25 * x0 + 0.75 * x1
                ry = 0.25 * y0 + 0.75 * y1
                new_pts.append((qx, qy))
                new_pts.append((rx, ry))
            # fecha
            if new_pts:
                new_pts.append(new_pts[0])
            pts = new_pts
        return pts

    def _points_to_ring(points):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in points:
            ring.AddPoint_2D(x, y)
        return ring

    gtype = geom.GetGeometryType()
    if gtype == ogr.wkbPolygon:
        # Exterior
        ext_ring = geom.GetGeometryRef(0)
        ext_pts = _ring_to_points(ext_ring)
        ext_sm = _chaikin(ext_pts, iterations)
        ext_ring_sm = _points_to_ring(ext_sm)

        poly_sm = ogr.Geometry(ogr.wkbPolygon)
        poly_sm.AddGeometry(ext_ring_sm)

        # Interiores (buracos)
        for r_i in range(1, geom.GetGeometryCount()):
            in_ring = geom.GetGeometryRef(r_i)
            in_pts = _ring_to_points(in_ring)
            in_sm = _chaikin(in_pts, iterations)
            in_ring_sm = _points_to_ring(in_sm)
            poly_sm.AddGeometry(in_ring_sm)

        return poly_sm

    elif gtype == ogr.wkbMultiPolygon:
        mp = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(i)
            poly_sm = smooth_polygon_chaikin(poly, iterations)
            if poly_sm:
                mp.AddGeometry(poly_sm)
        return mp

    # Tipos não polygon: retorna clone
    return geom.Clone()

def _centroid_xy(geom):
    c = geom.Centroid()
    return c.GetX(), c.GetY()

def _translate_geom_deg(geom, dx, dy):
    from osgeo import ogr
    gtype = geom.GetGeometryType()
    def _ring_to_points(ring):
        pts = []
        for i in range(ring.GetPointCount()):
            x, y, _ = ring.GetPoint(i)
            pts.append((x + dx, y + dy))
        return pts
    def _points_to_ring(points):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in points:
            ring.AddPoint_2D(x, y)
        return ring
    if gtype == ogr.wkbPolygon:
        ext_ring = geom.GetGeometryRef(0)
        ext_pts = _ring_to_points(ext_ring)
        poly_tr = ogr.Geometry(ogr.wkbPolygon)
        poly_tr.AddGeometry(_points_to_ring(ext_pts))
        for r_i in range(1, geom.GetGeometryCount()):
            in_ring = geom.GetGeometryRef(r_i)
            in_pts = _ring_to_points(in_ring)
            poly_tr.AddGeometry(_points_to_ring(in_pts))
        return poly_tr
    elif gtype == ogr.wkbMultiPolygon:
        mp = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(i)
            mp.AddGeometry(_translate_geom_deg(poly, dx, dy))
        return mp
    return geom.Clone()

def _best_match(target, candidates, min_overlap_ratio=0.1):
    best = None
    best_score = 0.0
    ta = target.geom.GetArea() if getattr(target, 'geom', None) else 0.0
    for cand in candidates:
        if not getattr(cand, 'geom', None):
            continue
        inter = target.geom.Intersection(cand.geom)
        ia = inter.GetArea() if inter else 0.0
        ca = cand.geom.GetArea()
        denom = max(1e-9, min(ta, ca))
        score = ia / denom
        if score > best_score:
            best_score = score
            best = cand
    return best if best_score >= min_overlap_ratio else None

class _SysWrap:
    def __init__(self, geom, timestamp=None, event=""):
        self.geom = geom
        self.timestamp = timestamp
        self.event = event

def build_motion_forecasts(systems_prev2, systems_prev1, systems_curr, ts_prev2, ts_prev1, ts_curr, intervals):
    fc = {t: [] for t in intervals}
    dt10 = ((ts_curr - ts_prev1).total_seconds() / 60.0) if (ts_curr and ts_prev1) else 0.0
    dt01 = ((ts_prev1 - ts_prev2).total_seconds() / 60.0) if (ts_prev1 and ts_prev2) else 0.0
    for cur in systems_curr:
        if not getattr(cur, 'geom', None):
            continue
        from osgeo import osr
        src = osr.SpatialReference(); src.ImportFromEPSG(4326)
        dst = osr.SpatialReference(); dst.ImportFromEPSG(3857)
        to = osr.CoordinateTransformation(src, dst)
        def _centroid_xy_m(g):
            cg = g.Centroid(); cg.Transform(to); return cg.GetX(), cg.GetY()
        xc0, yc0 = _centroid_xy_m(cur.geom)
        allowed_m = MAX_VEL_MPS * 60.0 * dt10 if dt10 > 0 else float('inf')
        cand_scored = []
        for cand in systems_prev1:
            if not getattr(cand, 'geom', None):
                continue
            inter = cur.geom.Intersection(cand.geom)
            ia = inter.GetArea() if inter else 0.0
            ta = cur.geom.GetArea(); ca = cand.geom.GetArea()
            denom = max(1e-9, min(ta, ca))
            overlap = ia / denom
            x1m, y1m = _centroid_xy_m(cand.geom)
            import math
            dist_m = math.hypot(xc0 - x1m, yc0 - y1m)
            if overlap < MATCH_MIN_OVERLAP or dist_m > allowed_m:
                continue
            score = overlap * (1.0 - (dist_m / (allowed_m + 1e-9)))
            cand_scored.append((score, cand, x1m, y1m))
        cand_scored.sort(key=lambda x: x[0], reverse=True)
        p1 = cand_scored[0][1] if cand_scored else None
        if not p1 or not getattr(p1, 'geom', None) or dt10 <= 0:
            continue
        xc, yc = xc0, yc0
        x1, y1 = _centroid_xy_m(p1.geom)
        vx1 = (xc - x1) / dt10
        vy1 = (yc - y1) / dt10
        import math
        spd_mpm = math.hypot(vx1, vy1)
        max_v_mpm = MAX_VEL_MPS * 60.0
        if spd_mpm > max_v_mpm and max_v_mpm > 0:
            sv = max_v_mpm / spd_mpm
            vx1 *= sv
            vy1 *= sv
        bearing10 = _bearing_deg_m(x1, y1, xc, yc)
        # Usa prev2 para suavizar a velocidade se disponível
        if systems_prev2 and p1:
            p2 = _best_match(p1, systems_prev2, min_overlap_ratio=MATCH_MIN_OVERLAP)
            if p2 and getattr(p2, 'geom', None) and dt01 > 0:
                x2, y2 = _centroid_xy_m(p2.geom)
                vx0 = (x1 - x2) / dt01
                vy0 = (y1 - y2) / dt01
                bearing01 = _bearing_deg_m(x2, y2, x1, y1)
                angdiff = _angle_diff_deg(bearing01, bearing10)
                w = 0.3 if angdiff <= MATCH_MAX_ANGLE_DIFF_DEG else 0.1
                vx = (1.0 - w) * vx1 + w * vx0
                vy = (1.0 - w) * vy0 + w * vy0
                spd_mpm = math.hypot(vx, vy)
                if spd_mpm > max_v_mpm and max_v_mpm > 0:
                    sv = max_v_mpm / spd_mpm
                    vx *= sv
                    vy *= sv
            else:
                vx, vy = vx1, vy1
        else:
            vx, vy = vx1, vy1
        for t in intervals:
            dx_m = vx * t
            dy_m = vy * t
            decay = VEL_DECAY.get(t, 1.0)
            dx_m *= decay
            dy_m *= decay
            disp = math.hypot(dx_m, dy_m)
            max_m = MAX_DISP_KM.get(t, float('inf')) * 1000.0
            if disp > max_m and max_m < float('inf'):
                s = max_m / disp
                dx_m *= s
                dy_m *= s
            moved = _translate_geom_m(cur.geom, dx_m, dy_m)
            fc[t].append(_SysWrap(moved, timestamp=(ts_curr + timedelta(minutes=t)) if ts_curr else None, event=f"FORECAST+{t}min"))
    return fc

def _translate_geom_m(geom, dx_m, dy_m):
    from osgeo import ogr, osr
    src = osr.SpatialReference(); src.ImportFromEPSG(4326)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(3857)
    to = osr.CoordinateTransformation(src, dst)
    back = osr.CoordinateTransformation(dst, src)
    g = geom.Clone(); g.Transform(to)
    gtype = g.GetGeometryType()
    def _ring_to_points_m(ring):
        pts = []
        for i in range(ring.GetPointCount()):
            x, y, _ = ring.GetPoint(i)
            pts.append((x + dx_m, y + dy_m))
        return pts
    def _points_to_ring_m(points):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in points:
            ring.AddPoint_2D(x, y)
        return ring
    if gtype == ogr.wkbPolygon:
        ext_ring = g.GetGeometryRef(0)
        ext_pts = _ring_to_points_m(ext_ring)
        poly_tr = ogr.Geometry(ogr.wkbPolygon)
        poly_tr.AddGeometry(_points_to_ring_m(ext_pts))
        for r_i in range(1, g.GetGeometryCount()):
            in_ring = g.GetGeometryRef(r_i)
            in_pts = _ring_to_points_m(in_ring)
            poly_tr.AddGeometry(_points_to_ring_m(in_pts))
        poly_tr.Transform(back)
        return poly_tr
    elif gtype == ogr.wkbMultiPolygon:
        mp = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(g.GetGeometryCount()):
            poly = g.GetGeometryRef(i)
            # translate each poly in 3857 then add
            ext_ring = poly.GetGeometryRef(0)
            ext_pts = _ring_to_points_m(ext_ring)
            poly_tr = ogr.Geometry(ogr.wkbPolygon)
            poly_tr.AddGeometry(_points_to_ring_m(ext_pts))
            for r_i in range(1, poly.GetGeometryCount()):
                in_ring = poly.GetGeometryRef(r_i)
                in_pts = _ring_to_points_m(in_ring)
                poly_tr.AddGeometry(_points_to_ring_m(in_pts))
            mp.AddGeometry(poly_tr)
        mp.Transform(back)
        return mp
    g.Transform(back)
    return g

def _bearing_deg_m(x0, y0, x1, y1):
    import math
    ang = math.degrees(math.atan2(x1 - x0, y1 - y0))
    return (ang + 360.0) % 360.0

def _angle_diff_deg(a, b):
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d

def export_shapefile_clean(systems, output_path):
    import os
    from osgeo import ogr, osr

    if not systems:
        warn(f"Nenhum sistema para salvar em {output_path}.")
        return

    # separa diretório e nome-base
    ds_dir = os.path.dirname(output_path) or '.'
    base_name = os.path.splitext(os.path.basename(output_path))[0]

    # garante diretório
    os.makedirs(ds_dir, exist_ok=True)

    # remove componentes antigos se existirem
    for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
        p = os.path.join(ds_dir, base_name + ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    drv = ogr.GetDriverByName('ESRI Shapefile')
    shp_fullpath = os.path.join(ds_dir, base_name + ".shp")
    ds = drv.CreateDataSource(shp_fullpath)
    if ds is None:
        error(f"Não foi possível criar DataSource (shp): {shp_fullpath}")
        return

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)  # WGS84
    layer = ds.CreateLayer(base_name, srs, geom_type=ogr.wkbPolygon)

    # campos mantidos
    f_ts = ogr.FieldDefn("timestamp", ogr.OFTString)
    f_ts.SetWidth(32)
    layer.CreateField(f_ts)

    f_event = ogr.FieldDefn("event", ogr.OFTString)
    f_event.SetWidth(64)
    layer.CreateField(f_event)

    written = 0
    for s in systems:
        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetField("timestamp", str(getattr(s, 'timestamp', '') or ''))
        feat.SetField("event", str(getattr(s, 'event', '') or ''))

        if getattr(s, 'geom', None):
            try:
                geom_sm = smooth_polygon_chaikin(s.geom, iterations=SMOOTH_ITERATIONS)
                feat.SetGeometry(geom_sm if geom_sm else s.geom)
            except Exception as ex:
                warn(f"Falha SetGeometry: {ex}")
        layer.CreateFeature(feat)
        feat = None
        written += 1

    layer.SyncToDisk()
    ds = None
    info(f"Shapefile limpo gravado ({written} features): {output_path}")
def merge_shapefiles(input_paths, output_path):
    from osgeo import ogr, osr
    import os

    valid_inputs = [p for p in input_paths if p and os.path.exists(p)]
    if not valid_inputs:
        warn(f"Nenhum shapefile válido para mesclar: {input_paths}")
        return

    ds_dir = os.path.dirname(output_path) or '.'
    base_name = os.path.splitext(os.path.basename(output_path))[0]
    os.makedirs(ds_dir, exist_ok=True)

    # Remove componentes antigos (se existirem)
    for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
        p = os.path.join(ds_dir, base_name + ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    drv = ogr.GetDriverByName('ESRI Shapefile')
    shp_fullpath = os.path.join(ds_dir, base_name + ".shp")
    ds_out = drv.CreateDataSource(shp_fullpath)
    if ds_out is None:
        error(f"Não foi possível criar shapefile de saída: {shp_fullpath}")
        return

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)  # WGS84
    layer_out = ds_out.CreateLayer(base_name, srs, geom_type=ogr.wkbPolygon)

    # Campos compatíveis com export_shapefile_clean (sem lead/color)
    f_ts = ogr.FieldDefn("timestamp", ogr.OFTString)
    f_ts.SetWidth(32)
    layer_out.CreateField(f_ts)

    f_event = ogr.FieldDefn("event", ogr.OFTString)
    f_event.SetWidth(64)
    layer_out.CreateField(f_event)

    written = 0
    for path in valid_inputs:
        try:
            ds_in = ogr.Open(path, 0)  # leitura
            if ds_in is None:
                warn(f"Aviso: não foi possível abrir '{path}' para leitura.")
                continue
            layer_in = ds_in.GetLayer(0)
            count_in = layer_in.GetFeatureCount()

            for feat_in in layer_in:
                feat_out = ogr.Feature(layer_out.GetLayerDefn())
                feat_out.SetField("timestamp", feat_in.GetField("timestamp"))
                feat_out.SetField("event", feat_in.GetField("event"))
                geom_in = feat_in.GetGeometryRef()
                if geom_in:
                    feat_out.SetGeometry(geom_in.Clone())
                layer_out.CreateFeature(feat_out)
                feat_out = None
                written += 1

            info(f"Adicionados {count_in} de '{os.path.basename(path)}'")
            ds_in = None
        except Exception as ex:
            warn(f"Falha ao mesclar '{path}': {ex}")

    layer_out.SyncToDisk()
    ds_out = None
    info(f"Shapefile mesclado gravado ({written} features): {output_path}")
def rasterize_systems_to_tif(systems, reference_tiff_path, out_tif_path):
    from osgeo import gdal, ogr, osr
    if not systems:
        return
    ref = gdal.Open(reference_tiff_path)
    drv = gdal.GetDriverByName('GTiff')
    xsize = ref.RasterXSize; ysize = ref.RasterYSize
    gt = ref.GetGeoTransform(); proj = ref.GetProjection()
    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_Byte, options=['TILED=YES','COMPRESS=LZW'])
    ds_out.SetGeoTransform(gt); ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1); band.SetNoDataValue(0); band.Fill(0)
    mem_drv = ogr.GetDriverByName('Memory'); ds_mem = mem_drv.CreateDataSource('mem')
    srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
    layer = ds_mem.CreateLayer('systems', srs, geom_type=ogr.wkbPolygon)
    for s in systems:
        if getattr(s,'geom',None):
            f = ogr.Feature(layer.GetLayerDefn())
            try:
                geom_sm = smooth_polygon_chaikin(s.geom, iterations=SMOOTH_ITERATIONS)
                f.SetGeometry(geom_sm if geom_sm else s.geom)
            except Exception:
                f.SetGeometry(s.geom)
            layer.CreateFeature(f); f=None
    gdal.RasterizeLayer(ds_out,[1],layer,burn_values=[1],options=['ALL_TOUCHED=TRUE'])
    ds_out=None; layer=None

def rasterize_forecasts_combined(systems_det, forecasts_dict, reference_tiff_path, out_tif_path, codes=None, earliest_priority=False):
    from osgeo import gdal, ogr, osr
    if codes is None:
        codes = {'det':1,20:2,40:3,60:4}
    ref = gdal.Open(reference_tiff_path)
    drv = gdal.GetDriverByName('GTiff')
    xsize = ref.RasterXSize; ysize = ref.RasterYSize
    gt = ref.GetGeoTransform(); proj = ref.GetProjection()
    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_UInt16, options=['TILED=YES','COMPRESS=LZW'])
    ds_out.SetGeoTransform(gt); ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1); band.SetNoDataValue(0); band.Fill(0)
    mem_drv = ogr.GetDriverByName('Memory')
    srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
    order = ['det',20,40,60]; order = list(reversed(order)) if earliest_priority else order
    def burn(systems,value):
        if not systems:
            return
        ds_mem = mem_drv.CreateDataSource('mem'); layer = ds_mem.CreateLayer('layer', srs, geom_type=ogr.wkbPolygon)
        for s in systems:
            if getattr(s,'geom',None):
                f = ogr.Feature(layer.GetLayerDefn())
                try:
                    geom_sm = smooth_polygon_chaikin(s.geom, iterations=SMOOTH_ITERATIONS)
                    f.SetGeometry(geom_sm if geom_sm else s.geom)
                except Exception:
                    f.SetGeometry(s.geom)
                layer.CreateFeature(f); f=None
        gdal.RasterizeLayer(ds_out,[1],layer,burn_values=[value],options=['ALL_TOUCHED=TRUE'])
        layer=None
    burn(systems_det,codes['det'])
    for t in [20,40,60]:
        burn(forecasts_dict.get(t,[]), codes.get(t,0))
    ds_out=None

def get_tiff_extent_and_resolution_km(tiff_path):
    from osgeo import gdal
    import math

    ds = gdal.Open(tiff_path)
    if ds is None:
        raise RuntimeError(f"Não foi possível abrir TIFF: {tiff_path}")
    gt = ds.GetGeoTransform()
    width = ds.RasterXSize
    height = ds.RasterYSize

    minx = gt[0]
    maxy = gt[3]
    px_w = gt[1]     # graus/pixel em lon
    px_h = gt[5]     # graus/pixel em lat (negativo)

    maxx = minx + px_w * width
    miny = maxy + px_h * height

    center_lat = (maxy + miny) / 2.0
    km_per_deg = 111.32
    res_lon_km = abs(px_w) * km_per_deg * math.cos(math.radians(center_lat))
    res_lat_km = abs(px_h) * km_per_deg
    res_km = max(res_lon_km, res_lat_km)

    extent = (minx, miny, maxx, maxy)
    return extent, res_km
def log(level, msg):
    print(f"[{level}] {msg}")

def info(msg):
    log("info", msg)

def warn(msg):
    log("warn", msg)

def error(msg):
    log("error", msg)


if __name__ == "__main__":
    main()
