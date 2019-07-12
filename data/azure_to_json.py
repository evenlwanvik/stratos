import xarray as xr
import json
import numpy as np
import os
from copy import deepcopy
import time # for execution time testing
import math
import zarr
import blosc
import sys
import logging
import warnings
# TODO: CHANGE BACK TO data.color_encoding
from color_encoding import temp_to_rgb, set_colormap_range


logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.WARNING,
    datefmt='%Y-%m-%d %H:%M:%S')

'''
 1. load template geojson file as DATA
 2. load output geojson file as output
 3. using data loaded by the dataset module
 4. run function for inserting source data into the temporary (templated) data file
 5. dump the data file into outputfile (the geojson file to be used in leaflet)
 create subgrid with indexes:
 (221,19) - (222,20) -> polygon "route" becomes (221,19), (221,20), (222,20), (222,19), (221,19)
 
     *(221,19) -> (221,2)
         ^          |
      (221,20) <- (222,20)                         
'''

# define paths to files
initial_template_path = "data/templates/initial_template.json"
feature_template_path = "data/templates/feature_template.json"
output_path = "data/outputs/surface_temp.json"


def geojson_grid_coord(lats, lons, startEdge):
    '''
    takes a xarray object of a netCDF file as source and variable loaded
    with a geojson dictionary template as target. 
    gridDim is the size of the square grid to be rendered, default is 2 (2x2)
    To show differences in temperature, the temperature is encoded into 
    rgb hexadecimal and inserted into the "fill" under "properties" of the 
    polygon. The polygon itself is inserted into the "coordinates" key under "geometry"
     
    nested loop:
    1. for each square within a given size
    2. y-direction 
    3. x-direction 
    always iterate over polygonedges and squares in the same convention as in moduledescription
    '''
    # output list
    coords = []
    # the box is 2x2, this will be made dynamic in the future
    xyRange = range(2)              
    # Index of the polygon square
    polyEdgeIdx = 0 
    for y in xyRange:           # iterate y
        innerRange = xyRange              
        if y == 1:
            innerRange = reversed(xyRange)     # backward iteration to complete the polygon
        for x in innerRange: # iterate x
            yx = (startEdge[0]+y, startEdge[1]+x) # (y,x) defined as such in the netCDF file
            lat  = float(lats[yx])
            lon  = float(lons[yx])
            # OBS! The GIS lat/lon is different from geojson standard, so these are "flipped"
            coords.append([lon, lat])
            polyEdgeIdx += 1
    # the last list must always be the same as the first 
    coords.append(coords[0])      
    return coords


def geojson_grid_temp(T_in):
    ''' Get T from T_in if not nan, convert to RGB hex and insert into T_out reference'''
    return temp_to_rgb(T_in)


def get_initial_template():
    with open(initial_template_path, "r") as template:
        return json.load(template)


def get_feature_template():
    with open(feature_template_path) as feature_json_template:
        return json.load(feature_json_template)


''' netcdf_to_json should just return the json object instead of writing it to file

def write_output(data):
    # open and dump data to output geojson file, remove if exists
    if os.path.isfile(output_path):
        os.remove(output_path)
    # open the final product file as output 
    with open(output_path, "w+") as output: 
        # dump new data into output json file 
        json.dump(data, output, indent=4)    
'''

def create_blob_client(dataset):
    ''' TODO: This should be made to properly react to user input, for now we only
    switch between one measurement per norsok and franfjorden. 
    It should also be able to react to type of measurement, e.g. temperature, wind..'''

    if ('norsok' in dataset['dataset']):
        BLOB_NAME = 'norsok/samples_NSEW.nc_201301_nc4.zarr'
    else:
        BLOB_NAME = 'Franfjorden32m/time&depth-chunked-consolidated-metadata.zarr'
    CONTAINER_NAME  = 'zarr'
    ACCOUNT_NAME    = 'stratos'
    ACCOUNT_KEY     = 'A7nrOYKyq6y2GLlprXc6tmd+olu50blx4sPjdH1slTasiNl8jpVuy+V0UBWFNmwgVFSHMGP2/kmzahXcQlh+Vg=='
    absstore_object = zarr.storage.ABSStore(CONTAINER_NAME, BLOB_NAME, ACCOUNT_NAME, ACCOUNT_KEY)
    return absstore_object


def set_colormap_encoding_range(measurements, fill_value):
    # finding min/max for color encoding for this grid, this should
    if fill_value == np.NaN:
        set_colormap_range({'min': measurements.nanmin(), 'max': measurements.nanmax()})
    elif fill_value == -32768:
        # omit fillvalues for finding minimum..
        x = measurements[measurements > -32768]
        set_colormap_range({'min': x.min(), 'max': x.max()})
    else:
        warnings.warn("unknown encoding of fill_value, insert in this if-else-section")


