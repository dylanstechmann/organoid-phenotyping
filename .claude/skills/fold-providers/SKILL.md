---
name: fold-providers
description: Decide how to get protein/complex structures in regen and bioprinting repos - experimental PDB/AlphaFold DB first, local folding second, remote providers (OMTX, JapanFold, NVIDIA NIM OpenFold3) only through the regen-workbench remote-fold CLI with keys from the private store. Use whenever a task needs a structure that is not already in the repo, docking input, a fold of a designed sequence, or provider routing for large complexes. Provider keys are never passed to MCP or committed files.
---

# Structure prediction provider routing

The authoritative provider docs live in `regen-workbench/DOCKING.md` and
`regen-workbench/MCP_SETUP.md`. This skill is the portable decision ladder for
any repo that needs a structure.

## Ladder (do not skip rungs)

1. **Experimental**: RCSB PDB entry, or the same protein in a sibling repo's
   `data/structures/`. Cite the PDB ID.
2. **AlphaFold DB**: existing AFDB model for the wild-type sequence
   (`alphafold` fetch via regen-workbench tools). Cite the AFDB accession.
3. **Local fold** (boltz / OpenFold3 Compose profile on the laptop) for designed
   or mutant sequences that no DB has.
4. **Remote fold — explicit choice required**: jobs that exceed the laptop's
   GPU/RAM budget use the `remote-fold` Compose profile with an explicit CLI
   command (see DOCKING.md). Providers: OMTX (`OMTX_API_KEY`), JapanFold
   (`JAPANFOLD_API_KEY`), NVIDIA NIM (`NVIDIA_API_KEY`, `nvapi-...`).

## Rules

- **Provider keys stay out of MCP.** They live in `~/.config/ai-keys.env` and
  reach the fold CLI through the shell environment (`aikeys`) or Compose
  interpolation — never in `.mcp.json`, tool arguments, logs, or committed files.
- **Remote jobs are a cost decision.** State the provider, model version and
  date in provenance; a remote fold is a computational hypothesis, not a result.
- **Fetch order still applies remotely**: do not pay to fold something that
  PDB/AFDB already solved.
- Docking scores (Vina/GNINA) and predicted structures remain hypotheses until
  a measured control exists — keep the repo's own claim vocabulary
  (`claim_support_unreviewed`, `UNVERIFIED`) intact.
