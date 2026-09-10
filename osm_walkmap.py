#!/usr/bin/env python3
"""osm-walkmap: a real OpenStreetMap map, redrawn in your own style, with walking routes.

Fetches roads, green areas, water and rail around a point (Overpass API), builds the
pedestrian graph, runs Dijkstra to each target, and writes:

  * map.svg   layered SVG in metres (x east, y south, origin at the house), one <g>
              per feature class and one <path id="route-N"> per destination, ready to
              be styled with CSS and animated with stroke-dashoffset.
  * map.json  distance in metres and walking minutes for each route, computed on the
              real pedestrian network, plus the ODbL attribution you must display.

Standard library only: no pip install, no geospatial stack.

Author: Luca Cazzaniga <https://github.com/Ioluca>
Licence: MIT (see LICENSE). Written with AI assistance (Claude).
Map data: (c) OpenStreetMap contributors, ODbL. Whatever you render from this
output must carry that attribution: https://www.openstreetmap.org/copyright
"""
from __future__ import annotations

import argparse
import heapq
import json
import logging
import math
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("osm_map")
CONTACT = os.environ.get("OSM_CONTACT", "")
UA = {"User-Agent": f"osm-walkmap/1.0 (+https://github.com/Ioluca/osm-walkmap{'; ' + CONTACT if CONTACT else ''})"}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
SPEED_KMH = {"walk": 4.5, "bike": 15.0}   # override with --speed walk=4.5,bike=15

NOT_WALKABLE = {"motorway", "motorway_link", "trunk", "trunk_link"}
NOT_CYCLABLE = NOT_WALKABLE | {"steps"}
FOOT_ONLY = {"footway", "pedestrian", "path"}   # closed to bikes unless tagged bicycle=yes/designated
ROAD_CLASS = {
    "primary": "road-major", "primary_link": "road-major", "secondary": "road-major", "secondary_link": "road-major",
    "tertiary": "road-mid", "tertiary_link": "road-mid", "residential": "road-minor", "unclassified": "road-minor",
    "living_street": "road-minor", "service": "road-service", "pedestrian": "path", "footway": "path",
    "path": "path", "cycleway": "path", "steps": "path", "track": "path",
}


def overpass(query: str) -> dict[str, Any]:
    """POST a query to Overpass, trying each mirror in turn."""
    if not CONTACT:
        log.warning("set OSM_CONTACT to an email or URL: the Overpass and Nominatim "
                    "usage policies ask every client to identify itself")
    data = ("data=" + urllib.parse.quote(query)).encode()
    last: Exception | None = None
    for host in OVERPASS:
        try:
            req = urllib.request.Request(host, data=data, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=180).read())
        except Exception as exc:  # noqa: BLE001
            log.warning("overpass %s failed: %s", host, exc)
            last = exc
    raise SystemExit(f"overpass unavailable: {last}")


class Proj:
    """Equirectangular projection in metres around the house."""

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 111320.0

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, -(lat - self.lat0) * self.ky)


def fetch(lat: float, lon: float, radius: int) -> dict[str, Any]:
    q = f"""[out:json][timeout:170];
(
  way["highway"](around:{radius},{lat},{lon});
  way["railway"~"^(rail|light_rail|tram)$"](around:{radius},{lat},{lon});
  way["waterway"~"^(river|stream|canal)$"](around:{radius},{lat},{lon});
  way["leisure"~"^(park|garden|pitch|playground)$"](around:{radius},{lat},{lon});
  way["landuse"~"^(grass|forest|meadow|recreation_ground|cemetery|farmland|orchard)$"](around:{radius},{lat},{lon});
  way["natural"~"^(wood|water|scrub|grassland)$"](around:{radius},{lat},{lon});
  relation["leisure"="park"](around:{radius},{lat},{lon});
);
out geom;"""
    return overpass(q)


def edge_speed_kmh(tags: dict[str, str], mode: str) -> float | None:
    """Speed at which the mode travels this way, or None if the way is closed to it.

    Bikes are pushed on foot along footways, paths and pedestrian streets (legal
    almost everywhere, and what people actually do), so those ways stay in the
    bike graph at walking speed instead of fragmenting it. Steps are closed.
    """
    hw = tags.get("highway")
    if not hw or tags.get("access") == "private":
        return None
    if mode == "walk":
        if hw in NOT_WALKABLE or tags.get("foot") == "no":
            return None
        return SPEED_KMH["walk"]
    if hw in NOT_CYCLABLE or tags.get("bicycle") == "no":
        return None
    if hw in FOOT_ONLY and tags.get("bicycle") not in ("yes", "designated", "permissive"):
        return SPEED_KMH["walk"]
    return SPEED_KMH[mode]


