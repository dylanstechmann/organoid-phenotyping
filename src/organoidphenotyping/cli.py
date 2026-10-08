"""Command line entry point for the organoid mask measurement workflow."""

from __future__ import annotations

import argparse
import json
import sys

from organoidphenotyping.bonn_import import import_bonn_figure4
from organoidphenotyping.annotation_pilot import prepare_annotation_pilot
from organoidphenotyping.annotation_workbench import audit_annotations, create_session, serve_annotation_session, _load_session
from organoidphenotyping import mask_review
from organoidphenotyping.core import StudyError, measure_study


def _run_ledger(args):
    common = ({"actor_id": args.actor_id, "session_id": args.session_id, "recorded_utc": args.recorded_utc,
               "rationale": args.rationale} if hasattr(args, "session_id") else {})
    if args.ledger_command == "open":
        return mask_review.open_ledger(
            args.ledger, protocol_version=args.protocol_version,
            required_accepting_reviews=args.required_accepting_reviews, actor_id=args.actor_id,
            recorded_utc=args.recorded_utc, rationale=args.rationale)
    if args.ledger_command == "submit":
        return mask_review.submit(
            args.ledger, task_id=args.task_id, revision=args.revision, kind=args.kind, mask_path=args.mask_path,
            mask_sha256=args.mask_sha256, source_image_sha256=args.source_image_sha256, **common)
    if args.ledger_command == "review":
        return mask_review.review(args.ledger, task_id=args.task_id, revision=args.revision,
                                  decision=args.decision, round_number=args.round, **common)
    if args.ledger_command == "adjudicate":
        return mask_review.adjudicate(args.ledger, task_id=args.task_id, revision=args.revision,
                                      decision=args.decision, **common)
    if args.ledger_command == "summary":
        return mask_review.summary(args.ledger)
    return mask_review.export_accepted(args.ledger, args.mask_root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure organoid masks with source and specimen provenance")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure", help="validate one frozen study and write measurements and a receipt")
    measure.add_argument("--manifest", required=True, help="acquisition-aware CSV manifest")
    measure.add_argument("--plan", required=True, help="versioned source, license and specimen-split JSON")
    measure.add_argument("--out", required=True, help="new output directory")
    measure.add_argument("--tracks", default=None,
                         help="optional reviewed CSV mapping frame_id, instance_label_value and track_id; path is relative to manifest directory")
    import_bonn = sub.add_parser("import-bonn-kidney", help="verify and index the licensed Bonn Figure 4 cyst-induction archive")
    import_bonn.add_argument("--archive", required=True, help="downloaded Bonn Figure 4 ZIP file")
    import_bonn.add_argument("--out", required=True, help="new study directory for extracted source images and manifests")
    import_bonn.add_argument("--retrieved-on", default=None, help="ISO retrieval date; defaults to today")
    annotation = sub.add_parser("prepare-annotation-pilot", help="create a treatment-concealed manual review batch")
    annotation.add_argument("--manifest", required=True, help="exact acquisition CSV")
    annotation.add_argument("--plan", required=True, help="frozen source, license and split JSON")
    annotation.add_argument("--out", required=True, help="new annotation-pack directory")
    annotation.add_argument("--timepoint-h", type=int, default=24)
    annotation.add_argument("--seed", type=int, default=20261005)
    annotation.add_argument("--repeat-tasks", type=int, default=5)
    desk = sub.add_parser("annotate", help="open the loopback manual polygon-mask annotation desk")
    desk.add_argument("--pilot", required=True, help="verified blinded annotation-pilot pack")
    desk.add_argument("--manifest", required=True, help="exact acquisition manifest used by the pilot")
    desk.add_argument("--plan", required=True, help="exact frozen source-study plan")
    desk.add_argument("--out", required=True, help="new session directory inside the source-study directory")
    desk.add_argument("--port", type=int, default=8765)
    desk.add_argument("--resume", action="store_true", help="resume an existing verified session at --out")
    audit = sub.add_parser("audit-annotations", help="unblind only to score concealed-repeat mask agreement locally")
    audit.add_argument("--session", required=True, help="annotation session to audit after review is complete")
    ledger = sub.add_parser("review-ledger", help="append-only review ledger for frozen masks (not an annotation tool)")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)

    def ledger_args(name, help_text, *, task=True, revision=False, decision=False, actor=True):
        item = ledger_sub.add_parser(name, help=help_text)
        item.add_argument("--ledger", required=True, help=f"ledger file ({mask_review.LEDGER_NAME})")
        if task:
            item.add_argument("--task-id", required=True)
        if revision:
            item.add_argument("--revision", type=int, required=True)
        if decision:
            item.add_argument("--decision", required=True, choices=mask_review.DECISIONS)
        if actor:
            item.add_argument("--actor-id", required=True, help="self-reported, unauthenticated")
            item.add_argument("--session-id", required=True)
            item.add_argument("--recorded-utc", required=True, help="YYYY-MM-DDTHH:MM:SSZ, self-reported")
            item.add_argument("--rationale", required=True)
        return item

    opened = ledger_sub.add_parser("open", help="create a new ledger")
    opened.add_argument("--ledger", required=True)
    opened.add_argument("--protocol-version", required=True)
    opened.add_argument("--required-accepting-reviews", type=int, default=2)
    opened.add_argument("--actor-id", required=True)
    opened.add_argument("--recorded-utc", required=True)
    opened.add_argument("--rationale", required=True)
    submitted = ledger_args("submit", "freeze one revision", revision=True)
    submitted.add_argument("--kind", required=True, choices=mask_review.KINDS)
    submitted.add_argument("--mask-path", required=True, help="path relative to the review root")
    submitted.add_argument("--mask-sha256", required=True)
    submitted.add_argument("--source-image-sha256", required=True)
    reviewed = ledger_args("review", "record one review of the latest revision", revision=True, decision=True)
    reviewed.add_argument("--round", type=int, default=1)
    ledger_args("adjudicate", "resolve a disagreement", revision=True, decision=True)
    ledger_args("summary", "verify the chain and count task states", task=False, actor=False)
    exported = ledger_args("export-accepted", "list accepted masks whose bytes still match", task=False, actor=False)
    exported.add_argument("--mask-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "review-ledger":
            result = _run_ledger(args)
        elif args.command == "measure":
            result = measure_study(args.manifest, args.plan, args.out, args.tracks)
        elif args.command == "import-bonn-kidney":
            result = import_bonn_figure4(args.archive, args.out, retrieved_on=args.retrieved_on)
        elif args.command == "prepare-annotation-pilot":
            result = prepare_annotation_pilot(
                args.manifest, args.plan, args.out, timepoint_h=args.timepoint_h,
                seed=args.seed, repeat_tasks=args.repeat_tasks,
            )
        elif args.command == "annotate":
            if args.resume:
                _load_session(args.out)
            else:
                result = create_session(args.pilot, args.manifest, args.plan, args.out)
                json.dump(result, sys.stdout, indent=2, allow_nan=False)
                sys.stdout.write("\n")
            serve_annotation_session(args.out, port=args.port)
            return 0
        else:
            result = audit_annotations(args.session)
    except (StudyError, mask_review.ReviewError, OSError) as exc:
        parser.error(str(exc))
    json.dump(result, sys.stdout, indent=2, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
