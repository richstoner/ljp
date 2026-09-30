# lajollapalisades.com

The website for La Jolla Palisades I: one static page with the DC&Rs, a map of every lot, and how to reach the Architectural Committee. GitHub Pages serves it straight from `main`, with no build step at deploy time.

## Files

- `index.html` is the whole site. Styles and script are inline.
- `assets/images/hero.jpg` is the header photo.
- `data/parcels.geojson` holds the lot polygons, with address, lot, unit, and APN only. The page offers it as a download.
- `data/streets.geojson` holds street centerlines and canyon scrub from OpenStreetMap.
- `tools/build_map.py` draws the map from those two files and writes the SVG into `index.html` between the `map:start` and `map:end` markers.
- `404.html` sends old links, such as `/about` and `/events`, to the home page.
- `CNAME` and `.nojekyll` are for GitHub Pages.

## Updating the map

We run the build with [uv](https://docs.astral.sh/uv/), which installs its dependencies on the fly.

```sh
uv run tools/build_map.py                                  # redraw from data/
uv run tools/build_map.py --xlsx ~/Downloads/export.xlsx   # new SanGIS parcel export
uv run tools/build_map.py --fetch-osm                      # refresh streets from OpenStreetMap
```

The SanGIS export includes assessed values and other fields we don't publish. The script copies only the columns the map needs, and `.gitignore` keeps `*.xlsx` out of the repo.

To preview locally, run `python3 -m http.server 8799` and open http://127.0.0.1:8799/.

## Sources

Lot lines come from the San Diego County Assessor via SanGIS (October 2023). Streets and canyon scrub are © OpenStreetMap contributors, under the ODbL.
