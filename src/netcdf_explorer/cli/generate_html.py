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

import argparse
import os
import logging
import yaml
import sys

from netcdf_explorer.api.html_generator import HTMLGenerator
from netcdf_explorer.api.config_error import ConfigError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", help="Set the title of the plot", required=True)
    parser.add_argument("--input-path", help="the netcdf4 file to visualise", required=True)
    parser.add_argument("--download-data", action="store_true",
                        help="include a download link in the HTML")
    parser.add_argument("--output-folder", help="folder to write html output", default="html_output")
    parser.add_argument("--config-path", help="YAML file specifying one or more layer specifications",
                        required=True)
    parser.add_argument("--sample-count", type=int, default=None,
                        help="randomly sample this many cases for display"),
    parser.add_argument("--sample-cases", nargs="+", type=int, default=None,
                        help="display these sample cases (provide their indices, starting at 0)")
    parser.add_argument("--filter-controls", action="store_true",
                        help="add month filter controls to the overlay view")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("generate_html")

    if not (args.config_path.endswith(".yml") or args.config_path.endswith(".yaml")):
        logger.error(f"Config file {args.config_path} should be in yaml (.yml or .yaml) format")
        sys.exit(1)

    with open(args.config_path) as f:
        try:
            config = yaml.load(f, Loader=yaml.FullLoader)
        except yaml.YAMLError as ex:
            logger.error(f"Config file {args.config_path} is not valid yaml: {ex}")
            sys.exit(1)

    if not isinstance(config, dict):
        logger.error(f"Config file {args.config_path} should contain a mapping of settings")
        sys.exit(1)

    if config.get("dimensions", {}).get("case", None) is None:
        logger.error(f"Config file {args.config_path} should provide a case dimension (dimensions: case: ...)")
        sys.exit(1)

    input_path = os.path.abspath(args.input_path)
    output_folder = os.path.abspath(args.output_folder)

    try:
        g = HTMLGenerator(config, input_path, output_folder, title=args.title, add_download_link=args.download_data,
                          filter_controls=args.filter_controls, sample_count=args.sample_count,
                          sample_cases=args.sample_cases)
        g.run()
    except ConfigError as ex:
        logger.error(f"Error in config file {args.config_path}: {ex}")
        sys.exit(1)


if __name__ == '__main__':
    main()
