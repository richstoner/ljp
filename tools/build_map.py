#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["shapely>=2", "openpyxl"]
# ///
"""Render the neighborhood map into index.html.

The map is drawn from two files in data/:

  parcels.geojson  lot polygons, extracted from a SanGIS parcel export
  streets.geojson  street centerlines and canyon scrub, from OpenStreetMap

Usage (uv installs the dependencies listed above):

  uv run tools/build_map.py                     re-render the map from data/
  uv run tools/build_map.py --xlsx EXPORT.xlsx  refresh parcels.geojson first
  uv run tools/build_map.py --fetch-osm         refresh streets.geojson first

The SanGIS export carries assessed values and other fields we don't publish,
so keep it out of the repo; only the columns written below end up public.
"""

import argparse
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import LineString, box, shape
from shapely.ops import linemerge, polylabel, substring, transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
PARCELS = ROOT / "data" / "parcels.geojson"
STREETS = ROOT / "data" / "streets.geojson"
INDEX = ROOT / "index.html"
START, END = "<!-- map:start -->", "<!-- map:end -->"

UNITS = {
    "LA JOLLA PALISADES UNIT # 1": 1,
    "LA JOLLA PALISADES #2": 2,
    "LA JOLLA PALISADES UNIT # 3": 3,
    "LA JOLLA PALISADES UNIT#4": 4,
}
SUFFIXES = {"RD": "Road", "DR": "Drive", "LN": "Lane", "WAY": "Way", "ST": "Street", "CT": "Court"}
ROAD_WIDTH = {"secondary": 15, "tertiary": 13, "residential": 11, "unclassified": 11}
FEET = 0.3048
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


# ---------------------------------------------------------------- extract


def street_name(street, suffix):
    words = [w.capitalize() if w not in ("DE", "DEL") else w.lower() for w in street.split()]
    if suffix:
        words.append(SUFFIXES.get(suffix, suffix.capitalize()))
    return " ".join(words)


def lot_label(legal, submap):
    m = re.match(r"(LOT|PAR)\s+(\d+)", legal or "")
    if not m:
        return None
    if m.group(1) == "PAR":
        return f"Parcel {m.group(2)} of Parcel Map {submap.removeprefix('PM')}"
    return f"Lot {m.group(2)}"


def parcels_from_xlsx(path):
    import openpyxl

    ws = openpyxl.load_workbook(path, read_only=True).active
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    features = []
    for row in rows:
        r = {k: "" if v is None else str(v).strip() for k, v in zip(header, row)}
        if not r.get("APN"):
            continue
        unit = UNITS.get(r["SUBNAME_FIX"])
        number = r["SITUS_ADDR"]
        street = street_name(r["SITUS_STRE"], r["SITUS_SUFF"]) if r["SITUS_STRE"] else None
        address = f"{number} {street}" if street and number not in ("", "0") else None
        lot = lot_label(r["LEGLDESC"], r["SUBMAP"])
        if unit is None and lot:
            lot = f"Pueblo {lot}"
        geom = shape_from_wkt(r["geometry"])
        features.append({
            "type": "Feature",
            "properties": {
                "apn": r["APN"],
                "address": address,
                "street": street,
                "lot": lot,
                "unit": unit,
                "map": r["SUBMAP"].lstrip("0") if unit else None,
            },
            "geometry": geom,
        })
    features.sort(key=lambda f: f["properties"]["apn"])
    return {
        "type": "FeatureCollection",
        "attribution": "Parcels: San Diego County Assessor via SanGIS, October 2023",
        "features": features,
    }


def shape_from_wkt(wkt):
    from shapely import wkt as shapely_wkt

    geom = shapely_wkt.loads(wkt)
    geom = transform(lambda x, y: (round(x, 7), round(y, 7)), geom)
    return geom.__geo_interface__


