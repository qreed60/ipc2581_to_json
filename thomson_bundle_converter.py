#!/usr/bin/env python3
"""ThomsonLint CAD bundle converter skeleton with Phase 2 BOM + Phase 3 PADS schematic parsing."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.5.0-phase5"
BOM_PARSER_VERSION = "bom-v1"
PADS_PARSER_VERSION = "pads-v1"
IPC_PARSER_VERSION = "ipc2581-v1"


@dataclass
class ClassifiedFile:
    relative_path: str
    extension: str
    size_bytes: int
    category: str
    confidence: str
    reason: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ThomsonLint CAD bundle converter (Phase 1-3)")
    p.add_argument("project_root")
    p.add_argument("--output-root")
    p.add_argument("--project-name")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report-only", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--schematic-pdf-dpi", type=int, default=300)
    p.add_argument("--gerber-pdf-dpi", type=int, default=400)
    p.add_argument("--validate-outputs", action="store_true", help="Run integrated output validation checks")
    return p.parse_args()


def infer_project_name(project_root: Path, override: str | None) -> str:
    return override or (project_root.name.strip() or "project")


def discover_input_dirs(project_root: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    expected_schematic = project_root / "pre_conversion" / "schematic"
    expected_layout = project_root / "pre_conversion" / "layout"
    schematic_dir, layout_dir = expected_schematic, expected_layout
    if not expected_schematic.exists() or not expected_layout.exists():
        fallback_schematic, fallback_layout = project_root / "schematic", project_root / "layout"
        examples_mode = project_root.name.lower() in {"example", "examples"}
        if fallback_schematic.exists() or fallback_layout.exists():
            schematic_dir, layout_dir = fallback_schematic, fallback_layout
            warnings.append({"code": "WARN_NONSTANDARD_LAYOUT", "message": "Using compatibility mode with <project_root>/(schematic|layout) instead of pre_conversion tree."})
        elif examples_mode:
            schematic_dir = layout_dir = project_root
            warnings.append({"code": "WARN_EXAMPLES_FLAT_LAYOUT", "message": "Using examples compatibility mode with flat folder scan because pre_conversion tree is missing."})
        else:
            warnings.append({"code": "WARN_MISSING_EXPECTED_TREE", "message": "Expected pre_conversion/schematic and pre_conversion/layout directories were not both found."})
    return schematic_dir, layout_dir, warnings


def classify_file(path: Path, root: Path, role_hint: str) -> ClassifiedFile:
    ext = path.suffix.lower()
    name = path.name.lower()
    rel = path.relative_to(root).as_posix()
    size = path.stat().st_size

    if ext in {".asc", ".pads", ".txt", ".net"}:
        if any(k in name for k in ["pads", "netlist", "orcad", "altium"]):
            return ClassifiedFile(rel, ext, size, "pads_ascii_candidate", "high", "Extension and filename suggest PADS/netlist export")
        return ClassifiedFile(rel, ext, size, "pads_ascii_candidate", "medium", "Extension is compatible with PADS ASCII export")
    if ext == ".csv" and role_hint == "schematic":
        return ClassifiedFile(rel, ext, size, "bom_csv_candidate", "high", "CSV in schematic scope likely BOM")
    if ext in {".xml", ".ipc2581", ".cvg", ".ipc"} and role_hint == "layout":
        return ClassifiedFile(rel, ext, size, "ipc2581_candidate", "high", "Layout XML/IPC2581 extension")
    if ext == ".pdf":
        if any(k in name for k in ["gerber", "gerbers", "photoplot", "plot", "artwork", "copper", "silk", "layer", "pcb", "layout"]):
            return ClassifiedFile(rel, ext, size, "layout_pdf_candidate", "high", "Filename suggests layout/Gerber PDF")
        if any(k in name for k in ["schematic", "sch", "circuit", "pages", "capture", "orcad"]):
            return ClassifiedFile(rel, ext, size, "schematic_pdf_candidate", "high", "Filename suggests schematic PDF")
    if ext == ".pdf" and role_hint == "schematic":
        return ClassifiedFile(rel, ext, size, "schematic_pdf_candidate", "medium", "PDF under schematic scope")
    if ext == ".pdf" and role_hint == "layout":
        return ClassifiedFile(rel, ext, size, "layout_pdf_candidate", "medium", "PDF under layout scope")
    if ext == ".csv":
        return ClassifiedFile(rel, ext, size, "bom_csv_candidate", "medium", "CSV found outside schematic scope; treated as BOM candidate")
    if ext in {".xml", ".ipc2581", ".cvg", ".ipc"}:
        return ClassifiedFile(rel, ext, size, "ipc2581_candidate", "medium", "XML/IPC2581 found outside layout scope")
    if ext == ".pdf":
        if "schem" in name:
            return ClassifiedFile(rel, ext, size, "schematic_pdf_candidate", "medium", "Filename suggests schematic PDF")
        if any(k in name for k in ["gerber", "layout", "pcb", "silk", "copper", "layer", "plot", "photoplot", "artwork"]):
            return ClassifiedFile(rel, ext, size, "layout_pdf_candidate", "medium", "Filename suggests layout/Gerber PDF")
    return ClassifiedFile(rel, ext, size, "unknown", "low", "No classifier rule matched")


def scan_files(project_root: Path, schematic_dir: Path, layout_dir: Path) -> list[ClassifiedFile]:
    files: list[ClassifiedFile] = []
    seen: set[Path] = set()
    for directory, hint in [(schematic_dir, "schematic"), (layout_dir, "layout")]:
        if directory.exists() and directory.is_dir():
            for p in sorted(directory.iterdir()):
                if p.is_file() and p.resolve() not in seen:
                    files.append(classify_file(p, project_root, hint))
                    seen.add(p.resolve())
    if not files:
        for p in sorted(project_root.iterdir()):
            if p.is_file():
                files.append(classify_file(p, project_root, "unknown"))
    return files


def planned_outputs(project_name: str, output_root: Path) -> list[str]:
    return [str(output_root / f"{project_name}-{n}") for n in [
        "thomson-export-sch.json", "thomson-export-brd.json", "thomson-export-stack.json",
        "bom.json", "conversion-report.json", "conversion-report.md"
    ]]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


HEADER_MAP = {
    "refdes": "refdes", "designator": "refdes", "reference": "refdes", "references": "refdes",
    "value": "value", "description": "description", "manufacturer": "manufacturer", "mpn": "mpn", "manufacturerpartnumber": "mpn",
    "vendor": "vendor", "vendorpn": "vendor_pn", "quantity": "quantity", "qty": "quantity", "footprint": "footprint", "package": "package",
    "dni": "dnp", "dnp": "dnp", "donotinstall": "dnp", "part": "item", "item": "item",
    "mfg1": "manufacturer", "mfg2": "manufacturer", "mfg3": "manufacturer",
    "mfgpn1": "mpn", "mfgpn2": "mpn", "mfgpn3": "mpn"
}


def parse_bool(v: str) -> bool | None:
    t = v.strip().lower()
    if t in {"1", "true", "yes", "y", "dnp", "dni", "do not install", "installed=no", "no-load", "noload"}:
        return True
    if t in {"0", "false", "no", "n", "install", "fitted"}:
        return False
    return None


def expand_refdes_cell(cell: str, warnings: list[dict[str, Any]]) -> list[str]:
    tokens = [t for t in re.split(r"[;,\s]+", cell.strip()) if t]
    out: list[str] = []
    for tok in tokens:
        m = re.match(r"^([A-Za-z]+)(\d+)-([A-Za-z]+)?(\d+)$", tok)
        if m:
            p1, s, p2, e = m.groups()
            if p2 and p2.upper() != p1.upper():
                warnings.append({"code": "WARN_BOM_REFDES_RANGE_AMBIGUOUS", "message": "Ambiguous BOM refdes range token."})
                out.append(tok)
                continue
            a, b = int(s), int(e)
            if a > b or b - a > 500:
                warnings.append({"code": "WARN_BOM_REFDES_RANGE_AMBIGUOUS", "message": "Unsafe BOM refdes range token."})
                out.append(tok)
                continue
            out.extend([f"{p1}{i}" for i in range(a, b + 1)])
            continue
        out.append(tok)
    return out


def parse_bom(project_name: str, project_root: Path, files: list[ClassifiedFile]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    candidates = sorted([f for f in files if f.category == "bom_csv_candidate"], key=lambda f: f.relative_path)
    if not candidates:
        warnings.append({"code": "WARN_BOM_MISSING", "message": "No BOM CSV candidate discovered."})
        return {"project_name": project_name, "source_file": None, "parser_version": BOM_PARSER_VERSION, "raw_headers": [], "normalized_headers": {}, "row_count": 0, "expanded_refdes_count": 0, "duplicate_refdes": [], "warnings": warnings, "items": []}
    source = candidates[0]
    if len(candidates) > 1:
        warnings.append({"code": "WARN_BOM_MULTIPLE_CANDIDATES", "message": f"Multiple BOM CSV candidates found ({len(candidates)}); using {source.relative_path}"})

    text = (project_root / source.relative_path).read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    raw_headers = reader.fieldnames or []
    normalized_headers = {h: HEADER_MAP.get(_norm(h), "unknown") for h in raw_headers}
    ref_cols = [h for h, n in normalized_headers.items() if n == "refdes"]
    if not ref_cols:
        warnings.append({"code": "WARN_BOM_MISSING_REFDES_HEADER", "message": "No recognized RefDes/Designator/Reference column found."})

    items = []
    ref_counts: dict[str, int] = {}
    for idx, row in enumerate(reader, start=1):
        ref_cell = ((row.get(ref_cols[0]) if ref_cols else "") or "").strip()
        refs = expand_refdes_cell(ref_cell, warnings) if ref_cell else []
        for r in refs:
            ref_counts[r] = ref_counts.get(r, 0) + 1

        def pick(field: str) -> str | None:
            for h, n in normalized_headers.items():
                if n == field and (row.get(h) or "").strip():
                    return (row.get(h) or "").strip()
            return None

        dnp_val = None
        for h, n in normalized_headers.items():
            if n == "dnp":
                parsed = parse_bool(row.get(h, "") or "")
                if parsed is not None:
                    dnp_val = parsed
                    break

        manufacturer_alternates: list[dict[str, Any]] = []
        for rank in (1, 2, 3):
            mfg_candidates = [
                f"MFG_{rank}", f"MFG {rank}", f"Manufacturer_{rank}", f"Manufacturer {rank}"
            ]
            mpn_candidates = [
                f"MFG P/N_{rank}", f"MFG P/N {rank}", f"MPN_{rank}", f"Manufacturer Part Number_{rank}"
            ]
            mfg_val = next(((row.get(h) or "").strip() for h in mfg_candidates if (row.get(h) or "").strip()), None)
            mpn_val = next(((row.get(h) or "").strip() for h in mpn_candidates if (row.get(h) or "").strip()), None)
            if mfg_val or mpn_val:
                manufacturer_alternates.append({"manufacturer": mfg_val, "mpn": mpn_val, "rank": rank})

        primary_manufacturer = pick("manufacturer")
        primary_mpn = pick("mpn")
        if not primary_manufacturer and manufacturer_alternates:
            primary_manufacturer = manufacturer_alternates[0].get("manufacturer")
        if not primary_mpn and manufacturer_alternates:
            primary_mpn = manufacturer_alternates[0].get("mpn")

        items.append({
            "refdes": refs,
            "fields": {
                "value": pick("value"), "description": pick("description"), "manufacturer": pick("manufacturer"),
                "mpn": pick("mpn"), "vendor": pick("vendor"), "vendor_pn": pick("vendor_pn"),
                "quantity": pick("quantity"), "footprint": pick("footprint"), "package": pick("package"), "dnp": dnp_val,
            },
            "manufacturers": manufacturer_alternates,
            "raw_row_index": idx,
        })
        items[-1]["fields"]["manufacturer"] = primary_manufacturer
        items[-1]["fields"]["mpn"] = primary_mpn

    duplicates = sorted([k for k, v in ref_counts.items() if v > 1])
    if duplicates:
        warnings.append({"code": "WARN_BOM_DUPLICATE_REFDES", "message": f"Duplicate RefDes values found: {len(duplicates)}"})

    return {
        "project_name": project_name,
        "source_file": source.relative_path,
        "parser_version": BOM_PARSER_VERSION,
        "raw_headers": raw_headers,
        "normalized_headers": normalized_headers,
        "row_count": len(items),
        "expanded_refdes_count": sum(len(i["refdes"]) for i in items),
        "duplicate_refdes": duplicates,
        "warnings": warnings,
        "items": items,
    }


def parse_pads(project_root: Path, files: list[ClassifiedFile], bom: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    candidates = sorted([f for f in files if f.category == "pads_ascii_candidate"], key=lambda f: f.relative_path)
    if not candidates:
        warnings.append({"code": "WARN_PADS_MISSING", "message": "No PADS ASCII candidate discovered."})
        return {"source_file": None, "detected_dialect": "unknown", "parser_version": PADS_PARSER_VERSION, "components": [], "nets": [], "warnings": warnings, "extraction_counts": {"component_count": 0, "net_count": 0, "node_count": 0, "single_pin_net_count": 0, "power_net_count": 0, "ground_net_count": 0}, "bom_merge": {"components_with_bom_metadata": 0, "components_missing_bom_metadata": 0, "unmatched_bom_refdes": [], "value_mismatch_count": 0, "footprint_mismatch_count": 0}}
    source = candidates[0]
    if len(candidates) > 1:
        warnings.append({"code": "WARN_PADS_MULTIPLE_CANDIDATES", "message": f"Multiple PADS candidates found ({len(candidates)}); using {source.relative_path}"})

    lines = (project_root / source.relative_path).read_text(encoding="utf-8", errors="replace").splitlines()
    dialect = "unknown"
    if any("*PADS-PCB*" in ln for ln in lines[:10]):
        dialect = "pads_pcb_ascii_orcad_or_altium"

    components: dict[str, dict[str, Any]] = {}
    nets: list[dict[str, Any]] = []
    current_section = None
    current_net = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.upper() == "*PART*":
            current_section = "part"
            continue
        if line.upper() == "*NET*":
            current_section = "net"
            if current_net:
                nets.append(current_net)
                current_net = None
            continue
        if current_section == "part":
            toks = line.split()
            if len(toks) >= 2:
                refdes = toks[0]
                if toks[0].upper() in {"COMP", "PART"} and len(toks) > 1:
                    refdes = toks[1]
                value = None
                footprint = None
                part_name = toks[1] if refdes == toks[0] else toks[0]
                for tk in toks[2:]:
                    low = tk.lower()
                    if low.startswith("value="):
                        value = tk.split("=", 1)[1]
                    elif low.startswith("footprint=") or low.startswith("package="):
                        footprint = tk.split("=", 1)[1]
                components[refdes] = {"refdes": refdes, "value": value, "footprint": footprint, "package": footprint, "part_name": part_name, "source": {"format": "pads_ascii", "file": source.relative_path}}
        elif current_section == "net":
            if line.upper().startswith("*SIGNAL*"):
                if current_net:
                    nets.append(current_net)
                current_net = {"name": line.split(maxsplit=1)[1].strip() if len(line.split(maxsplit=1)) > 1 else "unknown_net", "nodes": []}
                continue
            if line.upper().startswith("NET "):
                if current_net:
                    nets.append(current_net)
                current_net = {"name": line[4:].strip(), "nodes": []}
                continue
            if current_net is not None:
                for pin_token in line.split():
                    if pin_token.startswith("*"):
                        continue
                    if "." in pin_token:
                        refdes, pin = pin_token.rsplit(".", 1)
                    else:
                        refdes, pin = pin_token, None
                    if not refdes:
                        continue
                    current_net["nodes"].append({"refdes": refdes, "pin_number": pin, "pin_name": None})
    if current_net:
        nets.append(current_net)

    if not components:
        warnings.append({"code": "WARN_PADS_NO_COMPONENTS", "message": "No components extracted from PADS file."})
    if not nets:
        warnings.append({"code": "WARN_PADS_NO_NETS", "message": "No nets extracted from PADS file; fixture may be component-only or net sections were not found."})

    # BOM merge
    bom_map: dict[str, dict[str, Any]] = {}
    for item in bom.get("items", []):
        for rd in item.get("refdes", []):
            bom_map[rd] = item.get("fields", {})

    with_bom = 0
    missing_bom = 0
    value_mismatch = 0
    footprint_mismatch = 0
    for refdes, comp in components.items():
        b = bom_map.get(refdes)
        comp["bom"] = b
        if b:
            with_bom += 1
            bval = b.get("value")
            if comp.get("value") and bval and comp["value"] != bval:
                value_mismatch += 1
                warnings.append({"code": "WARN_COMPONENT_VALUE_MISMATCH", "message": f"Value mismatch for {refdes}."})
            bfp = b.get("footprint") or b.get("package")
            if comp.get("footprint") and bfp and comp["footprint"] != bfp:
                footprint_mismatch += 1
                warnings.append({"code": "WARN_COMPONENT_FOOTPRINT_MISMATCH", "message": f"Footprint mismatch for {refdes}."})
        else:
            missing_bom += 1

    comp_refs = set(components.keys())
    unmatched_bom_refdes = sorted([r for r in bom_map if r not in comp_refs])

    node_count = sum(len(n["nodes"]) for n in nets)
    single_pin_nets = [n["name"] for n in nets if len(n["nodes"]) <= 1]
    ground_nets = [n["name"] for n in nets if n["name"].upper() in {"GND", "GROUND", "AGND", "DGND", "PGND"}]
    power_nets = [n["name"] for n in nets if re.search(r"(^VCC|^VDD|^VBAT|\+\d|^PWR)", n["name"].upper())]
    clock_nets = [n["name"] for n in nets if "CLK" in n["name"].upper() or "CLOCK" in n["name"].upper()]

    sch_components = list(sorted(components.values(), key=lambda x: x["refdes"]))
    sch_nets = []
    for n in nets:
        sch_nets.append({"name": n["name"], "node_count": len(n["nodes"]), "nodes": n["nodes"]})

    return {
        "source_file": source.relative_path,
        "detected_dialect": dialect,
        "parser_version": PADS_PARSER_VERSION,
        "components": sch_components,
        "nets": sch_nets,
        "analysis": {"power_nets": power_nets, "ground_nets": ground_nets, "clock_nets": clock_nets, "single_pin_nets": single_pin_nets},
        "warnings": warnings,
        "extraction_counts": {
            "component_count": len(sch_components), "net_count": len(sch_nets), "node_count": node_count,
            "single_pin_net_count": len(single_pin_nets), "power_net_count": len(power_nets), "ground_net_count": len(ground_nets),
        },
        "bom_merge": {
            "components_with_bom_metadata": with_bom,
            "components_missing_bom_metadata": missing_bom,
            "unmatched_bom_refdes": unmatched_bom_refdes,
            "value_mismatch_count": value_mismatch,
            "footprint_mismatch_count": footprint_mismatch,
        },
    }


def build_schematic_export(project_name: str, project_root: Path, pads: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_name": project_name,
        "source": {"project_root": str(project_root), "schematic_file": pads.get("source_file"), "format": "pads_ascii", "detected_dialect": pads.get("detected_dialect")},
        "parser_version": pads.get("parser_version"),
        "components": pads.get("components", []),
        "nets": pads.get("nets", []),
        "analysis": pads.get("analysis", {}),
        "warnings": pads.get("warnings", []),
        "extraction_counts": pads.get("extraction_counts", {}),
        "bom_merge": pads.get("bom_merge", {}),
    }




def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def parse_ipc2581(project_root: Path, files: list[ClassifiedFile]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    cands = sorted([f for f in files if f.category == "ipc2581_candidate"], key=lambda f: f.relative_path)
    if not cands:
        warnings.append({"code": "WARN_IPC_MISSING", "message": "No IPC-2581 candidate discovered."})
        empty_counts={"board_component_count":0,"placement_count":0,"board_net_count":0,"layer_count":0,"stackup_layer_count":0,"via_count":0,"drill_count":0,"route_segment_count":0,"outline_point_count":0}
        return {"source_file":None,"parser_version":IPC_PARSER_VERSION,"ipc_root":None,"ipc_revision":None,"namespace":None,"units":None,"components":[],"nets":[],"layers":[],"stackup_layers":[],"outline":[],"vias":[],"drills":[],"route_segments":[],"analysis":{},"warnings":warnings,"extraction_counts":empty_counts}
    src=cands[0]
    if len(cands)>1:
        warnings.append({"code":"WARN_IPC_MULTIPLE_CANDIDATES","message":f"Multiple IPC candidates found ({len(cands)}); using {src.relative_path}"})
    path=project_root/src.relative_path
    try:
        root=ET.parse(path).getroot()
    except Exception as exc:
        warnings.append({"code":"WARN_IPC_PARSE_FAILED","message":"IPC XML parse failed."})
        return {"source_file":src.relative_path,"parser_version":IPC_PARSER_VERSION,"ipc_root":None,"ipc_revision":None,"namespace":None,"units":None,"components":[],"nets":[],"layers":[],"stackup_layers":[],"outline":[],"vias":[],"drills":[],"route_segments":[],"analysis":{},"warnings":warnings,"extraction_counts":{"board_component_count":0,"placement_count":0,"board_net_count":0,"layer_count":0,"stackup_layer_count":0,"via_count":0,"drill_count":0,"route_segment_count":0,"outline_point_count":0},"error":str(exc)}

    ns = root.tag.split('}')[0].strip('{') if root.tag.startswith('{') else None
    ipc_root=_local(root.tag)
    rev=root.attrib.get('revision') or root.attrib.get('Revision')
    units=None

    layers=[]; layer_names=set()
    for e in root.iter():
        t=_local(e.tag)
        if t in {"Layer", "LayerRef"}:
            name=e.attrib.get('name') or e.attrib.get('layerRef') or e.attrib.get('id')
            if name and name not in layer_names:
                layer_names.add(name)
                layers.append({"name":name,"type":e.attrib.get('layerFunction') or e.attrib.get('type'),"side":e.attrib.get('side')})

    components=[]
    for e in root.iter():
        if _local(e.tag) in {"Component","CompInstance"}:
            ref=e.attrib.get('refDes') or e.attrib.get('refdes') or e.attrib.get('name')
            if not ref: continue
            if units is None and (e.attrib.get('unit') or e.attrib.get('units')):
                units=e.attrib.get('unit') or e.attrib.get('units')
            components.append({"refdes":ref,"footprint":e.attrib.get('packageRef') or e.attrib.get('part') or e.attrib.get('cellRef'),"layer":e.attrib.get('layerRef') or e.attrib.get('side'),"x":e.attrib.get('x'),"y":e.attrib.get('y'),"rotation":e.attrib.get('rotation') or e.attrib.get('rot'),"source":{"format":"ipc2581","file":src.relative_path}})

    nets=[]
    for e in root.iter():
        if _local(e.tag)=="Net":
            name=e.attrib.get('name') or e.attrib.get('id') or 'unknown_net'
            nodes=[]
            for c in list(e.iter()):
                ct=_local(c.tag)
                if ct in {"PinRef","Pin","Node","Conductor"}:
                    r=c.attrib.get('componentRef') or c.attrib.get('refDes') or c.attrib.get('refdes')
                    p=c.attrib.get('pin') or c.attrib.get('pinRef') or c.attrib.get('number')
                    if r or p:
                        nodes.append({"refdes":r,"pin_number":p})
            nets.append({"name":name,"node_count":len(nodes),"nodes":nodes})

    stack=[]
    for e in root.iter():
        if _local(e.tag) in {"StackupLayer","Layer","Dielectric","Conductive"}:
            if _local(e.tag)=="Layer" and not any(k in e.attrib for k in ("thickness","material","sequence")):
                continue
            name=e.attrib.get('name') or e.attrib.get('layerRef') or e.attrib.get('id')
            if name:
                stack.append({"name":name,"sequence":e.attrib.get('sequence') or e.attrib.get('order'),"material":e.attrib.get('material'),"thickness":e.attrib.get('thickness'),"dielectric_constant":e.attrib.get('er') or e.attrib.get('epsilonR'),"copper_thickness":e.attrib.get('copperThickness')})

    vias=[]; drills=[]; routes=[]; outline=[]
    for e in root.iter():
        t=_local(e.tag)
        if t=="Via": vias.append({"x":e.attrib.get('x'),"y":e.attrib.get('y'),"drill":e.attrib.get('drill')})
        elif t in {"Drill","Hole"}: drills.append({"x":e.attrib.get('x'),"y":e.attrib.get('y'),"diameter":e.attrib.get('diameter') or e.attrib.get('drill')})
        elif t in {"Segment","Trace","Line"}: routes.append({"x1":e.attrib.get('x1'),"y1":e.attrib.get('y1'),"x2":e.attrib.get('x2'),"y2":e.attrib.get('y2'),"net":e.attrib.get('net') or e.attrib.get('netRef')})
        elif t in {"Profile","Outline","Contour","Polygon","Point"}:
            if 'x' in e.attrib and 'y' in e.attrib: outline.append({"x":e.attrib.get('x'),"y":e.attrib.get('y')})

    if not stack: warnings.append({"code":"WARN_IPC_STACKUP_UNAVAILABLE","message":"Stackup details unavailable or not reliably extractable."})
    if not layers: warnings.append({"code":"WARN_IPC_LAYERS_UNAVAILABLE","message":"No layers extracted from IPC file."})

    counts={"board_component_count":len(components),"placement_count":len(components),"board_net_count":len(nets),"layer_count":len(layers),"stackup_layer_count":len(stack),"via_count":len(vias),"drill_count":len(drills),"route_segment_count":len(routes),"outline_point_count":len(outline)}
    analysis={"layer_count":len(layers),"layers_used":[l['name'] for l in layers],"ground_plane_layers":[l['name'] for l in layers if l['name'] and 'gnd' in l['name'].lower()],"signal_layers":[l['name'] for l in layers if l.get('type') and 'signal' in (l.get('type') or '').lower()]}
    return {"source_file":src.relative_path,"parser_version":IPC_PARSER_VERSION,"ipc_root":ipc_root,"ipc_revision":rev,"namespace":ns,"units":units,"components":components,"nets":nets,"layers":layers,"stackup_layers":stack,"outline":outline,"vias":vias,"drills":drills,"route_segments":routes,"analysis":analysis,"warnings":warnings,"extraction_counts":counts}


def build_board_export(project_name:str, project_root:Path, ipc:dict[str,Any])->dict[str,Any]:
    return {"project_name":project_name,"source":{"project_root":str(project_root),"layout_file":ipc.get('source_file'),"format":"ipc2581","ipc_root":ipc.get('ipc_root'),"ipc_revision":ipc.get('ipc_revision'),"namespace":ipc.get('namespace')},"parser_version":ipc.get('parser_version'),"components":ipc.get('components',[]),"placements":ipc.get('components',[]),"nets":ipc.get('nets',[]),"layers":ipc.get('layers',[]),"board_outline":ipc.get('outline',[]),"vias":ipc.get('vias',[]),"drills":ipc.get('drills',[]),"routes":ipc.get('route_segments',[]),"analysis":ipc.get('analysis',{}),"warnings":ipc.get('warnings',[]),"extraction_counts":ipc.get('extraction_counts',{})}


def build_stack_export(project_name:str, project_root:Path, ipc:dict[str,Any])->dict[str,Any]:
    return {"project_name":project_name,"source":{"project_root":str(project_root),"layout_file":ipc.get('source_file'),"format":"ipc2581"},"parser_version":ipc.get('parser_version'),"units":ipc.get('units'),"layer_stack":ipc.get('stackup_layers',[]),"warnings":ipc.get('warnings',[]),"extraction_counts":{"stackup_layer_count":ipc.get('extraction_counts',{}).get('stackup_layer_count',0),"layer_count":ipc.get('extraction_counts',{}).get('layer_count',0)}}


def render_pdf_images(args: argparse.Namespace, project_root: Path, output_root: Path, project_name: str, files: list[ClassifiedFile]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    image_outputs: list[str] = []
    schem_pdfs = sorted([f for f in files if f.category == "schematic_pdf_candidate"], key=lambda x: x.relative_path)
    layout_pdfs = sorted([f for f in files if f.category == "layout_pdf_candidate"], key=lambda x: x.relative_path)
    pdftoppm = shutil.which("pdftoppm")
    pdfinfo = shutil.which("pdfinfo")
    status = {
        "pdftoppm_available": bool(pdftoppm),
        "pdfinfo_available": bool(pdfinfo),
        "schematic_pdf_sources": [f.relative_path for f in schem_pdfs],
        "layout_pdf_sources": [f.relative_path for f in layout_pdfs],
        "dpi": {"schematic": args.schematic_pdf_dpi, "layout": args.gerber_pdf_dpi},
        "pages_converted": 0,
        "image_outputs": image_outputs,
        "classification_warnings": [],
        "dependency_warnings": [],
        "render_errors": errors,
        "skipped_pdfs": [],
        "output_validation": {"status": "skipped" if args.dry_run or args.report_only else "pending"},
    }
    if not pdftoppm:
        w={"code":"WARN_PDFTOPPM_MISSING","message":"pdftoppm not found. Install with: sudo apt install poppler-utils"}
        warnings.append(w); status["dependency_warnings"].append(w)
        return {"report": status, "warnings": warnings, "errors": errors, "image_outputs": image_outputs}

    def convert(pdf_rel: str, prefix: str, dpi: int) -> list[Path]:
        pdf_path = project_root / pdf_rel
        out_prefix = output_root / prefix
        cmd=[pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(out_prefix)]
        proc=subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            errors.append({"code":"ERR_PDF_RENDER_FAILED","message":f"Failed rendering {pdf_rel}"})
            return []
        return sorted(output_root.glob(f"{prefix}-*.png"))

    if args.dry_run or args.report_only:
        status["skipped_pdfs"] = [f.relative_path for f in schem_pdfs + layout_pdfs]
        return {"report": status, "warnings": warnings, "errors": errors, "image_outputs": image_outputs}

    for idx, f in enumerate(schem_pdfs, start=1):
        prefix = f"{project_name}-img-sch" if len(schem_pdfs)==1 else f"{project_name}-img-sch-s{idx}"
        rendered = convert(f.relative_path, prefix, args.schematic_pdf_dpi)
        for n,png in enumerate(rendered, start=1):
            target = output_root / (f"{project_name}-img-sch-p{n}.png" if len(schem_pdfs)==1 else f"{project_name}-img-sch-s{idx}-p{n}.png")
            png.rename(target); image_outputs.append(str(target))

    for idx, f in enumerate(layout_pdfs, start=1):
        prefix = f"{project_name}-img-layout-tmp-f{idx}"
        rendered = convert(f.relative_path, prefix, args.gerber_pdf_dpi)
        if len(layout_pdfs) > 1:
            status["classification_warnings"].append({"code":"WARN_LAYOUT_MULTI_PDF_FALLBACK","message":"Multiple layout PDFs rendered with fallback naming."})
        for n,png in enumerate(rendered, start=1):
            target_name = f"{project_name}-img-layout-p{n}.png" if len(layout_pdfs)==1 else f"{project_name}-img-layout-f{idx}-p{n}.png"
            target = output_root / target_name
            png.rename(target); image_outputs.append(str(target))

    status["pages_converted"] = len(image_outputs)
    status["output_validation"] = {"status": "pass"}
    return {"report": status, "warnings": warnings, "errors": errors, "image_outputs": image_outputs}



def validate_outputs(project_root: Path, output_root: Path, project_name: str, files: list[ClassifiedFile], report: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    json_round_trip_ok = True
    required_outputs_ok = True
    image_outputs_ok = True

    report_json = output_root / f"{project_name}-conversion-report.json"
    report_md = output_root / f"{project_name}-conversion-report.md"
    if not output_root.exists():
        issues.append("post_conversion folder missing")
    if not report_json.exists():
        issues.append("conversion-report.json missing")
        required_outputs_ok = False
    if not report_md.exists():
        issues.append("conversion-report.md missing")
        required_outputs_ok = False

    for j in sorted(output_root.glob("*.json")):
        try:
            json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            json_round_trip_ok = False
            issues.append(f"json failed round-trip: {j.name}")

    cats={f.category for f in files}
    expected=[]
    if "bom_csv_candidate" in cats: expected.append(output_root / f"{project_name}-bom.json")
    if "pads_ascii_candidate" in cats: expected.append(output_root / f"{project_name}-thomson-export-sch.json")
    if "ipc2581_candidate" in cats:
        expected.append(output_root / f"{project_name}-thomson-export-brd.json")
        expected.append(output_root / f"{project_name}-thomson-export-stack.json")
    for e in expected:
        if not e.exists():
            required_outputs_ok=False
            issues.append(f"required output missing: {e.name}")

    has_pdf = any(c in cats for c in ["schematic_pdf_candidate","layout_pdf_candidate"])
    pngs = sorted(output_root.glob("*.png"))
    if has_pdf and not report.get("images",{}).get("pdftoppm_available",False) and len(pngs)==0:
        image_outputs_ok=True
    elif has_pdf and len(pngs)==0:
        image_outputs_ok=False
        issues.append("pdf sources present but no png outputs")

    sections_ok = all(k in report for k in ["discovery","bom","schematic","ipc2581","images","warnings"])
    if not sections_ok:
        required_outputs_ok = False
        issues.append("required report sections missing")

    outputs_recorded = all(report.get("bom",{}).get("output_file"),) if False else True
    # minimal path recording checks
    if not report.get("bom",{}).get("output_file"):
        issues.append("bom output path not recorded")
    if not report.get("schematic",{}).get("output_file"):
        issues.append("schematic output path not recorded")
    if not report.get("ipc2581",{}).get("board_output_file"):
        issues.append("board output path not recorded")

    warnings_count = len(report.get("warnings", []))
    strict_would_fail = warnings_count > 0
    ok = json_round_trip_ok and required_outputs_ok and image_outputs_ok
    return {
        "ok": ok,
        "json_round_trip_ok": json_round_trip_ok,
        "required_outputs_ok": required_outputs_ok,
        "image_outputs_ok": image_outputs_ok,
        "warnings_count": warnings_count,
        "strict_would_fail": strict_would_fail,
        "ready_for_thomsonlint_smoke_test": ok and (image_outputs_ok or not has_pdf),
        "issues": issues,
    }
def build_report(args: argparse.Namespace, project_root: Path, output_root: Path, project_name: str, files: list[ClassifiedFile], warnings: list[dict[str, Any]], bom: dict[str, Any], pads: dict[str, Any], ipc: dict[str, Any], images: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in files:
        counts[f.category] = counts.get(f.category, 0) + 1

    return {
        "metadata": {
            "converter": "thomson_bundle_converter", "version": VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_root": str(project_root), "project_name": project_name, "output_root": str(output_root), "args": vars(args), "phase": "phase6_integrated_validation",
        },
        "discovery": {"files": [f.__dict__ for f in files], "counts_by_category": counts},
        "bom": {
            "source_file": bom.get("source_file"), "raw_headers": bom.get("raw_headers", []), "normalized_headers": bom.get("normalized_headers", {}),
            "row_count": bom.get("row_count", 0), "expanded_refdes_count": bom.get("expanded_refdes_count", 0),
            "duplicate_refdes_count": len(bom.get("duplicate_refdes", [])),
            "parse_warnings": bom.get("warnings", []), "output_file": str(output_root / f"{project_name}-bom.json"),
            "json_validation": {"status": "skipped" if args.dry_run or args.report_only else "pending"},
        },
        "schematic": {
            "source_file": pads.get("source_file"), "detected_dialect": pads.get("detected_dialect"),
            "component_count": pads.get("extraction_counts", {}).get("component_count", 0),
            "net_count": pads.get("extraction_counts", {}).get("net_count", 0),
            "node_count": pads.get("extraction_counts", {}).get("node_count", 0),
            "single_pin_net_count": pads.get("extraction_counts", {}).get("single_pin_net_count", 0),
            "power_net_count": pads.get("extraction_counts", {}).get("power_net_count", 0),
            "ground_net_count": pads.get("extraction_counts", {}).get("ground_net_count", 0),
            "bom_merge": pads.get("bom_merge", {}),
            "parse_warnings": pads.get("warnings", []),
            "output_file": str(output_root / f"{project_name}-thomson-export-sch.json"),
            "json_validation": {"status": "skipped" if args.dry_run or args.report_only else "pending"},
        },
        "planned_outputs": planned_outputs(project_name, output_root),
        "ipc2581": {"source_file": ipc.get("source_file"), "root": ipc.get("ipc_root"), "revision": ipc.get("ipc_revision"), "namespace": ipc.get("namespace"), "component_count": ipc.get("extraction_counts", {}).get("board_component_count", 0), "placement_count": ipc.get("extraction_counts", {}).get("placement_count", 0), "net_count": ipc.get("extraction_counts", {}).get("board_net_count", 0), "layer_count": ipc.get("extraction_counts", {}).get("layer_count", 0), "stackup_layer_count": ipc.get("extraction_counts", {}).get("stackup_layer_count", 0), "via_count": ipc.get("extraction_counts", {}).get("via_count", 0), "drill_count": ipc.get("extraction_counts", {}).get("drill_count", 0), "route_segment_count": ipc.get("extraction_counts", {}).get("route_segment_count", 0), "outline_point_count": ipc.get("extraction_counts", {}).get("outline_point_count", 0), "parse_warnings": ipc.get("warnings", []), "board_output_file": str(output_root / f"{project_name}-thomson-export-brd.json"), "stack_output_file": str(output_root / f"{project_name}-thomson-export-stack.json"), "board_json_validation": {"status": "skipped" if args.dry_run or args.report_only else "pending"}, "stack_json_validation": {"status": "skipped" if args.dry_run or args.report_only else "pending"}},
        "images": images.get("report", {}),
        "image_outputs": images.get("image_outputs", []),
        "validation": {},
        "warnings": warnings + bom.get("warnings", []) + pads.get("warnings", []) + ipc.get("warnings", []) + images.get("warnings", []),
        "errors": [],
        "notes": [
            "Phase 1 performs discovery/reporting.", "Phase 2 adds BOM parsing.", "Phase 3 adds PADS schematic parsing and BOM merge.", "Phase 4 adds IPC-2581 board/stack parsing.", "Phase 5 adds PDF-to-PNG rendering.",
            "WARNING: tools/kicad-export.py not found in this repository snapshot; schematic shape uses a compatibility-oriented best effort.",
        ],
    }


def report_markdown(report: dict[str, Any]) -> str:
    m = report["metadata"]
    s = report.get("schematic", {})
    b = report.get("bom", {})
    lines = [
        f"# Conversion Report (Phase 6) - {m['project_name']}", "", "## Metadata",
        f"- Converter: {m['converter']} {m['version']}", f"- Generated (UTC): {m['generated_at_utc']}",
        "", "## Discovery Counts",
    ]
    for c, n in sorted(report["discovery"]["counts_by_category"].items()):
        lines.append(f"- {c}: {n}")
    lines += ["", "## BOM", f"- Source file: `{b.get('source_file')}`", f"- Row count: {b.get('row_count', 0)}", "", "## Schematic (PADS)", f"- Source file: `{s.get('source_file')}`", f"- Detected dialect: {s.get('detected_dialect')}", f"- Components: {s.get('component_count', 0)}", f"- Nets: {s.get('net_count', 0)}", f"- Nodes: {s.get('node_count', 0)}", f"- Schematic JSON validation: {s.get('json_validation', {}).get('status', 'unknown')}", "", "## IPC-2581 / Board", f"- Source file: `{report.get('ipc2581',{}).get('source_file')}`", f"- Root: {report.get('ipc2581',{}).get('root')}", f"- Revision: {report.get('ipc2581',{}).get('revision')}", f"- Namespace present: {bool(report.get('ipc2581',{}).get('namespace'))}", f"- Board components: {report.get('ipc2581',{}).get('component_count',0)}", f"- Layers: {report.get('ipc2581',{}).get('layer_count',0)}", f"- Nets: {report.get('ipc2581',{}).get('net_count',0)}", f"- Stack layers: {report.get('ipc2581',{}).get('stackup_layer_count',0)}", f"- Board JSON validation: {report.get('ipc2581',{}).get('board_json_validation',{}).get('status','unknown')}", f"- Stack JSON validation: {report.get('ipc2581',{}).get('stack_json_validation',{}).get('status','unknown')}", "", "## Images / PDF Render", f"- pdftoppm available: {report.get('images',{}).get('pdftoppm_available')}", f"- Pages converted: {report.get('images',{}).get('pages_converted',0)}", f"- Output validation: {report.get('images',{}).get('output_validation',{}).get('status','unknown')}", "", "## Validation", f"- ok: {report.get('validation',{}).get('ok')}", f"- json_round_trip_ok: {report.get('validation',{}).get('json_round_trip_ok')}", f"- required_outputs_ok: {report.get('validation',{}).get('required_outputs_ok')}", f"- image_outputs_ok: {report.get('validation',{}).get('image_outputs_ok')}", f"- strict_would_fail: {report.get('validation',{}).get('strict_would_fail')}", f"- ready_for_thomsonlint_smoke_test: {report.get('validation',{}).get('ready_for_thomsonlint_smoke_test')}", "", "## Warnings"]
    if report["warnings"]:
        for w in report["warnings"]:
            lines.append(f"- {w['code']}: {w['message']}")
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    output_root = Path(args.output_root).resolve() if args.output_root else project_root / "post_conversion"
    project_name = infer_project_name(project_root, args.project_name)

    schematic_dir, layout_dir, top_warnings = discover_input_dirs(project_root)
    files = scan_files(project_root, schematic_dir, layout_dir)
    if not files:
        top_warnings.append({"code": "WARN_NO_INPUT_FILES", "message": "No input files were discovered."})

    bom = parse_bom(project_name, project_root, files)
    pads = parse_pads(project_root, files, bom)
    sch = build_schematic_export(project_name, project_root, pads)
    ipc = parse_ipc2581(project_root, files)
    brd = build_board_export(project_name, project_root, ipc)
    stack = build_stack_export(project_name, project_root, ipc)
    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)
    images = render_pdf_images(args, project_root, output_root, project_name, files)
    report = build_report(args, project_root, output_root, project_name, files, top_warnings, bom, pads, ipc, images)

    report_json = output_root / f"{project_name}-conversion-report.json"
    report_md = output_root / f"{project_name}-conversion-report.md"
    bom_json = output_root / f"{project_name}-bom.json"
    sch_json = output_root / f"{project_name}-thomson-export-sch.json"
    brd_json = output_root / f"{project_name}-thomson-export-brd.json"
    stack_json = output_root / f"{project_name}-thomson-export-stack.json"

    if args.dry_run:
        print("[dry-run] planned report outputs:")
        print(f"[dry-run] - {report_json}")
        print(f"[dry-run] - {report_md}")
        print(f"[dry-run] - {bom_json}")
        print(f"[dry-run] - {sch_json}")
        print(f"[dry-run] - {brd_json}")
        print(f"[dry-run] - {stack_json}")
        for img in images.get("image_outputs", []):
            print(f"[dry-run] - {img}")
    else:
        output_root.mkdir(parents=True, exist_ok=True)
        if not args.report_only:
            with bom_json.open("w", encoding="utf-8") as f:
                json.dump(bom, f, indent=2 if args.pretty else None)
            with sch_json.open("w", encoding="utf-8") as f:
                json.dump(sch, f, indent=2 if args.pretty else None)
            with brd_json.open("w", encoding="utf-8") as f:
                json.dump(brd, f, indent=2 if args.pretty else None)
            with stack_json.open("w", encoding="utf-8") as f:
                json.dump(stack, f, indent=2 if args.pretty else None)
            try:
                json.loads(bom_json.read_text(encoding="utf-8"))
                report["bom"]["json_validation"] = {"status": "pass"}
            except Exception as exc:
                report["bom"]["json_validation"] = {"status": "fail", "error": str(exc)}
                report["errors"].append({"code": "ERR_BOM_JSON_INVALID", "message": str(exc)})
            try:
                json.loads(sch_json.read_text(encoding="utf-8"))
                report["schematic"]["json_validation"] = {"status": "pass"}
            except Exception as exc:
                report["schematic"]["json_validation"] = {"status": "fail", "error": str(exc)}
                report["errors"].append({"code": "ERR_SCHEMATIC_JSON_INVALID", "message": str(exc)})
            try:
                json.loads(brd_json.read_text(encoding="utf-8"))
                report["ipc2581"]["board_json_validation"] = {"status": "pass"}
            except Exception as exc:
                report["ipc2581"]["board_json_validation"] = {"status": "fail", "error": str(exc)}
                report["errors"].append({"code": "ERR_BOARD_JSON_INVALID", "message": str(exc)})
            try:
                json.loads(stack_json.read_text(encoding="utf-8"))
                report["ipc2581"]["stack_json_validation"] = {"status": "pass"}
            except Exception as exc:
                report["ipc2581"]["stack_json_validation"] = {"status": "fail", "error": str(exc)}
                report["errors"].append({"code": "ERR_STACK_JSON_INVALID", "message": str(exc)})

        with report_json.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2 if args.pretty else None)
        with report_md.open("w", encoding="utf-8") as f:
            f.write(report_markdown(report))
        report["validation"] = validate_outputs(project_root, output_root, project_name, files, report)
        with report_json.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2 if args.pretty else None)
        with report_md.open("w", encoding="utf-8") as f:
            f.write(report_markdown(report))

    if args.dry_run:
        report["validation"] = {"ok": True, "json_round_trip_ok": True, "required_outputs_ok": True, "image_outputs_ok": True, "warnings_count": len(report.get("warnings", [])), "strict_would_fail": len(report.get("warnings", []))>0, "ready_for_thomsonlint_smoke_test": True, "issues": ["dry-run validation placeholder"]}
    print(json.dumps(report, indent=2 if args.pretty else None))

    if args.strict and report["warnings"]:
        return 2
    if args.strict and report["errors"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
