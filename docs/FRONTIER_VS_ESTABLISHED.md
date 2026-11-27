# Frontier vs. established

**Evidence date:** 2026-11-27

This note separates reproducible software and data-contract observations from
research questions that remain open. The observations below are agent-run checks,
not independent biological review.

## Established by reproducible checks on this date

- The full `python -m unittest discover -s tests -v` suite completed with **49
  passing tests**.
- The pinned Bonn acquisition manifest
  (`6aca395889ff5864793d27f0327eda93d9e609aca7eb3c24069b789d605894fd`)
  contains **280 frames in five biological units**.
- The deterministic development-only packet contains **195 development frames
  from four development biological units**.
- The frozen held-out biological unit is excluded from the packet: its frame
  IDs, image paths and hashes are absent, and packet generation does not open
  held-out image contents.
- Receipt-bound source SHA-256 values show **zero exact-image overlap** between
  development and held-out frames. The measurement receipt also reports exact
  shared-file hashes when they occur.
- Packet validation requires byte-for-byte regeneration of the three text
  outputs and rejects grouping violations, exact cross-split image overlap,
  missing provenance and incomplete reviewer-disposition records.

## Frontier and not established

- **Annotation and human review:** all 195 included frames remain pending;
  `pending_review` is not a reviewer decision.
- **Masks, calibration and scale:** no mask revision or physical pixel scale is
  supplied in this packet. Magnification is not calibration.
- **Non-exact duplicate detection:** SHA-256 does not establish whether
  re-encoded, resized, cropped or otherwise transformed images are duplicates.
- **Segmentation:** the packet does not generate or validate segmentations.
- **Biological generalization:** the frozen split and held-out exclusion are
  leakage controls, not evidence of predictive or biological generalization.
- **Biological interpretation:** no viability, maturation, tissue function,
  regenerative potency or rejuvenation claim is established. This work supports
  supplied-mask morphology measurement only.

## Update when evidence changes

Update this document whenever new reproducible evidence changes these boundaries.
Record the evidence date, exact input and output hashes, commands, test counts,
and any reviewer or calibration provenance. Keep pending and missing states
explicit, and do not promote a software check into an annotation, biological,
generalization or clinical claim without the corresponding evidence.
