# Organoid Phenotyping

Organoid Phenotyping turns source-linked microscopy acquisitions and supplied
instance masks into image- and object-level morphology tables, cross-sectional
summaries, optional tracked-object trajectories, reference-mask comparisons,
overlays, and a hash-bound receipt. Acquisitions awaiting annotation remain
explicit; the package does not determine organoid health.

See the [development roadmap](ROADMAP.md) for annotation correctness, frozen
human review, instance agreement and the first reproducible real-image case.
The local annotation desk now guards task switches during slow loads and saves,
prompts before discarding edited contours, and has a fit/zoom control. It keeps
the server's 1600-pixel overview preview; zoom enlarges that preview, while mask
coordinates continue to map to the source image.

## Why this is separate

The existing `brightfield-colony-qc` package extracts hand-built features from
small colony fields. This package keeps every acquisition tied to its declared
sample unit and freezes splits at a declared independence level such as a
tracked specimen, clone or source kidney ID. A repeated specimen ID alone does
not prove that individual objects are tracked. Longitudinal object growth is
reported only when a separate reviewed track map links instance labels across
acquisitions.

The first source intake is Figure 4 (cyst induction) from the human adult-stem-
cell kidney-tubuloid dataset by Rohe et al. ([Bonn Data repository, CC BY 4.0](https://doi.org/10.60507/FK2/OM25XQ);
[associated study, 2026](https://doi.org/10.1186/s12860-026-00591-x)). The
import command verifies the pinned archive checksum, records license and hash,
extracts source TIFFs under ignored `artifacts/`, and freezes a split by the
source kidney identifier. The 280-image archive contains treatment, culture,
replicate and timepoint metadata, but no instance masks. Imported rows therefore
remain `pending_annotation`; no morphology or treatment-effect result is claimed.

## Run

Install in an environment that has permission to read the source images, then download the Figure 4 ZIP from the Bonn repository above:

```bash
python -m pip install -e .
organoid-phenotyping import-bonn-kidney \
  --archive artifacts/source-review/kidney-figure-4-cyst-induction.zip \
  --out artifacts/bonn-kidney-cyst-induction
organoid-phenotyping prepare-annotation-pilot \
  --manifest artifacts/bonn-kidney-cyst-induction/acquisitions.csv \
  --plan artifacts/bonn-kidney-cyst-induction/study-plan.json \
  --out artifacts/annotation-pilot-bonn-cyst-24h-v1
organoid-phenotyping measure \
  --manifest artifacts/bonn-kidney-cyst-induction/acquisitions.csv \
  --plan artifacts/bonn-kidney-cyst-induction/study-plan.json \
  --out artifacts/bonn-kidney-cyst-measurements
```

When a reviewer has linked specific mask labels across acquisitions, include a
track map (CSV paths are relative to the manifest directory):

```bash
organoid-phenotyping measure \
  --manifest artifacts/bonn-kidney-cyst-induction/acquisitions.csv \
  --plan artifacts/bonn-kidney-cyst-induction/study-plan.json \
  --tracks reviewed-object-tracks.csv \
  --out artifacts/bonn-kidney-cyst-tracked-measurements
```

The importer extracts the source images and records archive checksums, a frozen
source-kidney-ID split and acquisition manifest. Its rows remain `pending_annotation`
until a segmentation mask is reviewed. The output directory must be new. A
measurement run records input hashes and writes:

- `measurements.csv`: one row per acquisition, including missing/failed status,
  image and mask paths, calibration provenance, whole-mask frame geometry,
  source and segmentation metadata, and hashes. Frame-union area can include
  several objects and is not an organoid-size estimate.
- `cross_sectional_summary.csv`: object-area summaries by sample unit, source
  group, condition, treatment, acquisition and timepoint. No change across times
  is labeled growth without linked object identities.
- `tracked_object_growth.csv`: rows are emitted only for labels in a reviewed
  `--tracks` map. The map must refer to measured mask labels and may not assign
  two labels in one frame to the same track. Each trajectory stays within its
  declared specimen unit.
- `objects.csv`: one row per positive integer instance label in a supplied mask.
  A binary foreground value is one instance; disconnected components sharing
  that value are not split automatically.
- `segmentation_comparison.csv`: frame-level foreground IoU, Dice and physical-area
  error when a manual reference mask is supplied.
- `instance_agreement.csv`: object-level agreement for the same reference pairs.
  Objects are matched one-to-one by descending IoU at or above a declared
  threshold (0.5), and the row records matched counts, the signed count error,
  unmatched objects on each side, mean matched IoU/Dice, mean absolute matched
  area error, and split/merge counts. Foreground Dice cannot see a split, a merge
  or a count error at equal total area; these columns can. The receipt adds a
  per-source-group summary with both the pooled frame mean and an equal-group
  mean, their denominators, and no interval: a frame or object bootstrap would not
  be donor uncertainty. Agreement between two masks does not establish that
  either is correct.
- `overlays/`: original field with the supplied mask tinted for human review.
- `receipt.json` and `REPORT.md`: dataset terms, frozen split identities,
  output hashes and interpretation limits.

To show a run in ResearchDesk, use the adapter in the sibling RegenWorkbench
repository with the exact inputs recorded in that receipt:

```powershell
python ..\regen-workbench\tools\import_organoid_phenotyping.py `
  --output artifacts\bonn-kidney-cyst-measurements `
  --manifest artifacts\bonn-kidney-cyst-induction\acquisitions.csv `
  --plan artifacts\bonn-kidney-cyst-induction\study-plan.json
```

The adapter verifies the receipt and output hashes, checks the acquisition and
plan hashes, then copies bounded tables and the report into the ResearchDesk
study folder. It does not copy source images or overlays, infer cyst identity,
or mark the biological assay as measured.

## Manual annotation pilot

Run the package suite with `python -m unittest discover -s tests -v`. When
Node.js is available, this also exercises delayed task/image responses and
save-target handling in the embedded annotation desk.

`prepare-annotation-pilot` creates a 24-hour worklist with one deterministically
selected image per available development source-kidney × culture × treatment
stratum. It excludes the frozen final-test kidney and adds five differently
named repeat tasks for independent review. The task queues omit treatment and
source IDs. `curator/assignment_key.csv` contains those labels and the repeat
mapping; keep it away from annotators until both rounds are frozen. Images in
`public/images/` are byte-identical TIFF copies with opaque filenames, and the
contact sheet is only a low-resolution navigation aid.

The primary article reports QuPath 0.4.4 analysis, eight randomly selected and
tracked tubuloids per well in dome culture, and all cysts counted/measured per
well in suspension culture ([source methods](https://doi.org/10.1186/s12860-026-00591-x)).
The distributed archive has no original ROIs, per-object identities or explicit
well identifiers. The pilot's written dome/suspension targets are therefore
provisional annotation conventions, not an exact reproduction of the paper's
sampling or area rules. The pilot creates no masks or biological results.

To show its plan in ResearchDesk, run from the sibling `regen-workbench`
repository:

```powershell
python tools/import_organoid_annotation_pilot.py `
  --output ..\organoid-phenotyping\artifacts\annotation-pilot-bonn-cyst-24h-v1 `
  --manifest ..\organoid-phenotyping\artifacts\bonn-kidney-cyst-induction\acquisitions.csv `
  --plan ..\organoid-phenotyping\artifacts\bonn-kidney-cyst-induction\study-plan.json
```

The adapter verifies the pinned inputs, public files, queue contents, source
image bytes and held-out-group exclusion. It copies only the two queues, this
protocol, the pilot plan and contact sheet; full-resolution images and the
private assignment key stay in this repository's artifact pack.

After the pilot plan is registered, start a local polygon-mask session. The
session directory must be new and remain inside the source study directory;
the HTTP interface binds to loopback only. Assign different pseudonymous IDs
to reviewers when scoring concealed repeats:

```bash
organoid-phenotyping annotate \
  --pilot artifacts/annotation-pilot-bonn-cyst-24h-v1 \
  --manifest artifacts/bonn-kidney-cyst-induction/acquisitions.csv \
  --plan artifacts/bonn-kidney-cyst-induction/study-plan.json \
  --out artifacts/bonn-kidney-cyst-induction/annotation-session-v1
organoid-phenotyping audit-annotations \
  --session artifacts/bonn-kidney-cyst-induction/annotation-session-v1
```

The auditor reveals the repeat mapping only after the explicit local audit
command. It reports foreground Dice and annotator-ID independence for the
completed repeat pairs. These are segmentation-agreement measures: they do
not establish that a mask is correct or support a biological effect. If every
primary task has either a saved mask or an explicit disposition, the audit writes a new
`acquisitions-annotated-*.csv` beside the source manifest. It preserves the
original file. A mask remains marked for review; a disposition remains
`pending_annotation` and has no mask. Dispositions include no visible target,
ambiguous boundary, occluded, cropped, and unusable image, each with a required
rationale. Their revision files are hash-bound and kept separate from geometry.
The audit emits current and superseded revisions in `dispositions.csv` and
records disposition agreement separately from mask Dice. A later saved mask can
replace the task's current disposition while preserving its earlier record. Run
`measure` on the new manifest to verify the current disposition record hash;
dispositioned, pending, missing, or failed source rows remain visible and are
not measured.

To attach the audit record to the sibling RegenWorkbench ResearchDesk card:

```powershell
python tools/import_organoid_annotation_review.py `
  --audit ..\organoid-phenotyping\artifacts\bonn-kidney-cyst-induction\annotation-session-v1\audits\AUDIT_ID
```

The adapter copies only the bounded audit report, repeat and disposition tables,
and hash receipt. It never imports the private assignment key or masks, and
keeps agreement review separate from the later image-measurement run.

## Input contract

`acquisitions.csv` is UTF-8 CSV with one acquisition per row. Required columns:

| Column | Meaning |
| --- | --- |
| `frame_id` | Stable acquisition identifier; also used for overlay filenames. |
| `specimen_id` | Declared sample unit across repeated acquisitions; use a unique frame-level ID if source identities cannot be linked. An ID alone does not establish object tracking. |
| `biological_unit_id` | Source-defined higher-level group used to prevent leakage across the declared split. |
| `clone_id`, `culture_batch_id` | Biological preparation context; use `not_reported` when unavailable. |
| `imaging_lab`, `microscope_id` | Acquisition domain and instrument; use `not_reported` when unavailable. |
| `timepoint_h` | Hours from a declared study origin, finite and nonnegative. This field alone does not make repeated frames a tracked trajectory. |
| `image_path`, `mask_path` | Paths relative to the manifest directory. Measured rows require both. |
| `status` | `measured`, `pending_annotation`, `missing`, or `failed`. |
| `status_reason` | Required for pending, missing or failed frames; preserved in outputs. |

Optional disposition fields (`annotation_disposition`, rationale, task ID,
annotator ID, revision, relative record path and expected SHA-256) identify a
hash-verified annotation-desk outcome. They are allowed only on a
`pending_annotation` row and never count as a mask or measurement.

Optional columns include `pixel_size_um` with required `pixel_size_source`, `culture_condition`, `treatment`, `technical_replicate`,
`acquisition_date`, `passage`, `culture_day`, `magnification`, `source_uri`,
`license`, `segmentation_method`,
`segmentation_version`, source passage/day tokens, expected input SHA-256 values, and a
`reference_mask_path` plus `reference_mask_annotator`. Images and masks must
have matching dimensions. Masks must be single-channel integer label images;
the union of positive labels gives the frame-level area, while each distinct
positive integer label is one object in `objects.csv`. Paths must stay inside
the study directory. Files larger than 200 MB, duplicate frame IDs and hash
mismatches are rejected. Multiple fields/acquisitions may share one specimen,
timepoint and microscope.

`study-plan.json` has `schema_version: 1`, dataset metadata (`dataset_id`,
`title`, `source_url`, `license`, `retrieved_on`) and a `split` object. The split
must set `frozen_before_model_fit: true`, name a supported `grouping_field` and
`grouping_unit`, and list non-overlapping `development_group_ids` and
`final_test_group_ids`. Those lists must exactly match identities in the chosen
manifest field, preventing repeated specimens from crossing the group split. Optional
`acquisition_holdout_microscope_ids` measure acquisition transfer. If those
images share organoid IDs with development, they do **not** measure performance
on new organoids.

## Interpretation boundary

Pixel geometry is always reported for a supplied mask. Instance matching uses one
declared rule, published with every result; a different threshold or containment
fraction gives different counts, so the rule travels with the numbers. Frame-level area is the
union of positive labels; `objects.csv` reports each positive integer label as
one object, even if that label has disconnected components. Physical units
appear only when a positive pixel size is provided with provenance;
magnification is not used to infer scale. Geometry does not establish cell identity, viability,
maturation, tissue function, regenerative potency or rejuvenation. IoU and Dice
compare supplied segmentations; they do not establish that a reference
annotation is correct. Biological claims need an appropriate independent
functional assay and qualified collaborators.