def fetch_osm(parcels):
    minx, miny, maxx, maxy = unary_union([shape(f["geometry"]) for f in parcels["features"]]).bounds
    pad = 0.004
    bbox = f"{miny - pad},{minx - pad},{maxy + pad},{maxx + pad}"
    query = (
        f'[out:json][timeout:60];(way["highway"~"^({"|".join(ROAD_WIDTH)})$"]({bbox});'
        f'way["natural"="scrub"]({bbox}););out geom;'
    )
    for endpoint in OVERPASS:
        req = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode({"data": query}).encode(),
            headers={"User-Agent": "lajollapalisades.com map build"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                elements = json.load(resp)["elements"]
            break
        except urllib.error.URLError as err:
            print(f"{endpoint}: {err}")
    else:
        raise SystemExit("every Overpass endpoint failed; try again later")
    features = []
    for el in elements:
        tags = el.get("tags", {})
        coords = [(round(p["lon"], 7), round(p["lat"], 7)) for p in el["geometry"]]
        if "highway" in tags:
            geom = {"type": "LineString", "coordinates": coords}
            props = {"highway": tags["highway"], "name": tags.get("name")}
        else:
            geom = {"type": "Polygon", "coordinates": [coords]}
            props = {"natural": tags["natural"]}
        features.append({"type": "Feature", "properties": {"osm_id": el["id"], **props}, "geometry": geom})
    features.sort(key=lambda f: f["properties"]["osm_id"])
    return {
        "type": "FeatureCollection",
        "attribution": "© OpenStreetMap contributors, ODbL",
        "features": features,
    }


# ---------------------------------------------------------------- render


class Projection:
    """Local equirectangular projection in meters, y down, north up."""

    def __init__(self, lon0, lat0):
        phi = math.radians(lat0)
        self.lon0, self.lat0 = lon0, lat0
        self.m_lat = 111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
        self.m_lon = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)

    def __call__(self, geom):
        return transform(lambda x, y: ((x - self.lon0) * self.m_lon, (self.lat0 - y) * self.m_lat), geom)


