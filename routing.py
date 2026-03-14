# -*- coding: utf-8 -*-
"""
SpatiaLite routing engine — multi-leg with turn-by-turn directions.
"""
import sqlite3
import heapq
import math
import os


def _find_spatialite_lib():
    candidates = [
        'mod_spatialite', 'mod_spatialite.dll',
        r'C:\OSGeo4W\bin\mod_spatialite.dll',
        r'C:\OSGeo4W64\bin\mod_spatialite.dll',
        '/usr/local/lib/mod_spatialite.dylib',
        '/opt/homebrew/lib/mod_spatialite.dylib',
        '/usr/lib/x86_64-linux-gnu/mod_spatialite.so',
        '/usr/lib/aarch64-linux-gnu/mod_spatialite.so',
        '/usr/lib/mod_spatialite.so',
        '/usr/local/lib/mod_spatialite.so',
    ]
    for c in candidates:
        try:
            con = sqlite3.connect(':memory:')
            con.enable_load_extension(True)
            con.load_extension(c)
            con.close()
            return c
        except Exception:
            continue
    return None

SPATIALITE_LIB = _find_spatialite_lib()


class RoutingError(Exception):
    pass


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _open_db(db_path, need_spatialite=True):
    if not os.path.isfile(db_path):
        raise RoutingError(f"Database file not found:\n{db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if need_spatialite:
        if not SPATIALITE_LIB:
            con.close()
            raise RoutingError(
                "mod_spatialite not found.\n"
                "Linux: sudo apt install libsqlite3-mod-spatialite\n"
                "macOS: brew install spatialite-tools\n"
                "Windows: ships with OSGeo4W/QGIS installer."
            )
        try:
            con.enable_load_extension(True)
            con.load_extension(SPATIALITE_LIB)
        except Exception as e:
            con.close()
            raise RoutingError(f"Could not load SpatiaLite: {e}")
    return con


def _introspect_node_table(con, nodes_table):
    cur = con.execute(f"PRAGMA table_info({nodes_table})")
    cols = [r['name'] for r in cur.fetchall()]
    if not cols:
        raise RoutingError(f"Table '{nodes_table}' not found.")
    for c in ('node_id', 'NodeId', 'id', 'ID'):
        if c in cols:
            return c
    return cols[0]


def _introspect_routing_table(con, routing_table):
    cur = con.execute(f"PRAGMA table_info({routing_table})")
    cols = [r['name'] for r in cur.fetchall()]
    if not cols:
        raise RoutingError(f"Table '{routing_table}' not found. Run CreateRouting() first.")
    m = {c.lower(): c for c in cols}
    return (m.get('nodefrom', m.get('node_from', 'NodeFrom')),
            m.get('nodeto',   m.get('node_to',   'NodeTo')),
            m.get('cost', 'Cost'),
            m.get('geometry', m.get('geom', 'Geometry')))


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _haversine_m(lon1, lat1, lon2, lat2):
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing(lon1, lat1, lon2, lat2):
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r)*math.sin(lat2r) - math.sin(lat1r)*math.cos(lat2r)*math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _parse_wkt_coords(wkt):
    """Flatten a WKT LINESTRING or MULTILINESTRING to [(lon,lat), ...]."""
    if not wkt:
        return []
    wkt = wkt.strip()
    upper = wkt.upper()
    if upper.startswith('MULTILINESTRING'):
        inner = wkt[wkt.index('(')+1 : wkt.rindex(')')]
        inner = inner.replace('(', '').replace(')', '')
    elif upper.startswith('LINESTRING'):
        inner = wkt[wkt.index('(')+1 : wkt.rindex(')')]
    else:
        return []
    pts = []
    for pair in inner.split(','):
        parts = pair.strip().split()
        if len(parts) >= 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return pts


def _wkt_length_m(wkt):
    pts = _parse_wkt_coords(wkt)
    return sum(_haversine_m(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1])
               for i in range(1, len(pts)))


def _wkt_start_bearing(wkt):
    pts = _parse_wkt_coords(wkt)
    return _bearing(pts[0][0], pts[0][1], pts[1][0], pts[1][1]) if len(pts) >= 2 else 0.0


def _wkt_end_bearing(wkt):
    pts = _parse_wkt_coords(wkt)
    return _bearing(pts[-2][0], pts[-2][1], pts[-1][0], pts[-1][1]) if len(pts) >= 2 else 0.0


