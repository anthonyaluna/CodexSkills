from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SCRIPTS = REPO_ROOT / "skills/.system/skill-installer/scripts"
GITHUB_UTILS_PATH = INSTALLER_SCRIPTS / "github_utils.py"
GITHUB_UTILS_SPEC = importlib.util.spec_from_file_location(
    "github_utils", GITHUB_UTILS_PATH
)
github_utils = importlib.util.module_from_spec(GITHUB_UTILS_SPEC)
sys.modules[GITHUB_UTILS_SPEC.name] = github_utils
GITHUB_UTILS_SPEC.loader.exec_module(github_utils)

INSTALLER_PATH = INSTALLER_SCRIPTS / "install-skill-from-github.py"
SPEC = importlib.util.spec_from_file_location("install_skill_from_github", INSTALLER_PATH)
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

LIST_PATH = INSTALLER_SCRIPTS / "list-skills.py"
LIST_SPEC = importlib.util.spec_from_file_location("list_skills", LIST_PATH)
list_skills = importlib.util.module_from_spec(LIST_SPEC)
sys.modules[LIST_SPEC.name] = list_skills
LIST_SPEC.loader.exec_module(list_skills)


class FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None):
        self.payload = io.BytesIO(payload)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload.read(size)


def write_zip(
    path: Path,
    members: list[tuple[str, bytes]],
    compression: int = zipfile.ZIP_DEFLATED,
) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in members:
            archive.writestr(name, content)


