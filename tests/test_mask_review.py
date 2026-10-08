from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from organoidphenotyping import mask_review as mr  # noqa: E402
from organoidphenotyping.cli import main  # noqa: E402

T0 = "2026-10-08T10:00:00Z"
SRC = "a" * 64


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / mr.LEDGER_NAME
        (self.root / "masks").mkdir()
        mr.open_ledger(self.ledger, protocol_version="pilot-v1", required_accepting_reviews=2,
                       actor_id="lead", recorded_utc=T0, rationale="test ledger")

    def mask(self, name: str, content: bytes) -> tuple[str, str]:
        (self.root / "masks" / name).write_bytes(content)
        return f"masks/{name}", sha(content)

    def submit(self, task="t1", revision=1, content=b"mask-v1", actor="ann", session="s-ann"):
        path, digest = self.mask(f"{task}-r{revision}.png", content)
        return mr.submit(self.ledger, task_id=task, revision=revision, kind="mask", mask_path=path,
                         mask_sha256=digest, source_image_sha256=SRC, actor_id=actor, session_id=session,
                         recorded_utc=T0, rationale="first pass")

    def review(self, actor, decision="accepted", task="t1", revision=1, session=None):
        return mr.review(self.ledger, task_id=task, revision=revision, decision=decision, round_number=1,
                         actor_id=actor, session_id=session or f"s-{actor}", recorded_utc=T0, rationale="looked")

    def state(self, task="t1"):
        return mr.task_history(mr.read_events(self.ledger), task)["state"]


class LifecycleTests(Base):
    def test_submitted_then_in_review_then_accepted_at_the_required_count(self):
        self.submit()
        self.assertEqual(self.state(), mr.STATE_SUBMITTED)
        self.review("r1")
        self.assertEqual(self.state(), mr.STATE_IN_REVIEW)
        self.review("r2")
        self.assertEqual(self.state(), mr.STATE_ACCEPTED)

    def test_any_rejection_without_acceptance_is_rejected_and_a_mix_is_a_disagreement(self):
        self.submit()
        self.review("r1", "rejected")
        self.assertEqual(self.state(), mr.STATE_REJECTED)
        self.submit("t2")
        self.review("r1", "accepted", task="t2")
        self.review("r2", "rejected", task="t2")
        self.assertEqual(self.state("t2"), mr.STATE_DISAGREEMENT)

    def test_adjudication_resolves_only_a_disagreement_and_by_an_uninvolved_person(self):
        self.submit()
        self.review("r1")
        with self.assertRaisesRegex(mr.ReviewError, "only a disagreement"):
            mr.adjudicate(self.ledger, task_id="t1", revision=1, decision="accepted", actor_id="adj",
                          session_id="s-adj", recorded_utc=T0, rationale="x")
        self.review("r2", "rejected")
        for actor in ("ann", "r1", "r2"):
            with self.assertRaisesRegex(mr.ReviewError, "neither the annotator nor a reviewer"):
                mr.adjudicate(self.ledger, task_id="t1", revision=1, decision="accepted", actor_id=actor,
                              session_id="s-adj", recorded_utc=T0, rationale="x")
        mr.adjudicate(self.ledger, task_id="t1", revision=1, decision="accepted", actor_id="adj",
                      session_id="s-adj", recorded_utc=T0, rationale="boundary is clear on the full image")
        self.assertEqual(self.state(), mr.STATE_ADJ_ACCEPTED)

    def test_self_review_same_session_review_and_double_review_are_refused(self):
        self.submit()
        with self.assertRaisesRegex(mr.ReviewError, "annotator cannot review"):
            self.review("ann", session="s-other")
        with self.assertRaisesRegex(mr.ReviewError, "different session"):
            self.review("r1", session="s-ann")
        self.review("r1")
        with self.assertRaisesRegex(mr.ReviewError, "already reviewed"):
            self.review("r1", session="s-again")

    def test_a_decided_revision_takes_no_more_reviews(self):
        self.submit()
        self.review("r1")
        self.review("r2")
        with self.assertRaisesRegex(mr.ReviewError, "already accepted"):
            self.review("r3")

    def test_an_edit_is_a_new_revision_that_resets_the_task_and_keeps_history(self):
        self.submit()
        self.review("r1")
        self.review("r2")
        with self.assertRaisesRegex(mr.ReviewError, "must change the mask"):
            self.submit(revision=2, content=b"mask-v1")
        with self.assertRaisesRegex(mr.ReviewError, "next revision"):
            self.submit(revision=3, content=b"mask-v3")
        self.submit(revision=2, content=b"mask-v2")
        history = mr.task_history(mr.read_events(self.ledger), "t1")
        self.assertEqual((history["state"], history["head"]["revision"], history["revisions"]), (mr.STATE_SUBMITTED, 2, 2))
        with self.assertRaisesRegex(mr.ReviewError, "latest revision"):
            self.review("r3", revision=1)

    def test_the_source_image_cannot_change_between_revisions(self):
        self.submit()
        path, digest = self.mask("t1-r2.png", b"mask-v2")
        with self.assertRaisesRegex(mr.ReviewError, "source image"):
            mr.submit(self.ledger, task_id="t1", revision=2, kind="mask", mask_path=path, mask_sha256=digest,
                      source_image_sha256="b" * 64, actor_id="ann", session_id="s2", recorded_utc=T0, rationale="x")

    def test_bad_values_are_refused(self):
        for kwargs in ({"mask_path": "../escape.png"}, {"mask_path": "/abs.png"}, {"mask_path": "C:/x.png"},
                       {"mask_sha256": "xyz"}, {"kind": "model"}):
            base = dict(task_id="t9", revision=1, kind="mask", mask_path="masks/x.png", mask_sha256="c" * 64,
                        source_image_sha256=SRC, actor_id="ann", session_id="s", recorded_utc=T0, rationale="x")
            base.update(kwargs)
            with self.assertRaises(mr.ReviewError, msg=kwargs):
                mr.submit(self.ledger, **base)
        with self.assertRaises(mr.ReviewError):
            mr.open_ledger(self.root / "other.jsonl", protocol_version="p", required_accepting_reviews=0,
                           actor_id="a", recorded_utc=T0, rationale="x")
        with self.assertRaisesRegex(mr.ReviewError, "already exists"):
            mr.open_ledger(self.ledger, protocol_version="p", required_accepting_reviews=1, actor_id="a",
                           recorded_utc=T0, rationale="x")


