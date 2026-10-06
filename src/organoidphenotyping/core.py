"""Measure reviewed organoid masks without assigning biological identity or potency."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from PIL import Image

from organoidphenotyping import __version__


MANIFEST_COLUMNS = {
    "frame_id", "specimen_id", "biological_unit_id", "clone_id", "culture_batch_id",
    "imaging_lab", "microscope_id", "timepoint_h", "image_path", "mask_path",
    "status", "status_reason",
}
OPTIONAL_COLUMNS = {
    "segmentation_method", "segmentation_version", "reference_mask_path",
    "reference_mask_annotator", "source_uri", "license", "expected_image_sha256",
    "expected_mask_sha256", "expected_reference_sha256", "pixel_size_um", "pixel_size_source",
    "culture_condition", "treatment", "technical_replicate", "acquisition_date",
    "passage", "culture_day", "source_passage_day_token",
    "post_condition_passage_day_token", "magnification", "annotation_task_id",
    "mask_annotator_id", "mask_annotator_role", "annotation_protocol_version",
}
UNKNOWN = "not_reported"
MAX_IMAGE_BYTES = 200_000_000
MAX_IMAGE_PIXELS = 100_000_000
MAX_MANIFEST_BYTES = 20_000_000
MAX_MANIFEST_ROWS = 100_000
MAX_TRACK_MAP_BYTES = 20_000_000
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class StudyError(ValueError):
    """An input study cannot be measured under the declared contract."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest_rows(manifest_path: Path) -> tuple[list[dict[str, str]], bytes]:
    raw = manifest_path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise StudyError("manifest exceeds the 20 MB input limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StudyError("manifest must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames or []
    if len(header) != len(set(header)) or not MANIFEST_COLUMNS.issubset(header):
        raise StudyError("manifest needs unique columns including " + ", ".join(sorted(MANIFEST_COLUMNS)))
    unknown = set(header) - MANIFEST_COLUMNS - OPTIONAL_COLUMNS
    if unknown:
        raise StudyError("unsupported manifest columns: " + ", ".join(sorted(unknown)))
    rows = list(reader)
    if not rows:
        raise StudyError("manifest has no acquisition rows")
    if len(rows) > MAX_MANIFEST_ROWS:
        raise StudyError("manifest exceeds the 100,000-row input limit")
    for line, row in enumerate(rows, 2):
        if None in row or any(value is None for value in row.values()):
            raise StudyError(f"row {line}: ragged manifest")
        rows[line - 2] = {key: value.strip() for key, value in row.items()}
    return rows, raw


def _read_track_map(track_map_path: Path | None) -> tuple[list[dict[str, str]], bytes | None]:
    if track_map_path is None:
        return [], None
    raw = track_map_path.read_bytes()
    if len(raw) > MAX_TRACK_MAP_BYTES:
        raise StudyError("object track map exceeds the 20 MB input limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StudyError("object track map must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"frame_id", "instance_label_value", "track_id"}
    header = reader.fieldnames or []
    if len(header) != len(set(header)) or set(header) != required:
        raise StudyError("object track map needs exactly frame_id, instance_label_value and track_id columns")
    rows = list(reader)
    if len(rows) > MAX_MANIFEST_ROWS:
        raise StudyError("object track map exceeds the 100,000-row input limit")
    if not rows:
        raise StudyError("object track map has no object track rows")
    seen = set()
    for line, row in enumerate(rows, 2):
        if None in row or any(value is None for value in row.values()):
            raise StudyError(f"track-map row {line}: ragged CSV")
        row = {key: value.strip() for key, value in row.items()}
        if not ID_RE.fullmatch(row["frame_id"]) or not ID_RE.fullmatch(row["track_id"]):
            raise StudyError(f"track-map row {line}: frame_id and track_id must be simple identifiers")
        try:
            label = int(row["instance_label_value"])
        except ValueError as exc:
            raise StudyError(f"track-map row {line}: instance_label_value must be a positive integer") from exc
        if label <= 0 or str(label) != row["instance_label_value"]:
            raise StudyError(f"track-map row {line}: instance_label_value must be a canonical positive integer")
        key = (row["frame_id"], label)
        if key in seen:
            raise StudyError(f"track-map row {line}: duplicate frame and instance label")
        seen.add(key)
        row["instance_label_value"] = label
        rows[line - 2] = row
    return rows, raw


def _read_plan(plan_path: Path) -> tuple[dict, bytes]:
    raw = plan_path.read_bytes()

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StudyError(f"duplicate plan key: {key}")
            result[key] = value
        return result

    try:
        plan = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyError("study plan must be UTF-8 JSON") from exc
    if (not isinstance(plan, dict) or isinstance(plan.get("schema_version"), bool)
            or plan.get("schema_version") != 1):
        raise StudyError("study plan must be an object with schema_version 1")
    dataset = plan.get("dataset")
    split = plan.get("split")
    if not isinstance(dataset, dict) or not isinstance(split, dict):
        raise StudyError("study plan needs dataset and split objects")
    for field in ("dataset_id", "title", "source_url", "license", "retrieved_on"):
        if not isinstance(dataset.get(field), str) or not dataset[field].strip():
            raise StudyError(f"study plan dataset.{field} is required")
    parsed_url = urlparse(dataset["source_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise StudyError("dataset.source_url must be an http(s) source link")
    if split.get("frozen_before_model_fit") is not True:
        raise StudyError("split.frozen_before_model_fit must be true")
    if split.get("grouping_field") not in {"specimen_id", "biological_unit_id", "clone_id", "culture_batch_id"}:
        raise StudyError("split.grouping_field must select specimen_id, biological_unit_id, clone_id or culture_batch_id")
    if not isinstance(split.get("grouping_unit"), str) or not split["grouping_unit"].strip():
        raise StudyError("split.grouping_unit must name the independence level")
    for field in ("development_group_ids", "final_test_group_ids"):
        values = split.get(field)
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not ID_RE.fullmatch(value) for value in values)
                or len(values) != len(set(values))):
            raise StudyError(f"split.{field} must be a nonempty list of unique group IDs")
    development = set(split["development_group_ids"])
    final_test = set(split["final_test_group_ids"])
    if development & final_test:
        raise StudyError("development and final-test specimen IDs overlap")
    microscopes = split.get("acquisition_holdout_microscope_ids", [])
    if (not isinstance(microscopes, list)
            or any(not isinstance(value, str) or not ID_RE.fullmatch(value) for value in microscopes)
            or len(microscopes) != len(set(microscopes))):
        raise StudyError("acquisition_holdout_microscope_ids must be a list of unique IDs")
    return plan, raw


def _resolve_file(root: Path, value: str, field: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise StudyError(f"{field} must be a relative path within the study directory")
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise StudyError(f"{field} is missing or resolves outside the study directory") from exc
    if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise StudyError(f"{field} must be a regular file no larger than 200 MB")
    return path


def _load_image(path: Path, field: str, expected_hash: str = ""):
    raw = path.read_bytes()
    if expected_hash:
        if not SHA256_RE.fullmatch(expected_hash) or _sha256(raw) != expected_hash.lower():
            raise StudyError(f"{field} SHA-256 does not match the manifest")
    try:
        image = Image.open(io.BytesIO(raw))
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise StudyError(f"{field} exceeds the 100-megapixel decode limit")
        image.load()
    except (OSError, ValueError) as exc:
        if isinstance(exc, StudyError):
            raise
        raise StudyError(f"{field} cannot be decoded as an image") from exc
    return raw, image


def _measure_foreground(foreground: np.ndarray, pixel_size_um: float | None) -> dict:
    area_pixels = int(np.count_nonzero(foreground))
    if area_pixels == 0:
        raise StudyError("segmentation label has no foreground pixels")
    padded = np.pad(foreground, 1, mode="constant", constant_values=False)
    center = padded[1:-1, 1:-1]
    perimeter_pixels = sum(
        int(np.count_nonzero(center & ~neighbor))
        for neighbor in (padded[:-2, 1:-1], padded[2:, 1:-1], padded[1:-1, :-2], padded[1:-1, 2:])
    )
    equivalent_diameter_pixels = math.sqrt(4.0 * area_pixels / math.pi)
    pixel_area_um2 = pixel_size_um * pixel_size_um if pixel_size_um is not None else None
    if pixel_area_um2 is not None and not math.isfinite(pixel_area_um2):
        raise StudyError("pixel_size_um is too large for finite physical-area measurements")
    area_um2 = area_pixels * pixel_area_um2 if pixel_area_um2 is not None else None
    perimeter_um = perimeter_pixels * pixel_size_um if pixel_size_um is not None else None
    equivalent_diameter_um = equivalent_diameter_pixels * pixel_size_um if pixel_size_um is not None else None
    circularity = 4.0 * math.pi * area_pixels / perimeter_pixels ** 2 if perimeter_pixels else None
    if any(value is not None and not math.isfinite(value)
           for value in (area_um2, perimeter_um, equivalent_diameter_um, circularity)):
        raise StudyError("pixel_size_um yields non-finite physical measurements")
    height, width = foreground.shape
    return {
        "area_pixels": area_pixels,
        "area_um2": area_um2,
        "equivalent_diameter_pixels": equivalent_diameter_pixels,
        "equivalent_diameter_um": equivalent_diameter_um,
        "perimeter_pixels": perimeter_pixels,
        "perimeter_um": perimeter_um,
        "circularity": circularity,
        "touches_image_edge": bool(foreground[0, :].any() or foreground[-1, :].any()
                                    or foreground[:, 0].any() or foreground[:, -1].any()),
        "foreground_fraction": area_pixels / (height * width),
    }


def _mask_measurement(mask: Image.Image, pixel_size_um: float | None) -> dict:
    values = np.asarray(mask)
    if values.ndim != 2 or values.dtype.kind not in "uib":
        raise StudyError("segmentation masks must be single-channel integer label images")
    foreground = values > 0
    labels = np.unique(values[foreground])
    return {"foreground_label_value_count": int(len(labels)),
            **_measure_foreground(foreground, pixel_size_um)}


def _mask_object_rows(mask: Image.Image, pixel_size_um: float | None) -> list[dict]:
    values = np.asarray(mask)
    if values.ndim != 2 or values.dtype.kind not in "uib":
        raise StudyError("segmentation masks must be single-channel integer label images")
    return [{"instance_label_value": int(label),
             **_measure_foreground(values == label, pixel_size_um)}
            for label in np.unique(values) if label > 0]


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_overlay(image_bytes: bytes, foreground: np.ndarray, path: Path) -> None:
    with Image.open(io.BytesIO(image_bytes)) as source:
        base = source.convert("RGB")
    tint = Image.new("RGBA", base.size, (30, 170, 230, 0))
    alpha = Image.fromarray(np.where(foreground, 86, 0).astype(np.uint8), mode="L")
    tint.putalpha(alpha)
    Image.alpha_composite(base.convert("RGBA"), tint).convert("RGB").save(path)


def _cross_sectional_summary(frame_rows: list[dict], object_rows: list[dict]) -> list[dict]:
    """Summarize object areas across acquisitions without implying object tracking."""
    frames = {row["frame_id"]: row for row in frame_rows}
    grouped: dict[tuple, dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))
    object_counts: dict[tuple, int] = defaultdict(int)
    frame_ids: dict[tuple, set[str]] = defaultdict(set)
    for obj in object_rows:
        frame = frames[obj["frame_id"]]
        key = (frame["biological_unit_id"], frame["clone_id"], frame["culture_batch_id"],
               frame["culture_condition"], frame["treatment"], frame["imaging_lab"],
               frame["microscope_id"], frame["magnification"], frame["timepoint_h"], frame["split"])
        specimen = frame["specimen_id"]
        grouped[key][specimen][obj["frame_id"]].append(obj)
        object_counts[key] += 1
        frame_ids[key].add(obj["frame_id"])
    result = []
    for key, by_specimen in sorted(grouped.items()):
        specimen_medians_pixels = []
        specimen_medians_um2 = []
        for by_frame in by_specimen.values():
            frame_medians_pixels = [float(np.median([obj["area_pixels"] for obj in values]))
                                     for values in by_frame.values()]
            specimen_medians_pixels.append(float(np.median(frame_medians_pixels)))
            frame_medians_um2 = [float(np.median([obj["area_um2"] for obj in values
                                                  if obj["area_um2"] is not None]))
                                  for values in by_frame.values()
                                  if any(obj["area_um2"] is not None for obj in values)]
            if frame_medians_um2:
                specimen_medians_um2.append(float(np.median(frame_medians_um2)))
        result.append({
            "biological_unit_id": key[0], "clone_id": key[1], "culture_batch_id": key[2],
            "culture_condition": key[3], "treatment": key[4], "imaging_lab": key[5],
            "microscope_id": key[6], "magnification": key[7], "timepoint_h": key[8], "split": key[9],
            "n_specimen_units": len(specimen_medians_pixels), "n_acquisitions": len(frame_ids[key]),
            "n_objects": object_counts[key],
            "mean_specimen_median_object_area_pixels": float(np.mean(specimen_medians_pixels)),
            "median_specimen_median_object_area_pixels": float(np.median(specimen_medians_pixels)),
            "mean_specimen_median_object_area_um2": float(np.mean(specimen_medians_um2)) if specimen_medians_um2 else None,
            "median_specimen_median_object_area_um2": float(np.median(specimen_medians_um2)) if specimen_medians_um2 else None,
            "interpretation": "Cross-sectional mask objects; no longitudinal object identity is inferred.",
        })
    return result


def _tracked_object_growth(track_rows: list[dict], object_rows: list[dict]) -> list[dict]:
    object_lookup = {(row["frame_id"], row["instance_label_value"]): row for row in object_rows}
    trajectories: dict[tuple[str, str], list[dict]] = defaultdict(list)
    frame_track_pairs = set()
    for track in track_rows:
        key = (track["frame_id"], track["instance_label_value"])
        obj = object_lookup.get(key)
        if obj is None:
            raise StudyError(f"track map references no measured instance: {key[0]} label {key[1]}")
        pair = (track["frame_id"], track["track_id"])
        if pair in frame_track_pairs:
            raise StudyError(f"one tracked object maps to multiple instances in frame {track['frame_id']}")
        frame_track_pairs.add(pair)
        trajectories[(obj["specimen_id"], track["track_id"])].append(obj)
    result = []
    for (specimen_id, track_id), observations in sorted(trajectories.items()):
        by_timepoint: dict[float, list[dict]] = defaultdict(list)
        for obj in observations:
            by_timepoint[obj["timepoint_h"]].append(obj)
        prior = None
        for timepoint, values in sorted(by_timepoint.items()):
            area_pixels = float(np.median([obj["area_pixels"] for obj in values]))
            calibrated_values = [obj["area_um2"] for obj in values if obj["area_um2"] is not None]
            area_um2 = float(np.median(calibrated_values)) if calibrated_values else None
            signatures = {(obj["microscope_id"], obj["magnification"]) for obj in values}
            signature = next(iter(signatures)) if len(signatures) == 1 else None
            if prior and area_um2 is not None and prior["area_um2"] is not None and prior["area_um2"] > 0:
                growth, basis = 100 * (area_um2 / prior["area_um2"] - 1), "calibrated_area_um2"
            elif (prior and area_um2 is None and signature is not None and signature == prior["signature"]
                  and prior["area_pixels"] > 0):
                growth, basis = 100 * (area_pixels / prior["area_pixels"] - 1), "same_microscope_and_magnification_pixel_area"
            else:
                growth, basis = None, "not_comparable_without_shared_scale"
            first = values[0]
            result.append({
                "specimen_id": specimen_id, "track_id": track_id,
                "biological_unit_id": first["biological_unit_id"],
                "culture_condition": first["culture_condition"], "treatment": first["treatment"],
                "timepoint_h": timepoint, "n_tracked_acquisitions": len(values),
                "median_area_pixels": area_pixels, "median_area_um2": area_um2,
                "growth_percent_since_prior_tracked_timepoint": growth,
                "growth_basis": basis,
            })
            prior = {"area_pixels": area_pixels, "area_um2": area_um2, "signature": signature}
    return result


def measure_study(manifest_path: str | Path, plan_path: str | Path, output_path: str | Path,
                  track_map_path: str | Path | None = None) -> dict:
    """Measure supplied masks and write cross-sectional results plus optional reviewed tracks."""
    with tempfile.TemporaryDirectory(prefix="organoid-overlay-cache-") as overlay_directory:
        return _measure_study_impl(manifest_path, plan_path, output_path, track_map_path,
                                   Path(overlay_directory))


def _measure_study_impl(manifest_path: str | Path, plan_path: str | Path, output_path: str | Path,
                        track_map_path: str | Path | None, overlay_root: Path) -> dict:
    manifest_path = Path(manifest_path).resolve(strict=True)
    plan_path = Path(plan_path).resolve(strict=True)
    output_path = Path(output_path).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise StudyError(f"output already exists: {output_path}")
    root = manifest_path.parent.resolve()
    rows, manifest_raw = _manifest_rows(manifest_path)
    plan, plan_raw = _read_plan(plan_path)
    resolved_track_map = (_resolve_file(root, str(track_map_path), "track map")
                          if track_map_path is not None else None)
    track_rows, track_raw = _read_track_map(resolved_track_map)
    split = plan["split"]
    development = set(split["development_group_ids"])
    final_test = set(split["final_test_group_ids"])
    split_map = {**{value: "development" for value in development},
                 **{value: "final_test" for value in final_test}}
    grouping_field = split["grouping_field"]
    specimen_metadata = {}
    seen_frame_ids = set()
    frame_rows, comparison_rows = [], []
    object_rows = []
    overlay_rows = []
    acquisition_holdout_ids = set(split.get("acquisition_holdout_microscope_ids", []))
    observed_microscopes, all_specimens, all_groups = set(), set(), set()

    for line, row in enumerate(rows, 2):
        frame_id = row["frame_id"]
        specimen_id = row["specimen_id"]
        if not ID_RE.fullmatch(frame_id) or not ID_RE.fullmatch(specimen_id):
            raise StudyError(f"row {line}: frame_id and specimen_id must be simple stable identifiers")
        if frame_id in seen_frame_ids:
            raise StudyError(f"row {line}: duplicate frame_id")
        seen_frame_ids.add(frame_id)
        for key in ("biological_unit_id", "clone_id", "culture_batch_id", "imaging_lab", "microscope_id"):
            if not row[key]:
                raise StudyError(f"row {line}: {key} must be recorded or set to {UNKNOWN!r}")
        for key in ("culture_condition", "treatment", "technical_replicate", "magnification"):
            row.setdefault(key, UNKNOWN)
            row[key] = row[key] or UNKNOWN
        specimen_design = (row["biological_unit_id"], row["clone_id"], row["culture_batch_id"],
                           row["culture_condition"], row["treatment"], row["technical_replicate"])
        if specimen_id in specimen_metadata and specimen_metadata[specimen_id] != specimen_design:
            raise StudyError(f"row {line}: biological, culture and treatment metadata must stay attached to specimen {specimen_id}")
        specimen_metadata[specimen_id] = specimen_design
        all_specimens.add(specimen_id)
        group_id = row[grouping_field]
        if not group_id:
            raise StudyError(f"row {line}: selected split group field {grouping_field} is empty")
        all_groups.add(group_id)
        observed_microscopes.add(row["microscope_id"])
        try:
            timepoint = float(row["timepoint_h"])
        except ValueError as exc:
            raise StudyError(f"row {line}: timepoint_h must be numeric") from exc
        if not math.isfinite(timepoint) or timepoint < 0:
            raise StudyError(f"row {line}: timepoint_h must be finite and nonnegative")
        status = row["status"]
        if status not in {"measured", "missing", "failed", "pending_annotation"}:
            raise StudyError(f"row {line}: status must be measured, missing, failed or pending_annotation")
        record = {
            "frame_id": frame_id, "specimen_id": specimen_id,
            "biological_unit_id": row["biological_unit_id"],
            "clone_id": row["clone_id"], "culture_batch_id": row["culture_batch_id"],
            "culture_condition": row["culture_condition"], "treatment": row["treatment"],
            "technical_replicate": row["technical_replicate"],
            "acquisition_date": row.get("acquisition_date", ""),
            "passage": row.get("passage", ""), "culture_day": row.get("culture_day", ""),
            "source_passage_day_token": row.get("source_passage_day_token", ""),
            "post_condition_passage_day_token": row.get("post_condition_passage_day_token", ""),
            "magnification": row.get("magnification", UNKNOWN) or UNKNOWN,
            "imaging_lab": row["imaging_lab"], "microscope_id": row["microscope_id"],
            "timepoint_h": timepoint, "status": status,
            "status_reason": row.get("status_reason", ""),
            "image_path": row["image_path"], "mask_path": row["mask_path"],
            "source_uri": row.get("source_uri", ""),
            "license": row.get("license", ""),
            "segmentation_method": row.get("segmentation_method", UNKNOWN) or UNKNOWN,
            "segmentation_version": row.get("segmentation_version", UNKNOWN) or UNKNOWN,
            "annotation_task_id": row.get("annotation_task_id", ""),
            "mask_annotator_id": row.get("mask_annotator_id", ""),
            "mask_annotator_role": row.get("mask_annotator_role", ""),
            "annotation_protocol_version": row.get("annotation_protocol_version", ""),
            "quality_flags": "",
            "split": split_map.get(group_id, "unassigned"),
            "image_sha256": "", "mask_sha256": "", "foreground_label_value_count": None,
            "pixel_size_um": None, "area_pixels": None, "area_um2": None,
            "equivalent_diameter_pixels": None, "equivalent_diameter_um": None,
            "perimeter_pixels": None, "perimeter_um": None, "circularity": None, "touches_image_edge": None,
            "foreground_fraction": None,
        }
        if status in {"missing", "failed", "pending_annotation"}:
            if not record["status_reason"]:
                raise StudyError(f"row {line}: missing, failed and pending_annotation frames require status_reason")
            if status == "missing" and (row["image_path"] or row["mask_path"]):
                raise StudyError(f"row {line}: a missing frame cannot include image or mask paths")
            if row["mask_path"]:
                raise StudyError(f"row {line}: non-measured frames cannot carry a measured segmentation mask")
            if any(row.get(field, "") for field in ("annotation_task_id", "mask_annotator_id", "mask_annotator_role", "annotation_protocol_version")):
                raise StudyError(f"row {line}: non-measured frames cannot carry annotation provenance")
            if status == "pending_annotation" and not row["image_path"]:
                raise StudyError(f"row {line}: pending_annotation frames require an image")
            if row["image_path"]:
                image_path = _resolve_file(root, row["image_path"], "image_path")
                image_raw, _image = _load_image(image_path, "image", row.get("expected_image_sha256", ""))
                record["image_sha256"] = _sha256(image_raw)
        else:
            if not row["image_path"] or not row["mask_path"]:
                raise StudyError(f"row {line}: measured frames require image_path and mask_path")
            annotation_values = [row.get(field, "").strip() for field in (
                "annotation_task_id", "mask_annotator_id", "mask_annotator_role", "annotation_protocol_version")]
            if any(annotation_values) and not all(annotation_values):
                raise StudyError(f"row {line}: annotation task, annotator, role and protocol must be recorded together")
            if annotation_values[2] and annotation_values[2] not in {"primary", "repeat", "other"}:
                raise StudyError(f"row {line}: mask_annotator_role must be primary, repeat or other")
            pixel_text = row.get("pixel_size_um", "").strip()
            pixel_source = row.get("pixel_size_source", "").strip()
            pixel_size = None
            if pixel_text:
                try:
                    pixel_size = float(pixel_text)
                except ValueError as exc:
                    raise StudyError(f"row {line}: pixel_size_um must be numeric when supplied") from exc
                if not math.isfinite(pixel_size) or pixel_size <= 0:
                    raise StudyError(f"row {line}: pixel_size_um must be positive and finite")
                if not pixel_source:
                    raise StudyError(f"row {line}: pixel_size_um requires pixel_size_source provenance")
            image_path = _resolve_file(root, row["image_path"], "image_path")
            mask_path = _resolve_file(root, row["mask_path"], "mask_path")
            image_raw, image = _load_image(image_path, "image", row.get("expected_image_sha256", ""))
            mask_raw, mask = _load_image(mask_path, "mask", row.get("expected_mask_sha256", ""))
            if image.size != mask.size:
                raise StudyError(f"row {line}: image and mask dimensions differ")
            measures = _mask_measurement(mask, pixel_size)
            record.update(measures, image_sha256=_sha256(image_raw), mask_sha256=_sha256(mask_raw),
                          pixel_size_um=pixel_size, pixel_size_source=pixel_source)
            for instance in _mask_object_rows(mask, pixel_size):
                object_rows.append({
                    "frame_id": frame_id, "specimen_id": specimen_id,
                    "biological_unit_id": row["biological_unit_id"],
                    "clone_id": row["clone_id"], "culture_batch_id": row["culture_batch_id"],
                    "culture_condition": row["culture_condition"], "treatment": row["treatment"],
                    "technical_replicate": row["technical_replicate"],
                    "imaging_lab": row["imaging_lab"], "microscope_id": row["microscope_id"],
                    "magnification": row.get("magnification", UNKNOWN) or UNKNOWN,
                    "timepoint_h": timepoint, "split": record["split"],
                    "mask_sha256": record["mask_sha256"], **instance,
                })
            reference_path_value = row.get("reference_mask_path", "")
            if reference_path_value:
                reference_path = _resolve_file(root, reference_path_value, "reference_mask_path")
                reference_raw, reference = _load_image(reference_path, "reference mask", row.get("expected_reference_sha256", ""))
                if reference.size != mask.size:
                    raise StudyError(f"row {line}: predicted and reference masks differ in dimensions")
                predicted_values = np.asarray(mask)
                reference_values = np.asarray(reference)
                if (predicted_values.ndim != 2 or reference_values.ndim != 2
                        or predicted_values.dtype.kind not in "uib"
                        or reference_values.dtype.kind not in "uib"):
                    raise StudyError(f"row {line}: masks must be single-channel integer label images")
                predicted = predicted_values > 0
                target = reference_values > 0
                intersection = int(np.count_nonzero(predicted & target))
                union = int(np.count_nonzero(predicted | target))
                predicted_area = int(np.count_nonzero(predicted))
                target_area = int(np.count_nonzero(target))
                comparison_rows.append({
                    "frame_id": frame_id, "specimen_id": specimen_id,
                    "clone_id": row["clone_id"], "culture_batch_id": row["culture_batch_id"],
                    "imaging_lab": row["imaging_lab"], "microscope_id": row["microscope_id"],
                    "timepoint_h": timepoint, "split": record["split"],
                    "mask_path": row["mask_path"], "reference_mask_path": reference_path_value,
                    "mask_sha256": _sha256(mask_raw), "reference_mask_sha256": _sha256(reference_raw),
                    "reference_mask_annotator": row.get("reference_mask_annotator", UNKNOWN) or UNKNOWN,
                    "intersection_over_union": intersection / union if union else 1.0,
                    "dice": 2 * intersection / (predicted_area + target_area) if predicted_area + target_area else 1.0,
                    "absolute_area_error_pixels": abs(predicted_area - target_area),
                    "absolute_area_error_um2": abs(predicted_area - target_area) * pixel_size ** 2 if pixel_size is not None else None,
                })
            overlay_path = overlay_root / f"{frame_id}.png"
            _write_overlay(image_raw, np.asarray(mask) > 0, overlay_path)
            overlay_rows.append((frame_id, overlay_path))
        if status == "measured" and measures["touches_image_edge"]:
            record["quality_flags"] = "mask_touches_image_boundary_inspect_possible_crop_truncation"
        frame_rows.append(record)

    if all_groups != set(split_map):
        missing_from_plan = all_groups - set(split_map)
        absent_from_manifest = set(split_map) - all_groups
        raise StudyError(
            "frozen development/final-test group IDs must exactly match manifest groups "
            f"(unassigned={sorted(missing_from_plan)}, absent={sorted(absent_from_manifest)})"
        )
    unknown_microscopes = acquisition_holdout_ids - observed_microscopes
    if unknown_microscopes:
        raise StudyError(f"acquisition holdout microscope IDs absent from the manifest: {sorted(unknown_microscopes)}")
    development_specimens = {row["specimen_id"] for row in frame_rows if row["split"] == "development"}
    final_test_specimens = {row["specimen_id"] for row in frame_rows if row["split"] == "final_test"}
    heldout_microscope_specimens = {row["specimen_id"] for row in frame_rows
                                    if row["microscope_id"] in acquisition_holdout_ids}
    split_report = {
        "grouping_field": grouping_field,
        "grouping_unit": split["grouping_unit"],
        "development_group_count": len(development),
        "final_test_group_count": len(final_test),
        "development_specimen_count": len(development_specimens),
        "final_test_specimen_count": len(final_test_specimens),
        "specimen_overlap": sorted(development_specimens & final_test_specimens),
        "microscope_holdout_ids": sorted(acquisition_holdout_ids),
        "microscope_holdout_shared_specimens_with_development": sorted(heldout_microscope_specimens & development_specimens),
        "microscope_holdout_shared_specimens_with_final_test": sorted(heldout_microscope_specimens & final_test_specimens),
        "microscope_holdout_interpretation": (
            "Acquisition transfer only; if specimen IDs overlap, these images do not demonstrate performance on new biological specimens."
            if acquisition_holdout_ids else "No microscope-specific holdout was declared."
        ),
    }
    summary_rows = _cross_sectional_summary(frame_rows, object_rows)
    tracked_growth_rows = _tracked_object_growth(track_rows, object_rows)
    objects_by_key = {(obj["frame_id"], obj["instance_label_value"]): obj for obj in object_rows}
    trajectory_count = len({(objects_by_key[(track["frame_id"], track["instance_label_value"])]
                             ["specimen_id"], track["track_id"]) for track in track_rows})
    manifest_hash, plan_hash = _sha256(manifest_raw), _sha256(plan_raw)
    report = {
        "schema_version": 1,
        "tool": "organoid-phenotyping",
        "tool_version": __version__,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": plan["dataset"],
        "input_manifest_sha256": manifest_hash,
        "study_plan_sha256": plan_hash,
        "n_manifest_rows": len(frame_rows),
        "n_measured_frames": sum(row["status"] == "measured" for row in frame_rows),
        "n_missing_frames": sum(row["status"] == "missing" for row in frame_rows),
        "n_failed_frames": sum(row["status"] == "failed" for row in frame_rows),
        "n_pending_annotation_frames": sum(row["status"] == "pending_annotation" for row in frame_rows),
        "n_biological_units": len(all_groups),
        "object_tracking": {
            "status": "reviewed_track_map_supplied" if resolved_track_map is not None else "no_track_map",
            "track_map_path": str(resolved_track_map.relative_to(root)) if resolved_track_map is not None else None,
            "track_map_sha256": _sha256(track_raw) if track_raw is not None else None,
            "n_tracked_object_observations": len(track_rows),
            "n_tracked_object_trajectories": trajectory_count,
        },
        "split": split_report,
        "outputs": {},
        "limitations": [
            "Mask measurements describe the supplied segmentation; this package does not segment images or validate a mask as biologically correct.",
            "Area, diameter, shape and growth do not establish viability, identity, maturation, regenerative potency or rejuvenation.",
            "Repeated images and microscope acquisitions in one declared specimen unit are nested observations, not independent biological units.",
            "Object areas are cross-sectional unless a reviewed object-to-track map explicitly links labels across acquisitions.",
            "Physical area is reported only when pixel_size_um is supplied with provenance; otherwise only pixel geometry is available.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_path.name}-", dir=output_path.parent) as temporary:
        staged = Path(temporary)
        measurement_fields = [
            "frame_id", "specimen_id", "biological_unit_id", "clone_id", "culture_batch_id",
            "culture_condition", "treatment", "technical_replicate", "acquisition_date", "passage",
            "culture_day", "source_passage_day_token", "post_condition_passage_day_token",
            "imaging_lab", "microscope_id", "magnification", "timepoint_h", "status",
            "status_reason", "image_path", "mask_path", "source_uri", "license", "segmentation_method",
            "segmentation_version", "annotation_task_id", "mask_annotator_id", "mask_annotator_role",
            "annotation_protocol_version", "quality_flags", "split", "pixel_size_um", "foreground_label_value_count",
            "pixel_size_source",
            "area_pixels", "area_um2", "equivalent_diameter_pixels", "equivalent_diameter_um",
            "perimeter_pixels", "perimeter_um", "circularity",
            "touches_image_edge", "foreground_fraction", "image_sha256", "mask_sha256",
        ]
        _write_csv(staged / "measurements.csv", frame_rows, measurement_fields)
        _write_csv(staged / "cross_sectional_summary.csv", summary_rows, [
            "biological_unit_id", "clone_id", "culture_batch_id", "culture_condition", "treatment",
            "imaging_lab", "microscope_id", "magnification", "timepoint_h", "split",
            "n_specimen_units", "n_acquisitions", "n_objects",
            "mean_specimen_median_object_area_pixels", "median_specimen_median_object_area_pixels",
            "mean_specimen_median_object_area_um2", "median_specimen_median_object_area_um2", "interpretation",
        ])
        _write_csv(staged / "tracked_object_growth.csv", tracked_growth_rows, [
            "specimen_id", "track_id", "biological_unit_id", "culture_condition", "treatment",
            "timepoint_h", "n_tracked_acquisitions", "median_area_pixels", "median_area_um2",
            "growth_percent_since_prior_tracked_timepoint", "growth_basis",
        ])
        _write_csv(staged / "objects.csv", object_rows, [
            "frame_id", "specimen_id", "biological_unit_id", "culture_condition", "treatment",
            "technical_replicate", "timepoint_h", "split", "mask_sha256", "instance_label_value",
            "area_pixels", "area_um2", "equivalent_diameter_pixels", "equivalent_diameter_um",
            "perimeter_pixels", "perimeter_um", "circularity", "touches_image_edge", "foreground_fraction",
        ])
        _write_csv(staged / "segmentation_comparison.csv", comparison_rows, [
            "frame_id", "specimen_id", "clone_id", "culture_batch_id", "imaging_lab", "microscope_id",
            "timepoint_h", "split", "mask_path", "reference_mask_path", "mask_sha256", "reference_mask_sha256",
            "reference_mask_annotator", "intersection_over_union", "dice", "absolute_area_error_pixels",
            "absolute_area_error_um2",
        ])
        overlay_directory = staged / "overlays"
        if overlay_rows:
            overlay_directory.mkdir()
            for frame_id, cached_overlay in overlay_rows:
                shutil.copyfile(cached_overlay, overlay_directory / f"{frame_id}.png")
        for relative in ("measurements.csv", "cross_sectional_summary.csv", "tracked_object_growth.csv",
                         "objects.csv", "segmentation_comparison.csv"):
            report["outputs"][relative] = _sha256((staged / relative).read_bytes())
        report["outputs"]["overlays"] = {
            frame_id: _sha256((overlay_directory / f"{frame_id}.png").read_bytes())
            for frame_id, _cached_overlay in overlay_rows
        }
        report_lines = [
            f"# {plan['dataset']['title']} — mask measurement receipt", "",
            f"Source: {plan['dataset']['source_url']} (license recorded as {plan['dataset']['license']}; terms remain the user's responsibility).",
            f"Manifest SHA-256: `{manifest_hash}`. Study-plan SHA-256: `{plan_hash}`.", "",
            f"Measured frames: {report['n_measured_frames']}; pending annotation: {report['n_pending_annotation_frames']}; missing: {report['n_missing_frames']}; failed: {report['n_failed_frames']}.",
            f"Specimens: {len(all_specimens)}; {split['grouping_unit']} groups — development: {len(development)}, frozen final test: {len(final_test)}.", "",
            f"Tracked object identities: {report['object_tracking']['n_tracked_object_trajectories']}; tracking status: {report['object_tracking']['status']}.",
            "## Interpretation boundaries", "", *[f"- {item}" for item in report["limitations"]], "",
            "## Missing and failed frames", "",
            "See `measurements.csv` and `cross_sectional_summary.csv`; pending annotations, missing frames and failures remain separate and are not interpolated.",
            "Longitudinal growth is written only for object identities in the supplied reviewed track map (`tracked_object_growth.csv`). Without such a map, the package makes no object trajectory claim.", "",
            "This receipt reports image-mask geometry, not organoid health or regenerative function.", "",
        ]
        (staged / "REPORT.md").write_text("\n".join(report_lines), encoding="utf-8")
        report["outputs"]["REPORT.md"] = _sha256((staged / "REPORT.md").read_bytes())
        (staged / "receipt.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        output_path.mkdir()
        try:
            for entry in staged.iterdir():
                entry.rename(output_path / entry.name)
        except BaseException:
            shutil.rmtree(output_path)
            raise
    return report
