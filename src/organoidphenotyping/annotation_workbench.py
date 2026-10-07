"""Local treatment-concealed polygon annotation and repeat-review workflow."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageDraw

from organoidphenotyping.annotation_pilot import KEY_FIELDS, QUEUE_FIELDS, PUBLIC_FILES
from organoidphenotyping.core import (
    ANNOTATION_DISPOSITION_CODES,
    ANNOTATION_DISPOSITION_RATIONALE_MAX_CHARS,
    StudyError,
)

MAX_SESSION_BYTES = 4_000_000
MAX_ANNOTATION_BYTES = 2_000_000
MAX_IMAGE_PIXELS = 100_000_000
MAX_POLYGONS = 2_000
MAX_POINTS = 100_000
TASK_ID = re.compile(r"^b-[0-9a-f]{16}$")
ANNOTATOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DISPOSITION_CODES = ANNOTATION_DISPOSITION_CODES
DISPOSITION_RATIONALE_MAX_CHARS = ANNOTATION_DISPOSITION_RATIONALE_MAX_CHARS


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict:
    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise StudyError(f"duplicate JSON key in {path.name}: {key}")
            output[key] = value
        return output
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise StudyError(f"{path.name} must contain a JSON object")
    return value


def _safe_file(root: Path, value: str) -> Path:
    rel = PurePosixPath(value.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise StudyError("annotation pilot path must stay inside its public directory")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise StudyError("annotation pilot files cannot use symbolic links")
    path = path.resolve(strict=True)
    try:
        path.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise StudyError("annotation pilot path escapes its public directory") from exc
    if path.is_symlink() or not path.is_file():
        raise StudyError("annotation pilot files must be regular files")
    return path


def _queue_rows(pilot_root: Path, receipt: dict) -> dict[str, dict]:
    public_root = (pilot_root / "public").resolve(strict=True)
    output_hashes = receipt.get("outputs")
    if not isinstance(output_hashes, dict) or set(output_hashes) != set(PUBLIC_FILES):
        raise StudyError("annotation pilot output index is incomplete or unexpected")
    for filename in PUBLIC_FILES:
        raw = _safe_file(public_root, filename).read_bytes()
        if _sha256(raw) != output_hashes[filename]:
            raise StudyError(f"annotation pilot output hash mismatch: {filename}")
    tasks: dict[str, dict] = {}
    for filename in ("annotation_queue_round1.csv", "annotation_queue_round2.csv"):
        try:
            with _safe_file(public_root, filename).open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if set(reader.fieldnames or []) != set(QUEUE_FIELDS):
                    raise StudyError(f"{filename} must contain exactly the blinded queue fields")
                rows = list(reader)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise StudyError(f"cannot read {filename}: {exc}") from exc
        for row in rows:
            task_id = (row.get("task_id") or "").strip()
            if not TASK_ID.fullmatch(task_id) or task_id in tasks:
                raise StudyError("blinded task IDs must be unique and valid")
            image_value, mask_value = row.get("image_path", ""), row.get("mask_path", "")
            image_path = _safe_file(public_root, image_value)
            if mask_value != f"masks/{task_id}.tif":
                raise StudyError("annotation task has an unexpected mask path")
            image_bytes = image_path.read_bytes()
            image_hash = _sha256(image_bytes)
            if image_hash != row.get("image_sha256"):
                raise StudyError("blinded image does not match its queue hash")
            if row.get("status") != "pending_annotation" or row.get("annotator_id"):
                raise StudyError("pilot queue must contain unassigned pending tasks")
            try:
                with Image.open(image_path) as image:
                    width, height = image.size
                    image.verify()
            except (OSError, Image.DecompressionBombError) as exc:
                raise StudyError("a task image cannot be safely decoded") from exc
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise StudyError("task image dimensions exceed the annotation limit")
            tasks[task_id] = {
                "task_id": task_id,
                "image_path": image_value,
                "image_sha256": image_hash,
                "annotation_target": row.get("annotation_target", ""),
                "mask_path": mask_value,
                "annotation_protocol_version": row.get("annotation_protocol_version", ""),
                "width": width,
                "height": height,
            }
    if not tasks:
        raise StudyError("annotation pilot has no tasks")
    expected = receipt.get("n_total_tasks")
    if isinstance(expected, bool) or expected != len(tasks):
        raise StudyError("queue task count does not match the pilot receipt")
    return tasks


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_SESSION_BYTES:
        raise StudyError("annotation session metadata exceeds the size limit")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def create_session(pilot: str | Path, manifest: str | Path, plan: str | Path,
                   output: str | Path) -> dict:
    """Create a new local annotation session from an intact blinded pilot pack."""
    pilot_root = Path(pilot).resolve(strict=True)
    manifest_path = Path(manifest).resolve(strict=True)
    plan_path = Path(plan).resolve(strict=True)
    study_root = manifest_path.parent
    output_path = Path(output).absolute()
    try:
        output_path.resolve().relative_to(study_root)
    except ValueError as exc:
        raise StudyError("annotation session output must be inside the source study directory") from exc
    receipt_path = pilot_root / "curator" / "pilot_receipt.json"
    receipt_raw = receipt_path.read_bytes()
    receipt = _read_json(receipt_path)
    if (receipt.get("tool") != "organoid-phenotyping"
            or receipt.get("activity") != "manual_annotation_pilot_plan"
            or receipt.get("masks_generated") is not False
            or receipt.get("biological_results_generated") is not False):
        raise StudyError("source is not a label-free organoid annotation pilot")
    if _sha256(manifest_path.read_bytes()) != receipt.get("input_manifest_sha256"):
        raise StudyError("source acquisition manifest does not match the exact pilot input")
    if _sha256(plan_path.read_bytes()) != receipt.get("study_plan_sha256"):
        raise StudyError("source study plan does not match the exact pilot input")
    key_path = pilot_root / "curator" / "assignment_key.csv"
    key_raw = key_path.read_bytes()
    if _sha256(key_raw) != receipt.get("private_assignment_key_sha256"):
        raise StudyError("private assignment key hash does not match the pilot receipt")
    try:
        with key_path.open("r", encoding="utf-8-sig", newline="") as handle:
            key_reader = csv.DictReader(handle)
            if not set(KEY_FIELDS).issubset(key_reader.fieldnames or []):
                raise StudyError("private assignment key is missing its audit columns")
            key_rows = list(key_reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise StudyError("cannot read the private assignment key for local integrity validation") from exc
    tasks = _queue_rows(pilot_root, receipt)
    if {row.get("task_id") for row in key_rows} != set(tasks):
        raise StudyError("blinded queue and private assignment key contain different tasks")
    if output_path.exists() or output_path.is_symlink():
        raise StudyError(f"annotation session output already exists: {output_path}")
    task_ids = list(tasks)
    secrets.SystemRandom().shuffle(task_ids)
    session_id = secrets.token_hex(8)
    session = {
        "schema_version": 1,
        "session_id": session_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "pilot_path": pilot_root.as_posix(),
        "pilot_receipt_sha256": _sha256(receipt_raw),
        "study_root": study_root.as_posix(),
        "manifest_path": manifest_path.as_posix(),
        "plan_path": plan_path.as_posix(),
        "manifest_sha256": receipt["input_manifest_sha256"],
        "plan_sha256": receipt["study_plan_sha256"],
        "task_order": task_ids,
        "tasks": tasks,
        "latest": {},
        "latest_dispositions": {},
        "disposition_history": [],
        "notice": "Manual masks require review. This annotation session has no biological outcome or treatment labels.",
    }
    stage = Path(tempfile.mkdtemp(prefix=f".annotation-session-{session_id}-", dir=output_path.parent))
    try:
        (stage / "masks").mkdir()
        (stage / "revisions").mkdir()
        _atomic_json(stage / "session.json", session)
        stage.rename(output_path)
    except BaseException:
        import shutil
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"session_id": session_id, "task_count": len(tasks), "session_path": output_path.as_posix(),
            "masks_generated": False, "biological_results_generated": False}


def _load_session(path: str | Path) -> tuple[Path, dict]:
    root = Path(path).resolve(strict=True)
    session = _read_json(root / "session.json")
    if (session.get("schema_version") != 1 or not isinstance(session.get("tasks"), dict)
            or not isinstance(session.get("latest"), dict) or not isinstance(session.get("task_order"), list)):
        raise StudyError("unsupported or malformed annotation session")
    # These fields were added without changing the session format so existing
    # v1 sessions can still be resumed and audited.
    session.setdefault("latest_dispositions", {})
    session.setdefault("disposition_history", [])
    if (not isinstance(session["latest_dispositions"], dict)
            or not isinstance(session["disposition_history"], list)
            or not set(session["latest_dispositions"]).issubset(session["tasks"])
            or any(not isinstance(entry, dict) for entry in session["latest_dispositions"].values())
            or any(not isinstance(entry, dict) for entry in session["disposition_history"])):
        raise StudyError("annotation session has a malformed disposition index")
    study_root = Path(session.get("study_root", "")).resolve(strict=True)
    try:
        root.relative_to(study_root)
    except ValueError as exc:
        raise StudyError("annotation session must remain inside its source study directory") from exc
    if _sha256((Path(session["pilot_path"]) / "curator" / "pilot_receipt.json").read_bytes()) != session.get("pilot_receipt_sha256"):
        raise StudyError("annotation pilot receipt changed since session creation")
    if (_sha256(Path(session["manifest_path"]).read_bytes()) != session.get("manifest_sha256")
            or _sha256(Path(session["plan_path"]).read_bytes()) != session.get("plan_sha256")):
        raise StudyError("source acquisition manifest or plan changed since session creation")
    return root, session


def rasterize_polygons(width: int, height: int, polygons: list) -> Image.Image:
    """Rasterize normalized polygon vertices as positive integer object labels."""
    if (isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int)
            or not isinstance(height, int) or width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS):
        raise StudyError("mask dimensions are invalid or exceed the supported pixel count")
    if not isinstance(polygons, list) or not 1 <= len(polygons) <= MAX_POLYGONS:
        raise StudyError(f"an annotation must contain 1–{MAX_POLYGONS} object polygons")
    mask = Image.new("I", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    total_points = 0
    for label, polygon in enumerate(polygons, start=1):
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise StudyError("each object polygon requires at least three vertices")
        total_points += len(polygon)
        if total_points > MAX_POINTS:
            raise StudyError("annotation contains too many polygon vertices")
        points = []
        for vertex in polygon:
            if not isinstance(vertex, list) or len(vertex) != 2:
                raise StudyError("polygon vertices must be normalized [x, y] pairs")
            x, y = vertex
            if (isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (float, int))
                    or not isinstance(y, (float, int)) or not math.isfinite(x) or not math.isfinite(y)
                    or not 0 <= x <= 1 or not 0 <= y <= 1):
                raise StudyError("polygon coordinates must be finite fractions from zero to one")
            points.append((round(float(x) * (width - 1)), round(float(y) * (height - 1))))
        twice_area = abs(sum(points[index][0] * points[(index + 1) % len(points)][1]
                             - points[(index + 1) % len(points)][0] * points[index][1]
                             for index in range(len(points))))
        if twice_area < 1:
            raise StudyError("polygon area must cover at least one pixel")
        draw.polygon(points, fill=label)
    return mask


def _dice(left: Image.Image, right: Image.Image) -> float | None:
    import numpy as np
    a, b = np.asarray(left) > 0, np.asarray(right) > 0
    if a.shape != b.shape:
        raise StudyError("concealed repeat masks do not have matching source-image dimensions")
    denominator = int(a.sum()) + int(b.sum())
    return 1.0 if denominator == 0 else 2.0 * int((a & b).sum()) / denominator


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_annotation(session_path: str | Path, task_id: str, annotator_id: str,
                    polygons: list) -> dict:
    root, session = _load_session(session_path)
    if not TASK_ID.fullmatch(task_id) or task_id not in session["tasks"]:
        raise StudyError("unknown blinded annotation task")
    if not isinstance(annotator_id, str) or not ANNOTATOR_ID.fullmatch(annotator_id):
        raise StudyError("annotator ID must be a short pseudonymous identifier")
    task = session["tasks"][task_id]
    pilot_root = Path(session["pilot_path"])
    public_root = pilot_root / "public"
    image_path = _safe_file(public_root.resolve(strict=True), task["image_path"])
    image_raw = image_path.read_bytes()
    if _sha256(image_raw) != task["image_sha256"]:
        raise StudyError("task image changed since the annotation session started")
    mask = rasterize_polygons(task["width"], task["height"], polygons)
    revision = secrets.token_hex(8)
    mask_rel = f"masks/{task_id}-{revision}.tif"
    annotation_rel = f"revisions/{task_id}-{revision}.json"
    mask_path = root / mask_rel
    temporary_mask = mask_path.with_suffix(".tif.tmp")
    mask.save(temporary_mask, format="TIFF", compression="raw")
    os.replace(temporary_mask, mask_path)
    mask_raw = mask_path.read_bytes()
    annotation = {
        "schema_version": 1, "task_id": task_id, "revision": revision,
        "image_sha256": task["image_sha256"], "mask_path": mask_rel,
        "mask_sha256": _sha256(mask_raw), "width": task["width"], "height": task["height"],
        "polygons": polygons, "annotator_id": annotator_id,
        "annotation_protocol_version": task["annotation_protocol_version"],
        "saved_utc": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(root / annotation_rel, annotation)
    session["latest"][task_id] = {"revision": revision, "annotation_path": annotation_rel,
                                  "mask_path": mask_rel, "mask_sha256": annotation["mask_sha256"]}
    # A later mask is the current task outcome; the disposition JSON remains
    # on disk as an immutable superseded revision.
    session.setdefault("latest_dispositions", {}).pop(task_id, None)
    _atomic_json(root / "session.json", session)
    return {"task_id": task_id, "revision": revision, "mask_sha256": annotation["mask_sha256"],
            "has_mask": True, "saved_utc": annotation["saved_utc"]}


def save_disposition(session_path: str | Path, task_id: str, annotator_id: str,
                     disposition: str, rationale: str) -> dict:
    """Record a reasoned non-mask outcome as an immutable task revision."""
    root, session = _load_session(session_path)
    if not TASK_ID.fullmatch(task_id) or task_id not in session["tasks"]:
        raise StudyError("unknown blinded annotation task")
    if not isinstance(annotator_id, str) or not ANNOTATOR_ID.fullmatch(annotator_id):
        raise StudyError("annotator ID must be a short pseudonymous identifier")
    if not isinstance(disposition, str) or disposition not in DISPOSITION_CODES:
        raise StudyError("disposition must be one of " + ", ".join(sorted(DISPOSITION_CODES)))
    if (not isinstance(rationale, str) or not 8 <= len(rationale.strip()) <= DISPOSITION_RATIONALE_MAX_CHARS
            or any(ord(char) < 32 and char not in "\t\n\r" for char in rationale)):
        raise StudyError("disposition rationale must contain 8–500 printable characters")
    task = session["tasks"][task_id]
    public_root = (Path(session["pilot_path"]) / "public").resolve(strict=True)
    image_path = _safe_file(public_root, task["image_path"])
    if _sha256(image_path.read_bytes()) != task["image_sha256"]:
        raise StudyError("task image changed since the annotation session started")
    revision = secrets.token_hex(8)
    saved_utc = datetime.now(timezone.utc).isoformat()
    record = {
        "schema_version": 1, "task_id": task_id, "revision": revision,
        "image_sha256": task["image_sha256"], "disposition": disposition,
        "rationale": rationale.strip(), "annotator_id": annotator_id,
        "annotation_protocol_version": task["annotation_protocol_version"],
        "saved_utc": saved_utc,
        "interpretation": "Task-routing annotation only; no mask or biological outcome was generated.",
    }
    relative_path = f"revisions/{task_id}-{revision}-disposition.json"
    record_path = root / relative_path
    _atomic_json(record_path, record)
    record_hash = _sha256(record_path.read_bytes())
    index = {"revision": revision, "disposition": disposition, "disposition_path": relative_path,
             "disposition_sha256": record_hash}
    session.setdefault("latest_dispositions", {})[task_id] = index
    session.setdefault("disposition_history", []).append({"task_id": task_id, **index})
    _atomic_json(root / "session.json", session)
    return {"task_id": task_id, "revision": revision, "disposition": disposition,
            "disposition_sha256": record_hash, "has_disposition": True, "saved_utc": saved_utc}


def _load_disposition(root: Path, session: dict, task_id: str) -> dict:
    latest = session.get("latest_dispositions", {}).get(task_id)
    if not isinstance(latest, dict):
        raise StudyError("annotation task has no saved disposition")
    revision = latest.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{16}", revision):
        raise StudyError("saved disposition revision is malformed")
    relative_path = f"revisions/{task_id}-{revision}-disposition.json"
    if latest.get("disposition_path") != relative_path:
        raise StudyError("saved disposition path does not match its task and revision")
    if (root / "revisions").is_symlink():
        raise StudyError("annotation revision directory cannot be a symbolic link")
    revisions_root = (root / "revisions").resolve(strict=True)
    record_path = root / relative_path
    if record_path.is_symlink():
        raise StudyError("saved disposition cannot be a symbolic link")
    record_path = record_path.resolve(strict=True)
    try:
        record_path.relative_to(revisions_root)
    except ValueError as exc:
        raise StudyError("saved disposition path escapes the revision directory") from exc
    raw = record_path.read_bytes()
    if _sha256(raw) != latest.get("disposition_sha256"):
        raise StudyError("saved disposition hash does not match the session index")
    record = _read_json(record_path)
    if not isinstance(record, dict):
        raise StudyError("saved disposition record is malformed")
    rationale = record.get("rationale")
    disposition = record.get("disposition")
    saved_utc = record.get("saved_utc")
    if (record.get("schema_version") != 1
            or record.get("task_id") != task_id or record.get("revision") != revision
            or not isinstance(disposition, str) or disposition not in DISPOSITION_CODES
            or latest.get("disposition") != disposition
            or not isinstance(rationale, str)
            or not 8 <= len(rationale.strip()) <= DISPOSITION_RATIONALE_MAX_CHARS
            or record.get("image_sha256") != session["tasks"][task_id]["image_sha256"]
            or record.get("annotation_protocol_version") != session["tasks"][task_id]["annotation_protocol_version"]
            or not isinstance(record.get("annotator_id"), str)
            or not ANNOTATOR_ID.fullmatch(record["annotator_id"])
            or not isinstance(saved_utc, str)):
        raise StudyError("saved disposition record is malformed")
    try:
        parsed_time = datetime.fromisoformat(saved_utc)
    except ValueError as exc:
        raise StudyError("saved disposition timestamp is malformed") from exc
    if parsed_time.tzinfo is None:
        raise StudyError("saved disposition timestamp must include a timezone")
    return record


def _load_annotation(root: Path, session: dict, task_id: str) -> tuple[dict, Image.Image]:
    latest = session["latest"].get(task_id)
    if not latest:
        raise StudyError("annotation task has no saved mask")
    annotation_path = (root / latest["annotation_path"]).resolve(strict=True)
    mask_path = (root / latest["mask_path"]).resolve(strict=True)
    annotation_path.relative_to((root / "revisions").resolve(strict=True))
    mask_path.relative_to((root / "masks").resolve(strict=True))
    annotation = _read_json(annotation_path)
    mask_raw = mask_path.read_bytes()
    if (annotation.get("task_id") != task_id or annotation.get("mask_sha256") != _sha256(mask_raw)
            or annotation.get("mask_sha256") != latest.get("mask_sha256")):
        raise StudyError("saved annotation mask hash does not match the session index")
    with Image.open(io.BytesIO(mask_raw)) as image:
        mask = image.copy()
    if mask.size != (session["tasks"][task_id]["width"], session["tasks"][task_id]["height"]):
        raise StudyError("saved mask dimensions do not match the source image")
    return annotation, mask


def audit_annotations(session_path: str | Path) -> dict:
    """Reveal repeat mapping only after an explicit local audit request."""
    root, session = _load_session(session_path)
    pilot_root = Path(session["pilot_path"])
    receipt = _read_json(pilot_root / "curator" / "pilot_receipt.json")
    key_path = pilot_root / receipt.get("private_assignment_key_path", "")
    if _sha256(key_path.read_bytes()) != receipt.get("private_assignment_key_sha256"):
        raise StudyError("private task key failed its receipt hash")
    with key_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(KEY_FIELDS).issubset(reader.fieldnames or []):
            raise StudyError("private task key is missing required identity fields")
        key_rows = list(reader)
    assignments = {row["task_id"]: row for row in key_rows}
    if set(assignments) != set(session["tasks"]):
        raise StudyError("private task key does not match this session's blinded tasks")
    current_dispositions = {
        task_id: _load_disposition(root, session, task_id)
        for task_id in session["latest_dispositions"]
    }
    disposition_history_records = []
    history_keys = set()
    history_entries = {}
    for entry in session["disposition_history"]:
        task_id = entry.get("task_id") if isinstance(entry, dict) else None
        revision = entry.get("revision") if isinstance(entry, dict) else None
        if not isinstance(revision, str):
            raise StudyError("annotation disposition history has a malformed revision")
        key = (task_id, revision)
        if (not isinstance(task_id, str) or task_id not in session["tasks"]
                or key in history_keys):
            raise StudyError("annotation disposition history has duplicate or unknown task revisions")
        record = _load_disposition(root, {
            "tasks": session["tasks"], "latest_dispositions": {task_id: entry},
        }, task_id)
        history_keys.add(key)
        history_entries[key] = entry
        current_index = session["latest_dispositions"].get(task_id, {})
        disposition_history_records.append({
            "task_id": task_id, "revision": revision,
            "disposition": record["disposition"], "annotator_id": record["annotator_id"],
            "saved_utc": record["saved_utc"], "disposition_sha256": entry["disposition_sha256"],
            "is_current": (current_index.get("revision") == revision
                           and current_index.get("disposition_sha256") == entry.get("disposition_sha256")),
        })
    if any((task_id, entry.get("revision")) not in history_keys
           or history_entries[(task_id, entry.get("revision"))].get("disposition_sha256")
           != entry.get("disposition_sha256")
           for task_id, entry in session["latest_dispositions"].items()):
        raise StudyError("current annotation dispositions are missing from the revision history")
    loaded = {}
    saved_masks = {}
    missing = []
    for task_id in session["task_order"]:
        if task_id in session["latest"]:
            saved = _load_annotation(root, session, task_id)
            saved_masks[task_id] = saved
            if task_id not in current_dispositions:
                loaded[task_id] = saved
        elif task_id in current_dispositions:
            continue
        else:
            missing.append(task_id)
    primary = {row["frame_id"]: row for row in key_rows if row["round"] == "primary"}
    repeat_results = []
    disposition_agreement = []
    repeat_outcome_mismatches = []
    for repeat in (row for row in key_rows if row["round"] == "concealed_repeat"):
        primary_row = primary.get(repeat.get("frame_id"))
        if primary_row is None or repeat.get("repeat_of_task_id") != primary_row["task_id"]:
            raise StudyError("concealed repeat mapping is inconsistent with its primary assignment")
        primary_id, repeat_id = primary_row["task_id"], repeat["task_id"]
        primary_annotation = loaded.get(primary_id)
        repeat_annotation = loaded.get(repeat_id)
        primary_disposition = current_dispositions.get(primary_id)
        repeat_disposition = current_dispositions.get(repeat_id)
        if primary_annotation and repeat_annotation:
            primary_record, primary_mask = primary_annotation
            repeat_record, repeat_mask = repeat_annotation
            repeat_results.append({
                "primary_task_id": primary_id, "repeat_task_id": repeat_id,
                "source_frame_id": repeat["frame_id"], "biological_unit_id": repeat["biological_unit_id"],
                "different_annotator_ids": primary_record["annotator_id"] != repeat_record["annotator_id"],
                "primary_annotator_id": primary_record["annotator_id"],
                "repeat_annotator_id": repeat_record["annotator_id"],
                "foreground_dice": _dice(primary_mask, repeat_mask),
                "interpretation": "Segmentation agreement only; visible morphology may reveal treatment, and agreement does not establish mask correctness or biological effect.",
            })
        elif primary_disposition and repeat_disposition:
            disposition_agreement.append({
                "primary_task_id": primary_id, "repeat_task_id": repeat_id,
                "source_frame_id": repeat["frame_id"], "biological_unit_id": repeat["biological_unit_id"],
                "primary_disposition": primary_disposition["disposition"],
                "repeat_disposition": repeat_disposition["disposition"],
                "same_disposition": primary_disposition["disposition"] == repeat_disposition["disposition"],
                "different_annotator_ids": primary_disposition["annotator_id"] != repeat_disposition["annotator_id"],
                "primary_annotator_id": primary_disposition["annotator_id"],
                "repeat_annotator_id": repeat_disposition["annotator_id"],
                "interpretation": "Task-routing agreement only; dispositions do not measure morphology, mask correctness or biological outcome.",
            })
        elif ((primary_annotation or primary_disposition)
              and (repeat_annotation or repeat_disposition)):
            primary_outcome = "mask" if primary_annotation else "disposition"
            repeat_outcome = "mask" if repeat_annotation else "disposition"
            repeat_outcome_mismatches.append({
                "primary_task_id": primary_id, "repeat_task_id": repeat_id,
                "source_frame_id": repeat["frame_id"], "biological_unit_id": repeat["biological_unit_id"],
                "primary_outcome": primary_outcome, "repeat_outcome": repeat_outcome,
                "interpretation": "Review required; a mask and a task disposition are not directly comparable outcomes.",
            })
    all_primary_complete = all(
        row["task_id"] in loaded or row["task_id"] in current_dispositions
        for row in key_rows if row["round"] == "primary"
    )
    independently_reviewed = sum(item["different_annotator_ids"] for item in repeat_results)
    independently_dispositioned = sum(item["different_annotator_ids"] for item in disposition_agreement)
    report = {
        "schema_version": 1, "activity": "manual_annotation_repeat_audit",
        "session_id": session["session_id"], "created_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": receipt.get("dataset"),
        "pilot_receipt_sha256": session["pilot_receipt_sha256"],
        "n_total_tasks": len(assignments), "n_annotated_tasks": len(loaded),
        "n_dispositioned_tasks": len(current_dispositions), "missing_task_ids": missing,
        "n_tasks_with_saved_masks": len(saved_masks),
        "n_superseded_mask_tasks": len(set(saved_masks) & set(current_dispositions)),
        "n_primary_tasks_complete": sum(row["round"] == "primary" and (
            row["task_id"] in loaded or row["task_id"] in current_dispositions) for row in key_rows),
        "n_primary_mask_tasks": sum(row["task_id"] in loaded for row in primary.values()),
        "n_primary_disposition_tasks": sum(row["task_id"] in current_dispositions for row in primary.values()),
        "n_primary_tasks": len(primary), "n_repeat_pairs_scored": len(repeat_results),
        "n_repeat_pairs_with_distinct_annotator_ids": independently_reviewed,
        "n_repeat_pairs_with_disposition_agreement": len(disposition_agreement),
        "n_repeat_disposition_pairs_with_distinct_annotator_ids": independently_dispositioned,
        "n_repeat_pairs_with_outcome_mismatch": len(repeat_outcome_mismatches),
        "disposition_counts": {code: sum(record["disposition"] == code for record in current_dispositions.values())
                                for code in sorted(DISPOSITION_CODES)},
        "disposition_revision_count": len(session["disposition_history"]),
        "n_disposition_revision_records": len(disposition_history_records),
        "masks_generated": bool(saved_masks), "dispositions_recorded": bool(current_dispositions),
        "biological_results_generated": False,
        "interpretation": "Blinded masks, task dispositions and repeat agreement are workflow outputs. They are not validated biological measurements, efficacy estimates, or proof that an annotation is correct.",
        "repeat_agreement": repeat_results,
        "repeat_disposition_agreement": disposition_agreement,
        "repeat_outcome_mismatches": repeat_outcome_mismatches,
    }
    audit_id = secrets.token_hex(8)
    annotated_manifest = None
    if all_primary_complete:
        manifest_path = Path(session["manifest_path"]).resolve(strict=True)
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        if _sha256(manifest_path.read_bytes()) != session["manifest_sha256"]:
            raise StudyError("source acquisition manifest changed after session creation")
        for optional in ("annotation_task_id", "mask_annotator_id", "mask_annotator_role", "annotation_protocol_version",
                         "segmentation_method", "segmentation_version", "expected_mask_sha256",
                         "annotation_disposition", "annotation_disposition_rationale",
                         "annotation_disposition_annotator_id", "annotation_disposition_task_id",
                         "annotation_disposition_revision",
                         "annotation_disposition_path", "expected_annotation_disposition_sha256"):
            if optional not in fields:
                fields.append(optional)
        row_by_frame = {row["frame_id"]: row for row in rows}
        for key in key_rows:
            if key["round"] != "primary":
                continue
            row = row_by_frame.get(key["frame_id"])
            if row is None or row.get("status") != "pending_annotation":
                raise StudyError("primary annotation task no longer maps to a pending source frame")
            task_id = key["task_id"]
            if task_id in loaded:
                annotation, _mask = loaded[task_id]
                latest = session["latest"][task_id]
                row.update({
                    "status": "measured", "status_reason": "Manual source-image polygon mask generated in the blinded annotation session; review is still required.",
                    "mask_path": PurePosixPath(root.relative_to(manifest_path.parent).as_posix(), latest["mask_path"]).as_posix(),
                    "annotation_task_id": task_id, "mask_annotator_id": annotation["annotator_id"],
                    "mask_annotator_role": "primary", "annotation_protocol_version": annotation["annotation_protocol_version"],
                    "segmentation_method": "manual polygon mask", "segmentation_version": annotation["annotation_protocol_version"],
                    "expected_mask_sha256": latest["mask_sha256"],
                })
            else:
                record = current_dispositions[task_id]
                latest = session["latest_dispositions"][task_id]
                disposition_path = PurePosixPath(
                    root.relative_to(manifest_path.parent).as_posix(), latest["disposition_path"]
                ).as_posix()
                row.update({
                    "status": "pending_annotation",
                    "status_reason": f"Manual annotation disposition: {record['disposition']}. {record['rationale']} Review remains required; no mask was generated.",
                    "mask_path": "", "annotation_disposition": record["disposition"],
                    "annotation_disposition_rationale": record["rationale"],
                    "annotation_disposition_annotator_id": record["annotator_id"],
                    "annotation_disposition_task_id": task_id,
                    "annotation_disposition_revision": record["revision"],
                    "annotation_disposition_path": disposition_path,
                    "expected_annotation_disposition_sha256": latest["disposition_sha256"],
                })
        manifest_name = f"acquisitions-annotated-{session['session_id']}-{audit_id}.csv"
        manifest_output = manifest_path.parent / manifest_name
        with manifest_output.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        annotated_manifest = manifest_output.relative_to(manifest_path.parent).as_posix()
    report["annotated_manifest"] = annotated_manifest
    report["annotated_manifest_sha256"] = (
        _sha256((Path(session["study_root"]) / annotated_manifest).read_bytes()) if annotated_manifest else None
    )
    report["all_primary_tasks_complete"] = all_primary_complete
    report["annotation_review_needed"] = True
    report["audit_id"] = audit_id
    audit_dir = root / "audits" / audit_id
    _atomic_json(audit_dir / "audit_report.json", report)
    _write_csv(audit_dir / "repeat_agreement.csv", [
        "primary_task_id", "repeat_task_id", "source_frame_id", "biological_unit_id",
        "different_annotator_ids", "primary_annotator_id", "repeat_annotator_id", "foreground_dice", "interpretation",
    ], repeat_results)
    key_by_task = assignments
    disposition_rows = []
    for history_record in disposition_history_records:
        history_key = (history_record["task_id"], history_record["revision"])
        record = _load_disposition(root, {
            "tasks": session["tasks"],
            "latest_dispositions": {history_record["task_id"]: history_entries[history_key]},
        }, history_record["task_id"])
        task_key = key_by_task[history_record["task_id"]]
        disposition_rows.append({
            "task_id": history_record["task_id"], "frame_id": task_key["frame_id"],
            "biological_unit_id": task_key["biological_unit_id"],
            "disposition": record["disposition"], "rationale": record["rationale"],
            "annotator_id": record["annotator_id"], "saved_utc": record["saved_utc"],
            "revision": record["revision"], "disposition_sha256": history_record["disposition_sha256"],
            "is_current": str(history_record["is_current"]).lower(),
            "interpretation": record["interpretation"],
        })
    _write_csv(audit_dir / "dispositions.csv", [
        "task_id", "frame_id", "biological_unit_id", "disposition", "rationale",
        "annotator_id", "saved_utc", "revision", "disposition_sha256", "is_current", "interpretation",
    ], disposition_rows)
    output_records = {}
    for filename in ("audit_report.json", "repeat_agreement.csv", "dispositions.csv"):
        raw = (audit_dir / filename).read_bytes()
        output_records[filename] = {"sha256": _sha256(raw), "size_bytes": len(raw)}
    receipt_record = {
        "schema_version": 1, "tool": "organoid-phenotyping", "activity": "manual_annotation_repeat_audit",
        "session_id": session["session_id"], "audit_id": audit_id,
        "pilot_receipt_sha256": session["pilot_receipt_sha256"],
        "input_manifest_sha256": session["manifest_sha256"], "study_plan_sha256": session["plan_sha256"],
        "masks_generated": bool(saved_masks), "dispositions_recorded": bool(current_dispositions),
        "biological_results_generated": False,
        "n_total_tasks": len(assignments), "n_annotated_tasks": len(loaded),
        "n_tasks_with_saved_masks": len(saved_masks),
        "n_superseded_mask_tasks": len(set(saved_masks) & set(current_dispositions)),
        "n_dispositioned_tasks": len(current_dispositions),
        "n_disposition_revisions": len(disposition_history_records),
        "n_repeat_pairs_scored": len(repeat_results),
        "n_repeat_pairs_with_distinct_annotator_ids": independently_reviewed,
        "n_repeat_disposition_pairs_with_distinct_annotator_ids": independently_dispositioned,
        "annotated_manifest": annotated_manifest,
        "annotated_manifest_sha256": report["annotated_manifest_sha256"],
        "annotation_mask_sha256": {task_id: value[0]["mask_sha256"] for task_id, value in loaded.items()},
        "superseded_annotation_mask_sha256": {
            task_id: saved_masks[task_id][0]["mask_sha256"]
            for task_id in set(saved_masks) & set(current_dispositions)
        },
        "outputs": output_records,
    }
    _atomic_json(audit_dir / "audit_receipt.json", receipt_record)
    session.setdefault("audits", []).append({"audit_id": audit_id, "report_path": f"audits/{audit_id}/audit_report.json",
                                             "repeat_path": f"audits/{audit_id}/repeat_agreement.csv",
                                             "disposition_path": f"audits/{audit_id}/dispositions.csv",
                                             "annotated_manifest": annotated_manifest})
    session["current_audit_id"] = audit_id
    _atomic_json(root / "session.json", session)
    return report


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Organoid mask annotation</title><style>
body{margin:0;background:#f4f6f3;color:#1d2b24;font:15px system-ui,sans-serif}header{padding:14px 22px;background:#1e4232;color:#fff}main{max-width:1350px;margin:20px auto;padding:0 18px}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.panel{background:#fff;border:1px solid #d8e1da;border-radius:7px;padding:15px;margin:12px 0}.boundary{color:#6b4b15;background:#fff7df;padding:10px;border-left:3px solid #d7a942}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}button,select,input,textarea{font:inherit;padding:8px;border:1px solid #bac8bf;border-radius:4px;background:#fff}textarea{min-width:260px;max-width:100%;resize:vertical}button{cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}canvas{max-width:100%;height:auto;border:1px solid #899991;touch-action:none;cursor:crosshair;background:#111}.layout{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:14px}.task-list{max-height:600px;overflow:auto}.task-list button{display:block;width:100%;text-align:left;margin:4px 0}.active{background:#dcefe2}.small{font-size:12px;color:#64736b}.object{color:#264a37}@media(max-width:850px){.layout{grid-template-columns:1fr}}
</style></head><body><header><strong>Organoid mask annotation</strong> <span>Local, blinded workflow</span></header><main>
<p class="boundary">Annotate only the requested visible boundary. This worklist hides treatment labels and source identities; appearance can still reveal condition. Concealed repeats support an independent agreement estimate only when different people annotate the paired tasks. Masks are manual geometry, not evidence of viability, function, maturation, regenerative effect, or rejuvenation.</p>
<section class="panel row"><label>Annotator pseudonym <input id="annotator" maxlength="64" autocomplete="off" placeholder="e.g. reviewer-a"></label><label>Task <select id="task"></select></label><span id="progress" class="small"></span><button id="previous">Previous</button><button id="next">Next</button></section>
<div class="layout"><section class="panel"><div class="row"><strong id="target"></strong><span id="image-size" class="small"></span></div><div class="toolbar"><button id="finish">Finish polygon</button><button id="undo">Undo vertex</button><button id="remove-object">Remove last polygon</button><button id="clear">Clear annotation</button><button id="save">Save mask</button></div><canvas id="image"></canvas><p class="small">Click to place vertices. Finish a polygon to add one positive instance label. Coordinates are saved at source image resolution. The server supplies an overview preview capped at 1600 pixels; zoom changes its display size but adds no image detail.</p><section class="panel"><h3>Task disposition (no mask)</h3><div class="row"><label>Outcome <select id="disposition"><option value="">Choose a disposition</option><option value="no_visible_target">No visible target</option><option value="ambiguous">Ambiguous boundary</option><option value="occluded">Occluded</option><option value="cropped">Cropped by field edge</option><option value="unusable">Unusable image</option></select></label><label>Reason <textarea id="disposition-rationale" rows="3" maxlength="500" placeholder="Briefly describe the visible limitation and what review is needed."></textarea></label><button id="save-disposition">Save disposition</button></div><p class="small">Use this when a defensible mask cannot be drawn. A disposition remains pending annotation, records its rationale and reviewer, and never creates a mask or biological measurement.</p></section><div id="status" role="status"></div></section><aside class="panel task-list"><h2>Blinded tasks</h2><div id="tasks"></div></aside></div></main>
<script>
const $=s=>document.querySelector(s), state={tasks:[],current:null,polygons:[],draft:[],preview:null,selection:0,loading:false,saving:false,dirty:false,dispositionDirty:false,savedDisposition:null};
const taskSelect=$('#task'),canvas=$('#image'),ctx=canvas.getContext('2d');
const viewport=document.createElement('div'),zoom=document.createElement('input'),zoomLabel=document.createElement('span'),zoomFit=document.createElement('button'),zoomRow=document.createElement('div');
const panel=canvas.parentNode;viewport.style.cssText='width:100%;height:70vh;max-width:100%;overflow:auto;border:1px solid #899991';canvas.style.maxWidth='none';canvas.style.border='0';zoomRow.className='toolbar';zoom.type='range';zoom.min='0.05';zoom.max='4';zoom.step='0.05';zoom.value='1';zoom.setAttribute('aria-label','Image display scale');zoomLabel.className='small';zoomFit.type='button';zoomFit.textContent='Fit image';zoomFit.addEventListener('click',fitImage);zoom.addEventListener('input',()=>setZoom(zoom.value));const zoomText=document.createElement('span');zoomText.textContent='Display scale';zoomRow.append(zoomText,zoom,zoomLabel,zoomFit);panel.insertBefore(zoomRow,canvas);panel.insertBefore(viewport,zoomRow.nextSibling);viewport.append(canvas);
function status(text){$('#status').textContent=text}
function currentSelection(id,selection){return state.current===id&&state.selection===selection}
function setZoom(value){const scale=Math.max(0.05,Math.min(4,Number(value)||1));zoom.value=String(scale);zoomLabel.textContent=`${Math.round(scale*100)}%`;if(state.preview){canvas.style.width=`${Math.round(canvas.width*scale)}px`;canvas.style.height=`${Math.round(canvas.height*scale)}px`}}
function fitImage(){if(!state.preview)return;const width=viewport.clientWidth||window.innerWidth*0.7,height=viewport.clientHeight||window.innerHeight*0.65;setZoom(Math.min(1,width/canvas.width,height/canvas.height))}
function hasUnsavedEdits(){return state.dirty||state.dispositionDirty}
function syncDispositionDirty(){const code=$('#disposition').value,rationale=$('#disposition-rationale').value.trim();state.dispositionDirty=state.savedDisposition?code!==state.savedDisposition.disposition||rationale!==state.savedDisposition.rationale:!!(code||rationale)}
function syncControls(){const locked=state.loading||state.saving;for(const selector of ['#finish','#undo','#remove-object','#clear'])$(selector).disabled=locked||!state.preview;for(const selector of ['#save','#save-disposition'])$(selector).disabled=locked||!state.preview;taskSelect.disabled=state.saving;$('#previous').disabled=state.saving;$('#next').disabled=state.saving;}
function draw(){if(!state.preview)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(state.preview,0,0);const drawPoly=(poly,color)=>{ctx.beginPath();poly.forEach((p,i)=>{const x=p[0]*canvas.width,y=p[1]*canvas.height;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});if(poly.length>2)ctx.closePath();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();ctx.fillStyle=color.replace('1)','0.20)');if(poly.length>2)ctx.fill()};state.polygons.forEach((p,i)=>drawPoly(p,`hsla(${i*67%360},85%,55%,1)`));drawPoly(state.draft,'rgba(255,220,50,1)');}
async function selectTask(id){if(state.saving){status('Wait for the current save to finish before changing tasks.');return false}if(id===state.current&&!state.loading&&state.preview)return true;if(hasUnsavedEdits()&&!window.confirm('Discard the unsaved mask or disposition edits for this task?')){if(state.current)taskSelect.value=state.current;return false}const task=state.tasks.find(item=>item.task_id===id);if(!task)return false;const selection=++state.selection;state.current=id;state.polygons=[];state.draft=[];state.preview=null;state.dirty=false;state.dispositionDirty=false;state.savedDisposition=null;state.loading=true;canvas.width=0;canvas.height=0;canvas.style.width='0px';canvas.style.height='0px';taskSelect.value=id;$('#target').textContent=`Annotation target: ${task.annotation_target}`;$('#image-size').textContent=`${task.width} × ${task.height} source pixels`;$('#progress').textContent='Loading image and saved task outcome…';status('Loading the selected task. Annotation controls are paused.');syncControls();renderTaskButtons();try{const img=new Image();img.src=`/api/image/${encodeURIComponent(id)}`;await img.decode();if(!currentSelection(id,selection))return false;const responses=await Promise.all([fetch(`/api/annotation/${encodeURIComponent(id)}`),fetch(`/api/disposition/${encodeURIComponent(id)}`)]);let savedMask=null,savedDisposition=null;if(responses[0].ok)savedMask=await responses[0].json();else if(responses[0].status!==404)throw new Error(`Saved mask request returned ${responses[0].status}`);if(responses[1].ok)savedDisposition=await responses[1].json();else if(responses[1].status!==404)throw new Error(`Disposition request returned ${responses[1].status}`);if(!currentSelection(id,selection))return false;state.preview=img;canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;state.savedDisposition=savedDisposition;if(savedMask?.polygons&&!savedDisposition)state.polygons=savedMask.polygons;$('#disposition').value=savedDisposition?.disposition||'';$('#disposition-rationale').value=savedDisposition?.rationale||'';state.dispositionDirty=false;draw();fitImage();const index=state.tasks.findIndex(item=>item.task_id===id);const outcome=savedDisposition?`disposition: ${savedDisposition.disposition}`:task.has_mask?'saved mask':'awaiting annotation';$('#progress').textContent=`Task ${index+1} of ${state.tasks.length} · ${outcome}`;status(savedDisposition?`Showing the saved ${savedDisposition.disposition} disposition. No mask was generated for this task outcome.`:task.has_mask?'Showing the latest saved polygon mask.':'No mask or disposition saved for this task.');return true}catch(error){if(currentSelection(id,selection)){state.preview=null;canvas.width=0;canvas.height=0;canvas.style.width='0px';canvas.style.height='0px';$('#progress').textContent='Task could not be loaded';status(`Unable to load this task safely: ${error.message}`)}return false}finally{if(currentSelection(id,selection)){state.loading=false;syncControls()}}}
function renderTaskButtons(){const box=$('#tasks');box.replaceChildren();for(const task of state.tasks){const mark=task.has_disposition?'!':task.has_mask?'✓':'○';const b=document.createElement('button');b.textContent=`${mark} ${task.task_id}`;b.className=task.task_id===state.current?'active':'';b.title=task.has_disposition?`Disposition recorded: ${task.disposition||'review'}`:task.has_mask?'Mask saved':'Pending annotation';b.addEventListener('click',()=>selectTask(task.task_id));box.append(b)}}
async function refresh(reselect=true){const data=await fetch('/api/session').then(r=>r.json());state.tasks=data.tasks;taskSelect.replaceChildren();for(const task of state.tasks){const option=document.createElement('option');option.value=task.task_id;option.textContent=`${task.has_disposition?'Disposition':task.has_mask?'Saved mask':'Pending'} · ${task.task_id}`;taskSelect.append(option)}renderTaskButtons();if(reselect&&state.tasks.length)await selectTask(state.current||state.tasks[0].task_id);}
canvas.addEventListener('pointerdown',event=>{if(!state.preview||state.loading||state.saving)return;const rect=canvas.getBoundingClientRect();state.draft.push([(event.clientX-rect.left)/rect.width,(event.clientY-rect.top)/rect.height]);state.dirty=true;draw()});
$('#finish').addEventListener('click',()=>{if(state.draft.length<3){status('A polygon requires at least three vertices.');return}state.polygons.push(state.draft);state.draft=[];state.dirty=true;draw();status(`${state.polygons.length} object boundary/boundaries ready to save.`)});
$('#undo').addEventListener('click',()=>{if(state.draft.length){state.draft.pop();state.dirty=true}draw()});$('#remove-object').addEventListener('click',()=>{if(state.polygons.length){state.polygons.pop();state.dirty=true}draw()});$('#clear').addEventListener('click',()=>{if(state.polygons.length||state.draft.length)state.dirty=true;state.polygons=[];state.draft=[];draw()});
$('#disposition').addEventListener('change',()=>syncDispositionDirty());$('#disposition-rationale').addEventListener('input',()=>syncDispositionDirty());
$('#save').addEventListener('click',async()=>{if(state.loading||state.saving||!state.preview)return;if(state.dispositionDirty){status('Save or restore the edited disposition before saving a mask.');return}if(state.draft.length){status('Finish or discard the active polygon before saving.');return}if(!state.polygons.length){status('Draw at least one polygon before saving.');return}const annotator=$('#annotator').value.trim();if(!annotator){status('Enter a pseudonymous annotator ID.');return}const taskId=state.current,selection=state.selection,polygons=state.polygons.map(poly=>poly.map(point=>[...point]));localStorage.setItem('organoid-annotator-id',annotator);state.saving=true;syncControls();try{const response=await fetch('/api/annotation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId,annotator_id:annotator,polygons})});const result=await response.json();if(!response.ok)throw new Error(result.error||'Unable to save the mask.');if(!currentSelection(taskId,selection))return;state.dirty=false;state.savedDisposition=null;$('#disposition').value='';$('#disposition-rationale').value='';state.dispositionDirty=false;const index=state.tasks.findIndex(item=>item.task_id===taskId);$('#progress').textContent=`Task ${index+1} of ${state.tasks.length} · saved mask`;status(`Saved revision ${result.revision}. This mask remains subject to human review.`);await refresh(false)}catch(error){if(currentSelection(taskId,selection))status(`Unable to save the mask: ${error.message}`)}finally{state.saving=false;syncControls()}});
$('#save-disposition').addEventListener('click',async()=>{if(state.loading||state.saving||!state.preview)return;if(state.dirty){if(!window.confirm('Discard unsaved polygon edits and record this disposition instead?'))return;state.polygons=[];state.draft=[];state.dirty=false;draw()}const annotator=$('#annotator').value.trim(),disposition=$('#disposition').value,rationale=$('#disposition-rationale').value.trim();if(!annotator){status('Enter a pseudonymous annotator ID.');return}if(!disposition){status('Choose a task disposition.');return}if(rationale.length<8||rationale.length>500){status('Add a brief rationale of 8–500 characters.');return}const taskId=state.current,selection=state.selection;localStorage.setItem('organoid-annotator-id',annotator);state.saving=true;syncControls();try{const response=await fetch('/api/disposition',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId,annotator_id:annotator,disposition,rationale})});const result=await response.json();if(!response.ok)throw new Error(result.error||'Unable to save the disposition.');if(!currentSelection(taskId,selection))return;state.dirty=false;state.dispositionDirty=false;state.savedDisposition={disposition,rationale};state.polygons=[];state.draft=[];draw();const index=state.tasks.findIndex(item=>item.task_id===taskId);$('#progress').textContent=`Task ${index+1} of ${state.tasks.length} · disposition: ${disposition}`;status(`Saved disposition revision ${result.revision}. The task remains pending annotation and no mask was generated.`);await refresh(false)}catch(error){if(currentSelection(taskId,selection))status(`Unable to save the disposition: ${error.message}`)}finally{state.saving=false;syncControls()}});
taskSelect.addEventListener('change',()=>selectTask(taskSelect.value));$('#next').addEventListener('click',()=>{const i=state.tasks.findIndex(t=>t.task_id===state.current);selectTask(state.tasks[(i+1)%state.tasks.length].task_id)});$('#previous').addEventListener('click',()=>{const i=state.tasks.findIndex(t=>t.task_id===state.current);selectTask(state.tasks[(i-1+state.tasks.length)%state.tasks.length].task_id)});$('#annotator').value=localStorage.getItem('organoid-annotator-id')||'';refresh().catch(error=>status(`Could not load the local task queue: ${error}`));
</script></body></html>"""


