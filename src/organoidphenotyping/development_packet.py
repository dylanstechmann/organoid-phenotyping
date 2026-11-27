"""Build and verify a bounded, development-only morphology review packet.

The packet is assembled from acquisition and intake metadata. It never opens
source images, creates masks, or records review decisions on anyone's behalf.
"""

from __future__ import annotations

import csv
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile

from organoidphenotyping import __version__
from organoidphenotyping.core import (
    MAX_MANIFEST_BYTES,
    MAX_MANIFEST_ROWS,
    SHA256_RE,
    StudyError,
    _manifest_rows,
    _read_plan,
)


PACKET_FILES = ("review_packet.csv", "REPORT.md", "receipt.json")
DISPOSITION_FIELDS = (
    "annotation_disposition",
    "annotation_disposition_rationale",
    "annotation_disposition_annotator_id",
    "annotation_disposition_task_id",
    "annotation_disposition_revision",
    "annotation_disposition_path",
    "expected_annotation_disposition_sha256",
)
MEASUREMENT_REQUIRED_FIELDS = {
    "frame_id", "specimen_id", "biological_unit_id", "status", "status_reason",
    "image_path", "mask_path", "source_uri", "license", "split", "image_sha256",
    "mask_sha256",
}
REVIEW_FIELDS = (
    "frame_id", "specimen_id", "biological_unit_id", "culture_condition",
    "treatment", "technical_replicate", "timepoint_h", "status", "status_reason",
    "source_image_path", "source_image_sha256", "source_uri", "license",
    "mask_revision", "reviewer_disposition", "reviewer_id",
    "reviewer_disposition_task_id", "reviewer_disposition_revision",
    "reviewer_disposition_rationale", "reviewer_disposition_record_path",
    "reviewer_disposition_record_sha256", "scale_availability", "pixel_size_um",
    "pixel_size_source",
)
MAX_RECEIPT_BYTES = 5_000_000
BONN_DATASET_ID = "10.60507/FK2/OM25XQ"
BONN_DEVELOPMENT_GROUPS = frozenset({"kidney_00", "kidney_39", "kidney_69", "kidney_73-25"})
BONN_HELD_OUT_GROUPS = frozenset({"kidney_51"})


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StudyError(f"duplicate intake receipt key: {key}")
        result[key] = value
    return result


