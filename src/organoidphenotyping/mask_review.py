"""An append-only review ledger for frozen masks and dispositions.

A mask that exists is not a mask that anyone has accepted. This ledger records, for each task,
the frozen revisions that were submitted, the reviews they received and any adjudication, so a
benchmark can be given only masks whose latest revision was accepted and whose bytes still match.

Rules, enforced when an event is appended:

* A submission freezes one revision. An edit is a new submission with the next revision number
  and a different mask hash. A frozen revision is never rewritten.
* A reviewer must differ from the annotator and review in a different session. This is
  bookkeeping. Identifiers and the clock are self-reported and unauthenticated, so nothing here
  shows that two people were independent.
* A reviewer reviews a revision once. A revision is ``accepted`` only when the number of accepting
  reviews reaches the ledger's required count and nobody rejected it. A mix of accepting and
  rejecting reviews is a ``disagreement`` and only an adjudication resolves it; the adjudicator
  must be neither the annotator nor a reviewer of that revision.
* Only the latest revision counts. A new submission resets the task to ``submitted``; earlier
  decisions stay in the history.
* Each event carries the SHA-256 of the previous event, so removal or alteration of a past event
  is detectable. This is tamper evidence for accident, not protection against someone who can
  rewrite the whole file.

``export_accepted`` returns accepted tasks whose mask file still has the recorded hash, and lists
everything else, with the reason, in ``excluded``. Excluded items are kept for audit and are
never ground truth. An accepted mask has been reviewed under this ledger's rules; that is not a
statement that it is correct, that the object is a particular tissue, or that any assay result
follows from it.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

LEDGER_NAME = "mask_review_ledger.jsonl"
SCHEMA_VERSION = 1
GENESIS_PREVIOUS = "0" * 64
MAX_LEDGER_BYTES = 20_000_000
MAX_MASK_BYTES = 200_000_000
SHA256 = re.compile(r"[0-9a-f]{64}")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
KINDS = ("mask", "disposition")
DECISIONS = ("accepted", "rejected")

STATE_SUBMITTED = "submitted"
STATE_IN_REVIEW = "in_review"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
STATE_DISAGREEMENT = "disagreement"
STATE_ADJ_ACCEPTED = "adjudicated_accepted"
STATE_ADJ_REJECTED = "adjudicated_rejected"
USABLE_STATES = (STATE_ACCEPTED, STATE_ADJ_ACCEPTED)

LIMITS = [
    "Actor identifiers, session identifiers and times are self-reported and unauthenticated.",
    "Different identifiers and sessions do not show that two reviewers were independent.",
    "Accepted means reviewed under these rules, not that the mask is correct or that any biological "
    "quantity follows from it.",
]


class ReviewError(ValueError):
    """An event or ledger that breaks the review rules."""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def event_hash(event: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in event.items() if k != "event_sha256"})).hexdigest()


def _text(value, label, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ReviewError(f"{label} must be a non-empty string of at most {maximum} characters")
    if any(ord(ch) < 32 for ch in value):
        raise ReviewError(f"{label} must not contain control characters")
    return value.strip()


def _pattern(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ReviewError(f"{label} is not in the expected form")
    return value


def read_events(path) -> list[dict]:
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode):
        raise ReviewError("the ledger must be a regular file")
    if info.st_size > MAX_LEDGER_BYTES:
        raise ReviewError("the ledger is larger than the limit")
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ReviewError(f"line {number} is blank")
        try:
            events.append(json.loads(line, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c))))
        except ValueError as exc:
            raise ReviewError(f"line {number} is not strict JSON") from exc
    return events


def chain_problems(events: list[dict]) -> list[str]:
    problems, previous = [], GENESIS_PREVIOUS
    for position, event in enumerate(events):
        if not isinstance(event, dict):
            problems.append(f"event {position + 1} is not an object")
            break
        if event.get("seq") != position + 1:
            problems.append(f"event {position + 1}: sequence number is {event.get('seq')!r}")
        if event.get("prev_sha256") != previous:
            problems.append(f"event {position + 1}: previous-hash link is broken")
        if event.get("event_sha256") != event_hash(event):
            problems.append(f"event {position + 1}: its hash does not match its content")
        previous = event.get("event_sha256") if isinstance(event.get("event_sha256"), str) else previous
    if events and events[0].get("event") != "open":
        problems.append("the first event must be open")
    return problems


def task_history(events: list[dict], task_id: str) -> dict:
    """Derive one task's head revision and state from the events (assumes a valid chain)."""
    submits = [e for e in events if e.get("event") == "submit" and e.get("task_id") == task_id]
    if not submits:
        return {"head": None, "state": None, "reviews": [], "adjudication": None, "revisions": 0}
    head = submits[-1]
    revision = head["revision"]
    reviews = [e for e in events if e.get("event") == "review" and e.get("task_id") == task_id
               and e.get("revision") == revision]
    adjudication = next((e for e in events if e.get("event") == "adjudicate" and e.get("task_id") == task_id
                         and e.get("revision") == revision), None)
    required = events[0]["required_accepting_reviews"]
    accepts = sum(r["decision"] == "accepted" for r in reviews)
    rejects = sum(r["decision"] == "rejected" for r in reviews)
    if adjudication is not None:
        state = STATE_ADJ_ACCEPTED if adjudication["decision"] == "accepted" else STATE_ADJ_REJECTED
    elif accepts and rejects:
        state = STATE_DISAGREEMENT
    elif rejects:
        state = STATE_REJECTED
    elif accepts >= required:
        state = STATE_ACCEPTED
    elif accepts:
        state = STATE_IN_REVIEW
    else:
        state = STATE_SUBMITTED
    return {"head": head, "state": state, "reviews": reviews, "adjudication": adjudication,
            "revisions": len(submits)}


