"""Exercise release bookkeeping and atomic publication against isolated Git repos."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.prepare_release import MANIFEST, git, prepare, push, version_tuple


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Release test")
    git(root, "config", "user.email", "release@example.invalid")
    (root / MANIFEST).parent.mkdir(parents=True)
    (root / MANIFEST).write_text(json.dumps({"domain": "aruba_monitor", "version": "0.2.0"}) + "\n")
    (root / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.2.0"\n')
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial commit")
    return root


def merge(root: Path, message: str = "Merged change") -> str:
    """A synthetic merged commit is sufficient for ancestor validation."""
    git(root, "commit", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def remote(root: Path) -> Path:
    target = root.parent / "remote.git"
    target.mkdir()
    git(target, "init", "--bare", "--initial-branch=main")
    git(root, "remote", "add", "origin", str(target))
    git(root, "push", "origin", "main")
    return target


def test_bumps_metadata_and_creates_matching_tag(repository):
    sha = merge(repository)
    release = prepare(repository, 17, sha)

    assert release["version"] == "0.2.1"
    assert release["created"] == "true"
    assert release["publish_latest"] == "true"
    assert json.loads((repository / MANIFEST).read_text())["version"] == "0.2.1"
    assert 'version = "0.2.1"' in (repository / "pyproject.toml").read_text()
    assert git(repository, "rev-parse", "v0.2.1^{commit}") == release["sha"]
    assert git(repository, "status", "--porcelain") == ""
    assert git(repository, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == [
        MANIFEST.as_posix(),
        "pyproject.toml",
    ]


def test_versions_advance_from_highest_stable_tag(repository):
    git(repository, "tag", "v1.4.9")
    git(repository, "tag", "v99.0.0-rc1")
    git(repository, "tag", "v01.0.0")
    release = prepare(repository, 1, merge(repository))

    assert release["version"] == "1.4.10"


@pytest.mark.parametrize("version", ["01.0.0", "1.0", "v1.0.0", "1.0.0\n", "1.0.0-rc1", "-1.0.0"])
def test_rejects_noncanonical_versions(version):
    with pytest.raises(ValueError, match="Invalid release version"):
        version_tuple(version)


def test_rerunning_same_pr_recovers_version_without_bump(repository):
    sha = merge(repository)
    release = prepare(repository, 17, sha)
    rerun = prepare(repository, 17, sha)

    assert rerun == {**release, "created": "false"}
    assert git(repository, "rev-list", "--count", "HEAD") == "3"


def test_manual_run_bumps_once_and_rerun_recovers_release(repository):
    release = prepare(repository, run_id=12345)
    rerun = prepare(repository, run_id=12345)

    assert release["version"] == "0.2.1"
    assert release["created"] == "true"
    assert release["publish_latest"] == "true"
    assert rerun == {**release, "created": "false"}
    assert git(repository, "rev-list", "--count", "HEAD") == "2"
    assert "Aruba-Release-Run: 12345" in git(repository, "show", "-s", "--format=%B").splitlines()
    assert json.loads((repository / MANIFEST).read_text())["version"] == "0.2.1"
    assert 'version = "0.2.1"' in (repository / "pyproject.toml").read_text()
    assert git(repository, "rev-parse", "v0.2.1^{commit}") == release["sha"]


def test_manual_runs_and_merged_prs_have_independent_bookkeeping(repository):
    first = prepare(repository, run_id=17)
    second = prepare(repository, 17, merge(repository))
    third = prepare(repository, run_id=18)

    assert first["version"] == "0.2.1"
    assert second["version"] == "0.2.2"
    assert third["version"] == "0.2.3"


@pytest.mark.parametrize("pr,sha,run_id", [(None, None, 0), (1, "a" * 40, 1), (None, "a" * 40, 1)])
def test_rejects_invalid_manual_event_without_mutation(repository, pr, sha, run_id):
    before = git(repository, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="positive manual run ID without PR metadata"):
        prepare(repository, pr, sha, run_id=run_id)
    assert git(repository, "rev-parse", "HEAD") == before
    assert git(repository, "status", "--porcelain") == ""


def test_old_pr_rerun_does_not_roll_latest_back(repository):
    first_merge = merge(repository)
    first = prepare(repository, 17, first_merge)
    second = prepare(repository, 18, merge(repository))
    rerun = prepare(repository, 17, first_merge)

    assert rerun["sha"] == first["sha"]
    assert rerun["publish_latest"] == "false"
    assert git(repository, "rev-parse", "HEAD") == second["sha"]


def test_rejects_same_pr_with_different_merge_commit(repository):
    prepare(repository, 17, merge(repository))
    with pytest.raises(ValueError, match="different merge commit"):
        prepare(repository, 17, merge(repository))


def test_rejects_missing_or_retargeted_version_tag(repository):
    sha = merge(repository)
    release = prepare(repository, 17, sha)
    git(repository, "tag", "--force", release["tag"], sha)
    with pytest.raises(ValueError, match="another commit"):
        prepare(repository, 17, sha)
    git(repository, "tag", "--delete", release["tag"])
    with pytest.raises(ValueError, match="missing"):
        prepare(repository, 17, sha)


@pytest.mark.parametrize("pr,sha", [(0, "a" * 40), (1, "main"), (1, "a" * 39), (1, "A" * 40)])
def test_rejects_invalid_event_without_mutation(repository, pr, sha):
    before = git(repository, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="PR number"):
        prepare(repository, pr, sha)
    assert git(repository, "rev-parse", "HEAD") == before
    assert git(repository, "status", "--porcelain") == ""


def test_rejects_dirty_checkout(repository):
    sha = merge(repository)
    (repository / "untracked").write_text("user changes")
    with pytest.raises(ValueError, match="clean checkout"):
        prepare(repository, 1, sha)


def test_rejects_other_branch(repository):
    sha = merge(repository)
    git(repository, "checkout", "-b", "feature")
    with pytest.raises(ValueError, match="main branch"):
        prepare(repository, 1, sha)


def test_rejects_unmerged_commit(repository):
    git(repository, "checkout", "-b", "unmerged")
    sha = merge(repository)
    git(repository, "checkout", "main")
    with pytest.raises(subprocess.CalledProcessError):
        prepare(repository, 1, sha)


def test_rejects_inconsistent_metadata_without_mutation(repository):
    (repository / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    git(repository, "commit", "-am", "Mismatched metadata")
    with pytest.raises(ValueError, match="versions differ"):
        prepare(repository, 1, git(repository, "rev-parse", "HEAD"))
    assert git(repository, "status", "--porcelain") == ""


def test_atomic_push_publishes_branch_and_both_tags(repository):
    target = remote(repository)
    sha = merge(repository)
    release = prepare(repository, 17, sha)

    push(repository, release, "")

    assert git(target, "rev-parse", "main") == release["sha"]
    assert git(target, "rev-parse", "latest") == release["sha"]
    assert git(target, "rev-parse", f"{release['tag']}^{{commit}}") == release["sha"]
    # The same event can repair publication without adding another version.
    push(repository, prepare(repository, 17, sha), release["sha"])
    assert git(target, "tag", "--list", "v*") == "v0.2.1"


def test_atomic_push_rejection_does_not_change_branch_or_create_tag(repository):
    target = remote(repository)
    original = git(target, "rev-parse", "main")
    release = prepare(repository, 17, merge(repository))
    # An external actor creates latest after the release job read its absence.
    git(target, "tag", "latest", original)

    with pytest.raises(subprocess.CalledProcessError):
        push(repository, release, "")

    assert git(target, "rev-parse", "main") == original
    assert git(target, "rev-parse", "latest") == original
    assert git(target, "tag", "--list", "v*") == ""


def test_concurrent_merge_rejection_can_be_rebuilt_without_skipping_version(repository):
    target = remote(repository)
    merged = merge(repository)
    git(repository, "push", "origin", "main")
    external_tree = git(repository, "rev-parse", "HEAD^{tree}")
    external = git(repository, "commit-tree", external_tree, "-p", merged, "-m", "Another merged PR")
    failed_release = prepare(repository, 17, merged)
    git(repository, "push", "origin", f"{external}:refs/heads/main")

    with pytest.raises(subprocess.CalledProcessError):
        push(repository, failed_release, "")
    assert git(target, "rev-parse", "main") == external
    assert git(target, "tag", "--list") == ""

    # These are the retry operations used by the workflow after an atomic rejection.
    git(repository, "tag", "--delete", failed_release["tag"])
    git(repository, "fetch", "--force", "--tags", "origin", "main")
    git(repository, "reset", "--hard", "origin/main")
    retried = prepare(repository, 17, merged)
    push(repository, retried, "")

    assert retried["version"] == "0.2.1"
    assert git(target, "rev-parse", "main~1") == external
    assert git(target, "rev-parse", "latest") == retried["sha"]


def test_old_pr_rerun_preserves_remote_latest_and_version_tag(repository):
    target = remote(repository)
    first_merge = merge(repository)
    first = prepare(repository, 17, first_merge)
    push(repository, first, "")
    original_version_tag = git(target, "rev-parse", first["tag"])
    second = prepare(repository, 18, merge(repository))
    push(repository, second, first["sha"])

    push(repository, prepare(repository, 17, first_merge), second["sha"])

    assert git(target, "rev-parse", "latest") == second["sha"]
    assert git(target, "rev-parse", first["tag"]) == original_version_tag


def test_old_manual_rerun_preserves_remote_latest_and_version_tag(repository):
    target = remote(repository)
    first = prepare(repository, run_id=12345)
    push(repository, first, "")
    original_version_tag = git(target, "rev-parse", first["tag"])
    second = prepare(repository, run_id=12346)
    push(repository, second, first["sha"])

    rerun = prepare(repository, run_id=12345)
    push(repository, rerun, second["sha"])

    assert rerun == {**first, "created": "false", "publish_latest": "false"}
    assert git(target, "rev-parse", "latest") == second["sha"]
    assert git(target, "rev-parse", first["tag"]) == original_version_tag
