import json, subprocess
from pathlib import Path

PADS = """*PART*
COMP U1 value=MCU footprint=QFN48
COMP R1 value=10k footprint=0402
*NET*
NET GND
U1.1
R1.1
NET SIG
U1.2
R1.2
"""
BOM = "Designator,Value,MPN,Manufacturer,Description,Footprint\nU1,MCU,STM32,ST,Controller,QFN48\nR1,10k,RC0402,Yageo,Resistor,0402\nC9,100n,CC0402,Murata,Cap,0402\n"
XML = """<IPC2581><Layer name='L1' type='signal'/><Layer name='L2' type='signal'/><Component refdes='U1' x='1' y='2'/><Component refdes='R1' x='3' y='4'/><Net name='GND'><Pin refdes='U1' pin='1'/><Pin refdes='R1' pin='1'/></Net><Net name='SIG'><Pin refdes='U1' pin='2'/><Pin refdes='R1' pin='2'/></Net><Via x='5' y='6' drill='0.2'/><Segment x1='1' y1='1' x2='2' y2='2' net='SIG'/></IPC2581>"""
XML_NS = """<ns:IPC2581 xmlns:ns='urn:test'><ns:Layer name='L1'/><ns:Layer name='L2'/><ns:Component refdes='U1'/><ns:Component refdes='R1'/><ns:Net name='GND'><ns:Pin refdes='U1' pin='1'/><ns:Pin refdes='R1' pin='1'/></ns:Net></ns:IPC2581>"""

def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

def test_all(tmp_path):
    root = Path(__file__).resolve().parents[1]
    net = tmp_path/"pads.asc"; net.write_text(PADS)
    bom = tmp_path/"bom.csv"; bom.write_text(BOM)
    xml = tmp_path/"ipc.xml"; xml.write_text(XML)
    xmlns = tmp_path/"ipcns.xml"; xmlns.write_text(XML_NS)
    out = tmp_path/"out"

    r = run(["python3","pads_ascii_to_thomson_sch.py","--netlist",str(net),"--bom",str(bom),"--project","t1","--output",str(out),"--pretty"], root)
    assert r.returncode == 0
    sch = json.loads((out/"t1-thomson-export-sch.json").read_text())
    assert len(sch["components"]) >= 2 and len(sch["nets"]) >= 2

    (tmp_path/"empty.asc").write_text("")
    r = run(["python3","pads_ascii_to_thomson_sch.py","--netlist",str(tmp_path/"empty.asc"),"--project","e","--output",str(out)], root)
    assert r.returncode != 0

    r = run(["python3","ipc2581_to_thomson.py",str(xml),"--project","b1","--output",str(out),"--pretty"], root)
    assert r.returncode == 0
    brd = json.loads((out/"b1-thomson-export-brd.json").read_text())
    assert len(brd["components"]) == 2

    r = run(["python3","ipc2581_to_thomson.py",str(xmlns),"--project","b2","--output",str(out)], root)
    assert r.returncode == 0

    bad = tmp_path/"bad.xml"; bad.write_text("<x>")
    r = run(["python3","ipc2581_to_thomson.py",str(bad),"--project","bad","--output",str(out)], root)
    assert r.returncode != 0

    dry = tmp_path/"dry"; dry.mkdir()
    r = run(["python3","ipc2581_to_thomson.py",str(xml),"--project","dry","--output",str(dry),"--dry-run"], root)
    assert r.returncode == 0 and not (dry/"dry-thomson-export-brd.json").exists()
    r = run(["python3","altium_orcad_to_thomson_bundle.py","--bundle",str(tmp_path/"bundle_missing"),"--project","bdry","--output",str(tmp_path/"dry2"),"--dry-run"], root)
    assert r.returncode == 0 and not (tmp_path/"dry2").exists()

    bundle = tmp_path/"bundle"; (bundle/"schematic").mkdir(parents=True); (bundle/"layout").mkdir(parents=True)
    (bundle/"schematic/pads_netlist.asc").write_text(PADS)
    (bundle/"schematic/bom.csv").write_text(BOM)
    (bundle/"layout/ipc2581.xml").write_text(XML)
    r = run(["python3","altium_orcad_to_thomson_bundle.py","--bundle",str(bundle),"--project","bun","--output",str(out),"--pretty"], root)
    assert r.returncode == 0
    assert (out/"bun-thomson-export-sch.json").exists()
    assert (out/"bun-thomson-export-brd.json").exists()
    assert (out/"bun-thomson-export-stack.json").exists()
    assert (out/"bun-bundle-conversion-report.json").exists()


