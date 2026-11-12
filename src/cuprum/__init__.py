"""Cuprum: a thin layer over the KiCad IPC API (kicad-python)."""

from cuprum.board import DrcViolation, refill_zones, run_drc, sync_board
from cuprum.schematic import (
    ErcViolation,
    Pin,
    netlist,
    netlist_text,
    pin_positions,
    run_erc,
    symbol_pins,
    update_symbol,
)
from cuprum.session import connect, stop

__all__ = [
    "DrcViolation",
    "ErcViolation",
    "Pin",
    "connect",
    "netlist",
    "netlist_text",
    "pin_positions",
    "refill_zones",
    "run_drc",
    "run_erc",
    "stop",
    "symbol_pins",
    "sync_board",
    "update_symbol",
]
