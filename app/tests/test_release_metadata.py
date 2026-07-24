import re
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
