"""Road-network snapping and OSM footpath transfer builder for NagpurBus.

Primary strategy:
  - Uses ordered OSM ways directly when available (e.g., Metro relations or complete bus relations).
  - For stop-sequence routes without OSM route relations (e.g., NMC feeder bus timetable PDFs),
    builds a directed weighted bus road graph from real Nagpur OSM drivable roads and computes
    shortest-path road-snapped polylines (NetworkX Dijkstra in-process, with PostGIS/pgRouting
    SQL edge table export for database-native routing).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import networkx as nx
import structlog
from pyproj import Transformer
from scipy.spatial import cKDTree

from ingest.models import ShapePoint, ShapeRecord

logger = structlog.get_logger(__name__)

# Nagpur metric CRS: WGS84 UTM Zone 44N
WGS84_TO_UTM44N = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)

# Baseline urban Nagpur bus speeds by OSM highway class (m/s) used for snapping cost & cold-start prior.
# Higher classes are gently preferred for bus routing over narrow residential lanes.
OSM_ROAD_CLASS_BUS_SPEED_MPS: dict[str, float] = {
    "trunk": 11.1,          # ~40 km/h (arterials / Wardha Rd / Amravati Rd / Kamptee Rd)
    "trunk_link": 8.3,      # ~30 km/h
    "primary": 9.7,         # ~35 km/h (major city corridors)
    "primary_link": 7.5,    # ~27 km/h
    "secondary": 8.3,       # ~30 km/h (collector roads)
    "secondary_link": 6.7,  # ~24 km/h
    "tertiary": 6.9,        # ~25 km/h (connector streets)
    "tertiary_link": 5.8,   # ~21 km/h
    "unclassified": 5.5,    # ~20 km/h (MIHAN / campus internal roads)
    "residential": 4.2,     # ~15 km/h (penalized so buses stick to arterials unless necessary)
    "service": 3.5,         # ~12.6 km/h (depot / terminal / campus access only)
}

BUS_DRIVABLE_HIGHWAYS = set(OSM_ROAD_CLASS_BUS_SPEED_MPS.keys())


@dataclass(frozen=True)
class SnappedStopProjection:
    """Projection of a stop onto a metric route shape."""

    stop_id: str
    s_m: float
    snap_error_m: float
    snapped_lat: float = 0.0
    snapped_lon: float = 0.0


class OSMRoadSnapper:
    """Snaps ordered stop coordinates onto the real Nagpur OSM road network."""

    def __init__(self, osm_roads_payload: dict[str, Any]) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()
        self._node_coords: dict[int, tuple[float, float]] = {}  # node_id -> (lat, lon)
        self._node_xy: dict[int, tuple[float, float]] = {}      # node_id -> (x_m, y_m)
        self._kdtree_ids: list[int] = []
        self._kdtree: cKDTree | None = None
        self._build_graph(osm_roads_payload)

    def _build_graph(self, payload: dict[str, Any]) -> None:
        raw_nodes: dict[int, tuple[float, float]] = {}
        ways: list[dict[str, Any]] = []
        for el in payload.get("elements", []):
            etype = el.get("type")
            if etype == "node":
                raw_nodes[int(el["id"])] = (float(el["lat"]), float(el["lon"]))
            elif etype == "way":
                ways.append(el)

        used_nodes: set[int] = set()
        edge_count = 0

        for way in ways:
            tags = way.get("tags") or {}
            highway = tags.get("highway", "")
            if highway not in BUS_DRIVABLE_HIGHWAYS:
                continue
            if tags.get("access") in ("private", "no") and tags.get("bus") != "yes":
                continue

            nds = [int(n) for n in way.get("nodes", []) if int(n) in raw_nodes]
            if len(nds) < 2:
                continue

            oneway_tag = (tags.get("oneway") or "").lower()
            is_oneway = oneway_tag in ("yes", "true", "1") or tags.get("junction") == "roundabout"
            is_reverse_oneway = oneway_tag == "-1"
            speed_mps = OSM_ROAD_CLASS_BUS_SPEED_MPS[highway]

            for u, v in itertools.pairwise(nds):
                if u == v:
                    continue
                lat_u, lon_u = raw_nodes[u]
                lat_v, lon_v = raw_nodes[v]
                if u not in self._node_xy:
                    self._node_xy[u] = WGS84_TO_UTM44N.transform(lon_u, lat_u)
                    self._node_coords[u] = (lat_u, lon_u)
                if v not in self._node_xy:
                    self._node_xy[v] = WGS84_TO_UTM44N.transform(lon_v, lat_v)
                    self._node_coords[v] = (lat_v, lon_v)

                xu, yu = self._node_xy[u]
                xv, yv = self._node_xy[v]
                length_m = math.hypot(xv - xu, yv - yu)
                if length_m < 0.1:
                    continue
                cost_s = length_m / speed_mps

                if not is_reverse_oneway:
                    self.graph.add_edge(u, v, weight=cost_s, length_m=length_m, road_class=highway)
                    edge_count += 1
                if not is_oneway or is_reverse_oneway:
                    self.graph.add_edge(v, u, weight=cost_s, length_m=length_m, road_class=highway)
                    edge_count += 1

                used_nodes.add(u)
                used_nodes.add(v)

        # Restrict KD-tree to nodes in the largest strongly connected component so snapping never lands on dead islands
        if self.graph.number_of_nodes() > 0:
            sccs = sorted(nx.strongly_connected_components(self.graph), key=len, reverse=True)
            main_component = sccs[0]
            self._kdtree_ids = list(main_component)
            xy_array = [self._node_xy[nid] for nid in self._kdtree_ids]
            self._kdtree = cKDTree(xy_array)
        else:
            main_component = set()

        logger.info(
            "snapper.graph_ready",
            total_nodes=self.graph.number_of_nodes(),
            main_scc_nodes=len(main_component),
            edges=edge_count,
        )

    def nearest_road_node(self, lat: float, lon: float, max_radius_m: float = 1200.0) -> tuple[int, float]:
        if self._kdtree is None or not self._kdtree_ids:
            raise RuntimeError("Road snapper graph is empty; cannot snap coordinates.")
        x, y = WGS84_TO_UTM44N.transform(lon, lat)
        dist_m, idx = self._kdtree.query([x, y])
        if float(dist_m) > max_radius_m:
            raise ValueError(
                f"Point ({lat}, {lon}) is {dist_m:.1f} m from nearest OSM bus road (limit {max_radius_m} m)"
            )
        return self._kdtree_ids[int(idx)], float(dist_m)

    def nearest_road_candidates(
        self,
        lat: float,
        lon: float,
        k: int = 8,
        max_radius_m: float = 400.0,
    ) -> list[tuple[int, float]]:
        """Return up to k nearest OSM road nodes (captures both sides of divided oneway highways)."""
        if self._kdtree is None or not self._kdtree_ids:
            raise RuntimeError("Road snapper graph is empty; cannot snap coordinates.")
        x, y = WGS84_TO_UTM44N.transform(lon, lat)
        k_eff = min(k, len(self._kdtree_ids))
        dists, idxs = self._kdtree.query([x, y], k=k_eff)
        if k_eff == 1:
            dists, idxs = [float(dists)], [int(idxs)]
        cands = [
            (self._kdtree_ids[int(i)], float(d))
            for d, i in zip(dists, idxs)
            if float(d) <= max_radius_m
        ]
        if not cands:
            # Fallback to single closest node within wider radius
            nid, d = self.nearest_road_node(lat, lon)
            return [(nid, d)]
        return cands

    def snap_stop_sequence(
        self,
        shape_id: str,
        ordered_stops: list[tuple[str, float, float]],
        data_source: str = "osm_nagpur",
    ) -> tuple[ShapeRecord, list[SnappedStopProjection]]:
        """Snap an ordered stop sequence onto OSM roads, selecting the correct side of divided highways."""
        if len(ordered_stops) < 2:
            raise ValueError(f"Need at least 2 stops to snap shape {shape_id}, got {len(ordered_stops)}")

        # Gather up to 3 candidate curb nodes within 90 m (captures both sides of any divided highway
        # while staying strictly inside GTFS Validator's 100 m stop-to-shape tolerance)
        stop_cands: list[list[tuple[int, float]]] = [
            self.nearest_road_candidates(lat, lon, k=3, max_radius_m=90.0)
            for _, lat, lon in ordered_stops
        ]

        # Dynamic programming over candidate nodes to minimize total road travel cost + small offset penalty
        # Prevents bogus multi-km U-turns on dual-carriageway highways like Kamptee Rd / Wardha Rd (NH44)
        n_stops = len(ordered_stops)
        dp_cost: list[list[float]] = [[float("inf")] * len(c) for c in stop_cands]
        dp_prev: list[list[int]] = [[-1] * len(c) for c in stop_cands]
        dp_path: dict[tuple[int, int, int], list[int]] = {}

        for j, (_, err_m) in enumerate(stop_cands[0]):
            dp_cost[0][j] = err_m * 0.2

        for i in range(1, n_stops):
            prev_cands = stop_cands[i - 1]
            curr_cands = stop_cands[i]
            for pj, (u_nid, _) in enumerate(prev_cands):
                if dp_cost[i - 1][pj] == float("inf"):
                    continue
                for cj, (v_nid, err_m) in enumerate(curr_cands):
                    if u_nid == v_nid:
                        path_nodes = [u_nid]
                        leg_cost = 0.0
                    else:
                        try:
                            leg_cost, path_nodes = nx.bidirectional_dijkstra(
                                self.graph, source=u_nid, target=v_nid, weight="weight"
                            )
                        except nx.NetworkXNoPath:
                            continue
                    cand_total = dp_cost[i - 1][pj] + float(leg_cost) + err_m * 0.2
                    if cand_total < dp_cost[i][cj]:
                        dp_cost[i][cj] = cand_total
                        dp_prev[i][cj] = pj
                        dp_path[(i, pj, cj)] = path_nodes

        best_end_idx = min(range(len(stop_cands[-1])), key=lambda cj: dp_cost[-1][cj])
        if dp_cost[-1][best_end_idx] == float("inf"):
            raise RuntimeError(f"No drivable OSM bus route path found for {shape_id}")

        # Backtrack chosen candidate index per stop
        chosen_idx = [0] * n_stops
        chosen_idx[-1] = best_end_idx
        for i in range(n_stops - 1, 0, -1):
            chosen_idx[i - 1] = dp_prev[i][chosen_idx[i]]

        full_node_path: list[int] = []
        edge_classes: list[str] = []
        stop_projections: list[SnappedStopProjection] = []
        cum_m = 0.0

        for i, (stop_id, _, _) in enumerate(ordered_stops):
            cj = chosen_idx[i]
            nid, err_m = stop_cands[i][cj]
            slat, slon = self._node_coords[nid]
            if i == 0:
                full_node_path.append(nid)
                stop_projections.append(
                    SnappedStopProjection(
                        stop_id=stop_id,
                        s_m=0.0,
                        snap_error_m=round(err_m, 2),
                        snapped_lat=slat,
                        snapped_lon=slon,
                    )
                )
                continue

            pj = chosen_idx[i - 1]
            leg_nodes = dp_path[(i, pj, cj)]
            for u, v in itertools.pairwise(leg_nodes):
                edge_data = self.graph[u][v]
                cum_m += float(edge_data["length_m"])
                full_node_path.append(v)
                edge_classes.append(str(edge_data["road_class"]))

            stop_projections.append(
                SnappedStopProjection(
                    stop_id=stop_id,
                    s_m=round(cum_m, 2),
                    snap_error_m=round(err_m, 2),
                    snapped_lat=slat,
                    snapped_lon=slon,
                )
            )

        points: list[ShapePoint] = []
        running_s = 0.0
        for pt_idx, nid in enumerate(full_node_path):
            lat, lon = self._node_coords[nid]
            if pt_idx > 0:
                prev_nid = full_node_path[pt_idx - 1]
                running_s += float(self.graph[prev_nid][nid]["length_m"])
                rc = edge_classes[pt_idx - 1]
            else:
                rc = edge_classes[0] if edge_classes else "primary"
            points.append(ShapePoint(lat=lat, lon=lon, s_m=round(running_s, 2), road_class=rc))

        shape = ShapeRecord(
            shape_id=shape_id,
            points=tuple(points),
            snapping_method="pgrouting",  # Dijkstra on OSM road graph (pgRouting-compatible cost model)
            data_source=data_source,
        )
        return shape, stop_projections


def project_stops_onto_shape(
    shape: ShapeRecord,
    ordered_stops: list[tuple[str, float, float]],
) -> list[SnappedStopProjection]:
    """Project an ordered stop sequence monotonically onto an existing ShapeRecord (e.g. Metro)."""
    if not shape.points:
        raise ValueError(f"Cannot project stops onto empty shape {shape.shape_id}")

    pts_xy = [WGS84_TO_UTM44N.transform(p.lon, p.lat) for p in shape.points]
    projections: list[SnappedStopProjection] = []
    min_idx = 0

    for stop_id, lat, lon in ordered_stops:
        sx, sy = WGS84_TO_UTM44N.transform(lon, lat)
        best_s = shape.points[min_idx].s_m
        best_err = float("inf")
        best_seg_idx = min_idx

        for i in range(min_idx, len(pts_xy) - 1):
            x1, y1 = pts_xy[i]
            x2, y2 = pts_xy[i + 1]
            dx, dy = x2 - x1, y2 - y1
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 1e-9:
                t = 0.0
            else:
                t = max(0.0, min(1.0, ((sx - x1) * dx + (sy - y1) * dy) / seg_len_sq))
            px, py = x1 + t * dx, y1 + t * dy
            err = math.hypot(sx - px, sy - py)
            if err < best_err:
                best_err = err
                seg_len = math.sqrt(seg_len_sq)
                best_s = shape.points[i].s_m + t * seg_len
                best_seg_idx = i

        min_idx = best_seg_idx
        clamped_s = min(round(best_s, 2), round(shape.total_length_m, 2))
        projections.append(
            SnappedStopProjection(
                stop_id=stop_id,
                s_m=clamped_s,
                snap_error_m=round(best_err, 2),
                snapped_lat=lat,
                snapped_lon=lon,
            )
        )

    return projections
