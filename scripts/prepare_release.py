"""Prepare one patch release per merged PR or manual run, using Git and stdlib."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

MANIFEST = Path("custom_components/aruba_monitor/manifest.json")
PROJECT = Path("pyproject.toml")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def git(root: Path, *args: str) -> str:
    """Run Git without passing inputs through a shell."""
    return subprocess.run(["git", *args], cwd=root, check=True, text=True, capture_output=True).stdout.strip()


def version_tuple(value: str) -> tuple[int, int, int]:
    """Accept only stable, canonical major.minor.patch versions."""
    if not VERSION.fullmatch(value):
        raise ValueError(f"Invalid release version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def metadata_version(manifest_text: str, project_text: str) -> str:
    """Require matching versions before making any changes."""
    version = json.loads(manifest_text)["version"]
    version_tuple(version)
    if tomllib.loads(project_text)["project"]["version"] != version:
        raise ValueError("Manifest and development project versions differ")
    return version


def prepare(
    root: Path, pr: int | None = None, merge_sha: str | None = None, *, run_id: int | None = None
) -> dict[str, str]:
    """Commit and tag a bump, or recover the result of an already published run."""
    if run_id is not None:
        if run_id < 1 or pr is not None or merge_sha is not None:
            raise ValueError("A positive manual run ID without PR metadata is required")
        marker = f"Aruba-Release-Run: {run_id}"
        evidence = [marker]
    else:
        if (
            pr is None
            or pr < 1
            or merge_sha is None
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge_sha)
        ):
            raise ValueError("A positive PR number and full lowercase merge commit SHA are required")
        marker = f"Aruba-Release-PR: {pr}"
        evidence = [marker, f"Aruba-Release-Merge: {merge_sha}"]
    if git(root, "symbolic-ref", "--short", "HEAD") != "main":
        raise ValueError("Release preparation requires the main branch")
    if git(root, "status", "--porcelain"):
        raise ValueError("Release preparation requires a clean checkout")
    if merge_sha is not None:
        git(root, "merge-base", "--is-ancestor", merge_sha, "HEAD")

    manifest_text = (root / MANIFEST).read_text()
    project_text = (root / PROJECT).read_text()
    current = metadata_version(manifest_text, project_text)
    tags = {
        tag: version_tuple(tag[1:])
        for tag in git(root, "tag", "--list", "v*").splitlines()
        if VERSION.fullmatch(tag[1:])
    }
    matches = git(root, "log", "--first-parent", "--format=%H", f"--grep=^{marker}$")
    commits = matches.splitlines() if matches else []
    if len(commits) > 1:
        raise ValueError("More than one release commit records this release event")
    if commits:
        sha = commits[0]
        message = git(root, "show", "-s", "--format=%B", sha)
        if any(line not in message.splitlines() for line in evidence):
            raise ValueError("The recorded release belongs to a different merge commit")
        version = metadata_version(
            git(root, "show", f"{sha}:{MANIFEST.as_posix()}"),
            git(root, "show", f"{sha}:{PROJECT.as_posix()}"),
        )
        tag = f"v{version}"
        if tag not in tags or git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}") != sha:
            raise ValueError("The recorded release tag is missing or points to another commit")
        created = False
    else:
        major, minor, patch = max(version_tuple(current), *tags.values()) if tags else version_tuple(current)
        version = f"{major}.{minor}.{patch + 1}"
        tag = f"v{version}"
        manifest = json.loads(manifest_text)
        manifest["version"] = version
        updated_project, replacements = re.subn(
            rf'(?m)^version = "{re.escape(current)}"$', f'version = "{version}"', project_text
        )
        if replacements != 1:
            raise ValueError("Cannot identify the development project version line")
        (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
        (root / PROJECT).write_text(updated_project)
        git(root, "add", "--", MANIFEST.as_posix(), PROJECT.as_posix())
        git(
            root,
            "commit",
            "-m",
            f"Release {tag}\n\n" + "\n".join(evidence),
        )
        sha = git(root, "rev-parse", "HEAD")
        git(root, "tag", "-a", tag, "-m", f"Aruba Monitor {tag}", sha)
        created = True
    # Retrying an older event may repair its release page, but must never roll latest back.
    newest = max([version_tuple(current), version_tuple(version), *tags.values()])
    return {
        "version": version,
        "tag": tag,
        "sha": sha,
        "created": str(created).lower(),
        "publish_latest": str(version_tuple(version) == newest).lower(),
    }


def push(root: Path, release: dict[str, str], latest_before: str) -> None:
    """Publish branch and tags atomically; only the rolling tag may move."""
    args = [
        "push",
        "--atomic",
        "origin",
        "HEAD:refs/heads/main",
        f"{release['tag']}:refs/tags/{release['tag']}",
    ]
    if release["publish_latest"] == "true":
        args.extend(
            [
                f"{release['sha']}:refs/tags/latest",
                f"--force-with-lease=refs/tags/latest:{latest_before}",
            ]
        )
    subprocess.run(["git", *args], cwd=root, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr", type=int)
    source.add_argument("--run-id", type=int)
    parser.add_argument("--merge-sha")
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        release = prepare(Path.cwd(), arguments.pr, arguments.merge_sha, run_id=arguments.run_id)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Release preparation failed: {error}") from None
    arguments.output.write_text("".join(f"{key}={value}\n" for key, value in release.items()))
