from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from organoidphenotyping.annotation_pilot import prepare_annotation_pilot
from organoidphenotyping.annotation_workbench import audit_annotations, create_session, save_annotation
from organoidphenotyping.core import StudyError, measure_study


class AnnotationPilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "images").mkdir()
        rows = []
        for group, culture, treatments in [
            ("kidney_a", "Domes", ["DMSO", "Forskolin", "Media"]),
            ("kidney_b", "Suspension", ["DMSO", "Forskolin", "Media"]),
            ("kidney_test", "Domes", ["DMSO", "Forskolin", "Media"]),
        ]:
            for treatment in treatments:
                for replicate in ("A", "B"):
                    frame_id = f"{group}_{culture}_{treatment}_{replicate}_24h"
                    image_path = f"images/{frame_id}.tif"
                    Image.new("RGB", (8, 6), (100, 120, 140)).save(self.root / image_path)
                    rows.append({
                        "frame_id": frame_id,
                        "specimen_id": f"{group}_{culture}_{treatment}_{replicate}",
                        "biological_unit_id": group,
                        "clone_id": "not_reported",
                        "culture_batch_id": "not_reported",
                        "imaging_lab": "not_reported",
                        "microscope_id": "not_reported",
                        "culture_condition": culture,
                        "treatment": treatment,
                        "technical_replicate": replicate,
                        "timepoint_h": "24",
                        "image_path": image_path,
                        "mask_path": "",
                        "status": "pending_annotation",
                        "status_reason": "Awaiting manual annotation.",
                    })
        self.manifest = self.root / "acquisitions.csv"
        with self.manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        self.plan = self.root / "study-plan.json"
        self.plan.write_text(json.dumps({
            "schema_version": 1,
            "dataset": {
                "dataset_id": "10.60507/FK2/OM25XQ",
                "title": "Synthetic annotation test fixture",
                "source_url": "https://example.org/test-fixture",
                "license": "CC-BY-4.0",
                "retrieved_on": "2026-10-06",
                "source_archive_sha256": "a" * 64,
            },
            "split": {
                "frozen_before_model_fit": True,
                "grouping_field": "biological_unit_id",
                "grouping_unit": "synthetic test groups",
                "development_group_ids": ["kidney_a", "kidney_b"],
                "final_test_group_ids": ["kidney_test"],
            },
        }), encoding="utf-8")

    @staticmethod
    def _read_csv(path):
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_builds_stratified_development_only_packet_with_concealed_repeats(self):
        output = self.root / "pilot"
        receipt = prepare_annotation_pilot(
            self.manifest, self.plan, output, seed=7, repeat_tasks=2,
        )
        self.assertEqual(receipt["n_unique_frames"], 6)
        self.assertEqual(receipt["n_total_tasks"], 8)
        self.assertEqual(receipt["selected_final_test_overlap"], [])
        self.assertFalse(receipt["masks_generated"])
        round1 = self._read_csv(output / "public" / "annotation_queue_round1.csv")
        round2 = self._read_csv(output / "public" / "annotation_queue_round2.csv")
        key = self._read_csv(output / "curator" / "assignment_key.csv")
        self.assertEqual(len(round1), 6)
        self.assertEqual(len(round2), 2)
        self.assertNotIn("treatment", round1[0])
        self.assertNotIn("frame_id", round1[0])
        self.assertNotIn("biological_unit_id", round1[0])
        self.assertEqual({row["biological_unit_id"] for row in key}, {"kidney_a", "kidney_b"})
        self.assertNotIn("kidney_test", {row["biological_unit_id"] for row in key})
        primary_by_frame = {row["frame_id"]: row for row in key if row["round"] == "primary"}
        repeated = [row for row in key if row["round"] == "concealed_repeat"]
        self.assertEqual(len(repeated), 2)
        for row in repeated:
            primary = primary_by_frame[row["frame_id"]]
            self.assertNotEqual(row["task_id"], primary["task_id"])
            self.assertEqual(row["source_image_sha256"], primary["source_image_sha256"])
            self.assertEqual(row["repeat_of_task_id"], primary["task_id"])
        for task in round1 + round2:
            path = output / "public" / task["image_path"]
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), task["image_sha256"])
            self.assertFalse((output / "public" / task["mask_path"]).exists())
        self.assertTrue((output / "public" / "annotation_contact_sheet.png").is_file())
        self.assertTrue((output / "curator" / "pilot_receipt.json").is_file())

    def test_seed_repeats_the_frame_selection_and_output_is_never_overwritten(self):
        first = self.root / "pilot-one"
        second = self.root / "pilot-two"
        prepare_annotation_pilot(self.manifest, self.plan, first, seed=19, repeat_tasks=2)
        prepare_annotation_pilot(self.manifest, self.plan, second, seed=19, repeat_tasks=2)
        first_key = self._read_csv(first / "curator" / "assignment_key.csv")
        second_key = self._read_csv(second / "curator" / "assignment_key.csv")
        self.assertEqual(
            {row["frame_id"] for row in first_key if row["round"] == "primary"},
            {row["frame_id"] for row in second_key if row["round"] == "primary"},
        )
        with self.assertRaisesRegex(StudyError, "output already exists"):
            prepare_annotation_pilot(self.manifest, self.plan, first, seed=19, repeat_tasks=2)

    def test_blinded_polygon_session_records_provenance_and_repeat_agreement(self):
        pilot = self.root / "pilot-desk"
        prepare_annotation_pilot(self.manifest, self.plan, pilot, seed=7, repeat_tasks=2)
        session_path = self.root / "annotation-session"
        created = create_session(pilot, self.manifest, self.plan, session_path)
        self.assertFalse(created["masks_generated"])
        session = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(len(session["tasks"]), 8)
        self.assertNotIn("treatment", json.dumps(session["tasks"]).lower())
        key = self._read_csv(pilot / "curator" / "assignment_key.csv")
        for row in key:
            annotator = "reviewer-primary" if row["round"] == "primary" else "reviewer-repeat"
            save_annotation(session_path, row["task_id"], annotator,
                            [[[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]]])
        report = audit_annotations(session_path)
        self.assertEqual(report["n_annotated_tasks"], 8)
        self.assertEqual(report["n_repeat_pairs_scored"], 2)
        self.assertEqual(report["n_repeat_pairs_with_distinct_annotator_ids"], 2)
        self.assertTrue(all(item["foreground_dice"] == 1.0 for item in report["repeat_agreement"]))
        self.assertFalse(report["biological_results_generated"])
        annotated_manifest = self.root / report["annotated_manifest"]
        measured = measure_study(annotated_manifest, self.plan, self.root / "measured")
        self.assertEqual(measured["n_measured_frames"], 6)
        with (self.root / "measured" / "measurements.csv").open(encoding="utf-8", newline="") as handle:
            measurements = list(csv.DictReader(handle))
        annotated = [row for row in measurements if row["status"] == "measured"]
        self.assertEqual(len(annotated), 6)
        self.assertTrue(all(row["mask_annotator_id"] == "reviewer-primary" for row in annotated))
        self.assertTrue(all(row["annotation_task_id"].startswith("b-") for row in annotated))
        session = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
        audit_dir = session_path / session["audits"][-1]["report_path"]
        audit_receipt = json.loads((audit_dir.parent / "audit_receipt.json").read_text(encoding="utf-8"))
        self.assertFalse(audit_receipt["biological_results_generated"])
        self.assertIsNotNone(audit_receipt["annotated_manifest_sha256"])

    def test_rejects_split_overlap(self):
        document = json.loads(self.plan.read_text(encoding="utf-8"))
        document["split"]["final_test_group_ids"].append("kidney_a")
        self.plan.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(StudyError, "unique and disjoint"):
            prepare_annotation_pilot(self.manifest, self.plan, self.root / "bad-pilot", repeat_tasks=2)


if __name__ == "__main__":
    unittest.main()
