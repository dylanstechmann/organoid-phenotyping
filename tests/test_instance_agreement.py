from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from organoidphenotyping.instance_agreement import (  # noqa: E402
    group_agreement_summary,
    instance_agreement,
)


def blank(size: int = 20) -> np.ndarray:
    return np.zeros((size, size), dtype=np.uint16)


def box(labels: np.ndarray, label: int, top: int, left: int, height: int, width: int) -> np.ndarray:
    labels[top:top + height, left:left + width] = label
    return labels


class InstanceAgreementTests(unittest.TestCase):
    def test_identical_masks_match_every_object(self):
        mask = box(box(blank(), 1, 1, 1, 4, 4), 2, 10, 10, 6, 6)
        result = instance_agreement(mask, mask.copy())
        self.assertEqual(result["n_predicted_objects"], 2)
        self.assertEqual(result["n_matched_objects"], 2)
        self.assertEqual(result["signed_count_error"], 0)
        self.assertEqual(result["mean_matched_iou"], 1.0)
        self.assertEqual(result["mean_matched_dice"], 1.0)
        self.assertEqual(result["mean_absolute_matched_area_error_pixels"], 0)
        self.assertEqual(result["n_split_reference_objects"], 0)
        self.assertEqual(result["n_merged_predicted_objects"], 0)
        self.assertFalse(result["both_masks_empty"])

    def test_label_values_need_not_agree_for_a_match(self):
        reference = box(blank(), 1, 2, 2, 5, 5)
        predicted = box(blank(), 97, 2, 2, 5, 5)
        result = instance_agreement(predicted, reference)
        self.assertEqual(result["n_matched_objects"], 1)
        pair = result["matched_objects"][0]
        self.assertEqual((pair["predicted_label"], pair["reference_label"]), (97, 1))
        self.assertEqual(pair["intersection_over_union"], 1.0)

    def test_equal_foreground_dice_can_hide_a_count_error(self):
        # One reference object; the prediction covers the same pixels as two objects
        # separated by a one-pixel gap, so foreground overlap stays high.
        reference = box(blank(), 1, 4, 4, 4, 9)
        predicted = box(box(blank(), 1, 4, 4, 4, 4), 2, 4, 9, 4, 4)
        result = instance_agreement(predicted, reference)
        self.assertEqual((result["n_predicted_objects"], result["n_reference_objects"]), (2, 1))
        self.assertEqual(result["signed_count_error"], 1)
        self.assertEqual(result["n_split_reference_objects"], 1)
        self.assertEqual(result["split_reference_labels"], [1])
        self.assertEqual(result["n_merged_predicted_objects"], 0)
        # Foreground area is nearly identical even though the object count is wrong.
        self.assertEqual(result["signed_foreground_area_error_pixels"], -4)

    def test_merge_is_counted_from_the_prediction_side(self):
        reference = box(box(blank(), 1, 4, 4, 4, 4), 2, 4, 9, 4, 4)
        predicted = box(blank(), 1, 4, 4, 4, 9)
        result = instance_agreement(predicted, reference)
        self.assertEqual(result["n_merged_predicted_objects"], 1)
        self.assertEqual(result["merged_predicted_labels"], [1])
        self.assertEqual(result["n_split_reference_objects"], 0)
        self.assertEqual(result["signed_count_error"], -1)

    def test_matching_is_one_to_one_and_deterministic(self):
        reference = box(box(blank(), 1, 1, 1, 6, 6), 2, 10, 10, 6, 6)
        predicted = box(box(blank(), 5, 1, 1, 6, 6), 6, 1, 1, 6, 6)  # second write overwrites the first
        predicted = box(predicted, 7, 10, 10, 6, 6)
        result = instance_agreement(predicted, reference)
        self.assertEqual(result["n_matched_objects"], 2)
        pairs = {(item["predicted_label"], item["reference_label"]) for item in result["matched_objects"]}
        self.assertEqual(len(pairs), len({pair[0] for pair in pairs}))
        self.assertEqual(len(pairs), len({pair[1] for pair in pairs}))
        self.assertEqual(result, instance_agreement(predicted, reference))

    def test_overlap_below_the_threshold_leaves_both_objects_unmatched(self):
        # 6x6 boxes offset by three columns: intersection 18, union 54, IoU exactly 1/3.
        reference = box(blank(), 1, 2, 2, 6, 6)
        predicted = box(blank(), 1, 2, 5, 6, 6)
        result = instance_agreement(predicted, reference)
        self.assertEqual(result["n_matched_objects"], 0)
        self.assertIsNone(result["mean_matched_iou"])
        self.assertEqual(result["unmatched_predicted_labels"], [1])
        self.assertEqual(result["unmatched_reference_labels"], [1])
        relaxed = instance_agreement(predicted, reference, match_iou_threshold=0.25)
        self.assertEqual(relaxed["n_matched_objects"], 1)
        self.assertAlmostEqual(relaxed["mean_matched_iou"], 1 / 3)

    def test_empty_masks_are_reported_as_empty_not_as_perfect_agreement(self):
        result = instance_agreement(blank(), blank())
        self.assertTrue(result["both_masks_empty"])
        self.assertIsNone(result["mean_matched_iou"])
        self.assertEqual(result["n_matched_objects"], 0)

    def test_missed_and_extra_objects_are_separated(self):
        reference = box(box(blank(), 1, 1, 1, 4, 4), 2, 10, 1, 4, 4)
        predicted = box(box(blank(), 1, 1, 1, 4, 4), 2, 1, 10, 4, 4)
        result = instance_agreement(predicted, reference)
        self.assertEqual(result["n_matched_objects"], 1)
        self.assertEqual(result["unmatched_reference_labels"], [2])
        self.assertEqual(result["unmatched_predicted_labels"], [2])
        self.assertEqual(result["signed_count_error"], 0)

    def test_physical_area_error_requires_a_pixel_size(self):
        reference = box(blank(), 1, 2, 2, 4, 4)
        predicted = box(blank(), 1, 2, 2, 4, 5)
        without = instance_agreement(predicted, reference)
        self.assertIsNone(without["matched_objects"][0]["signed_area_error_um2"])
        withscale = instance_agreement(predicted, reference, pixel_size_um=2.0)
        self.assertEqual(withscale["matched_objects"][0]["signed_area_error_pixels"], 4)
        self.assertEqual(withscale["matched_objects"][0]["signed_area_error_um2"], 16.0)

    def test_malformed_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "identical dimensions"):
            instance_agreement(blank(10), blank(12))
        with self.assertRaisesRegex(ValueError, "single-channel"):
            instance_agreement(np.zeros((4, 4, 3), dtype=np.uint8), np.zeros((4, 4, 3), dtype=np.uint8))
        for bad in (0, -0.5, 1.5):
            with self.assertRaises(ValueError):
                instance_agreement(blank(), blank(), match_iou_threshold=bad)
            with self.assertRaises(ValueError):
                instance_agreement(blank(), blank(), containment_fraction=bad)

    def test_matching_rule_is_published_with_the_result(self):
        result = instance_agreement(blank(), blank())
        self.assertIn("descending intersection-over-union", result["matching_rule"])
        self.assertIn("containment_fraction", result["matching_rule"])


