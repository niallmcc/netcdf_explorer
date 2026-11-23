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
INPUT_PATH = os.path.join(TEST_FOLDER, "greenland.nc")
CONFIG_PATH = os.path.join(TEST_FOLDER, "example_layers.yaml")

# the test dataset has 12 cases (time steps) on a grid 320 wide and 256 high (dimensions ni and nj).
# ST_coarse is surface temperature on a grid 4 times coarser (dimensions ni2 and nj2), covering the same area,
# and Copernicus_DEM is an elevation model that does not vary from case to case.
CASE_COUNT = 12
GRID_SIZE = (320, 256)
COARSE_GRID_SIZE = (80, 64)

# layers in example_layers.yaml built from the input data at full resolution (one image per case).
# ST_fine and ST_coarse are the two layers of the ST layer group.
# CLOUD_MASK and DISCRETE_ST use bands from derive_bands, so they also check expression evaluation.
# The OSM layer is left out because it is fetched over the network from a WMS server.
CASE_LAYERS = ["CLOUD_MASK", "ST_fine", "DISCRETE_ST", "REF"]

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


def read_data_file(path):
    """Read a data file written by DataEncoder: an int32 height and width, then the values as float32."""
    with open(path, "rb") as f:
        content = gzip.decompress(f.read())
    (height, width) = np.frombuffer(content[:8], dtype="<i4")
    return np.frombuffer(content[8:], dtype="<f4").reshape(height, width)


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
                # the coarse layer keeps its own resolution (TestLayerDimensions also checks its data file)
                with Image.open(os.path.join(output_folder, scene["image_srcs"]["ST_coarse"])) as im:
                    self.assertEqual(im.size, COARSE_GRID_SIZE)

        # the elevation layer does not vary by case, so every case shares one image and one data file
        self.assertEqual(len({scene["image_srcs"]["Copernicus_DEM"] for scene in scenes["index"]}), 1)
        self.assertEqual(len({scene["data_srcs"]["Copernicus_DEM"]["url"] for scene in scenes["index"]}), 1)
        self.assertEqual(scenes["terrain_view"],
                         {"elevation_layer": "Copernicus_DEM", "pixel_size_in_elevation_units": 50})

        # the ST layer group initially shows its first layer, ST_fine, in both the grid and overlay views
        self.assertEqual(scenes["layer_groups"], {"ST": ["ST_fine", "ST_coarse"]})
        with open(os.path.join(output_folder, "index.html")) as f:
            html = f.read()
        self.assertIn('<col id="ST_fine_col"/>', html)
        self.assertIn('<col id="ST_coarse_col" style="visibility:collapse;"/>', html)
        self.assertIn('<tr id="ST_fine_overlay_row">', html)
        self.assertIn('<tr id="ST_coarse_overlay_row" style="display:none;">', html)

        download_path = os.path.join(output_folder, os.path.basename(INPUT_PATH))
        self.assertEqual(os.path.exists(download_path), expect_download_link)

    def check_bigplot_image(self, path, plot_width):
        self.assertTrue(os.path.isfile(path), f"missing {path}")
        with Image.open(path) as im:
            width, height = im.size
        self.assertEqual(width, plot_width + 2 * BIGPLOT_BORDER)
        # the plot keeps the grid's shape, with a title and legend added below or above it
        self.assertGreaterEqual(height, plot_width * GRID_SIZE[1] / GRID_SIZE[0])

    def test_api(self):
        output_folder = os.path.join(self.output_dir, "output_api")
        with open(CONFIG_PATH) as f:
            config = yaml.load(f, Loader=yaml.FullLoader)
        gen = HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=output_folder, title="greenland",
                            add_download_link=True, filter_controls=True)
        gen.run()
        self.check_html_output(output_folder, expect_download_link=True)

    def test_commandline(self):
        output_folder = os.path.join(self.output_dir, "output_cli")
        run_cli("netcdf_explorer.cli.generate_html",
                ["--input-path", INPUT_PATH, "--title", "greenland", "--output-folder", output_folder,
                 "--config-path", CONFIG_PATH, "--download-data"], cwd=self.output_dir)
        self.check_html_output(output_folder, expect_download_link=True)

    def test_commandline_sample_cases(self):
        output_folder = os.path.join(self.output_dir, "output_sampled")
        run_cli("netcdf_explorer.cli.generate_html",
                ["--input-path", INPUT_PATH, "--title", "greenland", "--output-folder", output_folder,
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
        output_path = os.path.join(self.output_dir, "greenland.png")
        ds = xr.open_dataset(INPUT_PATH).isel(time=1)
        da = xr.concat([ds["B2"], ds["B3"], ds["B4"]], dim="band")
        bp = netcdf_explorer.api.bigplot.BigPlot(da, x="ni", y="nj", vmin=0, vmax=1, legend_height=0,
                                                 output_path=output_path, plot_width=1024)
        bp.run()
        self.check_bigplot_image(output_path, plot_width=1024)

    def test_bigplot_commandline(self):
        output_path = os.path.join(self.output_dir, "greenland_cmdline.png")
        run_cli("netcdf_explorer.cli.bigplot",
                ["--input-path", INPUT_PATH, "--iselector", "time", "1", "1", "--input-variable", "B2", "B3", "B4",
                 "--x", "ni", "--y", "nj", "--output-path", output_path, "--title", "greenland", "--vmin", "0", "--vmax", "0.3",
                 "--plot-width", "512"], cwd=self.output_dir)
        self.check_bigplot_image(output_path, plot_width=512)

    def test_bigplot_commandline_st(self):
        output_path = os.path.join(self.output_dir, "greenland_cmdline_st.png")
        run_cli("netcdf_explorer.cli.bigplot",
                ["--input-path", INPUT_PATH, "--iselector", "time", "1", "1", "--input-variable", "ST",
                 "--x", "ni", "--y", "nj", "--output-path", output_path, "--title", "greenland_st", "--vmin", "270", "--vmax", "330",
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
        # add variables whose dimensions are named xx and yy rather than ni and nj:
        # a 1-D profile along xx, and a 2-D field stored as (xx, yy), transposed relative to the other variables
        ds = xr.open_dataset(INPUT_PATH)
        st = ds["ST"].isel(time=0).data
        ds["XPROFILE"] = xr.DataArray(np.linspace(0, 1, ds.sizes["ni"]), dims=("xx",))
        ds["ST_T"] = xr.DataArray(st.T, dims=("xx", "yy"))
        # the same field stored upside down and back to front, with its own (reversed) coordinates
        ds["lon_rev"] = xr.DataArray(ds["lon"].data[::-1], dims=("xr",))
        ds["lat_rev"] = xr.DataArray(ds["lat"].data[::-1], dims=("yr",))
        ds["ST_R"] = xr.DataArray(st[::-1, ::-1], dims=("yr", "xr"))
        layers = {
            "ST": {"type": "single", "min_value": 270, "max_value": 300},
            "XPROFILE": {"type": "single", "min_value": 0, "max_value": 1, "dimensions": {"x": "xx"}},
            "ST_T": {"type": "single", "min_value": 270, "max_value": 300, "dimensions": {"x": "xx", "y": "yy"}},
            "ST_R": {"type": "single", "min_value": 270, "max_value": 300, "dimensions": {"x": "xr", "y": "yr"},
                     "coordinates": {"x": "lon_rev", "y": "lat_rev"}},
            # ST_coarse in the input data is ST averaged over 4x4 blocks of pixels, covering the same area
            "ST_coarse": {"type": "single", "min_value": 270, "max_value": 300, "data": {"label": "{value} K"},
                          "dimensions": {"x": "ni2", "y": "nj2"}, "coordinates": {"x": "lon_coarse", "y": "lat_coarse"}},
        }
        config = {"dimensions": {"case": "time", "x": "ni", "y": "nj"},
                  "coordinates": {"x": "lon", "y": "lat", "time": "time"}, "layers": layers}
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
            for name in layers:
                with Image.open(os.path.join(output_folder, first_case["image_srcs"][name])) as im:
                    images[name] = np.array(im)
            coarse_data = read_data_file(os.path.join(output_folder, first_case["data_srcs"]["ST_coarse"]["url"]))

        self.assertEqual(images["XPROFILE"].shape[:2], GRID_SIZE[::-1])
        # a profile along x gives an image whose rows are all the same
        self.assertTrue((images["XPROFILE"] == images["XPROFILE"][0]).all())
        # the transposed field gives the same image as the original field for that case
        self.assertTrue((images["ST_T"] == images["ST"]).all())
        # the reversed field is turned the right way up using its own coordinates
        self.assertTrue((images["ST_R"] == images["ST"]).all())
        # a layer with a different resolution keeps its own resolution, and the same orientation as the others:
        # its pixels match the full-resolution image's pixels at the same positions (allowing for averaging)
        self.assertEqual(images["ST_coarse"].shape[:2], COARSE_GRID_SIZE[::-1])
        np.testing.assert_allclose(coarse_data, ds["ST_coarse"].isel(time=0).data.astype("float32"))
        # (the mean colour difference is about 7 the right way up, and over 60 for any flip)
        coarse_image = images["ST_coarse"][:, :, :3].astype(int)
        full_sampled = images["ST"][2::4, 2::4, :3].astype(int)
        self.assertLess(np.abs(coarse_image - full_sampled).mean(), 20)
        for flipped in [full_sampled[::-1], full_sampled[:, ::-1], full_sampled[::-1, ::-1]]:
            self.assertGreater(np.abs(coarse_image - flipped).mean(), 40)

    def test_band_without_the_layer_dimensions_is_skipped(self):
        config = load_example_config()
        # ST is on the full resolution grid, not the coarse grid this layer is set up for
        config["layers"]["ST"]["layers"]["coarse"]["band"] = "ST"
        del config["layers"]["OSM"]
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs(level="WARNING") as logs:
            HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                          add_download_link=False, show_progress=False).run()
            with open(os.path.join(tmp, "scenes.json")) as f:
                scenes = json.load(f)
        layer_names = [layer["name"] for layer in scenes["layers"]]
        self.assertNotIn("ST_coarse", layer_names)
        self.assertIn("ST_fine", layer_names)
        # the rest of the layer group is kept
        self.assertEqual(scenes["layer_groups"], {"ST": ["ST_fine"]})
        self.assertTrue(any("ST_coarse" in message and "(time, nj, ni)" in message and "ni2" in message
                            for message in logs.output), logs.output)


class TestTerrainView(unittest.TestCase):

    def make_config(self, terrain_view, dem_layer=None):
        dem_layer = dem_layer if dem_layer is not None else {
            "type": "single", "band": "Copernicus_DEM", "min_value": 0, "max_value": 600, "data": {"label": "{value} m"}}
        return {"dimensions": {"case": "time", "x": "ni", "y": "nj"},
                "coordinates": {"x": "lon", "y": "lat", "time": "time"},
                "layers": {"ST": {"type": "single", "min_value": 270, "max_value": 300}, "DEM": dem_layer},
                "terrain_view": terrain_view}

    def generate(self, config, output_folder):
        HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=output_folder, title="t",
                      add_download_link=False, show_progress=False).run()
        with open(os.path.join(output_folder, "scenes.json")) as f:
            return json.load(f)

    def test_elevation_data_is_written_for_the_page(self):
        for terrain_view in [{"based_on_layer": "DEM", "pixel_size_in_elevation_units": 30},
                             {"elevation_band": "DEM", "pixel_size_in_elevation_units": 30}]:
            with self.subTest(terrain_view=terrain_view), tempfile.TemporaryDirectory() as tmp:
                scenes = self.generate(self.make_config(terrain_view), tmp)
                # the page is given the same settings, whichever name the configuration used for the layer
                self.assertEqual(scenes["terrain_view"],
                                 {"elevation_layer": "DEM", "pixel_size_in_elevation_units": 30})
                with open(os.path.join(tmp, "index.html")) as f:
                    html = f.read()
                self.assertIn('id="terrain_view_btn"', html)
                self.assertIn("dependencies/babylon.js", html)
                elevation = read_data_file(os.path.join(tmp, scenes["index"][0]["data_srcs"]["DEM"]["url"]))
        dem = xr.open_dataset(INPUT_PATH)["Copernicus_DEM"].data.astype("float32")
        np.testing.assert_array_equal(elevation, dem)

    def test_terrain_view_config_errors(self):
        cases = [
            (self.make_config({"pixel_size_in_elevation_units": 30}), "based_on_layer"),
            (self.make_config({"based_on_layer": "DEMX"}), "'DEMX'"),
            (self.make_config({"based_on_layer": "DEM"},
                              dem_layer={"type": "single", "band": "Copernicus_DEM", "min_value": 0, "max_value": 600}),
             "'data'"),
        ]
        for (config, expected_fragment) in cases:
            with self.subTest(terrain_view=config["terrain_view"]), tempfile.TemporaryDirectory() as tmp, \
                    self.assertRaises(ConfigError) as cm:
                HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                              add_download_link=False, show_progress=False)
            self.assertIn("terrain_view", str(cm.exception))
            self.assertIn(expected_fragment, str(cm.exception))

    def test_terrain_view_is_left_out_if_its_layer_cannot_be_added(self):
        config = self.make_config({"based_on_layer": "DEM"}, dem_layer={
            "type": "single", "band": "NO_SUCH_BAND", "min_value": 0, "max_value": 600, "data": {"label": "{value}"}})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(level="WARNING") as logs:
                scenes = self.generate(config, tmp)
            with open(os.path.join(tmp, "index.html")) as f:
                html = f.read()
        self.assertNotIn("terrain_view", scenes)
        self.assertNotIn('id="terrain_view_btn"', html)
        self.assertTrue(any("terrain view" in message for message in logs.output), logs.output)


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

        # compare with the median of B2 over the EXTENDED_CLOUD_MASK pixels, case by case in time order
        ds = xr.open_dataset(INPUT_PATH).sortby("time")
        for (i, row) in enumerate(rows):
            case = ds.isel(time=i)
            cloud_mask = np.logical_not(np.bitwise_and(case["QA_PIXEL"].data.astype(int), 30) == 0)
            expected = float(np.nanmedian(np.where(cloud_mask, case["B2"].data, np.nan)))
            self.assertNotEqual(row["EXTENDED_CLOUD_MASK_B2_median"], "", f"median is blank for {row['datetime']}")
            self.assertAlmostEqual(float(row["EXTENDED_CLOUD_MASK_B2_median"]), expected, places=5)


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
        expected = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs(level="WARNING") as logs:
            gen = HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                                add_download_link=False, show_progress=False)
            gen.close()
        self.assertEqual(config, expected)
        # the generator itself only keeps the variables found in the input data, with a warning for the others
        (_, spec, _) = gen.timeseries_definitions[0]
        self.assertEqual(spec["variables"], ["ST"])
        self.assertEqual(spec["masks"], ["EXTENDED_CLOUD_MASK"])
        self.assertTrue(any("ST_cloudy" in message and "NO_SUCH_VARIABLE" in message for message in logs.output),
                        logs.output)

    def test_timeseries_without_any_variables_in_the_data_is_dropped(self):
        config = load_example_config()
        config["timeseries"]["ST_cloudy"]["variables"] = ["NO_SUCH_VARIABLE"]
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs(level="WARNING") as logs:
            gen = HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                                add_download_link=False, show_progress=False)
            gen.close()
        self.assertEqual(gen.timeseries_definitions, [])
        self.assertTrue(any("Unable to add timeseries ST_cloudy" in message for message in logs.output), logs.output)

    def test_unknown_timeseries_mask(self):
        config = load_example_config()
        config["timeseries"]["ST_cloudy"]["masks"].append("NO_SUCH_MASK")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ConfigError) as cm:
            HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                          add_download_link=False, show_progress=False)
        self.assertIn("'ST_cloudy'", str(cm.exception))
        self.assertIn("'NO_SUCH_MASK'", str(cm.exception))

    def test_timeseries_mask_from_a_derived_band_that_fails_is_left_out(self):
        config = load_example_config()
        # BROKEN_MASK is in derive_bands, but cannot be evaluated, which is reported as a warning
        config["derive_bands"]["BROKEN_MASK"] = "NO_SUCH_BAND > 1"
        config["timeseries"]["ST_cloudy"]["masks"].append("BROKEN_MASK")
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs(level="WARNING") as logs:
            gen = HTMLGenerator(config=config, input_path=INPUT_PATH, output_folder=tmp, title="t",
                                add_download_link=False, show_progress=False)
            gen.close()
        self.assertTrue(any("BROKEN_MASK" in message for message in logs.output), logs.output)
        (_, spec, _) = gen.timeseries_definitions[0]
        self.assertEqual(spec["masks"], ["EXTENDED_CLOUD_MASK"])

    def test_config_error_writes_no_output(self):
        config = load_example_config()
        del config["layers"]["ST"]["layers"]["fine"]["min_value"]
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
        del config["layers"]["ST"]["layers"]["fine"]["min_value"]
        result = self.run_with_config(yaml.dump(config))
        self.assertCleanError(result, "Error in config file", "'ST_fine'", "'min_value'")

    def test_derived_band_unknown_function(self):
        config = load_example_config()
        config["derive_bands"]["ST_MAX"] = "max(ST)"
        result = self.run_with_config(yaml.dump(config))
        self.assertCleanError(result, "Error in config file", "'ST_MAX'", "unknown function 'max'")

    def test_unknown_timeseries_mask(self):
        config = load_example_config()
        config["timeseries"]["ST_cloudy"]["masks"] = ["CLOUD_MASK"]
        result = self.run_with_config(yaml.dump(config))
        self.assertCleanError(result, "Error in config file", "'ST_cloudy'", "'CLOUD_MASK'", "derive_bands")

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
