import copy
import csv
import gzip
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

import numpy as np
import xarray as xr
import yaml
from PIL import Image

import netcdf_explorer.api.bigplot
from netcdf_explorer.api.config_error import ConfigError
from netcdf_explorer.api.expr_parser import ExpressionParser
from netcdf_explorer.api.html_generator import HTMLGenerator
from netcdf_explorer.api.layers import LayerFactory

TEST_FOLDER = os.path.dirname(os.path.abspath(__file__))
INPUT_PATH = os.path.join(TEST_FOLDER, "area_293_min.nc")
CONFIG_PATH = os.path.join(TEST_FOLDER, "example_layers.yaml")

# the test dataset has 3 cases (time steps) on a 401 x 401 grid
CASE_COUNT = 3
GRID_SIZE = (401, 401)

# layers in example_layers.yaml built from the input data (one image per case).
# CLOUD_MASK and ALL_CLOUD_MASK come from derive_bands, so they also check expression evaluation.
# The OSM layer is left out because it is fetched over the network from a WMS server.
CASE_LAYERS = ["CLOUD_MASK", "ALL_CLOUD_MASK", "ST", "REF"]

BIGPLOT_BORDER = 20  # default border, in pixels, on each side of a bigplot image


def run_cli(module, args, cwd):
    """Run a command-line tool in a subprocess, failing the test if it exits with an error."""
    result = subprocess.run([sys.executable, "-m", module] + args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{module} exited with code {result.returncode}\n"
                             f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def run_cli_expecting_failure(module, args, cwd):
    """Run a command-line tool in a subprocess that is expected to fail, returning the completed process."""
    result = subprocess.run([sys.executable, "-m", module] + args, cwd=cwd, capture_output=True, text=True)
    if result.returncode == 0:
        raise AssertionError(f"{module} was expected to fail but exited with code 0\n"
                             f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def load_example_config():
    with open(CONFIG_PATH) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


# a stand-in for HTMLGenerator, providing the attributes LayerFactory reads from it
STUB_CONVERTER = types.SimpleNamespace(case_dimension="time", x_dimension="x", y_dimension="y",
                                       time_coordinate="time", x_coordinate="x", y_coordinate="y")


class Test(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def check_html_output(self, output_folder, expect_download_link):
        for filename in ["index.html", "index.js", "index.css", "scenes.json"]:
            self.assertTrue(os.path.isfile(os.path.join(output_folder, filename)), f"missing {filename}")

        with open(os.path.join(output_folder, "scenes.json")) as f:
            scenes = json.load(f)

        self.assertEqual(len(scenes["index"]), CASE_COUNT)
        self.assertEqual((scenes["data_width"], scenes["data_height"]), GRID_SIZE)

        layer_names = [layer["name"] for layer in scenes["layers"]]
        for layer_name in CASE_LAYERS:
            self.assertIn(layer_name, layer_names)

        timestamps = [scene["timestamp"] for scene in scenes["index"]]
        self.assertEqual(timestamps, sorted(timestamps), "cases should be in time order")

        for scene in scenes["index"]:
            for layer_name in CASE_LAYERS:
                image_path = os.path.join(output_folder, scene["image_srcs"][layer_name])
                self.assertTrue(os.path.isfile(image_path), f"missing {image_path}")
                with Image.open(image_path) as im:
                    self.assertEqual(im.size, GRID_SIZE, f"unexpected size for {image_path}")

        download_path = os.path.join(output_folder, os.path.basename(INPUT_PATH))
        self.assertEqual(os.path.exists(download_path), expect_download_link)

    def check_bigplot_image(self, path, plot_width):
        self.assertTrue(os.path.isfile(path), f"missing {path}")
        with Image.open(path) as im:
            width, height = im.size
        self.assertEqual(width, plot_width + 2 * BIGPLOT_BORDER)
        self.assertGreaterEqual(height, plot_width)

    def test_293_api(self):
        output_folder = os.path.join(self.output_dir, "area_293_output_api")
        with open(CONFIG_PATH) as f:
            config = yaml.load(f, Loader=yaml.FullLoader)
        gen = HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=output_folder, title="area 293",
                            add_download_link=True, filter_controls=True)
        gen.run()
        self.check_html_output(output_folder, expect_download_link=True)

    def test_293_commandline(self):
        output_folder = os.path.join(self.output_dir, "area_293_output_cli")
        run_cli("netcdf_explorer.cli.generate_html",
                ["--input-path", INPUT_PATH, "--title", "area_293", "--output-folder", output_folder,
                 "--config-path", CONFIG_PATH, "--download-data"], cwd=self.output_dir)
        self.check_html_output(output_folder, expect_download_link=True)

    def test_293_commandline_sample_cases(self):
        output_folder = os.path.join(self.output_dir, "area_293_output_sampled")
        run_cli("netcdf_explorer.cli.generate_html",
                ["--input-path", INPUT_PATH, "--title", "area_293", "--output-folder", output_folder,
                 "--config-path", CONFIG_PATH, "--sample-cases", "2", "0"], cwd=self.output_dir)
        with open(os.path.join(output_folder, "scenes.json")) as f:
            scenes = json.load(f)
        self.assertEqual(len(scenes["index"]), 2)
        timestamps = [scene["timestamp"] for scene in scenes["index"]]
        self.assertEqual(timestamps, sorted(timestamps), "cases should be in time order")
        self.assertFalse(os.path.exists(os.path.join(output_folder, os.path.basename(INPUT_PATH))))

        # the timeseries CSV rows should also be in time order, whatever order the cases were selected in
        with open(os.path.join(output_folder, "timeseries", "ST_cloudy.csv")) as f:
            csv_timestamps = [line.split(",")[0] for line in f.read().splitlines()[1:]]
        self.assertEqual(len(csv_timestamps), 2)
        self.assertEqual(csv_timestamps, sorted(csv_timestamps))
        self.assertEqual(csv_timestamps, timestamps)

    def test_bigplot(self):
        output_path = os.path.join(self.output_dir, "area_293_min.png")
        ds = xr.open_dataset(INPUT_PATH).isel(time=1)
        da = xr.concat([ds["B2"], ds["B3"], ds["B4"]], dim="band")
        bp = netcdf_explorer.api.bigplot.BigPlot(da, x="x", y="y", vmin=0, vmax=1, legend_height=0,
                                                 output_path=output_path, plot_width=1024)
        bp.run()
        self.check_bigplot_image(output_path, plot_width=1024)

    def test_bigplot_commandline(self):
        output_path = os.path.join(self.output_dir, "area_293_min_cmdline.png")
        run_cli("netcdf_explorer.cli.bigplot",
                ["--input-path", INPUT_PATH, "--iselector", "time", "1", "1", "--input-variable", "B2", "B3", "B4",
                 "--output-path", output_path, "--title", "area_293_min", "--vmin", "0", "--vmax", "0.3",
                 "--plot-width", "512"], cwd=self.output_dir)
        self.check_bigplot_image(output_path, plot_width=512)

    def test_bigplot_commandline_st(self):
        output_path = os.path.join(self.output_dir, "area_293_min_cmdline_st.png")
        run_cli("netcdf_explorer.cli.bigplot",
                ["--input-path", INPUT_PATH, "--iselector", "time", "1", "1", "--input-variable", "ST",
                 "--output-path", output_path, "--title", "area_293_min_st", "--vmin", "270", "--vmax", "330",
                 "--cmap", "viridis", "--plot-width", "512"], cwd=self.output_dir)
        self.check_bigplot_image(output_path, plot_width=512)

    def test_bigplot_commandline_fails_on_missing_input(self):
        with self.assertRaises(AssertionError):
            run_cli("netcdf_explorer.cli.bigplot",
                    ["--input-path", os.path.join(self.output_dir, "missing.nc"), "--input-variable", "ST",
                     "--output-path", os.path.join(self.output_dir, "out.png")], cwd=self.output_dir)

    def test_bigplot_commandline_fails_on_unknown_variable(self):
        with self.assertRaises(AssertionError):
            run_cli("netcdf_explorer.cli.bigplot",
                    ["--input-path", INPUT_PATH, "--input-variable", "NO_SUCH_VARIABLE",
                     "--output-path", os.path.join(self.output_dir, "out.png")], cwd=self.output_dir)


class TestExpressionParser(unittest.TestCase):

    def setUp(self):
        self.parser = ExpressionParser()
        self.parser.add_unary_operator("not")
        self.parser.add_binary_operator("&", 3)
        self.parser.add_binary_operator("==", 2)

    def test_valid_expression(self):
        parsed = self.parser.parse("not (QA_PIXEL & 8 == 0)")
        self.assertEqual(parsed["operator"], "not")

    def test_invalid_expressions_raise(self):
        for expression in ["(QA_PIXEL & 8", "QA_PIXEL & 8)", "QA_PIXEL & & 8", "QA_PIXEL &"]:
            with self.subTest(expression=expression), self.assertRaises(ExpressionParser.ParseError):
                self.parser.parse(expression)


class TestDerivedBandExpressions(unittest.TestCase):
    """Parse and evaluate expressions with the operators and functions used for derive_bands."""

    def setUp(self):
        self.parser = HTMLGenerator.create_expression_parser()
        self.ds = xr.Dataset({"A": ("x", np.array([-2.0, 0.0, 1.0, 3.0])),
                              "B": ("x", np.array([1.0, 1.0, 2.0, 2.0])),
                              "Q": ("x", np.array([0, 8, 9, 30]))})

    def evaluate(self, expression):
        parsed = self.parser.parse(expression)
        HTMLGenerator.check_expression("D", parsed)
        from_bands = []
        # evaluate_expression only needs input_ds, so skip HTMLGenerator.__init__, which reads a whole config
        generator = HTMLGenerator.__new__(HTMLGenerator)
        generator.input_ds = self.ds
        result = generator.evaluate_expression(parsed, from_bands)
        return (np.broadcast_to(result, (4,)).tolist(), from_bands)

    def test_operators(self):
        cases = [
            ("A + B * 2", [0, 2, 5, 7]),
            ("(A + B) * 2", [-2, 2, 6, 10]),
            ("A - B - 1", [-4, -2, -2, 0]),
            ("A / B", [-2, 0, 0.5, 1.5]),
            ("Q & 8", [0, 8, 8, 8]),
            ("Q | 1", [1, 9, 9, 31]),
            ("A == 1", [False, False, True, False]),
            ("A > 0", [False, False, True, True]),
            ("A >= 1", [False, False, True, True]),
            ("A < 0", [True, False, False, False]),
            ("A<=0", [True, True, False, False]),
            ("A > 0 and B > 1", [False, False, True, True]),
            ("A > 2 or B < 2", [True, True, False, True]),
            ("not (Q & 8 == 0)", [False, True, True, True]),
            ("(not Q) and B < 2", [True, False, False, False]),
            ("-A", [2, 0, -1, -3]),
            ("A - -1", [-1, 1, 2, 4]),
            ("B * -A", [2, 0, -2, -6]),
            ("A > -1", [False, True, True, True]),
            ("1.5e1 - A", [17, 15, 14, 12]),
        ]
        for (expression, expected) in cases:
            with self.subTest(expression=expression):
                (result, _) = self.evaluate(expression)
                self.assertEqual(result, expected)

    def test_if_function(self):
        for expression in ["IF(A > 0, B, -B)", "if(A > 0, B, -B)"]:
            with self.subTest(expression=expression):
                (result, from_bands) = self.evaluate(expression)
                self.assertEqual(result, [-1, -1, 2, 2])
                self.assertEqual(sorted(set(from_bands)), ["A", "B"])
        (result, _) = self.evaluate("IF(A > 2, 3, IF(A > 0, 2, 1))")
        self.assertEqual(result, [1, 1, 2, 3])

    def test_invalid_function_calls_raise_config_error(self):
        cases = [
            ("max(B)", "unknown function 'max'"),
            ("IF(A > 0, 1)", "needs exactly 3 arguments, got 2"),
            ("A + IF(A > 0, 1, 2, 3)", "needs exactly 3 arguments, got 4"),
        ]
        for (expression, expected_fragment) in cases:
            with self.subTest(expression=expression), self.assertRaises(ConfigError) as cm:
                HTMLGenerator.check_expression("D", self.parser.parse(expression))
            self.assertIn("'D'", str(cm.exception))
            self.assertIn(expected_fragment, str(cm.exception))


class TestLayerDimensions(unittest.TestCase):

    def test_layer_with_own_x_and_y_dimension_names(self):
        # add variables whose dimensions are named xx and yy rather than x and y:
        # a 1-D profile along xx, and a 2-D field stored as (xx, yy), transposed relative to the other variables
        ds = xr.open_dataset(INPUT_PATH)
        width = ds.sizes["x"]
        ds["XPROFILE"] = xr.DataArray(np.linspace(0, 1, width), dims=("xx",))
        ds["ST_T"] = xr.DataArray(ds["ST"].isel(time=0).data.T, dims=("xx", "yy"))
        # the same field stored upside down and back to front, with its own (reversed) coordinates
        ds = ds.assign_coords(x_rev=("xr", ds["x"].data[::-1]), y_rev=("yr", ds["y"].data[::-1]))
        ds["ST_R"] = xr.DataArray(ds["ST"].isel(time=0).data[::-1, ::-1], dims=("yr", "xr"))
        # a half-resolution version of the field covering the same area, averaging 2x2 blocks of pixels
        half = ds["ST"].isel(time=0, x=slice(0, 400), y=slice(0, 400)).coarsen(x=2, y=2).mean()
        ds = ds.assign_coords(x_half=("xh", half["x"].data), y_half=("yh", half["y"].data))
        ds["ST_HALF"] = xr.DataArray(half.data, dims=("yh", "xh"))
        layers = {
            "ST": {"type": "single", "min_value": 270, "max_value": 300},
            "XPROFILE": {"type": "single", "min_value": 0, "max_value": 1, "dimensions": {"x": "xx"}},
            "ST_T": {"type": "single", "min_value": 270, "max_value": 300, "dimensions": {"x": "xx", "y": "yy"}},
            "ST_R": {"type": "single", "min_value": 270, "max_value": 300, "dimensions": {"x": "xr", "y": "yr"},
                     "coordinates": {"x": "x_rev", "y": "y_rev"}},
            "ST_HALF": {"type": "single", "min_value": 270, "max_value": 300, "data": {"label": "{value} K"},
                        "dimensions": {"x": "xh", "y": "yh"}, "coordinates": {"x": "x_half", "y": "y_half"}},
        }
        config = {"dimensions": {"case": "time", "x": "x", "y": "y"},
                  "coordinates": {"x": "x", "y": "y", "time": "time"}, "layers": layers}
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "input.nc")
            ds.to_netcdf(input_path)
            output_folder = os.path.join(tmp, "out")
            HTMLGenerator(config=config, input_path=input_path, output_folder=output_folder, title="t",
                          add_download_link=False, show_progress=False).run()
            with open(os.path.join(output_folder, "scenes.json")) as f:
                scenes = json.load(f)
            self.assertEqual(sorted(layer["name"] for layer in scenes["layers"]), sorted(layers))
            first_case = next(scene for scene in scenes["index"] if scene["pos"] == 0)
            images = {}
            for name in ["XPROFILE", "ST_T", "ST_R", "ST_HALF", "ST"]:
                with Image.open(os.path.join(output_folder, first_case["image_srcs"][name])) as im:
                    images[name] = np.array(im)
            # the half-resolution layer's data file is also written at its own resolution
            with open(os.path.join(output_folder, first_case["data_srcs"]["ST_HALF"]["url"]), "rb") as f:
                (half_height, half_width) = np.frombuffer(gzip.decompress(f.read())[:8], dtype="<i4")

        self.assertEqual(images["XPROFILE"].shape[:2], (GRID_SIZE[1], GRID_SIZE[0]))
        # a profile along x gives an image whose rows are all the same
        self.assertTrue((images["XPROFILE"] == images["XPROFILE"][0]).all())
        # the transposed field gives the same image as the original field for that case
        self.assertTrue((images["ST_T"] == images["ST"]).all())
        # the reversed field is turned the right way up using its own coordinates
        self.assertTrue((images["ST_R"] == images["ST"]).all())
        # a layer with a different resolution keeps its own resolution, and the same orientation as the others:
        # its pixels match the full-resolution image's pixels at the same positions (allowing for averaging)
        self.assertEqual(images["ST_HALF"].shape[:2], (200, 200))
        self.assertEqual((half_width, half_height), (200, 200))
        # (the mean colour difference is about 2.5 the right way up, and over 35 for any flip)
        half_image = images["ST_HALF"][:, :, :3].astype(int)
        full_sampled = images["ST"][1:400:2, 1:400:2, :3].astype(int)
        self.assertLess(np.abs(half_image - full_sampled).mean(), 10)
        for flipped in [full_sampled[::-1], full_sampled[:, ::-1], full_sampled[::-1, ::-1]]:
            self.assertGreater(np.abs(half_image - flipped).mean(), 30)


class TestTimeseriesAggregation(unittest.TestCase):

    def test_aggregations_skip_masked_values(self):
        # values outside the mask are NaN, as in HTMLGenerator.collect_timeseries_rows
        values = xr.DataArray(np.array([[1.0, np.nan, 7.0], [np.nan, 3.0, 10.0]]))
        expected = {"mean": np.nanmean, "min": np.nanmin, "max": np.nanmax, "median": np.nanmedian}
        for (aggregation_fn, numpy_fn) in expected.items():
            with self.subTest(aggregation_fn=aggregation_fn):
                result = HTMLGenerator.aggregate("TS", values, aggregation_fn)
                self.assertIsInstance(result, float)
                self.assertAlmostEqual(result, float(numpy_fn(values.data)))

    def test_aggregations_of_fully_masked_values_are_nan(self):
        values = xr.DataArray(np.full((2, 2), np.nan))
        for aggregation_fn in ["mean", "min", "max", "median"]:
            with self.subTest(aggregation_fn=aggregation_fn):
                self.assertTrue(np.isnan(HTMLGenerator.aggregate("TS", values, aggregation_fn)))

    def test_masked_median_in_csv(self):
        config = load_example_config()
        config["timeseries"]["ST_cloudy"]["variables"].append("B2:median")
        with tempfile.TemporaryDirectory() as tmp:
            HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                          add_download_link=False, show_progress=False).run()
            with open(os.path.join(tmp, "timeseries", "ST_cloudy.csv"), newline="") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), CASE_COUNT)

        # compare with the median of B2 over the CLOUD_MASK pixels, case by case in time order
        ds = xr.open_dataset(INPUT_PATH).sortby("time")
        for (i, row) in enumerate(rows):
            case = ds.isel(time=i)
            cloud_mask = np.logical_not(np.bitwise_and(case["QA_PIXEL"].data.astype(int), 8) == 0)
            expected = float(np.nanmedian(np.where(cloud_mask, case["B2"].data, np.nan)))
            self.assertNotEqual(row["CLOUD_MASK_B2_median"], "", f"median is blank for {row['datetime']}")
            self.assertAlmostEqual(float(row["CLOUD_MASK_B2_median"]), expected, places=5)