def _read_intake_receipt(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise StudyError("intake receipt exceeds the 5 MB input limit")
    try:
        receipt = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyError("intake receipt must be UTF-8 JSON") from exc
    if not isinstance(receipt, dict):
        raise StudyError("intake receipt must be a JSON object")
    return receipt, raw


def _read_measurement_rows(path: Path) -> tuple[list[dict[str, str]], bytes]:
    raw = path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise StudyError("intake measurements exceed the 20 MB input limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StudyError("intake measurements must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames or []
    if len(header) != len(set(header)) or not MEASUREMENT_REQUIRED_FIELDS.issubset(header):
        raise StudyError("intake measurements lack required provenance columns")
    rows = list(reader)
    if not rows or len(rows) > MAX_MANIFEST_ROWS:
        raise StudyError("intake measurements must contain 1 to 100,000 rows")
    cleaned = []
    for line, row in enumerate(rows, 2):
        if None in row or any(value is None for value in row.values()):
            raise StudyError(f"intake measurement row {line}: ragged CSV")
        cleaned.append({key: value.strip() for key, value in row.items()})
    return cleaned, raw


def _require_hash(value: str, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise StudyError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=REVIEW_FIELDS, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _build_files(manifest_path: Path, plan_path: Path, intake_receipt_path: Path) -> dict[str, bytes]:
    manifest_rows, manifest_raw = _manifest_rows(manifest_path)
    plan, plan_raw = _read_plan(plan_path)
    intake, intake_raw = _read_intake_receipt(intake_receipt_path)
    measurement_path = intake_receipt_path.parent / "measurements.csv"
    measurement_rows, measurement_raw = _read_measurement_rows(measurement_path)

    if intake.get("input_manifest_sha256") != _sha256(manifest_raw):
        raise StudyError("intake receipt does not match the acquisition manifest SHA-256")
    if intake.get("study_plan_sha256") != _sha256(plan_raw):
        raise StudyError("intake receipt does not match the study-plan SHA-256")
    if intake.get("dataset") != plan["dataset"]:
        raise StudyError("intake receipt dataset credit or reuse terms differ from the frozen plan")
    expected_measurement_hash = _require_hash(
        intake.get("outputs", {}).get("measurements.csv", "")
        if isinstance(intake.get("outputs"), dict) else "",
        "intake receipt outputs.measurements.csv",
    )
    if expected_measurement_hash != _sha256(measurement_raw):
        raise StudyError("intake measurements.csv does not match its receipt SHA-256")

    split = plan["split"]
    if split["grouping_field"] != "biological_unit_id":
        raise StudyError("development review packet requires frozen biological_unit_id grouping")
    development_ids = set(split["development_group_ids"])
    held_out_ids = set(split["final_test_group_ids"])
    split_map = {**{value: "development" for value in development_ids},
                 **{value: "final_test" for value in held_out_ids}}
    observed_groups = {row["biological_unit_id"] for row in manifest_rows}
    if observed_groups != set(split_map):
        raise StudyError("frozen development/final-test groups must exactly match manifest biological units")
    if plan["dataset"]["dataset_id"] == BONN_DATASET_ID and (
            len(manifest_rows) != 280 or len(observed_groups) != 5
            or development_ids != BONN_DEVELOPMENT_GROUPS or held_out_ids != BONN_HELD_OUT_GROUPS):
        raise StudyError("Bonn development packet requires 280 frames, five biological units, four development units and one held-out unit")

    manifest_by_frame = {}
    specimen_groups = {}
    for row in manifest_rows:
        frame_id = row["frame_id"]
        if frame_id in manifest_by_frame:
            raise StudyError(f"duplicate manifest frame_id: {frame_id}")
        manifest_by_frame[frame_id] = row
        specimen_id = row["specimen_id"]
        group_id = row["biological_unit_id"]
        if specimen_id in specimen_groups and specimen_groups[specimen_id] != group_id:
            raise StudyError(f"specimen {specimen_id} crosses biological-unit groups")
        specimen_groups[specimen_id] = group_id
        if not row.get("source_uri", "") or not row.get("license", ""):
            raise StudyError(f"frame {frame_id}: source_uri and license provenance are required")
        if row["source_uri"] != plan["dataset"]["source_url"] or row["license"] != plan["dataset"]["license"]:
            raise StudyError(f"frame {frame_id}: source or reuse terms differ from the frozen dataset record")

    measurement_by_frame = {}
    for row in measurement_rows:
        frame_id = row["frame_id"]
        if frame_id in measurement_by_frame:
            raise StudyError(f"duplicate intake measurement frame_id: {frame_id}")
        measurement_by_frame[frame_id] = row
    if set(measurement_by_frame) != set(manifest_by_frame) or intake.get("n_manifest_rows") != len(manifest_rows):
        raise StudyError("intake measurements and receipt must cover every manifest frame exactly once")

    image_frames = {"development": {}, "final_test": {}}
    development_specimens = set()
    held_out_specimens = set()
    status_counts = Counter()
    for frame_id, manifest in manifest_by_frame.items():
        measured = measurement_by_frame[frame_id]
        for field in ("specimen_id", "biological_unit_id", "status", "status_reason",
                      "image_path", "mask_path", "source_uri", "license"):
            if measured[field] != manifest[field]:
                raise StudyError(f"frame {frame_id}: intake {field} differs from the acquisition manifest")
        expected_split = split_map[manifest["biological_unit_id"]]
        if measured["split"] != expected_split:
            raise StudyError(f"frame {frame_id}: intake split differs from the frozen biological-unit grouping")
        if expected_split == "development":
            development_specimens.add(manifest["specimen_id"])
        else:
            held_out_specimens.add(manifest["specimen_id"])
        status_counts[manifest["status"]] += 1

        image_hash = measured["image_sha256"]
        if manifest["image_path"]:
            image_hash = _require_hash(image_hash, f"frame {frame_id} source_image_sha256")
            expected_image_hash = manifest.get("expected_image_sha256", "")
            if expected_image_hash and image_hash != _require_hash(
                    expected_image_hash, f"frame {frame_id} expected_image_sha256"):
                raise StudyError(f"frame {frame_id}: intake source hash differs from manifest expectation")
            image_frames[expected_split].setdefault(image_hash, []).append(frame_id)
        elif image_hash:
            raise StudyError(f"frame {frame_id}: an image hash requires an image path")

    if development_specimens & held_out_specimens:
        raise StudyError("development and held-out specimen IDs overlap")
    shared_hashes = sorted(set(image_frames["development"]) & set(image_frames["final_test"]))
    if shared_hashes:
        raise StudyError("exact image overlap detected between development and held-out frames")

    expected_receipt_values = {
        "n_measured_frames": status_counts["measured"],
        "n_missing_frames": status_counts["missing"],
        "n_failed_frames": status_counts["failed"],
        "n_pending_annotation_frames": status_counts["pending_annotation"],
        "n_biological_units": len(observed_groups),
    }
    for field, expected in expected_receipt_values.items():
        if intake.get(field) != expected:
            raise StudyError(f"intake receipt {field} does not match the acquisition manifest")
    intake_split = intake.get("split")
    if not isinstance(intake_split, dict):
        raise StudyError("intake receipt split summary is required")
    if (intake_split.get("grouping_field") != "biological_unit_id"
            or intake_split.get("development_group_count") != len(development_ids)
            or intake_split.get("final_test_group_count") != len(held_out_ids)
            or intake_split.get("specimen_overlap") != []):
        raise StudyError("intake receipt split summary is inconsistent with the frozen grouping")

    review_rows = []
    for frame_id in sorted(development for development, row in manifest_by_frame.items()
                           if row["biological_unit_id"] in development_ids):
        manifest = manifest_by_frame[frame_id]
        measured = measurement_by_frame[frame_id]
        status = manifest["status"]
        if status not in {"pending_annotation", "missing", "failed"}:
            raise StudyError(f"frame {frame_id}: measured rows require a separate reviewed mask-revision export")
        if not manifest["status_reason"]:
            raise StudyError(f"frame {frame_id}: pending, missing and failed rows require status_reason")
        if manifest["mask_path"] or measured["mask_sha256"]:
            raise StudyError(f"frame {frame_id}: an unmeasured review row cannot contain a mask")

        disposition_values = [manifest.get(field, "") for field in DISPOSITION_FIELDS]
        if any(disposition_values):
            if status != "pending_annotation" or not all(disposition_values):
                raise StudyError(f"frame {frame_id}: reviewer disposition requires complete pending-annotation provenance")
            code, rationale, reviewer_id, task_id, revision, record_path, record_hash = disposition_values
            if not rationale.strip() or not reviewer_id.strip() or not task_id.strip() or not record_path.strip():
                raise StudyError(f"frame {frame_id}: reviewer disposition provenance cannot be blank")
            _require_hash(record_hash, f"frame {frame_id} reviewer disposition record SHA-256")
            for field in DISPOSITION_FIELDS:
                if measured.get(field, "") != manifest.get(field, ""):
                    raise StudyError(f"frame {frame_id}: intake reviewer disposition provenance is incomplete")
            reviewer_disposition = code
        else:
            reviewer_id = task_id = revision = rationale = record_path = record_hash = ""
            reviewer_disposition = "pending_review" if status == "pending_annotation" else "not_reviewed"

        pixel_size = manifest.get("pixel_size_um", "")
        pixel_source = manifest.get("pixel_size_source", "")
        if bool(pixel_size) != bool(pixel_source):
            raise StudyError(f"frame {frame_id}: pixel size and its provenance must be recorded together")
        if pixel_size:
            try:
                numeric_size = float(pixel_size)
            except ValueError as exc:
                raise StudyError(f"frame {frame_id}: pixel_size_um must be numeric") from exc
            if not math.isfinite(numeric_size) or numeric_size <= 0:
                raise StudyError(f"frame {frame_id}: pixel_size_um must be positive and finite")
            scale_availability = "available"
        else:
            scale_availability = "missing"

        review_rows.append({
            "frame_id": frame_id,
            "specimen_id": manifest["specimen_id"],
            "biological_unit_id": manifest["biological_unit_id"],
            "culture_condition": manifest.get("culture_condition", "") or "not_reported",
            "treatment": manifest.get("treatment", "") or "not_reported",
            "technical_replicate": manifest.get("technical_replicate", "") or "not_reported",
            "timepoint_h": manifest["timepoint_h"],
            "status": status,
            "status_reason": manifest["status_reason"],
            "source_image_path": manifest["image_path"],
            "source_image_sha256": measured["image_sha256"],
            "source_uri": manifest["source_uri"],
            "license": manifest["license"],
            "mask_revision": "missing",
            "reviewer_disposition": reviewer_disposition,
            "reviewer_id": reviewer_id,
            "reviewer_disposition_task_id": task_id,
            "reviewer_disposition_revision": revision,
            "reviewer_disposition_rationale": rationale,
            "reviewer_disposition_record_path": record_path,
            "reviewer_disposition_record_sha256": record_hash.lower(),
            "scale_availability": scale_availability,
            "pixel_size_um": pixel_size,
            "pixel_size_source": pixel_source,
        })

    if any(row["biological_unit_id"] in held_out_ids for row in review_rows):
        raise StudyError("held-out biological unit entered the development packet")
    review_csv = _csv_bytes(review_rows)
    development_status = Counter(row["status"] for row in review_rows)
    disposition_counts = Counter(row["reviewer_disposition"] for row in review_rows)
    scale_counts = Counter(row["scale_availability"] for row in review_rows)
    dataset = plan["dataset"]
    report_lines = [
        "# DEVELOPMENT-ONLY organoid morphology review packet", "",
        f"Dataset: {dataset['title']}",
        f"Source: {dataset['source_url']}",
        f"Recorded reuse license: {dataset['license']}",
    ]
    if dataset.get("publication_doi"):
        report_lines.append(f"Associated publication DOI: {dataset['publication_doi']}")
    if dataset.get("source_readme_license_statement"):
        report_lines.append(f"Source README license statement: {dataset['source_readme_license_statement']}")
    report_lines += [
        "",
        f"Development frames: {len(review_rows)} of {len(manifest_rows)}; "
        f"development biological units: {len(development_ids)}; held-out biological units excluded: {len(held_out_ids)}.",
        f"Manifest SHA-256: `{_sha256(manifest_raw)}`.",
        f"Study-plan SHA-256: `{_sha256(plan_raw)}`.",
        f"V2 intake receipt SHA-256: `{_sha256(intake_raw)}`.",
        f"V2 intake measurements SHA-256: `{_sha256(measurement_raw)}`.", "",
        "## Review availability", "",
        f"- Mask revision `missing`: {sum(row['mask_revision'] == 'missing' for row in review_rows)}.",
        *[f"- Reviewer disposition `{key}`: {disposition_counts[key]}." for key in sorted(disposition_counts)],
        *[f"- Scale availability `{key}`: {scale_counts[key]}." for key in sorted(scale_counts)],
        "- No annotation, mask revision, reviewer decision, or calibration has been inferred or created.", "",
        "## Split and source-hash validation", "",
        f"- Manifest frames checked: {len(manifest_rows)}; biological units: {len(observed_groups)}.",
        "- Biological-unit group overlap: 0; specimen overlap: 0; exact cross-split image-hash overlap: 0.",
        "- Exact overlap uses the source SHA-256 values bound to the v2 intake receipt. Re-encoded, resized, or cropped copies can have different hashes.",
        "- Held-out image contents were not opened. No source images, masks, overlays, or weights are copied into this packet.", "",
        "## Interpretation boundary", "",
        "This packet supports review of future supplied-mask morphology measurements only. It performs no segmentation and establishes no viability, cell identity, maturation, tissue function, potency, or rejuvenation. Missing scale prevents physical-unit measurement; magnification is not calibration.", "",
    ]
    report_bytes = "\n".join(report_lines).encode("utf-8")
    receipt = {
        "schema_version": 1,
        "packet_type": "DEVELOPMENT-ONLY organoid morphology review",
        "tool": "organoid-phenotyping",
        "tool_version": __version__,
        "dataset": dataset,
        "inputs": {
            "manifest_sha256": _sha256(manifest_raw),
            "study_plan_sha256": _sha256(plan_raw),
            "intake_receipt_sha256": _sha256(intake_raw),
            "intake_measurements_sha256": _sha256(measurement_raw),
            "source_hash_basis": "Pinned v2 intake measurements; packet generation reads no source image bytes.",
        },
        "development": {
            "grouping_field": "biological_unit_id",
            "grouping_unit": split["grouping_unit"],
            "group_ids": sorted(development_ids),
            "frame_count": len(review_rows),
            "status_counts": {key: development_status[key] for key in sorted(development_status)},
            "mask_revision_counts": {"missing": len(review_rows)},
            "reviewer_disposition_counts": {key: disposition_counts[key] for key in sorted(disposition_counts)},
            "scale_availability_counts": {key: scale_counts[key] for key in sorted(scale_counts)},
        },
        "validation": {
            "manifest_frame_count": len(manifest_rows),
            "biological_unit_count": len(observed_groups),
            "development_group_count": len(development_ids),
            "held_out_group_count": len(held_out_ids),
            "held_out_frames_in_packet": 0,
            "group_overlap_count": 0,
            "specimen_overlap_count": 0,
            "exact_image_overlap_count": 0,
            "held_out_image_contents_inspected": False,
        },
        "third_party_assets_copied": False,
        "outputs": {
            "review_packet.csv": _sha256(review_csv),
            "REPORT.md": _sha256(report_bytes),
        },
        "limitations": [
            "No annotation, mask, reviewer disposition, or pixel calibration is created by this packet.",
            "The packet supports supplied-mask morphology measurement only; it is not segmentation, viability, maturation, function, or potency evidence.",
            "Exact-image overlap compares SHA-256 file identities and does not detect re-encoded, resized, or cropped copies.",
            "The held-out biological unit remains sealed; only its group count is reported, without held-out frame IDs, paths, hashes, or image contents.",
        ],
    }
    receipt_bytes = (json.dumps(receipt, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return {"review_packet.csv": review_csv, "REPORT.md": report_bytes, "receipt.json": receipt_bytes}


def prepare_development_packet(manifest: str | Path, plan: str | Path,
                               intake_receipt: str | Path, output: str | Path) -> dict:
    """Write a deterministic development-only packet without reading image content."""
    manifest_path = Path(manifest).resolve(strict=True)
    plan_path = Path(plan).resolve(strict=True)
    intake_path = Path(intake_receipt).resolve(strict=True)
    output_path = Path(output).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise StudyError(f"output already exists: {output_path}")
    files = _build_files(manifest_path, plan_path, intake_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_path.name}-", dir=output_path.parent) as temporary:
        staged = Path(temporary)
        for name in PACKET_FILES:
            (staged / name).write_bytes(files[name])
        staged.rename(output_path)
    return json.loads(files["receipt.json"])


def validate_development_packet(packet: str | Path, manifest: str | Path, plan: str | Path,
                                intake_receipt: str | Path) -> dict:
    """Require an exact, complete regeneration of all three packet text files."""
    packet_path = Path(packet)
    if packet_path.is_symlink() or not packet_path.is_dir():
        raise StudyError("development packet must be a real directory")
    packet_path = packet_path.resolve(strict=True)
    expected = _build_files(
        Path(manifest).resolve(strict=True), Path(plan).resolve(strict=True),
        Path(intake_receipt).resolve(strict=True),
    )
    entries = list(packet_path.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise StudyError("development packet may contain only regular text files")
    if {entry.name for entry in entries} != set(PACKET_FILES):
        raise StudyError("development packet must contain exactly review_packet.csv, REPORT.md and receipt.json")
    for name in PACKET_FILES:
        if (packet_path / name).read_bytes() != expected[name]:
            raise StudyError(f"development packet {name} fails exact regeneration validation")
    result = json.loads(expected["receipt.json"])
    result["packet_validation"] = "valid"
    return result
