## SCRIPT PARA DETECÇÃO DE TEMPÉSTADES NO SUL DO BRASIL A 
## PARTIR DE IMAGENS DO GOES-19 PARA UMA SERIE DE TEMPOS ESPECIFICADOS

import os
import glob
import re
from datetime import datetime, timedelta
from tathu.satellite import goes16
from tathu.tracking import detectors, descriptors, forecasters, trackers
from tathu.tracking.utils import area2degrees

TEMP_DIR = r"c:\Users\leandro.faria\Projects\tathu\script pronto\goes19-aws\goes19_ciclone\saidas"
OUTPUTS_SHP_DIR = "./saidas_detecao_script"

THRESHOLD_K = 228
MINAREA_KM2 = 3000
SUL_BRASIL_EXTENT = (-62.0, -34.0, -40.0, -18.0)
RESOLUTION_KM = 2.0
SMOOTH_ITERATIONS = 8
SMOOTH_BUFFER_KM = 10.0
MAX_VEL_MPS = 45
MATCH_MIN_OVERLAP = 0.2
MATCH_MAX_ANGLE_DIFF_DEG = 90
VEL_DECAY = {20: 0.85, 40: 0.75, 60: 0.65}
MAX_DISP_KM = {20: 30, 40: 60, 60: 90}

def main():
    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        tiffs = glob.glob(os.path.join(TEMP_DIR, "*.tif"))
        if not tiffs:
            error(f"Nenhum TIFF encontrado em: {TEMP_DIR}")
            return

        def parse_ts(p):
            bn = os.path.basename(p)
            m = re.search(r"(\d{12})", bn)
            return datetime.strptime(m.group(1), "%Y%m%d%H%M") if m else datetime.fromtimestamp(os.path.getmtime(p))

        tiffs = sorted(tiffs, key=parse_ts)
        for i, curr_path in enumerate(tiffs):
            info(f"Usando imagem: {os.path.basename(curr_path)}")

            extent_curr, res_km_curr = get_tiff_extent_and_resolution_km(curr_path)
            grid_det = goes16.rect2grid(curr_path, extent=extent_curr, resolution=res_km_curr, autoscale=False)
            minarea_deg2 = area2degrees(MINAREA_KM2)
            threshold_c = THRESHOLD_K - 273.15
            detector = detectors.LessThan(threshold_c, minarea_deg2)
            systems_curr = detector.detect(grid_det)

            mfn = re.search(r"(\d{12})", os.path.basename(curr_path))
            ts = datetime.strptime(mfn.group(1), "%Y%m%d%H%M") if mfn else None
            for s in systems_curr:
                s.timestamp = ts
                s.event = "DETECTED"

            descriptors.StatisticalDescriptor().describe(grid_det, systems_curr)
            info(f"Sistemas detectados (curr): {len(systems_curr)}")

            os.makedirs(OUTPUTS_SHP_DIR, exist_ok=True)
            m_ts = re.search(r"(\d{12})", os.path.basename(curr_path))
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

            merge_inputs = []

            prev2_path = tiffs[i - 2] if i > 1 else None
            prev_path = tiffs[i - 1] if i > 0 else None
            if not prev_path or not curr_path:
                warn("Não há dois TIFFs disponíveis para forecast (prev/curr).")
            else:
                grid_prev2 = None
                systems_prev2 = []
                ts_prev2 = None
                if prev2_path:
                    grid_prev2 = goes16.rect2grid(prev2_path, extent=extent_curr, resolution=res_km_curr, autoscale=False)
                    det_prev2 = detectors.LessThan(THRESHOLD_K - 273.15, area2degrees(MINAREA_KM2))
                    systems_prev2 = det_prev2.detect(grid_prev2)
                    descriptors.StatisticalDescriptor().describe(grid_prev2, systems_prev2)
                    m_prev2 = re.search(r"(\d{12})", os.path.basename(prev2_path))
                    ts_prev2 = datetime.strptime(m_prev2.group(1), "%Y%m%d%H%M") if m_prev2 else None
                    for s in systems_prev2:
                        s.timestamp = ts_prev2
                grid_prev = goes16.rect2grid(prev_path, extent=extent_curr, resolution=res_km_curr, autoscale=False)
                minarea_deg2 = area2degrees(MINAREA_KM2)
                detector_prev = detectors.LessThan(THRESHOLD_K - 273.15, minarea_deg2)
                systems_prev = detector_prev.detect(grid_prev)
                descriptors.StatisticalDescriptor().describe(grid_prev, systems_prev)
                systems_prev = systems_prev

                m_prev = re.search(r"(\d{12})", os.path.basename(prev_path))
                ts_prev = datetime.strptime(m_prev.group(1), "%Y%m%d%H%M") if m_prev else None
                for s in systems_prev:
                    s.timestamp = ts_prev

                info(f"Sistemas detectados (prev, núcleos frios): {len(systems_prev)}")

                intervals = [20, 40, 60]
                m_curr = re.search(r"(\d{12})", os.path.basename(curr_path))
                ts_curr = datetime.strptime(m_curr.group(1), "%Y%m%d%H%M") if m_curr else None
                forecasts = build_motion_forecasts(systems_prev2, systems_prev, systems_curr, ts_prev2, ts_prev, ts_curr, intervals)

                m_ts = re.search(r"(\d{12})", os.path.basename(curr_path))
                ts_str = m_ts.group(1) if m_ts else os.path.splitext(os.path.basename(curr_path))[0]
                for t in intervals:
                    for s in forecasts.get(t, []):
                        s.event = f"FORECAST+{t}min"
                        s.timestamp = (ts_curr + timedelta(minutes=t)) if ts_curr else ""

                try:
                    out_comb = os.path.join(OUTPUTS_SHP_DIR, f"{ts_str}_forecast.tif")
                    rasterize_forecasts_combined(systems_curr, {20: forecasts.get(20, []), 40: forecasts.get(40, []), 60: forecasts.get(60, [])}, curr_path, out_comb, codes={'det': 1, 20: 2, 40: 3, 60: 4}, earliest_priority=False)
                    info(f"GeoTIFF forecast combinado com detecção gravado: {out_comb}")
                except Exception as ex:
                    warn(f"Falha ao rasterizar forecast combinado: {ex}")

            info("Merge desativado: previsão gravada em único GeoTIFF por timestamp.")
    except Exception as ex:
        error(str(ex))