class InstallerArchiveLimitTests(unittest.TestCase):
    def test_github_request_rejects_oversized_response(self):
        with (
            mock.patch.object(
                github_utils.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"four"),
            ),
        ):
            with self.assertRaises(github_utils.GithubResponseTooLarge):
                github_utils.github_request(
                    "https://example.test", "test", max_bytes=3
                )

    def test_list_skills_preserves_cli_error_and_valid_listing_behavior(self):
        with mock.patch.object(
            list_skills,
            "_request",
            side_effect=github_utils.GithubResponseTooLarge("over limit"),
        ):
            with self.assertRaisesRegex(
                list_skills.ListError, "response exceeds size limit"
            ):
                list_skills._list_skills("owner/repo", "skills", "main")

        with mock.patch.object(
            list_skills,
            "_request",
            return_value=b'[{"name":"example","type":"dir"}]',
        ):
            self.assertEqual(
                list_skills._list_skills("owner/repo", "skills", "main"),
                ["example"],
            )

    def test_streamed_download_removes_partial_file_at_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "repo.zip"
            with mock.patch.object(
                github_utils.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"four"),
            ):
                with self.assertRaises(github_utils.GithubResponseTooLarge):
                    github_utils.github_download(
                        "https://example.test", "test", str(destination), 3
                    )
            self.assertFalse(destination.exists())

    def test_preflight_accepts_small_valid_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "valid.zip"
            write_zip(archive, [("repo/skills/example/SKILL.md", b"ok")])
            installer._preflight_zip_resources(str(archive))

    def test_preflight_counts_entries_before_zipfile_opens_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "many.zip"
            write_zip(archive, [("repo/a", b"a"), ("repo/b", b"b")])

            def copy_archive(_url, _agent, destination, _limit):
                shutil.copyfile(archive, destination)

            with (
                mock.patch.object(installer, "MAX_ARCHIVE_MEMBERS", 1),
                mock.patch.object(installer, "github_download", side_effect=copy_archive),
                mock.patch.object(installer.zipfile, "ZipFile") as zip_file,
            ):
                with self.assertRaisesRegex(installer.InstallError, "too many entries"):
                    installer._download_repo_zip("owner", "repo", "main", temp_dir)
            zip_file.assert_not_called()

    def test_preflight_rejects_bzip2_and_lzma_before_zipfile_opens_archive(self):
        for compression in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(compression=compression), tempfile.TemporaryDirectory() as temp_dir:
                archive = Path(temp_dir) / "unsupported.zip"
                write_zip(archive, [("repo/file", b"data")], compression)

                def copy_archive(_url, _agent, destination, _limit):
                    shutil.copyfile(archive, destination)

                with (
                    mock.patch.object(
                        installer, "github_download", side_effect=copy_archive
                    ),
                    mock.patch.object(installer.zipfile, "ZipFile") as zip_file,
                ):
                    with self.assertRaisesRegex(
                        installer.InstallError, "unsupported compression method"
                    ):
                        installer._download_repo_zip(
                            "owner", "repo", "main", temp_dir
                        )
                zip_file.assert_not_called()

    def test_auto_does_not_fall_back_to_git_after_a_resource_limit(self):
        source = installer.Source("owner", "repo", "main", ["skills/example"])
        with (
            mock.patch.object(
                installer,
                "_download_repo_zip",
                side_effect=installer.InstallError(
                    "Downloaded archive exceeds the 128 MiB limit."
                ),
            ),
            mock.patch.object(installer, "_git_sparse_checkout") as git_checkout,
        ):
            with self.assertRaisesRegex(installer.InstallError, "128 MiB limit"):
                installer._prepare_repo(source, "auto", "unused")
        git_checkout.assert_not_called()

    def test_preflight_enforces_declared_expanded_size_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "large.zip"
            write_zip(archive, [("repo/large.bin", b"1234")])

            def copy_archive(_url, _agent, destination, _limit):
                shutil.copyfile(archive, destination)

            with (
                mock.patch.object(installer, "MAX_EXTRACTED_BYTES", 3),
                mock.patch.object(installer, "github_download", side_effect=copy_archive),
                mock.patch.object(installer.zipfile, "ZipFile") as zip_file,
            ):
                with self.assertRaisesRegex(installer.InstallError, "256 MiB limit"):
                    installer._download_repo_zip("owner", "repo", "main", temp_dir)
            zip_file.assert_not_called()

    def test_actual_extracted_bytes_are_bounded(self):
        class Info:
            filename = "repo/file.txt"
            file_size = 1

            @staticmethod
            def is_dir():
                return False

        class FakeZip:
            @staticmethod
            def infolist():
                return [Info()]

            @staticmethod
            def open(_info, _mode):
                return io.BytesIO(b"four")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(installer, "MAX_EXTRACTED_BYTES", 3):
                with self.assertRaisesRegex(installer.InstallError, "256 MiB limit"):
                    installer._safe_extract_zip(FakeZip(), temp_dir)

    def test_valid_archive_is_extracted_and_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "valid.zip"
            write_zip(archive, [("repo/skills/example/SKILL.md", b"skill")])

            def copy_archive(_url, _agent, destination, _limit):
                shutil.copyfile(archive, destination)

            with mock.patch.object(installer, "github_download", side_effect=copy_archive):
                repo_root = installer._download_repo_zip(
                    "owner", "repo", "main", temp_dir
                )
            self.assertEqual(
                Path(repo_root, "skills", "example", "SKILL.md").read_bytes(),
                b"skill",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "traversal.zip"
            install_dir = Path(temp_dir) / "install"
            install_dir.mkdir()
            outside = Path(temp_dir) / "outside.txt"
            write_zip(archive, [("../" + outside.name, b"outside")])

            def copy_archive(_url, _agent, destination, _limit):
                shutil.copyfile(archive, destination)

            with mock.patch.object(installer, "github_download", side_effect=copy_archive):
                with self.assertRaisesRegex(installer.InstallError, "outside the destination"):
                    installer._download_repo_zip(
                        "owner", "repo", "main", str(install_dir)
                    )
            self.assertFalse(outside.exists())

    def test_main_installs_a_valid_skill_from_a_bounded_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "valid.zip"
            temp_root = Path(temp_dir) / "tmp"
            destination = Path(temp_dir) / "installed-skills"
            temp_root.mkdir()
            write_zip(archive, [("repo/skills/example/SKILL.md", b"skill")])

            def copy_archive(_url, _agent, target, _limit):
                shutil.copyfile(archive, target)

            with (
                mock.patch.object(
                    installer, "github_download", side_effect=copy_archive
                ),
                mock.patch.object(installer, "_tmp_root", return_value=str(temp_root)),
                redirect_stdout(io.StringIO()),
            ):
                result = installer.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--path",
                        "skills/example",
                        "--dest",
                        str(destination),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(
                (destination / "example" / "SKILL.md").read_bytes(), b"skill"
            )

    def test_path_depth_and_directory_count_are_bounded_before_extraction(self):
        for members, limits, error in (
            ([("repo/a/b/file", b"")], {"MAX_ARCHIVE_PATH_DEPTH": 3}, "depth limit"),
            (
                [("repo/a/file", b""), ("repo/b/file", b"")],
                {"MAX_ARCHIVE_DIRECTORIES": 2},
                "too many directories",
            ),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp_dir:
                archive = Path(temp_dir) / "paths.zip"
                install_dir = Path(temp_dir) / "install"
                install_dir.mkdir()
                write_zip(archive, members)

                def copy_archive(_url, _agent, destination, _limit):
                    shutil.copyfile(archive, destination)

                patches = [
                    mock.patch.object(installer, name, value)
                    for name, value in limits.items()
                ]
                with ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            installer, "github_download", side_effect=copy_archive
                        )
                    )
                    for patch in patches:
                        stack.enter_context(patch)
                    with self.assertRaisesRegex(installer.InstallError, error):
                        installer._download_repo_zip(
                            "owner", "repo", "main", str(install_dir)
                        )
                self.assertFalse((install_dir / "repo").exists())


if __name__ == "__main__":
    unittest.main()
