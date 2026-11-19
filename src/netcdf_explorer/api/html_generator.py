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

import csv
import datetime
import math
import os
import sys

import xarray as xr
import shutil
import json
import numpy as np
from mako.template import Template
import pyproj
import logging

from .colours import ColoursToRGB

from .layers import LayerFactory, LayerSingleBand, LayerWMS, LayerDiscrete
from .expr_parser import ExpressionParser
from .config_error import ConfigError, require

AGGREGATION_FUNCTIONS = ("mean", "min", "max", "median")
TIMESERIES_TYPES = ("timeseries", "seasonal")

from netcdf_explorer.htmlfive.html5_builder import Html5Builder, ElementFragment

from netcdf_explorer.fragments.utils import anti_aliasing_style
from netcdf_explorer.fragments.image import ImageFragment
from netcdf_explorer.fragments.table import TableFragment
from netcdf_explorer.fragments.legend import LegendFragment
from netcdf_explorer.fragments.select import SelectFragment

src_folder = os.path.split(__file__)[0]

js_paths = [os.path.join(src_folder, "cmap.js"),
            os.path.join(src_folder, "data_image.js"),
            os.path.join(src_folder, "leaflet_map.js"),
            os.path.join(src_folder, "timeseries_chart.js"),
            os.path.join(src_folder, "terrain_view.js"),
            os.path.join(src_folder, "html_view.js")]

css_path = os.path.join(src_folder, "index.css")

dygraph_dependency_paths = [os.path.join(src_folder,"..","dependencies","dygraph.css"),
                    os.path.join(src_folder,"..","dependencies","dygraph.js")]

babylonjs_dependency_paths = [os.path.join(src_folder,"..","dependencies","babylon.js"),
                    os.path.join(src_folder,"..","dependencies","babylonTerrain.js")]

class Progress:

    def __init__(self, label, enabled=True):
        self.label = label
        self.enabled = enabled
        self.last_progress_frac = None

    def report(self,msg,progress_frac):
        if not self.enabled:
            return
        if self.last_progress_frac is None or (progress_frac - self.last_progress_frac) >= 0.01:
            self.last_progress_frac = progress_frac
            i = int(100*progress_frac)
            if i > 100:
                i = 100
            si = i // 2
            sys.stdout.write("\r%s %s %-05s %s" % (self.label,msg,str(i)+"%","#"*si))
            sys.stdout.flush()

    def complete(self,msg):
        if not self.enabled:
            return
        self.report(msg, 1)
        sys.stdout.write("\n")
        sys.stdout.flush()