def build_graph(ways: list[dict[str, Any]], mode: str = "walk") -> tuple[dict[int, tuple[float, float]], dict[int, list[tuple[int, float, float]]]]:
    """Graph for one mode: node id -> (lat, lon); adjacency as (node, metres, minutes)."""
    coords: dict[int, tuple[float, float]] = {}
    adj: dict[int, list[tuple[int, float, float]]] = {}
    for w in ways:
        tags = w.get("tags", {})
        kmh = edge_speed_kmh(tags, mode)
        if kmh is None:
            continue
        nodes, geom = w.get("nodes", []), w.get("geometry", [])
        if len(nodes) != len(geom):
            continue
        for i in range(len(nodes)):
            coords[nodes[i]] = (geom[i]["lat"], geom[i]["lon"])
        for i in range(len(nodes) - 1):
            a, b = nodes[i], nodes[i + 1]
            d = haversine(coords[a], coords[b])
            cost = d / (kmh * 1000 / 60)
            adj.setdefault(a, []).append((b, d, cost))
            adj.setdefault(b, []).append((a, d, cost))
    return coords, adj


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def nearest_node(coords: dict[int, tuple[float, float]], pt: tuple[float, float],
                 among: set[int] | None = None) -> tuple[int, float]:
    """Graph node closest to pt, optionally restricted to a set of node ids.

    Restricting to the reachable set matters: the node physically nearest a
    destination is often part of a fragment the Overpass query clipped off the
    rest of the network, which would make a perfectly walkable target look
    unreachable.
    """
    best, bd = -1, float("inf")
    for nid, c in coords.items():
        if among is not None and nid not in among:
            continue
        d = haversine(c, pt)
        if d < bd:
            best, bd = nid, d
    return best, bd


def dijkstra(adj: dict[int, list[tuple[int, float, float]]], src: int) -> tuple[dict[int, float], dict[int, float], dict[int, int]]:
    """Fastest path from src. Returns minutes, metres along that path, and predecessors."""
    mins: dict[int, float] = {src: 0.0}
    metres: dict[int, float] = {src: 0.0}
    prev: dict[int, int] = {}
    heap = [(0.0, src)]
    while heap:
        c, u = heapq.heappop(heap)
        if c > mins.get(u, float("inf")):
            continue
        for v, d, w in adj.get(u, []):
            nc = c + w
            if nc < mins.get(v, float("inf")):
                mins[v], metres[v], prev[v] = nc, metres[u] + d, u
                heapq.heappush(heap, (nc, v))
    return mins, metres, prev


def path_to(prev: dict[int, int], src: int, dst: int) -> list[int]:
    out, cur = [dst], dst
    while cur != src:
        cur = prev[cur]
        out.append(cur)
    return out[::-1]


def simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker in metre space. Closed rings are split in two halves first,
    otherwise the degenerate first-last chord collapses the whole polygon."""
    if len(points) < 3:
        return points
    if points[0] == points[-1]:
        half = len(points) // 2
        return simplify(points[:half + 1], tol)[:-1] + simplify(points[half:], tol)
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy) or 1e-9
    idx, dmax = 0, 0.0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
        if d > dmax:
            idx, dmax = i, d
    if dmax > tol:
        return simplify(points[: idx + 1], tol)[:-1] + simplify(points[idx:], tol)
    return [points[0], points[-1]]


def stitch_rings(segs: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join outer member ways of a multipolygon into closed rings by matching endpoints."""
    rings: list[list[tuple[float, float]]] = []
    pool = [s for s in segs if len(s) > 1]
    while pool:
        ring = pool.pop(0)
        grown = True
        while grown and ring[0] != ring[-1]:
            grown = False
            for i, s in enumerate(pool):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[-2::-1]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[::-1][:-1] + ring
                else:
                    continue
                pool.pop(i)
                grown = True
                break
        rings.append(ring)
    return rings


