# IPC-2581 LayerFeature Geometry

Phase 6.11 extracts IPC-2581 `LayerFeature` / `Set` manufacturing geometry into both aggregate summaries and normalized row-oriented geometry. These fields are intended to help review tools such as ThomsonLint inspect concrete routing evidence without re-walking the IPC XML.

## Board JSON Fields

The board export keeps all previous fields and adds row-oriented companions:

- `copper_features`: existing copper-only per-set summaries. Kept for compatibility.
- `copper_feature_summary`: existing aggregate copper dictionary. Kept unchanged.
- `copper_feature_summary_rows`: copper-only row summaries with `layer`, `net`, `feature_domain`, feature counts, descriptor refs, contour count, and `feature_count`.
- `board_feature_summary`: existing aggregate dictionary across all LayerFeature sets. Kept unchanged.
- `board_feature_summary_rows`: row summaries for copper and non-copper LayerFeature sets.
- `board_geometry_analysis`: existing per-layer aggregate feature analysis.
- `review_geometry_summary`: review-oriented net/layer, candidate, trace-width, and net routing summaries.

Example row:

```json
{
  "layer": "TOP",
  "net": "CAN_RX",
  "feature_domain": "copper",
  "polylines": 1,
  "polygons": 2,
  "pads": 2,
  "cutouts": 0,
  "contours": 2,
  "line_desc_refs": ["ROUND_500"],
  "fill_desc_refs": ["SOLID_FILL"],
  "feature_count": 5
}
```

## Normalized Routing Geometry

`routing_geometry` exposes extracted geometry in stable arrays:

- `units`: resolved IPC units when available.
- `detailed_geometry_truncated`: whether compatibility summaries were truncated.
- `feature_count`, `polyline_count`, `polygon_count`, `pad_count`, `cutout_count`.
- `routes`: normalized `Polyline` rows.
- `polygons`: normalized `Contour/Polygon` rows.
- `pads`: normalized `Pad` rows where location metadata is available.
- `cutouts`: normalized `Cutout` rows.

Route rows include `id`, `source`, `layer`, `net`, `feature_domain`, `line_desc_ref`, resolved `line_width`, `line_width_units`, ordered `points`, `bbox`, `segment_count`, and `has_curve`.

Polygon and cutout rows include ordered points, curve metadata, bounding boxes, point counts, and fill metadata where available. Pad rows include coordinates, primitive refs, xform attributes, and point-like bounding boxes when coordinates are present.

## Descriptors

`line_descriptors` is parsed from IPC `DictionaryLineDesc` / `EntryLineDesc` / `LineDesc`:

```json
{
  "id": "ROUND_500",
  "width": 0.005,
  "units": "INCH",
  "shape": "ROUND",
  "raw": {}
}
```

Routes retain their `line_desc_ref`. When a descriptor resolves, `line_width` and `line_width_units` are populated. If resolution fails, the ref is preserved and width remains `null`.

`fill_descriptors` is parsed from IPC `DictionaryFillDesc` / `EntryFillDesc` / `FillDesc`:

```json
{
  "id": "SOLID_FILL",
  "fill_type": "solid",
  "raw": {}
}
```

Polygons retain `fill_desc_ref` and include resolved fill metadata when available.

## Completeness Accounting

`routing_geometry_extraction` reports source and normalized counts:

- source object counts for polylines, polygons, pads, and cutouts.
- normalized counts for routes, polygons, pads, and cutouts.
- non-copper polyline and polygon counts.
- truncation metadata.
- `dropped_or_unparsed_feature_count`.
- `parse_warnings` for geometry objects that were counted but had incomplete parsed details.

The converter should not silently drop LayerFeature objects. If source counts exceed normalized counts, the difference is reported.

## Review Summary

`review_geometry_summary` still includes `net_layer_presence`, `net_feature_totals`, `routing_candidates`, `pad_only_nets`, `line_width_ref_usage`, `fill_ref_usage`, and name-based `candidate_differential_or_paired_nets`.

Phase 6.11 also adds:

- `routing_geometry_available`.
- `routing_geometry_counts`.
- `trace_width_usage`: resolved width usage by line descriptor, route count, and nets.
- `net_routing_summary`: per-net route, polygon, pad, cutout, layer, trace width, descriptor, and bounding-box summary.

## Routing Topology Summary

Phase 6.12 adds compact topology-oriented summaries so review agents do not need to walk every normalized route row for common questions.

`routing_topology_summary` is the canonical location for agentic review topology summaries. Board-root `trace_width_by_net` and `trace_width_usage_by_layer` are retained as backward-compatible aliases when present.

`routing_topology_summary` contains:

