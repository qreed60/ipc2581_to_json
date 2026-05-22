#!/usr/bin/env python3
"""ThomsonLint CAD bundle converter skeleton with Phase 2 BOM parsing."""
from __future__ import annotations
import argparse, csv, io, json, re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.2.0-phase2"
PARSER_VERSION = "bom-v1"

@dataclass
class ClassifiedFile:
    relative_path:str; extension:str; size_bytes:int; category:str; confidence:str; reason:str

def parse_args()->argparse.Namespace:
    p=argparse.ArgumentParser(description="ThomsonLint CAD bundle converter (Phase 1+2)")
    p.add_argument("project_root"); p.add_argument("--output-root"); p.add_argument("--project-name")
    p.add_argument("--dry-run",action="store_true"); p.add_argument("--report-only",action="store_true")
    p.add_argument("--strict",action="store_true"); p.add_argument("--pretty",action="store_true")
    return p.parse_args()

def infer_project_name(project_root:Path, override:str|None)->str:
    return override or (project_root.name.strip() or "project")

def discover_input_dirs(project_root: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    warnings=[]; es=project_root/"pre_conversion"/"schematic"; el=project_root/"pre_conversion"/"layout"
    sd,ld=es,el
    if not es.exists() or not el.exists():
        fs,fl=project_root/"schematic",project_root/"layout"; ex=project_root.name.lower() in {"example","examples"}
        if fs.exists() or fl.exists():
            sd,ld=fs,fl; warnings.append({"code":"WARN_NONSTANDARD_LAYOUT","message":"Using compatibility mode with <project_root>/(schematic|layout) instead of pre_conversion tree."})
        elif ex:
            sd=ld=project_root; warnings.append({"code":"WARN_EXAMPLES_FLAT_LAYOUT","message":"Using examples compatibility mode with flat folder scan because pre_conversion tree is missing."})
        else:
            warnings.append({"code":"WARN_MISSING_EXPECTED_TREE","message":"Expected pre_conversion/schematic and pre_conversion/layout directories were not both found."})
    return sd,ld,warnings

def classify_file(path:Path, root:Path, role_hint:str)->ClassifiedFile:
    ext=path.suffix.lower(); name=path.name.lower(); rel=path.relative_to(root).as_posix(); size=path.stat().st_size
    if ext in {".asc",".pads",".txt"}:
        if any(k in name for k in ["pads","netlist","orcad","altium"]): return ClassifiedFile(rel,ext,size,"pads_ascii_candidate","high","Extension and filename suggest PADS/netlist export")
        return ClassifiedFile(rel,ext,size,"pads_ascii_candidate","medium","Extension is compatible with PADS ASCII export")
    if ext==".csv" and role_hint=="schematic": return ClassifiedFile(rel,ext,size,"bom_csv_candidate","high","CSV in schematic scope likely BOM")
    if ext in {".xml",".ipc2581"} and role_hint=="layout": return ClassifiedFile(rel,ext,size,"ipc2581_candidate","high","Layout XML/IPC2581 extension")
    if ext==".pdf" and role_hint=="schematic": return ClassifiedFile(rel,ext,size,"schematic_pdf_candidate","high","PDF under schematic scope")
    if ext==".pdf" and role_hint=="layout": return ClassifiedFile(rel,ext,size,"layout_pdf_candidate","high","PDF under layout scope")
    if ext==".csv": return ClassifiedFile(rel,ext,size,"bom_csv_candidate","medium","CSV found outside schematic scope; treated as BOM candidate")
    if ext in {".xml",".ipc2581"}: return ClassifiedFile(rel,ext,size,"ipc2581_candidate","medium","XML/IPC2581 found outside layout scope")
    if ext==".pdf":
        if "schem" in name: return ClassifiedFile(rel,ext,size,"schematic_pdf_candidate","medium","Filename suggests schematic PDF")
        if any(k in name for k in ["gerber","layout","pcb"]): return ClassifiedFile(rel,ext,size,"layout_pdf_candidate","medium","Filename suggests layout/Gerber PDF")
    return ClassifiedFile(rel,ext,size,"unknown","low","No classifier rule matched")

def scan_files(project_root:Path, schematic_dir:Path, layout_dir:Path)->list[ClassifiedFile]:
    files=[]; seen=set()
    for d,h in [(schematic_dir,"schematic"),(layout_dir,"layout")]:
        if d.exists() and d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and p.resolve() not in seen:
                    files.append(classify_file(p,project_root,h)); seen.add(p.resolve())
    if not files:
        for p in sorted(project_root.iterdir()):
            if p.is_file(): files.append(classify_file(p,project_root,"unknown"))
    return files

def planned_outputs(project_name:str, output_root:Path)->list[str]:
    return [str(output_root/f"{project_name}-{n}") for n in ["thomson-export-sch.json","thomson-export-brd.json","thomson-export-stack.json","bom.json","conversion-report.json","conversion-report.md"]]

def _norm(s:str)->str: return re.sub(r"[^a-z0-9]+","",s.lower())
HEADER_MAP={
"refdes":"refdes","designator":"refdes","reference":"refdes","references":"refdes",
"value":"value","description":"description","manufacturer":"manufacturer","mpn":"mpn","manufacturerpartnumber":"mpn",
"vendor":"vendor","vendorpn":"vendor_pn","quantity":"quantity","qty":"quantity","footprint":"footprint","package":"package",
"dni":"dnp","dnp":"dnp","donotinstall":"dnp","part":"item","item":"item"
}

def parse_bool(v:str)->bool|None:
    t=v.strip().lower()
    if t in {"1","true","yes","y","dnp","dni","do not install","installed=no","no-load","noload"}: return True
    if t in {"0","false","no","n","install","fitted"}: return False
    return None

def expand_refdes_cell(cell:str,warnings:list[dict[str,Any]])->list[str]:
    toks=[t for t in re.split(r"[;,\s]+",cell.strip()) if t]
    out=[]
    for tok in toks:
        m=re.match(r"^([A-Za-z]+)(\d+)-([A-Za-z]+)?(\d+)$",tok)
        if m:
            p1,s,p2,e=m.groups()
            if p2 and p2.upper()!=p1.upper():
                warnings.append({"code":"WARN_BOM_REFDES_RANGE_AMBIGUOUS","message":f"Ambiguous range token '{tok}'"}); out.append(tok); continue
            a,b=int(s),int(e)
            if a>b or b-a>500:
                warnings.append({"code":"WARN_BOM_REFDES_RANGE_AMBIGUOUS","message":f"Unsafe range token '{tok}'"}); out.append(tok); continue
            out.extend([f"{p1}{i}" for i in range(a,b+1)]); continue
        out.append(tok)
    return out

def parse_bom(project_name:str, project_root:Path, files:list[ClassifiedFile])->dict[str,Any]:
    warns=[]; cands=sorted([f for f in files if f.category=="bom_csv_candidate"], key=lambda f:f.relative_path)
    if not cands:
        warns.append({"code":"WARN_BOM_MISSING","message":"No BOM CSV candidate discovered."})
        return {"project_name":project_name,"source_file":None,"parser_version":PARSER_VERSION,"raw_headers":[],"normalized_headers":{},"row_count":0,"expanded_refdes_count":0,"duplicate_refdes":[],"warnings":warns,"items":[]}
    src=cands[0]
    if len(cands)>1: warns.append({"code":"WARN_BOM_MULTIPLE_CANDIDATES","message":f"Multiple BOM CSV candidates found ({len(cands)}); using {src.relative_path}"})
    p=project_root/src.relative_path
    text=p.read_text(encoding="utf-8-sig",errors="replace")
    reader=csv.DictReader(io.StringIO(text)); raw_headers=reader.fieldnames or []
    norm_headers={h:HEADER_MAP.get(_norm(h),"unknown") for h in raw_headers}
    refs=[h for h,n in norm_headers.items() if n=="refdes"]
    if not refs: warns.append({"code":"WARN_BOM_MISSING_REFDES_HEADER","message":"No recognized RefDes/Designator/Reference column found."})
    items=[]; ref_seen={}
    for idx,row in enumerate(reader, start=1):
        ref_col=refs[0] if refs else None; ref_cell=(row.get(ref_col) or "").strip() if ref_col else ""
        exp=expand_refdes_cell(ref_cell,warns) if ref_cell else []
        for r in exp: ref_seen[r]=ref_seen.get(r,0)+1
        dnp_val=None
        for h,n in norm_headers.items():
            if n=="dnp":
                b=parse_bool(row.get(h,"") or "")
                if b is not None: dnp_val=b; break
        def pick(k):
            for h,n in norm_headers.items():
                if n==k and (row.get(h) or "").strip(): return (row.get(h) or "").strip()
            return None
        items.append({"refdes":exp,"fields":{"value":pick("value"),"description":pick("description"),"manufacturer":pick("manufacturer"),"mpn":pick("mpn"),"vendor":pick("vendor"),"vendor_pn":pick("vendor_pn"),"quantity":pick("quantity"),"footprint":pick("footprint"),"package":pick("package"),"dnp":dnp_val},"raw_row_index":idx})
    dups=sorted([k for k,v in ref_seen.items() if v>1])
    if dups: warns.append({"code":"WARN_BOM_DUPLICATE_REFDES","message":f"Duplicate RefDes values found: {len(dups)}"})
    return {"project_name":project_name,"source_file":src.relative_path,"parser_version":PARSER_VERSION,"raw_headers":raw_headers,"normalized_headers":norm_headers,"row_count":len(items),"expanded_refdes_count":sum(len(i["refdes"]) for i in items),"duplicate_refdes":dups,"warnings":warns,"items":items}

def build_report(args,project_root,output_root,project_name,files,warnings,bom):
    counts={}
    for f in files: counts[f.category]=counts.get(f.category,0)+1
    return {"metadata":{"converter":"thomson_bundle_converter","version":VERSION,"generated_at_utc":datetime.now(timezone.utc).isoformat(),"project_root":str(project_root),"project_name":project_name,"output_root":str(output_root),"args":vars(args),"phase":"phase2_bom"},"discovery":{"files":[f.__dict__ for f in files],"counts_by_category":counts},"bom":{"source_file":bom.get("source_file"),"raw_headers":bom.get("raw_headers",[]),"normalized_headers":bom.get("normalized_headers",{}),"row_count":bom.get("row_count",0),"expanded_refdes_count":bom.get("expanded_refdes_count",0),"duplicate_refdes_count":len(bom.get("duplicate_refdes",[])),"missing_required_header_warnings":[w for w in bom.get("warnings",[]) if "HEADER" in w.get("code","")],"ambiguous_refdes_warnings":[w for w in bom.get("warnings",[]) if "RANGE" in w.get("code","")],"parse_warnings":bom.get("warnings",[]),"output_file":str(output_root/f"{project_name}-bom.json"),"json_validation":{"status":"skipped" if args.dry_run or args.report_only else "pending"}},"planned_outputs":planned_outputs(project_name,output_root),"warnings":warnings+bom.get("warnings",[]),"errors":[],"notes":["Phase 1 performs discovery and reporting.","Phase 2 adds BOM parsing only."]}

def report_markdown(report):
    m=report["metadata"]; b=report.get("bom",{})
    lines=[f"# Conversion Report (Phase 2) - {m['project_name']}","","## Metadata",f"- Converter: {m['converter']} {m['version']}",f"- Generated (UTC): {m['generated_at_utc']}","","## Discovery Counts"]
    for c,n in sorted(report["discovery"]["counts_by_category"].items()): lines.append(f"- {c}: {n}")
    lines += ["","## BOM","- Source file: `"+str(b.get("source_file"))+"`",f"- Row count: {b.get('row_count',0)}",f"- Expanded RefDes count: {b.get('expanded_refdes_count',0)}",f"- Duplicate RefDes count: {b.get('duplicate_refdes_count',0)}",f"- BOM JSON validation: {b.get('json_validation',{}).get('status','unknown')}","","## Warnings"]
    if report["warnings"]:
        for w in report["warnings"]: lines.append(f"- {w['code']}: {w['message']}")
    else: lines.append("- None")
    return "\n".join(lines)+"\n"

def main()->int:
    args=parse_args(); project_root=Path(args.project_root).resolve(); output_root=Path(args.output_root).resolve() if args.output_root else project_root/"post_conversion"; pn=infer_project_name(project_root,args.project_name)
    sd,ld,warn=discover_input_dirs(project_root); files=scan_files(project_root,sd,ld)
    if not files: warn.append({"code":"WARN_NO_INPUT_FILES","message":"No input files were discovered."})
    bom=parse_bom(pn,project_root,files)
    report=build_report(args,project_root,output_root,pn,files,warn,bom)
    jn=f"{pn}-conversion-report.json"; mn=f"{pn}-conversion-report.md"; bn=f"{pn}-bom.json"
    if args.dry_run:
        print("[dry-run] planned report outputs:"); print(f"[dry-run] - {output_root/jn}"); print(f"[dry-run] - {output_root/mn}"); print(f"[dry-run] - {output_root/bn}")
    else:
        output_root.mkdir(parents=True,exist_ok=True)
        if not args.report_only:
            bp=output_root/bn
            with bp.open("w",encoding="utf-8") as f: json.dump(bom,f,indent=2 if args.pretty else None)
            try:
                json.loads(bp.read_text(encoding='utf-8')); report["bom"]["json_validation"]={"status":"pass"}
            except Exception as exc:
                report["bom"]["json_validation"]={"status":"fail","error":str(exc)}; report["errors"].append({"code":"ERR_BOM_JSON_INVALID","message":str(exc)})
        with (output_root/jn).open("w",encoding="utf-8") as f: json.dump(report,f,indent=2 if args.pretty else None)
        with (output_root/mn).open("w",encoding="utf-8") as f: f.write(report_markdown(report))
    print(json.dumps(report,indent=2 if args.pretty else None))
    if args.strict and report["warnings"]: return 2
    if args.strict and report["errors"]: return 3
    return 0

if __name__=="__main__": raise SystemExit(main())