class GroupSummaryTests(unittest.TestCase):
    def rows(self):
        def row(frame, group, specimen, iou, predicted=1, reference=1, splits=0, merges=0):
            return {"frame_id": frame, "biological_unit_id": group, "specimen_id": specimen,
                    "mean_matched_iou": iou, "mean_matched_dice": iou,
                    "n_predicted_objects": predicted, "n_reference_objects": reference,
                    "n_split_reference_objects": splits, "n_merged_predicted_objects": merges}

        return [
            row("f1", "kidney-1", "s1", 0.9), row("f2", "kidney-1", "s1", 0.9),
            row("f3", "kidney-1", "s2", 0.9), row("f4", "kidney-2", "s3", 0.5, splits=1),
            row("f5", "kidney-3", "s4", None, predicted=0, reference=2),
        ]

    def test_equal_group_mean_differs_from_the_pooled_frame_mean(self):
        summary = group_agreement_summary(self.rows())
        self.assertEqual(summary["n_groups"], 3)
        self.assertEqual(summary["n_frames"], 5)
        self.assertEqual(summary["n_frames_with_matched_objects"], 4)
        self.assertEqual(summary["n_frames_without_matched_objects"], 1)
        self.assertAlmostEqual(summary["pooled_frame_mean_matched_iou"], (0.9 * 3 + 0.5) / 4)
        self.assertAlmostEqual(summary["equal_group_mean_matched_iou"], (0.9 + 0.5) / 2)
        self.assertEqual(summary["n_groups_contributing_to_equal_group_mean"], 2)

    def test_group_rows_keep_denominators_and_split_merge_totals(self):
        summary = group_agreement_summary(self.rows())
        first = next(group for group in summary["per_group"] if group["group_id"] == "kidney-1")
        self.assertEqual((first["n_frames"], first["n_specimens"]), (3, 2))
        unscored = next(group for group in summary["per_group"] if group["group_id"] == "kidney-3")
        self.assertIsNone(unscored["mean_matched_iou"])
        self.assertEqual(unscored["n_frames_with_matched_objects"], 0)
        self.assertEqual(summary["total_split_reference_objects"], 1)
        self.assertEqual(summary["total_reference_objects"], 6)

    def test_summary_declines_to_report_an_interval(self):
        summary = group_agreement_summary(self.rows())
        self.assertEqual(summary["uncertainty"], "not_reported")
        self.assertTrue(any("not uncertainty across independent donors" in item
                            for item in summary["limitations"]))

    def test_missing_group_values_become_an_explicit_bucket(self):
        summary = group_agreement_summary([
            {"frame_id": "f1", "biological_unit_id": "", "specimen_id": "s1", "mean_matched_iou": 0.8,
             "mean_matched_dice": 0.8, "n_predicted_objects": 1, "n_reference_objects": 1,
             "n_split_reference_objects": 0, "n_merged_predicted_objects": 0}])
        self.assertEqual(summary["per_group"][0]["group_id"], "not_reported")