class ChainAndExportTests(Base):
    def test_editing_or_removing_a_past_event_breaks_the_chain(self):
        self.submit()
        self.review("r1")
        lines = self.ledger.read_text().splitlines()
        self.assertEqual(mr.chain_problems(mr.read_events(self.ledger)), [])
        edited = json.loads(lines[1])
        edited["rationale"] = "rewritten"
        self.ledger.write_text("\n".join([lines[0], json.dumps(edited, sort_keys=True), lines[2]]) + "\n")
        self.assertTrue(mr.chain_problems(mr.read_events(self.ledger)))
        self.ledger.write_text("\n".join([lines[0], lines[2]]) + "\n")
        self.assertTrue(mr.chain_problems(mr.read_events(self.ledger)))
        with self.assertRaisesRegex(mr.ReviewError, "not intact"):
            self.review("r2")

    def test_export_returns_only_accepted_unchanged_masks_and_lists_the_rest(self):
        self.submit("ok", content=b"ok-mask")
        self.review("r1", task="ok")
        self.review("r2", task="ok")
        self.submit("pending", content=b"pending-mask")
        self.submit("bad", content=b"bad-mask")
        self.review("r1", "rejected", task="bad")
        self.submit("changed", content=b"changed-mask")
        self.review("r1", task="changed")
        self.review("r2", task="changed")
        (self.root / "masks" / "changed-r1.png").write_bytes(b"edited after acceptance")
        self.submit("gone", content=b"gone-mask")
        self.review("r1", task="gone")
        self.review("r2", task="gone")
        (self.root / "masks" / "gone-r1.png").unlink()
        exported = mr.export_accepted(self.ledger, self.root)
        self.assertEqual([row["task_id"] for row in exported["accepted"]], ["ok"])
        reasons = {row["task_id"]: row["reason"] for row in exported["excluded"]}
        self.assertEqual(set(reasons), {"pending", "bad", "changed", "gone"})
        self.assertIn("submitted", reasons["pending"])
        self.assertIn("rejected", reasons["bad"])
        self.assertIn("no longer matches", reasons["changed"])
        self.assertIn("missing", reasons["gone"])
        self.assertTrue(any("independent" in limit for limit in exported["limits"]))
        self.assertEqual(exported["accepted"][0]["reviewer_ids"], ["r1", "r2"])

    def test_a_symlinked_mask_is_not_exported(self):
        self.submit("link", content=b"real")
        self.review("r1", task="link")
        self.review("r2", task="link")
        target = self.root / "masks" / "link-r1.png"
        copy = self.root / "elsewhere.png"
        copy.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(copy)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        exported = mr.export_accepted(self.ledger, self.root)
        self.assertEqual(exported["accepted"], [])
        self.assertIn("linked", exported["excluded"][0]["reason"])

    def test_summary_counts_states(self):
        self.submit("a")
        self.submit("b", content=b"b")
        self.review("r1", task="b")
        report = mr.summary(self.ledger)
        self.assertEqual(report["tasks"], 2)
        self.assertEqual(report["state_counts"], {"in_review": 1, "submitted": 1})


class CliTests(Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["review-ledger", *argv])
        return code, json.loads(out.getvalue())

    def test_submit_review_summary_and_export_through_the_cli(self):
        path, digest = self.mask("cli-r1.png", b"cli-mask")
        common = ["--ledger", str(self.ledger), "--recorded-utc", T0]
        code, event = self.run_cli("submit", *common, "--task-id", "c1", "--revision", "1", "--kind", "mask",
                                   "--mask-path", path, "--mask-sha256", digest, "--source-image-sha256", SRC,
                                   "--actor-id", "ann", "--session-id", "s-ann", "--rationale", "pass one")
        self.assertEqual((code, event["event"]), (0, "submit"))
        for reviewer in ("r1", "r2"):
            self.run_cli("review", *common, "--task-id", "c1", "--revision", "1", "--decision", "accepted",
                         "--actor-id", reviewer, "--session-id", f"s-{reviewer}", "--rationale", "ok")
        _, summary = self.run_cli("summary", "--ledger", str(self.ledger))
        self.assertEqual(summary["state_counts"], {"accepted": 1})
        _, exported = self.run_cli("export-accepted", "--ledger", str(self.ledger), "--mask-root", str(self.root))
        self.assertEqual([row["task_id"] for row in exported["accepted"]], ["c1"])

    def test_a_rule_violation_exits_with_an_error_and_writes_nothing(self):
        path, digest = self.mask("cli-r1.png", b"cli-mask")
        before = self.ledger.read_bytes()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("review", "--ledger", str(self.ledger), "--recorded-utc", T0, "--task-id", "none",
                         "--revision", "1", "--decision", "accepted", "--actor-id", "r1", "--session-id", "s",
                         "--rationale", "x")
        self.assertEqual(self.ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
