#!/usr/bin/env python3
"""
ipc2581_to_thomson.py

Best-effort IPC-2581 XML -> ThomsonLint-style JSON exporter.

This is intended as a practical starting point for Altium IPC-2581 exports.
It emits files named like ThomsonLint expects:
  <project>-thomson-export-sch.json
  <project>-thomson-export-brd.json
  <project>-thomson-export-stack.json

Limitations:
- IPC-2581 is manufacturing/layout-oriented, not a full schematic database.
- Pin electrical direction is usually not present, so schematic pins are marked UNK.
- Different CAD tools use slightly different IPC-2581 attribute naming. This parser is
  namespace-insensitive and attribute-tolerant, but you should inspect the generated
  JSON against your first real Altium export.

Usage:
  python3 ipc2581_to_thomson.py input.xml --project MyBoard --output ./exports
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VERSION = "ipc2581-adapter-0.1"


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tag_is(elem: ET.Element, name: str) -> bool:
    return strip_ns(elem.tag).lower() == name.lower()


def iter_tag(root: ET.Element, *names: str) -> Iterable[ET.Element]:
    wanted = {n.lower() for n in names}
    for e in root.iter():
        if strip_ns(e.tag).lower() in wanted:
            yield e


def attr_any(elem: ET.Element, *names: str, default: str = "") -> str:
    wanted = [norm_key(n) for n in names]
    for k, v in elem.attrib.items():
        nk = norm_key(k)
        if nk in wanted:
            return v
    # weaker fallback: common variants like refDes vs refdes vs ref-des
    for k, v in elem.attrib.items():
        nk = norm_key(k)
        for w in wanted:
            if nk == w or nk.endswith(w) or w.endswith(nk):
                return v
    return default


def float_any(elem: ET.Element, *names: str, default: float = 0.0) -> float:
    val = attr_any(elem, *names, default="")
    try:
        return float(val)
    except Exception:
        return default


def text_of(elem: ET.Element) -> str:
    return "".join(elem.itertext()).strip()


def child_text_any(elem: ET.Element, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for c in elem:
        if strip_ns(c.tag).lower() in wanted:
            txt = text_of(c)
            if txt:
                return txt
    return ""


def is_power_net(name: str) -> bool:
    upper = (name or "").upper()
    for pat in (
        "VCC", "VDD", "VBUS", "VIN", "VOUT", "VBAT", "VSYS", "VPLUS",
        "VAUX", "VPP", "VANALOG", "+3V", "+5V", "+12V", "+24V",
        "+14V", "+15V", "+28V", "+48V", "-5V", "-12V", "-15V",
        "3V3", "5V0", "1V8", "1V2", "2V5", "14V0", "48V0", "POE", "PWR"
    ):
        if pat in upper:
            return True
    return False


def is_ground_net(name: str) -> bool:
    upper = (name or "").upper()
    return upper in {"GND", "AGND", "DGND", "PGND", "SGND"} or "VSS" in upper or "GND" in upper


def is_clock_net(name: str) -> bool:
    upper = (name or "").upper()
    return any(p in upper for p in ("CLK", "XTAL", "SCK", "SCLK", "MCLK", "BCLK", "LRCK", "OSC"))


def is_diff_pair_member(name: str) -> int:
    upper = (name or "").upper()
    if upper.endswith(("_P", "DP", "D+", "_DP")):
        return 1
    if upper.endswith(("_N", "DN", "D-", "_DN")):
        return -1
    return 0


def find_diff_partner(name: str) -> str:
    pairs = {"_P": "_N", "_N": "_P", "DP": "DN", "DN": "DP", "D+": "D-", "D-": "D+", "_DP": "_DN", "_DN": "_DP"}
    upper = name.upper()
    for suffix, partner in sorted(pairs.items(), key=lambda kv: -len(kv[0])):
        if upper.endswith(suffix):
            return name[: -len(suffix)] + partner
    return ""


def guess_voltage(name: str) -> str | None:
    upper = (name or "").upper()
    for pat, val in [
        ("-15V", "-15V"), ("-12V", "-12V"), ("-5V", "-5V"),
        ("48V", "48V"), ("POE", "48V"), ("28V", "28V"), ("24V", "24V"),
        ("15V", "15V"), ("14V", "14V"), ("12V", "12V"),
        ("3V3", "3.3V"), ("3.3", "3.3V"), ("5V0", "5V"), ("+5V", "5V"),
        ("1V8", "1.8V"), ("1.8", "1.8V"), ("1V2", "1.2V"), ("1.2", "1.2V"),
        ("2V5", "2.5V"), ("2.5", "2.5V"), ("VBUS", "5V"), ("VBAT", "3.7V"),
    ]:
        if pat in upper:
            return val
    return None


def classify_component(ref: str, desc: str = "") -> str:
    if not ref:
        return "unknown"
    u = desc.upper()
    if ref.startswith("FB"):
        return "ferrite_bead"
    if ref.startswith("TP"):
        return "test_point"
    if ref.startswith("SW"):
        return "switch"
    if ref.startswith("BT"):
        return "battery"
    if ref.startswith("MH"):
        return "mounting_hole"
    c = ref[0].upper()
    return {
        "U": "IC", "C": "capacitor", "R": "resistor", "L": "inductor",
        "Q": "transistor", "J": "connector", "F": "fuse", "T": "transformer",
        "K": "relay", "X": "crystal", "Y": "crystal",
    }.get(c, "LED" if c == "D" and "LED" in u else "TVS" if c == "D" and ("TVS" in u or "ESD" in u) else "diode" if c == "D" else "other")


def guess_diff_interface(name: str) -> str:
    upper = name.upper()
    for pat, iface in (("USB", "USB"), ("ETH", "Ethernet"), ("MDIO", "Ethernet"), ("HDMI", "HDMI"), ("LVDS", "LVDS"), ("CAN", "CAN"), ("RS485", "RS-485"), ("RS-485", "RS-485"), ("PCIE", "PCIe"), ("PCI", "PCIe"), ("SATA", "SATA"), ("MIPI", "MIPI")):
        if pat in upper:
            return iface
    return "unknown"


def parse_components(root: ET.Element) -> dict[str, dict[str, Any]]:
    comps: dict[str, dict[str, Any]] = {}
    for e in root.iter():
        tag = strip_ns(e.tag).lower()
        if tag not in {"component", "comp", "componentref", "componentinstance", "package"}:
            continue
        ref = attr_any(e, "refDes", "refdes", "referenceDesignator", "designator", "name", default="")
        if not re.match(r"^[A-Z]{1,3}\d+[A-Z]?$", ref or ""):
            continue
        value = attr_any(e, "value", "partValue", default="") or child_text_any(e, "value")
        package = attr_any(e, "package", "packageRef", "footprint", "footprintRef", "pkgRef", "part", default="")
        desc = attr_any(e, "description", "desc", "partDescription", default="") or child_text_any(e, "description", "desc")
        x = float_any(e, "x", "xLoc", "locationX", "posX", default=0.0)
        y = float_any(e, "y", "yLoc", "locationY", "posY", default=0.0)
        rot = float_any(e, "rotation", "rot", "angle", default=0.0)
        side_raw = attr_any(e, "side", "mountSide", "layerRef", "layer", default="")
        side = "bottom" if side_raw.lower().startswith(("b", "bottom", "bot")) else "top"
        comps[ref] = {
            "ref": ref,
            "value": value,
            "package": package,
            "device": package or desc or ref,
            "lib_id": package or desc or ref,
            "description": desc,
            "populate": True,
            "type": classify_component(ref, desc),
            "attributes": {"ipc2581_side_raw": side_raw} if side_raw else {},
            "x_mm": round(x, 4),
            "y_mm": round(y, 4),
            "rotation": round(rot, 1),
            "side": side,
            "pads": [],
        }
    return comps


def parse_nets_and_pins(root: ET.Element) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, str]]]]:
    """Return ThomsonLint-style schematic nets and net->pin map.

    Handles common IPC-2581 structures loosely:
    - Net / ElectricalNet / NetList nodes with name attributes
    - child PinRef / Pin / Connect / Node nodes with component/pin refs
    """
    nets: list[dict[str, Any]] = []
    net_pins: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen = set()
    net_tags = {"net", "electricalnet", "logicalnet"}
    pin_tags = {"pinref", "pin", "connect", "connection", "node", "padref", "landpatternref"}

    for net_elem in root.iter():
        if strip_ns(net_elem.tag).lower() not in net_tags:
            continue
        net_name = attr_any(net_elem, "name", "netName", "net", "signalName", default="")
        if not net_name or net_name in seen:
            continue
        seen.add(net_name)
        pins = []
        for p in net_elem.iter():
            if p is net_elem or strip_ns(p.tag).lower() not in pin_tags:
                continue
            ref = attr_any(p, "componentRef", "component", "refDes", "refdes", "designator", "part", default="")
            pin = attr_any(p, "pin", "pinRef", "pinNumber", "pad", "padRef", "terminal", "name", default="")
            if ref and pin and re.match(r"^[A-Z]{1,3}\d+[A-Z]?$", ref):
                pins.append({"part": ref, "pin": pin, "direction": "UNK"})
        net_pins[net_name].extend(pins)
        diff_member = is_diff_pair_member(net_name)
        nets.append({
            "name": net_name,
            "class": "Default",
            "is_power": is_power_net(net_name),
            "is_ground": is_ground_net(net_name),
            "is_clock": is_clock_net(net_name),
            "is_differential": diff_member != 0,
            "diff_pair_partner": find_diff_partner(net_name) if diff_member else None,
            "voltage_guess": guess_voltage(net_name),
            "pins": pins,
        })
    return nets, dict(net_pins)


def attach_pads_from_net_pins(components: dict[str, dict[str, Any]], net_pins: dict[str, list[dict[str, str]]]) -> None:
    for net_name, pins in net_pins.items():
        for p in pins:
            ref = p.get("part", "")
            pin = p.get("pin", "")
            if ref in components:
                components[ref].setdefault("pads", []).append({
                    "name": pin,
                    "x_mm": components[ref].get("x_mm", 0.0),
                    "y_mm": components[ref].get("y_mm", 0.0),
                    "net_name": net_name,
                })


def parse_layers(root: ET.Element) -> list[dict[str, Any]]:
    layers = []
    seen = set()
    for e in root.iter():
        tag = strip_ns(e.tag).lower()
        if tag not in {"layer", "stackuplayer", "conductorlayer", "dielectriclayer"}:
            continue
        name = attr_any(e, "name", "layerName", "id", "layerRef", default="")
        if not name or name in seen:
            continue
        seen.add(name)
        layer_type = attr_any(e, "type", "layerType", "function", default="")
        is_copper = any(s in (name + " " + layer_type).lower() for s in ["cu", "signal", "plane", "conductor", "power", "mixed"])
        layers.append({
            "number": len(layers) + 1,
            "name": name,
            "type": layer_type or ("signal" if is_copper else "dielectric"),
            "material": attr_any(e, "material", "materialRef", default=""),
            "thickness_mm": float_any(e, "thickness", "thick", default=0.0),
            "is_copper": is_copper,
        })
    return layers


def parse_outline(root: ET.Element) -> dict[str, Any] | None:
    xs, ys = [], []
    for e in root.iter():
        for xk, yk in [("x", "y"), ("xLoc", "yLoc"), ("locationX", "locationY")]:
            xv = attr_any(e, xk, default="")
            yv = attr_any(e, yk, default="")
            if xv and yv:
                try:
                    xs.append(float(xv)); ys.append(float(yv))
                except Exception:
                    pass
    if not xs or not ys:
        return None
    return {
        "x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys),
        "width_mm": round(max(xs) - min(xs), 4),
        "height_mm": round(max(ys) - min(ys), 4),
    }


def parse_traces_and_vias(root: ET.Element, net_names: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    segments = []
    vias = []
    for e in root.iter():
        tag = strip_ns(e.tag).lower()
        if tag in {"via", "viapadstack", "blindvia", "buriedvia"}:
            net = attr_any(e, "net", "netName", "signalName", default="")
            vias.append({
                "x_mm": float_any(e, "x", "xLoc", "locationX", default=0.0),
                "y_mm": float_any(e, "y", "yLoc", "locationY", default=0.0),
                "size": float_any(e, "diameter", "size", default=0.0),
                "drill": float_any(e, "drill", "hole", "holeDiameter", default=0.0),
                "net_name": net,
                "layers": [],
            })
        elif tag in {"trace", "segment", "line", "polyline"}:
            net = attr_any(e, "net", "netName", "signalName", default="")
            if not net and net_names:
                continue
            x1 = float_any(e, "x1", "startX", default=0.0)
            y1 = float_any(e, "y1", "startY", default=0.0)
            x2 = float_any(e, "x2", "endX", default=0.0)
            y2 = float_any(e, "y2", "endY", default=0.0)
            length = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            segments.append({
                "start": [x1, y1], "end": [x2, y2],
                "width": float_any(e, "width", "lineWidth", default=0.0),
                "layer": attr_any(e, "layer", "layerRef", default=""),
                "net_name": net,
                "length": length,
            })
    return segments, vias


def signal_stats(nets: list[dict[str, Any]], segments: list[dict[str, Any]], vias: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seg_by_net = defaultdict(list)
    for s in segments:
        seg_by_net[s.get("net_name", "")].append(s)
    via_by_net = defaultdict(int)
    for v in vias:
        via_by_net[v.get("net_name", "")] += 1
    out = []
    for n in nets:
        name = n["name"]
        segs = seg_by_net.get(name, [])
        widths = [s.get("width", 0.0) for s in segs if s.get("width", 0.0)]
        entry = {
            "name": name,
            "is_power": n.get("is_power", False),
            "is_ground": n.get("is_ground", False),
            "is_clock": n.get("is_clock", False),
            "trace_length_mm": round(sum(s.get("length", 0.0) for s in segs), 4),
            "min_width_mm": round(min(widths), 4) if widths else 0.0,
            "max_width_mm": round(max(widths), 4) if widths else 0.0,
            "via_count": via_by_net.get(name, 0),
            "segment_count": len(segs),
        }
        if is_clock_net(name) or is_diff_pair_member(name):
            entry["trace_segments"] = [
                {
                    "layer": s.get("layer", ""),
                    "x1_mm": round(s["start"][0], 4), "y1_mm": round(s["start"][1], 4),
                    "x2_mm": round(s["end"][0], 4), "y2_mm": round(s["end"][1], 4),
                    "width_mm": round(s.get("width", 0.0), 4),
                } for s in segs
            ]
        out.append(entry)
    return out


def build_analysis(nets: list[dict[str, Any]]) -> dict[str, Any]:
    diff_pairs = []
    seen = set()
    for n in nets:
        name = n["name"]
        if is_diff_pair_member(name) == 1 and name not in seen:
            partner = find_diff_partner(name)
            seen.add(name); seen.add(partner)
            diff_pairs.append({"positive": name, "negative": partner, "interface": guess_diff_interface(name)})
    return {
        "power_nets": [n["name"] for n in nets if n.get("is_power")],
        "ground_nets": [n["name"] for n in nets if n.get("is_ground")],
        "differential_pairs": diff_pairs,
        "clock_nets": [n["name"] for n in nets if n.get("is_clock")],
        "floating_inputs": [],
        "single_pin_nets": [n["name"] for n in nets if len(n.get("pins", [])) == 1],
        "adapter_limitations": [
            "Schematic pin electrical directions are marked UNK because IPC-2581 usually does not preserve schematic symbol electrical types.",
            "Board geometry/routing extraction is best-effort and depends on Altium IPC-2581 attribute names.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ipc2581", help="Path to IPC-2581 XML file exported from Altium")
    ap.add_argument("--project", "-p", default=None, help="Project/base name for output files")
    ap.add_argument("--output", "-o", default="exports", help="Output directory")
    args = ap.parse_args()

    src = Path(args.ipc2581).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    project = args.project or src.stem

    try:
        tree = ET.parse(src)
    except ET.ParseError as e:
        print(f"ERROR: failed to parse XML: {e}", file=sys.stderr)
        return 2
    root = tree.getroot()

    export_date = datetime.now(timezone.utc).isoformat()
    components = parse_components(root)
    nets, net_pins = parse_nets_and_pins(root)
    attach_pads_from_net_pins(components, net_pins)
    layers = parse_layers(root)
    outline = parse_outline(root)
    net_names = {n["name"] for n in nets}
    segments, vias = parse_traces_and_vias(root, net_names)

    sch_json = {
        "thomsonlint_version": VERSION,
        "export_date": export_date,
        "mode": "schematic",
        "project": {"name": project, "variant": "", "sheets_count": 0, "source_format": "IPC-2581"},
        "components": [
            {k: v for k, v in c.items() if k not in {"x_mm", "y_mm", "rotation", "side", "pads"}}
            for c in sorted(components.values(), key=lambda x: x["ref"])
        ],
        "nets": nets,
        "analysis": build_analysis(nets),
    }

    board_components = []
    for c in sorted(components.values(), key=lambda x: x["ref"]):
        board_components.append({
            "ref": c["ref"], "package": c.get("package", ""), "value": c.get("value", ""),
            "x_mm": c.get("x_mm", 0.0), "y_mm": c.get("y_mm", 0.0),
            "rotation": c.get("rotation", 0.0), "side": c.get("side", "top"),
            "pads": [{"name": p.get("name", ""), "x_mm": p.get("x_mm", 0.0), "y_mm": p.get("y_mm", 0.0)} for p in c.get("pads", [])],
        })

    brd_json = {
        "thomsonlint_version": VERSION,
        "export_date": export_date,
        "mode": "board",
        "components": board_components,
        "board": {
            "area": {
                "width_mm": outline["width_mm"] if outline else 0,
                "height_mm": outline["height_mm"] if outline else 0,
                "x1_mm": round(outline["x1"], 4) if outline else 0,
                "y1_mm": round(outline["y1"], 4) if outline else 0,
                "x2_mm": round(outline["x2"], 4) if outline else 0,
                "y2_mm": round(outline["y2"], 4) if outline else 0,
            },
            "layers_used": [{"number": l["number"], "name": l["name"]} for l in layers if l.get("is_copper")],
            "layer_count": len([l for l in layers if l.get("is_copper")]),
            "holes": [],
            "polygons": [],
        },
        "signals": signal_stats(nets, segments, vias),
        "analysis": {
            "component_edge_distances": [],
            "decoupling_proximity": [],
            "ground_plane_layers": [l["name"] for l in layers if "gnd" in l["name"].lower() or "ground" in l["name"].lower()],
            "adapter_limitations": [
                "Component edge distances and decoupling proximity need reliable placement/pad coordinates from the IPC-2581 export; verify before relying on them.",
                "If routing segments are empty, the Altium IPC-2581 geometry tags need a project-specific extraction rule.",
            ],
        },
    }

    stack_json = {
        "thomsonlint_version": VERSION,
        "export_date": export_date,
        "mode": "stackup",
        "project": {"name": project, "source_format": "IPC-2581"},
        "layers": layers,
        "copper_layers": [l for l in layers if l.get("is_copper")],
    }

    paths = {
        "schematic": out_dir / f"{project}-thomson-export-sch.json",
        "board": out_dir / f"{project}-thomson-export-brd.json",
        "stack": out_dir / f"{project}-thomson-export-stack.json",
    }
    paths["schematic"].write_text(json.dumps(sch_json, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["board"].write_text(json.dumps(brd_json, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["stack"].write_text(json.dumps(stack_json, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {paths['schematic']}")
    print(f"Wrote {paths['board']}")
    print(f"Wrote {paths['stack']}")
    print(f"Summary: components={len(components)} nets={len(nets)} layers={len(layers)} segments={len(segments)} vias={len(vias)}")
    if len(nets) == 0:
        print("WARNING: no nets found. The parser likely needs tag/attribute tuning for this Altium IPC-2581 export.", file=sys.stderr)
    if len(components) == 0:
        print("WARNING: no components found. The parser likely needs tag/attribute tuning for this Altium IPC-2581 export.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
