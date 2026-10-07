"""Instance-level agreement between a supplied mask and a reference mask.

Foreground Dice answers one question: how much of the image did two
segmentations call foreground. It cannot see that one annotator split a cyst
into two objects, merged two cysts into one, or found a different number of
objects at the same total area. These functions add object-level matching,
count and area error, and split/merge rates under an explicitly declared rule.

Agreement is agreement. A high matched IoU does not establish that either mask
is correct, and no quantity here measures viability, maturation, treatment
response or regenerative potency.
"""

from __future__ import annotations

from statistics import mean

import numpy as np

MATCHING_RULE = (
    "One-to-one greedy matching by descending intersection-over-union, keeping pairs at or above "
    "match_iou_threshold. Containment for split and merge counting is separate: an object counts as "
    "contained in an object of the other mask when at least containment_fraction of its own pixels fall "
    "inside it. Each distinct positive integer label is one instance; disconnected components that share a "
    "label value stay one instance, matching the rest of this package."
)
DEFAULT_MATCH_IOU = 0.5
DEFAULT_CONTAINMENT_FRACTION = 0.5


def _label_areas(labels: np.ndarray) -> dict[int, int]:
    values, counts = np.unique(labels[labels > 0], return_counts=True)
    return {int(value): int(count) for value, count in zip(values, counts)}


def _overlaps(predicted: np.ndarray, reference: np.ndarray) -> dict[tuple[int, int], int]:
    """Pixel counts for every co-occurring positive label pair."""
    both = (predicted > 0) & (reference > 0)
    if not both.any():
        return {}
    pairs, counts = np.unique(
        np.stack([predicted[both].astype(np.int64), reference[both].astype(np.int64)], axis=1),
        axis=0, return_counts=True,
    )
    return {(int(pair[0]), int(pair[1])): int(count) for pair, count in zip(pairs, counts)}


def instance_agreement(predicted: np.ndarray, reference: np.ndarray, *,
                       match_iou_threshold: float = DEFAULT_MATCH_IOU,
                       containment_fraction: float = DEFAULT_CONTAINMENT_FRACTION,
                       pixel_size_um: float | None = None) -> dict:
    """Compare two integer label images at object level under the declared rule.

    Returns matched pairs, unmatched objects on each side, count and area error,
    and split/merge counts. Empty masks are reported as empty rather than as
    perfect agreement, because "both found nothing" is not a measurement of
    object agreement.
    """
    if predicted.shape != reference.shape:
        raise ValueError("predicted and reference masks must have identical dimensions")
    if predicted.ndim != 2 or reference.ndim != 2:
        raise ValueError("instance agreement requires single-channel label images")
    if not 0 < match_iou_threshold <= 1:
        raise ValueError("match_iou_threshold must be greater than 0 and at most 1")
    if not 0 < containment_fraction <= 1:
        raise ValueError("containment_fraction must be greater than 0 and at most 1")

    predicted_areas = _label_areas(predicted)
    reference_areas = _label_areas(reference)
    overlaps = _overlaps(predicted, reference)

    candidates = []
    for (predicted_label, reference_label), intersection in overlaps.items():
        union = predicted_areas[predicted_label] + reference_areas[reference_label] - intersection
        iou = intersection / union if union else 0.0
        if iou >= match_iou_threshold:
            candidates.append((iou, intersection, predicted_label, reference_label))
    # Deterministic order: best overlap first, then by label so equal scores never
    # depend on dictionary iteration order.
    candidates.sort(key=lambda item: (-item[0], item[2], item[3]))

    matched, used_predicted, used_reference = [], set(), set()
    for iou, intersection, predicted_label, reference_label in candidates:
        if predicted_label in used_predicted or reference_label in used_reference:
            continue
        used_predicted.add(predicted_label)
        used_reference.add(reference_label)
        predicted_area = predicted_areas[predicted_label]
        reference_area = reference_areas[reference_label]
        matched.append({
            "predicted_label": predicted_label,
            "reference_label": reference_label,
            "intersection_over_union": iou,
            "dice": 2 * intersection / (predicted_area + reference_area),
            "predicted_area_pixels": predicted_area,
            "reference_area_pixels": reference_area,
            "signed_area_error_pixels": predicted_area - reference_area,
            "signed_area_error_um2": ((predicted_area - reference_area) * pixel_size_um ** 2
                                      if pixel_size_um is not None else None),
        })

    split_reference_labels = sorted(
        reference_label for reference_label, reference_area in reference_areas.items()
        if sum(1 for (predicted_label, other), intersection in overlaps.items()
               if other == reference_label
               and intersection >= containment_fraction * predicted_areas[predicted_label]) >= 2
    )
    merged_predicted_labels = sorted(
        predicted_label for predicted_label, predicted_area in predicted_areas.items()
        if sum(1 for (other, reference_label), intersection in overlaps.items()
               if other == predicted_label
               and intersection >= containment_fraction * reference_areas[reference_label]) >= 2
    )

    foreground_predicted = int(np.count_nonzero(predicted > 0))
    foreground_reference = int(np.count_nonzero(reference > 0))
    return {
        "matching_rule": MATCHING_RULE,
        "match_iou_threshold": match_iou_threshold,
        "containment_fraction": containment_fraction,
        "n_predicted_objects": len(predicted_areas),
        "n_reference_objects": len(reference_areas),
        "n_matched_objects": len(matched),
        "signed_count_error": len(predicted_areas) - len(reference_areas),
        "unmatched_predicted_labels": sorted(set(predicted_areas) - used_predicted),
        "unmatched_reference_labels": sorted(set(reference_areas) - used_reference),
        "mean_matched_iou": mean(item["intersection_over_union"] for item in matched) if matched else None,
        "mean_matched_dice": mean(item["dice"] for item in matched) if matched else None,
        "mean_absolute_matched_area_error_pixels":
            mean(abs(item["signed_area_error_pixels"]) for item in matched) if matched else None,
        "n_split_reference_objects": len(split_reference_labels),
        "split_reference_labels": split_reference_labels,
        "n_merged_predicted_objects": len(merged_predicted_labels),
        "merged_predicted_labels": merged_predicted_labels,
        "foreground_predicted_pixels": foreground_predicted,
        "foreground_reference_pixels": foreground_reference,
        "signed_foreground_area_error_pixels": foreground_predicted - foreground_reference,
        "both_masks_empty": not predicted_areas and not reference_areas,
        "matched_objects": matched,
    }


