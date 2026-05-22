# Converter usage

```bash
python3 pads_ascii_to_thomson_sch.py --netlist review_bundle/schematic/pads_netlist.asc --bom review_bundle/schematic/bom.csv --project my_board --output ./exports --pretty
python3 ipc2581_to_thomson.py review_bundle/layout/ipc2581.xml --project my_board --output ./exports --pretty
python3 altium_orcad_to_thomson_bundle.py --bundle ./review_bundle --project my_board --output ./exports --pretty
python3 altium_orcad_to_thomson_bundle.py --bundle ./review_bundle --project my_board --output ./exports --strict --pretty
python3 pads_ascii_to_thomson_sch.py --netlist review_bundle/schematic/pads_netlist.asc --inspect --project my_board
python3 ipc2581_to_thomson.py review_bundle/layout/ipc2581.xml --inspect
python3 -m pytest tests
```