def smooth_polygon_chaikin(geom, iterations=5):
    from osgeo import ogr

    def _ring_to_points(ring):
        pts = []
        for i in range(ring.GetPointCount()):
            x, y, _ = ring.GetPoint(i)
            pts.append((x, y))
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
        ext_ring = geom.GetGeometryRef(0)
        ext_pts = _ring_to_points(ext_ring)
        ext_sm = _chaikin(ext_pts, iterations)
        ext_ring_sm = _points_to_ring(ext_sm)

        poly_sm = ogr.Geometry(ogr.wkbPolygon)
        poly_sm.AddGeometry(ext_ring_sm)

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
    return geom.Clone()

def buffer_geom_km(geom, km):
    from osgeo import osr
    if km <= 0 or geom is None:
        return geom
    src = osr.SpatialReference(); src.ImportFromEPSG(4326)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(3857)
    to = osr.CoordinateTransformation(src, dst)
    back = osr.CoordinateTransformation(dst, src)
    g = geom.Clone(); g.Transform(to)
    gb = g.Buffer(km * 1000.0)
    gb.Transform(back)
    return gb

def _best_match(target, candidates, min_overlap_ratio=0.1):
    best = None
    best_score = 0.0
    for cand in candidates or []:
        if not getattr(cand, 'geom', None):
            continue
        inter = target.geom.Intersection(cand.geom)
        ia = inter.GetArea() if inter else 0.0
        ta = target.geom.GetArea(); ca = cand.geom.GetArea()
        denom = max(1e-9, min(ta, ca))
        score = ia / denom
        if score > best_score:
            best_score = score
            best = cand
    return best if best_score >= min_overlap_ratio else None

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
        from osgeo import ogr as _ogr
        ring = _ogr.Geometry(_ogr.wkbLinearRing)
        for x, y in points:
            ring.AddPoint_2D(x, y)
        return ring
    if gtype == 3:
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
    elif gtype == 6:
        mp = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(g.GetGeometryCount()):
            poly = g.GetGeometryRef(i)
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

