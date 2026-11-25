---
name: kicad
description: Draw, edit and check KiCad schematics and boards directly through the KiCad 11 IPC API with the `cuprum` command line. Use this whenever a task involves a KiCad schematic (.kicad_sch) or PCB (.kicad_pcb) - placing symbols, wiring, net labels, power symbols, values and footprints, hierarchical sheets, exporting the netlist, ERC, bringing the schematic onto a board, placing footprints, routing tracks and vias, copper zones, board outline, DRC, or rendering either one to an image - even if the user does not mention IPC or cuprum.
---

# KiCad with cuprum

`cuprum` edits a design the way an engineer does in KiCad: place symbols, draw wires, add labels,
then bring the result onto a board, place the footprints and route them. KiCad itself works out
what is connected. Each command is one small operation, and you do all of the design work with
them, layout and routing included; there is no autorouter.

Read `board.md` next to this file before working on a board.

## The schematic is the source of truth

Make every change to the circuit on the schematic itself. Connectivity is only ever exported from
it; do not write a netlist, table or DSL first and generate the schematic from that. The person
reviewing the design reviews the schematic. The board follows the schematic, never the reverse.

Connections in a schematic are implied by coordinates, so ask KiCad what is connected
(`cuprum netlist`) rather than inferring it from the file. How and when to check your work is
your call: `netlist`, `erc`, `pcb drc` and the two `render` commands are there for it.

## Running it

`cuprum` lives in the uv environment of the Cuprum repository (the one this skill is in, under
`skills/kicad/`): use `cuprum` if it is on PATH, else `uv run --project <Cuprum repo> cuprum ...`.
Run it in the KiCad project directory, or pass the design with `-f`.

If the design is open in a running KiCad (API server enabled in its preferences), the commands act
on the open editor and the user sees each change: the schematic editor for the schematic, the PCB
editor for `pcb`. Only the user can open those windows. When KiCad has the project open without
the editor a command needs, the command stops and says so instead of working behind KiCad's back.
With the project open in no KiCad, the first command starts a background KiCad server (a few
seconds; a `note:` says so) and later commands attach to it (about 0.2 s each). `--gui` on a
command insists on the running KiCad, `--headless` on the background server. Either way every
change is saved at once. The background server keeps its own copy of the design in memory, so run
`cuprum stop` before anything else (git, say) changes the files.

## Schematic commands

    new FILE
    find KEYWORD... [--footprint] [--limit N]
    sym LIB:SYMBOL
    fp LIB:FOOTPRINT
    lib symbol NAME --pin NUMBER[,NUMBER...]:NAME:TYPE:SIDE[:alt=NAME/TYPE]... [--gap SIDE]...
        [--ref U] [--footprint LIB:NAME] [--description TEXT] [--datasheet URL]
        [--keywords TEXT] [--fp-filter PATTERN]...
    lib footprint NAME [--pad NUMBER:X,Y:WxH[:SHAPE][:drill=MM][:paste=COLSxROWS[/PERCENT]]
        [:back]]... [--row FIRST-LAST:X,Y:DX,DY:WxH[:SHAPE]...]... [--body WxH]
        [--description TEXT] [--silk]
    lib import PATH [NAME] [--footprint LIB:NAME]
    lib list [PATH]
    add LIB:SYMBOL POSITION [--ref REF] [--rot 0|90|180|270] [--mirror x|y] [--unit N]
        [--value V] [--footprint LIB:NAME] [--field NAME=VALUE]
    set REF [--value V] [--footprint LIB:NAME] [--ref NEW] [--at X,Y] [--rot ...]
        [--mirror x|y|none] [--unit N] [--field NAME=VALUE] [--[no-]dnp] [--[no-]bom]
        [--[no-]board] [--[no-]sim] [--[no-]pos]
    field REF [X,Y] [--name reference|value|footprint|FIELD] [--unit N] [--hide | --show]
    wire POSITION POSITION...
    label NET POSITION [--global | --hier] [--spin left|right|up|down]
        [--shape input|output|bidi|tristate|passive]
    junction POSITION...
    nc POSITION...
    text TEXT POSITION [--box [WIDTH,HEIGHT]]
    edit ID [--at POSITION] [--text TEXT] [--spin ...] [--box [WIDTH,HEIGHT]]
    rm REF_OR_ID...
    show [REF...]
    page [--size A4|A3|...] [--portrait | --landscape] [--title T] [--rev R] [--date D]
        [--company C]
    sheets
    sheet add NAME POSITION [--size WIDTH,HEIGHT] [--file-name FILE]
    sheet pin SHEET PIN POSITION [--shape ...]
    netlist
    erc [--severity CHECK=ignore|warning|error]...
    render [--area X,Y X,Y] [--dpi N] [--out DIR] [--full-page]
    stop
    kicad-cli ARGUMENTS...

