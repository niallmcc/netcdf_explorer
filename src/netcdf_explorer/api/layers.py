# MIT License
#
# Copyright (c) 2023-2024 University of Reading
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import math
import os
import shutil
import requests

from PIL import Image
from matplotlib import cm
import numpy as np
import xarray as xr

from .data_encoder import DataEncoder

from .colours import colours_to_rgb, ColoursToRGB
from .config_error import ConfigError, require

def save_image(arr,vmin,vmax,path,cmap_name="coolwarm"):
    if not hasattr(cm,cmap_name):
        raise ValueError("Unknown colour map: " + cmap_name)
    cmap_fn = getattr(cm,cmap_name)
    im = Image.fromarray(np.uint8((255*cmap_fn((arr-vmin)/(vmax-vmin)))))
    im.save(path)

def save_image_falsecolour(data_red, data_green, data_blue, path, min_value=None, max_value=None, low_percentile=None,
                           high_percentile=None, red_gamma=0.5, green_gamma=0.5, blue_gamma=0.5):
    alist = []

    combined = np.concatenate((data_red,data_green,data_blue))
    low_val = 0
    high_val = 1
    if min_value is not None or max_value is not None:
        if min_value is not None:
            low_val = min_value
        if max_value is not None:
            high_val = max_value
    elif low_percentile is not None or high_percentile is not None:
        if low_percentile is None:
            low_percentile = 0
        if high_percentile is None:
            high_percentile = 100
        low_val, high_val = np.nanpercentile(combined, [low_percentile, high_percentile])
    for (arr,gamma) in [(data_red,red_gamma),(data_green,green_gamma),(data_blue,blue_gamma)]:
        v = np.clip(arr, low_val, high_val)
        v = (v - low_val) / (high_val - low_val)
        v = np.where(np.isnan(v), np.nan, np.power(np.where(np.isnan(v),0,v), gamma))
        # convert to pixel values
        v = v * 255
        v = np.where(np.isnan(v),0, v)
        alist.append(v.astype(np.uint8))
    arr = np.stack(alist,axis=-1)
    im = Image.fromarray(arr)
    im.save(path)

def save_image_mask(arr, path, r, g, b):
    alist = []
    a = np.zeros(arr.shape)
    alist.append((a + r).astype(np.uint8))
    alist.append((a + g).astype(np.uint8))
    alist.append((a + b).astype(np.uint8))
    alist.append(np.where(arr>0,255,0).astype(np.uint8))
    rgba_arr = np.stack(alist, axis=-1)
    im = Image.fromarray(rgba_arr)
    im.save(path)

def save_image_discrete(arr,path,values):
    lookup = {}
    for (k,v) in values.items():
        (label,colour) = v
        if not isinstance(k,str):
            k = int(k)
        lookup[k] = ColoursToRGB.lookup(colour)+[255]

    def get_rgba(value):
        if isinstance(value,str):
            key = value
        else:
            key = int(value) if not math.isnan(value) else -1
        if key not in lookup:
            return np.array([0,0,0,255])
        else:
            return np.array(lookup[key])

    im = Image.fromarray(np.uint8((np.vectorize(get_rgba,signature='()->(n)')(arr))),mode="RGBA")
    im.save(path)

class LayerCommon:
    """Settings shared by every layer and layer group: name, label, group and which views it appears in."""

    def __init__(self, converter, layer_name, layer_label):
        self.converter = converter
        self.layer_name = layer_name
        self.layer_label = layer_label
        self.group = None
        self.case_wise = False
        self.grid_view = False
        self.overlay_view = False

    def set_group(self, group):
        self.group = group

    def get_group(self):
        return self.group

    def set_case_wise(self, is_case_wise):
        self.case_wise = is_case_wise

    def get_case_wise(self):
        return self.case_wise

    def set_grid_view(self, grid_view):
        self.grid_view = grid_view

    def get_grid_view(self):
        return self.grid_view

    def set_overlay_view(self, overlay_view):
        self.overlay_view = overlay_view

    def get_overlay_view(self):
        return self.overlay_view

    def save_data(self):
        return False

    def get_sublayers(self):
        return None


