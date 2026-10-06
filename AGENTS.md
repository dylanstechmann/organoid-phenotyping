# Agent instructions — organoid-phenotyping

This package measures supplied whole-organoid masks across image acquisitions.
It is not a segmentation model, viability assay, maturation classifier, or
regenerative-potency test.

## Do not

- Commit third-party image datasets, masks, or model weights without checking
  and recording their exact reuse terms.
- Randomly split frames from one organoid across development and final test.
- Treat a microscope holdout that shares specimens as biological generalization.
- Infer pixel scale, cell identity, maturation, viability, or function from mask
  geometry.
- Replace a missing or failed frame with an interpolated measurement.

## Done when

The acquisition manifest, frozen specimen split, and source/license provenance
are reviewable; any performance claim is reported at the appropriate specimen
level and remains separate from morphology-only outputs.

