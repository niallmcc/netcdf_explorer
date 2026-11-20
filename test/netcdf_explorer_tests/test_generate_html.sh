#!/bin/bash

python -m netcdf_explorer.cli.generate_html \
    --input-path  area_293_min.nc \
    --title area_293 \
    --output-folder test_output_html \
    --config-path example_layers.yaml \
    --download-data