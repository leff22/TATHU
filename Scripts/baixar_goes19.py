## ESSE SCRIPT BAIXA AS IMAGENS DO GOES-19 NO AWS S3 BUCKET, 
## TRATA OS DADOS E CRIA UMA IMAGEM TIFF

#========================================================================================================================#
#                                          IMPORTAÇÃO DAS BIBLIOTECAS
#========================================================================================================================#
import xarray as xr
import matplotlib.pyplot as plt
import cartopy, cartopy.crs as ccrs
from osgeo import gdal
from datetime import datetime, timedelta
from utilities_goes16e19 import download_CMI, remap
import numpy as np
import os
import warnings
warnings.filterwarnings("ignore")

#========================================================================================================================#
#                                        CRIA DIRETÓRIO DE ENTRADA E SAÍDA
#========================================================================================================================#
input = './goes19-aws/goes19_ciclone'
output = './goes19-aws/goes19_ciclone/saidas'; os.makedirs(output, exist_ok=True)

start_dt = datetime(2026, 1, 31, 19, 0)
end_dt = datetime(2026, 2, 1, 1, 0)

goes_number = '19'
band = '13'

extent = [-62.0, -34.0, -40.0, -18.0]
driver = gdal.GetDriverByName('GTiff')

current_dt = start_dt
while current_dt <= end_dt:
    yyyymmddhhmn = current_dt.strftime('%Y%m%d%H%M')
    try:
        file_name = download_CMI(yyyymmddhhmn, band, goes_number, input)
        path = f'{input}/{file_name}.nc'
        grid = remap(path, 'CMI', extent, 2)
        data = grid.ReadAsArray() - 273.15
        tif_path = f'{output}/{yyyymmddhhmn}_{goes_number}_ch{band}_retangular_cinza.tif'
        dst = driver.Create(tif_path, grid.RasterXSize, grid.RasterYSize, 1, gdal.GDT_Float32, options=['COMPRESS=LZW'])
        dst.SetProjection(grid.GetProjection())
        dst.SetGeoTransform(grid.GetGeoTransform())
        dst.GetRasterBand(1).WriteArray(data.astype(np.float32))
        dst.GetRasterBand(1).SetNoDataValue(np.nan)
        dst = None
        grid = None
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    current_dt += timedelta(minutes=10)
