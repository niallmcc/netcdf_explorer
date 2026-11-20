var cmap = new CMap("cmaps");

class DataImage {

    constructor() {
        this.layer_names = [];
        this.data_layers = {};
        this.layer_sizes = {}; // layer_name => {"width": ..., "height": ...}, layers may differ in resolution
        this.layer_options = {};
        this.ele = null;
        this.mouseover_listener = null;
        this.mouseout_listener = null;
        this.zoom = 1;
    }

    register_layer(layer_name) {
        this.layer_names.push(layer_name);
    }

    async load(layer_name, data_source) {
        let from_url = data_source.url;
        let options = data_source.options;
        let fetched = await fetch(from_url);
        let blob = await fetched.blob();
        const ds = new DecompressionStream("gzip");
        const decompressedStream = blob.stream().pipeThrough(ds);
        let r = await new Response(decompressedStream).arrayBuffer();
        let dv = new DataView(r);
        let height = dv.getInt32(0, true);
        let width = dv.getInt32(4, true);

        let pos = 8;
        let data = [];
        for (let y = 0; y < height; y++) {
            let row = [];
            for (let x = 0; x < width; x++) {
                let v = dv.getFloat32(pos, true /* littleEndian */);
                row.push(v);
                pos += 4;
            }
            data.push(row);
        }
        this.data_layers[layer_name] = data;
        this.layer_sizes[layer_name] = {"width": width, "height": height};
        this.layer_options[layer_name] = options;
    }

    get_data(layer_name, x, y) {
        return this.data_layers[layer_name][y][x];
    }

    get_height(layer_name) {
        return this.layer_sizes[layer_name].height;
    }

    get_width(layer_name) {
        return this.layer_sizes[layer_name].width;
    }

    get_value(layer_name, y, x) {
        return this.data_layers[layer_name][y][x];
    }

    get_legend_url(cmap_name, vmin, vmax, height, width) {
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        ctx.canvas.width = width;
        ctx.canvas.height = height;

        for (let x = 0; x < width; x++) {
            let v = vmin + ((x + 0.5) / (width)) * (vmax - vmin);
            for (let y = 0; y < height; y++) {
                let rgb = cmap.get_rgb(cmap_name, vmin, vmax, v);
                ctx.fillStyle = "rgb(" + 255 * rgb[0] + "," + 255 * rgb[1] + "," + 255 * rgb[2] + ")";
                ctx.fillRect(x, y, 1, 1);
            }
        }
        return canvas.toDataURL("image/png");
    }

    get_image_url(layer_name, cmap_name, vmin, vmax) {
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        let width = this.get_width(layer_name);
        let height = this.get_height(layer_name);
        ctx.canvas.width = width;
        ctx.canvas.height = height;
        let data = this.data_layers[layer_name];
        for (let y = 0; y < height; y++) {
            for (let x = 0; x < width; x++) {
                let v = data[y][x];
                let rgb = cmap.get_rgb(cmap_name, vmin, vmax, v);
                if (rgb !== null) {
                    ctx.fillStyle = "rgb(" + 255 * rgb[0] + "," + 255 * rgb[1] + "," + 255 * rgb[2] + ")";
                    ctx.fillRect(x, y, 1, 1);
                }
            }
        }
        return canvas.toDataURL("image/png");
    }

    set_zoom(zoom) {
        console.log("set_zoom:" + zoom);
        this.zoom = zoom;
    }

    get_popup_html(y_frac, x_frac) {
        let s = "";
        this.layer_names.forEach((layer_name) => {
            let v = "[loading]";
            if (layer_name in this.data_layers) {
                // convert the position on the map to a pixel in this layer, which may differ in resolution from others
                let width = this.get_width(layer_name);
                let height = this.get_height(layer_name);
                let x = Math.min(Math.floor(x_frac * width), width - 1);
                let y = Math.min(Math.floor((1-y_frac) * height), height - 1);
                v = this.get_value(layer_name, y, x);
            }
            let label = "{value}";
            let fixed = 2;
            if (layer_name in this.layer_options) {
                let options = this.layer_options[layer_name];
                if ("label" in options) {
                    label = this.layer_options[layer_name].label;
                }
                if ("fixed" in options) {
                    fixed = options.fixed;
                }
            }

            if (!isNaN(v)) {
                v = v.toFixed(fixed);
            } else {
                v = "(missing)";
            }
            s += "<p>" + label.replace("{value}", v) + "</p>";

        });
        return s;
    }
}

