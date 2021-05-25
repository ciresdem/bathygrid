import numpy as np
import xarray as xr
from dask.array import Array
from dask.distributed import wait, progress
from typing import Union
import matplotlib.pyplot as plt

from bathygrid.grids import BaseGrid
from bathygrid.tile import SRTile
from bathygrid.utilities import bin2d_with_indices, dask_find_or_start_client


depth_resolution_lookup = {20: 0.5, 40: 1.0, 60: 2.0, 80: 4.0, 160: 8.0, 320: 16.0, 640: 32.0, 1280: 64.0, 2560: 128.0,
                           5120: 256.0, 10240: 512.0, 20480: 1024.0}


class BathyGrid(BaseGrid):
    """
    Manage a rectangular grid of tiles, each able to operate independently and in parallel.  BathyGrid automates the
    creation and updating of each Tile, which happens under the hood when you add or remove points.

    Used in the VRGridTile as the tiles of the master grid.  Each tile of the VRGridTile is a BathyGrid with tiles within
    that grid.
    """

    def __init__(self, min_x: float = 0, min_y: float = 0, max_x: float = 0, max_y: float = 0, tile_size: float = 1024.0,
                 set_extents_manually: bool = False):
        super().__init__(min_x=min_x, min_y=min_y, tile_size=tile_size)

        if set_extents_manually:
            self.min_x = min_x
            self.min_y = min_y
            self.max_x = max_x
            self.max_y = max_y

        self.mean_depth = None

        self.epsg = None  # epsg code
        self.vertical_reference = None  # string identifier for the vertical reference
        self.resolutions = []

        self.min_grid_resolution = None
        self.max_grid_resolution = None

        self.layer_lookup = {'depth': 'z', 'vertical_uncertainty': 'tvu', 'horizontal_uncertainty': 'thu'}
        self.rev_layer_lookup = {'z': 'depth', 'tvu': 'vertical_uncertainty', 'thu': 'horizontal_uncertainty'}

        self.client = None

    @property
    def no_grid(self):
        """
        Simple check to see if this instance contains gridded data or not.  Looks for the first existing Tile and checks
        if that Tile has a grid.

        Returns
        -------
        bool
            True if the BathyGrid instance contains no grids, False if it does contain a grid.
        """

        if self.tiles is None:
            return True

        for tile in self.tiles.flat:
            if tile:
                if isinstance(tile, BathyGrid):
                    for subtile in tile.tiles.flat:
                        if subtile:
                            tile = subtile
                if tile.cells:
                    return False
                else:
                    return True

    def _update_mean_depth(self):
        """
        Calculate the mean depth of all loaded points before they are loaded into tiles and cleared from this object
        """

        if self.data is None or not self.data['z'].any():
            self.mean_depth = None
        else:
            self.mean_depth = self.data['z'].mean()

    def _calculate_resolution(self):
        """
        Use the depth resolution lookup to find the appropriate depth resolution band.  The lookup is the max depth and
        the resolution that applies.

        Returns
        -------
        float
            resolution to use at the existing mean_depth
        """

        if self.mean_depth is None:
            raise ValueError('SRTile: Unable to calculate resolution when mean_depth is None')
        dpth_keys = list(depth_resolution_lookup.keys())
        # get next positive value in keys of resolution lookup
        range_index = np.argmax((np.array(dpth_keys) - self.mean_depth) > 0)
        return depth_resolution_lookup[dpth_keys[range_index]]

    def _build_tile(self, tile_x_origin: float, tile_y_origin: float):
        """
        Default tile of the BathyGrid is just a simple SRTile

        Parameters
        ----------
        tile_x_origin
            x origin coordinate for the tile, in the same units as the BathyGrid
        tile_y_origin
            y origin coordinate for the tile, in the same units as the BathyGrid

        Returns
        -------
        SRTile
            empty SRTile for this origin / tile size
        """

        return SRTile(tile_x_origin, tile_y_origin, self.tile_size)

    def _build_empty_tile_space(self):
        """
        Build a 2d array of NaN for the size of one of the tiles.
        """

        return np.full((self.tile_size, self.tile_size), np.nan)

    def _build_layer_grid(self, resolution: float):
        """
        Build a 2d array of NaN for the size of the whole BathyGrid (given the provided resolution)

        Parameters
        ----------
        resolution
            float resolution that we want to use to build the grid
        """

        y_size = self.height / resolution
        x_size = self.width / resolution
        assert y_size.is_integer()
        assert x_size.is_integer()
        return np.full((int(y_size), int(x_size)), np.nan)

    def _convert_dataset(self):
        """
        inherited class can write code here to convert the input data
        """
        pass

    def _update_metadata(self, container_name: str = None, file_list: list = None, epsg: int = None,
                         vertical_reference: str = None):
        """
        inherited class can write code here to handle the metadata
        """
        pass

    def _validate_input_data(self):
        """
        inherited class can write code here to validate the input data
        """
        pass

    def _update_base_grid(self):
        """
        If the user adds new points, we need to make sure that we don't need to extend the grid in a direction.
        Extending a grid will build a new existing_tile_index for where the old tiles need to go in the new grid, see
        _update_tiles.
        """

        # extend the grid for new data or start a new grid if there are no existing tiles
        if self.data is not None:
            if self.is_empty:  # starting a new grid
                if self.can_grow:
                    self._init_from_extents(self.data['y'].min(), self.data['x'].min(), self.data['y'].max(),
                                            self.data['x'].max())
                else:
                    self._init_from_extents(self.min_y, self.min_x, self.max_y, self.max_x)
            elif self.can_grow:
                self._update_extents(self.data['y'].min(), self.data['x'].min(), self.data['y'].max(),
                                     self.data['x'].max())
            else:  # grid can't grow, so we just leave existing tiles where they are
                pass

    def _update_tiles(self, container_name):
        """
        Pick up existing tiles and put them in the correct place in the new grid.  Then add the new points to all of
        the tiles.

        Parameters
        ----------
        container_name
            the folder name of the converted data, equivalent to splitting the output_path variable in the kluster
            dataset
        """

        if self.data is not None:
            if self.is_empty:  # build empty list the same size as the tile attribute arrays
                self.tiles = np.full(self.tile_x_origin.shape, None, dtype=object)
            elif self.can_grow:
                new_tiles = np.full(self.tile_x_origin.shape, None, dtype=object)
                new_tiles[self.existing_tile_mask] = self.tiles.ravel()
                self.tiles = new_tiles
            else:  # grid can't grow, so we just leave existing tiles where they are
                pass
            self._add_points_to_tiles(container_name)

    def _add_points_to_tiles(self, container_name):
        """
        Add new points to the tiles.  Will run bin2d to figure out which points go in which tiles.  If there is no tile
        where the points go, will build a new tile and add the points to it.  Otherwise, adds the points to an existing
        tile.  If the container_name is already in the tile (we have previously added these points), the tile will
        clear out old points and replace them with new.

        If for some reason the resulting state of the tile is empty (no points in the tile) we replace the tile with None.

        Parameters
        ----------
        container_name
            the folder name of the converted data, equivalent to splitting the output_path variable in the kluster
            dataset
        """

        if self.data is not None:
            binnum = bin2d_with_indices(self.data['x'], self.data['y'], self.tile_edges_x, self.tile_edges_y)
            unique_locs = np.unique(binnum)
            flat_tiles = self.tiles.ravel()
            tilexorigin = self.tile_x_origin.ravel()
            tileyorigin = self.tile_y_origin.ravel()
            for ul in unique_locs:
                point_mask = binnum == ul
                pts = self.data[point_mask]
                if flat_tiles[ul] is None:
                    flat_tiles[ul] = self._build_tile(tilexorigin[ul], tileyorigin[ul])
                flat_tiles[ul].add_points(pts, container_name)
                if flat_tiles[ul].is_empty:
                    flat_tiles[ul] = None

    def add_points(self, data: Union[xr.Dataset, Array, np.ndarray], container_name: str, file_list: list = None,
                   crs: int = None, vertical_reference: str = None):
        """
        Add new points to the grid.  Build new tiles to encapsulate those points, or add the points to existing tiles
        if they fall within existing tile boundaries.

        Parameters
        ----------
        data
            Sounding data from Kluster.  Should contain at least 'x', 'y', 'z' variable names/data
        container_name
            the folder name of the converted data, equivalent to splitting the output_path variable in the kluster
            dataset
        file_list
            list of multibeam files that exist in the data to add to the grid
        crs
            epsg (or proj4 string) for the coordinate system of the data.  Proj4 only shows up when there is no valid
            epsg
        vertical_reference
            vertical reference of the data
        """

        if isinstance(data, (Array, xr.Dataset)):
            data = data.compute()
        self.data = data
        self._validate_input_data()
        self._update_metadata(container_name, file_list, crs, vertical_reference)
        self._update_base_grid()
        self._update_tiles(container_name)
        self._update_mean_depth()
        self.data = None  # points are in the tiles, clear this attribute to free up memory

    def remove_points(self, container_name: str = None):
        """
        We go through all the existing tiles and remove the points associated with container_name

        Parameters
        ----------
        container_name
            the folder name of the converted data, equivalent to splitting the output_path variable in the kluster
            dataset
        """

        if container_name in self.container:
            self.container.pop(container_name)
            if not self.is_empty:
                flat_tiles = self.tiles.ravel()
                for tile in flat_tiles:
                    if tile:
                        tile.remove_points(container_name)
                        if tile.is_empty:
                            flat_tiles[flat_tiles == tile] = None
            if self.is_empty:
                self.tiles = None

    def get_layer_by_name(self, layer: str = 'depth', resolution: float = None):
        """
        Return the numpy 2d grid for the provided layer, resolution.  Will check to ensure that you have gridded at this
        resolution already.  Grid returned will have NaN values for empty spaces.

        Parameters
        ----------
        layer
            string identifier for the layer to access, one of 'depth', 'horizontal_uncertainty', 'vertical_uncertainty'
        resolution
            resolution of the layer we want to access

        Returns
        -------
        np.ndarray
            gridded data for the provided layer, resolution across all tiles
        """

        if self.no_grid:
            raise ValueError('BathyGrid: Grid is empty, gridding has not been run yet.')
        if not resolution:
            if len(self.resolutions) > 1:
                raise ValueError('BathyGrid: you must specify a resolution to return layer data when multiple resolutions are found')
            resolution = self.resolutions[0]
        data = self._build_layer_grid(resolution)
        for cnt, tile in enumerate(self.tiles.flat):
            if tile:
                col, row = self._tile_idx_to_row_col(cnt)
                tile_cell_count = self.tile_size / resolution
                assert tile_cell_count.is_integer()
                tile_cell_count = int(tile_cell_count)
                data_col, data_row = col * tile_cell_count, row * tile_cell_count
                data[data_col:data_col + tile_cell_count, data_row:data_row + tile_cell_count] = tile.get_layer_by_name(layer, resolution)
        return data

    def get_layer_trimmed(self, layer: str = 'depth', resolution: float = None):
        """
        Get the layer indicated by the provided layername and trim to the minimum bounding box of real values in the
        layer.

        Parameters
        ----------
        layer
            string identifier for the layer to access, one of 'depth', 'horizontal_uncertainty', 'vertical_uncertainty'
        resolution
            resolution of the layer we want to access

        Returns
        -------
        np.ndarray
            2dim array of gridded layer trimmed to the minimum bounding box
        list
            new mins to use
        list
            new maxs to use
        """
        data = self.get_layer_by_name(layer, resolution)
        notnan = ~np.isnan(data)
        rows = np.any(notnan, axis=1)
        cols = np.any(notnan, axis=0)
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]

        rmax += 1
        cmax += 1

        return data[rmin:rmax, cmin:cmax], [rmin, cmin], [rmax, cmax]

    def _grid_regular(self, algorithm: str, resolution: float, clear_existing: bool):
        """
        Run the gridding without Dask, Tile after Tile.

        Parameters
        ----------
        resolution
            resolution of the gridded data in the Tiles
        algorithm
            algorithm to grid by
        clear_existing
            if True, will clear out any existing grids before generating this one
        """

        self.resolutions = []
        for tile in self.tiles.flat:
            if tile:
                resolution = tile.grid(algorithm, resolution, clear_existing=clear_existing)
                if resolution not in self.resolutions:
                    self.resolutions.append(resolution)
        self.resolutions = np.sort(np.unique(self.resolutions))

    def _grid_parallel(self, algorithm: str, resolution: float, clear_existing: bool):
        """
        Use Dask to submit the tiles in parallel to the cluster for processing.  Probably should think up a more
        intelligent way to do this than sending around the whole Tile obejct.  That object has a bunch of other stuff
        that isn't used by the gridding process.  Although maybe with lazy loading of data, that doesnt matter as much.

        Parameters
        ----------
        resolution
            resolution of the gridded data in the Tiles
        algorithm
            algorithm to grid by
        clear_existing
            if True, will clear out any existing grids before generating this one
        """

        if not self.client:
            self.client = dask_find_or_start_client()

        chunks_at_a_time = len(self.client.ncores())
        total_runs = int(np.ceil(len(self.tiles.flat) / 8))
        cur_run = 1
        self.resolutions = []

        data_for_workers = []
        futs = []
        chunk_index = 0
        for tile in self.tiles.flat:
            if tile:
                data_for_workers.append([tile, algorithm, resolution, clear_existing])
                chunk_index += 1
                if chunk_index == chunks_at_a_time:
                    print('processing surface: group {} out of {}'.format(cur_run, total_runs))
                    cur_run += 1
                    chunk_index = 0
                    data_for_workers = self.client.scatter(data_for_workers)
                    futs.append(self.client.map(_gridding_parallel, data_for_workers))
                    data_for_workers = []
                    progress(futs, multi=False)
        if data_for_workers:
            print('processing surface: group {} out of {}'.format(cur_run, total_runs))
            data_for_workers = self.client.scatter(data_for_workers)
            futs.append(self.client.map(_gridding_parallel, data_for_workers))
            progress(futs, multi=False)
        wait(futs)
        results = self.client.gather(futs)
        results = [item for sublist in results for item in sublist]
        resolutions = [res[0] for res in results]
        tiles = [res[1] for res in results]
        self.tiles[self.tiles != None] = tiles
        self.resolutions = np.sort(np.unique(resolutions))

    def grid(self, algorithm: str = 'mean', resolution: float = None, clear_existing: bool = False, use_dask: bool = False):
        """
        Gridding involves calling 'grid' on all child grids/tiles until you eventually call 'grid' on a Tile.  The Tiles
        are the objects that actually contain the points / gridded data

        Parameters
        ----------
        resolution
            resolution of the gridded data in the Tiles
        algorithm
            algorithm to grid by
        clear_existing
            if True, will clear out any existing grids before generating this one
        """

        if self.is_empty:
            raise ValueError('BathyGrid: Grid is empty, no points have been added')
        if resolution is None:
            resolution = self._calculate_resolution()
        if use_dask:
            self._grid_parallel(algorithm, resolution, clear_existing)
        else:
            self._grid_regular(algorithm, resolution, clear_existing)
        return resolution

    def plot(self, layer: str = 'depth', resolution: float = None):
        """
        Use matplotlib imshow to plot the layer/resolution.

        Parameters
        ----------
        layer
            string identifier for the layer to access, one of 'depth', 'horizontal_uncertainty', 'vertical_uncertainty'
        resolution
            resolution of the layer we want to access
        """

        if self.no_grid:
            raise ValueError('BathyGrid: Grid is empty, gridding has not been run yet.')
        if not resolution:
            if len(self.resolutions) > 1:
                raise ValueError('BathyGrid: you must specify a resolution to return layer data when multiple resolutions are found')
            resolution = self.resolutions[0]
        data = self.get_layer_by_name(layer, resolution)
        plt.imshow(data, origin='lower')
        plt.title('{}_{}'.format(layer, resolution))

    def return_layer_names(self):
        """
        Return a list of layer names based on what layers exist in the BathyGrid instance.

        Returns
        -------
        list
            list of str surface layer names (ex: ['depth', 'horizontal_uncertainty', 'vertical_uncertainty']
        """
        if self.no_grid:
            return []
        for tile in self.tiles.flat:
            if tile:
                return list(tile.cells.keys)
        return []

    def return_extents(self):
        """
        Return the 2d extents of the BathyGrid

        Returns
        -------
        list
            [[minx, miny], [maxx, maxy]]
        """

        return [[self.min_x, self.min_y], [self.max_x, self.max_y]]

    def return_surf_xyz(self, layer: str = 'depth', resolution: float = None, cell_boundaries: bool = True):
        """
        Return the xyz grid values as well as an index for the valid nodes in the surface.  z is the gridded result that
        matches the provided layername

        Parameters
        ----------
        layer
            string identifier for the layer to access, one of 'depth', 'horizontal_uncertainty', 'vertical_uncertainty'
        resolution
            resolution of the layer we want to access
        cell_boundaries
            If True, the user wants the cell boundaries, not the node locations.  If False, returns the node locations
            instead.  If you use matplotlib pcolormesh, you want this to be False.

        Returns
        -------
        np.ndarray
            numpy array, 1d x locations for the grid nodes
        np.ndarray
            numpy array, 1d y locations for the grid nodes
        np.ndarray
            numpy 2d array, 2d grid depth values
        np.ndarray
            numpy 2d array, boolean mask for valid nodes with depth
        list
            new minimum x,y coordinate for the trimmed layer
        list
            new maximum x,y coordinate for the trimmed layer
        """
        if self.no_grid:
            raise ValueError('BathyGrid: Grid is empty, gridding has not been run yet.')
        if not resolution:
            if len(self.resolutions) > 1:
                raise ValueError('BathyGrid: you must specify a resolution to return layer data when multiple resolutions are found')
            resolution = self.resolutions[0]
        surf, new_mins, new_maxs = self.get_layer_trimmed(layer)
        valid_nodes = ~np.isnan(surf)
        if not cell_boundaries:  # get the node locations for each cell
            x = (np.arange(self.min_x, self.max_x, resolution) + resolution / 2)[new_mins[0]:new_maxs[0]]
            y = (np.arange(self.min_y, self.max_y, resolution) + resolution / 2)[new_mins[1]:new_maxs[1]]
        else:  # get the cell boundaries for each cell, will be one longer than the node locations option (this is what matplotlib pcolormesh wants)
            x = np.arange(self.min_x, self.max_x, resolution)[new_mins[0]:new_maxs[0] + 1]
            y = np.arange(self.min_y, self.max_y, resolution)[new_mins[1]:new_maxs[1] + 1]
        return x, y, surf, valid_nodes, new_mins, new_maxs


