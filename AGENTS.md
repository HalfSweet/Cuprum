# AGENTS.md

Cuprum lets an agent draw schematics directly in KiCad through the KiCad 11 IPC API. The repository
holds the `cuprum` command line and a thin Python layer (`src/cuprum/`), plus the skill that
agents use (`skills/`).

## Code conventions

- Write all code comments and docstrings in English, never in Chinese. Command-line help text,
  output and error messages are in English too.
- Keep the layer thin: add only what kicad-python lacks, and do not re-wrap what it already has.
- A command builds its result as plain data and hands it to `emit()` in `cli_common.py`, which
  renders the text form by default and prints the data itself with `--json`. Do not print results
  directly, or the command ends up without a JSON form. Notes and errors go to stderr.

## Design principles

- The schematic is the single source of truth. Every change is made to schematic objects
  (symbols, wires, labels).
- Connectivity is only ever exported from the schematic, for checking. Do not introduce a DSL or
  an intermediate representation, and never write the connectivity first and generate the
  schematic from it.
- Talk to the KiCad IPC API directly. Do not put an MCP server or a similar bridge in between.
- How and when to check the work is up to the agent using the skill. Do not add restrictions on
  that to the skill or to the code.
- The agent does all of the design work itself, placement and routing included. Do not add an
  autorouter or an auto-placer.
- Every operation is a `cuprum` command that reads like a tool call. Add a capability as a
  command first; the Python functions exist to serve the commands.
- The skill has to be cheap in tokens: state facts and rules, leave out worked examples, and
  leave command details to `cuprum <command> --help`.

## Development

- Commit messages follow Conventional Commits, in English: `feat(pcb): ...`, `fix(cli): ...`,
  `docs(skill): ...`, `refactor: ...`, `chore: ...`. One logical change per commit.
- Dependencies are managed with uv. kicad-python is installed from git and pinned to a commit;
  see `pyproject.toml`.
- KiCad 11 is required (currently the 10.99 nightly).
- Do not add tests to this repository, smoke tests in particular. To confirm behaviour, run the
  `cuprum` commands or a throwaway script kept outside the repository against a real KiCad.
- The upstream API is unreleased and may change. Saying so in the documentation is enough; do not
  add checks for it.
- The headless server of the nightly has bugs that only show on the second use within one server
  (stale export jobs, stale zone rules). When confirming behaviour, repeat the operation after a
  change in the same server rather than once in a fresh one.