def get_decompressed_arrays(dataset, depthIdx=0, timeIdx=0):
    # azure zarr-blob object
    absstore_obj = create_blob_client(dataset)

    datatype = dataset['type']

    section_start = time.time()
    decom_meas = zarr.blosc.decompress(absstore_obj['{}/{}.{}.0.0'.format(datatype,timeIdx,depthIdx)])
    decom_lats = zarr.blosc.decompress(absstore_obj['gridLons/0.0'])
    decom_lons = zarr.blosc.decompress(absstore_obj['gridLats/0.0'])
    end = time.time()
    logging.warning("decompressing azure blob chunks execution time: %f",end-section_start)

    #The metadata for coodinates might be different from the measurement
    coord_metadata = json.loads(absstore_obj['gridLats/.zarray'])
    meas_metadata = json.loads(absstore_obj['{}/.zarray'.format(datatype)])
    coord_shape = tuple(coord_metadata['chunks'])
    meas_shape = coord_shape
    coord_datatype = coord_metadata['dtype']
    meas_datatype = meas_metadata['dtype']
    logging.warning(meas_metadata)

    # create numpy arrays from the decompressed buffers and give it our grid shape
    section_start = time.time()
    lons = np.frombuffer(decom_lats, dtype=coord_datatype).reshape(coord_shape)
    lats = np.frombuffer(decom_lons, dtype=coord_datatype).reshape(coord_shape)
    measurements = np.array(np.frombuffer(decom_meas, dtype=meas_datatype).reshape(meas_shape))
    end = time.time()
    logging.warning("creating numpy arrays of decompresed arrays execution time: %f",end-section_start)

    return([lons, lats, measurements, meas_metadata['fill_value']])

def azure_to_json(startEdge=(0,0), 
                    nGrids=10, 
                    gridSize=1, 
                    depthIdx=0,
                    timeIdx=0,
                    dataset={'dataset':'nordfjord32m', 'type':'temperature'}):
    ''' 
    Inserts netCDF data from 'startEdge' in a square of size 'nGrids' in positive lat/lon
    index direction into a geojson whose path is described as output. 
    It appends geojson template dictionaries from a given file-path into a 
    temporary data variable, whose data is changed and dumped into output object.

    Parameters
    ----------
    depthIdx : int
        the depth of the grid to be computed
    timeIdx : string
        the time index of the grid to be computed
    filetype : dict
        dictionary containing what dataset and measurement type to be computed
    '''
    start = time.time() 

    # Get the initial template
    jsonData = get_initial_template() 
    # Get the feature template, this will be appended to jsonData for each feature inserted
    feature_template = get_feature_template()

    # download z-arrays from azure cloud and decompress requested chunks
    [lons, lats, measurements, fill_value] = get_decompressed_arrays(dataset, timeIdx, depthIdx)

    # find min and max and set the color encoding for displaying change on map
    set_colormap_encoding_range(measurements, fill_value)

    # featureIdx counts what feature we are working on = 0,1,2,3, ... 
    featureIdx = 0
    for y in range(nGrids):                 
        for x in range(nGrids): 
            # get the start edge of the next polygon to be inserted into dictionary
            edge = (startEdge[0]+y, startEdge[1]+x)       
            # if temp=nan: skip grid -> else: insert lat/lon and temp into data
            measurement = float(measurements[edge])
            if math.isnan(measurement) or measurement == -32768:
                continue
            else:
                # add a copy of the feature dictionary template to features
                jsonData['features'].append(deepcopy(feature_template))    
                # create a reference to the current feature we are working on
                feature = jsonData['features'][featureIdx]
                # insert hex into fill
                feature['properties']['fill'] = geojson_grid_temp(measurement) 
                # getting coordinates of polygon and insert into geojson feature
                feature['geometry']['coordinates'][0] = geojson_grid_coord(lats, lons, edge)                 
                
                featureIdx = featureIdx + 1

    logging.warning("showing %s for %s", dataset['type'], dataset['dataset'])
    logging.warning("startEdge: %s, nGrids: %d, depthIdx: %d, timeIdx: %d",startEdge,nGrids,depthIdx,timeIdx)
    #write_output(jsonData)

    end = time.time()
    logging.warning("execution time: %f",end-start)
    return jsonData

''' 
One example of reducing the load of taking the whole source file as xarray could 
be to nest the opens as such:
with xr.open_dataset(source_pat, drop_variables[...]) as geo_coord:
    with xr.open_dataset(source_path, drop_variables[...]) as tmp:

or just

coord = xr.open_dataset(source_path, drop_variables[...])
tmp = xr.open_dataset(source_path, drop_variables[...])

25/06/2019:
    Testing execution time with getting 100 grids:
        - normal operation (x=1) takes about 1e-6
        - appending the 'features' template to the data file takes about 2e-4
        - copying lat/lon data takes about 4e-4
        - fetching the hex RGB from temperature takes about 5e-3
        - the loop for inserting coordinates takes about 1.5e-2 :O

26/06/2019:
    Split data into subset copies for processing, still getting 100 grids:
        - the loop for inserting coordinates now takes about 7e-3
        - full operation takes 0.68815 seconds as before it took
            2.14507 seconds. big improvements.
    takes a total of 206 seconds to get 250^2=62500 grids

Tried to increase number of CPUs to 2 and memory to about 1k. The execution time of 
the insertion loop still took 1.5e-2 seconds. Maybe implement multithreading, spreading
the resource between different grid squares.

'''


# TODO: Keep this for quick-testing for now..
#dataset={'dataset':'nordfjord32m', 'type':'temperature'}
#dataset={'dataset':'nordfjord32m', 'type':'salinity'}
#dataset={'dataset':'norsok', 'type':'temperature'}

#azure_to_json(nGrids=100, 
#                    depthIdx=0,
#                    timeIdx=0,
#                    dataset=dataset)