def build_motion_forecasts(systems_prev2, systems_prev1, systems_curr, ts_prev2, ts_prev1, ts_curr, intervals):
    from osgeo import osr
    import math
    fc = {t: [] for t in intervals}
    dt10 = ((ts_curr - ts_prev1).total_seconds() / 60.0) if (ts_curr and ts_prev1) else 0.0
    dt01 = ((ts_prev1 - ts_prev2).total_seconds() / 60.0) if (ts_prev1 and ts_prev2) else 0.0
    src = osr.SpatialReference(); src.ImportFromEPSG(4326)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(3857)
    to = osr.CoordinateTransformation(src, dst)
    def _centroid_xy_m(g):
        cg = g.Centroid(); cg.Transform(to); return cg.GetX(), cg.GetY()
    for cur in systems_curr:
        if not getattr(cur, 'geom', None):
            continue
        xc0, yc0 = _centroid_xy_m(cur.geom)
        allowed_m = MAX_VEL_MPS * 60.0 * dt10 if dt10 > 0 else float('inf')
        cand_scored = []
        for cand in systems_prev1 or []:
            if not getattr(cand, 'geom', None):
                continue
            inter = cur.geom.Intersection(cand.geom)
            ia = inter.GetArea() if inter else 0.0
            ta = cur.geom.GetArea(); ca = cand.geom.GetArea()
            denom = max(1e-9, min(ta, ca))
            overlap = ia / denom
            x1m, y1m = _centroid_xy_m(cand.geom)
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
        spd_mpm = math.hypot(vx1, vy1)
        max_v_mpm = MAX_VEL_MPS * 60.0
        if spd_mpm > max_v_mpm and max_v_mpm > 0:
            sv = max_v_mpm / spd_mpm
            vx1 *= sv
            vy1 *= sv
        bearing10 = _bearing_deg_m(x1, y1, xc, yc)
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
                vy = (1.0 - w) * vy1 + w * vy0
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
            fc[t].append(type('S', (), {'geom': moved, 'timestamp': (ts_curr + timedelta(minutes=t)) if ts_curr else None, 'event': f'FORECAST+{t}min'}))
    return fc

def prepare_geom_for_raster(geom):
    try:
        g = smooth_polygon_chaikin(geom, iterations=SMOOTH_ITERATIONS)
        if SMOOTH_BUFFER_KM > 0:
            g = buffer_geom_km(g, SMOOTH_BUFFER_KM)
        return g
    except Exception:
        return geom

