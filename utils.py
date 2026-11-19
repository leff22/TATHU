import json
import logging
import os
import time

import geopandas as gpd
import numpy as np
import rasterio
import requests
from bs4 import BeautifulSoup
from osgeo import gdal
from pyproj import CRS
from rasterio.mask import mask
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import from_bounds


def download_from_url(url, file_dest, params=None, headers=None, verify=True):
    """Download file from url to destination

    Args:
        url (str): URL source
        file_dest (str): File destination
        params (dict): Query params
        headers (dict): Request headers
    """

    if headers is None:
        headers = {}
    if params is None:
        params = {}

    with requests.get(
        f"{url}", params=params, stream=True, headers=headers, timeout=30, verify=verify
    ) as r:
        r.raise_for_status()
        if not os.path.exists(file_dest):
            with open(file_dest, "wb") as fp:
                for chunk in r.iter_content(chunk_size=8192):
                    fp.write(chunk)


def write_to_json(data, file_name: str):
    with open(file_name, "w", encoding="utf8") as file:
        json.dump(data, file, ensure_ascii=False)


def read_from_json(file_name: str):
    if not os.path.exists(file_name):
        return {}

    with open(file_name, encoding="utf8") as file:
        return json.load(file)


def reprojection(input_file, target_crs, output_file):
    """Reproject input data according to a target projection

    Args:
        input_file (str): input filename
        target_crs (str): coordinate reference system
        output_file (str): File destination
    """

    # Open the input file
    with rasterio.open(input_file) as src:
        # Calculate the transform and new size for reprojected data
        transform, width, height = calculate_default_transform(
            src.crs, target_crs, src.width, src.height, *src.bounds
        )

        # Update the profile to reflect the new CRS, resolution, and dimensions
        profile = src.profile
        profile.update(crs=target_crs, transform=transform, width=width, height=height)

        # Reproject and save the reprojected raster
        with rasterio.open(output_file, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=target_crs,
                resampling=Resampling.nearest,
            )


def cropping_area(input_file, band, bounding_box, output_file):
    """Select a band and crop its geographical area

    Args:
        input_file (str): input filename
        band (int): band number
        bounding_box (list): [w, s, e, n]
        output_file (str): file destination
    """

    # Open the original GeoTIFF file
    with rasterio.open(input_file) as src:
        # Create a window corresponding to the bounding box
        window = from_bounds(*bounding_box, transform=src.transform)

        # Read the data for the window
        data = src.read(band, window=window)

        # Get the metadata for the new cropped raster
        profile = src.profile

        # Update the profile with the new dimensions
        profile.update(
            height=window.height,
            width=window.width,
            count=1,  # save only one band
            transform=rasterio.transform.from_origin(
                bounding_box[0],  # left
                bounding_box[3],  # top
                src.transform[0],  # pixel width
                abs(src.transform[4]),  # pixel height
            ),
        )

        with rasterio.open(output_file, "w", **profile) as dst:
            dst.write(data, 1)


def resampling(input_file, target_resolution, output_file):
    """Select a band and crop its geographical area

    Args:
        input_file (str): input filename
        target_resolution (float): new resolution
        output_file (str): file destination
    """

    # Open input data
    with rasterio.open(input_file) as src:
        # Calculate the new pixel size
        pixel_width = target_resolution
        pixel_height = target_resolution

        # Calculate the new width and height of the image based on the desired resolution
        new_width = int(src.width * (src.transform[0] / pixel_width))
        new_height = int(src.height * (abs(src.transform[4]) / pixel_height))

        # Define the new transform based on the target resolution
        new_transform = rasterio.transform.from_origin(
            src.bounds[0],  # new left
            src.bounds[3],  # new top
            pixel_width,  # new pixel width (in meters)
            pixel_height,  # new pixel height (in meters)
        )

        # Update profile for the new resolution
        profile = src.profile
        profile.update(width=new_width, height=new_height, transform=new_transform)

        # Create a new file to store the resampled raster
        with rasterio.open(output_file, "w", **profile) as dst:
            data = src.read(1)
            # Resample the data to the target resolution
            data_resampled = np.empty((new_height, new_width), dtype=data.dtype)
            src.read(1, out=data_resampled)
            dst.write(data_resampled, 1)


