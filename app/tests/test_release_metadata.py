import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_release_version_changelog_and_whats_new_are_synchronized():
    version = (ROOT / "VERSION").read_text().strip()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    frontend = (ROOT / "app/frontend/index.html").read_text()

    changelog_release = re.search(
        r"^## \[(\d+\.\d+\.\d+)\] — (\d{4}-\d{2}-\d{2})$",
        changelog,
        re.MULTILINE,
    )
    whats_new_release = re.search(
        r'const RELEASE_NOTES = \[\s*\{ v: "([^"]+)", date: "([^"]+)"',
        frontend,
    )

    assert changelog_release is not None
    assert whats_new_release is not None
    assert changelog_release.group(1) == version
    assert whats_new_release.group(1) == version
    assert whats_new_release.group(2) == changelog_release.group(2)


def test_update_uses_canonical_script_stop_uri():
    update = (ROOT / "update.js").read_text()

    assert 'uri: "{{path.resolve(cwd, \'start.js\')}}"' in update
    assert not re.search(
        r'method:\s*"script\.stop",\s*params:\s*\{\s*uri:\s*"start\.js"',
        update,
    )


def test_managed_release_contract_is_documented():
    readme = (ROOT / "README.md").read_text()
    capability_contract = (ROOT / "CAPABILITY_CONTRACT.md").read_text()

    assert "/api/hub/maintenance/release-intent" in readme
    assert "schema version 3" in capability_contract.lower()


def test_managed_release_examples_pin_published_sibling_merge_commits():
    integration = (ROOT / "studiohub_genstudio_integration.md").read_text()

    assert "7e6b25a73ff7e8ad4b0c1e838a697341c97eb51b" in integration
    assert "bf13bdf7d9688da87ec6e3a5e89961245beeede0" in integration


def test_dependency_convergence_bridge_release_is_truthful():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    frontend = (ROOT / "app/frontend/index.html").read_text()

    assert "## [2.11.5] — 2026-08-21" in changelog
    assert "one fixed dependency-convergence path" in changelog
    assert "Image 1.30.3" in changelog
    assert "Voice 2.4.2" in changelog
    assert "No live update occurred" in changelog
    assert 'v: "2.11.5"' in frontend
    assert "dependency-convergence capability" in frontend


def test_root_runtime_repair_state_is_ignored():
    gitignore = (ROOT / ".gitignore").read_text().splitlines()

    assert "/.enrollment_repair_journal.json" in gitignore
    assert ".enrollment_repair_journal.json.lock" in gitignore
    assert "controller_settings.json.repair.lock" in gitignore
    root = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", ".enrollment_repair_journal.json"]
    )
    nested = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "nested/.enrollment_repair_journal.json"]
    )
    assert root.returncode == 0
    assert nested.returncode == 1


def test_runtime_state_migration_release_is_truthful():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    frontend = (ROOT / "app/frontend/index.html").read_text()

    assert "## [2.11.6] — 2026-08-21" in changelog
    assert "repair journal" in changelog.lower()
    assert "never deletes" in changelog.lower()
    assert 'v: "2.11.6"' in frontend
    assert "preserved, not deleted" in frontend
