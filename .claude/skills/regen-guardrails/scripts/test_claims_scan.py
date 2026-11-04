#!/usr/bin/env python3
"""Regression test for claims_scan.py. Builds a throwaway git repo (named like a real one so the
per-repo tripwires and required statements apply), commits a clean README, then adds deliberately bad
and deliberately benign text and checks what the scanner reports.

  python3 -B test_claims_scan.py        # run after editing any rule or pattern
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCAN = Path(__file__).with_name("claims_scan.py")
CLEAN_README = (
    "# Demo\n\nThis is a personal hobby and learning project, developed with substantial assistance from AI coding tools.\n\n"
    "`protocol_sha256` over the complete input dataclass.\n"
)
BAD = """\
Take 5 mg/kg rapamycin daily; you should take it with food.
This pipeline is clinical-grade and ready for patients.
Our model proves that the compound rejuvenates cells.
A novel, state-of-the-art breakthrough.
I built this entirely by myself to impress hiring managers.
password = "abcd1234abcd1234abcd1234abcd1234"
token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked
"""
BENIGN = """\
This tool does not provide dosing advice and is not a regimen.
The score does not
establish rejuvenation or biological age.
No GMP or IND claims are made here.
Pelage reports a Phase 3 trial (doi:10.1000/xyz123) of dosing schedules.
"""


def run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(SCAN), "--repo", str(repo), *args], capture_output=True, text=True)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


class ClaimsScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cache = Path.home() / ".cache" / "claims-scan-test"
        cache.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=cache)
        cls.repo = Path(cls.tmp.name) / "cell-protocol-compiler"     # name selects the per-repo tripwires/required list
        (cls.repo / "src/protocolcompiler").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(cls.repo)], check=True)
        (cls.repo / "README.md").write_text(CLEAN_README, encoding="utf-8")
        (cls.repo / "src/protocolcompiler/library.py").write_text("WINDOW = (2, 12)\n", encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "base")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_clean_tree_is_quiet(self):
        out = run(self.repo)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("0 HIGH, 0 MED, 0 LOW", out.stdout)

    def test_bad_text_is_caught_and_benign_text_is_not(self):
        readme = self.repo / "README.md"
        readme.write_text(CLEAN_README + BAD + BENIGN, encoding="utf-8")
        (self.repo / ".env").write_text("X=1\n", encoding="utf-8")
        (self.repo / "src/protocolcompiler/library.py").write_text("WINDOW = (1, 20)\n", encoding="utf-8")
        out = run(self.repo)
        text = out.stdout
        self.assertEqual(out.returncode, 1, text)
        for rid in ("person-directed", "clinical-release", "proof-language", "biology-leap", "novelty-hype",
                    "sole-author", "career-framing", "secret", "env-file", "tripwire"):
            self.assertIn(rid, text, f"{rid} not reported")
        self.assertNotIn("abcd1234abcd1234", text, "a secret value was printed")
        self.assertNotIn("ghp_abcdefghijkl", text, "a token was printed")
        # Benign lines: negated, wrapped-negated, and bibliographic text must stay hidden by default.
        self.assertNotIn("does not provide dosing advice", text)
        self.assertNotIn("establish rejuvenation or biological age", text)
        self.assertNotIn("No GMP or IND claims", text)
        self.assertNotIn("Phase 3 trial", text)
        # ...but --all reveals them, flagged as guarded.
        self.assertIn("guarded", run(self.repo, "--all").stdout)

    def test_disclosure_removal_and_missing_required_statement(self):
        (self.repo / "README.md").write_text("# Demo\n\nNothing here.\n", encoding="utf-8")
        out = run(self.repo)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("disclosure-removed", out.stdout)
        self.assertIn("MISSING", out.stdout)
        self.assertIn("protocol_sha256", out.stdout)

    def tearDown(self):
        git(self.repo, "checkout", "-q", "--", ".")
        subprocess.run(["git", "-C", str(self.repo), "clean", "-fdq"], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
