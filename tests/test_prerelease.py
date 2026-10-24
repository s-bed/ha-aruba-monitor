"""Exercise PR packaging and publication using isolated Git repos and a fake API."""

import json
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from awesomeversion import AwesomeVersion

from scripts import prepare_prerelease as preview


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    preview.git(root, "init", "--initial-branch=main")
    preview.git(root, "config", "user.name", "Preview test")
    preview.git(root, "config", "user.email", "preview@example.invalid")
    (root / preview.MANIFEST).parent.mkdir(parents=True)
    (root / preview.MANIFEST).write_text(json.dumps({"domain": "aruba_monitor", "version": "0.2.1"}))
    (root / preview.COMPONENT / "__init__.py").write_text("# integration\n")
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github/workflows/release.yml").write_text("# trusted workflow\n")
    preview.git(root, "add", ".")
    preview.git(root, "commit", "-m", "Trusted main")
    target = root.parent / "remote.git"
    target.mkdir()
    preview.git(target, "init", "--bare", "--initial-branch=main")
    preview.git(root, "remote", "add", "origin", str(target))
    preview.git(root, "push", "origin", "main")
    return root


def head_change(root, *, path="frontend/panel.js", content="// preview\n"):
    """Create a PR commit, then return to trusted main."""
    preview.git(root, "checkout", "-B", "pr")
    path = root / preview.COMPONENT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    (root / ".github/workflows/release.yml").write_text("# malicious PR workflow is never used\n")
    (root / "unrelated.txt").write_text("Not runtime\n")
    preview.git(root, "add", ".")
    preview.git(root, "commit", "-m", "PR change")
    head = preview.text_git(root, "rev-parse", "HEAD")
    preview.git(root, "push", "--force", "origin", f"{head}:refs/pull/3/head")
    preview.git(root, "checkout", "main")
    return head


def test_packages_exact_pr_runtime_with_unique_ha_version_without_executing_it(repository, tmp_path):
    head = head_change(repository, path="__init__.py", content="raise RuntimeError('must never execute')\n")
    # Even a tracked executable is handled only as blob bytes. Untracked runtime
    # files are excluded without inspecting their potentially sensitive contents.
    (repository / preview.COMPONENT / "credentials.json").write_text("must not package")
    output = tmp_path / "aruba_monitor.zip"
    result = preview.package(repository, head, 3, 123, output)

    assert result["version"] == f"0.2.2-dev.3.123.g{head[:12]}"
    assert AwesomeVersion("0.2.1") < AwesomeVersion(result["version"]) < AwesomeVersion("0.2.2")
    assert AwesomeVersion(result["version"]).valid
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["aruba_monitor/__init__.py", "aruba_monitor/manifest.json"]
        assert archive.read("aruba_monitor/__init__.py").startswith(b"raise RuntimeError")
        assert json.loads(archive.read("aruba_monitor/manifest.json"))["version"] == result["version"]
        for entry in archive.namelist():
            assert archive.read(entry) == preview.git(
                repository, "show", f"{result['sha']}:custom_components/{entry}"
            )
    assert (
        preview.git(repository, "show", f"{result['sha']}:.github/workflows/release.yml")
        == b"# trusted workflow\n"
    )
    assert preview.text_git(repository, "diff", "--name-only", "HEAD", result["sha"]).splitlines() == [
        f"{preview.COMPONENT}/__init__.py",
        preview.MANIFEST,
    ]
    assert preview.text_git(repository, "symbolic-ref", "--short", "HEAD") == "main"
    assert json.loads((repository / preview.MANIFEST).read_text())["version"] == "0.2.1"


def test_rerun_is_reproducible_and_new_run_or_commit_has_another_version(repository, tmp_path):
    head = head_change(repository)
    first_zip, second_zip = tmp_path / "first.zip", tmp_path / "second.zip"
    first = preview.package(repository, head, 3, 123, first_zip)
    second = preview.package(repository, head, 3, 123, second_zip)
    assert first == second
    assert first_zip.read_bytes() == second_zip.read_bytes()
    assert preview.package(repository, head, 3, 124, second_zip)["version"] != first["version"]
    new_head = head_change(repository, content="// changed again\n")
    assert preview.package(repository, new_head, 3, 123, second_zip)["version"] != first["version"]