def task_ids(events: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for event in events:
        if event.get("event") == "submit":
            seen.setdefault(event["task_id"], None)
    return list(seen)


def _load_intact(path) -> list[dict]:
    """Read the ledger and refuse to build on one whose chain is broken."""
    events = read_events(path)
    problems = chain_problems(events)
    if problems:
        raise ReviewError("the existing ledger is not intact: " + problems[0])
    return events


def _append(path: Path, events: list[dict], body: dict) -> dict:
    problems = chain_problems(events)
    if problems:
        raise ReviewError("the existing ledger is not intact: " + problems[0])
    event = {"schema_version": SCHEMA_VERSION, "seq": len(events) + 1,
             "prev_sha256": events[-1]["event_sha256"] if events else GENESIS_PREVIOUS, **body}
    event["event_sha256"] = event_hash(event)
    line = json.dumps(event, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    return event


def _common(actor_id, session_id, recorded_utc, rationale):
    return {"actor_id": _pattern(actor_id, ACTOR, "actor_id"), "session_id": _pattern(session_id, ACTOR, "session_id"),
            "recorded_utc": _pattern(recorded_utc, UTC, "recorded_utc"), "rationale": _text(rationale, "rationale")}


def open_ledger(path, *, protocol_version, required_accepting_reviews, actor_id, recorded_utc, rationale) -> dict:
    path = Path(path)
    if path.exists():
        raise ReviewError("the ledger already exists; it is append-only and is not reopened")
    if isinstance(required_accepting_reviews, bool) or not isinstance(required_accepting_reviews, int) \
            or not 1 <= required_accepting_reviews <= 5:
        raise ReviewError("required_accepting_reviews must be an integer from 1 to 5")
    body = {"event": "open", "protocol_version": _text(protocol_version, "protocol_version", 200),
            "required_accepting_reviews": required_accepting_reviews, "limits": LIMITS,
            "actor_id": _pattern(actor_id, ACTOR, "actor_id"),
            "recorded_utc": _pattern(recorded_utc, UTC, "recorded_utc"), "rationale": _text(rationale, "rationale")}
    return _append(path, [], body)


def submit(path, *, task_id, revision, kind, mask_path, mask_sha256, source_image_sha256,
           actor_id, session_id, recorded_utc, rationale) -> dict:
    """Freeze one revision of a mask or disposition record."""
    path = Path(path)
    events = _load_intact(path)
    if not events:
        raise ReviewError("open the ledger first")
    _pattern(task_id, TASK_ID, "task_id")
    history = task_history(events, task_id)
    expected = history["revisions"] + 1
    if revision != expected:
        raise ReviewError(f"the next revision of {task_id} is {expected}")
    if kind not in KINDS:
        raise ReviewError(f"kind must be one of {list(KINDS)}")
    _pattern(mask_sha256, SHA256, "mask_sha256")
    _pattern(source_image_sha256, SHA256, "source_image_sha256")
    if history["head"] is not None:
        if mask_sha256 == history["head"]["mask_sha256"]:
            raise ReviewError("a new revision must change the mask; this hash equals the previous revision")
        if source_image_sha256 != history["head"]["source_image_sha256"]:
            raise ReviewError("a task's source image cannot change between revisions")
    relative = _text(mask_path, "mask_path", 400)
    if relative.startswith(("/", "\\")) or ".." in Path(relative).parts or "\\" in relative or re.match(r"[A-Za-z]:", relative):
        raise ReviewError("mask_path must be a relative path inside the review root")
    body = {"event": "submit", "task_id": task_id, "revision": revision, "kind": kind, "mask_path": relative,
            "mask_sha256": mask_sha256, "source_image_sha256": source_image_sha256,
            "protocol_version": events[0]["protocol_version"],
            **_common(actor_id, session_id, recorded_utc, rationale)}
    return _append(path, events, body)


def review(path, *, task_id, revision, decision, round_number, actor_id, session_id, recorded_utc, rationale) -> dict:
    path = Path(path)
    events = _load_intact(path)
    history = task_history(events, _pattern(task_id, TASK_ID, "task_id"))
    head = history["head"]
    if head is None:
        raise ReviewError("there is no submitted revision of this task")
    if revision != head["revision"]:
        raise ReviewError(f"only the latest revision ({head['revision']}) can be reviewed")
    if decision not in DECISIONS:
        raise ReviewError(f"decision must be one of {list(DECISIONS)}")
    if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
        raise ReviewError("round_number must be a positive integer")
    common = _common(actor_id, session_id, recorded_utc, rationale)
    if actor_id == head["actor_id"]:
        raise ReviewError("the annotator cannot review their own revision")
    if session_id == head["session_id"]:
        raise ReviewError("a review must happen in a different session from the annotation")
    if any(r["actor_id"] == actor_id for r in history["reviews"]):
        raise ReviewError("this reviewer has already reviewed this revision")
    if history["state"] in (STATE_ACCEPTED, STATE_REJECTED, STATE_ADJ_ACCEPTED, STATE_ADJ_REJECTED):
        raise ReviewError(f"this revision is already {history['state']}; edit it as a new revision")
    body = {"event": "review", "task_id": task_id, "revision": revision, "decision": decision,
            "round": round_number, **common}
    return _append(path, events, body)


def adjudicate(path, *, task_id, revision, decision, actor_id, session_id, recorded_utc, rationale) -> dict:
    path = Path(path)
    events = _load_intact(path)
    history = task_history(events, _pattern(task_id, TASK_ID, "task_id"))
    head = history["head"]
    if head is None or revision != head["revision"]:
        raise ReviewError("only the latest submitted revision can be adjudicated")
    if history["state"] != STATE_DISAGREEMENT:
        raise ReviewError(f"only a disagreement is adjudicated; this revision is {history['state']}")
    if decision not in DECISIONS:
        raise ReviewError(f"decision must be one of {list(DECISIONS)}")
    common = _common(actor_id, session_id, recorded_utc, rationale)
    involved = {head["actor_id"], *(r["actor_id"] for r in history["reviews"])}
    if actor_id in involved:
        raise ReviewError("the adjudicator must be neither the annotator nor a reviewer of this revision")
    body = {"event": "adjudicate", "task_id": task_id, "revision": revision, "decision": decision,
            "resolves_review_seqs": [r["seq"] for r in history["reviews"]], **common}
    return _append(path, events, body)


def _read_mask(root: Path, relative: str) -> bytes | None:
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MASK_BYTES:
        return None
    return current.read_bytes()


def summary(path) -> dict:
    events = read_events(path)
    problems = chain_problems(events)
    if problems:
        raise ReviewError("the ledger is not intact: " + problems[0])
    counts: dict[str, int] = {}
    for task in task_ids(events):
        state = task_history(events, task)["state"]
        counts[state] = counts.get(state, 0) + 1
    return {"tasks": len(task_ids(events)), "state_counts": dict(sorted(counts.items())),
            "ledger_head_sha256": events[-1]["event_sha256"] if events else None, "limits": LIMITS}


def export_accepted(path, mask_root) -> dict:
    """Accepted latest revisions whose files still match; everything else is listed as excluded."""
    events = read_events(path)
    problems = chain_problems(events)
    if problems:
        raise ReviewError("the ledger is not intact: " + problems[0])
    root = Path(mask_root)
    accepted, excluded = [], []
    for task in task_ids(events):
        history = task_history(events, task)
        head, state = history["head"], history["state"]
        row = {"task_id": task, "revision": head["revision"], "kind": head["kind"], "state": state,
               "mask_sha256": head["mask_sha256"], "mask_path": head["mask_path"],
               "source_image_sha256": head["source_image_sha256"], "annotator_id": head["actor_id"]}
        if state not in USABLE_STATES:
            excluded.append({**row, "reason": f"latest revision is {state}"})
            continue
        raw = _read_mask(root, head["mask_path"])
        if raw is None:
            excluded.append({**row, "reason": "the mask file is missing, linked or too large"})
        elif hashlib.sha256(raw).hexdigest() != head["mask_sha256"]:
            excluded.append({**row, "reason": "the mask file no longer matches its frozen hash"})
        else:
            accepted.append({**row, "reviewer_ids": [r["actor_id"] for r in history["reviews"]],
                             "adjudicator_id": history["adjudication"]["actor_id"] if history["adjudication"] else None})
    return {"schema_version": SCHEMA_VERSION, "ledger_head_sha256": events[-1]["event_sha256"] if events else None,
            "protocol_version": events[0]["protocol_version"] if events else None,
            "accepted": accepted, "excluded": excluded,
            "use": "accepted rows are the only ones a benchmark may treat as reference masks; excluded rows "
                   "are for audit and are never ground truth",
            "limits": LIMITS}
