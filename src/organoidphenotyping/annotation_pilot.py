"""Create a treatment-concealed, source-pinned manual annotation pilot."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import tempfile

from PIL import Image, ImageDraw, ImageFont

from organoidphenotyping import __version__
from organoidphenotyping.core import StudyError


DATASET_ID = "10.60507/FK2/OM25XQ"
SOURCE_LICENSE = "CC-BY-4.0"
TIMEPOINT_H = 24
REPEAT_TASKS = 5
PROTOCOL_VERSION = "bonn-cyst-annotation-pilot-0.1"
PUBLIC_FILES = (
    "annotation_queue_round1.csv",
    "annotation_queue_round2.csv",
    "annotation_protocol.md",
    "pilot_plan.json",
    "annotation_contact_sheet.png",
)
QUEUE_FIELDS = (
    "task_id", "image_path", "image_sha256", "annotation_target", "mask_path",
    "status", "status_reason", "annotator_id", "annotation_protocol_version",
)
KEY_FIELDS = (
    "task_id", "round", "frame_id", "biological_unit_id", "culture_condition",
    "treatment", "technical_replicate", "timepoint_h", "source_image_path",
    "source_image_sha256", "repeat_of_task_id",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StudyError(f"expected a JSON object in {path}")
    return value


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _rank(seed: int, frame_id: str, purpose: str) -> str:
    return hashlib.sha256(f"{purpose}|{seed}|{frame_id}".encode("utf-8")).hexdigest()


def _task_id(salt: str, frame_id: str, round_number: int) -> str:
    return "b-" + hashlib.sha256(f"{salt}|{frame_id}|{round_number}".encode("utf-8")).hexdigest()[:16]


def _source_rows(manifest_path: Path, plan: dict, timepoint_h: int, seed: int,
                 repeat_tasks: int) -> tuple[list[dict[str, str]], list[dict[str, str]], dict]:
    try:
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"frame_id", "biological_unit_id", "culture_condition", "treatment",
                        "technical_replicate", "timepoint_h", "image_path", "status"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise StudyError("acquisition manifest is missing fields required for the frozen annotation pilot")
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise StudyError(f"cannot read acquisition manifest {manifest_path}: {exc}") from exc
    if not rows:
        raise StudyError("acquisition manifest has no rows")
    frame_ids = [row.get("frame_id", "").strip() for row in rows]
    if any(not frame_id for frame_id in frame_ids) or len(set(frame_ids)) != len(frame_ids):
        raise StudyError("acquisition manifest frame IDs must be nonblank and unique")

    dataset = plan.get("dataset")
    split = plan.get("split")
    archive_hash = dataset.get("source_archive_sha256") if isinstance(dataset, dict) else None
    if (plan.get("schema_version") != 1 or not isinstance(dataset, dict)
            or dataset.get("dataset_id") != DATASET_ID or dataset.get("license") != SOURCE_LICENSE
            or not isinstance(archive_hash, str) or len(archive_hash) != 64
            or any(character not in "0123456789abcdef" for character in archive_hash.lower())):
        raise StudyError("study plan does not match the pinned CC BY Bonn kidney-tubuloid dataset")
    if (not isinstance(split, dict) or split.get("frozen_before_model_fit") is not True
            or split.get("grouping_field") != "biological_unit_id"):
        raise StudyError("study plan must contain the frozen source-kidney split")
    development = split.get("development_group_ids")
    final_test = split.get("final_test_group_ids")
    if (not isinstance(development, list) or not development or not isinstance(final_test, list)
            or any(not isinstance(value, str) or not value for value in development + final_test)
            or len(set(development)) != len(development) or len(set(final_test)) != len(final_test)
            or set(development) & set(final_test)):
        raise StudyError("study plan development and final-test groups must be unique and disjoint")
    observed_groups = {row.get("biological_unit_id", "").strip() for row in rows}
    if observed_groups != set(development) | set(final_test):
        raise StudyError("study plan groups do not exactly match the acquisition manifest")

    root = manifest_path.parent.resolve(strict=True)
    candidates = []
    for row in rows:
        try:
            point = float(row.get("timepoint_h", ""))
        except ValueError as exc:
            raise StudyError(f"invalid timepoint for frame {row['frame_id']}") from exc
        if point != point or abs(point) == float("inf"):
            raise StudyError(f"non-finite timepoint for frame {row['frame_id']}")
        group_id = row.get("biological_unit_id", "").strip()
        if row.get("status", "").strip() != "pending_annotation" or point != timepoint_h or group_id not in development:
            continue
        culture = row.get("culture_condition", "").strip()
        treatment = row.get("treatment", "").strip()
        if culture not in {"Domes", "Suspension"} or treatment not in {"DMSO", "Forskolin", "Media"}:
            raise StudyError(f"unsupported pilot stratum for frame {row['frame_id']}")
        image_value = row.get("image_path", "").strip()
        image_rel = Path(image_value)
        if not image_value or image_rel.is_absolute() or ".." in image_rel.parts:
            raise StudyError(f"unsafe or missing source image path for frame {row['frame_id']}")
        image_path = (root / image_rel).resolve(strict=True)
        try:
            image_path.relative_to(root)
        except ValueError as exc:
            raise StudyError(f"source image escapes the acquisition directory for frame {row['frame_id']}") from exc
        if not image_path.is_file() or image_path.suffix.lower() not in {".tif", ".tiff"}:
            raise StudyError(f"pilot source must be a TIFF image for frame {row['frame_id']}")
        try:
            with Image.open(image_path) as image:
                image.verify()
        except OSError as exc:
            raise StudyError(f"cannot decode source image for frame {row['frame_id']}: {exc}") from exc
        candidates.append({
            **row,
            "frame_id": row["frame_id"].strip(),
            "biological_unit_id": group_id,
            "culture_condition": culture,
            "treatment": treatment,
            "technical_replicate": row.get("technical_replicate", "").strip(),
            "timepoint_h": str(timepoint_h),
            "image_path": image_value,
            "_resolved_image": str(image_path),
            "_sha256": _sha256_file(image_path),
        })

    strata: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in candidates:
        key = (row["biological_unit_id"], row["culture_condition"], row["treatment"])
        strata.setdefault(key, []).append(row)
    selected = [min(group, key=lambda row: _rank(seed, row["frame_id"], "stratum"))
                for _key, group in sorted(strata.items())]
    development_groups = sorted({row["biological_unit_id"] for row in selected})
    if not selected or set(row["biological_unit_id"] for row in selected) & set(final_test):
        raise StudyError("no eligible development images were found, or final-test images entered the pilot")
    if repeat_tasks < len(development_groups) or repeat_tasks > len(selected):
        raise StudyError("repeat task count must cover every represented development group and fit the selected batch")

    repeats = []
    for group_id in development_groups:
        group_rows = [row for row in selected if row["biological_unit_id"] == group_id]
        repeats.append(min(group_rows, key=lambda row: _rank(seed, row["frame_id"], "repeat-group")))
    if len(repeats) < repeat_tasks:
        repeated_ids = {row["frame_id"] for row in repeats}
        extra_candidates = [row for row in selected if row["frame_id"] not in repeated_ids]
        # Prefer one repeat from a second culture mode when a development group
        # contains both modes; this checks the two target definitions.
        for group_id in development_groups:
            group_rows = [row for row in selected if row["biological_unit_id"] == group_id]
            primary_repeat = next((row for row in repeats if row["biological_unit_id"] == group_id), None)
            if primary_repeat is None:
                continue
            alternatives = [row for row in group_rows if row["culture_condition"] != primary_repeat["culture_condition"]
                            and row["frame_id"] not in repeated_ids]
            if alternatives:
                repeats.append(min(alternatives, key=lambda row: _rank(seed, row["frame_id"], "repeat-mode")))
                break
        repeated_ids = {row["frame_id"] for row in repeats}
        extra_candidates = [row for row in selected if row["frame_id"] not in repeated_ids]
        while len(repeats) < repeat_tasks:
            if not extra_candidates:
                raise StudyError("not enough selected source images for the requested blinded repeats")
            next_row = min(extra_candidates, key=lambda row: _rank(seed, row["frame_id"], "repeat-extra"))
            repeats.append(next_row)
            extra_candidates.remove(next_row)

    return selected, repeats, {"development": development, "final_test": final_test, "dataset": dataset,
                               "development_groups": development_groups, "strata_count": len(strata)}


def _queue_row(task_id: str, image_sha256: str, target: str) -> dict[str, str]:
    return {
        "task_id": task_id,
        "image_path": f"images/{task_id}.tif",
        "image_sha256": image_sha256,
        "annotation_target": target,
        "mask_path": f"masks/{task_id}.tif",
        "status": "pending_annotation",
        "status_reason": "Awaiting manual annotation; no mask has been generated.",
        "annotator_id": "",
        "annotation_protocol_version": PROTOCOL_VERSION,
    }


def _write_contact_sheet(path: Path, tasks: list[dict[str, str]], images_dir: Path) -> None:
    columns = 3
    cell_width, cell_height, image_height = 320, 260, 225
    rows = (len(tasks) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, task in enumerate(tasks):
        with Image.open(images_dir / f"{task['task_id']}.tif") as image:
            preview = image.convert("RGB")
            preview.thumbnail((cell_width - 16, image_height - 12), Image.Resampling.LANCZOS)
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        left = x + (cell_width - preview.width) // 2
        top = y + max(4, (image_height - preview.height) // 2)
        sheet.paste(preview, (left, top))
        draw.text((x + 8, y + image_height + 10), task["task_id"], fill="black", font=font)
    # The sheet is a navigation aid, not a source of measurements. Keep its
    # pixels and palette small so a worklist does not add a multi-megabyte
    # photographic montage to the reproducible study record.
    sheet.quantize(colors=64).save(path, format="PNG", optimize=True)


def prepare_annotation_pilot(manifest: str | Path, plan_path: str | Path, output: str | Path,
                             *, timepoint_h: int = TIMEPOINT_H, seed: int = 20261005,
                             repeat_tasks: int = REPEAT_TASKS) -> dict:
    """Build a small, development-only manual annotation packet without labels."""
    manifest_path = Path(manifest).resolve(strict=True)
    plan_file = Path(plan_path).resolve(strict=True)
    plan = _json(plan_file)
    if isinstance(timepoint_h, bool) or not isinstance(timepoint_h, int) or timepoint_h < 0:
        raise StudyError("timepoint_h must be a nonnegative integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise StudyError("seed must be a nonnegative integer")
    if isinstance(repeat_tasks, bool) or not isinstance(repeat_tasks, int) or repeat_tasks < 1:
        raise StudyError("repeat_tasks must be a positive integer")
    target_dir = Path(output).absolute()
    if target_dir.exists() or target_dir.is_symlink():
        raise StudyError(f"output already exists: {target_dir}")

    selected, repeats, metadata = _source_rows(manifest_path, plan, timepoint_h, seed, repeat_tasks)
    source_manifest_sha256 = _sha256_file(manifest_path)
    study_plan_sha256 = _sha256_file(plan_file)
    salt = secrets.token_hex(32)
    task_lookup: dict[str, dict[str, str]] = {}
    primary_tasks: list[dict[str, str]] = []
    repeat_task_rows: list[dict[str, str]] = []
    key_rows: list[dict[str, str]] = []
    for row in selected:
        task_id = _task_id(salt, row["frame_id"], 1)
        target = "whole_tubuloid_outer_boundary" if row["culture_condition"] == "Domes" else "visible_cyst_boundary"
        task_lookup[row["frame_id"]] = {"task_id": task_id, "target": target}
        primary_tasks.append(_queue_row(task_id, row["_sha256"], target))
        key_rows.append({
            "task_id": task_id, "round": "primary", "frame_id": row["frame_id"],
            "biological_unit_id": row["biological_unit_id"], "culture_condition": row["culture_condition"],
            "treatment": row["treatment"], "technical_replicate": row["technical_replicate"],
            "timepoint_h": row["timepoint_h"], "source_image_path": row["image_path"],
            "source_image_sha256": row["_sha256"], "repeat_of_task_id": "",
        })
    for row in repeats:
        primary = task_lookup[row["frame_id"]]
        task_id = _task_id(salt, row["frame_id"], 2)
        repeat_task_rows.append(_queue_row(task_id, row["_sha256"], primary["target"]))
        key_rows.append({
            "task_id": task_id, "round": "concealed_repeat", "frame_id": row["frame_id"],
            "biological_unit_id": row["biological_unit_id"], "culture_condition": row["culture_condition"],
            "treatment": row["treatment"], "technical_replicate": row["technical_replicate"],
            "timepoint_h": row["timepoint_h"], "source_image_path": row["image_path"],
            "source_image_sha256": row["_sha256"], "repeat_of_task_id": primary["task_id"],
        })

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target_dir.name}-", dir=target_dir.parent) as temporary:
        stage = Path(temporary)
        public = stage / "public"
        curator = stage / "curator"
        images_dir = public / "images"
        images_dir.mkdir(parents=True)
        curator.mkdir()
        task_records = [(row, "primary") for row in selected] + [(row, "concealed_repeat") for row in repeats]
        for row, round_name in task_records:
            task_key = task_lookup[row["frame_id"]]["task_id"] if round_name == "primary" else next(
                item["task_id"] for item in key_rows
                if item["frame_id"] == row["frame_id"] and item["round"] == "concealed_repeat"
            )
            source = Path(row["_resolved_image"])
            destination = images_dir / f"{task_key}.tif"
            shutil.copyfile(source, destination)
            if _sha256_file(destination) != row["_sha256"]:
                raise StudyError(f"copied image hash mismatch for blinded task {task_key}")
        _write_csv(public / "annotation_queue_round1.csv", QUEUE_FIELDS, primary_tasks)
        _write_csv(public / "annotation_queue_round2.csv", QUEUE_FIELDS, repeat_task_rows)
        _write_contact_sheet(public / "annotation_contact_sheet.png", primary_tasks, images_dir)

        protocol = f"""# Bonn kidney-tubuloid annotation pilot

