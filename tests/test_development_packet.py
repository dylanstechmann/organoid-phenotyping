from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from organoidphenotyping.core import StudyError
from organoidphenotyping.development_packet import (
    PACKET_FILES, prepare_development_packet, validate_development_packet,
)


DISPOSITION_FIELDS = (
    "annotation_disposition", "annotation_disposition_rationale",
    "annotation_disposition_annotator_id", "annotation_disposition_task_id",
    "annotation_disposition_revision", "annotation_disposition_path",
    "expected_annotation_disposition_sha256",
)


class DevelopmentPacketTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.intake = self.root / "intake-v2"
        self.intake.mkdir()
        self.manifest = self.root / "acquisitions.csv"
        self.plan = self.root / "study-plan.json"
        self.receipt = self.intake / "receipt.json"
        self.rows = []
        for frame, group in [
            ("dev_a0", "kidney_a"), ("dev_a1", "kidney_a"),
            ("dev_b0", "kidney_b"), ("test_c0", "kidney_test"),
        ]:
            self.rows.append({
                "frame_id": frame,
                "specimen_id": f"specimen_{frame}",
                "biological_unit_id": group,
                "clone_id": "not_reported",
                "culture_batch_id": "not_reported",
                "imaging_lab": "not_reported",
                "microscope_id": "not_reported",
                "timepoint_h": "24",
                "image_path": f"images/{frame}.tif",
                "mask_path": "",
                "status": "pending_annotation",
                "status_reason": "Awaiting a reviewed instance mask.",
                "source_uri": "https://example.org/synthetic-source",
                "license": "CC-BY-4.0",
                "culture_condition": "Suspension",
                "treatment": "DMSO",
                "technical_replicate": "A",
                "pixel_size_um": "",
                "pixel_size_source": "",
                **{field: "" for field in DISPOSITION_FIELDS},
            })
        self.plan_document = {
            "schema_version": 1,
            "dataset": {
                "dataset_id": "synthetic-development-packet-fixture",
                "title": "Synthetic development packet fixture",
                "source_url": "https://example.org/synthetic-source",
                "license": "CC-BY-4.0",
                "retrieved_on": "2026-11-27",
                "source_archive_sha256": "a" * 64,
                "source_readme_license_statement": "Synthetic fixture records CC-BY-4.0.",
            },
            "split": {
                "frozen_before_model_fit": True,
                "grouping_field": "biological_unit_id",
                "grouping_unit": "synthetic biological unit",
                "development_group_ids": ["kidney_a", "kidney_b"],
                "final_test_group_ids": ["kidney_test"],
            },
        }
        self._write_inputs()

    @staticmethod
    def _write_csv(path, rows):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _hash(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_inputs(self):
        self._write_csv(self.manifest, self.rows)
        self.plan.write_text(json.dumps(self.plan_document), encoding="utf-8")
        development = set(self.plan_document["split"]["development_group_ids"])
        measurement_rows = []
        for row in self.rows:
            measurement_rows.append({
                **row,
                "split": "development" if row["biological_unit_id"] in development else "final_test",
                "image_sha256": hashlib.sha256(row["frame_id"].encode("utf-8")).hexdigest(),
                "mask_sha256": "",
            })
        self._write_csv(self.intake / "measurements.csv", measurement_rows)
        status = {name: sum(row["status"] == name for row in self.rows)
                  for name in ("measured", "missing", "failed", "pending_annotation")}
        groups = {row["biological_unit_id"] for row in self.rows}
        receipt = {
            "schema_version": 1,
            "dataset": self.plan_document["dataset"],
            "input_manifest_sha256": self._hash(self.manifest),
            "study_plan_sha256": self._hash(self.plan),
            "n_manifest_rows": len(self.rows),
            "n_measured_frames": status["measured"],
            "n_missing_frames": status["missing"],
            "n_failed_frames": status["failed"],
            "n_pending_annotation_frames": status["pending_annotation"],
            "n_biological_units": len(groups),
            "split": {
                "grouping_field": "biological_unit_id",
                "development_group_count": len(development),
                "final_test_group_count": len(self.plan_document["split"]["final_test_group_ids"]),
                "specimen_overlap": [],
            },
            "outputs": {"measurements.csv": self._hash(self.intake / "measurements.csv")},
        }
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

    def _measurement_rows(self):
        with (self.intake / "measurements.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_packet_is_deterministic_development_only_and_honestly_pending(self):
        first = self.root / "packet-one"
        second = self.root / "packet-two"
        receipt = prepare_development_packet(self.manifest, self.plan, self.receipt, first)
        prepare_development_packet(self.manifest, self.plan, self.receipt, second)
        self.assertEqual(receipt["development"]["frame_count"], 3)
        self.assertEqual(receipt["validation"], {
            "manifest_frame_count": 4,
            "biological_unit_count": 3,
            "development_group_count": 2,
            "held_out_group_count": 1,
            "held_out_frames_in_packet": 0,
            "group_overlap_count": 0,
            "specimen_overlap_count": 0,
            "exact_image_overlap_count": 0,
            "held_out_image_contents_inspected": False,
        })
        self.assertEqual(receipt["development"]["mask_revision_counts"], {"missing": 3})
        self.assertEqual(receipt["development"]["reviewer_disposition_counts"], {"pending_review": 3})
        self.assertEqual(receipt["development"]["scale_availability_counts"], {"missing": 3})
        self.assertEqual(receipt["dataset"], self.plan_document["dataset"])
        self.assertFalse(receipt["third_party_assets_copied"])
        for name in PACKET_FILES:
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
            self.assertNotIn("kidney_test", (first / name).read_text(encoding="utf-8"))
            self.assertNotIn("test_c0", (first / name).read_text(encoding="utf-8"))
        with (first / "review_packet.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["frame_id"] for row in rows], ["dev_a0", "dev_a1", "dev_b0"])
        for row in rows:
            self.assertEqual(len(row["source_image_sha256"]), 64)
            self.assertEqual(row["mask_revision"], "missing")
            self.assertEqual(row["reviewer_disposition"], "pending_review")
            self.assertEqual(row["scale_availability"], "missing")
        validated = validate_development_packet(first, self.manifest, self.plan, self.receipt)
        self.assertEqual(validated["packet_validation"], "valid")

    def test_rejects_group_overlap_and_unassigned_biological_unit(self):
        self.plan_document["split"]["final_test_group_ids"].append("kidney_a")
        self._write_inputs()
        with self.assertRaisesRegex(StudyError, "overlap"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "overlap")

        self.setUp()
        self.rows[0]["biological_unit_id"] = "kidney_unassigned"
        self._write_inputs()
        with self.assertRaisesRegex(StudyError, "exactly match manifest biological units"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "unassigned")

    def test_bonn_profile_rejects_wrong_frame_or_group_counts(self):
        self.plan_document["dataset"]["dataset_id"] = "10.60507/FK2/OM25XQ"
        self._write_inputs()
        with self.assertRaisesRegex(StudyError, "280 frames, five biological units"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "wrong-bonn-profile")

    def test_rejects_exact_image_overlap_across_the_frozen_split(self):
        measurements = self._measurement_rows()
        measurements[-1]["image_sha256"] = measurements[0]["image_sha256"]
        self._write_csv(self.intake / "measurements.csv", measurements)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["outputs"]["measurements.csv"] = self._hash(self.intake / "measurements.csv")
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(StudyError, "exact image overlap"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "image-overlap")

    def test_rejects_missing_source_hash_and_dataset_provenance(self):
        measurements = self._measurement_rows()
        measurements[0]["image_sha256"] = ""
        self._write_csv(self.intake / "measurements.csv", measurements)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["outputs"]["measurements.csv"] = self._hash(self.intake / "measurements.csv")
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(StudyError, "source_image_sha256"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "missing-hash")

        self.setUp()
        self.rows[0]["source_uri"] = ""
        self._write_inputs()
        with self.assertRaisesRegex(StudyError, "source_uri and license provenance"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "missing-source")

    def test_rejects_incomplete_reviewer_disposition_and_intake_provenance(self):
        self.rows[0]["annotation_disposition"] = "ambiguous"
        self._write_inputs()
        with self.assertRaisesRegex(StudyError, "complete pending-annotation provenance"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "partial-disposition")

        self.setUp()
        complete = {
            "annotation_disposition": "ambiguous",
            "annotation_disposition_rationale": "The synthetic boundary is ambiguous.",
            "annotation_disposition_annotator_id": "reviewer-a",
            "annotation_disposition_task_id": "b-0123456789abcdef",
            "annotation_disposition_revision": "0123456789abcdef",
            "annotation_disposition_path": "dispositions/revision.json",
            "expected_annotation_disposition_sha256": "b" * 64,
        }
        self.rows[0].update(complete)
        self._write_inputs()
        measurements = self._measurement_rows()
        for field in DISPOSITION_FIELDS:
            measurements[0][field] = ""
        self._write_csv(self.intake / "measurements.csv", measurements)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["outputs"]["measurements.csv"] = self._hash(self.intake / "measurements.csv")
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(StudyError, "intake reviewer disposition provenance is incomplete"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "missing-intake-disposition")

    def test_rejects_scale_without_provenance_and_tampered_packet(self):
        self.rows[0]["pixel_size_um"] = "0.5"
        self._write_inputs()
        with self.assertRaisesRegex(StudyError, "pixel size and its provenance"):
            prepare_development_packet(self.manifest, self.plan, self.receipt, self.root / "missing-scale")

        self.setUp()
        packet = self.root / "tampered"
        prepare_development_packet(self.manifest, self.plan, self.receipt, packet)
        with (packet / "review_packet.csv").open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            rows = list(reader)
        rows[0]["reviewer_disposition"] = "accepted"
        with (packet / "review_packet.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(StudyError, "exact regeneration"):
            validate_development_packet(packet, self.manifest, self.plan, self.receipt)


if __name__ == "__main__":
    unittest.main()