class SRGrid(BathyGrid):
    """
    SRGrid is the basic implementation of the BathyGrid.  This class contains the metadata and other functions required
    to build and maintain the BathyGrid
    """
    def __init__(self, min_x: float = 0, min_y: float = 0, tile_size: float = 1024.0):
        super().__init__(min_x=min_x, min_y=min_y, tile_size=tile_size)
        self.can_grow = True

    def _convert_dataset(self):
        """
        We currently convert xarray Dataset input into a numpy structured array.  Xarry Datasets appear to be rather
        slow in testing, I believe because they do some stuff under the hood with matching coordinates when you do
        basic operations.  Also, you can't do any fancy indexing with xarray Dataset, at best you can use slice with isel.

        For all these reasons, we just convert to numpy.
        """
        allowed_vars = ['x', 'y', 'z', 'tvu', 'thu']
        dtyp = [(varname, self.data[varname].dtype) for varname in allowed_vars if varname in self.data]
        empty_struct = np.empty(len(self.data['x']), dtype=dtyp)
        for varname, vartype in dtyp:
            empty_struct[varname] = self.data[varname].values
        self.data = empty_struct

    def _update_metadata(self, container_name: str = None, file_list: list = None, epsg: int = None,
                         vertical_reference: str = None):
        """
        Update the bathygrid metadata for the new data

        Parameters
        ----------
        container_name
            the folder name of the converted data, equivalent to splitting the output_path variable in the kluster
            dataset
        file_list
            list of multibeam files that exist in the data to add to the grid
        epsg
            epsg (or proj4 string) for the coordinate system of the data.  Proj4 only shows up when there is no valid
            epsg
        vertical_reference
            vertical reference of the data
        """

        if file_list:
            self.container[container_name] = file_list
        else:
            self.container[container_name] = ['Unknown']

        if self.epsg and (self.epsg != int(epsg)):
            raise ValueError('BathyGrid: Found existing coordinate system {}, new coordinate system {} must match'.format(self.epsg,
                                                                                                                          epsg))
        if self.vertical_reference and (self.vertical_reference != vertical_reference):
            raise ValueError('BathyGrid: Found existing vertical reference {}, new vertical reference {} must match'.format(self.vertical_reference,
                                                                                                                            vertical_reference))
        self.epsg = int(epsg)
        self.vertical_reference = vertical_reference

    def _validate_input_data(self):
        """
        Ensure you get a structured numpy array as the input dataset.  If dataset is an Xarray Dataset, we convert it to
        Numpy for performance reasons.
        """

        if type(self.data) in [np.ndarray, Array]:
            if not self.data.dtype.names:
                raise ValueError('BathyGrid: numpy array provided for data, but no names were found, array must be a structured array')
            if 'x' not in self.data.dtype.names or 'y' not in self.data.dtype.names:
                raise ValueError('BathyGrid: numpy structured array provided for data, but "x" or "y" not found in variable names')
            self.layernames = [self.rev_layer_lookup[var] for var in self.data.dtype.names if var in ['z', 'tvu']]
        elif type(self.data) == xr.Dataset:
            if 'x' not in self.data:
                raise ValueError('BathyGrid: xarray Dataset provided for data, but "x" or "y" not found in variable names')
            if len(self.data.dims) > 1:
                raise ValueError('BathyGrid: xarray Dataset provided for data, but found multiple dimensions, must be one dimensional: {}'.format(self.data.dims))
            self.layernames = [self.rev_layer_lookup[var] for var in self.data if var in ['z', 'tvu']]
            self._convert_dataset()  # internally we just convert xarray dataset to numpy for ease of use
        else:
            raise ValueError('QuadTree: numpy structured array or dask array with "x" and "y" as variable must be provided')


