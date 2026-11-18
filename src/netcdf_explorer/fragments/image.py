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

from ..htmlfive.html5_builder import Html5Builder, Fragment, ElementFragment
import base64
from .utils import prepare_attrs

class ImageFragment(ElementFragment):

    def __init__(self, src, id, alt_text="", w=None, h=None, load_url=None):
        attrs = {
            "src": src, "alt":alt_text, "id":id, "width": w, "height": h
        }
        if load_url is not None:
            attrs["load_url"] = load_url
        else:
            attrs["loading"] = "lazy"
        super().__init__("img", prepare_attrs(attrs))


def inlined_image(from_path,mime_type="image/png"):
    with open(from_path,"rb") as f:
        content_bytes = f.read()
    return "data:" + mime_type + ";charset=US-ASCII;base64," + str(base64.b64encode(content_bytes), "utf-8")


class InlineImageFragment(ElementFragment):

    def __init__(self, path, alt_text="", w=None, h=None):
        super().__init__("img", prepare_attrs({
            "src": inlined_image(path), "alt":alt_text, "width": w, "height": h}))
