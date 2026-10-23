#!/usr/bin/env python3
"""Install a skill from a GitHub repo path into $CODEX_HOME/skills."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ntpath
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import zipfile

from github_utils import GithubResponseTooLarge, github_download
DEFAULT_REF = "main"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_PATH_LENGTH = 4096
MAX_ARCHIVE_PATH_DEPTH = 64
MAX_ARCHIVE_COMPONENT_LENGTH = 255
MAX_ARCHIVE_DIRECTORIES = 20_000
_EXTRACTION_CHUNK_BYTES = 64 * 1024
_ZIP_EOCD = struct.Struct("<4s4H2LH")
_ZIP_CENTRAL_DIRECTORY_ENTRY = struct.Struct("<4s6H3I5H2I")


@dataclass
class Args:
    url: str | None = None
    repo: str | None = None
    path: list[str] | None = None
    ref: str = DEFAULT_REF
    dest: str | None = None
    name: str | None = None
    method: str = "auto"


@dataclass
class Source:
    owner: str
    repo: str
    ref: str
    paths: list[str]
    repo_url: str | None = None


class InstallError(Exception):
    pass


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))


def _tmp_root() -> str:
    base = os.path.join(tempfile.gettempdir(), "codex")
    os.makedirs(base, exist_ok=True)
    return base


def _parse_github_url(url: str, default_ref: str) -> tuple[str, str, str, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "github.com":
        raise InstallError("Only GitHub URLs are supported for download mode.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise InstallError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1]
    ref = default_ref
    subpath = ""
    if len(parts) > 2:
        if parts[2] in ("tree", "blob"):
            if len(parts) < 4:
                raise InstallError("GitHub URL missing ref or path.")
            ref = parts[3]
            subpath = "/".join(parts[4:])
        else:
            subpath = "/".join(parts[2:])
    return owner, repo, ref, subpath or None


def _download_repo_zip(owner: str, repo: str, ref: str, dest_dir: str) -> str:
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    zip_path = os.path.join(dest_dir, "repo.zip")
    try:
        github_download(
            zip_url,
            "codex-skill-install",
            zip_path,
            MAX_ARCHIVE_BYTES,
        )
    except urllib.error.HTTPError as exc:
        raise InstallError(f"Download failed: HTTP {exc.code}") from exc
    except GithubResponseTooLarge as exc:
        raise InstallError("Downloaded archive exceeds the 128 MiB limit.") from exc
    _preflight_zip_resources(zip_path)
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            _safe_extract_zip(zip_file, dest_dir)
            top_levels = {
                info.filename.replace("\\", "/").split("/")[0]
                for info in zip_file.infolist()
                if info.filename
            }
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise InstallError("Downloaded archive could not be extracted.") from exc
    if not top_levels:
        raise InstallError("Downloaded archive was empty.")
    if len(top_levels) != 1:
        raise InstallError("Unexpected archive layout.")
    return os.path.join(dest_dir, next(iter(top_levels)))


def _preflight_zip_resources(zip_path: str) -> None:
    archive_size = os.path.getsize(zip_path)
    if archive_size > MAX_ARCHIVE_BYTES:
        raise InstallError("Downloaded archive exceeds the 128 MiB limit.")

    tail_size = min(archive_size, 22 + 0xFFFF)
    with open(zip_path, "rb") as archive:
        archive.seek(archive_size - tail_size)
        tail = archive.read(tail_size)
        eocd_offset = tail.rfind(b"PK\x05\x06")
        if eocd_offset < 0:
            raise InstallError("Downloaded archive is invalid.")
        eocd = tail[eocd_offset : eocd_offset + _ZIP_EOCD.size]
        if len(eocd) != _ZIP_EOCD.size:
            raise InstallError("Downloaded archive is invalid.")
        (
            signature,
            disk_number,
            directory_disk,
            entries_on_disk,
            entry_count,
            directory_size,
            directory_offset,
            comment_size,
        ) = _ZIP_EOCD.unpack(eocd)
        if (
            signature != b"PK\x05\x06"
            or eocd_offset + _ZIP_EOCD.size + comment_size != len(tail)
        ):
            raise InstallError("Downloaded archive is invalid.")
        if disk_number != 0 or directory_disk != 0 or entries_on_disk != entry_count:
            raise InstallError("Multi-disk archives are not supported.")
        if entry_count > MAX_ARCHIVE_MEMBERS:
            raise InstallError("Downloaded archive contains too many entries.")
        if directory_size > MAX_CENTRAL_DIRECTORY_BYTES:
            raise InstallError("Downloaded archive has an oversized directory.")
        if directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
            raise InstallError("ZIP64 archives are not supported by download mode.")

        eocd_position = archive_size - tail_size + eocd_offset
        directory_prefix = eocd_position - directory_size - directory_offset
        if directory_prefix < 0:
            raise InstallError("Downloaded archive is invalid.")
        directory_start = directory_offset + directory_prefix
        if directory_start + directory_size > eocd_position:
            raise InstallError("Downloaded archive is invalid.")

        archive.seek(directory_start)
        remaining = directory_size
        actual_entries = 0
        declared_extracted_bytes = 0
        while remaining:
            if remaining < _ZIP_CENTRAL_DIRECTORY_ENTRY.size:
                raise InstallError("Downloaded archive is invalid.")
            header = archive.read(_ZIP_CENTRAL_DIRECTORY_ENTRY.size)
            if len(header) != _ZIP_CENTRAL_DIRECTORY_ENTRY.size:
                raise InstallError("Downloaded archive is invalid.")
            fields = _ZIP_CENTRAL_DIRECTORY_ENTRY.unpack(header)
            if fields[0] != b"PK\x01\x02":
                raise InstallError("Downloaded archive is invalid.")
            compression_method = fields[4]
            if compression_method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise InstallError(
                    "Archive uses an unsupported compression method."
                )
            compressed_size = fields[8]
            extracted_size = fields[9]
            filename_size, extra_size, member_comment_size = fields[10:13]
            disk_start = fields[13]
            local_header_offset = fields[16]
            if disk_start != 0:
                raise InstallError("Multi-disk archives are not supported.")
            if (
                compressed_size == 0xFFFFFFFF
                or extracted_size == 0xFFFFFFFF
                or local_header_offset == 0xFFFFFFFF
            ):
                raise InstallError(
                    "ZIP64 archives are not supported by download mode."
                )
            record_size = (
                _ZIP_CENTRAL_DIRECTORY_ENTRY.size
                + filename_size
                + extra_size
                + member_comment_size
            )
            if record_size > remaining:
                raise InstallError("Downloaded archive is invalid.")
            actual_entries += 1
            if actual_entries > MAX_ARCHIVE_MEMBERS:
                raise InstallError("Downloaded archive contains too many entries.")
            declared_extracted_bytes += extracted_size
            if declared_extracted_bytes > MAX_EXTRACTED_BYTES:
                raise InstallError("Downloaded archive exceeds the 256 MiB limit.")
            archive.seek(filename_size + extra_size + member_comment_size, os.SEEK_CUR)
            remaining -= record_size

        if actual_entries != entry_count:
            raise InstallError("Downloaded archive is invalid.")


def _run_git(args: list[str]) -> None:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise InstallError(result.stderr.strip() or "Git command failed.")


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: str) -> None:
    dest_root = os.path.realpath(dest_dir)
    infos = zip_file.infolist()
    targets = []
    directories = set()
    for info in infos:
        try:
            parts = _archive_path_parts(info.filename)
            directory_parts = parts if info.is_dir() else parts[:-1]
            for depth in range(1, len(directory_parts) + 1):
                directory = os.path.normcase(os.path.join(*directory_parts[:depth]))
                directories.add(directory)
                if len(directories) > MAX_ARCHIVE_DIRECTORIES:
                    raise InstallError("Archive creates too many directories.")
            target = os.path.join(dest_dir, info.filename)
            extracted_path = os.path.realpath(target)
        except InstallError:
            raise
        except (OSError, ValueError) as exc:
            raise InstallError("Archive contains an invalid path.") from exc
        if extracted_path == dest_root or extracted_path.startswith(dest_root + os.sep):
            targets.append(target)
        else:
            raise InstallError("Archive contains files outside the destination.")

    extracted_bytes = 0
    for info, target in zip(infos, targets):
        try:
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zip_file.open(info, "r") as source, open(target, "wb") as output:
                while True:
                    chunk = source.read(
                        min(
                            _EXTRACTION_CHUNK_BYTES,
                            MAX_EXTRACTED_BYTES - extracted_bytes + 1,
                        )
                    )
                    if not chunk:
                        break
                    extracted_bytes += len(chunk)
                    if extracted_bytes > MAX_EXTRACTED_BYTES:
                        raise InstallError(
                            "Downloaded archive exceeds the 256 MiB limit."
                        )
                    output.write(chunk)
        except InstallError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            raise InstallError("Downloaded archive could not be extracted.") from exc


def _archive_path_parts(filename: str) -> list[str]:
    normalized = filename.replace("\\", "/")
    if not normalized or "\0" in normalized:
        raise InstallError("Archive contains an invalid path.")
    if len(normalized) > MAX_ARCHIVE_PATH_LENGTH:
        raise InstallError("Archive path exceeds the length limit.")
    if normalized.startswith("/") or ntpath.splitdrive(normalized)[0]:
        raise InstallError("Archive contains files outside the destination.")
    raw_parts = normalized.split("/")
    if ".." in raw_parts:
        raise InstallError("Archive contains files outside the destination.")
    parts = [part for part in raw_parts if part not in ("", ".")]
    if not parts:
        raise InstallError("Archive contains an invalid path.")
    if len(parts) > MAX_ARCHIVE_PATH_DEPTH:
        raise InstallError("Archive path exceeds the depth limit.")
    if any(len(part) > MAX_ARCHIVE_COMPONENT_LENGTH for part in parts):
        raise InstallError("Archive path component exceeds the length limit.")
    return parts


def _validate_relative_path(path: str) -> None:
    if os.path.isabs(path) or os.path.normpath(path).startswith(".."):
        raise InstallError("Skill path must be a relative path inside the repo.")


def _validate_skill_name(name: str) -> None:
    altsep = os.path.altsep
    if not name or os.path.sep in name or (altsep and altsep in name):
        raise InstallError("Skill name must be a single path segment.")
    if name in (".", ".."):
        raise InstallError("Invalid skill name.")


def _git_sparse_checkout(repo_url: str, ref: str, paths: list[str], dest_dir: str) -> str:
    repo_dir = os.path.join(dest_dir, "repo")
    clone_cmd = [
        "git",
        "clone",
        "--filter=blob:none",
        "--depth",
        "1",
        "--sparse",
        "--single-branch",
        "--branch",
        ref,
        repo_url,
        repo_dir,
    ]
    try:
        _run_git(clone_cmd)
    except InstallError:
        _run_git(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                "--single-branch",
                repo_url,
                repo_dir,
            ]
        )
    _run_git(["git", "-C", repo_dir, "sparse-checkout", "set", *paths])
    _run_git(["git", "-C", repo_dir, "checkout", ref])
    return repo_dir


def _validate_skill(path: str) -> None:
    if not os.path.isdir(path):
        raise InstallError(f"Skill path not found: {path}")
    skill_md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill_md):
        raise InstallError("SKILL.md not found in selected skill directory.")


def _copy_skill(src: str, dest_dir: str) -> None:
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    if os.path.exists(dest_dir):
        raise InstallError(f"Destination already exists: {dest_dir}")
    shutil.copytree(src, dest_dir)


def _build_repo_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def _build_repo_ssh(owner: str, repo: str) -> str:
    return f"git@github.com:{owner}/{repo}.git"


def _prepare_repo(source: Source, method: str, tmp_dir: str) -> str:
    if method in ("download", "auto"):
        try:
            return _download_repo_zip(source.owner, source.repo, source.ref, tmp_dir)
        except InstallError as exc:
            if method == "download":
                raise
            err_msg = str(exc)
            if "HTTP 401" in err_msg or "HTTP 403" in err_msg or "HTTP 404" in err_msg:
                pass
            else:
                raise
    if method in ("git", "auto"):
        repo_url = source.repo_url or _build_repo_url(source.owner, source.repo)
        try:
            return _git_sparse_checkout(repo_url, source.ref, source.paths, tmp_dir)
        except InstallError:
            repo_url = _build_repo_ssh(source.owner, source.repo)
            return _git_sparse_checkout(repo_url, source.ref, source.paths, tmp_dir)
    raise InstallError("Unsupported method.")


def _resolve_source(args: Args) -> Source:
    if args.url:
        owner, repo, ref, url_path = _parse_github_url(args.url, args.ref)
        if args.path is not None:
            paths = list(args.path)
        elif url_path:
            paths = [url_path]
        else:
            paths = []
        if not paths:
            raise InstallError("Missing --path for GitHub URL.")
        return Source(owner=owner, repo=repo, ref=ref, paths=paths)

    if not args.repo:
        raise InstallError("Provide --repo or --url.")
    if "://" in args.repo:
        return _resolve_source(
            Args(url=args.repo, repo=None, path=args.path, ref=args.ref)
        )

    repo_parts = [p for p in args.repo.split("/") if p]
    if len(repo_parts) != 2:
        raise InstallError("--repo must be in owner/repo format.")
    if not args.path:
        raise InstallError("Missing --path for --repo.")
    paths = list(args.path)
    return Source(
        owner=repo_parts[0],
        repo=repo_parts[1],
        ref=args.ref,
        paths=paths,
    )


def _default_dest() -> str:
    return os.path.join(_codex_home(), "skills")


def _parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description="Install a skill from GitHub.")
    parser.add_argument("--repo", help="owner/repo")
    parser.add_argument("--url", help="https://github.com/owner/repo[/tree/ref/path]")
    parser.add_argument(
        "--path",
        nargs="+",
        help="Path(s) to skill(s) inside repo",
    )
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--dest", help="Destination skills directory")
    parser.add_argument(
        "--name", help="Destination skill name (defaults to basename of path)"
    )
    parser.add_argument(
        "--method",
        choices=["auto", "download", "git"],
        default="auto",
    )
    return parser.parse_args(argv, namespace=Args())


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        source = _resolve_source(args)
        source.ref = source.ref or args.ref
        if not source.paths:
            raise InstallError("No skill paths provided.")
        for path in source.paths:
            _validate_relative_path(path)
        dest_root = args.dest or _default_dest()
        tmp_dir = tempfile.mkdtemp(prefix="skill-install-", dir=_tmp_root())
        try:
            repo_root = _prepare_repo(source, args.method, tmp_dir)
            installed = []
            for path in source.paths:
                skill_name = args.name if len(source.paths) == 1 else None
                skill_name = skill_name or os.path.basename(path.rstrip("/"))
                _validate_skill_name(skill_name)
                if not skill_name:
                    raise InstallError("Unable to derive skill name.")
                dest_dir = os.path.join(dest_root, skill_name)
                if os.path.exists(dest_dir):
                    raise InstallError(f"Destination already exists: {dest_dir}")
                skill_src = os.path.join(repo_root, path)
                _validate_skill(skill_src)
                _copy_skill(skill_src, dest_dir)
                installed.append((skill_name, dest_dir))
        finally:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        for skill_name, dest_dir in installed:
            print(f"Installed {skill_name} to {dest_dir}")
        return 0
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