class TestConfigErrors(unittest.TestCase):

    def assertConfigError(self, layer_name, layer, *expected_fragments):
        with self.assertRaises(ConfigError) as cm:
            LayerFactory.create(STUB_CONVERTER, layer_name, layer)
        for fragment in expected_fragments:
            self.assertIn(fragment, str(cm.exception))

    def test_missing_type(self):
        self.assertConfigError("ST", {"band": "ST"}, "'ST'", "'type'")

    def test_unknown_type(self):
        self.assertConfigError("ST", {"type": "singel"}, "'ST'", "unknown type 'singel'")

    def test_missing_required_settings(self):
        cases = [
            ({"type": "single", "max_value": 1}, "'min_value'"),
            ({"type": "single", "min_value": 0}, "'max_value'"),
            ({"type": "rgb", "red_band": "B4", "green_band": "B3"}, "'blue_band'"),
            ({"type": "discrete"}, "'values'"),
            ({"type": "wms"}, "'url'"),
            ({"type": "static_image"}, "'image_path'"),
        ]
        for (layer, missing_key) in cases:
            with self.subTest(layer=layer):
                self.assertConfigError("L", layer, "'L'", missing_key)

    def test_unknown_cmap(self):
        self.assertConfigError("ST", {"type": "single", "min_value": 0, "max_value": 1, "cmap": "rainbw"},
                               "'ST'", "'rainbw'")

    def test_unknown_mask_colour(self):
        self.assertConfigError("M", {"type": "mask", "colour": "not-a-colour"}, "'M'", "'not-a-colour'")

    def test_layer_group_errors(self):
        self.assertConfigError("G", {"type": "layer_group", "layers": {}}, "'G'", "at least one layer")
        nested = {"type": "layer_group", "layers": {"inner": {"type": "layer_group", "layers": {}}}}
        self.assertConfigError("G", nested, "cannot be nested")

    def test_unsupported_layer_dimensions(self):
        layer = {"type": "single", "min_value": 0, "max_value": 1}
        self.assertConfigError("ST", dict(layer, dimensions={"case": "t"}), "'ST'", "'case'")
        self.assertConfigError("ST", dict(layer, dimensions={"z": "depth"}), "'ST'", "'z'")
        self.assertConfigError("ST", dict(layer, dimensions="x"), "'ST'", "'dimensions'")
        self.assertConfigError("ST", dict(layer, coordinates={"time": "t"}), "'ST'", "'time'")
        self.assertConfigError("ST", dict(layer, coordinates={"z": "depth"}), "'ST'", "'z'")
        self.assertConfigError("ST", dict(layer, coordinates="lon"), "'ST'", "'coordinates'")
        # x and y dimensions and coordinates may be set per layer
        created = LayerFactory.create(STUB_CONVERTER, "ST", dict(layer, dimensions={"x": "xx", "y": "yy"},
                                                                 coordinates={"x": "lon", "y": "lat"}))
        self.assertEqual((created.x_dimension, created.y_dimension), ("xx", "yy"))
        self.assertEqual((created.x_coordinate, created.y_coordinate), ("lon", "lat"))

    def test_layer_not_a_mapping(self):
        self.assertConfigError("ST", "single", "'ST'", "mapping")

    def test_timeseries_spec_errors(self):
        cases = [
            ({}, "'variables'"),
            ({"variables": []}, "non-empty list"),
            ({"variables": ["ST:average"]}, "'average'"),
            ({"variables": ["ST"], "type": "weekly"}, "'weekly'"),
            ({"variables": ["ST", "B2"], "type": "seasonal"}, "exactly 1 variable"),
        ]
        for (spec, expected) in cases:
            with self.subTest(spec=spec), self.assertRaises(ConfigError) as cm:
                HTMLGenerator.check_timeseries_spec("TS", spec)
            self.assertIn("'TS'", str(cm.exception))
            self.assertIn(expected, str(cm.exception))

    def test_valid_timeseries_spec(self):
        HTMLGenerator.check_timeseries_spec("TS", {"variables": ["ST", "B2:median"], "masks": ["CLOUD_MASK"]})
        HTMLGenerator.check_timeseries_spec("TS", {"variables": ["ST"], "type": "seasonal"})

    def test_invalid_derived_band_expression(self):
        config = load_example_config()
        config["derive_bands"]["CLOUD_MASK"] = "not (QA_PIXEL & 8 == 0"
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ConfigError) as cm:
            HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                          add_download_link=False, show_progress=False)
        self.assertIn("'CLOUD_MASK'", str(cm.exception))
        self.assertIn("unclosed_parenthesis", str(cm.exception))

    def test_config_is_not_modified(self):
        config = load_example_config()
        config["timeseries"]["ST_cloudy"]["variables"].append("NO_SUCH_VARIABLE")
        config["timeseries"]["ST_cloudy"]["masks"].append("NO_SUCH_MASK")
        expected = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp:
            gen = HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                                add_download_link=False, show_progress=False)
        self.assertEqual(config, expected)
        # the generator itself only keeps the variables and masks found in the input data
        (_, spec, _) = gen.timeseries_definitions[0]
        self.assertEqual(spec["variables"], ["ST"])
        self.assertEqual(spec["masks"], ["CLOUD_MASK", "EXTENDED_CLOUD_MASK"])

    def test_config_error_writes_no_output(self):
        config = load_example_config()
        del config["layers"]["ST"]["min_value"]
        with tempfile.TemporaryDirectory() as tmp:
            output_folder = os.path.join(tmp, "out")
            with self.assertRaises(ConfigError):
                HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=output_folder, title="t",
                              add_download_link=True, show_progress=False)
            self.assertFalse(os.path.exists(output_folder))


