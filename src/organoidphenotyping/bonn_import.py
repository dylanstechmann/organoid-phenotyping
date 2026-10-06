"""Import the licensed Bonn kidney-tubuloid cyst-induction image archive."""

from __future__ import annotations

import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from organoidphenotyping.core import StudyError


DATASET_DOI = "10.60507/FK2/OM25XQ"
DATASET_URL = f"https://doi.org/{DATASET_DOI}"
FIGURE4_FILE_ID = 14509
FIGURE4_BYTES = 904_984_408
FIGURE4_MD5 = "89dbdd5be4b3d4c4602536f35bca3f70"
FIGURE4_API_URL = f"https://bonndata.uni-bonn.de/api/access/datafile/{FIGURE4_FILE_ID}"
SOURCE_LICENSE = "CC-BY-4.0"
MICROSCOPE = "Leica_DM_IRB_Nikon_DSU3"
IMAGING_LAB = "University_of_Bonn"
FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_kidney-(?P<kidney>\d+(?:-\d+)*)_"
    r"P(?P<passage>\d+)D(?P<culture_day>\d+)_"
    r"(?P<condition>Domes|Suspension)_"
    r"(?:P(?P<post_passage>\d+)D(?P<post_culture_day>\d+)_)?"
    r"(?P<treatment>Forskolin|DMSO|Media)-(?P<replicate>[A-C])_"
    r"(?P<timepoint_h>\d+)h_BF_(?P<magnification>5x)\.tif$"
)