def test_phase1_bundle_converter_discovery_and_dryrun(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path/"proj"
    (proj/"pre_conversion"/"schematic").mkdir(parents=True)
    (proj/"pre_conversion"/"layout").mkdir(parents=True)
    (proj/"pre_conversion"/"schematic"/"n.asc").write_text("*PADS-PCB*\n")
    (proj/"pre_conversion"/"schematic"/"bom.csv").write_text("A,B\n1,2\n")
    (proj/"pre_conversion"/"schematic"/"sch.pdf").write_text("pdf")
    (proj/"pre_conversion"/"layout"/"board.xml").write_text("<IPC-2581/>")
    (proj/"pre_conversion"/"layout"/"fab.pdf").write_text("pdf")

    out = tmp_path/"out"
    r = run(["python3","thomson_bundle_converter.py",str(proj),"--output-root",str(out),"--dry-run","--report-only"], root)
    assert r.returncode == 0
    assert not out.exists()

    r2 = run(["python3","thomson_bundle_converter.py",str(proj),"--output-root",str(out),"--pretty"], root)
    assert r2.returncode == 0
    report = json.loads((out/"proj-conversion-report.json").read_text())
    cats = report["discovery"]["counts_by_category"]
    assert cats["pads_ascii_candidate"] == 1
    assert cats["bom_csv_candidate"] == 1
    assert cats["schematic_pdf_candidate"] == 1
    assert cats["ipc2581_candidate"] == 1
    assert cats["layout_pdf_candidate"] == 1


def test_phase1_examples_compat_mode(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path/"examples"
    proj.mkdir()
    (proj/"example_pads.asc").write_text("*PADS-PCB*\n")
    (proj/"example_bom.csv").write_text("A,B\n1,2\n")
    (proj/"example_ipc.xml").write_text("<IPC-2581/>")
    (proj/"example_schematic.pdf").write_text("pdf")
    (proj/"example_gerbers.pdf").write_text("pdf")

    out = tmp_path/"o2"
    r = run(["python3","thomson_bundle_converter.py",str(proj),"--output-root",str(out),"--pretty"], root)
    assert r.returncode == 0
    report = json.loads((out/"examples-conversion-report.json").read_text())
    warn_codes = {w["code"] for w in report["warnings"]}
    assert "WARN_EXAMPLES_FLAT_LAYOUT" in warn_codes
    cats = report["discovery"]["counts_by_category"]
    assert cats["pads_ascii_candidate"] == 1
    assert cats["bom_csv_candidate"] == 1
    assert cats["ipc2581_candidate"] == 1
    assert cats.get("schematic_pdf_candidate", 0) + cats.get("layout_pdf_candidate", 0) == 2
    files = report["discovery"]["files"]
    gerber = [f for f in files if f["relative_path"] == "example_gerbers.pdf"][0]
    assert gerber["category"] == "layout_pdf_candidate"

def test_phase2_bom_simple_and_multi_refdes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text(
        "Designator,Value,Qty,DNP\nR1 R2,10k,2,No\nC1-C3,100n,3,Yes\n"
    )
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out), "--pretty"], root)
    assert r.returncode == 0
    bom = json.loads((out / "proj-bom.json").read_text())
    assert bom["row_count"] == 2
    assert bom["expanded_refdes_count"] == 5
    assert bom["items"][0]["fields"]["dnp"] is False
    assert bom["items"][1]["fields"]["dnp"] is True


def test_phase2_bom_duplicate_refdes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text(
        "RefDes,Description\nU1,MCU\nU1,MCU2\n"
    )
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out)], root)
    assert r.returncode == 0
    bom = json.loads((out / "proj-bom.json").read_text())
    assert "U1" in bom["duplicate_refdes"]