@pytest.mark.parametrize(
    "path",
    [".env", "data.sqlite3", "private.key", "credentials.json", "captures/a.json", "../outside.py", "x\n.py"],
)
def test_rejects_unsafe_data_secret_or_path_names(path):
    with pytest.raises(ValueError):
        preview.validate_path(f"{preview.COMPONENT}/{path}", "100644")


@pytest.mark.parametrize("mode", ["120000", "160000", "040000"])
def test_rejects_links_submodules_and_non_regular_entries(mode):
    with pytest.raises(ValueError, match="link or submodule"):
        preview.validate_path(f"{preview.COMPONENT}/runtime.py", mode)


def test_rejects_tracked_symlink_before_writing_zip(repository, tmp_path):
    head_change(repository)
    preview.git(repository, "checkout", "pr")
    (repository / preview.COMPONENT / "secret.py").symlink_to("/private/controller-config")
    preview.git(repository, "add", ".")
    preview.git(repository, "commit", "-m", "Unsafe link")
    head = preview.text_git(repository, "rev-parse", "HEAD")
    preview.git(repository, "checkout", "main")
    with pytest.raises(ValueError, match="link or submodule"):
        preview.package(repository, head, 3, 123, tmp_path / "preview.zip")
    assert not (tmp_path / "preview.zip").exists()


@pytest.mark.parametrize("head,pr,run_id", [("main", 3, 123), ("a" * 40, 0, 123), ("a" * 40, 3, 0)])
def test_invalid_metadata_cannot_start_packaging(repository, tmp_path, head, pr, run_id):
    with pytest.raises(ValueError, match="PR/run numbers"):
        preview.package(repository, head, pr, run_id, tmp_path / "preview.zip")


class FakeGitHub(preview.GitHub):
    """Record API operations and simulate current PR state without network access."""

    def __init__(self, head, release=None):
        super().__init__("owner/repo")
        self.head = head
        self.release = {"tag_name": "pr-3", **release} if release is not None else None
        self.other_releases = []
        self.state = "open"
        self.base = "main"
        self.base_repo = self.repository
        self.calls = []
        self.reads = 0
        self.change_after = None

    def api(self, method, path, payload=None, *, missing=False):
        self.calls.append((method, path, payload))
        if path.startswith("pulls/"):
            self.reads += 1
            if self.change_after is not None and self.reads > self.change_after:
                self.head = "a" * 40
            return {
                "head": {"sha": self.head},
                "state": self.state,
                "base": {"ref": self.base, "repo": {"full_name": self.base_repo}},
            }
        if method == "GET" and path.startswith("releases/tags/"):
            # GitHub's tag lookup is published-only, even with push access.
            if self.release and not self.release.get("draft", False):
                assert self.release["tag_name"] == path.removeprefix("releases/tags/")
                return self.release
            return None
        if method == "GET" and path.startswith("releases?"):
            query = parse_qs(urlsplit(path).query)
            size, page = int(query["per_page"][0]), int(query["page"][0])
            releases = self.other_releases + ([self.release] if self.release else [])
            return releases[(page - 1) * size : page * size]
        if method == "POST":
            assert self.release is None, "Attempted to create a duplicate draft release"
            self.release = {"id": 123, **payload}
            return self.release
        if method == "DELETE":
            assert path == f"releases/{self.release['id']}"
            self.release = None
            return None
        assert method == "PATCH" and path == f"releases/{self.release['id']}"
        self.release.update(payload)
        return self.release


@pytest.fixture
def uploads(monkeypatch):
    calls = []
    run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == "gh":
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)
        return run(command, **kwargs)

    monkeypatch.setattr(preview.subprocess, "run", fake_run)
    return calls