def cropping_shape(input_file, shape_file, output_file):
    """Crop input data according to a shapefile

    Args:
        input_file (str): input filename
        shape_file (float): shapefile defining areas of interest
        output_file (str): file destination
    """

    # Read the shapefile using geopandas
    shape = gpd.read_file(shape_file)

    with rasterio.open(input_file) as new_ds:
        # Read the metadata for the entire dataset
        metadata = new_ds.meta.copy()

        # Ensure the shapefile's CRS matches the raster CRS
        if shape.crs != new_ds.crs:
            shape = shape.to_crs(new_ds.crs)

        # Extract the geometry of the shapefile
        geom = shape.geometry.values

        # Ensure the NoData value is set correctly
        nodata_value = new_ds.nodata if new_ds.nodata is not None else -9999

        # Mask the specified band using the shapefile geometry (crop and mask)
        out_image, out_transform = mask(new_ds, geom, crop=True, nodata=nodata_value)

        # Update metadata to reflect the new dimensions and include NoData
        metadata.update(
            {
                "driver": "GTiff",
                "count": out_image.shape[0],
                "nodata": nodata_value,
                "height": out_image.shape[1],
                "width": out_image.shape[2],
                "transform": out_transform,
            }
        )

        # Set the pixels outside the polygons to NoData (masking outside)
        # In this case, outside areas will have NaN after mask(), we can set them to NoData value
        out_image = np.where(out_image == new_ds.nodata, nodata_value, out_image)

        # Save the masked raster as a new file
        with rasterio.open(output_file, "w", **metadata) as dest:
            dest.write(out_image)


def tif_to_ascii(input_file, output_file):
    """Convert a tif file to an ascii file

    Args:
        input_file (str): input filename
        output_file (str): file destination
    """

    # Open the raster with rasterio
    with rasterio.open(input_file) as src:
        # Read the raster data (assuming it's a single band)
        band = src.read(1)

        # Get metadata
        ncols = src.width
        nrows = src.height
        xllcorner = src.bounds[0]  # left
        yllcorner = src.bounds[1]  # bottom
        cellsize = abs(src.transform[0])  # pixel size (in the x-direction)
        nodata = int(src.nodata) if src.nodata is not None else -9999

        # Write to ASCII file
        with open(output_file, "w") as asc:
            # Write the header information
            asc.write(f"ncols         {ncols}\n")
            asc.write(f"nrows         {nrows}\n")
            asc.write(f"xllcorner     {xllcorner}\n")
            asc.write(f"yllcorner     {yllcorner}\n")
            asc.write(f"cellsize      {cellsize}\n")
            asc.write(f"NODATA_value  {nodata}\n")

            # Write the raster data
            for row in band:
                # Convert all values to integers (if the value is NoData value, print as integer)
                row_str = " ".join(
                    [str(int(val)) if val != nodata else str(int(nodata)) for val in row]
                )
                asc.write(f"{row_str}\n")