class LayerGroup(LayerCommon):

    def __init__(self, layer, converter, layer_name, layer_label, sublayers):
        super().__init__(converter, layer_name, layer_label)
        self.layer = layer
        self.sublayers = sublayers

    def has_legend(self):
        return self.sublayers[0].has_legend()

    def build_legend(self, path):
        return self.sublayers[0].build_legend(path)

    def check(self, ds):
        remove_layers = []
        for layer in self.sublayers:
            err = layer.check(ds)
            if err:
                self.converter.logger.warning(f"Unable to add layer {layer.layer_name}: {err}")
                remove_layers.append(layer)

        for layer in remove_layers:
            self.sublayers.remove(layer)

        if len(self.sublayers) == 0:
            return "No valid layers in this group"

    def build(self,ds,path):
        for layer in self.sublayers:
            layer.build(ds,path)

    def get_layers(self):
        return self.sublayers

    def get_sublayers(self):
        return self.sublayers

class LayerBase(LayerCommon):

    def __init__(self, layer, converter, layer_name, layer_label, selectors=None):
        super().__init__(converter, layer_name, layer_label)
        self.selectors = selectors if selectors is not None else {}
        self.flipud = False
        self.fliplr = False
        # a layer may set its own x and y dimensions and coordinates (see LayerFactory.check_layer_dimensions);
        # the case dimension and the time coordinate always come from the top-level configuration
        self.case_dimension = converter.case_dimension
        self.x_dimension = layer.get("dimensions",{}).get("x",converter.x_dimension)
        self.y_dimension = layer.get("dimensions",{}).get("y",converter.y_dimension)
        self.time_coordinate = converter.time_coordinate
        self.x_coordinate = layer.get("coordinates",{}).get("x",converter.x_coordinate)
        self.y_coordinate = layer.get("coordinates",{}).get("y",converter.y_coordinate)

    def get_coords(self, ds, coordinate_name):
        """Return this layer's coordinate variable, taken from the first case if it varies from case to case."""
        coords = ds[coordinate_name]
        if self.case_dimension in coords.dims:
            return coords.isel(**{self.case_dimension: 0}).squeeze()
        return coords

    def check(self, ds):
        for variable in [self.x_coordinate, self.y_coordinate, self.time_coordinate]:
            if variable and variable not in ds:
                return f"No variable {variable}"
        xc = self.get_coords(ds, self.x_coordinate)
        yc = self.get_coords(ds, self.y_coordinate)
        if len(xc.shape) != 1:
            return f"x_coordinate {self.x_coordinate} must be 1-dimensional"
        if len(yc.shape) != 1:
            return f"y_coordinate {self.y_coordinate} must be 1-dimensional"

        if float(xc.data[0]) > float(xc.data[-1]):
            self.fliplr = True
        if float(yc.data[0]) < float(yc.data[-1]):
            self.flipud = True

    def check_band(self, ds, band_name):
        """Return an error message if band_name is not in ds, otherwise record whether it varies from case to case."""
        if band_name not in ds:
            return f"No variable {band_name}"
        # apart from the case dimension, selected dimensions and any of length 1 (which get_data removes),
        # the variable can only have the layer's x and y dimensions
        dims = ds[band_name].dims
        other_dims = [dim for dim in dims if dim != self.case_dimension and dim not in self.selectors
                      and ds.sizes[dim] > 1 and dim not in (self.x_dimension, self.y_dimension)]
        if other_dims:
            return (f"Variable {band_name} has dimensions ({', '.join(dims)}) but the layer's x and y dimensions "
                    f"are {self.x_dimension} and {self.y_dimension}")
        if self.case_dimension and self.case_dimension in ds[band_name].dims:
            self.set_case_wise(True)
        return None

    def get_data(self, da):
        if self.selectors:
            da = da.isel(**self.selectors)
        da = da.squeeze()
        ndims = len(da.dims)

        # data that varies along only x or y is repeated along the other dimension to make an image
        if ndims == 1:
            shape = (self.converter.data_height, self.converter.data_width)
            if da.dims[0] == self.y_dimension:
                da = xr.DataArray(
                    np.broadcast_to(da.data[None].T, shape),
                    dims=(self.y_dimension, self.x_dimension))
            elif da.dims[0] == self.x_dimension:
                da = xr.DataArray(
                    np.broadcast_to(da.data, shape), dims=(self.y_dimension, self.x_dimension))
            ndims = len(da.dims)
        if ndims != 2:
            raise Exception(f"Data for layer {self.layer_name} is not 2D")

        x_index = da.dims.index(self.x_dimension)
        y_index = da.dims.index(self.y_dimension)
        arr = da.data
        if y_index > x_index:
            arr = np.transpose(arr)
        if self.flipud:
            arr = np.flipud(arr)
        if self.fliplr:
            arr = np.fliplr(arr)
        return arr