if __name__ == "__main__":
    unittest.main()


class MeasurementRunAgreementTests(unittest.TestCase):
    """A measurement run with reference masks emits grouped object-level agreement."""

    def build_study(self, root: Path) -> None:
        import csv
        import json

        from PIL import Image

        (root / "images").mkdir()
        (root / "masks").mkdir()
        (root / "reference").mkdir()
        # frame_a0: the reference calls one object; the supplied mask splits it in two,
        #   and neither piece reaches the match threshold, so the frame scores no pair.
        # frame_a1/frame_a2: one matched object each at IoU 9/12 = 0.75.
        # frame_b0: final-test group, one perfectly matched object.
        frames = [
            ("frame_a0", "kidney_a", "well_a", 0,
             [[1, 1, 0, 2, 2, 0], [1, 1, 0, 2, 2, 0], [0] * 6, [0] * 6, [0] * 6, [0] * 6],
             [[1, 1, 1, 1, 1, 0], [1, 1, 1, 1, 1, 0], [0] * 6, [0] * 6, [0] * 6, [0] * 6]),
            ("frame_a1", "kidney_a", "well_a", 12,
             [[3, 3, 3, 3, 0, 0], [3, 3, 3, 3, 0, 0], [3, 3, 3, 3, 0, 0], [0] * 6, [0] * 6, [0] * 6],
             [[7, 7, 7, 0, 0, 0], [7, 7, 7, 0, 0, 0], [7, 7, 7, 0, 0, 0], [0] * 6, [0] * 6, [0] * 6]),
            ("frame_a2", "kidney_a", "well_b", 12,
             [[3, 3, 3, 3, 0, 0], [3, 3, 3, 3, 0, 0], [3, 3, 3, 3, 0, 0], [0] * 6, [0] * 6, [0] * 6],
             [[7, 7, 7, 0, 0, 0], [7, 7, 7, 0, 0, 0], [7, 7, 7, 0, 0, 0], [0] * 6, [0] * 6, [0] * 6]),
            ("frame_b0", "kidney_b", "well_c", 0,
             [[1, 1, 0, 0, 0, 0], [1, 1, 0, 0, 0, 0], [0] * 6, [0] * 6, [0] * 6, [0] * 6],
             [[1, 1, 0, 0, 0, 0], [1, 1, 0, 0, 0, 0], [0] * 6, [0] * 6, [0] * 6, [0] * 6]),
        ]
        rows = []
        for frame, group, specimen, timepoint, mask_values, reference_values in frames:
            Image.new("RGB", (6, 6), "white").save(root / f"images/{frame}.png")
            Image.fromarray(np.array(mask_values, dtype=np.uint8)).save(root / f"masks/{frame}.png")
            Image.fromarray(np.array(reference_values, dtype=np.uint8)).save(root / f"reference/{frame}.png")
            rows.append({
                "frame_id": frame, "specimen_id": specimen, "biological_unit_id": group,
                "clone_id": "not_reported", "culture_batch_id": "batch_a", "imaging_lab": "lab_a",
                "microscope_id": "scope_a", "timepoint_h": timepoint,
                "image_path": f"images/{frame}.png", "mask_path": f"masks/{frame}.png",
                "status": "measured", "status_reason": "",
                "pixel_size_um": "0.5", "pixel_size_source": "stage-micrometer calibration record",
                "culture_condition": "dome", "treatment": "forskolin", "magnification": "5x",
                "reference_mask_path": f"reference/{frame}.png",
                "reference_mask_annotator": "reviewer-1",
            })
        with (root / "acquisitions.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        (root / "study-plan.json").write_text(json.dumps({
            "schema_version": 1,
            "dataset": {"dataset_id": "test", "title": "Agreement test study",
                        "source_url": "https://example.org/study", "license": "CC0-1.0",
                        "retrieved_on": "2026-10-07"},
            "split": {"frozen_before_model_fit": True, "grouping_field": "biological_unit_id",
                      "grouping_unit": "source group", "development_group_ids": ["kidney_a"],
                      "final_test_group_ids": ["kidney_b"]},
        }), encoding="utf-8")

    def test_measurement_run_reports_grouped_instance_agreement(self):
        import csv
        import tempfile

        from organoidphenotyping.core import measure_study

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.build_study(root)
            report = measure_study(root / "acquisitions.csv", root / "study-plan.json", root / "output")

            agreement = report["instance_agreement"]
            self.assertEqual(agreement["n_frames"], 4)
            self.assertEqual(agreement["n_groups"], 2)
            self.assertEqual(agreement["n_frames_with_matched_objects"], 3)
            self.assertEqual(agreement["n_frames_without_matched_objects"], 1)
            self.assertEqual(agreement["total_split_reference_objects"], 1)
            self.assertEqual(agreement["total_merged_predicted_objects"], 0)
            self.assertEqual(agreement["total_predicted_objects"], 5)
            self.assertEqual(agreement["total_reference_objects"], 4)
            self.assertEqual(agreement["uncertainty"], "not_reported")
            # Three scored frames weight the heavily imaged group; two groups do not.
            self.assertAlmostEqual(agreement["pooled_frame_mean_matched_iou"], (0.75 + 0.75 + 1.0) / 3)
            self.assertAlmostEqual(agreement["equal_group_mean_matched_iou"], (0.75 + 1.0) / 2)
            development = next(group for group in agreement["per_group"]
                               if group["group_id"] == "kidney_a")
            self.assertEqual((development["n_frames"], development["n_specimens"]), (3, 2))
            self.assertEqual(development["n_frames_with_matched_objects"], 2)

            with (root / "output" / "instance_agreement.csv").open(encoding="utf-8", newline="") as handle:
                csv_rows = {row["frame_id"]: row for row in csv.DictReader(handle)}
            self.assertEqual(set(csv_rows), {"frame_a0", "frame_a1", "frame_a2", "frame_b0"})
            self.assertEqual(csv_rows["frame_a0"]["n_split_reference_objects"], "1")
            self.assertEqual(csv_rows["frame_a0"]["signed_count_error"], "1")
            self.assertEqual(csv_rows["frame_a0"]["n_matched_objects"], "0")
            self.assertEqual(csv_rows["frame_a0"]["mean_matched_iou"], "")
            self.assertEqual(csv_rows["frame_a1"]["n_matched_objects"], "1")
            self.assertEqual(csv_rows["frame_a1"]["mean_matched_iou"], "0.75")
            self.assertEqual(csv_rows["frame_b0"]["mean_matched_iou"], "1.0")

            self.assertIn("instance_agreement.csv", report["outputs"])
            text = (root / "output" / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("Instance-level agreement", text)
            self.assertIn("equal-group mean", text)
            self.assertTrue(any("not the correctness of either" in item for item in report["limitations"]))

    def test_run_without_reference_masks_reports_no_agreement(self):
        import csv
        import json
        import tempfile

        from organoidphenotyping.core import measure_study

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.build_study(root)
            with (root / "acquisitions.csv").open(encoding="utf-8", newline="") as handle:
                rows = [{key: value for key, value in row.items()
                         if key not in {"reference_mask_path", "reference_mask_annotator"}}
                        for row in csv.DictReader(handle)]
            with (root / "no-reference.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            report = measure_study(root / "no-reference.csv", root / "study-plan.json", root / "plain-output")
            self.assertEqual(report["instance_agreement"],
                             {"n_frames": 0, "status": "no_reference_mask_supplied"})
            self.assertIn("No reference mask was supplied",
                          (root / "plain-output" / "REPORT.md").read_text(encoding="utf-8"))
            self.assertIn("instance_agreement.csv", report["outputs"])
            self.assertEqual(json.loads(json.dumps(report))["instance_agreement"]["n_frames"], 0)