def export_shapefile_clean(systems, output_path):
    from osgeo import ogr, osr

    if not systems:
        warn(f"Nenhum sistema para salvar em {output_path}.")
        return

    ds_dir = os.path.dirname(output_path) or "."
    base_name = os.path.splitext(os.path.basename(output_path))[0]
    os.makedirs(ds_dir, exist_ok=True)
    for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
        p = os.path.join(ds_dir, base_name + ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    drv = ogr.GetDriverByName("ESRI Shapefile")
    ds = drv.CreateDataSource(ds_dir)
    if ds is None:
        error(f"Não foi possível criar DataSource (dir): {ds_dir}")
        return

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = ds.CreateLayer(base_name, srs, geom_type=ogr.wkbPolygon)

    f_ts = ogr.FieldDefn("timestamp", ogr.OFTString)
    f_ts.SetWidth(32)
    layer.CreateField(f_ts)

    f_event = ogr.FieldDefn("event", ogr.OFTString)
    f_event.SetWidth(64)
    layer.CreateField(f_event)

    written = 0
    for s in systems:
        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetField("timestamp", str(getattr(s, "timestamp", "") or ""))
        feat.SetField("event", str(getattr(s, "event", "") or ""))
        if getattr(s, "geom", None):
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

    valid_inputs = [p for p in input_paths if p and os.path.exists(p)]
    if not valid_inputs:
        warn(f"Nenhum shapefile válido para mesclar: {input_paths}")
        return

    ds_dir = os.path.dirname(output_path) or "."
    base_name = os.path.splitext(os.path.basename(output_path))[0]
    os.makedirs(ds_dir, exist_ok=True)
    for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
        p = os.path.join(ds_dir, base_name + ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    drv = ogr.GetDriverByName("ESRI Shapefile")
    ds_out = drv.CreateDataSource(ds_dir)
    if ds_out is None:
        error(f"Não foi possível criar DataSource (dir): {ds_dir}")
        return

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer_out = ds_out.CreateLayer(base_name, srs, geom_type=ogr.wkbPolygon)

    f_ts = ogr.FieldDefn("timestamp", ogr.OFTString)
    f_ts.SetWidth(32)
    layer_out.CreateField(f_ts)

    f_event = ogr.FieldDefn("event", ogr.OFTString)
    f_event.SetWidth(64)
    layer_out.CreateField(f_event)

    written = 0
    for path in valid_inputs:
        try:
            ds_in = ogr.Open(path, 0)
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

def rasterize_forecasts_combined(systems_det, forecasts_dict, reference_tiff_path, out_tif_path, codes=None, earliest_priority=False):
    from osgeo import gdal, ogr, osr
    import os
    if codes is None:
        codes = {'det': 1, 20: 2, 40: 3, 60: 4}
    has_fc = any(forecasts_dict.get(k) for k in [20, 40, 60])
    if not (systems_det or has_fc):
        return
    if not os.path.exists(reference_tiff_path):
        raise RuntimeError(f"Não foi possível localizar TIFF de referência: {reference_tiff_path}")
    ref = gdal.Open(reference_tiff_path)
    if ref is None:
        raise RuntimeError(f"Não foi possível abrir TIFF de referência: {reference_tiff_path}")
    drv = gdal.GetDriverByName("GTiff")
    xsize = ref.RasterXSize
    ysize = ref.RasterYSize
    gt = ref.GetGeoTransform()
    proj = ref.GetProjection()
    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_UInt16, options=["TILED=YES", "COMPRESS=LZW"])
    if ds_out is None:
        raise RuntimeError(f"Não foi possível criar GeoTIFF de saída: {out_tif_path}")
    ds_out.SetGeoTransform(gt)
    ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.Fill(0)
    mem_drv = ogr.GetDriverByName("Memory")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    order = ['det', 20, 40, 60]
    if earliest_priority:
        order = list(reversed(order))
    def burn(systems, value):
        if not systems:
            return
        ds_mem = mem_drv.CreateDataSource("mem")
        layer = ds_mem.CreateLayer("layer", srs, geom_type=ogr.wkbPolygon)
        for s in systems:
            if getattr(s, "geom", None):
                feat = ogr.Feature(layer.GetLayerDefn())
                try:
                    geom_sm = prepare_geom_for_raster(s.geom)
                    feat.SetGeometry(geom_sm if geom_sm else s.geom)
                except Exception:
                    feat.SetGeometry(s.geom)
                layer.CreateFeature(feat)
                feat = None
        gdal.RasterizeLayer(ds_out, [1], layer, options=["ALL_TOUCHED=TRUE", "MERGE_ALG=REPLACE"], burn_values=[value])
        layer = None
        ds_mem = None
    for key in order:
        if key == 'det':
            burn(systems_det, codes.get('det', 1))
        else:
            burn(forecasts_dict.get(key, []), codes.get(key, 0))
    ds_out = None

def rasterize_systems_to_tif(systems, reference_tiff_path, out_tif_path):
    from osgeo import gdal, ogr, osr
    if not systems:
        return
    if not os.path.exists(reference_tiff_path):
        raise RuntimeError(f"Não foi possível localizar TIFF de referência: {reference_tiff_path}")
    ref = gdal.Open(reference_tiff_path)
    if ref is None:
        raise RuntimeError(f"Não foi possível abrir TIFF de referência: {reference_tiff_path}")
    drv = gdal.GetDriverByName("GTiff")
    xsize = ref.RasterXSize
    ysize = ref.RasterYSize
    gt = ref.GetGeoTransform()
    proj = ref.GetProjection()
    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_Byte, options=["TILED=YES", "COMPRESS=LZW"])
    if ds_out is None:
        raise RuntimeError(f"Não foi possível criar GeoTIFF de saída: {out_tif_path}")
    ds_out.SetGeoTransform(gt)
    ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.Fill(0)
    mem_drv = ogr.GetDriverByName("Memory")
    ds_mem = mem_drv.CreateDataSource("mem")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = ds_mem.CreateLayer("systems", srs, geom_type=ogr.wkbPolygon)
    for s in systems:
        if getattr(s, "geom", None):
            feat = ogr.Feature(layer.GetLayerDefn())
            try:
                geom_sm = prepare_geom_for_raster(s.geom)
                feat.SetGeometry(geom_sm if geom_sm else s.geom)
            except Exception:
                feat.SetGeometry(s.geom)
            layer.CreateFeature(feat)
            feat = None
    gdal.RasterizeLayer(ds_out, [1], layer, options=["ALL_TOUCHED=TRUE", "MERGE_ALG=REPLACE"], burn_values=[1])
    ds_out = None
    layer = None