def test_publishes_and_updates_same_pr_release_without_changing_stable_refs(repository, tmp_path, uploads):
    head = head_change(repository)
    github = FakeGitHub(head)
    output = tmp_path / "aruba_monitor.zip"
    main = preview.text_git(repository, "rev-parse", "HEAD")
    preview.git(repository, "push", "origin", "HEAD:refs/tags/latest", "HEAD:refs/tags/v0.2.1")
    first = preview.publish(repository, github, 3, head, 123, output, run_number=1)
    assert github.release["draft"] is False
    assert github.release["prerelease"] is True
    assert github.release["make_latest"] == "false"
    assert f"`{head}`" in github.release["body"]
    assert len(uploads) == 1
    assert "--clobber" in uploads[0]
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3").split()[0] == first["sha"]

    new_head = head_change(repository, content="// revised preview\n")
    github.head = new_head
    github.calls.clear()
    second = preview.publish(repository, github, 3, new_head, 124, output, run_number=2)
    assert second["sha"] != first["sha"]
    assert not any(method == "POST" for method, _, _ in github.calls)
    patches = [payload for method, _, payload in github.calls if method == "PATCH"]
    assert patches[0]["draft"] is True
    assert patches[-1]["draft"] is False
    assert len(uploads) == 2
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3").split()[0] == second["sha"]
    for ref in ("refs/heads/main", "refs/tags/latest", "refs/tags/v0.2.1"):
        assert preview.text_git(repository, "ls-remote", "origin", ref).split()[0] == main


@pytest.mark.parametrize(
    "attribute,value",
    [("state", "closed"), ("base", "other"), ("base_repo", "other/repo"), ("head", "a" * 40)],
)
def test_closed_retargeted_and_old_rerun_events_never_publish(
    repository, tmp_path, uploads, attribute, value
):
    head = head_change(repository)
    github = FakeGitHub(head)
    setattr(github, attribute, value)
    assert preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1) is None
    assert uploads == []
    assert not (tmp_path / "preview.zip").exists()
    assert all(method == "GET" for method, _, _ in github.calls)


def test_manual_run_uses_current_open_head(repository, tmp_path, uploads):
    head = head_change(repository)
    result = preview.publish(
        repository, FakeGitHub(head), 3, "", 123, tmp_path / "aruba_monitor.zip", run_number=1
    )
    assert result["head"] == head
    assert len(uploads) == 1


@pytest.mark.parametrize("draft", [False, True])
def test_same_head_old_rerun_preserves_newer_manual_preview(repository, tmp_path, uploads, draft):
    head = head_change(repository)
    github = FakeGitHub(head)
    output = tmp_path / "aruba_monitor.zip"
    first = preview.publish(repository, github, 3, head, 123, output, run_number=1)
    newer = preview.publish(repository, github, 3, "", 124, output, run_number=2)
    assert first["version"] != newer["version"]
    github.release["draft"] = draft
    release = github.release.copy()
    github.calls.clear()

    assert preview.publish(repository, github, 3, head, 123, output, run_number=1) is None

    assert len(uploads) == 2
    assert github.release == release
    assert all(method == "GET" for method, _, _ in github.calls)
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3").split()[0] == newer["sha"]


@pytest.mark.parametrize("padding", [0, 100])
def test_same_run_rerun_reuses_version_and_repairs_its_release(repository, tmp_path, uploads, padding):
    head = head_change(repository)
    github = FakeGitHub(head)
    output = tmp_path / "aruba_monitor.zip"
    first = preview.publish(repository, github, 3, head, 123, output, run_number=1)
    release_id = github.release["id"]
    github.release["draft"] = True
    github.other_releases = [{"tag_name": f"v1.0.{i}"} for i in range(padding)]
    github.calls.clear()

    rerun = preview.publish(repository, github, 3, head, 123, output, run_number=1)

    assert rerun == first
    assert github.release["id"] == release_id
    assert github.release["draft"] is False
    assert len(uploads) == 2
    assert not any(method == "POST" for method, _, _ in github.calls)
    assert ("GET", f"releases?per_page=100&page={1 + padding // 100}", None) in github.calls


