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


class ConfigError(ValueError):
    """Raised when the layer configuration (YAML) is invalid.

    The message names the layer, timeseries or band concerned and the setting that is wrong,
    so it can be shown to the user directly, without a traceback.
    """


def require(spec, key, kind, name):
    """Return spec[key], raising ConfigError if the setting is missing.

    Arguments:
        spec: the dictionary read from the configuration for one layer or timeseries
        key: the name of the required setting
        kind: a description of what spec configures, for example "layer" or "timeseries"
        name: the name of the layer or timeseries
    """
    if key not in spec:
        raise ConfigError(f"{kind} '{name}' is missing required setting '{key}'")
    return spec[key]
