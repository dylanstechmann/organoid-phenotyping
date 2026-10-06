# Development roadmap

Code review: 2026-10-06. The next milestone is a reviewed real microscopy
benchmark with correct measurement units and a reproducible ResearchDesk
dossier. New segmentation/health-classifier families are lower priority.

## Current baseline

The package measures supplied masks, preserves source/acquisition provenance,
freezes source-group splits, exports morphology/overlays and compares reference
masks. Tracked-object outputs require a reviewed track map. A local polygon
pilot, concealed repeats and agreement audits also exist.

The first intake is the [Bonn kidney-tubuloid dataset](https://doi.org/10.60507/FK2/OM25XQ)
and [associated study](https://doi.org/10.1186/s12860-026-00591-x). Source images
have no supplied instance masks. Original well identities, selected ROIs,
pixel calibration and object tracks must not be invented from filenames.
The pilot/audit is not a completed independent review or measured biological
treatment result. No GitHub remote is configured at this review date;
publication is a separate outstanding task.

## O1 — Make annotation task state reliable

**First blocker before serious labeling.** Task selection is asynchronous:
older image/annotation requests can finish after a newer selection. Pin each
request, rendered image and save to the same task generation/identity.

- Cancel/ignore stale loads; reject saves until the selected image and contours
  are ready; preserve edits when navigation is interrupted.
- Add original-resolution zoom/pan or tiles and coordinate round-trip checks.
  Current previews shrink images to at most 1600 × 1600 while masks use
  original dimensions; a preview is not full-resolution boundary review.
- Add vertex/instance editing, contrast, labels, undo and an unsaved edit guard.
- Support no-visible-target, ambiguous, occluded, cropped and unusable task
  dispositions with rationale. Requiring a polygon for every image biases
  intake toward visible/successful objects.

**Acceptance:** out-of-order loads cannot save contours to the wrong task;
coordinates survive reload; edits are guarded; empty/excluded tasks stay counted.

## O2 — Freeze masks and implement review/adjudication

- Introduce draft → submitted/frozen → accepted/rejected/adjudicated states.
  Keep revision history, mask hashes, protocol version, reviewer, timestamp
  and rationale. Edits create new revisions.
- Distinguish geometry available from reviewed geometry. Current `measured`
  status can coexist with `annotation_review_needed=True`; downstream exports
  must not interpret it as accepted annotation.
- Separate review sessions/permissions/rounds and freeze before unblinding.
  Distinct pseudonyms alone do not demonstrate independent review.
- Export accepted masks/dispositions for benchmarks; preserve unresolved and
  rejected versions for audits without using them as ground truth.

**Acceptance:** benchmark inputs resolve to accepted frozen revisions; later
edits cannot overwrite review; human review remains distinct from verified bytes.

## O3 — Complete a real development case before final-test evaluation

Depends on O1–O2 and genuine reviewer input.

1. Write an annotation rubric, source/license card, ambiguity rules and the
   intended independent unit.
2. Recover original well/ROI/calibration/track records where accessible;
   otherwise retain missingness and narrow the analysis.
3. Freeze preprocessing, exclusions, outcomes and source-group split. Keep the
   final-test source kidney sealed until methods/review policy are frozen.
4. Annotate/review a development subset, report every disposition, measure
   accepted masks and produce reproducible figures.
5. Export review, geometry and benchmark receipts into ResearchDesk with source
   references and accepted-mask ancestry.

**Acceptance:** each result traces to source bytes, accepted revision and
independent unit. Without original selected ROIs/well IDs, call this a
descriptive reanalysis, not exact reproduction of the paper's selected-object
analysis or evidence of a biological treatment effect.

## O4 — Evaluate agreement at the correct level

- Retain foreground Dice; add instance matching, count/area error, split/merge
  rates and matched-object overlap with declared matching rules.
- Report by source group with equal-group summaries. Donor/well/image/object
  denominators differ; adjacent frames are not independent donors.
- Add uncertainty only when independent-group counts and assumptions support
  it. A frame/object bootstrap is not donor uncertainty.
- Keep repeatability, reference agreement and geometry separate from viability,
  maturation, treatment response and regenerative potency.

**Acceptance:** empty masks, unmatched objects and exclusions remain visible;
tables expose group counts, missingness and each interval's independent unit.

## O5 — Harden geometry and tracking semantics

- Suppress pixel-area growth when acquisition scale signatures are only
  `not_reported`. Unknown microscope/magnification does not establish comparable
  scale; require verified same scale or compatible calibrated units.
- Reject mixed calibrated/uncalibrated comparisons and incompatible units;
  retain gaps rather than interpolate failed frames.
- Define polygon overlap rules; review current last-polygon-wins behavior.
  Check self-intersections, degenerate contours, disconnected/negative labels
  and empty foreground where appropriate.
- Bind track identity, acquisition metadata and uncertainty to receipts.

**Acceptance:** missing scale cannot support a growth claim; track identity is
not inferred from label order; malformed geometry fails or has a documented
disposition.

## O6 — Publish and integrate a reproducible methods package

- Configure the intended GitHub repository, CI and a synthetic example that
  runs from a fresh wheel install with CLI smoke checks.
- Document exact licensed downloads/hashes; keep large third-party archives
  and private annotations out of Git.
- Export benchmark-ready geometry/features to `regen-benchmark-kit`, which
  owns grouped predictive evaluation; link receipts/results in ResearchDesk.
- Distinguish intact bytes, resolved ancestry and accepted scientific review
  in the portable dossier verifier.
- Use `organoid-oxygen-lab` only with reviewed physical-size assumptions and
  uncertainty: 2D area does not directly provide spherical radius or uptake.
  Functional/single-cell extensions need specific measurable questions and a
  data-access audit; controlled-access RNA-seq is not presumed downloadable.

**Acceptance:** clean-install example, licensed source documentation, reviewed
development case and independently verifiable linked results.

## Order

O1/O5 correctness → O2 review lifecycle → O3 development case → O4 grouped
agreement → O6 publication/integration. Packaging/source-access checks can
proceed while awaiting human review. Synthetic audits cannot substitute for
accepted real masks. The [RegenWorkbench roadmap](https://github.com/dylanstechmann/regen-workbench/blob/main/ROADMAP.md)
tracks integration and the broader portfolio.