# ---------------------------------------------------------------------------
# Turn classification
# ---------------------------------------------------------------------------
def _cardinal(b):
    return ['N','NE','E','SE','S','SW','W','NW'][int((b + 22.5) / 45) % 8]


def _turn_instruction(prev_b, next_b, street):
    diff = (next_b - prev_b + 360) % 360
    if   diff < 20 or diff > 340:  d = 'Continue straight'
    elif diff < 60:                 d = 'Bear right'
    elif diff < 120:                d = 'Turn right'
    elif diff < 170:                d = 'Sharp right'
    elif diff < 200:                d = 'Make a U-turn'
    elif diff < 240:                d = 'Sharp left'
    elif diff < 300:                d = 'Turn left'
    else:                           d = 'Bear left'
    return f'{d} onto {street}' if street else d


def _fmt_dist(m, imperial=False):
    if imperial:
        ft = m * 3.28084
        if ft < 500:   return f'{ft:.0f} ft'
        mi = m / 1609.344
        if mi < 0.1:   return f'{round(ft/10)*10:.0f} ft'
        return f'{mi:.2f} mi'
    else:
        if m < 50:    return f'{m:.0f} m'
        if m < 1000:  return f'{round(m/10)*10:.0f} m'
        return f'{m/1000:.1f} km'


def _fmt_duration(sec):
    if sec < 60:   return f'{int(sec)} sec'
    mins = int(sec / 60)
    if mins < 60:  return f'{mins} min'
    return f'{mins//60} hr {mins%60} min'


# ---------------------------------------------------------------------------
# Node snapping
# ---------------------------------------------------------------------------
def find_nearest_node(con, lon, lat, nodes_table='road_routing_nodes'):
    pk = _introspect_node_table(con, nodes_table)
    for delta in (0.05, 0.15, 0.5, 2.0):
        sql = f"""
            SELECT {pk} AS node_id, ST_X(geometry) AS lon, ST_Y(geometry) AS lat,
                   ((ST_X(geometry)-:lon)*(ST_X(geometry)-:lon)
                   +(ST_Y(geometry)-:lat)*(ST_Y(geometry)-:lat)) AS d2
            FROM {nodes_table}
            WHERE ST_X(geometry) BETWEEN :lon-:d AND :lon+:d
              AND ST_Y(geometry) BETWEEN :lat-:d AND :lat+:d
            ORDER BY d2 LIMIT 1"""
        row = con.execute(sql, {'lon': lon, 'lat': lat, 'd': delta}).fetchone()
        if row:
            return int(row['node_id']), float(row['lon']), float(row['lat'])
    sql = f"""SELECT {pk} AS node_id, ST_X(geometry) AS lon, ST_Y(geometry) AS lat
              FROM {nodes_table}
              ORDER BY ((ST_X(geometry)-:lon)*(ST_X(geometry)-:lon)
                       +(ST_Y(geometry)-:lat)*(ST_Y(geometry)-:lat)) LIMIT 1"""
    row = con.execute(sql, {'lon': lon, 'lat': lat}).fetchone()
    if not row:
        raise RoutingError(f"No nodes in '{nodes_table}'.")
    return int(row['node_id']), float(row['lon']), float(row['lat'])


# ---------------------------------------------------------------------------
# Primary strategy: pre-computed by_car_routing
# ---------------------------------------------------------------------------
def _query_routing_table(con, start_id, end_id, routing_table):
    nf, nt, cost_col, geom_col = _introspect_routing_table(con, routing_table)
    sql = f"""SELECT {cost_col} AS cost, ST_AsText({geom_col}) AS wkt
              FROM {routing_table} WHERE {nf}=:s AND {nt}=:e LIMIT 1"""
    row = con.execute(sql, {'s': start_id, 'e': end_id}).fetchone()
    if not row or not row['wkt']:
        return None
    return [{'seq': 1, 'node_from': start_id, 'node_to': end_id,
              'cost': row['cost'], 'name': '', 'geometry_wkt': row['wkt']}]