Commands take `-f FILE` to name the design, and those that work on a sheet take `--sheet NAME`.
`cuprum COMMAND --help` explains the options. A POSITION is `X,Y` in mils or `REF.PIN`, where PIN
is a pin number or an unambiguous pin name: `cuprum wire R1.2 4500,3000 U3.VDD`. Quote references
that start with `#` (`'#PWR01'`).

Do not parse the text output. To filter, extract or compute, add `--json` to any command and use
`jq`: the same content on one line, with complete IDs and lengths as numbers in the text's unit.

- `find` matches keywords against `LIB:NAME`. `sym` shows a library symbol before you place it:
  description, default footprint, and each pin's number, name, type, connection point relative
  to the symbol origin (unrotated) and side of the body, with `hidden` on the pins of a stack
  that do not show and `alt` before a pin's other functions. `fp` shows a library footprint:
  courtyard, and each pad's number, position and size in millimetres.
- `add` and `set` print the absolute position of every pin and the side of the symbol it is on
  as placed, which is the way a wire leaves it. `show` lists everything on the sheet with the
  IDs that `edit` and `rm` take; `show REF` lists a symbol's pins, fields and texts.
  Symbols go by reference; two that share one by mistake are told apart by the `id` in
  `show --json`, which `show`, `set` and `rm` take in its place.
- `edit` moves or rewords a label, a text or a text box. `rm` deletes anything; if one target
  is wrong it deletes nothing. KiCad joins wires that meet end to end, which gives them new
  IDs: `wire` prints the wire as joined, which can reach further than what was drawn, and `rm`
  the ends of each wire it removed.
- `netlist` prints `NET: REF.PIN ...` for the whole design, `erc` runs the rule check, `render`
  writes one PNG per sheet, cropped to what is drawn; `render --area` enlarges one rectangle of
  a sheet instead. `kicad-cli` runs KiCad's own command line.
- Coordinates are in mils with Y pointing down. Keep symbol origins, wire ends and labels on the
  50 mil grid; library pins land on the grid when the symbol origin does.
- A wire connects to a pin when it ends on, or passes over, the pin's connection point. A wire
  that ends on the middle of another wire connects to it. Wires that merely cross do not connect
  unless a junction sits on the crossing. Junction dots are not added automatically: where three
  or more connections meet, place one so the reader can see the connection. KiCad drops a
  junction it does not need, such as one on a pin that sits on the middle of a wire; a `note:`
  says so.
- `wire` draws a straight segment between consecutive points, diagonal if they are not aligned.
  Give the corner points to route at right angles, and keep the path clear of pins it should not
  touch, including the other pin of the part it ends on.
- A label names the net of the wire or pin it sits on. Equal local labels connect within a sheet,
  global ones across sheets. Exported nets are `/NAME` for local labels, `NAME` for global.
- A power symbol's only pin is at its origin, so `cuprum add power:GND C1.2` attaches it to that
  pin. The net takes the symbol's value as its name.
- ERC wants every power net to be driven. A net fed through a connector is not, as far as it can
  tell: put a `power:PWR_FLAG` on it, or it reports `POWERPIN_NOT_DRIVEN`.
- `erc --severity CHECK=ignore` (or `warning`, `error`) first sets how serious a check is, named
  by the type its violations print. That is a project setting: the background server restarts
  to take it, and a project that is open in KiCad has to be given it there, in Schematic Setup.
- `text` starts at POSITION, its bottom-left corner. `text --box` draws a bordered text box
  with its top-left corner there, as large as the text needs or WIDTH,HEIGHT; `edit ID --box`
  resizes one the same way.
- `add` picks the next free reference unless `--ref` is given; the units of a multi-unit device
  share a `--ref` and differ in `--unit`. A reference that is in use is refused.
- `set --at/--rot/--mirror` moves the symbol and its texts but not the wires attached to it.
  `field` moves, hides or shows one text: the reference, the value or another field.
- `set REF --field NAME=VALUE` stores part data such as `MPN` or `LCSC` on the symbol. `--dnp`
  marks a part as not populated; `--no-bom`, `--no-board`, `--no-sim` and `--no-pos` leave it out
  of the BOM, the board, simulation and position files. `--no-dnp`, `--bom`, ... undo that.

### Parts the libraries lack