- `units`.
- `net_count`, `routed_net_count`, `pad_only_net_count`, `plane_candidate_count`, and `paired_net_candidate_count`.
- `nets`: one row per logical or geometry-bearing net.
- `paired_net_geometry_comparison`: compact geometry comparison rows for name-based paired-net candidates.
- `layer_transition_candidates`: conservative multi-layer copper evidence rows.
- `trace_width_by_net`: compact per-routed-net trace width summary.
- `trace_width_usage_by_layer`: compact per-layer and per-line-descriptor usage summary.
- `routing_evidence_warnings`: deterministic review hints.
- `limitations`: caveats specific to the topology summary.

Per-net topology rows include:

```json
{
  "net": "CAN_RX",
  "layers": ["TOP"],
  "route_count": 1,
  "polygon_count": 2,
  "pad_count": 2,
  "cutout_count": 0,
  "line_desc_refs": ["ROUND_500"],
  "min_trace_width": 0.005,
  "max_trace_width": 0.005,
  "bbox": {},
  "has_top_copper": true,
  "has_bottom_copper": false,
  "has_internal_copper": false,
  "has_plane_evidence": false,
  "is_plane_candidate": false,
  "is_pad_only": false,
  "is_routing_candidate": true,
  "geometry_evidence": [
    "1 route/polyline on TOP",
    "2 polygons on TOP",
    "2 pads on TOP",
    "trace width refs: ROUND_500"
  ]
}
```

`routing_topology_summary.trace_width_by_net` contains one row per routed net:

```json
{
  "net": "CAN_RX",
  "line_desc_refs": ["ROUND_500"],
  "min_trace_width": 0.005,
  "max_trace_width": 0.005,
  "route_count": 1,
  "layers": ["TOP"]
}
```

`routing_topology_summary.trace_width_usage_by_layer` groups normalized copper routes by layer and `LineDescRef`:

```json
{
  "layer": "TOP",
  "line_desc_ref": "ROUND_500",
  "line_width": 0.005,
  "units": "INCH",
  "route_count": 123,
  "nets": ["CAN_RX"]
}
```

Trace widths come from IPC-2581 `LineDescRef` resolution against `DictionaryLineDesc` / `EntryLineDesc` / `LineDesc`. If a descriptor is missing or cannot be resolved, the descriptor reference is preserved and width fields remain `null`; the converter does not guess widths.

These trace-width summaries are evidence indexes for review. They are not DRC, impedance, spacing, skew, route-length, or length-matching verification.

## Paired-Net Geometry Comparison

`paired_net_geometry_comparison` is generated from `review_geometry_summary.candidate_differential_or_paired_nets`. It reports each pair, candidate reason, whether geometry is available, compact rows for each net, and comparison booleans/deltas such as same layer set, same trace-width refs, route count delta, polygon count delta, pad count delta, and possible bounding-box overlap.

These rows do not infer that the pair is correctly routed. They do not verify impedance, coupling, spacing, skew, or length matching.

## Layer Transition Candidates

`layer_transition_candidates` identifies nets with copper evidence on multiple copper layers. The converter may have pad, hole, drill, or via-related data elsewhere, but this field intentionally uses conservative candidate language.

Do not treat a layer transition candidate as proof of actual via connectivity unless explicit via or hole association by net is present and reviewed separately.

## Plane Candidates

Plane candidate detection is heuristic and evidence-based. A net is not a plane candidate merely because it has pads on internal layers, appears on many layers, or has small top/bottom polygons around pads.

Stronger evidence is required, such as:

- power/ground-style net name plus polygon or cutout evidence.
- high cutout count on an internal layer.
- multiple polygons or cutouts on an internal plane layer.
- layer metadata indicating `PLANE` with non-pad copper geometry.
- large copper bounding-box evidence combined with polygon or cutout geometry.

Candidate reasons cite specific evidence, for example `GND has 682 cutouts on LAYER3` or `V3P3 has 314 cutouts on LAYER4`.

## Limitations

The converter does not prove electrical or manufacturing correctness from these summaries alone:

- Geometry is extracted from IPC-2581 manufacturing/export features, not from live CAD constraints.
- No true clearance DRC is performed.
- No net-short or spacing verification is performed.
- No polygon boolean connectivity verification is performed.
- Line width references are reported and resolved when possible, but not validated against design rules unless explicit rules are provided.
- Differential/paired nets are name-based candidates only.
- Plane candidate detection is heuristic and evidence-based.

Do not conclude that a plane is correct, a route is connected, or a differential pair is impedance controlled from these geometry and topology summaries alone. Use them to decide what ThomsonLint should inspect next and to cite concrete extracted evidence in findings.