# ---------------------------------------------------------------------------
# Fallback: Python Dijkstra
# ---------------------------------------------------------------------------
def _build_graph(con, edges_table):
    cols = {r['name'].lower(): r['name'] for r in
            con.execute(f"PRAGMA table_info({edges_table})").fetchall()}
    if not cols:
        raise RoutingError(f"Edge table '{edges_table}' not found.")
    nf  = cols.get('node_from', 'node_from')
    nt  = cols.get('node_to',   'node_to')
    c   = cols.get('cost',      'cost')
    ow1 = cols.get('oneway_fromto')
    ow2 = cols.get('oneway_tofrom')
    nm  = cols.get('name')
    gm  = cols.get('geometry', cols.get('geom', 'geometry'))
    sql = f"""SELECT {nf} AS nf, {nt} AS nt, {c} AS cost,
                     {ow1 or 'NULL'} AS ow_ft, {ow2 or 'NULL'} AS ow_tf,
                     {nm  or 'NULL'} AS name, ST_AsText({gm}) AS wkt
              FROM {edges_table} WHERE {c} IS NOT NULL AND {c}>0"""
    rows = con.execute(sql).fetchall()
    if not rows:
        raise RoutingError(f"No routable edges in '{edges_table}'.")
    graph = {}; ewkt = {}; ename = {}
    for row in rows:
        nfv = int(row['nf']); ntv = int(row['nt']); cost = float(row['cost'])
        ow_ft = int(row['ow_ft']) if row['ow_ft'] is not None else 0
        ow_tf = int(row['ow_tf']) if row['ow_tf'] is not None else 0
        wkt = row['wkt'] or ''; nm_ = row['name'] or ''
        ewkt[(nfv,ntv)] = wkt; ewkt[(ntv,nfv)] = wkt
        ename[(nfv,ntv)] = nm_; ename[(ntv,nfv)] = nm_
        if ow_ft == 1 and ow_tf == 0:
            graph.setdefault(nfv, []).append((cost, ntv))
        elif ow_ft == 0 and ow_tf == 1:
            graph.setdefault(ntv, []).append((cost, nfv))
        else:
            graph.setdefault(nfv, []).append((cost, ntv))
            graph.setdefault(ntv, []).append((cost, nfv))
    return graph, ewkt, ename


def _dijkstra(graph, start_id, end_id):
    dist = {start_id: 0.0}; prev = {}; heap = [(0.0, start_id)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, float('inf')): continue
        if u == end_id: break
        for cost, v in graph.get(u, []):
            nd = d + cost
            if nd < dist.get(v, float('inf')):
                dist[v] = nd; prev[v] = u; heapq.heappush(heap, (nd, v))
    if end_id not in dist:
        raise RoutingError(
            "No route found.\n"
            "• Points may be in disconnected parts of the network\n"
            "• One-way restrictions may block the route\n"
            "• Try clicking closer to a road")
    return dist, prev


def _reconstruct_path(prev, start_id, end_id, ewkt, ename):
    path = []; node = end_id
    while node != start_id:
        p = prev[node]; path.append((p, node)); node = p
    path.reverse()
    segs = []
    for i, (nf, nt) in enumerate(path, 1):
        segs.append({'seq': i, 'node_from': nf, 'node_to': nt, 'cost': None,
                     'name': ename.get((nf,nt),''), 'geometry_wkt': ewkt.get((nf,nt),'')})
    return segs


def _fallback_dijkstra(con, start_id, end_id, edges_table):
    graph, ewkt, ename = _build_graph(con, edges_table)
    missing = [str(n) for n in (start_id, end_id) if n not in graph]
    if missing:
        raise RoutingError(f"Snapped node(s) not in graph: {', '.join(missing)}.\n"
                           "Try clicking a different nearby intersection.")
    dist, prev = _dijkstra(graph, start_id, end_id)
    segs = _reconstruct_path(prev, start_id, end_id, ewkt, ename)
    running = 0.0
    for seg in segs:
        seg['cost'] = dist.get(seg['node_to'], 0.0) - running
        running = dist.get(seg['node_to'], 0.0)
    return segs