Protocol version: `{PROTOCOL_VERSION}`

## Purpose and scope

This is a manual annotation usability and repeatability pilot on source images. It is not a treatment-effect analysis, model evaluation, or replication of the paper's reported measurements. The selected 24-hour images come only from development source-kidney groups. The frozen final-test group was excluded.

The source methods report QuPath 0.4.4 analysis, selecting and tracking eight random tubuloids per well in dome culture, and counting/measuring all cysts per well in suspension culture. The distributed image archive has no original object masks, per-object identities, or explicit well identifiers. The targets below are provisional operational targets and must not be described as exact source-paper reproductions without recovering and checking the supplementary object/area rules.

## Provisional annotation targets

- **Dome culture:** outline the visible outer envelope of each clearly separable whole tubuloid. Do not draw the lumen as a separate object.
- **Suspension culture:** outline each clearly separable visible cyst as its own object. Do not label an undifferentiated aggregate as a cyst when its boundary is unclear.
- Use background label `0`; assign each object a distinct positive integer label. Preserve the source image width and height. Save one single-channel integer TIFF mask per task at the `mask_path` listed in its queue.
- If an object boundary is ambiguous, partly outside the field, occluded, or not represented by a stable visible edge, do not invent the contour. Record the issue and ask for review; do not silently discard a difficult field.
- Record annotation software and version, annotator ID, segmentation method, and protocol version with each completed mask. No pixel calibration is available; report pixel geometry only until a calibration source is supplied.

