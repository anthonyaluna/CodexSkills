import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / ".system" / "skill-installer" / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "install-skill-from-github.py"
sys.path.insert(0, str(SCRIPTS_DIR))
SPEC = importlib.util.spec_from_file_location("skill_installer", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class SkillInstallerRefEncodingTest(unittest.TestCase):
    def _download_url_for_ref(self, ref: str) -> str:
        requested_urls = []

        def reject_request(url: str) -> bytes:
            requested_urls.append(url)
            raise urllib.error.HTTPError(url, 404, "not found", None, None)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(installer, "_request", side_effect=reject_request):
                with self.assertRaises(installer.InstallError):
                    installer._download_repo_zip("owner", "repo", ref, temp_dir)

        self.assertEqual(len(requested_urls), 1)
        return requested_urls[0]

    def test_download_ref_is_quoted_without_losing_branch_slashes(self) -> None:
        cases = {
            "main": "main",
            "feature/release": "feature/release",
            "release#candidate": "release%23candidate",
            "release?candidate": "release%3Fcandidate",
            "release%23candidate": "release%2523candidate",
        }

        for ref, encoded_ref in cases.items():
            with self.subTest(ref=ref):
                url = self._download_url_for_ref(ref)
                parsed = urllib.parse.urlsplit(url)
                self.assertEqual(
                    parsed.path,
                    f"/owner/repo/zip/{encoded_ref}",
                )
                self.assertEqual(parsed.query, "")
                self.assertEqual(parsed.fragment, "")

    def test_percent_encoded_url_ref_is_decoded_then_encoded_once(self) -> None:
        owner, repo, ref, path = installer._parse_github_url(
            "https://github.com/owner/repo/tree/release%23candidate/skills/demo",
            "main",
        )

        self.assertEqual(
            (owner, repo, ref, path),
            ("owner", "repo", "release#candidate", "skills/demo"),
        )
        self.assertEqual(
            self._download_url_for_ref(ref),
            "https://codeload.github.com/owner/repo/zip/release%23candidate",
        )

    def test_url_semicolon_ref_is_not_truncated_as_params(self) -> None:
        source = installer._resolve_source(
            installer.Args(
                url="https://github.com/owner/repo/tree/release;candidate",
                path=["skills/demo"],
            )
        )

        self.assertEqual(source.ref, "release;candidate")
        self.assertEqual(
            self._download_url_for_ref(source.ref),
            "https://codeload.github.com/owner/repo/zip/release%3Bcandidate",
        )

    def test_git_fallback_receives_the_original_ref(self) -> None:
        commands = []
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(installer, "_run_git", side_effect=commands.append):
                installer._git_sparse_checkout(
                    "https://github.com/owner/repo.git",
                    "release#candidate",
                    ["skills/demo"],
                    temp_dir,
                )

        clone_command = commands[0]
        self.assertEqual(
            clone_command[clone_command.index("--branch") + 1],
            "release#candidate",
        )
        self.assertEqual(commands[-1][-1], "release#candidate")


if __name__ == "__main__":
    unittest.main()