def _finite(values: list) -> list:
    return [value for value in values if value is not None]


def group_agreement_summary(rows: list[dict], *, group_field: str = "biological_unit_id") -> dict:
    """Summarize per-frame agreement by source group, then weight groups equally.

    Frames are not independent units: several frames can come from one specimen and
    several specimens from one source group. The pooled per-frame mean and the
    equal-group mean answer different questions, so both are reported with their
    denominators. No interval is produced: a frame bootstrap would not be group
    uncertainty, and these cohorts have too few independent groups to support one.
    """
    scored = [row for row in rows if row.get("mean_matched_iou") is not None]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get(group_field) or "not_reported"), []).append(row)

    per_group = []
    for group_id, group_rows in sorted(groups.items()):
        group_scored = [row for row in group_rows if row.get("mean_matched_iou") is not None]
        per_group.append({
            "group_id": group_id,
            "n_frames": len(group_rows),
            "n_frames_with_matched_objects": len(group_scored),
            "n_specimens": len({row.get("specimen_id") for row in group_rows}),
            "mean_matched_iou": mean(row["mean_matched_iou"] for row in group_scored) if group_scored else None,
            "mean_matched_dice": mean(row["mean_matched_dice"] for row in group_scored) if group_scored else None,
            "total_predicted_objects": sum(row["n_predicted_objects"] for row in group_rows),
            "total_reference_objects": sum(row["n_reference_objects"] for row in group_rows),
            "total_split_reference_objects": sum(row["n_split_reference_objects"] for row in group_rows),
            "total_merged_predicted_objects": sum(row["n_merged_predicted_objects"] for row in group_rows),
        })

    group_means = _finite([group["mean_matched_iou"] for group in per_group])
    return {
        "group_field": group_field,
        "n_groups": len(per_group),
        "n_frames": len(rows),
        "n_frames_with_matched_objects": len(scored),
        "n_frames_without_matched_objects": len(rows) - len(scored),
        "pooled_frame_mean_matched_iou": mean(row["mean_matched_iou"] for row in scored) if scored else None,
        "equal_group_mean_matched_iou": mean(group_means) if group_means else None,
        "n_groups_contributing_to_equal_group_mean": len(group_means),
        "total_predicted_objects": sum(row["n_predicted_objects"] for row in rows),
        "total_reference_objects": sum(row["n_reference_objects"] for row in rows),
        "total_split_reference_objects": sum(row["n_split_reference_objects"] for row in rows),
        "total_merged_predicted_objects": sum(row["n_merged_predicted_objects"] for row in rows),
        "per_group": per_group,
        "uncertainty": "not_reported",
        "limitations": [
            "A frame is not an independent biological unit. Several frames can share a specimen and "
            "several specimens can share a source group, so the pooled frame mean overweights "
            "heavily imaged groups.",
            "No interval is reported. A frame or object bootstrap would describe within-cohort "
            "resampling, not uncertainty across independent donors or sites.",
            "Frames where no object pair reached the match threshold are counted separately and are "
            "excluded from mean matched overlap, not scored as zero or as perfect.",
            "Agreement between two masks does not establish that either is correct, and says nothing "
            "about viability, maturation, treatment response or regenerative potency.",
        ],
    }
