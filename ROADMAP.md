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
treatment result. The GitHub remote (dylanstechmann/organoid-phenotyping) exists as of
2026-10-08; CI is not yet configured (a workflow patch is pending the
`workflow` token scope).

## O1 — Make annotation task state reliable

**Task-switch protection is implemented locally.** Every selection now has a
generation identity, so a delayed image or saved-mask response cannot replace
the current task. Controls pause during loads/saves, saves use a frozen task and
contour snapshot, and navigation asks before dropping unsaved edits.

**Reasoned non-mask dispositions are implemented locally.** Annotators can
record no visible target, ambiguous boundary, occluded, cropped or unusable
outcomes with a rationale. Each disposition is an immutable revision tied to
the source-image hash and reviewer; a later mask can supersede it without
deleting the disposition record. Audits count a disposition as task completion
without calling it a mask, and they report disposition agreement separately
from foreground Dice. The exported manifest keeps the row pending, carries the
relative disposition record path and hash, and the measurement command verifies
both the hash and fields before reporting it.

- Remaining: add original-resolution tiles or pyramid display. The endpoint
  downsamples previews to at most 1600 × 1600. The new display-scale control
  enlarges that preview; it does not add image detail. Polygon coordinates
  remain normalized against preview dimensions and are rasterized on the
  source-image mask, so original registration stays intact.
- A Node UI harness now delays image/mask responses and checks stale-task
  rejection, cancelled edits, frozen save targets, dispositions and zoom
  behavior. Add a real browser integration test when this package has a browser
  test runtime.
- Add original-resolution coordinate round-trip checks when full-resolution
  tiles arrive.
- Add vertex dragging, contrast controls and clearer instance IDs. Vertex
  placement, whole-polygon removal, undo and the unsaved-edit guard are present.
- Add explicit disposition-level unresolved and excluded counts to the
  ResearchDesk review card; pending-disposition rows stay outside every
  measured-mask count.

**Acceptance:** late loads cannot replace another task; a cancelled edit remains
visible; attempted navigation during save is blocked; zoom never claims detail
above the downloaded pixels; full-resolution tiles preserve mask coordinates;
empty/excluded tasks stay counted.

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

**Implemented 2026-10-08 (`mask_review.py`, `organoid-phenotyping review-ledger`):** an
append-only, hash-chained ledger. A submission freezes one revision (mask hash, source-image
hash, annotator, session, protocol version, rationale); an edit is a new revision with a
different hash. Reviews need a reviewer and session different from the annotation; a revision is
`accepted` at the ledger's required count of accepting reviews, `rejected` on a rejection with no
acceptance, and a mix is a `disagreement` that only an uninvolved adjudicator resolves. Only the
latest revision counts, and `export-accepted` returns accepted masks whose bytes still match their
frozen hash and lists everything else, with the reason, as excluded (never ground truth).
Identities and clocks are self-reported, so none of this shows independent review.
- Remaining: wire the annotation desk to submit into the ledger; round and permission separation
  across sessions; blinded-until-frozen unblinding gates; `measure` reading the export instead of
  the manifest's mask paths; a reviewed-geometry flag in the measurement outputs.

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

**Instance-level agreement is implemented.** `instance_agreement.csv` reports
one-to-one matching by descending IoU at a declared threshold, matched counts,
signed count error, unmatched objects on each side, mean matched IoU/Dice, mean
absolute matched area error, and split/merge counts under a separate declared
containment rule. Frame-level foreground Dice is retained unchanged. The receipt
carries a per-source-group summary with both the pooled frame mean and an
equal-group mean, specimen and frame denominators per group, and an explicit
`uncertainty: not_reported`. Frames where no pair reached the threshold are
counted separately rather than scored as zero or as perfect, and empty masks are
reported as empty rather than as perfect agreement. Tests cover identical,
split, merged, below-threshold, missed/extra, unscored and malformed cases, plus
an end-to-end run where the pooled and equal-group means differ.

- Remaining: panoptic-style summary quality, per-object matched-boundary error,
  and agreement computed across more than two annotators at once.
- Add uncertainty only when independent-group counts and assumptions support
  it. A frame/object bootstrap is not donor uncertainty. **Still not reported;
  the current cohorts have too few independent groups.**
- Keep repeatability, reference agreement and geometry separate from viability,
  maturation, treatment response and regenerative potency.

**Acceptance:** empty masks, unmatched objects and exclusions remain visible;
tables expose group counts, missingness and each interval's independent unit.

## O5 — Harden geometry and tracking semantics

The current correction suppresses pixel-area growth when scale signatures are
unknown and disallows pixel comparison across calibrated/uncalibrated
transitions.

- Continue to verify scale metadata provenance; a reported microscope name and
  magnification alone cannot establish calibration or same optical settings.
- Reject incompatible physical units and retain gaps rather than interpolate
  failed frames.
- Define polygon overlap rules; review current last-polygon-wins behavior.
  Check self-intersections, degenerate contours, disconnected/negative labels
  and empty foreground where appropriate.
- Bind track identity, acquisition metadata and uncertainty to receipts.

**Acceptance:** missing scale cannot support a growth claim; track identity is
not inferred from label order; malformed geometry fails or has a documented
disposition.

## O6 — Publish and integrate a reproducible methods package

- Configure CI (the repository exists) and a synthetic example that
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
tracks integration across the related projects.