class TestGenerateHtmlConfigErrors(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_with_config(self, config_text, filename="config.yaml"):
        config_path = os.path.join(self.output_dir, filename)
        with open(config_path, "w") as f:
            f.write(config_text)
        return run_cli_expecting_failure("netcdf_explorer.cli.generate_html",
                                         ["--input-path", INPUT_PATH, "--title", "t", "--config-path", config_path,
                                          "--output-folder", os.path.join(self.output_dir, "out")],
                                         cwd=self.output_dir)

    def assertCleanError(self, result, *expected_fragments):
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        for fragment in expected_fragments:
            self.assertIn(fragment, result.stderr)

    def test_layer_missing_setting(self):
        config = load_example_config()
        del config["layers"]["ST"]["min_value"]
        result = self.run_with_config(yaml.dump(config))
        self.assertCleanError(result, "Error in config file", "'ST'", "'min_value'")

    def test_derived_band_unknown_function(self):
        config = load_example_config()
        config["derive_bands"]["ST_MAX"] = "max(ST)"
        result = self.run_with_config(yaml.dump(config))
        self.assertCleanError(result, "Error in config file", "'ST_MAX'", "unknown function 'max'")

    def test_missing_case_dimension(self):
        config = load_example_config()
        del config["dimensions"]["case"]
        self.assertCleanError(self.run_with_config(yaml.dump(config)), "case dimension")

    def test_not_yaml_extension(self):
        self.assertCleanError(self.run_with_config("{}", filename="config.json"), "yaml")

    def test_invalid_yaml(self):
        self.assertCleanError(self.run_with_config("layers: [unclosed"), "not valid yaml")

    def test_empty_config(self):
        self.assertCleanError(self.run_with_config(""), "mapping of settings")


if __name__ == "__main__":
    unittest.main()
