#!/usr/bin/env python3
"""Phase 1 ThomsonLint CAD bundle converter skeleton.

Discovery/report-only implementation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0-phase1"


@dataclass
class ClassifiedFile:
    relative_path: str
    extension: str
    size_bytes: int
    category: str
    confidence: str
    reason: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ThomsonLint CAD bundle converter (Phase 1 skeleton)")
    p.add_argument("project_root", help="Project root path")
    p.add_argument("--output-root", help="Output folder (default: <project_root>/post_conversion)")
    p.add_argument("--project-name", help="Project name override")
    p.add_argument("--dry-run", action="store_true", help="Do not write files/folders")
    p.add_argument("--report-only", action="store_true", help="Only generate/print report artifacts")
    p.add_argument("--strict", action="store_true", help="Return non-zero when warnings exist")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return p.parse_args()


def infer_project_name(project_root: Path, override: str | None) -> str:
    if override:
        return override
    name = project_root.name.strip()
    return name or "project"


def discover_input_dirs(project_root: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    expected_schematic = project_root / "pre_conversion" / "schematic"
    expected_layout = project_root / "pre_conversion" / "layout"

    schematic_dir = expected_schematic
    layout_dir = expected_layout

    if not expected_schematic.exists() or not expected_layout.exists():
        fallback_schematic = project_root / "schematic"
        fallback_layout = project_root / "layout"
        examples_mode = project_root.name.lower() in {"example", "examples"}

        if fallback_schematic.exists() or fallback_layout.exists():
            schematic_dir = fallback_schematic
            layout_dir = fallback_layout
            warnings.append(
                {
                    "code": "WARN_NONSTANDARD_LAYOUT",
                    "message": "Using compatibility mode with <project_root>/(schematic|layout) instead of pre_conversion tree.",
                }
            )
        elif examples_mode:
            schematic_dir = project_root
            layout_dir = project_root
            warnings.append(
                {
                    "code": "WARN_EXAMPLES_FLAT_LAYOUT",
                    "message": "Using examples compatibility mode with flat folder scan because pre_conversion tree is missing.",
                }
            )
        else:
            warnings.append(
                {
                    "code": "WARN_MISSING_EXPECTED_TREE",
                    "message": "Expected pre_conversion/schematic and pre_conversion/layout directories were not both found.",
                }
            )

    return schematic_dir, layout_dir, warnings


def classify_file(path: Path, root: Path, role_hint: str) -> ClassifiedFile:
    ext = path.suffix.lower()
    name = path.name.lower()
    rel = path.relative_to(root).as_posix()
    size = path.stat().st_size

    if ext in {".asc", ".pads", ".txt"}:
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

    return ClassifiedFile(rel, ext, size, "unknown", "low", "No Phase 1 classifier rule matched")


def scan_files(project_root: Path, schematic_dir: Path, layout_dir: Path) -> list[ClassifiedFile]:
    files: list[ClassifiedFile] = []
    seen: set[Path] = set()

    if schematic_dir.exists() and schematic_dir.is_dir():
        for p in sorted(schematic_dir.iterdir()):
            if p.is_file():
                files.append(classify_file(p, project_root, "schematic"))
                seen.add(p.resolve())

    if layout_dir.exists() and layout_dir.is_dir():
        for p in sorted(layout_dir.iterdir()):
            if p.is_file() and p.resolve() not in seen:
                files.append(classify_file(p, project_root, "layout"))
                seen.add(p.resolve())

    if not files:
        # last-resort shallow scan for compatibility only
        for p in sorted(project_root.iterdir()):
            if p.is_file():
                files.append(classify_file(p, project_root, "unknown"))

    return files


def planned_outputs(project_name: str, output_root: Path) -> list[str]:
    names = [
        f"{project_name}-thomson-export-sch.json",
        f"{project_name}-thomson-export-brd.json",
        f"{project_name}-thomson-export-stack.json",
        f"{project_name}-bom.json",
        f"{project_name}-conversion-report.json",
        f"{project_name}-conversion-report.md",
    ]
    return [str(output_root / n) for n in names]


def build_report(args: argparse.Namespace, project_root: Path, output_root: Path, project_name: str, files: list[ClassifiedFile], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in files:
        counts[f.category] = counts.get(f.category, 0) + 1

    report = {
        "metadata": {
            "converter": "thomson_bundle_converter",
            "version": VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_root": str(project_root),
            "project_name": project_name,
            "output_root": str(output_root),
            "args": vars(args),
            "phase": "phase1_discovery_report",
        },
        "discovery": {
            "files": [f.__dict__ for f in files],
            "counts_by_category": counts,
        },
        "planned_outputs": planned_outputs(project_name, output_root),
        "warnings": warnings,
        "errors": [],
        "notes": [
            "Phase 1 performs discovery and reporting only.",
            "Deep parsing of BOM/PADS/IPC-2581 is deferred to later phases.",
        ],
    }
    return report


def report_markdown(report: dict[str, Any]) -> str:
    m = report["metadata"]
    lines = [
        f"# Conversion Report (Phase 1) - {m['project_name']}",
        "",
        "## Metadata",
        f"- Converter: {m['converter']} {m['version']}",
        f"- Generated (UTC): {m['generated_at_utc']}",
        f"- Project root: `{m['project_root']}`",
        f"- Output root: `{m['output_root']}`",
        "",
        "## Discovery Counts",
    ]
    for cat, count in sorted(report["discovery"]["counts_by_category"].items()):
        lines.append(f"- {cat}: {count}")

    lines.extend(["", "## Warnings"])
    if report["warnings"]:
        for w in report["warnings"]:
            lines.append(f"- {w['code']}: {w['message']}")
    else:
        lines.append("- None")

    lines.extend(["", "## Planned Outputs"])
    for out in report["planned_outputs"]:
        lines.append(f"- `{out}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    output_root = Path(args.output_root).resolve() if args.output_root else project_root / "post_conversion"
    project_name = infer_project_name(project_root, args.project_name)

    warnings: list[dict[str, Any]] = []

    schematic_dir, layout_dir, dir_warnings = discover_input_dirs(project_root)
    warnings.extend(dir_warnings)

    files = scan_files(project_root, schematic_dir, layout_dir)
    if not files:
        warnings.append({"code": "WARN_NO_INPUT_FILES", "message": "No input files were discovered."})

    report = build_report(args, project_root, output_root, project_name, files, warnings)

    json_name = f"{project_name}-conversion-report.json"
    md_name = f"{project_name}-conversion-report.md"

    if args.dry_run:
        print("[dry-run] planned report outputs:")
        print(f"[dry-run] - {output_root / json_name}")
        print(f"[dry-run] - {output_root / md_name}")
    else:
        output_root.mkdir(parents=True, exist_ok=True)
        with (output_root / json_name).open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2 if args.pretty else None)
        with (output_root / md_name).open("w", encoding="utf-8") as f:
            f.write(report_markdown(report))

    print(json.dumps(report, indent=2 if args.pretty else None))

    if args.strict and report["warnings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
