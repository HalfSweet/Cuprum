# Cuprum

Cuprum lets an agent draw schematics, and lay them out on a board, directly in KiCad. It drives
the design through the KiCad 11 IPC API, with no DSL, intermediate representation or MCP server in
between.

The schematic is the single source of truth. Connectivity is exported from the schematic (KiCad's
own engine computes it) and is only used for checking; the schematic is never generated from it.

The repository contains:

- `src/cuprum/`: the `cuprum` command line and a thin Python layer over the official
  [kicad-python](https://gitlab.com/kicad/code/kicad-python), adding what it still lacks.
- `skills/kicad/`: the skill for agents.

## Setup

- KiCad 11. It is not released yet, so use a 10.99 nightly; `kicad-cli api-server` is required.
- [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

kicad-python is installed from git and pinned to a commit, because the release on PyPI has no
schematic support yet. Its build needs the protocol definitions from the KiCad sources, so the
first `uv sync` clones the whole KiCad repository as a submodule (about 3.5 GB and a few minutes).
After that uv uses its cache.

To have `cuprum` on PATH everywhere:

```bash
uv tool install --editable .
```

## Usage

```bash
cuprum new demo.kicad_sch
cuprum add Device:R 4000,3000 --rot 90 --value 10k
cuprum add Device:C 4500,3150 --value 100n
cuprum wire R1.2 C1.1
cuprum add power:GND C1.2
cuprum netlist
cuprum erc
cuprum render
```

Schematic coordinates are in mils. A position is either `X,Y` or `REF.PIN`. Run `cuprum --help`
for the list of commands and `cuprum COMMAND --help` for their options.

The output is text meant to be read. With `--json` a command prints the same content as one line
of JSON instead, for scripts: `cuprum pcb show --net GND --json | jq -r '.tracks[].id'`. Lengths
are numbers in the unit of the text (mils, millimetres under `pcb`), IDs are complete, and a value
that is not set is `null`.

The board commands live under `cuprum pcb` and work in millimetres:

```bash
cuprum set R1 --footprint Resistor_SMD:R_0603_1608Metric
cuprum set C1 --footprint Capacitor_SMD:C_0603_1608Metric
cuprum pcb sync                    # update the board from the schematic
cuprum pcb outline 100,100 120,110
cuprum pcb move R1 105,105 --rot 90
cuprum pcb track R1.2 108,105 C1.1
cuprum pcb track C1.2 112,103 via   # drop into the ground pour on the back
cuprum pcb zone GND 100,100 120,110 --layer B.Cu
cuprum pcb drc
cuprum pcb render
```

Placement and routing are done by the agent with these commands; there is no autorouter.
`pcb track` goes through the positions it is given and only shapes the way between them:
segments at 0, 45 or 90 degrees, with square corners cut. `pcb at` and `pcb gap` answer what
copper lies at a position and how far two objects are apart, `pcb rule` and `pcb keepout` hold
the requirements that net classes cannot express.

When the design is open in a running KiCad whose API server is enabled, the commands act on the
open editor and every change shows up there. Otherwise the first command for a project starts a
headless KiCad server (`kicad-cli api-server`) in the background and later commands attach to it,
so each one takes a fraction of a second. Changes are saved to disk immediately. `cuprum stop`
shuts the background server down.

The same package is usable from Python:

```python
from cuprum import connect, netlist_text, run_erc

with connect("demo.kicad_sch") as kicad:  # attaches to the same background server
    sch = kicad.get_schematic()
    print(netlist_text(sch))
```

## Workarounds for the KiCad nightly

A few things in the headless API server of the current nightly do not behave as they should.
Each workaround is explained where it lives in the code; they are worth reporting upstream.

- Export jobs (netlist, plots) load the document once and keep reusing that copy, so later
  exports miss every change made since. Cuprum saves the document and runs a one-shot `kicad-cli`
  on the file instead (`export.py`).
- Zones are filled with the design rules as they were at the last DRC run, which on a new board
  means no clearances at all. Cuprum runs a DRC before every refill (`board.py`).
- Project requests are validated with the comparisons inverted, so a request naming the open
  project is rejected (`cli_pcb.py`).
- kicad-python unpacks the items returned by `create_items` / `update_items` twice and raises
  (`session.py`).
- The angle of a board text set through the API is reported back but not drawn, checked or
  saved, and every update of a text or of its footprint through the API resets the real one.
  Cuprum writes the angle into the saved board and has KiCad load it again (`board.py`).
- The API can open a library symbol or footprint and edit its children, but it cannot create
  one, name it or set its fields, and the library-table commands have no handler. `cuprum lib`
  therefore writes the project's library files itself and uses the API to read them back
  (`library.py`).
- `kicad-cli` cannot plot part of a board. `pcb render --area` plots a copy of the board whose
  bounding box is centred on the area, which is where a scaled plot puts the middle of the page
  (`export.py`). A schematic is plotted as the whole page, finer, and cut.

## API stability

The schematic IPC API in KiCad 11 and its kicad-python bindings are still under development and
can change at any time. After updating the KiCad nightly or the kicad-python commit pinned in
`pyproject.toml`, this code may need to follow.