class LayerRGB(LayerBase):

    def __init__(self, layer, converter, layer_name, layer_label, selectors, red_variable, green_variable,
                 blue_variable, red_gamma=0.5, green_gamma=0.5, blue_gamma=0.5, min_value=None, max_value=None,
                 low_percentile=None, high_percentile=None):
        super().__init__(layer, converter, layer_name, layer_label, selectors)
        self.red_variable = red_variable
        self.green_variable = green_variable
        self.blue_variable = blue_variable
        self.red_gamma = red_gamma
        self.green_gamma = green_gamma
        self.blue_gamma = blue_gamma
        self.min_value = min_value
        self.max_value = max_value
        self.low_percentile = low_percentile
        self.high_percentile = high_percentile

    def has_legend(self):
        return False

    def check(self, ds):
        err = super().check(ds)
        if err:
            return err
        for variable in [self.red_variable, self.green_variable, self.blue_variable]:
            err = self.check_band(ds, variable)
            if err:
                return err

    def build(self,ds,path):
        red = self.get_data(ds[self.red_variable])
        green = self.get_data(ds[self.green_variable])
        blue = self.get_data(ds[self.blue_variable])
        save_image_falsecolour(red, green, blue, path, red_gamma=self.red_gamma,
                               green_gamma=self.green_gamma, blue_gamma=self.blue_gamma,
                               min_value=self.min_value, max_value=self.max_value,
                               low_percentile=self.low_percentile, high_percentile=self.high_percentile)


class LayerSingleBand(LayerBase):

    def __init__(self, layer, converter, layer_name, layer_label, selectors, band_name, vmin, vmax, cmap_name, data):
        super().__init__(layer, converter, layer_name, layer_label, selectors)
        self.band_name = band_name
        self.vmin = vmin
        self.vmax = vmax
        self.cmap_name = cmap_name
        self.data = data

    def get_cmap(self):
        return self.cmap_name

    def check(self, ds):
        err = super().check(ds)
        if err:
            return err
        return self.check_band(ds, self.band_name)

    def build(self,ds,path):
        save_image(self.get_data(ds[self.band_name]), self.vmin, self.vmax, path, self.cmap_name)

    def has_legend(self):
        return True

    def build_legend(self, path):
        legend_width, legend_height = 200, 20
        a = np.zeros(shape=(legend_height,legend_width))
        for i in range(0,legend_width):
            a[:,i] = self.vmin + (i/legend_width) * (self.vmax-self.vmin)
        save_image(a, self.vmin, self.vmax, path, self.cmap_name)

    def save_data(self):
        return self.data is not None

    def build_data(self,ds,path):
        de = DataEncoder()
        de.encode(self.get_data(ds[self.band_name]), path)
        return self.data

class LayerWMS(LayerBase):

    def __init__(self, layer, converter, layer_name, layer_label, wms_url, scale):
        super().__init__(layer, converter, layer_name, layer_label)
        self.wms_url = wms_url
        self.cache = {}
        self.failed = set()
        self.scale = scale

    def check(self, ds):
        err = super().check(ds)
        if err:
            return err
        # the map area changes from case to case if the coordinates do
        if self.case_dimension and (self.case_dimension in ds[self.x_coordinate].dims
                                    or self.case_dimension in ds[self.y_coordinate].dims):
            self.set_case_wise(True)

    def has_legend(self):
        return False

    def get_bounds(self,ds):
        xc = self.get_coords(ds, self.x_coordinate)
        yc = self.get_coords(ds, self.y_coordinate)

        spacing_x = abs(float(xc[0]) - float(xc[1]))
        spacing_y = abs(float(yc[0]) - float(yc[1]))

        x_min = float(xc.min()) - spacing_x/2
        x_max = float(xc.max()) + spacing_x/2
        y_min = float(yc.min()) - spacing_y/2
        y_max = float(yc.max()) + spacing_y/2
        return ((x_min,y_min),(x_max,y_max))

    def build(self,ds,path):
        if os.path.exists(path):
            os.remove(path)
        image_width, image_height = self.converter.get_image_dimensions(ds)
        image_width *= self.scale
        image_height *= self.scale

        ((x_min, y_min), (x_max, y_max)) = self.get_bounds(ds)

        url = self.wms_url.replace("{WIDTH}",str(image_width)).replace("{HEIGHT}",str(image_height)) \
            .replace("{YMIN}",str(y_min)).replace("{YMAX}",str(y_max)) \
            .replace("{XMIN}",str(x_min)).replace("{XMAX}", str(x_max))

        if url in self.cache:
            shutil.copyfile(self.cache[url],path)
        elif url in self.failed:
            pass
        else:
            r = requests.get(url, stream=True)
            if r.status_code == 200:
                with open(path, 'wb') as f:
                    r.raw.decode_content = True
                    shutil.copyfileobj(r.raw, f)
                self.cache[url] = path
            else:
                self.failed.add(url)