# ---------------------------------------------------------------------------
# Segment geometry orientation
# ---------------------------------------------------------------------------
def _orient_segment_wkt(wkt, node_from_lon, node_from_lat,
                         node_to_lon, node_to_lat):
    """
    Return a WKT LINESTRING whose first vertex is closest to node_from
    and whose last vertex is closest to node_to.  If the stored geometry
    runs in the opposite direction it is reversed.
    Handles LINESTRING and MULTILINESTRING (flattened to LINESTRING).
    """
    if not wkt:
        return wkt
    pts = _parse_wkt_coords(wkt)
    if len(pts) < 2:
        return wkt

    def d2(lon1, lat1, lon2, lat2):
        return (lon1 - lon2) ** 2 + (lat1 - lat2) ** 2

    first_lon, first_lat = pts[0]
    # Distance from geometry's first vertex to node_from vs node_to
    d_first_to_from = d2(first_lon, first_lat, node_from_lon, node_from_lat)
    d_first_to_to   = d2(first_lon, first_lat, node_to_lon,   node_to_lat)

    if d_first_to_to < d_first_to_from:
        # Geometry is reversed relative to travel direction — flip it
        pts = list(reversed(pts))

    coords = ', '.join(f'{lon} {lat}' for lon, lat in pts)
    return f'LINESTRING({coords})'



def _route_one_leg(con, slon, slat, elon, elat, routing_table, edges_table, nodes_table):
    s_id, _, _ = find_nearest_node(con, slon, slat, nodes_table)
    e_id, _, _ = find_nearest_node(con, elon, elat, nodes_table)
    if s_id == e_id:
        raise RoutingError(f"Start and end snap to the same node ({s_id}).\n"
                           "Choose points further apart.")
    segs = _query_routing_table(con, s_id, e_id, routing_table)
    if segs is None:
        segs = _fallback_dijkstra(con, s_id, e_id, edges_table)
    return segs, s_id, e_id


# ---------------------------------------------------------------------------
# Directions builder
# ---------------------------------------------------------------------------
def _merge_same_street(segments):
    """Merge consecutive same-street segments for cleaner directions."""
    if not segments:
        return []
    merged = [dict(segments[0])]
    for seg in segments[1:]:
        prev = merged[-1]
        same = (seg['name'] and prev['name'] and
                seg['name'].lower() == prev['name'].lower() and
                not prev.get('_is_waypoint_end'))
        if same:
            if prev['cost'] is not None and seg['cost'] is not None:
                prev['cost'] += seg['cost']
            prev.setdefault('_extra_dist_m', 0.0)
            prev['_extra_dist_m'] += _wkt_length_m(seg['geometry_wkt'])
            if seg.get('_is_waypoint_end'):
                prev['_is_waypoint_end'] = True
                prev['_waypoint_label'] = seg.get('_waypoint_label', '')
        else:
            merged.append(dict(seg))
    return merged


def _build_directions(merged_segs, waypoint_labels, imperial=False):
    steps = []
    cumulative_m = 0.0
    prev_end_b = None
    for i, seg in enumerate(merged_segs):
        wkt = seg.get('geometry_wkt') or ''
        dist_m = _wkt_length_m(wkt) + seg.get('_extra_dist_m', 0.0)
        street = seg.get('name') or ''
        start_b = _wkt_start_bearing(wkt)
        end_b   = _wkt_end_bearing(wkt)

        if i == 0:
            instr = f'Head {_cardinal(start_b)}'
            if street: instr += f' on {street}'
        elif prev_end_b is not None:
            instr = _turn_instruction(prev_end_b, start_b, street)
        else:
            instr = f'Continue on {street}' if street else 'Continue'

        steps.append({
            'step':           len(steps) + 1,
            'instruction':    instr,
            'street':         street,
            'distance_m':     dist_m,
            'distance_str':   _fmt_dist(dist_m, imperial=imperial),
            'cumulative_m':   cumulative_m,
            'is_waypoint':    seg.get('_is_waypoint_end', False),
            'waypoint_label': seg.get('_waypoint_label', ''),
        })
        cumulative_m += dist_m
        prev_end_b = end_b

    last_label = waypoint_labels[-1] if waypoint_labels else 'Destination'
    steps.append({
        'step': len(steps) + 1, 'instruction': f'Arrive at {last_label}',
        'street': '', 'distance_m': 0.0, 'distance_str': '',
        'cumulative_m': cumulative_m, 'is_waypoint': True,
        'waypoint_label': last_label,
    })
    return steps