def poly_d(points: list[tuple[float, float]], close: bool = False) -> str:
    d = "M" + " L".join(f"{x:.0f},{y:.0f}" for x, y in points)
    return d + (" Z" if close else "")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius", type=int, default=1800)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--target", action="append", default=[], help='"Label=lat,lon" or "Label=lat,lon:bike"')
    ap.add_argument("--target-boundary", action="append", default=[], help='"Label=key=value[:bike]" nearest reachable point of that area')
    ap.add_argument("--speed", default="", help='override speeds in km/h, e.g. "walk=5,bike=14"')
    ap.add_argument("--area-full", action="append", default=[], help='exact OSM name of a park/area to fetch whole (not clipped by radius)')
    ap.add_argument("--tol", type=float, default=4.0, help="simplification tolerance (m)")
    args = ap.parse_args()
    for part in filter(None, args.speed.split(",")):
        k, v = part.split("=")
        SPEED_KMH[k.strip()] = float(v)

    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.out / "overpass.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        log.info("using cached overpass data")
    else:
        data = fetch(args.lat, args.lon, args.radius)
        cache.write_text(json.dumps(data))
    elements = data["elements"]
    full_names = set(args.area_full)
    if full_names:
        cache_a = args.out / "overpass-areas.json"
        if cache_a.exists():
            areas = json.loads(cache_a.read_text())
        else:
            names = "|".join(n.replace("(", "\\(").replace(")", "\\)") for n in full_names)
            areas = overpass(f'[out:json][timeout:170];(relation["leisure"="park"]["name"~"^({names})$"];way["leisure"="park"]["name"~"^({names})$"];);out geom;')
            cache_a.write_text(json.dumps(areas))
        # drop the clipped copies, keep the complete ones
        elements = [e for e in elements if e.get("tags", {}).get("name") not in full_names] + areas["elements"]
        log.info("areas fetched whole: %s", ", ".join(sorted(full_names)))
    ways = [e for e in elements if e["type"] == "way"]
    proj = Proj(args.lat, args.lon)

    # --- drawing layers ---
    layers: dict[str, list[str]] = {"field": [], "green": [], "green-hero": [], "water": [], "rail": [], "road-service": [], "path": [],
                                    "road-minor": [], "road-mid": [], "road-major": []}
    park_polys: list[tuple[dict[str, str], list[tuple[float, float]]]] = []
    for w in ways:
        t = w.get("tags", {})
        pts = simplify([proj.xy(g["lat"], g["lon"]) for g in w.get("geometry", [])], args.tol)
        if len(pts) < 2:
            continue
        if "highway" in t:
            cls = ROAD_CLASS.get(t["highway"])
            if cls:
                layers[cls].append(poly_d(pts))
        elif "railway" in t:
            layers["rail"].append(poly_d(pts))
        elif "waterway" in t:
            layers["water"].append(poly_d(pts))
        elif t.get("natural") == "water":
            layers["water"].append(poly_d(pts, True))
        elif t.get("landuse") in {"farmland", "orchard", "meadow", "cemetery"}:
            layers["field"].append(poly_d(pts, True))
        else:
            layers["green"].append(poly_d(pts, True))
            park_polys.append((t, pts))
    for r in (e for e in elements if e["type"] == "relation" and e.get("tags", {}).get("leisure") == "park"):
        segs = [[(g["lat"], g["lon"]) for g in m["geometry"]] for m in r.get("members", [])
                if m.get("role") == "outer" and m.get("geometry")]
        hero = r["tags"].get("name") in full_names
        for ring in stitch_rings(segs):
            pts = simplify([proj.xy(la, lo) for la, lo in ring], args.tol)
            if len(pts) >= 3:
                layers["green-hero" if hero else "green"].append(poly_d(pts, True))
                park_polys.append((r["tags"], pts))

    # --- routing: one graph and one Dijkstra per mode actually requested ---
    nets: dict[str, tuple[dict, dict, int, dict, dict, dict]] = {}
    snaps: dict[str, float] = {}

    def net(mode: str) -> tuple[dict, dict, int, dict, dict, dict]:
        if mode not in SPEED_KMH:
            raise SystemExit(f"unknown mode {mode!r}: known {sorted(SPEED_KMH)}")
        if mode not in nets:
            coords, adj = build_graph(ways, mode)
            src, snap = nearest_node(coords, (args.lat, args.lon))
            log.info("%s graph: %d nodes, house snapped to node at %.0f m", mode, len(coords), snap)
            snaps[mode] = snap
            mins, dist, prev = dijkstra(adj, src)
            nets[mode] = (coords, adj, src, dist, prev, mins)
        return nets[mode]

    def split_mode(spec: str) -> tuple[str, str]:
        head, _, tail = spec.rpartition(":")
        return (head, tail) if head and tail in SPEED_KMH else (spec, "walk")


    routes: list[dict[str, Any]] = []
    for spec in args.target:
        spec, mode = split_mode(spec)
        coords, adj, src, dist, prev, mins = net(mode)
        label, ll = spec.split("=", 1)
        tlat, tlon = map(float, ll.split(","))
        dst, dsnap = nearest_node(coords, (tlat, tlon), among=set(dist))
        if dst < 0:
            log.warning("target %s unreachable on the pedestrian graph", label)
            routes.append({"label": label, "mode": mode, "reachable": False, "point": proj.xy(tlat, tlon)})
            continue
        if dsnap > 150:
            log.warning("target %s: nearest reachable node is %.0f m away, so the "
                        "route stops short. Is the target outside --radius, or on a "
                        "street the pedestrian filter excluded?", label, dsnap)
        pts = [proj.xy(*coords[n]) for n in path_to(prev, src, dst)]
        metres = dist[dst] + dsnap
        total_min = mins[dst] + dsnap / (SPEED_KMH["walk"] * 1000 / 60)
        routes.append({"label": label, "mode": mode, "reachable": True, "metres": round(metres), "minutes": math.ceil(total_min),
                       "snap_m": round(dsnap), "point": proj.xy(tlat, tlon), "path": simplify(pts, args.tol)})
    for spec in args.target_boundary:
        spec, mode = split_mode(spec)
        coords, adj, src, dist, prev, mins = net(mode)
        label, kv = spec.split("=", 1)
        key, val = kv.split("=", 1)
        cands = [(tags, pts) for tags, pts in park_polys if tags.get(key) == val]
        named = [(tags, pts) for tags, pts in cands if tags.get("name") in full_names]
        if named:
            cands = named
        if not cands:
            log.warning("boundary %s: no area with %s=%s in the download", label, key, val)
            continue
        verts = [v for _, pts in cands for v in pts]
        # candidate graph nodes within 30 m of any vertex of the matching areas
        best_node, best_d = -1, float("inf")
        for nid, c in coords.items():
            if nid not in mins:
                continue
            x, y = proj.xy(*c)
            if any(math.hypot(x - vx, y - vy) < 30 for vx, vy in verts):
                if mins[nid] < best_d:
                    best_node, best_d = nid, mins[nid]
        if best_node < 0:
            log.warning("no reachable node on boundary of %s", label)
            continue
        pts = [proj.xy(*coords[n]) for n in path_to(prev, src, best_node)]
        routes.append({"label": label, "mode": mode, "reachable": True, "metres": round(dist[best_node]), "minutes": math.ceil(best_d),
                       "area": ", ".join(sorted({tg.get("name", "?") for tg, _ in cands})), "point": pts[-1], "path": simplify(pts, args.tol)})

    # --- SVG ---
    R = args.radius
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-R} {-R} {2*R} {2*R}" id="osm-map">',
           "<style>",
           ".field{fill:#121c1c;stroke:none}.green{fill:#15251e;stroke:none}.green-hero{fill:#1f4a35;stroke:#2f6b4c;stroke-width:4}.water{fill:none;stroke:#2f5f6b;stroke-width:6}.rail{fill:none;stroke:#3a4650;stroke-width:5;stroke-dasharray:22 12}",
           ".road-service{fill:none;stroke:#3c3a33;stroke-width:3}.path{fill:none;stroke:#4a4436;stroke-width:2.5;stroke-dasharray:8 7}",
           ".road-minor{fill:none;stroke:#7a6536;stroke-width:6}.road-mid{fill:none;stroke:#a77a37;stroke-width:10}.road-major{fill:none;stroke:#f0ae39;stroke-width:14}",
           ".route{fill:none;stroke:#fff7ea;stroke-width:12;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:28 18}",
           "</style>"]
    for name in ["field", "green", "green-hero", "water", "rail", "road-service", "path", "road-minor", "road-mid", "road-major"]:
        svg.append(f'<g class="{name}" id="layer-{name}">' + "".join(f'<path d="{d}"/>' for d in layers[name]) + "</g>")
    svg.append('<g id="routes">')
    for i, r in enumerate(routes):
        if r.get("reachable"):
            svg.append(f'<path id="route-{i}" class="route" d="{poly_d(r["path"])}"/>')
    svg.append("</g></svg>")
    (args.out / "map.svg").write_text("\n".join(svg), encoding="utf-8")

    meta = {"house": {"lat": args.lat, "lon": args.lon, "snap_m": round(min(snaps.values()) if snaps else 0)}, "radius": R, "speed_kmh": SPEED_KMH,
            "routes": [{k: v for k, v in r.items() if k != "path"} | {"path_points": len(r.get("path", []))} for r in routes],
            "layers": {k: len(v) for k, v in layers.items()}, "attribution": "© OpenStreetMap contributors (ODbL)"}
    (args.out / "map.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    size = (args.out / "map.svg").stat().st_size // 1024
    log.info("map.svg %d KB, layers %s", size, meta["layers"])
    for r in routes:
        if r.get("reachable"):
            log.info("%-22s %5d m  %2d min by %s", r["label"], r["metres"], r["minutes"], r["mode"])
        else:
            log.info("%-22s unreachable", r["label"])


if __name__ == "__main__":
    main()