## Review design

Round 1 contains one selected image per available development-kidney × culture × treatment stratum at 24 hours. Round 2 contains five differently named repeat images. Give the repeat queue to an independent reviewer when possible. If one annotator completes both rounds, separate them in time and do not reveal the repeat mapping. The assignment key is private and must be withheld until both annotation sets are frozen.

The filenames and queues conceal source treatment labels, but visible morphology may suggest a condition; this is not guaranteed blinding. The contact sheet is a low-resolution navigation aid only. Annotate the full-resolution TIFFs in `images/` and never draw on the source images.

## Handoff

After both rounds are complete, preserve original masks and hashes, use the private key to join tasks back to acquisitions, compare repeat masks, adjudicate disagreements, and update the acquisition manifest with reviewed mask paths. Keep the existing final-test kidney group untouched until the segmentation method and review rules are frozen. Importing this plan into ResearchDesk does not mark the assay measured.
"""
        (public / "annotation_protocol.md").write_text(protocol, encoding="utf-8", newline="\n")
        pilot_plan = {
            "schema_version": 1,
            "pilot_id": "bonn-kidney-cyst-annotation-24h-v1",
            "purpose": "Manual mask-workflow usability and repeatability; no biological effect estimate.",
            "dataset_id": DATASET_ID,
            "license": SOURCE_LICENSE,
            "source_archive_sha256": metadata["dataset"]["source_archive_sha256"],
            "acquisition_manifest_sha256": source_manifest_sha256,
            "study_plan_sha256": study_plan_sha256,
            "selection": {
                "timepoint_h": timepoint_h,
                "seed": seed,
                "rule": "Select the lowest SHA-256 rank of seed and frame ID within each available development biological-unit × culture-condition × treatment stratum at the fixed timepoint.",
                "n_unique_frames": len(selected),
                "n_round1_tasks": len(primary_tasks),
                "n_round2_concealed_repeat_tasks": len(repeat_task_rows),
                "n_total_tasks": len(primary_tasks) + len(repeat_task_rows),
                "n_development_source_groups_represented": len(metadata["development_groups"]),
                "final_test_group_excluded": True,
            },
            "annotation_protocol_version": PROTOCOL_VERSION,
            "masks_generated": False,
            "biological_results_generated": False,
            "blinding_limit": "Treatment labels and source filenames are withheld from queues; appearance may reveal condition.",
        }
        (public / "pilot_plan.json").write_text(json.dumps(pilot_plan, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        _write_csv(curator / "assignment_key.csv", KEY_FIELDS, key_rows)
        key_hash = _sha256_file(curator / "assignment_key.csv")
        public_hashes = {name: _sha256_file(public / name) for name in PUBLIC_FILES}
        receipt = {
            "schema_version": 1,
            "tool": "organoid-phenotyping",
            "activity": "manual_annotation_pilot_plan",
            "tool_version": __version__,
            "dataset": {
                "dataset_id": DATASET_ID,
                "license": SOURCE_LICENSE,
                "source_archive_sha256": metadata["dataset"]["source_archive_sha256"],
            },
            "input_manifest_sha256": source_manifest_sha256,
            "study_plan_sha256": study_plan_sha256,
            "timepoint_h": timepoint_h,
            "selection_seed": seed,
            "grouping_field": "biological_unit_id",
            "grouping_unit": "source kidney identifier",
            "n_unique_frames": len(selected),
            "n_round1_tasks": len(primary_tasks),
            "n_round2_concealed_repeat_tasks": len(repeat_task_rows),
            "n_total_tasks": len(primary_tasks) + len(repeat_task_rows),
            "n_development_source_groups": len(metadata["development_groups"]),
            "excluded_final_test_group_ids": sorted(metadata["final_test"]),
            "selected_final_test_overlap": [],
            "masks_generated": False,
            "biological_results_generated": False,
            "outputs": public_hashes,
            "private_assignment_key_sha256": key_hash,
            "private_assignment_key_path": "curator/assignment_key.csv",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }
        (curator / "pilot_receipt.json").write_text(json.dumps(receipt, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists() or target_dir.is_symlink():
            raise StudyError(f"output already exists: {target_dir}")
        try:
            # Publish the complete pack atomically after all files and hashes are ready.
            stage.rename(target_dir)
        except FileExistsError as exc:
            raise StudyError(f"output already exists: {target_dir}") from exc
    return receipt