class HTMLGenerator:

    def __init__(self, config, input_path, output_folder, title,
                 add_download_link=True, filter_controls=False, sample_count=None, sample_cases=None,
                 show_progress=True):
        """Read and check the configuration and input data.

        No output is written until run() is called, so a configuration error leaves the output folder untouched.
        """
        self.logger = logging.getLogger("generate_html")
        self.show_progress = show_progress

        self.input_path = input_path
        self.output_folder = output_folder
        self.output_html_path = os.path.join(output_folder, "index.html")
        self.title = title
        self.add_download_link = add_download_link
        self.download_filename = ""  # set by run() once the download link has been created
        self.filter_controls = filter_controls
        self.min_timestamp = self.max_timestamp = None # datetime in "YYYY-MM-DDTHH:MM:SS" format

        self.read_config(config)

        self.input_ds = self.select_cases(xr.open_dataset(input_path), sample_count, sample_cases)
        self.data_width = self.input_ds.sizes[self.x_dimension] if self.x_dimension else None
        self.data_height = self.input_ds.sizes[self.y_dimension] if self.y_dimension else None

        self.parser = self.create_expression_parser()
        self.build_derived_bands()

        if self.x_coordinate:
            self.input_ds = self.reduce_coordinate_dimension(self.input_ds, self.x_coordinate, self.case_dimension)
        if self.y_coordinate:
            self.input_ds = self.reduce_coordinate_dimension(self.input_ds, self.y_coordinate, self.case_dimension)

        self.layer_definitions = self.create_layers(config)
        self.timeseries_definitions = self.create_timeseries(config)

        # filled in by run()
        self.layer_images = []
        self.layer_data = []
        self.layer_legends = {}

        self.all_cmaps = sorted(["Purples", "gist_rainbow", "gist_ncar", "Blues", "Greys", "autumn", "gist_gray", "magma", "Set3",
                     "cool", "tab20c", "GnBu", "brg", "cividis", "Pastel1", "YlOrRd", "Spectral", "gist_earth", "PuBu",
                     "OrRd", "PuRd", "plasma", "winter", "PuBuGn", "inferno", "bwr", "RdGy", "Wistia", "gist_stern",
                     "gist_heat", "BuGn", "twilight", "RdBu", "twilight_shifted", "Paired", "PiYG", "RdYlBu", "Dark2",
                     "CMRmap", "BuPu", "gnuplot", "PRGn", "nipy_spectral", "ocean", "viridis", "bone", "BrBG",
                     "gnuplot2", "Oranges", "turbo", "YlGn", "PuOr", "hot", "Set2", "afmhot", "hsv", "YlOrBr",
                     "terrain", "Accent", "copper", "cubehelix", "RdPu", "tab10", "Reds", "Greens", "gray", "rainbow",
                     "spring", "tab20", "pink", "coolwarm", "RdYlGn", "Set1", "tab20b", "flag", "gist_yarg", "binary",
                     "YlGnBu", "seismic", "prism", "Pastel2", "jet", "summer"], key=lambda v:v.lower())

    def read_config(self, config):
        """Read the top-level settings from the configuration."""
        dimensions = config.get("dimensions", {})
        coordinates = config.get("coordinates", {})
        image = config.get("image", {})

        self.case_dimension = dimensions.get("case", "")
        self.x_dimension = dimensions.get("x", "")
        self.y_dimension = dimensions.get("y", "")
        self.x_coordinate = coordinates.get("x", "")
        self.y_coordinate = coordinates.get("y", "")
        self.time_coordinate = coordinates.get("time", "")

        self.max_zoom = image.get("max-zoom", None)
        self.grid_image_width = image.get("grid-width", None)

        self.info = config.get("info",{})
        self.crs = config.get("crs",None)
        self.timeseries = config.get("timeseries",{})
        self.derive_bands = config.get("derive_bands",{})
        self.terrain_view = config.get("terrain_view",{})

    def select_cases(self, ds, sample_count, sample_cases):
        """Return ds restricted to the requested cases: either the given indexes or a random sample."""
        if sample_count is None and sample_cases is None:
            return ds

        n = len(ds[self.case_dimension])
        selected_indexes = list(range(n))

        if sample_cases:
            selected_indexes = sample_cases
        else:
            if sample_count and sample_count < n:
                selected_indexes = np.random.choice(np.array(selected_indexes), sample_count,
                                                replace=False).tolist()
        if sample_cases or sample_count:
            ds = ds.isel(**{self.case_dimension: selected_indexes})
        return ds

    @staticmethod
    def create_expression_parser():
        """Create the parser used to read derive_bands expressions."""
        parser = ExpressionParser()
        parser.add_unary_operator("not")
        parser.add_binary_operator("*", 5)
        parser.add_binary_operator("/", 5)
        parser.add_binary_operator("+", 4)
        parser.add_binary_operator("-", 4)
        parser.add_binary_operator("|", 3)
        parser.add_binary_operator("&", 3)
        parser.add_binary_operator("==",2)
        parser.add_binary_operator("and",1)
        parser.add_binary_operator("or",1)
        return parser

    def create_layers(self, config):
        """Create a layer definition for each entry in the configuration's layers section."""
        return [LayerFactory.create(self, layer_name, layer_spec)
                for (layer_name, layer_spec) in config.get("layers", {}).items()]

    def create_timeseries(self, config):
        """Check each timeseries in the configuration, keeping only variables and masks found in the input data.

        Returns a list of (timeseries_name, timeseries_spec, timeseries_detail) tuples.
        """
        timeseries_definitions = []
        for (timeseries_name, timeseries_spec) in config.get("timeseries", {}).items():

            self.check_timeseries_spec(timeseries_name, timeseries_spec)

            valid_variables = []
            for variable in timeseries_spec["variables"]:
                variable_components = variable.split(":")
                variable_name = variable_components[0]
                if variable_name in self.input_ds:
                    valid_variables.append(variable)

            if len(valid_variables) == 0:
                self.logger.warning(f"Unable to add timeseries {timeseries_name}, no valid variables")
                continue
            else:
                timeseries_spec["variables"] = valid_variables

            mask_names = timeseries_spec.get("masks",[])
            if len(mask_names) > 0:
                valid_masks = []
                for mask_name in timeseries_spec.get("masks",[]):
                    if mask_name in self.input_ds:
                        valid_masks.append(mask_name)
                if len(valid_masks) == 0:
                    self.logger.warning(f"Unable to add timeseries {timeseries_name}, no valid masks")
                    continue
                else:
                    timeseries_spec["masks"] = valid_masks

            timeseries_definitions.append((timeseries_name, timeseries_spec, {}))
        return timeseries_definitions

    def write_static_files(self):
        """Create the output folders and copy in the javascript, css, colour maps and libraries the page uses.

        Also creates the download link to the input data, if one was requested.
        """
        os.makedirs(self.output_folder, exist_ok=True)

        if self.add_download_link:
            self.download_filename = os.path.split(self.input_path)[-1]
            download_link_path = os.path.join(self.output_folder, self.download_filename)
            if not os.path.exists(download_link_path):
                os.symlink(self.input_path, download_link_path)
            else:
                self.logger.warning(f"Download path {download_link_path} already exists, unable to create new download link.  Data will not be linked in output.")
                self.download_filename = ""

        for folder in ["images", "data", "cmaps"]:
            os.makedirs(os.path.join(self.output_folder, folder), exist_ok=True)

        js_code = ""
        for js_path in js_paths:
            with open(js_path) as f:
                js_code += f.read()

        with open(os.path.join(self.output_folder, "index.js"), "w") as f:
            f.write(js_code)

        shutil.copyfile(css_path, os.path.join(self.output_folder, "index.css"))

        # copy in dependencies
        dependency_folder = os.path.join(self.output_folder, "dependencies")
        os.makedirs(dependency_folder, exist_ok=True)

        if self.timeseries:
            for dependency_path in dygraph_dependency_paths:
                filename = os.path.split(dependency_path)[-1]
                shutil.copyfile(dependency_path,os.path.join(dependency_folder, filename))

        if self.terrain_view:
            for dependency_path in babylonjs_dependency_paths:
                filename = os.path.split(dependency_path)[-1]
                shutil.copyfile(dependency_path,os.path.join(dependency_folder, filename))

        cmap_folder = os.path.join(self.output_folder, "cmaps")
        for cmap in self.all_cmaps:
            source_path = os.path.join(os.path.abspath(os.path.split(__file__)[0]),"..","misc","cmaps", cmap+".json")
            dest_path = os.path.join(cmap_folder, cmap + ".json")
            shutil.copyfile(source_path, dest_path)

    def flatten_layers(self, layer_definitions, only_grid_view=False, only_overlay_view=False):
        flattened_layers = []
        for layer in layer_definitions:
            if only_grid_view and not layer.get_grid_view():
                continue
            if only_overlay_view and not layer.get_overlay_view():
                continue
            if layer.get_sublayers() is not None:
                flattened_layers += layer.get_sublayers()
            else:
                flattened_layers.append(layer)
        return flattened_layers

    @staticmethod
    def check_timeseries_spec(timeseries_name, timeseries_spec):
        """Raise ConfigError if a timeseries specification is invalid, before any work is done."""
        variables = require(timeseries_spec, "variables", "timeseries", timeseries_name)
        if not isinstance(variables, list) or len(variables) == 0:
            raise ConfigError(f"timeseries '{timeseries_name}' setting 'variables' should be a non-empty list")
        for variable in variables:
            variable_components = variable.split(":")
            if len(variable_components) > 1 and variable_components[1] not in AGGREGATION_FUNCTIONS:
                raise ConfigError(f"timeseries '{timeseries_name}' variable '{variable}' uses unknown aggregation function "
                                  f"'{variable_components[1]}', should be one of: {', '.join(AGGREGATION_FUNCTIONS)}")
        timeseries_type = timeseries_spec.get("type", "timeseries")
        if timeseries_type not in TIMESERIES_TYPES:
            raise ConfigError(f"timeseries '{timeseries_name}' has unknown type '{timeseries_type}', "
                              f"should be one of: {', '.join(TIMESERIES_TYPES)}")
        if timeseries_type == "seasonal" and len(variables) != 1:
            raise ConfigError(f"timeseries '{timeseries_name}' is seasonal, so it needs exactly 1 variable, got {len(variables)}")

    def build_derived_bands(self):
        for (band_name, band_expression) in self.derive_bands.items():
            from_bands = []
            try:
                parsed_expression = self.parser.parse(band_expression)
            except ExpressionParser.ParseError as ex:
                raise ConfigError(f"derived band '{band_name}' has an invalid expression '{band_expression}': "
                                  f"{ex.error_type} at position {ex.error_position}") from ex
            try:
                arr = self.evaluate_expression(parsed_expression, from_bands)
            except Exception as ex:
                self.logger.warning(f"Unable to derive band {band_name}: {str(ex)}")
                continue
            dims = []
            # the dimensions of the derived band should match the longest dimensions of the input bands
            for band in from_bands:
                if len(self.input_ds[band].dims) > len(dims):
                    dims = self.input_ds[band].dims
            self.input_ds[band_name] = xr.DataArray(arr,dims=dims)

    def evaluate_expression(self, parsed_expression, from_bands_accumulator):
        if "name" in parsed_expression:
            source_band = parsed_expression["name"]
            if source_band in self.input_ds:
                from_bands_accumulator.append(source_band)
                return self.input_ds[source_band].data
            else:
                raise Exception(f"Expression references unknown band: {source_band}")
        elif "operator" in parsed_expression:
            operator = parsed_expression["operator"]
            args = parsed_expression["args"]
            arrays = []
            for arg in args:
                arrays.append(self.evaluate_expression(arg,from_bands_accumulator))
            if operator == "==":
                assert len(args) == 2
                return np.equal(arrays[0],arrays[1])
            elif operator == "&":
                assert len(args) == 2
                return np.bitwise_and(np.astype(arrays[0],int),np.astype(arrays[1],int))
            elif operator == "|":
                assert len(args) == 2
                return np.bitwise_or(np.astype(arrays[0],int), np.astype(arrays[1],int))
            elif operator == "and":
                assert len(args) == 2
                return np.logical_and(np.astype(arrays[0],int),np.astype(arrays[1],int))
            elif operator == "or":
                assert len(args) == 2
                return np.logical_or(np.astype(arrays[0],int), np.astype(arrays[1],int))
            elif operator == "not":
                assert(len(args) == 1)
                return np.logical_not(arrays[0])
            elif operator == "*":
                assert len(args) == 2
                return np.multiply(arrays[0],arrays[1])
            elif operator == "/":
                assert len(args) == 2
                return np.divide(arrays[0],arrays[1])
            elif operator == "+":
                assert len(args) == 2
                return np.add(arrays[0],arrays[1])
            elif operator == "-":
                assert len(args) == 2
                return np.subtract(arrays[0], arrays[1])
            else:
                raise Exception(f"Unknown operator: {operator}")
        elif "literal" in parsed_expression:
            return np.array([parsed_expression["literal"]])

    def reduce_coordinate_dimension(self, ds, coordinate_name, case_dimension):
        dims = ds[coordinate_name].dims
        ndims = len(dims)
        if case_dimension in dims:
            ndims -= 1
        if ndims == 1:
            return ds  # already 1-dimensional
        if ndims == 2:
            if case_dimension in dims:
                raise Exception(f"Unable to make spatial coordinate {coordinate_name} 1-dimensional")

            # coordinate is 2-dimensional, see if it can be reduced to 1-dimensional
            da = ds[coordinate_name]
            arr = da.data
            if np.all(arr[0, :] == arr[-1, :]):
                # assume all rows are identical if first and last rows are
                ds[coordinate_name] = xr.DataArray(arr[0, :], dims=(da.dims[1],), attrs=da.attrs)
                return ds
            if np.all(arr[:, 0] == arr[:, -1]):
                # assume all columns are identical if first and last columns are
                ds[coordinate_name] = xr.DataArray(arr[:, 0], dims=(da.dims[0],), attrs=da.attrs)
                return ds
        raise Exception(f"Unable to make spatial coordinate {coordinate_name} 1-dimensional")

    def get_image_path(self, key, index=None):
        if index is not None:
            filename = f"{key}_{index}.png"
        else:
            filename = f"{key}.png"
        src = os.path.join("images", filename)
        path = os.path.join(self.output_folder, src)
        return (src, path)

    def get_data_path(self, key, index=None):
        if index is not None:
            filename = f"{key}_{index}.gz"
        else:
            filename = f"{key}.gz"
        src = os.path.join("data", filename)
        path = os.path.join(self.output_folder, src)
        return (src, path)

    def get_x_coords(self, ds, for_case=0):
        x_dims = ds[self.x_coordinate].dims
        if self.case_dimension in x_dims:
            return ds[self.x_coordinate].isel(**{self.case_dimension: for_case}).squeeze()
        else:
            return ds[self.x_coordinate]

    def get_y_coords(self, ds, for_case=0):
        y_dims = ds[self.y_coordinate].dims
        if self.case_dimension in y_dims:
            return ds[self.y_coordinate].isel(**{self.case_dimension: for_case}).squeeze()
        else:
            return ds[self.y_coordinate]

    def get_image_dimensions(self, ds):
        x_coords = self.get_x_coords(ds)
        y_coords = self.get_y_coords(ds)
        if len(x_coords.shape) == 1 and len(y_coords.shape) == 1:
            image_height = y_coords.shape[0]
            image_width = x_coords.shape[0]
            return (image_width, image_height)
        else:
            raise Exception("Unable to determine image dimensions from dataset")

    def generate_info_table(self, index, ds):
        d = self.generate_info_dict(index, ds)
        tf = TableFragment(attrs={"class":"info_table"},style={"width":"%dpx"%self.grid_image_width})
        for (key,value) in d.items():
            tf.add_row([key,value])
        ef = ElementFragment("button", attrs={"class": "info_table"},style={"max-height":"%dpx"%self.grid_image_width}).add_fragment(tf)
        return ef

    def generate_info_dict(self, index, ds):
        d = {}
        variables = { "data": ds, "index":index }
        if self.crs:
            transformer = pyproj.Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
            lon, lat = transformer.transform(ds[self.x_coordinate].mean().item(), ds[self.y_coordinate].mean().item())
            variables["lon"] = lon
            variables["lat"] = lat

        for (key,value) in self.info.items():
            template = Template(value)
            try:
                d[key] = template.render(**variables)
            except Exception as ex:
                self.logger.warning(f"Unable to render info for key: {str(ex)}")
        return d

    def run(self):
        """Write the HTML page, images, data and timeseries files to the output folder."""
        self.write_static_files()

        image_width = None
        image_height = None
        if self.layer_definitions:
            image_width, image_height = self.get_image_dimensions(self.input_ds)
            cases = self.read_cases()
            self.check_layers()
            self.build_legends()
            self.build_layer_images(cases)

        if self.timeseries_definitions:
            self.build_timeseries_csv()

        builder = self.build_page(image_width, image_height)
        self.write_scenes_json()
        self.write_timeseries_json()

        self.logger.info(f"writing {self.output_html_path}")
        with open(self.output_html_path, "w") as f:
            f.write(builder.get_html())

    def read_cases(self):
        """Return a list of (index, timestamp, dataset) for each case, in time order if there is a time coordinate.

        Also records the earliest and latest timestamps.
        """
        cases = []
        n = len(self.input_ds[self.case_dimension])

        p = Progress("Reading data", enabled=self.show_progress)
        for i in range(n):
            p.report("", i/n)
            # timestamp will be formatted YYYY-MM-DDT:HH:MM:SS
            timestamp = str(self.input_ds[self.time_coordinate].data[i])[:19] if self.time_coordinate else None
            if timestamp:
                if self.min_timestamp is None or timestamp < self.min_timestamp:
                    self.min_timestamp = timestamp
                if self.max_timestamp is None or timestamp > self.max_timestamp:
                    self.max_timestamp = timestamp

            cases.append((i, timestamp, self.input_ds.isel(**{self.case_dimension: i})))
        if self.time_coordinate:
            cases = sorted(cases, key=lambda t: t[1])
        p.complete("Done")
        return cases

    def check_layers(self):
        """Remove any layers that cannot be built from the input data, logging a warning for each."""
        remove_layers = []
        for layer_definition in self.layer_definitions:
            err = layer_definition.check(self.input_ds)
            if err:
                self.logger.warning(f"Unable to add layer {layer_definition.layer_name}: {err}")
                remove_layers.append(layer_definition)

        for layer_definition in remove_layers:
            self.layer_definitions.remove(layer_definition)

    def build_legends(self):
        for layer_definition in self.flatten_layers(self.layer_definitions):
            if layer_definition.has_legend():
                legend_src, legend_path = self.get_image_path(layer_definition.layer_name + "_legend")
                layer_definition.build_legend(legend_path)
                self.layer_legends[layer_definition.layer_name] = legend_src

    def build_layer_images(self, cases):
        """Write the images (and data files) for every layer, filling in self.layer_images with one entry per case.

        Layers that do not vary by case are built once and shared by all cases.
        """
        n = len(cases)
        p = Progress("Building images", enabled=self.show_progress)

        static_image_srcs = {}
        static_data_srcs = {}

        for layer_definition in self.flatten_layers(self.layer_definitions):
            if not layer_definition.get_case_wise():
                (src, path) = self.get_image_path(layer_definition.layer_name)
                layer_definition.build(self.input_ds, path)
                static_image_srcs[layer_definition.layer_name] = src
                if layer_definition.save_data():
                    (data_src, data_path) = self.get_data_path(layer_definition.layer_name)
                    data_options = layer_definition.build_data(self.input_ds, data_path)
                    static_data_srcs[layer_definition.layer_name] = {"url": data_src, "options": data_options}

        for (index, timestamp, ds) in cases:
            p.report("",index/n)
            image_srcs = {}
            data_srcs = {}

            for layer_definition in self.flatten_layers(self.layer_definitions):
                if layer_definition.get_case_wise():
                    (src, path) = self.get_image_path(layer_definition.layer_name, index=index)
                    layer_definition.build(ds, path)
                    image_srcs[layer_definition.layer_name] = src
                    if layer_definition.save_data():
                        (data_src, data_path) = self.get_data_path(layer_definition.layer_name, index)
                        data_options = layer_definition.build_data(ds, data_path)
                        data_srcs[layer_definition.layer_name] = {"url":data_src, "options":data_options}
                else:
                    image_srcs[layer_definition.layer_name] = static_image_srcs[layer_definition.layer_name]
                    if layer_definition.save_data():
                        data_srcs[layer_definition.layer_name] = static_data_srcs[layer_definition.layer_name]

            self.layer_images.append((index, timestamp, image_srcs, data_srcs, ds))

        p.complete("Done")

    def build_page(self, image_width, image_height):
        """Build the HTML page, returning the Html5Builder holding it."""
        builder = Html5Builder(language="en")

        builder.head().add_element("title").add_text(self.title)
        builder.head().add_element("style").add_text(anti_aliasing_style)
        builder.head().add_element("script", {"src": "index.js"})
        builder.head().add_element("link", {"rel": "stylesheet", "href": "index.css"})
        builder.head().add_element("link", {"rel": "stylesheet", "href":"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
                                            "integrity":"sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=", "crossorigin":""})
        builder.head().add_element("script", {"src":"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
                                              "integrity":"sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
                                              "crossorigin":""})

        # dygraphs
        if self.timeseries:
            builder.head().add_element("script", {"type": "text/javascript", "src":"dependencies/dygraph.js"})
            builder.head().add_element("link", {"type": "text/css", "rel":"stylesheet", "href": "dependencies/dygraph.css"})

        # babylonJS
        if self.terrain_view:
            builder.head().add_element("script", {"type": "text/javascript", "src": "dependencies/babylon.js"})
            builder.head().add_element("script", {"type": "text/javascript", "src": "dependencies/babylonTerrain.js"})

        root = builder.body().add_element("div", {"id":"root_element"})

        container_div = root.add_element("div")

        display_timeseries = len(self.timeseries_definitions) > 0
        if self.layer_definitions:
            overlay_container_div = container_div.add_element("div",
                                                              {"id": "overlay_container", "style": "display:none;"})
            grid_container_div = container_div.add_element("div", {"id": "grid_container", "style": "display:none;"})
            self.build_overlay_view(overlay_container_div, builder, image_width, image_height)
            self.build_grid_view(grid_container_div, builder, image_width, image_height, display_timeseries=display_timeseries)

        if display_timeseries:
            timeseries_container_div = container_div.add_element("div", {"id": "timeseries_container",
                                                                         "style": "display:none;"})

            self.build_timeseries_view(timeseries_container_div, builder)

        if self.terrain_view:
            terrain_container_div = container_div.add_element("div", {"id": "terrain_container", "style":"display:none;"})
            self.build_terrain_view(terrain_container_div, builder)

        if image_width:
            builder.head().add_element("script").add_text("let image_width=" + json.dumps(image_width) + ";")

        return builder

    def write_scenes_json(self):
        """Write scenes.json, describing the layers and, for each case, its images, data files and info."""
        scenes = {"layers": [], "index": [], "layer_groups": {}}

        if self.terrain_view:
            scenes["terrain_view"] = self.terrain_view

        for layer_definition in self.page_order_layers():
            layer_dict = {"name": layer_definition.layer_name, "label": layer_definition.layer_label, "has_data": layer_definition.save_data()}
            if isinstance(layer_definition,LayerWMS):
                layer_dict["wms_url"] = layer_definition.wms_url
            if isinstance(layer_definition,LayerDiscrete):
                layer_dict["classes"] = {}
                for (k,v) in layer_definition.values.items():
                    (label,colour) = v
                    layer_dict["classes"][k] = { "label": label, "colour": ColoursToRGB.to_hex(ColoursToRGB.lookup(colour)) }
            scenes["layers"].append(layer_dict)

        for layer_definition in self.page_order_layers()[::-1]:
            group = layer_definition.get_group()
            if group:
                group_layer_name = group.layer_name
                if group_layer_name not in scenes["layer_groups"]:
                    scenes["layer_groups"][group_layer_name] = []
                scenes["layer_groups"][group_layer_name].append(layer_definition.layer_name)

        for (index, timestamp, layer_sources, data_sources, ds) in self.layer_images:

            info = {}
            if self.info:
                info = self.generate_info_dict(index, ds)

            scene_dict = {"timestamp": timestamp if timestamp is not None else "", "image_srcs": layer_sources,
                               "data_srcs": data_sources, "info": info, "pos":index}
            for layer_definition in self.page_order_layers()[::-1]:
                if isinstance(layer_definition, LayerWMS):
                    ((x_min, y_min), (x_max, y_max)) = layer_definition.get_bounds(ds)
                    scene_dict["x_min"] = x_min
                    scene_dict["x_max"] = x_max
                    scene_dict["y_min"] = y_min
                    scene_dict["y_max"] = y_max

            scenes["index"].append(scene_dict)

        scenes["data_width"] = self.data_width
        scenes["data_height"] = self.data_height

        with open(os.path.join(self.output_folder, "scenes.json"), "w") as f:
            f.write(json.dumps(scenes,indent=4))

    def write_timeseries_json(self):
        """Write timeseries.json, listing each timeseries chart and its CSV file (only if there are timeseries)."""
        if not self.timeseries_definitions:
            return
        timeseries = []
        for (timeseries_name, timeseries_spec, timeseries_detail) in self.timeseries_definitions:
            timeseries.append({
                "name": timeseries_name,
                "spec": timeseries_spec,
                "csv_url": timeseries_detail["csv_url"],
                "div_id": timeseries_detail["div_id"]
            })
        with open(os.path.join(self.output_folder, "timeseries.json"), "w") as f:
            f.write(json.dumps(timeseries, indent=4))

    def page_order_layers(self, only_grid_view=False):
        """Return the layers in the order the grid view and scenes.json list them.

        Top-level layers are in configuration order, but the layers inside each layer group are in reverse order.
        (This preserves the order the page has always used.)
        """
        return self.flatten_layers(self.layer_definitions[::-1], only_grid_view=only_grid_view)[::-1]

    def has_data_layers(self):
        """Return True if any single band layer saves its data, so the page can show data values."""
        for layer_definition in self.flatten_layers(self.layer_definitions):
            if layer_definition.has_legend():
                if isinstance(layer_definition,LayerSingleBand) and layer_definition.save_data():
                    return True
        return False

    @staticmethod
    def add_spacer(div):
        div.add_element("span", {"class": "spacer"}).add_text("|")

    def add_date_picker(self, div, element_id):
        """Add a date input limited to the range of the cases' timestamps, if they are known."""
        if self.min_timestamp is not None and self.max_timestamp is not None:
            self.add_spacer(div)
            div.add_element("input",
                {"type":"date", "id":element_id,"min":self.min_timestamp[:10],"max":self.max_timestamp[:10]})

    def add_download_anchor(self, div, link_text):
        """Add a link to download the input data, if a download link was created."""
        if self.download_filename:
            self.add_spacer(div)
            div.add_element("a", {"href": self.download_filename,
                                  "download": self.download_filename}).add_text(link_text)

    @staticmethod
    def add_control_panel(parent, panel_id, title, close_button_id):
        """Add a control panel container with a title bar and close button, returning the panel."""
        panel = parent.add_element("div", {"id": panel_id, "class": "control_container"})
        panel.add_element("div", {"id": panel_id + "_header", "class": "control_container_header"}).add_text(
            title).add_element("input",{"type":"button","id":close_button_id,"value":"X","class":"close_button"})
        return panel

    @staticmethod
    def add_group_select(parent, group, layer_definition, select_id):
        """Add a select control for choosing which layer of a layer group to show."""
        sf = SelectFragment({"id": select_id})
        for layer in group.get_layers():
            sf.add_option(layer.layer_name, layer.layer_label,
                          is_selected=(layer.layer_name == layer_definition.layer_name))
        parent.add_fragment(sf)

    def build_grid_view(self, grid_container_div, builder, image_width, image_height, display_timeseries=False):
        self.build_grid_toolbar(grid_container_div, display_timeseries)
        grid_container_div.add_fragment(self.build_grid_table(image_width, image_height))

    def build_grid_toolbar(self, grid_container_div, display_timeseries):
        grid_container_div.add_element("input",
                                       {"type": "button", "id": "overlay_view_btn", "value": "Show Overlay View"})
        if display_timeseries:
            grid_container_div.add_element("input",
                                       {"type": "button", "id": "timeseries_view_btn", "value": "Show Timeseries View"})

        grid_container_div.add_element("span").add_text(self.title)

        self.add_date_picker(grid_container_div, "grid_select_date")
        self.add_download_anchor(grid_container_div, "download data")

        self.add_spacer(grid_container_div)
        grid_container_div.add_element("button", {"id": "prev_page_btn"}).add_text("Previous page")
        grid_container_div.add_element("input", {"type": "range", "id": "page_index"})
        grid_container_div.add_element("button", {"id": "next_page_btn"}).add_text("Next Page")
        grid_container_div.add_element("span", {"id": "page_label"}).add_text("1/1")
        grid_container_div.add_element("input", {"id":"page_size", "type": "number", "value":"10", "min":"1", "step":"1"}).add_text("Page Size")

    def build_grid_table(self, image_width, image_height):
        """Build the grid view's table: one column per layer and one row per case."""
        grid_layers = self.page_order_layers(only_grid_view=True)

        tf = TableFragment()

        columns_hidden = [False]
        column_ids = ["index_col"]

        if self.info:
            column_ids += ["info"]
            columns_hidden += [False]

        groups = set()
        for layer_definition in grid_layers:
            group = layer_definition.get_group()
            column_ids.append(layer_definition.layer_name + "_col")
            # if this column is one of a group and not the first, hide it initially
            columns_hidden.append(bool(group and group in groups))
            if group:
                groups.add(group)

        tf.set_column_ids(column_ids, columns_hidden)

        header_cells = ["Index"]
        if self.info:
            header_cells.append("Info")
        for layer_definition in grid_layers:
            header_cells.append(self.build_grid_header_cell(layer_definition, image_width))
        tf.add_header_row(header_cells)

        button_cells = [""]
        if self.info:
            button_cells.append("")
        for layer_definition in grid_layers:
            button_cells.append(
                ElementFragment("button", {"id": layer_definition.layer_name + "_hide"}).add_text("Hide"))
        tf.add_header_row(button_cells)

        width = self.grid_image_width if self.grid_image_width else image_width
        height = round(self.grid_image_width * (image_height/image_width)) if self.grid_image_width else image_height

        for (row, (index, timestamp, layer_sources, _, ds)) in enumerate(self.layer_images):
            cells = [self.generate_index_cell(row)]
            if self.info:
                cells += [self.generate_info_table(index,ds)]

            for layer_definition in grid_layers:
                img = ImageFragment("", layer_definition.layer_name + "_grid_" + str(index), alt_text=timestamp,
                                    w=width,
                                    h=height,
                                    load_url=layer_sources[layer_definition.layer_name])
                div = ElementFragment("div",{"style":f"width:{width}px;height:{height}px;"})
                div.add_fragment(img)
                cells.append(div)

            tf.add_row(cells, {"id":f"row{row}"}, {"display":"none"})

        return tf

    def build_grid_header_cell(self, layer_definition, image_width):
        """Build a grid column heading: the layer's legend (or label) and, for a layer group, a layer selector."""
        group = layer_definition.get_group()
        label = group.layer_label if group else layer_definition.layer_label
        header_div = ElementFragment("div")
        if layer_definition.layer_name in self.layer_legends:
            img = ImageFragment(self.layer_legends[layer_definition.layer_name],
                                layer_definition.layer_name + "_grid_legend", alt_text="legend",
                                w=self.grid_image_width if self.grid_image_width else image_width)
            vmin = getattr(layer_definition, "vmin", None)
            vmax = getattr(layer_definition, "vmax", None)
            header_div.add_fragment(LegendFragment(label, img, vmin, vmax))
        else:
            header_div.add_fragment(ElementFragment("span").add_text(label))
        if group:
            header_div.add_fragment(ElementFragment("br"))
            self.add_group_select(header_div, group, layer_definition, layer_definition.layer_name + "_group_select")
        return header_div

    def generate_index_cell(self, index):
        return ElementFragment("button",attrs={"id":f"open_{index}_btn"}).add_text("Open: "+str(index+1))

    def build_overlay_view(self, overlay_container_div, builder, image_width, image_height):
        has_data = self.has_data_layers()

        self.build_overlay_toolbar(overlay_container_div, has_data)
        self.build_layer_controls(overlay_container_div)

        if self.filter_controls:
            self.build_filter_controls(overlay_container_div)

        if self.info:
            info_div = self.add_control_panel(overlay_container_div, "info_container", "Scene Info", "close_info_btn")
            info_div.add_element("div", attrs={"id":"info_content"})

        if has_data:
            data_div = self.add_control_panel(overlay_container_div, "data_container", "Data", "close_data_btn")
            data_div.add_element("div", attrs={"id":"data_content"})

        overlay_container_div.add_element("div",{"id":"map"})

    def build_overlay_toolbar(self, overlay_container_div, has_data):
        overlay_container_div.add_element("input", {"type": "button", "id": "grid_view_btn", "value": "Show Grid View"})
        overlay_container_div.add_element("span").add_text(self.title)

        self.add_download_anchor(overlay_container_div, "download netcdf4")

        self.add_spacer(overlay_container_div)
        overlay_container_div.add_element("button", {"id": "prev_btn"}).add_text("Previous")
        overlay_container_div.add_element("input", {"type": "range", "id": "time_index"})
        overlay_container_div.add_element("button", {"id": "next_btn"}).add_text("Next")
        overlay_container_div.add_element("span", {"id": "scene_label"}).add_text("?")

        self.add_date_picker(overlay_container_div, "overlay_select_date")

        self.add_spacer(overlay_container_div)

        if self.terrain_view:
            overlay_container_div.add_element("button", {"id": "terrain_view_btn"}).add_text("Terrain View")
            self.add_spacer(overlay_container_div)

        overlay_container_div.add_element("input", {"type": "button", "id": "show_layers", "value": "Layer Controls"})

        self.add_spacer(overlay_container_div)

        if self.filter_controls:
            overlay_container_div.add_element("input", {"type": "button", "id": "show_filters", "value":"Filter Controls"})

        if self.info:
            overlay_container_div.add_element("input", {"type": "button", "id": "show_info", "value":"Show Info"})

        if has_data:
            overlay_container_div.add_element("input", {"type": "button", "id": "show_data", "value": "Show Data"})

    def build_layer_controls(self, overlay_container_div):
        """Build the Layers panel: a row of controls (opacity, legend, colour map) for each overlay layer."""
        controls_div = self.add_control_panel(overlay_container_div, "layer_container", "Layers", "close_layer_btn")

        slider_fieldset = controls_div.add_element("fieldset", style={"display": "inline"})
        slider_fieldset.add_element("legend").add_text("Layers")
        slider_container = slider_fieldset.add_element("div", {"id": "legend_controls"})
        slider_container.add_element("input", {"type": "button", "id": "close_all_sliders", "value": "Hide All Layers"})
        slider_table = slider_container.add_element("table", {"id": "slider_controls"})

        groups = set()
        for layer_definition in self.flatten_layers(self.layer_definitions, only_overlay_view=True):
            group = layer_definition.get_group()
            # only the first layer of a group is shown initially
            hidden = bool(group and group in groups)
            if group:
                groups.add(group)
            self.build_layer_control_row(slider_table, layer_definition, hidden)

    def build_layer_control_row(self, slider_table, layer_definition, hidden):
        group = layer_definition.get_group()
        style = {"display": "none"} if hidden else {}

        row = slider_table.add_element("tr", attrs={"id":layer_definition.layer_name+"_overlay_row"},style=style)
        (col1, col2, col3, col4, col5, col6) = [row.add_element("td") for _ in range(6)]

        if group:
            col1.add_text(group.layer_label)
            self.add_group_select(col1, group, layer_definition, layer_definition.layer_name + "_group_select_row")
        else:
            col1.add_text(layer_definition.layer_label)

        opacity_control_id = layer_definition.layer_name + "_opacity"
        col2.add_element("input", {"type": "range", "id": opacity_control_id})

        if layer_definition.has_legend():
            layer_has_data = isinstance(layer_definition,LayerSingleBand) and layer_definition.save_data()
            legend_src = self.layer_legends[layer_definition.layer_name]
            if layer_has_data:
                col3.add_element("input",{"type":"number","value":str(layer_definition.vmin),"id":layer_definition.layer_name+"_min_input"})
            else:
                col3.add_element("span").add_text(str(layer_definition.vmin))
            col4.add_element("img", {"src": legend_src, "class": "legend", "id":layer_definition.layer_name+"_legend_img"})
            if layer_has_data:
                col5.add_element("input", {"type": "number", "value": str(layer_definition.vmax),
                                           "id": layer_definition.layer_name + "_max_input"})
            else:
                col5.add_element("span").add_text(str(layer_definition.vmax))
            if layer_has_data:
                selector_control_id = layer_definition.layer_name+"_camp_selector"
                cmap_selector = col6.add_element("select",{"id":selector_control_id})
                for cmap in self.all_cmaps:
                    option_attrs = {"value":cmap}
                    if cmap == layer_definition.get_cmap():
                        option_attrs["selected"] = "selected"
                    cmap_selector.add_element("option",option_attrs).add_text(cmap)

        elif isinstance(layer_definition,LayerDiscrete):
            col4.add_element("div", {"id":layer_definition.layer_name+"_classes"})

    def build_filter_controls(self, overlay_container_div):
        """Build the Scene Filters panel, with a checkbox for each month."""
        filter_div = self.add_control_panel(overlay_container_div, "filter_container", "Scene Filters", "close_filter_btn")

        filter_fieldset = filter_div.add_element("fieldset", style={"display": "inline"})
        filter_fieldset.add_element("legend").add_text("Filters")

        month_filter = filter_fieldset.add_element("div", {})

        for (month, month_name) in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1):
            month_filter.add_element("span").add_text(month_name)
            month_filter.add_element("input", {"id": f"month{month}", "type": "checkbox", "checked": "checked"})

    def build_timeseries_csv(self):
        """Write a CSV file for each timeseries, in the timeseries/ folder."""
        folder = os.path.join(self.output_folder, "timeseries")
        os.makedirs(folder, exist_ok=True)

        for (timeseries_name, timeseries_spec, timeseries_detail) in self.timeseries_definitions:
            csv_path = os.path.join(folder, timeseries_name + ".csv")
            timeseries_type = timeseries_spec.get("type","timeseries")

            (data, years) = self.collect_timeseries_rows(timeseries_name, timeseries_spec)
            column_names = self.timeseries_column_names(timeseries_spec)

            with open(csv_path, "w") as f:
                writer = csv.writer(f)
                if timeseries_type == "timeseries":
                    self.write_timeseries_rows(writer, data, column_names)
                elif timeseries_type == "seasonal":
                    if len(column_names) != 1:
                        raise ConfigError(f"timeseries '{timeseries_name}' is seasonal, so it needs exactly 1 variable, got {len(column_names)}")
                    self.write_seasonal_rows(writer, data, years, column_names[0])

            timeseries_detail["csv_url"] = os.path.join("timeseries", timeseries_name + ".csv")

    @staticmethod
    def timeseries_column_names(timeseries_spec):
        """Return the names of a timeseries' value columns: one per variable, or one per (mask, variable) pair."""
        source_masks = timeseries_spec.get("masks", [])
        variables = timeseries_spec["variables"]
        if not source_masks:
            return variables
        return [f"{source_mask}_{variable.replace(':', '_')}" for source_mask in source_masks for variable in variables]

    def collect_timeseries_rows(self, timeseries_name, timeseries_spec):
        """Compute a timeseries' values for every case.

        Returns (rows, years): rows is a list of dicts holding "datetime", "timestamp" and a value for each column,
        and years is the sorted list of years the cases fall in.
        """
        source_masks = timeseries_spec.get("masks", [])
        variables = timeseries_spec["variables"]

        data = []
        n = len(self.input_ds[self.time_coordinate])

        years = set()

        for i in range(n):
            timestamp = str(self.input_ds[self.time_coordinate].data[i])[:19] if self.time_coordinate else None

            data_slice = self.input_ds.isel(**{self.case_dimension: i})

            dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
            years.add(dt.year)

            rowdata = {"datetime": dt, "timestamp": timestamp}

            if source_masks:
                # build a timseries for each combination of mask and variable
                for source_mask in source_masks:
                    mask_slice = self.input_ds[source_mask].squeeze()
                    if self.case_dimension in mask_slice.dims:
                        mask_slice = mask_slice.isel(**{self.case_dimension: i})

                    for variable in variables:
                        variable_components = variable.split(":")
                        variable_name = variable_components[0]
                        aggregation_fn = variable_components[1] if len(variable_components) == 2 else "mean"
                        values = xr.where(mask_slice, data_slice[variable_name], np.nan)
                        column = f"{source_mask}_{variable.replace(':', '_')}"
                        rowdata[column] = self.aggregate(timeseries_name, values, aggregation_fn)

            else:
                for variable in variables:
                    rowdata[variable] = data_slice[variable].item()

            data.append(rowdata)

        return (data, sorted(years))

    @staticmethod
    def aggregate(timeseries_name, values, aggregation_fn):
        """Reduce a masked array of values to a single number using the named aggregation function."""
        if aggregation_fn == "mean":
            return values.mean(skipna=True).item()
        elif aggregation_fn == "min":
            return values.min(skipna=True).item()
        elif aggregation_fn == "max":
            return values.max(skipna=True).item()
        elif aggregation_fn == "median":
            return float(np.median(values.data))
        else:
            raise ConfigError(f"timeseries '{timeseries_name}' uses unknown aggregation function '{aggregation_fn}'")

    @staticmethod
    def csv_value(v):
        """Format a value for the CSV files, writing NaN as an empty cell."""
        return "" if math.isnan(v) else v

    def write_timeseries_rows(self, writer, data, column_names):
        """Write a timeseries CSV: a datetime column, then one column per value, with one row per case."""
        headers = ["datetime"] + column_names
        writer.writerow(headers)
        for rowdata in data:
            writer.writerow([rowdata["timestamp"]] + [self.csv_value(rowdata.get(header,"")) for header in headers[1:]])

    def write_seasonal_rows(self, writer, data, years, column_name):
        """Write a seasonal CSV: one row per day of the year (1-366) and one column per year."""
        writer.writerow(["day"] + years)

        for day in range(1,367):
            values_by_year = {}
            for rowdata in data:
                dt = rowdata["datetime"]
                if dt.timetuple()[7] == day:
                    values_by_year[dt.year] = rowdata[column_name]

            row = [day]
            for year in years:
                row.append(self.csv_value(values_by_year[year]) if year in values_by_year else "")
            writer.writerow(row)

    def build_timeseries_view(self, timeseries_container_div, builder):
        if self.layer_definitions:
            timeseries_container_div.add_element("input", {"type": "button", "id": "grid_view_btn2", "value": "Show Grid View"})

        timeseries_container_div.add_element("span").add_text(self.title)

        if self.download_filename:
            timeseries_container_div.add_element("span", {"class": "spacer"}).add_text("|")
            timeseries_container_div.add_element("a", {"href": self.download_filename,
                                                 "download": self.download_filename}).add_text("download data")

        timeseries_container_div.add_element("h2").add_text("timeseries")

        for (timeseries_name,timeseries_spec, timeseries_detail) in self.timeseries_definitions:

            timeseries_label = timeseries_spec.get("label",timeseries_name)
            timeseries_container_div.add_element("h3").add_text(timeseries_label)
            timeseries_div_id = f"timeseries_{timeseries_name}"
            timeseries_container_div.add_element("div",{"id":timeseries_div_id, "class":"timeseries_chart"})
            timeseries_detail["div_id"] = timeseries_div_id

    def build_terrain_view(self, terrain_container_div, builder):
        terrain_container_div.add_element("input", {"id": "exit_terrain_view", "type":"button", "value":"Exit Terrain View"})
        terrain_container_div.add_element("span", {"class": "spacer"}).add_text("|")
        terrain_container_div.add_element("label", attrs={"for": "terrain_zoom"}).add_text("Exaggerate Terrain")
        terrain_container_div.add_element("input", {"id":"terrain_zoom", "step":"0.1", "type": "range", "min": "1", "max": "10", "value":"1"})
        terrain_container_div.add_element("span", {"id": "terrain_zoom_value"}).add_text("1")
        terrain_container_div.add_element("p")
        terrain_container_div.add_element("canvas",{"id": "render_canvas"})