def test_phase2_bom_real_examples_smoke_if_present(tmp_path):
    root = Path(__file__).resolve().parents[1]
    examples = root / "examples"
    if not examples.exists():
        return
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(examples), "--project-name", "example", "--output-root", str(out), "--pretty"], root)
    assert r.returncode == 0
    assert (out / "example-bom.json").exists()
    report = json.loads((out / "example-conversion-report.json").read_text())
    assert "bom" in report

def test_phase3_pads_parse_and_bom_merge(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "net.asc").write_text(
        "*PADS-PCB*\n*PART*\nCOMP U1 value=MCU footprint=QFN48\nCOMP R1 value=10k footprint=0402\n*NET*\nNET GND\nU1.1\nR1.1\nNET SIG\nU1.2\nR1.2\n"
    )
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text(
        "RefDes,Value,Footprint,Description\nU1,MCU,QFN48,Controller\nR1,10k,0402,Resistor\n"
    )
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out), "--pretty"], root)
    assert r.returncode == 0
    sch = json.loads((out / "proj-thomson-export-sch.json").read_text())
    assert len(sch["components"]) == 2
    assert len(sch["nets"]) == 2
    assert sch["bom_merge"]["components_with_bom_metadata"] == 2
    sig = {n["name"]: n for n in sch["nets"]}["SIG"]
    assert sig["node_count"] == 2


def test_phase3_pads_value_mismatch_warning(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "net.asc").write_text(
        "*PADS-PCB*\n*PART*\nCOMP U1 value=A footprint=QFN48\n*NET*\nNET G\nU1.1\n"
    )
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text("RefDes,Value\nU1,B\n")
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out)], root)
    assert r.returncode == 0
    report = json.loads((out / "proj-conversion-report.json").read_text())
    codes = {w["code"] for w in report["warnings"]}
    assert "WARN_COMPONENT_VALUE_MISMATCH" in codes


def test_phase3_real_examples_smoke_if_present(tmp_path):
    root = Path(__file__).resolve().parents[1]
    examples = root / "examples"
    if not examples.exists():
        return
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(examples), "--project-name", "example", "--output-root", str(out), "--pretty"], root)
    assert r.returncode == 0
    assert (out / "example-thomson-export-sch.json").exists()
    sch = json.loads((out / "example-thomson-export-sch.json").read_text())
    assert "components" in sch and "nets" in sch

def test_phase4_ipc_minimal_and_namespace(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text("RefDes,Value\nU1,MCU\n")
    (proj / "pre_conversion" / "schematic" / "net.asc").write_text("*PADS-PCB*\n*PART*\nCOMP U1 value=MCU footprint=QFN\n*NET*\nNET GND\nU1.1\n")
    (proj / "pre_conversion" / "layout" / "board.xml").write_text("""<ns:IPC-2581 xmlns:ns='urn:test' revision='B'><ns:Layer name='L1' layerFunction='signal'/><ns:Component refDes='U1' x='1' y='2' layerRef='L1'/><ns:Net name='GND'><ns:PinRef componentRef='U1' pin='1'/></ns:Net><ns:Via x='1' y='1' drill='0.2'/><ns:Segment x1='0' y1='0' x2='1' y2='1' net='GND'/></ns:IPC-2581>""")
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out)], root)
    assert r.returncode == 0
    brd = json.loads((out / "proj-thomson-export-brd.json").read_text())
    stk = json.loads((out / "proj-thomson-export-stack.json").read_text())
    assert brd["source"]["ipc_root"] == "IPC-2581"
    assert len(brd["components"]) == 1
    assert len(brd["layers"]) >= 1
    assert "layer_stack" in stk


def test_phase4_real_examples_ipc_smoke_if_present(tmp_path):
    root = Path(__file__).resolve().parents[1]
    examples = root / "examples"
    if not examples.exists():
        return
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(examples), "--project-name", "example", "--output-root", str(out), "--pretty"], root)
    assert r.returncode == 0
    assert (out / "example-thomson-export-brd.json").exists()
    assert (out / "example-thomson-export-stack.json").exists()