@pytest.mark.parametrize("existing", [False, True])
def test_failed_upload_recovers_same_draft_and_preserves_newer_run(
    repository, tmp_path, uploads, monkeypatch, existing
):
    head = head_change(repository)
    github = FakeGitHub(head)
    output = tmp_path / "aruba_monitor.zip"
    if existing:
        preview.publish(repository, github, 3, head, 123, output, run_number=1)
    run = preview.subprocess.run

    def fail_upload(command, **kwargs):
        if command[0] == "gh":
            raise subprocess.CalledProcessError(1, command)
        return run(command, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(preview.subprocess, "run", fail_upload)
        with pytest.raises(subprocess.CalledProcessError):
            preview.publish(repository, github, 3, head, 124, output, run_number=2)
    release = github.release.copy()
    tag = preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3")
    assert release["draft"] is True
    assert f"pr=3 run=124 sequence=2 head={head}" in release["body"]
    github.calls.clear()

    assert preview.publish(repository, github, 3, head, 123, output, run_number=1) is None
    assert github.release == release
    assert all(method == "GET" for method, _, _ in github.calls)
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3") == tag

    repaired = preview.publish(repository, github, 3, head, 124, output, run_number=2)
    assert repaired is not None
    assert github.release["id"] == release["id"]
    assert github.release["draft"] is False
    assert len(uploads) == 1 + existing
    assert not any(method == "POST" for method, _, _ in github.calls)
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3") == tag


def test_missing_release_checks_all_listing_pages():
    github = FakeGitHub("a" * 40)
    github.other_releases = [{"tag_name": f"v1.0.{i}"} for i in range(100)]

    assert github.find_release("pr-3") is None
    assert github.calls == [
        ("GET", "releases/tags/pr-3", None),
        ("GET", "releases?per_page=100&page=1", None),
        ("GET", "releases?per_page=100&page=2", None),
    ]


@pytest.mark.parametrize("draft", [False, True])
def test_unmarked_reserved_prerelease_is_not_overwritten(repository, tmp_path, uploads, draft):
    head = head_change(repository)
    github = FakeGitHub(
        head, {"id": 123, "prerelease": True, "body": "A manually created prerelease", "draft": draft}
    )
    with pytest.raises(ValueError, match="does not belong to this PR preview"):
        preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1)
    assert uploads == []
    assert all(method == "GET" for method, _, _ in github.calls)


def test_head_change_during_package_skips_before_mutating_release(repository, tmp_path, uploads):
    head = head_change(repository)
    github = FakeGitHub(head)
    github.change_after = 1
    assert preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1) is None
    assert uploads == []
    assert all(method == "GET" for method, _, _ in github.calls)
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3") == ""


def test_head_change_during_upload_leaves_preview_hidden_until_new_run(repository, tmp_path, uploads):
    head = head_change(repository)
    github = FakeGitHub(head)
    github.change_after = 2
    assert preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1) is None
    assert len(uploads) == 1
    assert github.release["draft"] is True
    release_id = github.release["id"]
    new_head = head_change(repository, content="// next PR head\n")
    github.change_after = None
    github.head = new_head
    github.calls.clear()

    assert preview.publish(repository, github, 3, new_head, 124, tmp_path / "preview.zip", run_number=2)
    assert len(uploads) == 2
    assert github.release["id"] == release_id
    assert github.release["draft"] is False
    assert f"`{new_head}`" in github.release["body"]
    assert not any(method == "POST" for method, _, _ in github.calls)


@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize(
    "release", [{"id": 123, "prerelease": False}, {"id": 123, "prerelease": True, "immutable": True}]
)
def test_existing_stable_or_immutable_release_is_not_overwritten(
    repository, tmp_path, uploads, release, draft
):
    head = head_change(repository)
    github = FakeGitHub(head, {**release, "draft": draft})
    with pytest.raises(ValueError, match="mutable prerelease"):
        preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1)
    assert uploads == []
    assert all(method == "GET" for method, _, _ in github.calls)