# ---------------------------------------------------------------------------
# Public API — multi-leg
# ---------------------------------------------------------------------------
def run_multi_leg_route(db_path, waypoints_4326,
                        routing_table='by_car_routing',
                        edges_table='road_routing',
                        nodes_table='road_routing_nodes',
                        imperial=False):
    """
    Route through an ordered list of (lon,lat) waypoints in EPSG:4326.
    Returns a dict with keys: all_segments, merged, directions,
                               total_m, total_cost, snapped_nodes, waypoint_labels.
    """
    if len(waypoints_4326) < 2:
        raise RoutingError("At least 2 waypoints required.")
    con = _open_db(db_path, need_spatialite=True)
    try:
        labels = (['Start'] +
                  [f'Via {i}' for i in range(1, len(waypoints_4326)-1)] +
                  ['Destination'])
        all_segs = []
        snapped  = []
        pk = _introspect_node_table(con, nodes_table)

        for leg in range(len(waypoints_4326) - 1):
            slon, slat = waypoints_4326[leg]
            elon, elat = waypoints_4326[leg+1]
            leg_segs, s_id, e_id = _route_one_leg(
                con, slon, slat, elon, elat,
                routing_table, edges_table, nodes_table)

            # Collect snapped node coordinates
            for nid in ([s_id] if leg == 0 else []) + [e_id]:
                row = con.execute(
                    f"SELECT ST_X(geometry) AS lon, ST_Y(geometry) AS lat "
                    f"FROM {nodes_table} WHERE {pk}=?", (nid,)).fetchone()
                lon_ = float(row['lon']) if row else waypoints_4326[leg if nid==s_id else leg+1][0]
                lat_ = float(row['lat']) if row else waypoints_4326[leg if nid==s_id else leg+1][1]
                snapped.append((nid, lon_, lat_))

            # Mark leg end
            if leg_segs:
                leg_segs[-1]['_is_waypoint_end'] = True
                leg_segs[-1]['_waypoint_label']  = labels[leg+1]

            # Fix geometry direction on each segment so arrows always point
            # from node_from → node_to (the actual travel direction).
            for seg in leg_segs:
                wkt = seg.get('geometry_wkt') or ''
                if not wkt:
                    continue
                nf_id = seg.get('node_from')
                nt_id = seg.get('node_to')
                if nf_id is None or nt_id is None:
                    continue
                row_f = con.execute(
                    f"SELECT ST_X(geometry) AS lon, ST_Y(geometry) AS lat "
                    f"FROM {nodes_table} WHERE {pk}=?", (nf_id,)).fetchone()
                row_t = con.execute(
                    f"SELECT ST_X(geometry) AS lon, ST_Y(geometry) AS lat "
                    f"FROM {nodes_table} WHERE {pk}=?", (nt_id,)).fetchone()
                if row_f and row_t:
                    seg['geometry_wkt'] = _orient_segment_wkt(
                        wkt,
                        float(row_f['lon']), float(row_f['lat']),
                        float(row_t['lon']), float(row_t['lat']))

            offset = len(all_segs)
            for s in leg_segs: s['seq'] += offset
            all_segs.extend(leg_segs)

        total_m    = sum(_wkt_length_m(s['geometry_wkt']) for s in all_segs)
        total_cost = sum(s['cost'] for s in all_segs if s['cost'] is not None)
        merged     = _merge_same_street(all_segs)
        directions = _build_directions(merged, labels, imperial=imperial)

        return {'all_segments': all_segs, 'merged': merged, 'directions': directions,
                'total_m': total_m, 'total_cost': total_cost,
                'snapped_nodes': snapped, 'waypoint_labels': labels,
                'imperial': imperial}
    finally:
        con.close()


# Backward-compatible single-leg wrapper
def run_shortest_path(db_path, start_lon, start_lat, end_lon, end_lat,
                      routing_table='by_car_routing',
                      edges_table='road_routing',
                      nodes_table='road_routing_nodes'):
    r = run_multi_leg_route(db_path, [(start_lon,start_lat),(end_lon,end_lat)],
                            routing_table, edges_table, nodes_table)
    return r['all_segments'], r['snapped_nodes'][0][0], r['snapped_nodes'][-1][0]


def get_spatialite_status():
    return (f"✓ mod_spatialite found: {SPATIALITE_LIB}" if SPATIALITE_LIB
            else "✗ mod_spatialite NOT found — routing will not work.")