def test_phase5_pdf_report_section_and_dryrun(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "n.asc").write_text("*PADS-PCB*\n*PART*\nCOMP U1\n*NET*\nNET G\nU1.1\n")
    (proj / "pre_conversion" / "schematic" / "b.csv").write_text("RefDes\nU1\n")
    (proj / "pre_conversion" / "layout" / "i.xml").write_text("<IPC-2581/>")
    (proj / "pre_conversion" / "schematic" / "s.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out), "--dry-run", "--report-only"], root)
    assert r.returncode == 0
    assert not list(out.glob("*.png"))


def test_phase5_real_examples_png_smoke_if_poppler(tmp_path):
    import shutil
    if not shutil.which("pdftoppm"):
        return
    root = Path(__file__).resolve().parents[1]
    examples = root / "examples"
    if not examples.exists():
        return
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(examples), "--project-name", "example", "--output-root", str(out)], root)
    assert r.returncode == 0
    report = json.loads((out / "example-conversion-report.json").read_text())
    assert "images" in report

def test_phase6_validation_summary_present(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "n.asc").write_text("*PADS-PCB*\n*PART*\nCOMP U1\n*NET*\nNET G\nU1.1\n")
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text("RefDes\nU1\n")
    (proj / "pre_conversion" / "layout" / "i.xml").write_text("<IPC-2581/>")
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out), "--pretty"], root)
    assert r.returncode == 0
    report = json.loads((out / "proj-conversion-report.json").read_text())
    assert "validation" in report
    assert "ok" in report["validation"]


def test_phase6_missing_optional_pdf_non_strict_ok(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "n.asc").write_text("*PADS-PCB*\n*PART*\nCOMP U1\n*NET*\nNET G\nU1.1\n")
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text("RefDes\nU1\n")
    (proj / "pre_conversion" / "layout" / "i.xml").write_text("<IPC-2581/>")
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out)], root)
    assert r.returncode == 0
    report = json.loads((out / "proj-conversion-report.json").read_text())
    assert report["validation"]["required_outputs_ok"] is True


def test_phase6_examples_smoke_validation_if_present(tmp_path):
    root = Path(__file__).resolve().parents[1]
    examples = root / "examples"
    if not examples.exists():
        return
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(examples), "--project-name", "example", "--output-root", str(out)], root)
    assert r.returncode == 0
    report = json.loads((out / "example-conversion-report.json").read_text())
    assert "validation" in report


def test_phase66_pads_multinode_line_and_numbered_mfg_headers(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proj = tmp_path / "proj"
    (proj / "pre_conversion" / "schematic").mkdir(parents=True)
    (proj / "pre_conversion" / "layout").mkdir(parents=True)
    (proj / "pre_conversion" / "schematic" / "net.asc").write_text(
        "*PADS-PCB*\n*PART*\nJ3 footprint\nJ27 footprint\n*NET*\n*SIGNAL* ABORT_NEG\nJ3.41 J27.30\n"
    )
    (proj / "pre_conversion" / "schematic" / "bom.csv").write_text(
        "RefDes,MFG_1,MFG P/N_1\nJ3,Murata,ABC123\nJ27,AVX,XYZ999\n"
    )
    (proj / "pre_conversion" / "layout" / "i.xml").write_text("<IPC-2581/>")
    out = tmp_path / "out"
    r = run(["python3", "thomson_bundle_converter.py", str(proj), "--output-root", str(out)], root)
    assert r.returncode == 0
    sch = json.loads((out / "proj-thomson-export-sch.json").read_text())
    net = {n["name"]: n for n in sch["nets"]}["ABORT_NEG"]
    assert net["node_count"] == 2
    refs = {n["refdes"] for n in net["nodes"]}
    assert {"J3", "J27"} <= refs
    bom = json.loads((out / "proj-bom.json").read_text())
    first = bom["items"][0]
    assert first["fields"]["manufacturer"] is not None
    assert first["fields"]["mpn"] is not None
