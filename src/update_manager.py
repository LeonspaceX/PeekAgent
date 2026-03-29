"""Update checking helpers for GitHub releases."""

from __future__ import annotations

from dataclasses import dataclass

import requests


GITHUB_RELEASE_LATEST_API = "https://api.github.com/repos/LeonspaceX/PeekAgent/releases/latest"
RELEASE_ASSET_NAME = "PeekAgent-windows-amd64.zip"


@dataclass(slots=True)
class ReleaseInfo:
    version: str
    asset_name: str
    download_url: str
    sha256: str


def strip_version_prefix(value: str) -> str:
    version = (value or "").strip()
    if version.lower().startswith("v"):
        version = version[1:]
    return version


def parse_version_parts(value: str) -> tuple[int, ...]:
    version = strip_version_prefix(value)
    if not version:
        return tuple()

    parts: list[int] = []
    for item in version.split("."):
        item = item.strip()
        if not item:
            parts.append(0)
            continue
        if not item.isdigit():
            raise ValueError(f"无效版本号：{value}")
        parts.append(int(item))
    return tuple(parts)


def compare_versions(left: str, right: str) -> int:
    left_parts = list(parse_version_parts(left))
    right_parts = list(parse_version_parts(right))
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def build_release_download_url(version: str) -> str:
    clean_version = strip_version_prefix(version)
    return (
        f"https://github.com/LeonspaceX/PeekAgent/releases/download/"
        f"v{clean_version}/{RELEASE_ASSET_NAME}"
    )


def build_mirrored_url(direct_url: str, mirror_prefix: str) -> str:
    prefix = (mirror_prefix or "").strip()
    return f"{prefix}{direct_url}" if prefix else direct_url


def extract_sha256_digest(value: str) -> str:
    digest = (value or "").strip()
    if not digest:
        raise ValueError("GitHub API 未返回 digest 字段")
    if ":" in digest:
        algo, digest = digest.split(":", 1)
        if algo.strip().lower() != "sha256":
            raise ValueError(f"不支持的 digest 算法：{algo}")
    digest = digest.strip().lower()
    if len(digest) != 64:
        raise ValueError("GitHub API 返回的 sha256 长度不正确")
    return digest


def parse_latest_release_info(payload: dict) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise ValueError("GitHub API 返回格式不正确")

    version = strip_version_prefix(payload.get("tag_name", ""))
    if not version:
        raise ValueError("GitHub API 未返回有效 tag_name")

    assets = payload.get("assets") or []
    asset = next((item for item in assets if item.get("name") == RELEASE_ASSET_NAME), None)
    if asset is None:
        raise ValueError(f"Release 中未找到 {RELEASE_ASSET_NAME}")

    return ReleaseInfo(
        version=version,
        asset_name=RELEASE_ASSET_NAME,
        download_url=build_release_download_url(version),
        sha256=extract_sha256_digest(asset.get("digest", "")),
    )


def fetch_latest_release_info(timeout: tuple[int, int] = (10, 30)) -> ReleaseInfo:
    response = requests.get(
        GITHUB_RELEASE_LATEST_API,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PeekAgent-Updater",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_latest_release_info(response.json())