def probe_db(db_path):
    result = {'ok': False, 'message': '', 'tables': []}
    try:
        con = _open_db(db_path, need_spatialite=True)
        result['tables'] = [r[0] for r in
            con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        con.close()
        missing = {'road_routing','road_routing_nodes','by_car_routing'} - {t.lower() for t in result['tables']}
        if missing:
            result['message'] = f"Missing tables: {', '.join(sorted(missing))}"
        else:
            result['ok'] = True; result['message'] = 'Database looks good.'
    except RoutingError as e:
        result['message'] = str(e)
    return result


def diagnose_route(db_path, start_lon, start_lat, end_lon, end_lat,
                   edges_table='road_routing', nodes_table='road_routing_nodes',
                   routing_table='by_car_routing'):
    lines = []
    try:
        con = _open_db(db_path, need_spatialite=True)
        s_id,slon,slat = find_nearest_node(con,start_lon,start_lat,nodes_table)
        e_id,elon,elat = find_nearest_node(con,end_lon,  end_lat,  nodes_table)
        lines.append(f"Start → node {s_id}  ({slat:.6f}, {slon:.6f})")
        lines.append(f"End   → node {e_id}  ({elat:.6f}, {elon:.6f})")
        if s_id == e_id:
            lines.append("⚠ Same node!"); con.close(); return '\n'.join(lines)
        nf,nt,cc,gc = _introspect_routing_table(con,routing_table)
        c = con.execute(f"SELECT COUNT(*) AS c FROM {routing_table} WHERE {nf}=? AND {nt}=?",
                        (s_id,e_id)).fetchone()['c']
        lines.append(f"Pre-computed rows for pair: {c}")
        ecols = {r['name'].lower():r['name'] for r in con.execute(f"PRAGMA table_info({edges_table})").fetchall()}
        nfe=ecols.get('node_from','node_from'); nte=ecols.get('node_to','node_to'); ce=ecols.get('cost','cost')
        total = con.execute(f"SELECT COUNT(*) AS c FROM {edges_table} WHERE {ce}>0").fetchone()['c']
        lines.append(f"Total routable edges: {total}")
        for lbl,nid in [('Start',s_id),('End',e_id)]:
            cnt = con.execute(f"SELECT COUNT(*) AS c FROM {edges_table} WHERE ({nfe}=? OR {nte}=?) AND {ce}>0",(nid,nid)).fetchone()['c']
            lines.append(f"Edges at {lbl} node {nid}: {cnt}")
        con.close()
    except Exception as e:
        lines.append(f"Error: {e}")
    return '\n'.join(lines)

# ---------------------------------------------------------------------------
# Nodes layer loader (for map display)
# ---------------------------------------------------------------------------
def load_nodes_layer(db_path, nodes_table='road_routing_nodes'):
    """
    Load road_routing_nodes from db_path as a QgsVectorLayer with a 3-layer
    complex marker symbol:
      Layer 0 — circle 1.5mm, transparent fill, black stroke 0.5
      Layer 1 — circle 1.0mm, transparent fill, white stroke 0.5
      Layer 2 — circle 0.8mm, red opaque fill,  transparent stroke 0.5
    Returns the layer (not yet added to project) or None on failure.
    """
    try:
        from qgis.core import (
            QgsVectorLayer, QgsSingleSymbolRenderer, QgsMarkerSymbol,
            QgsSimpleMarkerSymbolLayer
        )

        uri = f'{db_path}|layername={nodes_table}'
        lyr = QgsVectorLayer(uri, 'Road Routing Nodes', 'ogr')
        if not lyr.isValid():
            return None

        def _circle(size, fill_rgba, stroke_rgba, stroke_width='0.5'):
            return QgsSimpleMarkerSymbolLayer.create({
                'name':               'circle',
                'color':              fill_rgba,
                'outline_color':      stroke_rgba,
                'outline_width':      stroke_width,
                'outline_width_unit': 'MM',
                'size':               str(size),
                'size_unit':          'MM',
            })

        transparent = '0,0,0,0'

        sl0 = _circle(1.5, transparent,        '0,0,0,255')      # black stroke
        sl1 = _circle(1.0, transparent,        '255,255,255,255')  # white stroke
        sl2 = _circle(0.8, '255,0,0,255',      transparent)      # red fill

        marker = QgsMarkerSymbol()
        marker.changeSymbolLayer(0, sl0)   # replace the default layer
        marker.appendSymbolLayer(sl1)
        marker.appendSymbolLayer(sl2)
        marker.setOpacity(0.5)

        lyr.setRenderer(QgsSingleSymbolRenderer(marker))
        return lyr
    except Exception:
        return None