class VRGridTile(SRGrid):
    """
    VRGridTile is a simple approach to variable resolution gridding.  We build a grid of BathyGrids, where each BathyGrid
    has a certain number of tiles (each tile with size subtile_size).  Each of those tiles can have a different resolution
    depending on depth.
    """

    def __init__(self, min_x: float = 0, min_y: float = 0, tile_size: float = 1024, subtile_size: float = 128):
        super().__init__(min_x=min_x, min_y=min_y, tile_size=tile_size)
        self.can_grow = True
        self.subtile_size = subtile_size

    def _build_tile(self, tile_x_origin: float, tile_y_origin: float):
        """
        For the VRGridTile class, the 'Tiles' are in fact BathyGrids, which contain their own tiles.  subtile_size controls
        the size of the Tiles within this BathyGrid.

        Parameters
        ----------
        tile_x_origin
            x origin coordinate for the tile, in the same units as the BathyGrid
        tile_y_origin
            y origin coordinate for the tile, in the same units as the BathyGrid

        Returns
        -------
        BathyGrid
            empty BathyGrid for this origin / tile size
        """
        return BathyGrid(min_x=tile_x_origin, min_y=tile_y_origin, max_x=tile_x_origin + self.tile_size,
                         max_y=tile_y_origin + self.tile_size, tile_size=self.subtile_size,
                         set_extents_manually=True)


def _gridding_parallel(data_blob: list):
    tile, algorithm, resolution, clear_existing = data_blob
    resolution = tile.grid(algorithm, resolution, clear_existing=clear_existing)
    return resolution, tile