def fmt(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def ring_d(coords):
    pts = list(coords)[:-1]
    return "M" + "L".join(f"{fmt(x)},{fmt(y)}" for x, y in pts) + "Z"


def poly_d(geom):
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    return "".join(ring_d(p.exterior.coords) + "".join(ring_d(i.coords) for i in p.interiors) for p in polys)


def line_d(line):
    return "M" + "L".join(f"{fmt(x)},{fmt(y)}" for x, y in line.coords)


def chaikin(line, rounds=2):
    pts = list(line.coords)
    for _ in range(rounds):
        out = [pts[0]]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            out += [(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1), (0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)]
        out.append(pts[-1])
        pts = out
    return LineString(pts)


def upright(line):
    """Orient a label path so its text reads left to right, or bottom to top."""
    (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < 0.35 * abs(dy):
        flip = dy > 0
    else:
        flip = dx < 0
    return LineString(list(line.coords)[::-1]) if flip else line


def label_spots(line, length, spacing):
    """Pick the straightest stretches of a line that can hold a label."""
    if line.length < length * 1.1:
        return []
    candidates = []
    step = 4
    s = 0.0
    while s + length <= line.length:
        seg = substring(line, s, s + length)
        (x0, y0), (x1, y1) = seg.coords[0], seg.coords[-1]
        straight = math.hypot(x1 - x0, y1 - y0) / length
        if straight > 0.93:
            candidates.append((straight, s))
        s += step
    candidates.sort(reverse=True)
    chosen = []
    for straight, s in candidates:
        if all(abs(s - c) > spacing for c in chosen):
            chosen.append(s)
    return [substring(line, s, s + length) for s in sorted(chosen)]


def render(parcels, streets):
    lots = [(f["properties"], shape(f["geometry"])) for f in parcels["features"]]
    everything = unary_union([g for _, g in lots])
    minx, miny, maxx, maxy = everything.bounds
    proj = Projection((minx + maxx) / 2, (miny + maxy) / 2)
    lots = [(p, proj(g)) for p, g in lots]

    x0, y0, x1, y1 = unary_union([g for _, g in lots]).bounds
    pad = 34
    vx, vy = math.floor(x0 - pad), math.floor(y0 - pad)
    vw, vh = math.ceil(x1 - x0 + 2 * pad), math.ceil(y1 - y0 + 2 * pad + 40)
    view = box(vx, vy, vx + vw, vy + vh)
    bw, bh = 192, 44
    bx, by = vx + vw - 8 - bw, vy + vh - 8 - bh
    inner = view.buffer(-14).difference(box(bx - 10, by - 10, bx + bw, by + bh))

    out = [
        f'<svg id="plat" class="plat" viewBox="{vx} {vy} {vw} {vh}" role="img" '
        f'aria-labelledby="plat-title plat-desc" xmlns="http://www.w3.org/2000/svg">',
        '<title id="plat-title">Map of La Jolla Palisades</title>',
        '<desc id="plat-desc">Lot lines for La Jolla Palisades Units 1 through 4, '
        "with Unit 1 shaded. Use the address search to find a lot.</desc>",
        "<defs>",
        '<pattern id="scrub" width="7" height="7" patternUnits="userSpaceOnUse">'
        '<circle class="dot" cx="1.5" cy="1.5" r=".55"/><circle class="dot" cx="5" cy="5" r=".55"/></pattern>',
        f'<clipPath id="frame"><rect x="{vx + 8}" y="{vy + 8}" width="{vw - 16}" height="{vh - 16}"/></clipPath>',
        "</defs>",
        f'<rect class="paper" x="{vx}" y="{vy}" width="{vw}" height="{vh}"/>',
        '<g clip-path="url(#frame)">',
    ]

    # Canyon scrub and streets sit under the lots.
    roads = []
    out.append('<g class="scrub">')
    for f in streets["features"]:
        props, geom = f["properties"], proj(shape(f["geometry"]))
        if not geom.intersects(view):
            continue
        if props.get("natural") == "scrub":
            out.append(f'<path d="{poly_d(geom.buffer(0).intersection(view.buffer(20)))}"/>')
        else:
            roads.append((props, geom))
    out.append("</g>")
    for layer in ("road", "centerline"):
        out.append(f'<g class="{layer}s">')
        for props, geom in roads:
            width = f' stroke-width="{ROAD_WIDTH[props["highway"]]}"' if layer == "road" else ""
            out.append(f'<path d="{line_d(geom)}"{width}/>')
        out.append("</g>")

    # Lots: shaded for Unit 1, outlined for the neighboring units.
    out.append('<g class="lots">')
    for p, g in lots:
        cls = f"lot u{p['unit']}" if p["unit"] else "lot open"
        attrs = {
            "id": f"apn-{p['apn']}",
            "data-apn": p["apn"],
            "data-address": p["address"] or "",
            "data-street": p["street"] or "",
            "data-lot": p["lot"] or "",
            "data-unit": p["unit"] or "",
        }
        attr_s = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        out.append(f'<path class="{cls}" {attr_s} d="{poly_d(g)}"/>')
    out.append("</g>")

    # Block outlines: the union of each unit's lots, drawn heavier.
    out.append('<g class="blocks">')
    for unit in (2, 3, 4, 1):
        merged = unary_union([g.buffer(0.4) for p, g in lots if p["unit"] == unit]).buffer(-0.4)
        out.append(f'<path class="block u{unit}" d="{poly_d(merged)}"/>')
    out.append("</g>")

    # House numbers at each lot's pole of inaccessibility.
    out.append('<g class="nums" aria-hidden="true">')
    for p, g in lots:
        if not p["address"]:
            continue
        spot = polylabel(g, tolerance=0.5)
        if g.exterior.distance(spot) < 5:
            continue
        number = p["address"].split()[0]
        out.append(f'<text x="{fmt(spot.x)}" y="{fmt(spot.y + 2.6)}">{number}</text>')
    open_space = next((g for p, g in lots if not p["unit"]), None)
    if open_space is not None:
        spot = polylabel(open_space, tolerance=0.5)
        out.append(f'<text class="note" x="{fmt(spot.x)}" y="{fmt(spot.y)}">Canyon</text>')
    out.append("</g>")

    # Street names along the centerlines.
    address_streets = {p["street"] for p, _ in lots if p["street"]}
    by_name = {}
    for props, geom in roads:
        if props.get("name"):
            by_name.setdefault(props["name"], []).append(geom)
    out.append('<g class="streets" aria-hidden="true">')
    n = 0
    for name, geoms in sorted(by_name.items()):
        merged = unary_union(geoms)
        if merged.geom_type == "MultiLineString":
            merged = linemerge(merged)
        merged = merged.intersection(inner)
        parts = getattr(merged, "geoms", [merged])
        size = 10.5 if name in address_streets else 9
        length = len(name) * size * 0.5 + 6
        for part in parts:
            if part.geom_type != "LineString":
                continue
            for seg in label_spots(part.simplify(1.5), length, spacing=260):
                n += 1
                path = upright(chaikin(seg))
                cls = "street" if name in address_streets else "street context"
                out.append(f'<path id="st{n}" class="guide" d="{line_d(path)}"/>')
                out.append(
                    f'<text class="{cls}" dy="3.4"><textPath href="#st{n}" startOffset="50%">{name}</textPath></text>'
                )
    out.append("</g>")
    out.append("</g>")

    # North arrow and scale bar in a box at the bottom right, as on a map sheet.
    ax, ay = bx + 20, by + 37
    out.append(
        f'<g class="marks" aria-hidden="true">'
        f'<rect class="scale-box" x="{bx}" y="{by}" width="{bw}" height="{bh}"/>'
        f'<path class="north" d="M{ax},{ay}L{ax},{ay - 22}L{ax + 6},{ay - 6}L{ax},{ay - 9}Z"/>'
        f'<text class="mark-n" x="{ax}" y="{ay - 25}">N</text>'
    )
    sx, sy = bx + 42, by + 30
    ticks = [0, 100, 200, 400]
    for i, (a, b) in enumerate(zip(ticks, ticks[1:])):
        cls = "bar fill" if i % 2 == 0 else "bar"
        out.append(f'<rect class="{cls}" x="{fmt(sx + a * FEET)}" y="{sy - 4}" width="{fmt((b - a) * FEET)}" height="4"/>')
    for t in ticks:
        out.append(f'<text class="tick" x="{fmt(sx + t * FEET)}" y="{sy - 9}">{t}</text>')
    out.append(f'<text class="tick unit" x="{fmt(sx + 400 * FEET + 5)}" y="{sy}">feet</text>')
    out.append("</g>")

    # Neat line, doubled like a recorded map sheet.
    out.append(
        f'<rect class="neat outer" x="{vx + 4}" y="{vy + 4}" width="{vw - 8}" height="{vh - 8}"/>'
        f'<rect class="neat" x="{vx + 8}" y="{vy + 8}" width="{vw - 16}" height="{vh - 16}"/>'
    )
    out.append("</svg>")
    return "\n".join(out)


def inject(svg):
    html = INDEX.read_text()
    head, rest = html.split(START, 1)
    _, tail = rest.split(END, 1)
    INDEX.write_text(f"{head}{START}\n{svg}\n{END}{tail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", type=Path, help="SanGIS parcel export to refresh parcels.geojson from")
    ap.add_argument("--fetch-osm", action="store_true", help="refresh streets.geojson from the Overpass API")
    args = ap.parse_args()

    if args.xlsx:
        PARCELS.parent.mkdir(exist_ok=True)
        PARCELS.write_text(json.dumps(parcels_from_xlsx(args.xlsx), separators=(",", ":")) + "\n")
    parcels = json.loads(PARCELS.read_text())
    if args.fetch_osm:
        STREETS.write_text(json.dumps(fetch_osm(parcels), separators=(",", ":")) + "\n")
    streets = json.loads(STREETS.read_text())

    inject(render(parcels, streets))
    print(f"map written to {INDEX.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
