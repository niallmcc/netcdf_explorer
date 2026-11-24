# generate_html configuration file

`generate_html` reads a YAML configuration file (`.yaml` or `.yml`), passed with `--config-path`. The file says
how the input netCDF4 file is laid out and which layers, timeseries and other views to build from it.

A complete, commented example is [example_layers.yaml](../test/netcdf_explorer_tests/example_layers.yaml).
It builds a page from the test dataset [greenland.nc](../test/netcdf_explorer_tests/greenland.nc).

## Contents

- [Overview](#overview)
- [dimensions](#dimensions)
- [coordinates](#coordinates)
- [image](#image)
- [info](#info)
- [crs](#crs)
- [derive_bands](#derive_bands)
- [layers](#layers)
  - [Settings for every layer](#settings-for-every-layer)
  - [single](#single)
  - [mask](#mask)
  - [discrete](#discrete)
  - [rgb](#rgb)
  - [wms](#wms)
  - [layer_group](#layer_group)
  - [static_image](#static_image)
  - [vector](#vector)
  - [Colours](#colours)
- [timeseries](#timeseries)
- [terrain_view](#terrain_view)
- [Errors and warnings](#errors-and-warnings)

## Overview

The input data is split into *cases*, usually one per time step, along a case dimension. Each case is shown as a
row in the page's grid view and as one scene in its overlay view, a map on which the layers are stacked.

A configuration file has these top-level sections. Only `dimensions` and `coordinates` are always needed:

```yaml
dimensions:     # which dimensions hold the cases and the x and y axes
coordinates:    # which variables hold the x, y and time coordinates
image:          # optional: image size in the grid view
info:           # optional: text to show for each case
crs:            # optional: the projection of the x and y coordinates
derive_bands:   # optional: new variables computed from expressions
layers:         # the images to show for each case
timeseries:     # optional: charts of values over time
terrain_view:   # optional: a 3D view draped over an elevation model
```

## dimensions

```yaml
dimensions:
  case: time
  x: ni
  y: nj
```

| Setting | Description |
|---|---|
| `case` | The dimension that separates the cases. Variables that vary from case to case have this dimension. Variables without it, such as an elevation model, are the same for every case. |
| `x` | The dimension of the horizontal (west to east) axis. |
| `y` | The dimension of the vertical (south to north) axis. |

A layer can use different x and y dimensions, for example a variable on a coarser grid. See
[Settings for every layer](#settings-for-every-layer).

## coordinates

```yaml
coordinates:
  x: lon
  y: lat
  time: time
```

| Setting | Description |
|---|---|
| `x` | The variable holding the x coordinate of each column, for example longitude. |
| `y` | The variable holding the y coordinate of each row, for example latitude. |
| `time` | The variable holding each case's date and time. It is needed for timeseries, and for the dates the page shows and lets you pick. |

The x and y coordinates must be 1-dimensional and evenly spaced. A 2-dimensional coordinate is accepted if every
row (or every column) is the same, since it can then be reduced to one dimension.

The coordinates also decide which way up each image is drawn. If x decreases from left to right, or y increases
from top to bottom, the image is flipped so that north is at the top and west on the left.

The coordinates are used as map positions for a `wms` layer, so they must be in the CRS that the WMS request uses.

## image

```yaml
image:
  grid-width: 250
```

| Setting | Description |
|---|---|
| `grid-width` | The width, in pixels, of each image in the grid view. The height is set to keep the data's aspect ratio. Without this setting, images are shown at the data's own width. |
| `max-zoom` | Accepted, but not currently used. |

## info

Text to show for each case, in the grid view's info column and the overlay view's information panel. Each entry is
a label and a [Mako](https://www.makotemplates.org/) template:

```yaml
info:
  Date: "${str(data['time'].data)[0:19]}"
  Platform: "${data['platform'].item()}"
```

A template can use:

| Name | Value |
|---|---|
| `data` | The xarray Dataset for the case. |
| `index` | The case's position, counting from 0, in the input file, or among the cases chosen with `--sample-count` or `--sample-cases`. |
| `lon`, `lat` | The longitude and latitude of the centre of the case's area. Only available if [`crs`](#crs) is set. |

A template that fails is left out for that case, with a warning.

## crs

The coordinate reference system of the x and y coordinates, in any form pyproj accepts, for example `EPSG:3413`.
It is only used to work out `lon` and `lat` for the [`info`](#info) templates.

```yaml
crs: "EPSG:3413"
```

## derive_bands

New variables computed from the input variables. Each entry is a name and an expression. Layers, timeseries and
later expressions can then use the new variable like any other.

```yaml
derive_bands:
  CLOUD_MASK: not (QA_PIXEL & 8 == 0)
  WARM: ST > 290
  DISCRETE_TEMP: IF(ST > 290, 3, IF(ST > 280, 2, 1))
```

An expression can use variable names, numbers, parentheses and these operators:

| Operators | Meaning |
|---|---|
| `-` (before a value), `not` | Negation and logical not. These bind most tightly, so `not A and B` means `(not A) and B`. |
| `*` `/` | Multiplication and division. |
| `+` `-` | Addition and subtraction. |
| `&` `\|` | Bitwise and/or. The values are converted to integers first. |
| `==` `>` `>=` `<` `<=` | Comparisons. |
| `and` `or` | Logical and/or. The values are converted to integers first, so a value between 0 and 1 counts as false. |

Operators in each row bind more tightly than those in the rows below, and operators in the same row are applied
from left to right. So `QA_PIXEL & 8 == 0` means `(QA_PIXEL & 8) == 0`. Use parentheses to change the order.

The one function is `IF(condition, value_if_true, value_if_false)`. Function names are not case sensitive.

An expression with a syntax error, an unknown function or the wrong number of arguments stops `generate_html`
with an error. An expression that fails when it is evaluated, for example because it uses a variable that is not
in the input file, is left out with a warning.

## layers

Each entry under `layers` is one layer: a name, then its settings. The `type` setting decides what the layer
shows:

| Type | Shows |
|---|---|
| [`single`](#single) | A continuous variable, coloured with a colour map. |
| [`mask`](#mask) | Where a variable is greater than 0, in one colour, transparent elsewhere. |
| [`discrete`](#discrete) | A variable with a few distinct values, each in its own colour. |
| [`rgb`](#rgb) | Three variables as the red, green and blue of a false colour image. |
| [`wms`](#wms) | A background map fetched from a WMS server. |
| [`layer_group`](#layer_group) | Several layers, one shown at a time, with a selector to choose which. |
| [`static_image`](#static_image) | An image file, the same for every case. |
| [`vector`](#vector) | Lines drawn from a JSON description stored in a variable. |

Layers are shown in the order they are listed. In the grid view, each layer is a column. In the overlay view,
layers listed first are drawn on top.

### Settings for every layer

| Setting | Default | Description |
|---|---|---|
| `type` | (required) | One of the types above. |
| `label` | the layer's name | The name shown on the page. |
| `grid_view` | `true` | Set to `false` to leave the layer out of the grid view. |
| `overlay_view` | `true` | Set to `false` to leave the layer out of the overlay view. |
| `selectors` | none | Picks one position along any other dimension the variable has, as `{dimension: index}`. For example, `{depth: 0}` uses the first depth. |
| `dimensions` | the top-level `x` and `y` | The layer's own `x` and `y` dimensions. |
| `coordinates` | the top-level `x` and `y` | The layer's own `x` and `y` coordinates. |

`grid_view` and `overlay_view` apply to top-level layers and layer groups, not to the layers inside a group.

`dimensions` and `coordinates` are for a variable whose dimensions or coordinates have different names from the
rest of the file. The variable may have a different resolution, but it should cover the same area, because its
image is stretched over the same map area as the other layers. The case dimension and time coordinate can only be
set at the top level.

```yaml
  ST_coarse:
    type: single
    band: ST_coarse
    min_value: 270
    max_value: 300
    dimensions:
      x: ni2
      y: nj2
    coordinates:
      x: lon_coarse
      y: lat_coarse
```

### single

A continuous variable, coloured with a matplotlib colour map. The page shows a legend, and in the overlay view
the colour map and range can be changed.

```yaml
  ST:
    label: Surface Temperature
    type: single
    band: ST
    min_value: 270
    max_value: 300
    cmap: rainbow
    data:
      label: "B10_ST: {value} K"
      fixed: 2
```

| Setting | Default | Description |
|---|---|---|
| `band` | the layer's name | The variable to show. |
| `min_value` | (required) | The value at the bottom of the colour map. |
| `max_value` | (required) | The value at the top of the colour map. |
| `cmap` | `coolwarm` | A [matplotlib colour map](https://matplotlib.org/stable/users/explain/colors/colormaps.html) name. |
| `data` | none | Saves the layer's values for the page. The overlay view then shows the value under the mouse, and the layer can be used by a [terrain view](#terrain_view). |

`data` has two settings:

| Setting | Default | Description |
|---|---|---|
| `label` | `{value}` | The text to show, with `{value}` replaced by the value. |
| `fixed` | `2` | The number of decimal places. |

### mask

Shows the pixels where a variable is greater than 0 in one colour, and leaves the rest transparent. This is useful for
cloud or quality masks, often made with [`derive_bands`](#derive_bands).

```yaml
  CLOUD_MASK:
    label: Cloud Mask
    type: mask
    band: CLOUD_MASK
    r: 150
    g: 150
    b: 150
```

| Setting | Default | Description |
|---|---|---|
| `band` | the layer's name | The variable to show. Its values are converted to integers, and pixels greater than 0 are coloured. |
| `colour` | none | A colour name. See [Colours](#colours). |
| `r`, `g`, `b` | `0` | The red, green and blue of the colour (0 to 255), used if `colour` is not set. |

### discrete

A variable with a small number of distinct values, each shown in its own colour. The overlay view shows a key.

```yaml
  DISCRETE_ST:
    label: Surface Temperature Discrete
    type: discrete
    band: DISCRETE_TEMP
    values:
      1: ["low", "blue"]
      2: ["medium", "purple"]
      3: ["high", "red"]
```

| Setting | Default | Description |
|---|---|---|
| `band` | the layer's name | The variable to show. |
| `values` | (required) | Maps each value to a `[label, colour]` pair. The colour is a name or `#RRGGBB`; see [Colours](#colours). |

Pixels with any other value are shown in black.

### rgb

A false colour image, with three variables as the red, green and blue channels.

```yaml
  REF:
    label: Reflectance
    type: rgb
    red_band: B4
    green_band: B3
    blue_band: B2
    red_gamma: 0.5
    green_gamma: 0.5
    blue_gamma: 0.5
    low_percentile: 2
    high_percentile: 98
```

| Setting | Default | Description |
|---|---|---|
| `red_band`, `green_band`, `blue_band` | (required) | The variables for each channel. |
| `red_gamma`, `green_gamma`, `blue_gamma` | `0.5` | Gamma correction for each channel. Values below 1 brighten the channel. |
| `min_value`, `max_value` | `0`, `1` | The value range that is stretched over each channel's full brightness. |
| `low_percentile`, `high_percentile` | none | Instead of `min_value` and `max_value`, set the range from percentiles of all three variables together, for example 2 and 98. |

Set either `min_value`/`max_value` or `low_percentile`/`high_percentile`, not both. If both are set,
`min_value` and `max_value` are used.

### wms

A map image fetched from a [WMS](https://www.ogc.org/standards/wms) server, covering each case's area. It is
often used as a background, so it's usually listed last.

```yaml
  OSM:
    label: Open Street Map
    type: wms
    scale: 1
    url: "https://ows.terrestris.de/osm/service?SERVICE=WMS&VERSION=1.1.1&request=GetMap&layers=OSM-WMS&styles=&format=image%2Fpng&transparent=false&width={WIDTH}&height={HEIGHT}&srs=EPSG%3A4326&bbox={XMIN},{YMIN},{XMAX},{YMAX}"
```

| Setting | Default | Description |
|---|---|---|
| `url` | (required) | The GetMap request URL, with placeholders `{WIDTH}`, `{HEIGHT}`, `{XMIN}`, `{YMIN}`, `{XMAX}` and `{YMAX}`. |
| `scale` | `1` | Multiplies the requested image size, for a more detailed image. |

The bounding box comes from the x and y coordinates, so the URL must ask for the same CRS as those coordinates
(`srs=` or `crs=` in the URL). The image is fetched once per distinct area. If a request fails, the layer has no
image for that case.

### layer_group

Several layers that take turns in the same place. Only one is shown at a time, with a selector in the grid view
column heading and in the overlay view's layer controls to choose which. The first layer in the group is shown
to begin with.

```yaml
  ST:
    label: Surface Temperature
    type: layer_group
    layers:
      fine:
        label: Full resolution
        type: single
        band: ST
        min_value: 270
        max_value: 300
        cmap: rainbow
      coarse:
        label: Coarse (4x4 pixel means)
        type: single
        band: ST_coarse
        min_value: 270
        max_value: 300
        cmap: rainbow
        dimensions:
          x: ni2
          y: nj2
        coordinates:
          x: lon_coarse
          y: lat_coarse
```

| Setting | Description |
|---|---|
| `layers` | The layers in the group, in the same form as top-level layers. At least one is required, and groups cannot be nested. |

Each layer in a group is named `<group name>_<layer name>`, so the layers above are `ST_fine` and `ST_coarse`.
Use these names in [`terrain_view`](#terrain_view). If a layer in the group cannot be built, it is left out and
the rest of the group is kept.

### static_image

An existing image file, shown the same for every case and stretched over the map area.

```yaml
  Outline:
    label: Coastline
    type: static_image
    image_path: images/coastline.png
```

| Setting | Description |
|---|---|
| `image_path` | (required) The path of the image file, relative to the folder `generate_html` is run from. |

### vector

Lines drawn over the map. The variable must hold, for each case, a JSON string: a list of lines, each a list of
`[y, x]` pixel positions. `x` counts columns from the left (west) edge and `y` counts rows from the bottom
(south) edge, both from 0.

```yaml
  TRACKS:
    label: Tracks
    type: vector
    band: tracks
    thickness: 2
    colour: [255, 0, 0, 255]
```

For example, the value `[[[0, 0], [10, 20]]]` draws one line from the bottom-left pixel to the pixel 20 columns
across and 10 rows up.

| Setting | Default | Description |
|---|---|---|
| `band` | the layer's name | The variable holding the JSON. |
| `scale` | `10` | How many times larger than the data the image is drawn, for smoother lines. |
| `thickness` | `3` | The line width, in pixels of the scaled image. |
| `colour` | `[0, 0, 0, 255]` | The line colour as `[red, green, blue, alpha]`, each 0 to 255. |

### Colours

Where a setting takes a colour name, these names can be used:

violet, magenta, purple, indigo, pink, crimson, darkred, red, darkorange, orange, yellow, lightyellow, gold,
brown, lightgreen, green, darkgreen, cyan, lightblue, blue, darkblue, white, lightgray, darkgray, gray, black.

A `discrete` layer also accepts `#RRGGBB` colours.

## timeseries

Charts of values over time, shown in the page's timeseries view. Each chart's values are also written to a CSV
file in the output's `timeseries/` folder. Timeseries need the [`time` coordinate](#coordinates).

```yaml
timeseries:
  ST_cloudy:
    label: Mean temperature of cloudy pixels
    masks: ["CLOUD_MASK"]
    variables: ["ST", "ST:max"]
```

| Setting | Default | Description |
|---|---|---|
| `variables` | (required) | A list of the variables to chart. |
| `masks` | none | A list of mask variables. |
| `label` | the timeseries' name | The chart heading. |
| `type` | `timeseries` | `timeseries` charts each value against date. `seasonal` charts one line per year, against day of the year, and needs exactly one variable (and at most one mask). |

**With masks**, each variable is reduced to one number per case, over the pixels where the mask is non-zero. A
chart line is drawn for each combination of mask and variable. By default the mean is used; add `:min`, `:max`,
`:median` or `:mean` to a variable name to choose, for example `ST:median`. A mask is a variable from the input
file or from [`derive_bands`](#derive_bands).

**Without masks**, each variable must already hold one value per case, for example a variable whose only
dimension is the case dimension.

## terrain_view

Adds a 3D view to the overlay view. The layers that are currently shown are draped over an elevation model and can
be rotated and zoomed, with a slider to exaggerate the terrain.

```yaml
terrain_view:
  based_on_layer: Copernicus_DEM
  pixel_size_in_elevation_units: 30
```

| Setting | Default | Description |
|---|---|---|
| `based_on_layer` | (required) | The name of the `single` layer holding the elevations. The layer needs a `data` setting, so that its values are saved for the page. (Older configurations call this setting `elevation_band`.) |
| `pixel_size_in_elevation_units` | `50` | The width of a pixel, in the same units as the elevations. For 30 m pixels and elevations in metres, use 30. |

## Errors and warnings

`generate_html` checks the configuration before it writes anything.

**Mistakes in the configuration stop it with an error** that names the setting. For example:
- an unknown layer type, colour map, or `mask` colour name
- a missing required setting
- a layer group with no layers, or one nested inside another
- a layer that sets its own case dimension or time coordinate
- an invalid expression in `derive_bands`
- an unknown timeseries `type` or aggregation (such as `ST:sum`), or a `seasonal` timeseries without exactly
  one variable
- a timeseries mask that is neither an input variable nor a derived band
- a `terrain_view` layer that doesn't exist or has no `data` setting

**Data missing from the input file is skipped with a warning**, so the same configuration can be used with files
that don't all have the same variables:
- a layer whose variable is missing is left out
- a timeseries variable that is missing is left out of its chart, and a timeseries with no variables left is
  left out
- if the `terrain_view` layer is left out, so is the terrain view
