# Board work with `cuprum pcb`

    pcb sync [--delete-extra] [--by-reference] [-v]
    pcb show [REF...] [--net NET]
    pcb at X,Y [--radius MM]
    pcb gap OBJECT OBJECT
    pcb move REF [X,Y] [--rot DEGREES] [--side front|back] [--pads]
    pcb field REF... | --all [--except REF...] [X,Y] [--name reference|value|both]
        [--rot DEGREES] [--size MM] [--hide | --show]
    pcb add LIB:FOOTPRINT X,Y --ref REF [--rot DEGREES] [--side front|back]
    pcb outline X,Y X,Y [X,Y...] [--radius MM] [--replace]
    pcb track STEP... [--layer L] [--width MM] [--net NET] [--via-size MM] [--via-drill MM]
        [--diagonal middle|start|end] [--chamfer MM] [--exact]
    pcb via POSITION... [--array COLSxROWS --pitch MM|DX,DY] [--net NET] [--size MM] [--drill MM]
    pcb zone NET X,Y X,Y [X,Y...] | --edit ID [NET] [X,Y...]
        [--layer L,L] [--name N] [--priority N] [--clearance MM] [--min-width MM]
        [--solid | --thermal] [--thermal-gap MM] [--spoke-width MM] [--islands remove|keep|MM2]
    pcb keepout X,Y X,Y [X,Y...] | --edit ID [X,Y...]
        [--layer L,L] [--name N] [--no tracks,vias,copper,pads,footprints]
    pcb fill
    pcb text TEXT X,Y [--layer L] [--size MM] [--rot DEGREES]
    pcb edit [ID...] [--net NET] --width MM
    pcb edit ID [--at X,Y] [--text TEXT] [--size MM] [--rot DEGREES]
    pcb rm ID_OR_REF...
    pcb rules
    pcb rule [NAME [CONDITION] [--clearance MM] [--hole-clearance MM] [--edge-clearance MM]
        [--track-width MM] [--layer L|outer|inner] | NAME --remove]
    pcb netclass NAME [--clearance MM] [--track-width MM] [--via-size MM] [--via-drill MM]
        [--nets PATTERN...] [--release PATTERN...] [--remove]
    pcb layers [COUNT]
    pcb drc [--no-parity]
    pcb render [--layers L,L,...] [--area X,Y X,Y] [--dpi N] [--out DIR]

## How the board works

- Coordinates and sizes are in millimetres (not mils), Y pointing down; rotation is in degrees,
  counter-clockwise. A POSITION is `X,Y`, or `REF.PAD` for the centre of a pad; after the first one
  of a command also `@DX,DY`, `x=X` or `y=Y`, relative to the position before. A pad number can
  occur twice (the tab of a SOT-223); `REF.PAD` is then the one nearest to the neighbouring point of
  the command. A net named by a local label is `/NAME` here; `--net NAME` finds it too.
- `pcb sync` is KiCad's "update PCB from schematic", for which every symbol needs a footprint
  (`set REF --footprint LIB:NAME`, found with `find --footprint`). It adds footprints for new
  symbols, updates changed ones, reassigns pad nets and prints the changes. Footprints whose symbol
  is gone are only removed with `--delete-extra`. A symbol that was deleted and placed again is a
  new one and gets a second footprint, unless `--by-reference` keeps the placed one. Change the
  circuit in the schematic and sync again; never patch nets on the board.
- `pcb outline` draws a rectangle from two corners (`--radius` rounds them) or a polygon from more.
  New footprints pile up near the page origin; `pcb move` places one and prints its courtyard and,
  with `--pads`, its pads. `pcb add` places one that has no symbol (a mounting hole).
- `pcb show` lists footprints, outline, tracks, vias, zones, keepouts and text with their IDs;
  `pcb show REF` a footprint's courtyard (the box other parts must stay out of), reference text and
  each pad's net, centre, size and kind; `pcb show --net NET` one net's pads and copper.
- `pcb at X,Y` lists the copper under a position, layer by layer, and with `--radius MM` what is
  near it and how far. `pcb gap A B` measures copper to copper between two objects (`REF.PAD`, a
  reference, or the ID of a track, via or zone) on each layer they share, as DRC measures it.
- A track connects to a pad, a via or another track of its net when its end lies on the other's
  copper. Copper of different nets that touches is a short, so keep a track off every pad it does
  not belong to, including the other pad of the part it ends on.
- `pcb track` routes one connection per command. Its steps are positions, `via` (a via on the
  position before it, continuing on the other side, or on `via:In1.Cu`) and `w=MM` (the width from
  there on): `pcb track U1.2 @-1,0 y=92 via x=121 via w=0.5 C2.1`. A path may end in `via` to drop
  into a pour. The net comes from the pads or copper the path touches, the width from the net class,
  the layer from the side of the first pad.
