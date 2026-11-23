#!/bin/bash

python -m netcdf_explorer.cli.generate_html \
    --input-path  greenland.nc \
    --title Greenland \
    --output-folder test_output_html \
    --config-path example_layers.yaml \
    --download-data