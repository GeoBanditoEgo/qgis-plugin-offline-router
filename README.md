# SpatiaLite Router — QGIS Plugin

Find the **shortest path** between two map-clicked points using a **local SpatiaLite
routing database** created with `spatialite_osm_net`.  
The plugin works **completely offline** — no internet connection is needed at runtime.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Preparing the Routing Database](#preparing-the-routing-database)
4. [Usage](#usage)
5. [How It Works](#how-it-works)
6. [Troubleshooting](#troubleshooting)
7. [File Layout](#file-layout)

---

## Requirements

| Component | Minimum version |
|-----------|----------------|
| QGIS | 3.10 |
| Python | 3.7 (bundled with QGIS) |
| mod_spatialite | any recent version |

`mod_spatialite` must be discoverable at runtime (see [Troubleshooting](#troubleshooting)).

---

## Installation

### Option A — Install from ZIP (recommended)

1. In QGIS, open **Plugins → Manage and Install Plugins… → Install from ZIP**.
2. Select `spatialite_router.zip`.
3. Click **Install Plugin**.
4. The plugin toolbar button (blue route icon) appears in the toolbar.

### Option B — Manual install

1. Copy the `spatialite_router/` folder to your QGIS plugins directory:

   | Platform | Path |
   |----------|------|
   | Windows  | `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\` |
   | macOS    | `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/` |
   | Linux    | `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/` |

2. Restart QGIS.
3. Enable the plugin: **Plugins → Manage and Install Plugins → Installed → ☑ SpatiaLite Router**.

---

## Preparing the Routing Database

### Step 1 — Create the SpatiaLite OSM network

```bash
# Download an OSM extract for your area (.osm or .osm.pbf)
# Then run:
spatialite_osm_net -o your_area.osm -d routing.sqlite -T road_routing
```

This creates the `road_routing` (edges) and `road_routing_nodes` tables.

### Step 2 — Generate routing data tables

Open the database in `spatialite_gui` (or any SpatiaLite shell) and run:

```sql
SELECT CreateRouting(
    'by_car_routing_data',  -- routing data table
    'by_car_routing',       -- routing table name
    'road_routing',         -- edges table
    'node_from',            -- from-node column
    'node_to',              -- to-node column
    'geometry',             -- geometry column
    'cost',                 -- cost column
    'name',                 -- name column
    1,                      -- directed graph
    1,                      -- has on-way fields
    'oneway_fromto',        -- one-way from→to column
    'oneway_tofrom',        -- one-way to→from column
    0                       -- not bidirectional
);
```

This creates the `by_car_routing` and `by_car_routing_data` tables needed for
the `SP_ShortestPath()` virtual table.

### Step 3 — (Optional) Add the database as QGIS layers

You can add `road_routing` (lines) and `road_routing_nodes` (points) as regular
QGIS layers for visual reference via **Layer → Add Layer → Add SpatiaLite Layer**.

The plugin does **not** require these layers to be loaded in the project — it
reads the SQLite file directly.

---

## Usage

1. **Open the plugin**: click the toolbar icon or use **Plugins → SpatiaLite Router**.

2. **Select the database**: click **Browse…** and navigate to your `.sqlite` routing file.
   The path is remembered between sessions.

3. **Pick the start point**:
   - Click **📍 Pick Start Point** (button turns active).
   - Click anywhere on the QGIS map canvas.
   - The coordinates (EPSG:4326) appear next to the button.

4. **Pick the end point**:
   - Click **🏁 Pick End Point**.
   - Click on the canvas.

5. **Run routing**: Click **▶ Find Shortest Path**.
   - A background thread executes the SpatiaLite query.
   - On success, two new layers are added to your project:
     - **Shortest Path Result** — blue line showing the route.
     - **Route Start/End Points** — green (start) and red (end) markers.
   - The map canvas zooms to the route extent.

6. **Repeat** as needed.  
   The *Remove previous result layer* option keeps the layer list clean.

---

## How It Works

```
User click (EPSG:3857)
        │
        ▼
  Reproject to EPSG:4326
        │
        ▼
  find_nearest_node()
  ────────────────────
  SELECT node_id, ...
  FROM road_routing_nodes
  ORDER BY ST_Distance(geometry, MakePoint(lon,lat,4326))
  LIMIT 1
        │
        ▼
  SP_ShortestPath()
  ─────────────────
  SELECT seq, node_from, node_to, cost,
         r.name, AsText(r.geometry)
  FROM SP_ShortestPath('by_car_routing', 0, start_id, end_id)
  JOIN road_routing r ON r.node_from=... AND r.node_to=...
  ORDER BY seq
        │
        ▼
  Load segments as QgsVectorLayer (EPSG:3857)
  Add to QGIS project & zoom to extent
```

**Coordinate handling**:

- The QGIS project canvas is in **EPSG:3857** (Web Mercator).
- The SpatiaLite database is in **EPSG:4326** (WGS 84 geographic).
- The plugin re-projects click coordinates from 3857 → 4326 before querying,
  and re-projects result geometries from 4326 → 3857 before displaying.

---

## Troubleshooting

### "Could not find the mod_spatialite shared library"

The SpatiaLite extension must be loadable by Python's `sqlite3` module.

**Linux**:
```bash
sudo apt install libsqlite3-mod-spatialite   # Debian/Ubuntu
sudo dnf install spatialite                  # Fedora
```

**macOS**:
```bash
brew install spatialite-tools
```

**Windows (OSGeo4W)**:
`mod_spatialite.dll` ships with QGIS.  If not found automatically, add
`C:\OSGeo4W\bin` to your `PATH` environment variable.

### "No route found between the selected points"

- The points may be in disconnected parts of the road network (e.g., an island).
- One-way restrictions may block the path.
- Try clicking closer to a road — the node-snapping uses Euclidean distance,
  so large gaps from roads can snap to unexpected nodes.

### "SP_ShortestPath" table function error

Make sure you ran the `CreateRouting(...)` SQL statement **after** importing the
OSM data. The `by_car_routing` table must exist in the database.

### Route result looks wrong / missing segments

The JOIN `ON r.node_from = sp.node_from AND r.node_to = sp.node_to` assumes that
each directed edge appears exactly once in `road_routing`. If your data has
duplicate edges, add `LIMIT 1` or use a subquery.

---

## File Layout

```
spatialite_router/
├── __init__.py          — QGIS plugin entry point
├── plugin.py            — Plugin class (toolbar, menu)
├── dialog.py            — Main UI dialog
├── map_tool.py          — Canvas click tool
├── routing.py           — SpatiaLite query engine
├── metadata.txt         — QGIS plugin metadata
├── icons/
│   ├── icon.svg
│   └── icon.png
└── README.md
```

---

## License

MIT — free to use, modify, and redistribute.
