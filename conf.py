import os

from pydantic import BaseSettings


class Settings(BaseSettings):
    """Settings for the application.

    Settings are loaded from environment variables or .env file

    Attributes:
        DESTINATION_DIR              Base destination dir
        BBOX                         BBOX as a comma separated list
        WRF_FORECAST_HOURS           WRF forecast hours to be acquired
        WRF_DESTINATION_DIR          WRF destination dir
        PCDS_IBGE_CODE               PCDs IBGE city codes as a comma separated list
        PCDS_DESTINATION_DIR         PCDs destination dir
        PCDS_USER: str               PCDs username
        PCDS_PASS: str               PCDs password
        GFS_FORECAST_HOURS           GFS Forecast hours
        GFS_DESTINANTION_DIR         GFS destination dir
        GFS_WIND_DESTINATION_DIR     GFS Wind destination dir
        GOES16_DESTINATION_DIR       GOES16 destination dir
        FORTRAC_DESTINATION_DIR      FORTRAC destination dir
        GOES16_HIDRO_DESTINATION_DIR GOES16 Hidroestimator destination dir
        MERGE_GPM_DESTINATION_DIR    Merge GPM destination dir
        TRIGRS_DESTINATION_DIR       TRIGRS output destination dir
        TRIGRS_DATA                  TRIGRS data dir
        TRIGRS_PATH                  TRIGRS install path

    """

    BBOX: str
    DESTINATION_DIR: str = "/data" if os.getenv("ENV") == "prod" else "./data"
    WRF_FORECAST_HOURS: int = 180
    WRF_FORECAST_HOURS_1H: int = 72
    WRF_DESTINATION_DIR: str = "previsao_wrf"
    WRF_DESTINATION_DIR_1H: str = "previsao_wrf_1h"
    WRF_TEMP_DESTINATION_DIR: str = "wrf_temp"
    PCDS_IBGE_CODE: str
    PCDS_DESTINATION_DIR: str = "pcds_cemaden"
    PCDS_USER: str
    PCDS_PASS: str
    GFS_DESTINATION_DIR: str = "previsao_gfs"
    GFS_FORECAST_HOURS: int = 72
    GFS_WIND_DESTINATION_DIR: str = "gfs_wind"
    GOES16_DESTINATION_DIR: str = "goes_temp"
    FORTRAC_DESTINATION_DIR: str = "fortrac"
    GOES16_HIDRO_DESTINATION_DIR: str = "goes_hidro"
    MERGE_GPM_DESTINATION_DIR: str = "merge_gpm"
    TRIGRS_DESTINATION_DIR: str = "trigrs"
    TRIGRS_PATH: str = "/opt/trigrs/trigrs_mpi"
    TRIGRS_DATA: str = "/trigrs_data" if os.getenv("ENV") == "prod" else "./trigrs_data"
    TRIGRS_RAINFALL_INDEX: int = 1
    BULLETIN_DESTINATION_DIR: str = "bulletin"
    BULLETIN_DATA: str = "/bulletin_data" if os.getenv("ENV") == "prod" else "./bulletin_data"

settings = Settings()  # type: ignore