def serve_annotation_session(session_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise StudyError("annotation server can bind only to loopback")
    root, session = _load_session(session_path)
    lock = threading.RLock()
    preview_cache = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "OrganoidAnnotator/1"

        def log_message(self, *_args):
            pass

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict):
            self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json")

        def _allowed(self, mutation: bool = False):
            host_header = self.headers.get("Host", "")
            if not re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", host_header):
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host_header}", f"https://{host_header}"}:
                return False
            if mutation and self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return False
            return True

        def do_GET(self):
            if not self._allowed():
                return self._json(403, {"error": "Local same-origin access is required."})
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/session":
                with lock:
                    current = _read_json(root / "session.json")
                    tasks = []
                    for task_id in current["task_order"]:
                        task = current["tasks"][task_id]
                        has_disposition = task_id in current["latest_dispositions"]
                        tasks.append({"task_id": task_id, "annotation_target": task["annotation_target"],
                                      "width": task["width"], "height": task["height"],
                                      "has_mask": task_id in current["latest"] and not has_disposition,
                                      "has_disposition": has_disposition,
                                      "disposition": current["latest_dispositions"].get(task_id, {}).get("disposition")})
                    return self._json(200, {"session_id": current["session_id"], "tasks": tasks,
                                            "notice": current["notice"]})
            image_match = re.fullmatch(r"/api/image/([^/]+)", path)
            if image_match:
                task_id = unquote(image_match.group(1))
                with lock:
                    current = _read_json(root / "session.json")
                    task = current["tasks"].get(task_id)
                    if task is None:
                        return self._json(404, {"error": "Unknown task."})
                    cached = preview_cache.get(task_id)
                    if cached is None:
                        public_root = Path(current["pilot_path"]) / "public"
                        image_path = _safe_file(public_root.resolve(strict=True), task["image_path"])
                        raw = image_path.read_bytes()
                        if _sha256(raw) != task["image_sha256"]:
                            return self._json(409, {"error": "Task image hash changed."})
                        try:
                            with Image.open(io.BytesIO(raw)) as image:
                                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                                output = io.BytesIO(); image.convert("RGB").save(output, format="PNG", optimize=True)
                            cached = output.getvalue()
                        except (OSError, Image.DecompressionBombError):
                            return self._json(422, {"error": "Task image cannot be decoded."})
                        preview_cache[task_id] = cached
                    return self._send(200, cached, "image/png")
            annotation_match = re.fullmatch(r"/api/annotation/([^/]+)", path)
            if annotation_match:
                task_id = unquote(annotation_match.group(1))
                with lock:
                    current = _read_json(root / "session.json")
                    if task_id not in current["tasks"]:
                        return self._json(404, {"error": "Unknown task."})
                    latest = current["latest"].get(task_id)
                    if not latest:
                        return self._json(404, {"error": "No saved mask."})
                    value = _read_json(root / latest["annotation_path"])
                    return self._json(200, {"polygons": value["polygons"], "annotator_id": value["annotator_id"],
                                            "revision": value["revision"]})
            disposition_match = re.fullmatch(r"/api/disposition/([^/]+)", path)
            if disposition_match:
                task_id = unquote(disposition_match.group(1))
                with lock:
                    current = _read_json(root / "session.json")
                    if task_id not in current["tasks"]:
                        return self._json(404, {"error": "Unknown task."})
                    if task_id not in current.get("latest_dispositions", {}):
                        return self._json(404, {"error": "No saved task disposition."})
                    value = _load_disposition(root, current, task_id)
                    return self._json(200, {
                        "disposition": value["disposition"], "rationale": value["rationale"],
                        "annotator_id": value["annotator_id"], "revision": value["revision"],
                        "saved_utc": value["saved_utc"],
                    })
            return self._json(404, {"error": "Not found."})

        def do_POST(self):
            if not self._allowed(mutation=True):
                return self._json(403, {"error": "Local same-origin JSON access is required."})
            request_path = urlsplit(self.path).path
            if request_path not in {"/api/annotation", "/api/disposition"}:
                return self._json(404, {"error": "Not found."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json(400, {"error": "Invalid content length."})
            if not 1 <= length <= MAX_ANNOTATION_BYTES:
                return self._json(413, {"error": "Annotation request exceeds the size limit."})
            try:
                request = json.loads(self.rfile.read(length))
                if request_path == "/api/annotation":
                    if not isinstance(request, dict) or set(request) != {"task_id", "annotator_id", "polygons"}:
                        raise StudyError("annotation request has an unexpected shape")
                elif not isinstance(request, dict) or set(request) != {
                    "task_id", "annotator_id", "disposition", "rationale"
                }:
                    raise StudyError("disposition request has an unexpected shape")
                with lock:
                    if request_path == "/api/annotation":
                        result = save_annotation(root, request["task_id"], request["annotator_id"], request["polygons"])
                    else:
                        result = save_disposition(
                            root, request["task_id"], request["annotator_id"],
                            request["disposition"], request["rationale"],
                        )
                return self._json(200, result)
            except (StudyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc)[:500]})

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"Blinded annotation desk: http://127.0.0.1:{httpd.server_address[1]}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