def test_workflow_runs_only_trusted_main_and_serializes_publication():
    workflow = Path(".github/workflows/prerelease.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "types: [opened, reopened, synchronize, closed]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "ref: main" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" in workflow
    assert "contents: write" in workflow
    assert '--run-number "$GITHUB_RUN_NUMBER"' in workflow
    assert "pip install" not in workflow
    assert "github.event.pull_request.head.ref" not in workflow


@pytest.fixture
def published_preview(repository, tmp_path, uploads):
    head = head_change(repository)
    github = FakeGitHub(head)
    result = preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1)
    return github, result


@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("padding", [0, 100])
def test_cleanup_closed_pr_removes_owned_release_and_tag_only(
    repository, published_preview, uploads, draft, padding
):
    github, _ = published_preview
    main = preview.text_git(repository, "rev-parse", "HEAD")
    preview.git(
        repository, "push", "origin", "HEAD:refs/tags/latest", "HEAD:refs/tags/v0.2.1", "HEAD:refs/tags/pr-4"
    )
    before = preview.text_git(repository, "ls-remote", "--refs", "origin").splitlines()
    github.state = "closed"
    github.release["draft"] = draft
    github.other_releases = [{"tag_name": f"v1.0.{i}"} for i in range(padding)]
    github.calls.clear()

    assert preview.cleanup(repository, github, 3)
    assert github.release is None
    assert [(method, path) for method, path, _ in github.calls if method != "GET"] == [
        ("DELETE", "releases/123")
    ]
    assert preview.text_git(repository, "ls-remote", "--refs", "origin").splitlines() == [
        line for line in before if not line.endswith("refs/tags/pr-3")
    ]
    assert preview.text_git(repository, "rev-parse", "HEAD") == main
    assert preview.text_git(repository, "status", "--porcelain") == ""
    assert len(uploads) == 1
    # Repeated close events and manual retries are harmless.
    assert not preview.cleanup(repository, github, 3)


def test_manual_preview_run_for_closed_pr_cleans_up_without_packaging(
    repository, tmp_path, published_preview, uploads
):
    github, result = published_preview
    github.state = "closed"
    output = tmp_path / "closed-pr.zip"
    assert preview.publish(repository, github, 3, "", 124, output, run_number=2) is None
    assert github.release is None
    assert preview.text_git(repository, "ls-remote", "origin", f"refs/tags/{result['tag']}") == ""
    assert not output.exists()
    assert len(uploads) == 1


@pytest.mark.parametrize(
    "attribute,value", [("state", "open"), ("base", "other"), ("base_repo", "other/repo")]
)
def test_cleanup_refuses_open_reopened_or_retargeted_pr(repository, published_preview, attribute, value):
    github, _ = published_preview
    github.state = "closed"
    setattr(github, attribute, value)
    before = preview.text_git(repository, "ls-remote", "--refs", "origin")
    github.calls.clear()
    assert not preview.cleanup(repository, github, 3)
    assert github.release is not None
    assert preview.text_git(repository, "ls-remote", "--refs", "origin") == before
    assert all(method == "GET" for method, _, _ in github.calls)


@pytest.mark.parametrize("missing", ["release", "tag"])
def test_cleanup_recovers_partial_publication_or_partial_cleanup(repository, published_preview, missing):
    github, _ = published_preview
    github.state = "closed"
    if missing == "release":
        github.release = None
    else:
        preview.git(repository, "push", "origin", ":refs/tags/pr-3")
    assert preview.cleanup(repository, github, 3)
    assert github.release is None
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3") == ""