class LayerMask(LayerBase):

    def __init__(self, layer, converter, layer_name, layer_label, selectors, band_name, r, g, b):
        super().__init__(layer, converter, layer_name, layer_label, selectors)
        self.band_name = band_name
        self.r = r
        self.g = g
        self.b = b

    def has_legend(self):
        return False

    def check(self, ds):
        err = super().check(ds)
        if err:
            return err
        return self.check_band(ds, self.band_name)

    def build(self,ds,path):
        save_image_mask(self.get_data(ds[self.band_name].astype(int)), path, self.r, self.g, self.b)

class LayerDiscrete(LayerBase):

    def __init__(self, layer, converter, layer_name, layer_label, selectors, band_name, values):
        super().__init__(layer, converter, layer_name, layer_label, selectors)
        self.band_name = band_name
        self.values = values

    def check(self, ds):
        err = super().check(ds)
        if err:
            return err
        for (_label, color) in self.values.values():
            if ColoursToRGB.lookup(color) is None:
                return f"Invalid colour {color}"
        return self.check_band(ds, self.band_name)

    def build(self,ds,path):
        save_image_discrete(self.get_data(ds[self.band_name]), path, self.values)

    def has_legend(self):
        return False

class LayerStaticImage(LayerBase):

    def __init__(self, layer, converter, layer_name, layer_label, selectors, image_path):
        super().__init__(layer, converter, layer_name, layer_label, selectors)
        self.image_path = image_path

    def has_legend(self):
        return False

    def check(self, ds):
        return None

    def build(self,ds,path):
        shutil.copyfile(self.image_path, path)

