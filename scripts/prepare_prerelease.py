"""Package PR Git blobs using trusted code and update one rolling prerelease per PR."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

COMPONENT = "custom_components/aruba_monitor"
MANIFEST = f"{COMPONENT}/manifest.json"
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
RUNTIME_SUFFIXES = {".py", ".json", ".js", ".css", ".png", ".svg"}
RELEASE_MARKER = re.compile(
    r"(?m)^<!-- aruba-monitor-preview pr=(\d+) run=(\d+) sequence=(\d+) head=([0-9a-f]{40}|[0-9a-f]{64}) -->$"
)


def git(root: Path, *args: str, data: bytes | None = None, env: dict | None = None) -> bytes:
    """Run Git without executing any PR code, shell expressions or hooks."""
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=root,
        input=data,
        env=env,
        check=True,
        capture_output=True,
    ).stdout


def text_git(root: Path, *args: str, **kwargs) -> str:
    return git(root, *args, **kwargs).decode().strip()


def validate_path(path: str, mode: str) -> None:
    """Allow regular runtime files only; reject secrets, data, links and submodules."""
    relative = PurePosixPath(path).relative_to(COMPONENT)
    if mode not in {"100644", "100755"}:
        raise ValueError("Preview integration contains a link or submodule")
    if (
        any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) for part in relative.parts)
        or relative.suffix.lower() not in RUNTIME_SUFFIXES
        or any(
            part.lower().startswith(("secret", "credential", "capture", "__pycache__"))
            or ".sqlite" in part.lower()
            for part in relative.parts
        )
    ):
        raise ValueError("Preview integration contains a disallowed runtime path")


def package(root: Path, head: str, pr: int, run_id: int, output: Path) -> dict[str, str]:
    """Build a deterministic ZIP and tag commit without checking out PR content."""
    if pr < 1 or run_id < 1 or not SHA.fullmatch(head):
        raise ValueError("Positive PR/run numbers and a full lowercase head SHA are required")
    base = text_git(root, "rev-parse", "HEAD")
    manifest = json.loads(git(root, "show", f"{base}:{MANIFEST}"))
    match = VERSION.fullmatch(manifest["version"])
    if not match:
        raise ValueError("Trusted main must have a stable integration version")
    major, minor, patch = (int(part) for part in match.groups())
    version = f"{major}.{minor}.{patch + 1}-dev.{pr}.{run_id}.g{head[:12]}"

    files = {}
    records = git(root, "ls-tree", "-rz", head, "--", COMPONENT).split(b"\0")
    for record in filter(None, records):
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        path = raw_path.decode()
        validate_path(path, mode)
        if kind != "blob":
            raise ValueError("Preview integration must contain only regular files")
        files[path] = git(root, "cat-file", "blob", blob)
    for required in (MANIFEST, f"{COMPONENT}/__init__.py"):
        if required not in files:
            raise ValueError("Preview integration is missing a required file")
    preview_manifest = json.loads(files[MANIFEST])
    if not isinstance(preview_manifest, dict) or preview_manifest.get("domain") != "aruba_monitor":
        raise ValueError("Preview manifest has an unexpected integration domain")
    preview_manifest["version"] = version
    files[MANIFEST] = (json.dumps(preview_manifest, indent=2) + "\n").encode()

    # Preserve trusted main outside the integration. GitHub's GITHUB_TOKEN cannot
    # publish refs containing PR workflow changes; this also makes tag and ZIP
    # integration contents agree exactly. HEAD/index/worktree remain untouched.
    with tempfile.TemporaryDirectory(prefix="aruba-preview-") as folder:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(folder) / "index")}
        git(root, "read-tree", base, env=env)
        paths = git(root, "ls-files", "-z", "--", COMPONENT, env=env)
        git(root, "update-index", "--force-remove", "-z", "--stdin", data=paths, env=env)
        entries = []
        for path, content in sorted(files.items()):
            blob = text_git(root, "hash-object", "-w", "--stdin", data=content)
            entries.append(f"100644 {blob}\t{path}\0".encode())
        git(root, "update-index", "-z", "--index-info", data=b"".join(entries), env=env)
        tree = text_git(root, "write-tree", env=env)
        timestamp = text_git(root, "show", "-s", "--format=%ct", head)
        author = {
            "GIT_AUTHOR_NAME": "github-actions[bot]",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "github-actions[bot]",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
        }
        commit = text_git(
            root,
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            f"Preview PR #{pr}\n\nAruba-Preview-Head: {head}\nAruba-Preview-Version: {version}",
            env={**env, **author},
        )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(str(PurePosixPath(path).relative_to("custom_components")))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return {"version": version, "sha": commit, "head": head, "tag": f"pr-{pr}"}


class GitHub:
    """Small authenticated API client that never emits response bodies or tokens."""

    def __init__(self, repository: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.repository = repository
        self.url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def api(self, method: str, path: str, payload: dict | None = None, *, missing=False):
        request = urllib.request.Request(
            f"{self.url}/repos/{self.repository}/{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f"GitHub {method} failed with HTTP {error.code}") from None

    def current(self, pr: int, expected: str = "") -> str | None:
        """Closed, retargeted or superseded PR events cannot publish a preview."""
        pull = self.api("GET", f"pulls/{pr}")
        head = pull["head"]["sha"]
        if not SHA.fullmatch(head):
            raise ValueError("GitHub returned an invalid PR head SHA")
        if (
            pull["state"] != "open"
            or pull["base"]["ref"] != "main"
            or pull["base"]["repo"]["full_name"].lower() != self.repository.lower()
            or (expected and head != expected)
        ):
            return None
        return head

    def find_release(self, tag: str) -> dict | None:
        """Find published previews or drafts left by an interrupted publication."""
        if release := self.api("GET", f"releases/tags/{tag}", missing=True):
            return release
        # The tag endpoint only returns published releases. Authenticated release
        # listings include drafts, which must pass the same ownership/run checks.
        page = 1
        while True:
            releases = self.api("GET", f"releases?per_page=100&page={page}")
            if release := next((item for item in releases if item["tag_name"] == tag), None):
                return release
            if len(releases) < 100:
                return None
            page += 1


def publish(
    root: Path,
    github: GitHub,
    pr: int,
    expected: str,
    run_id: int,
    output: Path,
    *,
    run_number: int,
) -> dict | None:
    """Replace a PR preview, hiding it while tag, asset and metadata are updated."""
    if pr < 1 or run_id < 1 or run_number < 1 or (expected and not SHA.fullmatch(expected)):
        raise ValueError("Invalid preview event metadata")
    head = github.current(pr, expected)
    if head is None:
        return None
    git(root, "fetch", "--no-tags", "origin", f"refs/pull/{pr}/head")
    if text_git(root, "rev-parse", "FETCH_HEAD") != head:
        return None
    preview = package(root, head, pr, run_id, output)
    if github.current(pr, head) is None:
        return None
    tag = preview["tag"]
    metadata = {
        "tag_name": tag,
        "name": f"PR #{pr} preview",
        "body": (
            f"Test build for PR #{pr}, source commit `{head}`.\n\n"
            f"Integration version: `{preview['version']}`.\n\n"
            "Download `aruba_monitor.zip`, extract its `aruba_monitor` directory into "
            "Home Assistant's `custom_components` directory, then restart Home Assistant.\n\n"
            "The preview tag contains only the PR integration snapshot on trusted main, "
            "with a preview manifest version. It is replaced when the PR is updated.\n\n"
            f"<!-- aruba-monitor-preview pr={pr} run={run_id} sequence={run_number} head={head} -->"
        ),
        "draft": True,
        "prerelease": True,
        "make_latest": "false",
    }
    release = github.find_release(tag)
    if release is not None:
        if not release["prerelease"] or release.get("immutable", False):
            raise ValueError("Rolling preview requires a mutable prerelease at its reserved PR tag")
        marker = RELEASE_MARKER.search(release.get("body") or "")
        if marker is None or int(marker[1]) != pr:
            raise ValueError("Existing release does not belong to this PR preview workflow")
        # The PR head may be unchanged after a newer manual run. Checking head
        # alone would let an older event rerun roll back the installed version.
        # GitHub guarantees run_number increases and remains fixed on reruns.
        if int(marker[3]) > run_number:
            return None
        if int(marker[3]) == run_number and int(marker[2]) != run_id:
            raise ValueError("Preview workflow run number belongs to a different run")
        # Record the in-progress run before changing its tag or ZIP so an older
        # rerun cannot replace a newer preview whose upload was interrupted.
        github.api("PATCH", f"releases/{release['id']}", metadata)
    ref = f"refs/tags/{tag}"
    previous = text_git(root, "ls-remote", "--refs", "origin", ref).split()
    git(
        root,
        "push",
        f"--force-with-lease={ref}:{previous[0] if previous else ''}",
        "origin",
        f"{preview['sha']}:{ref}",
    )
    if release is None:
        release = github.api("POST", "releases", metadata)
    # gh streams the ZIP without interpreting any file from the pull request.
    subprocess.run(
        ["gh", "release", "upload", tag, str(output), "--repo", github.repository, "--clobber"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    if github.current(pr, head) is None:
        # Keep the release hidden until the queued run supplies the new head.
        return None
    github.api("PATCH", f"releases/{release['id']}", {**metadata, "draft": False})
    return preview


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--expected-head", default="")
    parser.add_argument("--local-head", help="Package this full SHA locally without contacting GitHub")
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-number", type=int, help="Increasing workflow run number; required to publish")
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.local_head:
            preview = package(
                Path.cwd(), arguments.local_head, arguments.pr, arguments.run_id, arguments.output
            )
            print(f"Prepared local preview {preview['version']} from {preview['head']}.")
            raise SystemExit(0)
        repository = os.environ["GITHUB_REPOSITORY"]
        if arguments.run_number is None:
            raise ValueError("Publishing requires the workflow run number")
        preview = publish(
            Path.cwd(),
            GitHub(repository),
            arguments.pr,
            arguments.expected_head,
            arguments.run_id,
            arguments.output,
            run_number=arguments.run_number,
        )
    except OSError, ValueError, KeyError, subprocess.CalledProcessError, RuntimeError:
        # Git/API error output may contain repository-controlled text. Keep it out
        # of workflow logs along with tokens and potential controller captures.
        raise SystemExit(
            "PR preview failed; check event metadata, runtime paths and repository permissions."
        ) from None
    if preview:
        print(f"Published {preview['tag']} at {preview['head']} ({preview['version']}).")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a") as stream:
                stream.write(
                    f"[Download PR #{arguments.pr} preview](https://github.com/{repository}/releases/tag/{preview['tag']})\n"
                )
    else:
        print("Preview skipped because the PR was closed, retargeted or superseded.")