@pytest.mark.parametrize(
    "change",
    [
        {"prerelease": False},
        {"immutable": True},
        {"body": "A manually created release"},
        {"body": "<!-- aruba-monitor-preview pr=4 run=123 sequence=1 head=" + "a" * 40 + " -->"},
    ],
)
def test_cleanup_refuses_stable_immutable_unmarked_or_other_pr_release(repository, published_preview, change):
    github, _ = published_preview
    github.state = "closed"
    github.release.update(change)
    github.calls.clear()
    before = preview.text_git(repository, "ls-remote", "--refs", "origin")
    with pytest.raises(ValueError, match="mutable prerelease owned by this PR"):
        preview.cleanup(repository, github, 3)
    assert github.release is not None
    assert preview.text_git(repository, "ls-remote", "--refs", "origin") == before
    assert all(method == "GET" for method, _, _ in github.calls)


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("replacement", ["stable", "another-pr", "wrong-head"])
def test_cleanup_validates_tag_ownership_even_with_owned_release(
    repository, tmp_path, published_preview, orphan, replacement
):
    github, result = published_preview
    github.state = "closed"
    if orphan:
        github.release = None
    if replacement == "stable":
        replacement_sha = preview.text_git(repository, "rev-parse", "HEAD")
    elif replacement == "another-pr":
        replacement_sha = preview.package(repository, github.head, 4, 124, tmp_path / "other.zip")["sha"]
    else:
        tree = preview.text_git(repository, "rev-parse", f"{result['sha']}^{{tree}}")
        replacement_sha = preview.text_git(
            repository,
            "commit-tree",
            tree,
            "-m",
            f"Preview PR #3\n\nAruba-Preview-Head: {github.head}\n"
            "Aruba-Preview-Version: 0.2.2-dev.3.123.g000000000000",
        )
    preview.git(repository, "push", "--force", "origin", f"{replacement_sha}:refs/tags/pr-3")
    before = preview.text_git(repository, "ls-remote", "--refs", "origin")
    github.calls.clear()
    with pytest.raises(ValueError, match="tag does not belong"):
        preview.cleanup(repository, github, 3)
    assert preview.text_git(repository, "ls-remote", "--refs", "origin") == before
    assert all(method == "GET" for method, _, _ in github.calls)


def test_cleanup_rechecks_closed_state_immediately_before_deletion(
    repository, published_preview, monkeypatch
):
    github, _ = published_preview
    checks = iter([True, False])
    monkeypatch.setattr(github, "closed", lambda pr: next(checks))
    github.calls.clear()
    before = preview.text_git(repository, "ls-remote", "--refs", "origin")
    assert not preview.cleanup(repository, github, 3)
    assert github.release is not None
    assert preview.text_git(repository, "ls-remote", "--refs", "origin") == before
    assert all(method == "GET" for method, _, _ in github.calls)


def test_cleanup_tag_lease_protects_concurrent_replacement(repository, published_preview, monkeypatch):
    github, _ = published_preview
    github.state = "closed"
    original_git = preview.git
    replacement = preview.text_git(repository, "rev-parse", "HEAD")

    def race(root, *args, **kwargs):
        if args[0] == "push" and args[-1] == ":refs/tags/pr-3":
            original_git(root, "push", "--force", "origin", f"{replacement}:refs/tags/pr-3")
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(preview, "git", race)
    with pytest.raises(subprocess.CalledProcessError):
        preview.cleanup(repository, github, 3)
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3").split()[0] == replacement


def test_close_during_upload_cleans_up_instead_of_leaving_hidden_preview(
    repository, tmp_path, uploads, monkeypatch
):
    head = head_change(repository)
    github = FakeGitHub(head)
    original_run = preview.subprocess.run

    def close_during_upload(command, **kwargs):
        result = original_run(command, **kwargs)
        if command[0] == "gh":
            github.state = "closed"
        return result

    monkeypatch.setattr(preview.subprocess, "run", close_during_upload)
    assert preview.publish(repository, github, 3, head, 123, tmp_path / "preview.zip", run_number=1) is None
    assert len(uploads) == 1
    assert github.release is None
    assert preview.text_git(repository, "ls-remote", "origin", "refs/tags/pr-3") == ""


def test_github_delete_accepts_empty_success_response(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.setattr(
        preview.urllib.request, "urlopen", lambda request, timeout: nullcontext(SimpleNamespace(status=204))
    )
    assert preview.GitHub("owner/repo").api("DELETE", "releases/123") is None