class LayerFactory:

    @staticmethod
    def check_layer_dimensions(layer_name, layer):
        """Raise ConfigError for per-layer dimension or coordinate settings that are not supported.

        A layer may set its own x and y dimensions and x and y coordinates, for a variable whose dimensions
        or coordinates are named differently, or which is stored in a different orientation. The case dimension
        and the time coordinate can only be set at the top level, because they decide how the data is split
        into cases.
        """
        for (setting, example, top_level_only) in [("dimensions", "{x: xx, y: yy}", "case dimension"),
                                                   ("coordinates", "{x: lon, y: lat}", "time coordinate")]:
            values = layer.get(setting, {})
            if not isinstance(values, dict):
                raise ConfigError(f"layer '{layer_name}' setting '{setting}' should be a mapping, "
                                  f"for example {example}")
            for key in values:
                if key not in ("x", "y"):
                    raise ConfigError(f"layer '{layer_name}' cannot set {setting} '{key}', a layer can only set its "
                                      f"x and y {setting} (the {top_level_only} can only be set at the top level)")

    @staticmethod
    def create(converter, layer_name, layer, in_layer_group=False):
        """Create a layer (or layer group) from its configuration settings.

        Raises:
            ConfigError: if the settings are not valid
        """
        if not isinstance(layer, dict):
            raise ConfigError(f"layer '{layer_name}' should be a mapping of settings, got {type(layer).__name__}")
        layer_type = require(layer, "type", "layer", layer_name)
        LayerFactory.check_layer_dimensions(layer_name, layer)
        if layer_type not in LayerFactory.CREATORS:
            raise ConfigError(f"layer '{layer_name}' has unknown type '{layer_type}', should be one of: "
                              + ", ".join(LayerFactory.CREATORS))
        layer_label = layer.get("label", layer_name)
        selectors = layer.get("selectors", {})
        creator = LayerFactory.CREATORS[layer_type]
        created_layer = creator(converter, layer_name, layer_label, selectors, layer, in_layer_group)
        if not in_layer_group:
            created_layer.set_grid_view(layer.get("grid_view", True))
            created_layer.set_overlay_view(layer.get("overlay_view", True))
        return created_layer

    # each of the following creates one type of layer from its settings:
    # (converter, layer_name, layer_label, selectors, layer, in_layer_group) -> layer

    @staticmethod
    def create_single(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        layer_band = layer.get("band", layer_name)
        vmin = require(layer, "min_value", "layer", layer_name)
        vmax = require(layer, "max_value", "layer", layer_name)
        cmap = layer.get("cmap", "coolwarm")
        if not hasattr(cm, cmap):
            raise ConfigError(f"layer '{layer_name}' has unknown cmap '{cmap}', "
                              f"should be the name of a matplotlib colour map")
        data = layer.get("data", None)
        return LayerSingleBand(layer, converter, layer_name, layer_label, selectors, layer_band, vmin, vmax, cmap, data)

    @staticmethod
    def create_mask(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        layer_band = layer.get("band", layer_name)
        if "colour" in layer:
            colour = layer["colour"]
            if colour not in colours_to_rgb:
                raise ConfigError(f"layer '{layer_name}' has unknown colour '{colour}'")
            rgb = colours_to_rgb[colour]
            (r, g, b) = (rgb["r"], rgb["g"], rgb["b"])
        else:
            (r, g, b) = (layer.get("r",0), layer.get("g",0), layer.get("b",0))
        return LayerMask(layer, converter, layer_name, layer_label, selectors, layer_band, r, g, b)

    @staticmethod
    def create_rgb(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        return LayerRGB(layer, converter, layer_name, layer_label, selectors,
                        red_variable=require(layer, "red_band", "layer", layer_name),
                        green_variable=require(layer, "green_band", "layer", layer_name),
                        blue_variable=require(layer, "blue_band", "layer", layer_name),
                        red_gamma=layer.get("red_gamma",0.5), green_gamma=layer.get("green_gamma",0.5),
                        blue_gamma=layer.get("blue_gamma",0.5),
                        min_value=layer.get("min_value"), max_value=layer.get("max_value"),
                        low_percentile=layer.get("low_percentile"), high_percentile=layer.get("high_percentile"))

    @staticmethod
    def create_discrete(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        layer_band = layer.get("band", layer_name)
        values = require(layer, "values", "layer", layer_name)
        return LayerDiscrete(layer, converter, layer_name, layer_label, selectors, layer_band, values)

    @staticmethod
    def create_wms(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        url = require(layer, "url", "layer", layer_name)
        return LayerWMS(layer, converter, layer_name, layer_label, url, layer.get("scale", 1))

    @staticmethod
    def create_static_image(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        image_path = require(layer, "image_path", "layer", layer_name)
        return LayerStaticImage(layer, converter, layer_name, layer_label, selectors, image_path)

    @staticmethod
    def create_layer_group(converter, layer_name, layer_label, selectors, layer, in_layer_group):
        if in_layer_group:
            raise ConfigError(f"layer group '{layer_name}' cannot be nested inside another layer group")
        sublayers = []
        for (sublayer_name, sublayer) in layer.get("layers",{}).items():
            sublayers.append(LayerFactory.create(converter,layer_name+"_"+sublayer_name,sublayer,in_layer_group=True))
        if len(sublayers) == 0:
            raise ConfigError(f"layer group '{layer_name}' should contain at least one layer")
        created_layer = LayerGroup(layer, converter, layer_name, layer_label, sublayers)
        for sublayer in sublayers:
            sublayer.set_group(created_layer)
        return created_layer


# the function that creates each type of layer, keyed by the layer's "type" setting
LayerFactory.CREATORS = {
    "single": LayerFactory.create_single,
    "mask": LayerFactory.create_mask,
    "rgb": LayerFactory.create_rgb,
    "discrete": LayerFactory.create_discrete,
    "wms": LayerFactory.create_wms,
    "static_image": LayerFactory.create_static_image,
    "layer_group": LayerFactory.create_layer_group,
}
