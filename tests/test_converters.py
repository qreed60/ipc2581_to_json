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
