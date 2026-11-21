#!/usr/bin/env python3
"""Shared GitHub helpers for skill install scripts."""

from __future__ import annotations

import os
import urllib.request

MAX_GITHUB_RESPONSE_BYTES = 16 * 1024 * 1024
GITHUB_REQUEST_TIMEOUT_SECONDS = 30
_RESPONSE_CHUNK_BYTES = 64 * 1024


class GithubResponseTooLarge(Exception):
    """Raised when a GitHub response exceeds its configured byte limit."""


def _request(url: str, user_agent: str) -> urllib.request.Request:
    headers = {"User-Agent": user_agent}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return urllib.request.Request(url, headers=headers)


def _content_length(response) -> int | None:
    try:
        length = int(response.headers.get("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _bounded_chunks(response, max_bytes: int):
    total = 0
    while True:
        chunk = response.read(min(_RESPONSE_CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise GithubResponseTooLarge(
                f"GitHub response exceeds the {max_bytes}-byte limit."
            )
        yield chunk


def github_request(
    url: str,
    user_agent: str,
    max_bytes: int = MAX_GITHUB_RESPONSE_BYTES,
) -> bytes:
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    req = _request(url, user_agent)
    with urllib.request.urlopen(
        req, timeout=GITHUB_REQUEST_TIMEOUT_SECONDS
    ) as response:
        length = _content_length(response)
        if length is not None and length > max_bytes:
            raise GithubResponseTooLarge(
                f"GitHub response exceeds the {max_bytes}-byte limit."
            )
        payload = bytearray()
        for chunk in _bounded_chunks(response, max_bytes):
            payload.extend(chunk)
        return bytes(payload)


def github_download(
    url: str,
    user_agent: str,
    destination: str,
    max_bytes: int,
) -> int:
    """Stream a GitHub response to a new file without exceeding max_bytes."""
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    req = _request(url, user_agent)
    created = False
    completed = False
    total = 0
    try:
        with urllib.request.urlopen(
            req, timeout=GITHUB_REQUEST_TIMEOUT_SECONDS
        ) as response:
            length = _content_length(response)
            if length is not None and length > max_bytes:
                raise GithubResponseTooLarge(
                    f"GitHub response exceeds the {max_bytes}-byte limit."
                )
            with open(destination, "xb") as output:
                created = True
                for chunk in _bounded_chunks(response, max_bytes):
                    output.write(chunk)
                    total += len(chunk)
        completed = True
        return total
    finally:
        if created and not completed:
            try:
                os.unlink(destination)
            except OSError:
                pass


def github_api_contents_url(repo: str, path: str, ref: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