def rasterize_merge_to_tif(shp_path, reference_tiff_path, out_tif_path):
    from osgeo import gdal, ogr

    if not (os.path.exists(shp_path) and os.path.exists(reference_tiff_path)):
        raise RuntimeError(f"Entrada inválida para rasterização: {shp_path}, {reference_tiff_path}")

    ref = gdal.Open(reference_tiff_path)
    if ref is None:
        raise RuntimeError(f"Não foi possível abrir TIFF de referência: {reference_tiff_path}")

    drv = gdal.GetDriverByName("GTiff")
    xsize = ref.RasterXSize
    ysize = ref.RasterYSize
    gt = ref.GetGeoTransform()
    proj = ref.GetProjection()

    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_Byte, options=["TILED=YES", "COMPRESS=LZW"])
    if ds_out is None:
        raise RuntimeError(f"Não foi possível criar GeoTIFF de saída: {out_tif_path}")

    ds_out.SetGeoTransform(gt)
    ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.Fill(0)

    ds_shp = ogr.Open(shp_path, 0)
    if ds_shp is None:
        raise RuntimeError(f"Não foi possível abrir shapefile para rasterizar: {shp_path}")
    layer = ds_shp.GetLayer(0)

    gdal.RasterizeLayer(ds_out, [1], layer, options=["ALL_TOUCHED=TRUE", "MERGE_ALG=REPLACE"], burn_values=[1])

    ds_out = None
    layer = None

def rasterize_shp_to_tif(shp_path, reference_tiff_path, out_tif_path):
    from osgeo import gdal, ogr

    if not (os.path.exists(shp_path) and os.path.exists(reference_tiff_path)):
        raise RuntimeError(f"Entrada inválida para rasterização: {shp_path}, {reference_tiff_path}")

    ref = gdal.Open(reference_tiff_path)
    if ref is None:
        raise RuntimeError(f"Não foi possível abrir TIFF de referência: {reference_tiff_path}")

    drv = gdal.GetDriverByName("GTiff")
    xsize = ref.RasterXSize
    ysize = ref.RasterYSize
    gt = ref.GetGeoTransform()
    proj = ref.GetProjection()

    ds_out = drv.Create(out_tif_path, xsize, ysize, 1, gdal.GDT_Byte, options=["TILED=YES", "COMPRESS=LZW"])
    if ds_out is None:
        raise RuntimeError(f"Não foi possível criar GeoTIFF de saída: {out_tif_path}")

    ds_out.SetGeoTransform(gt)
    ds_out.SetProjection(proj)
    band = ds_out.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.Fill(0)

    ds_shp = ogr.Open(shp_path, 0)
    if ds_shp is None:
        raise RuntimeError(f"Não foi possível abrir shapefile para rasterizar: {shp_path}")
    layer = ds_shp.GetLayer(0)

    gdal.RasterizeLayer(ds_out, [1], layer, options=["ALL_TOUCHED=TRUE", "MERGE_ALG=REPLACE"], burn_values=[1])

    ds_out = None
    layer = None

def get_tiff_extent_and_resolution_km(tiff_path):
    from osgeo import gdal, osr
    import math

    ds = gdal.Open(tiff_path)
    if ds is None:
        raise RuntimeError(f"Não foi possível abrir TIFF: {tiff_path}")
    gt = ds.GetGeoTransform()
    width = ds.RasterXSize
    height = ds.RasterYSize
    proj = ds.GetProjection()
    srs = osr.SpatialReference()
    srs.ImportFromWkt(proj)
    is_3857 = srs.IsProjected() == 1 and srs.GetAuthorityCode(None) == "3857"

    minx = gt[0]
    maxy = gt[3]
    px_w = gt[1]
    px_h = gt[5]

    maxx = minx + px_w * width
    miny = maxy + px_h * height

    if is_3857:
        res_km = max(abs(px_w), abs(px_h)) / 1000.0
    else:
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