def ascii_to_tif(ascii_file, in_epsg, out_epsg, output_file):
    """Convert an ascii file to a tif file

    Args:
        ascii_file (str): input filename
        in_epsg (int): input EPSG code, ONLY use it if not defined in the ascii file
        out_epsg (int): output EPSG code
        output_file (str): file destination
    """

    # Open ASCII file
    with rasterio.open(ascii_file) as src:
        # Check if the input has a CRS (if it's missing, manually define it)
        src_crs = CRS.from_epsg(in_epsg) if src.crs is None else src.crs

        # Get the input's metadata
        input_transform = src.transform
        input_width = src.width
        input_height = src.height
        input_data = src.read(1)  # Read the first band

        # Assign nodata value
        nodata_value = src.nodata if src.nodata is not None else -9999

        # Define the desired CRS
        dst_crs = CRS.from_epsg(out_epsg)

        # Calculate the transform for the new projection
        transform, width, height = calculate_default_transform(
            src_crs, dst_crs, input_width, input_height, *src.bounds
        )

        # Open the output GeoTIFF file to write the resampled data
        with rasterio.open(
            output_file,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,  # One band (single-band raster)
            dtype=src.dtypes[0],
            crs=dst_crs,  # new CRS
            transform=transform,
            nodata=nodata_value,
        ) as dst:
            # Perform the reprojection from input CRS to EPSG:4326
            reproject(
                input_data,
                destination=rasterio.band(dst, 1),  # write directly to the output file (band 1)
                src_transform=input_transform,
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,  # select the resampling method
            )


def get_grib2_filenames_from_url(url):
    """
    Get a sorted list of grib2 files from a url

    Parameters:
        url (str): url

    Returns:
        lfiles (list of str): list of sorted GRIB2 filenames
    """

    resp = requests.get(url, timeout=30, verify=True)

    soup = BeautifulSoup(resp.text, "html.parser")
    lfiles = [
        link.get("href") for link in soup.find_all("a") if link.get("href").endswith(".grib2")
    ]
    lfiles.sort()

    return lfiles


def safe_open_gdal(vsi_url, retries=3, delay=5):
    """
    Add retry logic with a delay to open a file with gdal

    Parameters:
        vsi_url (str): GRIB2 file URL
        retries (int): number of attempts to open the GRIB2 file
        delay (int): time delay in seconds between attempts to open the GRIB2 file

    Returns:
        dataset (gdal structure) or None: file oppened if successful, otherwise None
    """

    for attempt in range(retries):
        dataset = gdal.Open(vsi_url)
        if dataset is not None and dataset.RasterCount > 0:
            return dataset
        logging.warning(f"Attempt {attempt + 1} failed to open: {vsi_url}")
        time.sleep(delay)
    logging.warning(f"All {retries} attempts failed for: {vsi_url}")

    return None


def process_field_from_grib(url, bbox, b_name, b_level, out_fn):
    """
    Process a GRIB2 URL to extract, clip, and save one field as GeoTIFF.
    The reprojection and other facilities can be added later.

    Parameters:
        url (str): GRIB2 file URL
        bbox (tuple): bounding box to clip to (minX, minY, maxX, maxY)
        b_name (str): band name
        b_level (str): band level
        out_fn (str): GeoTIFF filename

    Returns:
        (bool): True if successful, otherwise False
    """

    try:
        vsi_url = f"/vsicurl/{url}"

        # Open GRIB2 file
        ds = safe_open_gdal(vsi_url, retries=3, delay=5)
        if ds is None:
            logging.warning(f"Failed to open {url}")
            return False

        # Get total precipitation field
        band_index = None
        for b in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(b)
            meta = band.GetMetadata()
            grib_element = meta.get("GRIB_ELEMENT", "")
            short_name = meta.get("GRIB_SHORT_NAME", "")
            if b_name in grib_element and b_level in short_name:
                band_index = b
                break

        if band_index is None:
            logging.warning(f"No input band found in {url}")
            ds = None  # Cleanup
            return False

        # Translate options for clipping
        warp_options = gdal.WarpOptions(
            outputBounds=bbox,
            format="GTiff",
            srcBands=[band_index],
            dstNodata=-9999,
            multithread=True,
            creationOptions=[
                "COMPRESS=LZW",
            ],
        )

        # Clip and save as GeoTIFF using gdal.Warp
        gdal.Warp(destNameOrDestDS=out_fn, srcDSOrSrcDSTab=ds, options=warp_options)

    except Exception as e:
        logging.warning(f"Error processing {url}: {e}")
        return False

    return True
