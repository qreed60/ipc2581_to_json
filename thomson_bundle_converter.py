#!/usr/bin/env python3
"""ThomsonLint CAD bundle converter skeleton with Phase 2 BOM + Phase 3 PADS schematic parsing."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.3.0-phase3"
BOM_PARSER_VERSION = "bom-v1"
PADS_PARSER_VERSION = "pads-v1"


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
    if ext in {".xml", ".ipc2581"} and role_hint == "layout":
        return ClassifiedFile(rel, ext, size, "ipc2581_candidate", "high", "Layout XML/IPC2581 extension")
    if ext == ".pdf" and role_hint == "schematic":
        return ClassifiedFile(rel, ext, size, "schematic_pdf_candidate", "high", "PDF under schematic scope")
    if ext == ".pdf" and role_hint == "layout":
        return ClassifiedFile(rel, ext, size, "layout_pdf_candidate", "high", "PDF under layout scope")
    if ext == ".csv":
        return ClassifiedFile(rel, ext, size, "bom_csv_candidate", "medium", "CSV found outside schematic scope; treated as BOM candidate")
    if ext in {".xml", ".ipc2581"}:
        return ClassifiedFile(rel, ext, size, "ipc2581_candidate", "medium", "XML/IPC2581 found outside layout scope")
    if ext == ".pdf":
        if "schem" in name:
            return ClassifiedFile(rel, ext, size, "schematic_pdf_candidate", "medium", "Filename suggests schematic PDF")
        if any(k in name for k in ["gerber", "layout", "pcb"]):
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
    "dni": "dnp", "dnp": "dnp", "donotinstall": "dnp", "part": "item", "item": "item"
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

        items.append({
            "refdes": refs,
            "fields": {
                "value": pick("value"), "description": pick("description"), "manufacturer": pick("manufacturer"),
                "mpn": pick("mpn"), "vendor": pick("vendor"), "vendor_pn": pick("vendor_pn"),
                "quantity": pick("quantity"), "footprint": pick("footprint"), "package": pick("package"), "dnp": dnp_val,
            },
            "raw_row_index": idx,
        })

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
                refdes = toks[1]
                value = None
                footprint = None
                part_name = toks[0]
                for tk in toks[2:]:
                    low = tk.lower()
                    if low.startswith("value="):
                        value = tk.split("=", 1)[1]
                    elif low.startswith("footprint=") or low.startswith("package="):
                        footprint = tk.split("=", 1)[1]
                components[refdes] = {"refdes": refdes, "value": value, "footprint": footprint, "package": footprint, "part_name": part_name, "source": {"format": "pads_ascii", "file": source.relative_path}}
        elif current_section == "net":
            if line.upper().startswith("NET "):
                if current_net:
                    nets.append(current_net)
                current_net = {"name": line[4:].strip(), "nodes": []}
                continue
            if current_net is not None:
                pin_token = line.split()[0]
                if "." in pin_token:
                    refdes, pin = pin_token.split(".", 1)
                else:
                    refdes, pin = pin_token, None
                current_net["nodes"].append({"refdes": refdes, "pin_number": pin, "pin_name": None})
    if current_net:
        nets.append(current_net)

    if not components:
        warnings.append({"code": "WARN_PADS_NO_COMPONENTS", "message": "No components extracted from PADS file."})
    if not nets:
        warnings.append({"code": "WARN_PADS_NO_NETS", "message": "No nets extracted from PADS file."})

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


def build_report(args: argparse.Namespace, project_root: Path, output_root: Path, project_name: str, files: list[ClassifiedFile], warnings: list[dict[str, Any]], bom: dict[str, Any], pads: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in files:
        counts[f.category] = counts.get(f.category, 0) + 1

    return {
        "metadata": {
            "converter": "thomson_bundle_converter", "version": VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_root": str(project_root), "project_name": project_name, "output_root": str(output_root), "args": vars(args), "phase": "phase3_pads_schematic",
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
        "warnings": warnings + bom.get("warnings", []) + pads.get("warnings", []),
        "errors": [],
        "notes": [
            "Phase 1 performs discovery/reporting.", "Phase 2 adds BOM parsing.", "Phase 3 adds PADS schematic parsing and BOM merge.",
            "WARNING: tools/kicad-export.py not found in this repository snapshot; schematic shape uses a compatibility-oriented best effort.",
        ],
    }


def report_markdown(report: dict[str, Any]) -> str:
    m = report["metadata"]
    s = report.get("schematic", {})
    b = report.get("bom", {})
    lines = [
        f"# Conversion Report (Phase 3) - {m['project_name']}", "", "## Metadata",
        f"- Converter: {m['converter']} {m['version']}", f"- Generated (UTC): {m['generated_at_utc']}",
        "", "## Discovery Counts",
    ]
    for c, n in sorted(report["discovery"]["counts_by_category"].items()):
        lines.append(f"- {c}: {n}")
    lines += ["", "## BOM", f"- Source file: `{b.get('source_file')}`", f"- Row count: {b.get('row_count', 0)}", "", "## Schematic (PADS)", f"- Source file: `{s.get('source_file')}`", f"- Detected dialect: {s.get('detected_dialect')}", f"- Components: {s.get('component_count', 0)}", f"- Nets: {s.get('net_count', 0)}", f"- Nodes: {s.get('node_count', 0)}", f"- Schematic JSON validation: {s.get('json_validation', {}).get('status', 'unknown')}", "", "## Warnings"]
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
    report = build_report(args, project_root, output_root, project_name, files, top_warnings, bom, pads)

    report_json = output_root / f"{project_name}-conversion-report.json"
    report_md = output_root / f"{project_name}-conversion-report.md"
    bom_json = output_root / f"{project_name}-bom.json"
    sch_json = output_root / f"{project_name}-thomson-export-sch.json"

    if args.dry_run:
        print("[dry-run] planned report outputs:")
        print(f"[dry-run] - {report_json}")
        print(f"[dry-run] - {report_md}")
        print(f"[dry-run] - {bom_json}")
        print(f"[dry-run] - {sch_json}")
    else:
        output_root.mkdir(parents=True, exist_ok=True)
        if not args.report_only:
            with bom_json.open("w", encoding="utf-8") as f:
                json.dump(bom, f, indent=2 if args.pretty else None)
            with sch_json.open("w", encoding="utf-8") as f:
                json.dump(sch, f, indent=2 if args.pretty else None)
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

        with report_json.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2 if args.pretty else None)
        with report_md.open("w", encoding="utf-8") as f:
            f.write(report_markdown(report))

    print(json.dumps(report, indent=2 if args.pretty else None))

    if args.strict and report["warnings"]:
        return 2
    if args.strict and report["errors"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