- The segments come out horizontal, vertical or at 45 degrees and are printed. Two positions that
  are not in line get a bend: straight, diagonal, straight along the longer axis
  (`--diagonal start|end` puts the diagonal at one end). A square corner is cut at 45 degrees
  (`--chamfer MM` from the corner; 0 keeps it), which leaves the position you gave beside the
  copper: branch off from a pad, a via or a point on a printed segment. Vias and the ends of the
  path stay put. `--exact` draws straight between positions.
- `pcb track` and `pcb via` print a `note:`, which stops nothing, when the new copper lies on copper
  of another net, a track end touches no other copper of its net, or a track doubles back.
- Set up net classes before routing: `pcb netclass Power --track-width 0.5 --nets +5V +3V3 GND`. A
  class only sets the width of tracks drawn afterwards; `pcb edit --net NET --width MM` changes the
  ones already there. `--release PATTERN` takes an assignment back, `--remove` the class.
- `pcb rule NAME "CONDITION" --clearance MM` adds a requirement, in KiCad's rule language, for the
  pairs of objects A and B that the condition picks; DRC and zone fills follow it. A later rule
  overrides an earlier one, so an exception goes after the rule it relaxes. Conditions:
  `A.NetClass == 'HV' && B.NetClass != 'HV'` (one class against the rest),
  `A.NetName == '/SW' && B.NetName == '/FB'` (two nets),
  `A.memberOfFootprint('U1') && B.memberOfFootprint('U1')` (the pads of one part).
- A zone fills its area and by itself keeps clear of other nets and the board edge. Pads of its net
  join it through thermal reliefs (`--solid`: fully), vias directly. `pcb zone --edit ID` replaces
  what a zone has by the net, corners and options given. `pcb zone`, `pcb keepout`, `pcb fill` and
  `pcb drc` refill the zones; until then a pour is stale after copper has moved. `pcb show` prints
  the settings of a zone that are not the defaults.
- `pcb keepout` marks an area, on all copper layers by default, that tracks, vias and zone copper
  (`--no` picks the kinds) must stay out of: fills go around it, DRC reports `ALLOWED_ITEMS`.
- `pcb text` and `pcb field` take the position of the middle of the text. `pcb field` moves, turns,
  hides or shows reference and value texts. To keep those off the silkscreen, hide them with
  `pcb field --all --name both --hide`; leave the silkscreen drawings of the footprints as they are.
- `pcb drc` reports each connection still to be routed as `UNCONNECTED_ITEMS` with its two ends, a
  symbol not yet synced as `MISSING_FOOTPRINT`, spacing problems as `CLEARANCE`, touching nets as
  `SHORTING_ITEMS`. A finished board reports no violations.
- `pcb render` writes `NAME-pcb.png`; another `--layers` list, bottom layer first, gets its own
  file, and so does `--area`, which plots one rectangle enlarged and prints its scale.
- Fabrication files: `cuprum kicad-cli pcb export gerbers|drill|pos ...` on the saved board.

## Layout

- Placement decides how hard routing will be. Draw the outline and fix what the mechanics dictate
  first: connectors on the edges they face, mounting holes, anything with a set position.
- Place the main ICs next, then each one's supporting parts right beside the pins they serve:
  decoupling capacitors closest to the supply pins, a crystal next to its two pins.
- Keep the parts of one function together and let the board follow the signal flow of the schematic.
  Rotate parts so that connected pads face each other; `pcb drc` lists the open connections, and
  fewer long or crossing ones means an easier board.
- Leave room between parts for tracks and vias: courtyards must not overlap, and `pcb show REF`
  tells how large each one is. Keep copper at least 0.5 mm from the board edge.
- Once parts are placed, move reference texts that sit on pads or under other parts.

## Routing

- Route the short, critical connections first: decoupling, crystals, power input. Then the rest.
- Tracks run at 0, 45 or 90 degrees without square corners; `pcb track` sees to it: give the points
  a track has to pass (out of a pad, through a gap) and it places the bends.
- Signals at the net class width are fine; widen power according to its current.
- On a two-layer board, pour ground on the back (often on both sides), drop a via beside each
  surface-mount ground pad, and keep the front for signals and power.
- When two tracks must cross, take one to the other side through vias. Keep vias out of pads, except
  a thermal pad, which wants a grid of them: `pcb via X,Y --array 3x3 --pitch 1`.
- Work net by net: `pcb show --net NET` shows its pads and what is already routed, `pcb render`
  shows the board, `pcb drc` tells what is left.