def parse_figure4_filename(filename: str) -> dict[str, str]:
    """Parse source-defined identity and acquisition fields without guessing."""
    match = FILENAME_RE.fullmatch(Path(filename).name)
    if not match:
        raise StudyError(f"unrecognized Bonn Figure 4 image filename: {filename}")
    metadata = match.groupdict()
    metadata["source_passage_day_token"] = f"P{metadata['passage']}D{metadata['culture_day']}"
    metadata["post_condition_passage_day_token"] = (
        f"P{metadata['post_passage']}D{metadata['post_culture_day']}"
        if metadata["post_passage"] is not None else ""
    )
    metadata.pop("post_passage")
    metadata.pop("post_culture_day")
    return metadata


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def import_bonn_figure4(archive_path: str | Path, output_path: str | Path,
                        *, retrieved_on: str | None = None) -> dict:
    """Extract source TIFFs, freeze a kidney-ID split and write an annotation-ready manifest.

    The import records image acquisitions as ``pending_annotation``. It does
    not create masks, infer pixel scale, or report a biological measurement.
    """
    archive_path = Path(archive_path).resolve(strict=True)
    output_path = Path(output_path).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise StudyError(f"output already exists: {output_path}")
    if archive_path.stat().st_size != FIGURE4_BYTES:
        raise StudyError(
            f"source archive must match the recorded {FIGURE4_BYTES}-byte Bonn Figure 4 file"
        )
    archive_md5 = _hash_file(archive_path, "md5")
    if archive_md5 != FIGURE4_MD5:
        raise StudyError("source archive MD5 does not match the Bonn Dataverse file record")
    archive_sha256 = _hash_file(archive_path, "sha256")
    try:
        retrieval_date = date.fromisoformat(retrieved_on).isoformat() if retrieved_on else date.today().isoformat()
    except ValueError as exc:
        raise StudyError("retrieved_on must be an ISO date") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_path.name}-", dir=output_path.parent) as temporary:
        staged = Path(temporary)
        images_dir = staged / "images"
        images_dir.mkdir()
        rows: list[dict[str, str]] = []
        kidney_ids: set[str] = set()
        frame_ids: set[str] = set()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                image_entries = [entry for entry in archive.infolist()
                                 if not entry.is_dir() and entry.filename.lower().endswith(".tif")]
                if not image_entries:
                    raise StudyError("Bonn Figure 4 archive contains no TIFF images")
                if len(image_entries) != 280:
                    raise StudyError(f"expected 280 source TIFFs in the pinned archive, found {len(image_entries)}")
                for entry in image_entries:
                    source_name = Path(entry.filename).name
                    if source_name != entry.filename or source_name in {"", ".", ".."}:
                        raise StudyError(f"archive member is not a simple image filename: {entry.filename}")
                    metadata = parse_figure4_filename(source_name)
                    kidney_id = f"kidney_{metadata['kidney']}"
                    kidney_ids.add(kidney_id)
                    specimen_id = "_".join((kidney_id, metadata["condition"],
                                             metadata["treatment"], metadata["replicate"]))
                    frame_id = Path(source_name).stem
                    if frame_id in frame_ids:
                        raise StudyError(f"duplicate source image identity in archive: {frame_id}")
                    frame_ids.add(frame_id)
                    relative_image = f"images/{source_name}"
                    target = images_dir / source_name
                    with archive.open(entry) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                    rows.append({
                        "frame_id": frame_id,
                        "specimen_id": specimen_id,
                        "biological_unit_id": kidney_id,
                        "clone_id": "not_reported",
                        "culture_batch_id": "not_reported",
                        "imaging_lab": IMAGING_LAB,
                        "microscope_id": MICROSCOPE,
                        "timepoint_h": metadata["timepoint_h"],
                        "image_path": relative_image,
                        "mask_path": "",
                        "status": "pending_annotation",
                        "status_reason": "Source image indexed; no instance mask was supplied or generated.",
                        "source_uri": DATASET_URL,
                        "license": SOURCE_LICENSE,
                        "culture_condition": metadata["condition"],
                        "treatment": metadata["treatment"],
                        "technical_replicate": metadata["replicate"],
                        "acquisition_date": metadata["date"],
                        "passage": metadata["passage"],
                        "culture_day": metadata["culture_day"],
                        "source_passage_day_token": metadata["source_passage_day_token"],
                        "post_condition_passage_day_token": metadata["post_condition_passage_day_token"],
                        "magnification": metadata["magnification"],
                    })
        except (OSError, zipfile.BadZipFile) as exc:
            raise StudyError(f"cannot read Bonn Figure 4 archive: {exc}") from exc

        if len(kidney_ids) < 2:
            raise StudyError("at least two source kidney IDs are required for a grouped development/test split")
        ranked = sorted(kidney_ids, key=lambda value: hashlib.sha256(
            f"organoid-phenotyping-bonn-cyst-v1:{value}".encode("utf-8")).hexdigest())
        final_test = ranked[:1]
        development = sorted(kidney_ids - set(final_test))
        fields = list(rows[0])
        with (staged / "acquisitions.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: row["frame_id"]))

        plan = {
            "schema_version": 1,
            "dataset": {
                "dataset_id": DATASET_DOI,
                "title": "Microscopic analysis of human ASC-derived kidney organoids for polycystic kidney disease modelling — Figure 4 cyst induction",
                "source_url": DATASET_URL,
                "license": SOURCE_LICENSE,
                "retrieved_on": retrieval_date,
                "publication_doi": "10.1186/s12860-026-00591-x",
                "source_file_id": FIGURE4_FILE_ID,
                "source_file_api_url": FIGURE4_API_URL,
                "source_archive_bytes": archive_path.stat().st_size,
                "source_archive_md5": archive_md5,
                "source_archive_sha256": archive_sha256,
                "source_readme_license_statement": "Dataset README states CC-BY-4.0.",
            },
            "split": {
                "frozen_before_model_fit": True,
                "grouping_field": "biological_unit_id",
                "grouping_unit": "source kidney identifier",
                "development_group_ids": development,
                "final_test_group_ids": final_test,
                "selection_rule": "One source kidney identifier selected by the minimum SHA-256 rank of organoid-phenotyping-bonn-cyst-v1:<identifier>; other groups form development. The rank rule does not use images, treatments or outcomes.",
                "acquisition_holdout_microscope_ids": [],
            },
            "analysis_scope": {
                "purpose": "Reproducible acquisition inventory and future image-mask measurement of source-provided kidney-tubuloid cyst-induction microscopy.",
                "specimen_id_definition": "Source kidney, culture condition, treatment and A/B/C technical replicate identify the source culture unit; image filenames do not identify individual tracked tubuloids.",
                "segmentation_status": "No mask was included in the Figure 4 archive. Imported rows remain pending_annotation until a reviewed segmentation is supplied.",
                "object_tracking_status": "No individual object tracks are encoded in source filenames. Cross-timepoint object growth requires a separately reviewed object track map.",
                "pixel_scale_status": "No physical pixel size is assigned by this importer. Use a recorded calibration source before reporting physical units.",
                "biological_claim": "Image morphology is not a viability, renal-function, disease-modification or rejuvenation assay.",
            },
        }
        (staged / "study-plan.json").write_text(json.dumps(plan, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (staged / "source-archive.json").write_text(json.dumps({
            "source_url": DATASET_URL,
            "source_license": SOURCE_LICENSE,
            "source_file_api_url": FIGURE4_API_URL,
            "source_file_id": FIGURE4_FILE_ID,
            "filename": archive_path.name,
            "bytes": archive_path.stat().st_size,
            "md5": archive_md5,
            "sha256": archive_sha256,
            "retrieved_on": retrieval_date,
            "image_count": len(rows),
            "kidney_ids": sorted(kidney_ids),
        }, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        output_path.mkdir()
        try:
            for entry in staged.iterdir():
                entry.rename(output_path / entry.name)
        except BaseException:
            shutil.rmtree(output_path)
            raise
    return {
        "study_directory": str(output_path),
        "n_source_images": len(rows),
        "kidney_ids": sorted(kidney_ids),
        "development_group_ids": development,
        "final_test_group_ids": final_test,
        "status": "source_images_indexed_pending_annotation",
        "source_archive_sha256": archive_sha256,
    }
