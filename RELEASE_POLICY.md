# Studio Hub release policy

Every committed or pushed Studio Hub change is a release. This applies to code,
frontend, configuration, documentation, and integration-contract changes.

Before committing:

1. Increment `VERSION` according to the SemVer guidance in `CHANGELOG.md`.
2. Add a matching dated release section immediately below `Unreleased` in
   `CHANGELOG.md`.
3. Add the same version and date as the first entry in the dashboard's
   `RELEASE_NOTES` list in `app/frontend/index.html`.
4. Describe the operator-visible additions, changes, fixes, and important
   safety or upgrade behavior in both the changelog and **What's New**.
5. Run the complete test suite, syntax checks, and `git diff --check` before
   committing and pushing.

Documentation-only changes receive at least a patch-version increase. Do not
push an unversioned change or leave release details only under `Unreleased`.

`app/tests/test_release_metadata.py` verifies that the three release metadata
sources identify the same newest release. This test complements—rather than
replaces—the required version increase for every new commit.

