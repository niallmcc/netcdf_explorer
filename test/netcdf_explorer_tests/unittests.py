import json
import os
import subprocess
import sys
import tempfile
import unittest

import xarray as xr
import yaml
from PIL import Image

import netcdf_explorer.api.bigplot
from netcdf_explorer.api.html_generator import HTMLGenerator

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
                 "--config-path", CONFIG_PATH, "--sample-cases", "0", "2"], cwd=self.output_dir)
        with open(os.path.join(output_folder, "scenes.json")) as f:
            scenes = json.load(f)
        self.assertEqual(len(scenes["index"]), 2)
        self.assertFalse(os.path.exists(os.path.join(output_folder, os.path.basename(INPUT_PATH))))

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


if __name__ == "__main__":
    unittest.main()