When `find` has no symbol or footprint for a part, create it from the datasheet, or copy it from
a library that KiCad's tables do not list, such as the user's own. Both go into a library that
belongs to the project and carries its name, so they are used as `PROJECT:NAME`. The first of
these commands in a project restarts the background server, which takes a few seconds.

- `lib symbol` draws a box with pins. Each `--pin` gives number, name, type (as `sym` prints it:
  `input`, `output`, `bidirectional`, `passive`, `power_input`, `power_output`, `open_collector`,
  `no_connect`, ...) and side (`left`, `right`, `top`, `bottom`); the pins of a side appear in
  the order given, top to bottom or left to right, and a `--gap SIDE` among them leaves one
  position free. Several numbers (`9,24,33:GND:power_input:bottom`) are pads that are one node:
  a stack, of which one pin shows and which a wire to it connects as a whole.
  `:alt=NAME/TYPE` adds another function the pin can take.
- Follow the KiCad Library Convention, and look at a similar stock symbol with `sym` first.
  Group the pins by function with a gap between groups: supply at the top, ground at the bottom,
  inputs left, outputs right. Give a pin its main function as the name and the others as
  alternates, not `PA8/RXD1`. Number the exposed pad pin count + 1, as the stock footprints do.
  Fill in `--keywords` and `--fp-filter`.
- `lib footprint` takes pads in millimetres relative to the footprint origin, y pointing down, as
  seen from the top of the board. `--row 1-6:-1.95,-1.25:0,0.5:0.7x0.25` lays out pads 1 to 6
  from that position at that pitch. SHAPE is `rect` (default), `roundrect`, `oval` or `circle`;
  `drill=MM` makes a pad through-hole. `back` puts a pad on the back of the board, where one row
  of a connector that clamps the board edge solders; `fp` and `pcb show` name the layer of such
  a pad. A courtyard is added; nothing goes on the silkscreen unless `--silk` asks for a pin-1
  dot. Check dimension lines in a datasheet carefully: whether they run between pad centres or
  pad edges decides where the pads go.
- A large pad under a chip wants `paste=2x2` or so: paste in a grid of windows covering 65% of the
  pad (or PERCENT), since one big opening floats the chip. `paste=none` leaves a pad without paste.
- `lib list PATH` names the parts in a `.kicad_sym` file, a `.pretty` folder or a folder of such
  libraries. `lib import PATH NAME` copies one of them into the project library: a symbol from a
  `.kicad_sym`, a footprint from a `.pretty` (or give its `.kicad_mod` and no name). The default
  footprint of an imported symbol still names the library it came from; `--footprint` sets it.
- Running any of these again replaces the part in the library. A symbol or footprint that is
  already placed keeps its old copy: delete and place the symbol again (`rm`, `add --ref` with
  the same reference), or delete the footprint and sync. `pcb sync --by-reference` lets a symbol
  that was placed again keep its footprint and the footprint's position.

### Hierarchical sheets

`sheet add power 5000,2500` draws a sheet symbol on this sheet and creates `power.kicad_sch`;
`--sheet power` on any command then works inside that sub-sheet. A signal crosses the boundary
through a pin on the sheet symbol (`sheet pin power VOUT 5000,3000`) and a hierarchical label of the
same name inside (`label VOUT U1.VO --hier --sheet power`). On the parent sheet the pin is a
position too: `wire R1.1 power.VOUT`. References are unique across sheets.

## Python

For bulk or unusual operations the same package is a Python API over kicad-python:
`from cuprum import connect, symbol_pins, update_symbol, netlist, run_erc, sync_board, run_drc`.
`connect(path)` attaches as the commands do and returns kicad-python's `KiCad` object. Do not call
its `export_*` methods, because the server's export jobs keep returning the document as it was at
the first export; save and use `cuprum.export`. kicad-python's source is under
`.venv/lib/python*/site-packages/kipy/` in the repository.

## Drawing style

A schematic is read by people: besides being correct, it should show at a glance what it does.

- Signals flow left to right: inputs left, outputs right. Power at the top, ground at the bottom.
- Group by function (power, MCU, interfaces, analog front end) and leave space between groups.
  When a sheet gets crowded, use a larger page or split it into sub-sheets rather than squeeze.
- Inside a group, connect with wires so the topology is visible. Between groups, use net labels
  instead of long wires across the sheet.
- Route wires horizontally and vertically with few bends and few crossings.
- Use power symbols for power and ground, not labels.
- Draw each decoupling capacitor next to the supply pin it serves, not in a pile in a corner.
- Give nets meaningful names (`SPI1_MOSI`), active-low ones an overbar (`~{RESET}`).
- Keep references and values clear of wires and other symbols; space symbols so their texts fit.
