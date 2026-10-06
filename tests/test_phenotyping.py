from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from organoidphenotyping.bonn_import import parse_figure4_filename
from organoidphenotyping.core import StudyError, _mask_measurement, _mask_object_rows, _tracked_object_growth, measure_study


class PhenotypingTests(unittest.TestCase):
    def test_labeled_mask_reports_union_and_per_object_geometry_with_optional_scale(self):
        labels = np.array([
            [0, 1, 1, 0, 0],
            [0, 1, 1, 0, 0],
            [0, 0, 0, 2, 2],
            [0, 0, 0, 2, 2],
        ], dtype=np.uint8)
        mask = Image.fromarray(labels)
        summary = _mask_measurement(mask, 0.5)
        objects = _mask_object_rows(mask, 0.5)
        self.assertEqual(summary["foreground_label_value_count"], 2)
        self.assertEqual(summary["area_pixels"], 8)
        self.assertEqual(summary["area_um2"], 2.0)
        self.assertEqual([item["instance_label_value"] for item in objects], [1, 2])
        self.assertEqual([item["area_pixels"] for item in objects], [4, 4])
        self.assertTrue(all(item["area_um2"] == 1.0 for item in objects))
        uncalibrated = _mask_measurement(mask, None)
        self.assertIsNone(uncalibrated["area_um2"])
        self.assertEqual(uncalibrated["area_pixels"], 8)

    def test_bonn_filename_preserves_distinct_pre_and_post_condition_passage_tokens(self):
        row = parse_figure4_filename(
            "2024-01-03_kidney-73-25_P3D7_Domes_P3D8_Forskolin-A_24h_BF_5x.tif"
        )
        self.assertEqual(row["kidney"], "73-25")
        self.assertEqual(row["source_passage_day_token"], "P3D7")
        self.assertEqual(row["post_condition_passage_day_token"], "P3D8")
        self.assertEqual(row["timepoint_h"], "24")

    def test_growth_is_emitted_only_from_a_validated_object_track_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            (root / "masks").mkdir()
            rows = []
            for frame, group, specimen, timepoint, mask_values, status in [
                ("frame_a0", "kidney_a", "well_a", 0, [[1, 1, 0, 0], [1, 1, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "measured"),
                ("frame_a12", "kidney_a", "well_a", 12, [[1, 1, 1, 1], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]], "measured"),
                ("frame_b0", "kidney_b", "well_b", 0, None, "pending_annotation"),
            ]:
                image_path = f"images/{frame}.png"
                Image.new("RGB", (4, 4), "white").save(root / image_path)
                mask_path = ""
                reason = ""
                if mask_values is not None:
                    mask_path = f"masks/{frame}.png"
                    Image.fromarray(np.array(mask_values, dtype=np.uint8)).save(root / mask_path)
                else:
                    reason = "Awaiting a reviewed instance mask."
                rows.append({
                    "frame_id": frame, "specimen_id": specimen, "biological_unit_id": group,
                    "clone_id": "not_reported", "culture_batch_id": "batch_a",
                    "imaging_lab": "lab_a", "microscope_id": "scope_a", "timepoint_h": timepoint,
                    "image_path": image_path, "mask_path": mask_path, "status": status,
                    "status_reason": reason, "pixel_size_um": "0.5" if status == "measured" else "",
                    "pixel_size_source": "stage-micrometer calibration record" if status == "measured" else "",
                    "culture_condition": "dome", "treatment": "forskolin", "magnification": "5x",
                })
            with (root / "acquisitions.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            invalid_rows = [dict(row) for row in rows]
            invalid_rows[0]["pixel_size_source"] = ""
            with (root / "uncalibrated.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=invalid_rows[0].keys(), lineterminator="\n")
                writer.writeheader()
                writer.writerows(invalid_rows)
            plan = {
                "schema_version": 1,
                "dataset": {"dataset_id": "test", "title": "Small microscopy test study",
                            "source_url": "https://example.org/study", "license": "CC0-1.0",
                            "retrieved_on": "2026-10-04"},
                "split": {"frozen_before_model_fit": True, "grouping_field": "biological_unit_id",
                          "grouping_unit": "source group", "development_group_ids": ["kidney_a"],
                          "final_test_group_ids": ["kidney_b"]},
            }
            (root / "study-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (root / "tracks.csv").write_text(
                "frame_id,instance_label_value,track_id\nframe_a0,1,cyst_1\nframe_a12,1,cyst_1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StudyError, "pixel_size_um requires pixel_size_source"):
                measure_study(root / "uncalibrated.csv", root / "study-plan.json", root / "invalid-output")

            report = measure_study(root / "acquisitions.csv", root / "study-plan.json",
                                   root / "tracked-output", "tracks.csv")
            self.assertEqual(report["n_pending_annotation_frames"], 1)
            self.assertEqual(report["object_tracking"]["status"], "reviewed_track_map_supplied")
            self.assertEqual(report["object_tracking"]["n_tracked_object_trajectories"], 1)
            with (root / "tracked-output" / "tracked_object_growth.csv").open(encoding="utf-8", newline="") as handle:
                growth = list(csv.DictReader(handle))
            self.assertEqual(len(growth), 2)
            self.assertEqual(growth[0]["growth_percent_since_prior_tracked_timepoint"], "")
            self.assertEqual(float(growth[1]["growth_percent_since_prior_tracked_timepoint"]), 100.0)
            self.assertEqual(growth[1]["growth_basis"], "calibrated_area_um2")
            with (root / "tracked-output" / "cross_sectional_summary.csv").open(encoding="utf-8", newline="") as handle:
                summary = list(csv.DictReader(handle))
            self.assertEqual(len(summary), 2)
            self.assertNotIn("growth_percent_since_prior_tracked_timepoint", summary[0])

            untracked = measure_study(root / "acquisitions.csv", root / "study-plan.json",
                                      root / "untracked-output")
            self.assertEqual(untracked["object_tracking"]["status"], "no_track_map")
            self.assertEqual(untracked["object_tracking"]["n_tracked_object_trajectories"], 0)
            with (root / "untracked-output" / "tracked_object_growth.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_track_map_rejects_a_label_that_does_not_exist(self):
        with self.assertRaisesRegex(StudyError, "track map references no measured instance"):
            _tracked_object_growth(
                [{"frame_id": "missing", "instance_label_value": 1, "track_id": "obj"}], []
            )


if __name__ == "__main__":
    unittest.main()
