#!/usr/bin/env python3
"""Safely move local Studio checkouts to ignored runtime state."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import fleet_bootstrap


APP_SPECS = (
    ("imagestudio-mac", "Image Studio", "https://github.com/theng12/imagestudio-mac.git", 47868),
    ("voicestudio-mac", "Voice Studio", "https://github.com/theng12/voicestudio-mac.git", 47870),
    ("studiohub-mac", "Studio Hub", "https://github.com/theng12/studiohub-mac.git", 47873),
)
APPROVED_ENVIRONMENT_LINES = (
    "PINOKIO_SCRIPT_AUTOLAUNCH=start.js",
    "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false",
    "PINOKIO_SCRIPT_REQUIRES=",
)
LEGACY_HUB_EXCLUDES = (
    "/.enrollment_repair_journal.json",
    "/.enrollment_repair_journal.json.lock",
    "/controller_settings.json.repair.lock",
)
KERNEL = "pinokio://127.0.0.1:42000/api"


class MigrationError(RuntimeError):
    """The local checkout cannot be migrated without operator review."""


@dataclass(frozen=True)
class RepositoryResult:
    name: str
    path: Path | None
    status: str
    backup_path: Path | None = None
    refusal_reason: str | None = None


@dataclass(frozen=True)
class MigrationReport:
    repositories: tuple[RepositoryResult, ...]

    @property
    def ok(self) -> bool:
        return not any(item.status in {"refused", "failed"} for item in self.repositories)

    def for_name(self, name: str) -> RepositoryResult:
        return next(item for item in self.repositories if item.name == name)


@dataclass(frozen=True)
class _Repository:
    name: str
    title: str
    url: str
    port: int
    path: Path


@dataclass(frozen=True)
class _EnvironmentSnapshot:
    content: bytes
    mode: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class _EnvironmentClaim:
    path: Path
    snapshot: _EnvironmentSnapshot


CommandRunner = Callable[[Sequence[str], Optional[Path]], subprocess.CompletedProcess]
UpdateRunner = Callable[[Path, Path], str]
ActivityProbe = Callable[[_Repository], Optional[str]]
PostUpdateProbe = Callable[[_Repository], Optional[str]]


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(command), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _normalize_git_url(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git").lower()


class MigrationEngine:
    """Evidence-gated local bridge; it never broad-cleans a Studio checkout."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        backup_root: Path | None = None,
        dry_run: bool = False,
        runner: CommandRunner = _run,
        pterm_resolver: Callable[[Path], Path | None] = fleet_bootstrap.resolve_pterm,
        control_plane_ready: Callable[[], bool] = fleet_bootstrap.control_plane_ready,
        update_runner: UpdateRunner | None = None,
        activity_probe: ActivityProbe | None = None,
        post_update_probe: PostUpdateProbe | None = None,
        app_specs: tuple[tuple[str, str, str, int], ...] = APP_SPECS,
    ) -> None:
        self.home = home
        self.backup_root = backup_root or (
            Path.home() / "Library/Application Support/TerraNash/runtime-state-migration"
        )
        self.dry_run = dry_run
        self.runner = runner
        self.pterm_resolver = pterm_resolver
        self.control_plane_ready = control_plane_ready
        self.update_runner = update_runner or self._run_update
        self.activity_probe = activity_probe or self._active_work
        self.post_update_probe = post_update_probe or self._post_update_state
        self.app_specs = app_specs
        self._pterm: Path | None = None

    def plan(self) -> MigrationReport:
        """Return the exact local state without writing to any checkout."""
        try:
            repositories = self._resolve_repositories()
        except MigrationError as exc:
            return MigrationReport(tuple(
                RepositoryResult(name, None, "refused", refusal_reason=str(exc))
                for name, _title, _url, _port in self.app_specs
            ))
        results = []
        for repository in repositories:
            try:
                results.append(self._plan_repository(repository))
            except MigrationError as exc:
                results.append(RepositoryResult(
                    repository.name, repository.path, "refused", refusal_reason=str(exc)
                ))
        return MigrationReport(tuple(results))

    def run(self) -> MigrationReport:
        """Apply only a fully preflighted plan, Image then Voice then Hub."""
        report = self.plan()
        if not report.ok or self.dry_run:
            return report
        repositories = self._resolve_repositories()
        planned = {result.name: result for result in report.repositories}
        results: list[RepositoryResult] = []
        for index, repository in enumerate(repositories):
            result = planned[repository.name]
            if result.status == "already_ready":
                results.append(result)
                continue
            try:
                backup = self._migrate_repository(repository)
                results.append(RepositoryResult(repository.name, repository.path, "migrated", backup))
            except MigrationError as exc:
                results.append(RepositoryResult(
                    repository.name,
                    repository.path,
                    "failed",
                    backup_path=getattr(exc, "backup_path", None),
                    refusal_reason=str(exc),
                ))
                results.extend(RepositoryResult(
                    future.name, future.path, "not_run"
                ) for future in repositories[index + 1:])
                return MigrationReport(tuple(results))
        return MigrationReport(tuple(results))

    def _resolve_repositories(self) -> tuple[_Repository, ...]:
        home = self.home or fleet_bootstrap.resolve_pinokio_home()
        if home is None or not home.is_dir():
            raise MigrationError("PINOKIO_HOME could not be resolved; open Pinokio and retry")
        pterm = self.pterm_resolver(home)
        if pterm is None or not pterm.is_file() or not os.access(pterm, os.X_OK):
            raise MigrationError("Pinokio pterm is unavailable; finish Pinokio setup and retry")
        if not self.control_plane_ready():
            raise MigrationError("Pinokio control plane is unavailable; open Pinokio and retry")
        self._pterm = pterm
        repositories = []
        for name, title, url, port in self.app_specs:
            canonical = home / "api" / name
            legacy = home / "api" / f"{name}.git"
            target = canonical if canonical.is_dir() else legacy if legacy.is_dir() else None
            if target is None:
                raise MigrationError(f"{title} checkout is missing under {home / 'api'}")
            repository = _Repository(name, title, url, port, target)
            self._validate_repository(repository)
            active = self.activity_probe(repository)
            if active:
                raise MigrationError(f"{title} has active {active}; wait for it to finish")
            repositories.append(repository)
        return tuple(repositories)

    def _validate_repository(self, repository: _Repository) -> None:
        self._git_text(repository, "rev-parse", "--git-dir")
        origin = self._git_optional_text(repository, "config", "--get", "remote.origin.url")
        if _normalize_git_url(origin) != _normalize_git_url(repository.url):
            raise MigrationError(f"{repository.title} has an unexpected Git origin")
        if self._git_text(repository, "symbolic-ref", "--quiet", "--short", "HEAD").strip() != "main":
            raise MigrationError(f"{repository.title} must be on main")
        upstream = self._git_text(
            repository, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        ).strip()
        if upstream != "origin/main":
            raise MigrationError(f"{repository.title} must track origin/main")
        if not (repository.path / "update.js").is_file():
            raise MigrationError(f"{repository.title} has no update.js")

    def _plan_repository(self, repository: _Repository) -> RepositoryResult:
        status = self._status(repository)
        if repository.name == "studiohub-mac":
            return self._plan_hub(repository, status)
        return self._plan_studio(repository, status)

    def _plan_studio(self, repository: _Repository, status: tuple[tuple[str, str], ...]) -> RepositoryResult:
        environment = repository.path / "ENVIRONMENT"
        is_tracked = self._git_code(repository, "ls-files", "--error-unmatch", "ENVIRONMENT") == 0
        if not is_tracked:
            if status:
                raise MigrationError(self._unknown_status(repository, status))
            if self._git_code(repository, "check-ignore", "-q", "ENVIRONMENT") != 0:
                raise MigrationError(f"{repository.title} does not ignore root ENVIRONMENT")
            return RepositoryResult(repository.name, repository.path, "already_ready")
        if self._is_exact_legacy_environment(repository, status, environment):
            return RepositoryResult(repository.name, repository.path, "requires_migration")
        if status:
            raise MigrationError(self._unknown_status(repository, status))
        return RepositoryResult(repository.name, repository.path, "requires_migration")

    def _plan_hub(self, repository: _Repository, status: tuple[tuple[str, str], ...]) -> RepositoryResult:
        unknown = tuple(item for item in status if item[1] not in {
            pattern.removeprefix("/") for pattern in LEGACY_HUB_EXCLUDES
        })
        if unknown:
            raise MigrationError(self._unknown_status(repository, unknown))
        journal_is_ignored = self._git_code(
            repository, "check-ignore", "-q", ".enrollment_repair_journal.json"
        ) == 0
        if journal_is_ignored and not status:
            return RepositoryResult(repository.name, repository.path, "already_ready")
        return RepositoryResult(repository.name, repository.path, "requires_migration")

    def _is_exact_legacy_environment(
        self, repository: _Repository, status: tuple[tuple[str, str], ...], environment: Path
    ) -> bool:
        if status != ((" M", "ENVIRONMENT"),):
            return False
        try:
            mode = environment.lstat().st_mode
        except OSError:
            return False
        if not stat.S_ISREG(mode) or environment.is_symlink():
            return False
        current = environment.read_bytes()
        return self._is_exact_environment_content(repository, current)

    def _is_exact_environment_content(self, repository: _Repository, current: bytes) -> bool:
        original = self._git_bytes(repository, "show", "HEAD:ENVIRONMENT")
        lines = current.splitlines(keepends=True)
        removed = [line for line in lines if line.rstrip(b"\r\n").decode("utf-8", "surrogateescape")
                   in APPROVED_ENVIRONMENT_LINES]
        retained = b"".join(line for line in lines if line not in removed)
        return (
            retained == original
            and tuple(line.rstrip(b"\r\n").decode("utf-8", "surrogateescape") for line in removed)
            == APPROVED_ENVIRONMENT_LINES
        )

    def _migrate_repository(self, repository: _Repository) -> Path | None:
        if self._plan_repository(repository).status != "requires_migration":
            raise MigrationError(f"{repository.title} changed after preflight; rerun the migration")
        if repository.name == "studiohub-mac":
            self._add_hub_excludes(repository)
            self._pull(repository)
            self._verify_hub(repository)
            self._invoke_update(repository)
            self._verify_update_state(repository)
            self._verify_hub(repository)
            return None
        environment = repository.path / "ENVIRONMENT"
        snapshot = self._validated_environment_snapshot(repository, environment)
        self._require_snapshot_current(repository, environment, snapshot)
        backup = self._backup_environment(repository, snapshot.content)
        original = snapshot.content
        mode = snapshot.mode
        claim: _EnvironmentClaim | None = None
        head: _EnvironmentClaim | None = None
        try:
            claim = self._claim_environment(repository, environment, snapshot, "machine")
            head = self._restore_head_environment(repository)
            self._require_clean(repository)
            self._pull(repository)
        except MigrationError as exc:
            exc = MigrationError(self._recover_pre_pull(repository, environment, claim, head, exc))
            exc.backup_path = backup
            raise exc
        restored: _EnvironmentSnapshot | None = None
        try:
            self._discard_claim(repository, head)
            assert claim is not None
            restored = self._install_claim_into_empty(repository, claim, environment)
            assert restored is not None
            self._verify_studio(repository, environment, original)
            self._invoke_update(repository)
            self._verify_update_state(repository)
            self._verify_studio(repository, environment, original)
            self._discard_claim(repository, claim)
            return backup
        except MigrationError as exc:
            if claim is not None and restored is not None:
                exc = MigrationError(
                    self._recover_post_update(repository, environment, claim, restored, original, mode, exc)
                )
            else:
                exc = MigrationError(f"{exc}; ENVIRONMENT recovery claim was unavailable; backup preserved")
            exc.backup_path = backup
            raise exc

    def _validated_environment_snapshot(
        self, repository: _Repository, environment: Path
    ) -> _EnvironmentSnapshot:
        snapshot = self._snapshot_environment(repository, environment)
        status = self._status(repository)
        if self._git_code(repository, "ls-files", "--error-unmatch", "ENVIRONMENT") != 0:
            raise MigrationError(f"{repository.title} no longer tracks ENVIRONMENT")
        if status == ((" M", "ENVIRONMENT"),):
            if not self._is_exact_environment_content(repository, snapshot.content):
                raise MigrationError(self._unknown_status(repository, status))
        elif status:
            raise MigrationError(self._unknown_status(repository, status))
        self._require_snapshot_current(repository, environment, snapshot)
        return snapshot

    @staticmethod
    def _snapshot_environment(repository: _Repository, environment: Path) -> _EnvironmentSnapshot:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise MigrationError(f"{repository.title} cannot safely snapshot ENVIRONMENT on this platform")
        try:
            descriptor = os.open(environment, os.O_RDONLY | nofollow)
        except OSError as exc:
            raise MigrationError(f"{repository.title} cannot safely open ENVIRONMENT: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise MigrationError(f"{repository.title} ENVIRONMENT is not a regular file")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                content = handle.read()
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        return _EnvironmentSnapshot(
            content=content,
            mode=stat.S_IMODE(metadata.st_mode),
            identity=(metadata.st_dev, metadata.st_ino),
        )

    def _require_snapshot_current(
        self, repository: _Repository, environment: Path, snapshot: _EnvironmentSnapshot
    ) -> None:
        if not self._snapshot_matches_current(repository, environment, snapshot):
            raise MigrationError(
                f"{repository.title} ENVIRONMENT changed after validation; refusing to overwrite it"
            )

    def _snapshot_matches_current(
        self, repository: _Repository, environment: Path, snapshot: _EnvironmentSnapshot
    ) -> bool:
        try:
            current = self._snapshot_environment(repository, environment)
        except MigrationError:
            return False
        return (
            current == snapshot
            and self._same_regular_file(environment, current.identity)
        )

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int]:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise MigrationError("ENVIRONMENT changed to a non-regular file")
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _same_regular_file(path: Path, identity: tuple[int, int]) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and not path.is_symlink()
            and (metadata.st_dev, metadata.st_ino) == identity
        )

    def _backup_environment(self, repository: _Repository, content: bytes) -> Path:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.backup_root / stamp / repository.name / "ENVIRONMENT"
        path.parent.mkdir(parents=True, exist_ok=False)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        path.chmod(0o600)
        return path

    def _claim_directory(self, repository: _Repository, environment: Path) -> Path:
        claim_directory = self._git_path(repository, "runtime-state-migration")
        try:
            claim_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = claim_directory.lstat()
            if claim_directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("claim path is not a directory")
            if metadata.st_dev != environment.parent.stat().st_dev:
                raise OSError("claim path is not on the checkout filesystem")
        except OSError as exc:
            raise MigrationError(
                f"{repository.title} cannot create a safe Git claim directory: {exc}"
            ) from exc
        return claim_directory

    def _write_claim(
        self, repository: _Repository, environment: Path, content: bytes, mode: int, purpose: str
    ) -> _EnvironmentClaim:
        directory = self._claim_directory(repository, environment)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{purpose}-", suffix=".ENVIRONMENT", dir=directory
        )
        path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(mode)
            snapshot = self._snapshot_environment(repository, path)
            if snapshot.content != content or snapshot.mode != mode:
                raise MigrationError(f"{repository.title} could not verify {purpose} ENVIRONMENT claim")
            return _EnvironmentClaim(path, snapshot)
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _capture_environment(
        self, repository: _Repository, environment: Path, purpose: str
    ) -> _EnvironmentClaim:
        """Atomically move the current path into Git-owned recovery storage."""
        directory = self._claim_directory(repository, environment)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{purpose}-", suffix=".ENVIRONMENT", dir=directory
        )
        path = Path(temporary_name)
        reservation = self._file_identity(path)
        os.close(descriptor)
        try:
            os.replace(environment, path)
        except OSError as exc:
            if self._same_regular_file(path, reservation):
                try:
                    path.unlink()
                except OSError:
                    pass
            raise MigrationError(f"{repository.title} could not atomically claim ENVIRONMENT: {exc}") from exc
        try:
            claimed = self._snapshot_environment(repository, path)
        except MigrationError as exc:
            error = MigrationError(f"{repository.title} claimed ENVIRONMENT is unsafe: {exc}")
            error.claim_path = path
            raise error from exc
        return _EnvironmentClaim(path, claimed)

    def _claim_environment(
        self,
        repository: _Repository,
        environment: Path,
        expected: _EnvironmentSnapshot,
        purpose: str,
    ) -> _EnvironmentClaim:
        claim = self._capture_environment(repository, environment, purpose)
        claimed = claim.snapshot
        path = claim.path
        if claimed != expected or not self._snapshot_matches_current(repository, path, expected):
            restored = self._install_claim_into_empty(repository, claim, environment, require_expected=False)
            location = str(path) if restored is None else str(environment)
            error = MigrationError(
                f"{repository.title} ENVIRONMENT changed after validation concurrently; "
                f"preserved it at {location}"
            )
            error.claim_path = path
            raise error
        return claim

    def _install_claim_into_empty(
        self,
        repository: _Repository,
        claim: _EnvironmentClaim,
        environment: Path,
        *,
        require_expected: bool = True,
    ) -> _EnvironmentSnapshot | None:
        if require_expected and not self._snapshot_matches_current(repository, claim.path, claim.snapshot):
            raise MigrationError(f"{repository.title} claimed ENVIRONMENT changed; retained {claim.path}")
        try:
            os.link(claim.path, environment, follow_symlinks=False)
        except FileExistsError:
            if require_expected:
                raise MigrationError(
                    f"{repository.title} has concurrent ENVIRONMENT state; retained {claim.path}"
                )
            return None
        except OSError as exc:
            raise MigrationError(
                f"{repository.title} could not restore claimed ENVIRONMENT at {claim.path}: {exc}"
            ) from exc
        installed = self._snapshot_environment(repository, environment)
        if require_expected and installed != claim.snapshot:
            raise MigrationError(
                f"{repository.title} ENVIRONMENT changed concurrently; retained {claim.path}"
            )
        return installed

    def _discard_claim(self, repository: _Repository, claim: _EnvironmentClaim | None) -> None:
        if claim is None:
            return
        if not self._snapshot_matches_current(repository, claim.path, claim.snapshot):
            raise MigrationError(f"{repository.title} retained changed claim at {claim.path}")
        try:
            claim.path.unlink()
        except OSError as exc:
            raise MigrationError(f"{repository.title} could not clean claim {claim.path}: {exc}") from exc

    def _restore_head_environment(self, repository: _Repository) -> _EnvironmentClaim:
        content = self._git_bytes(repository, "show", "HEAD:ENVIRONMENT")
        mode_text = self._git_text(
            repository, "ls-tree", "--format=%(objectmode)", "HEAD", "--", "ENVIRONMENT"
        ).strip()
        try:
            mode = int(mode_text, 8) & 0o777
        except ValueError as exc:
            raise MigrationError(f"{repository.title} has no regular HEAD ENVIRONMENT template") from exc
        if not mode_text.startswith("100"):
            raise MigrationError(f"{repository.title} has no regular HEAD ENVIRONMENT template")
        environment = repository.path / "ENVIRONMENT"
        head = self._write_claim(repository, environment, content, mode, "head")
        self._install_claim_into_empty(repository, head, environment)
        return head

    def _recover_pre_pull(
        self,
        repository: _Repository,
        environment: Path,
        claim: _EnvironmentClaim | None,
        head: _EnvironmentClaim | None,
        error: MigrationError,
    ) -> str:
        if claim is None:
            retained = getattr(error, "claim_path", None)
            if retained is not None:
                return f"{error}; retained concurrent claim at {retained} with backup"
            return f"{error}; no ENVIRONMENT claim was created"
        if head is not None and environment.exists() and not environment.is_symlink():
            try:
                retired = self._claim_environment(repository, environment, head.snapshot, "head-recovery")
                self._discard_claim(repository, retired)
            except MigrationError:
                return f"{error}; concurrent ENVIRONMENT change; retained {claim.path} and backup"
        elif environment.exists() or environment.is_symlink():
            return f"{error}; concurrent ENVIRONMENT change; retained {claim.path} and backup"
        try:
            self._install_claim_into_empty(repository, claim, environment)
        except MigrationError:
            return f"{error}; retained {claim.path} and backup for recovery"
        return f"{error}; restored ENVIRONMENT and retained {claim.path} with backup"

    def _recover_post_update(
        self,
        repository: _Repository,
        environment: Path,
        original_claim: _EnvironmentClaim,
        restored: _EnvironmentSnapshot,
        original: bytes,
        mode: int,
        error: MigrationError,
    ) -> str:
        try:
            current = self._capture_environment(repository, environment, "post-update")
        except MigrationError as capture_error:
            retained = getattr(capture_error, "claim_path", original_claim.path)
            return f"{error}; concurrent ENVIRONMENT state retained at {retained}; backup preserved"

        if current.snapshot.identity != restored.identity:
            try:
                self._install_claim_into_empty(repository, current, environment)
            except MigrationError:
                return f"{error}; concurrent ENVIRONMENT claim retained at {current.path}; backup preserved"
            return f"{error}; concurrently replaced ENVIRONMENT restored from {current.path}; backup preserved"

        if current.snapshot != restored:
            try:
                recovery = self._write_claim(repository, environment, original, mode, "original-recovery")
                self._install_claim_into_empty(repository, recovery, environment)
            except MigrationError:
                return (
                    f"{error}; divergent ENVIRONMENT retained at {current.path}; "
                    "backup preserved"
                )
            return (
                f"{error}; divergent ENVIRONMENT retained at {current.path}; "
                "restored approved original from a recovery claim"
            )

        try:
            self._install_claim_into_empty(repository, current, environment)
        except MigrationError:
            return f"{error}; unchanged ENVIRONMENT retained at {current.path}; backup preserved"
        return f"{error}; restored unchanged ENVIRONMENT from {current.path}; backup preserved"

    def _add_hub_excludes(self, repository: _Repository) -> None:
        exclude = self._git_path(repository, "info/exclude")
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            missing = [pattern for pattern in LEGACY_HUB_EXCLUDES if pattern not in existing.splitlines()]
            if not missing:
                return
            content = existing + ("" if not existing or existing.endswith("\n") else "\n")
            temporary = exclude.with_name(f".{exclude.name}.{os.getpid()}.migration")
            temporary.write_text(content + "\n".join(missing) + "\n", encoding="utf-8")
            os.replace(temporary, exclude)
        except OSError as exc:
            raise MigrationError(f"{repository.title} could not update Git excludes: {exc}") from exc

    def _pull(self, repository: _Repository) -> None:
        self._require_success(repository, "pull", "--ff-only")

    def _invoke_update(self, repository: _Repository) -> None:
        assert self._pterm is not None
        try:
            self.update_runner(repository.path, self._pterm)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise MigrationError(f"{repository.title} update.js failed: {exc}") from exc

    def _verify_update_state(self, repository: _Repository) -> None:
        reason = self.post_update_probe(repository)
        if reason:
            raise MigrationError(f"{repository.title} update verification failed: {reason}")

    def _run_update(self, target: Path, pterm: Path) -> str:
        result = self.runner(
            (str(pterm), "start", "update.js", "--ref", f"{KERNEL}/{target.name}"), None
        )
        output = result.stdout + result.stderr
        if result.returncode:
            raise RuntimeError(output.decode("utf-8", "replace")[-500:])
        return output.decode("utf-8", "replace")

    def _verify_studio(self, repository: _Repository, environment: Path, expected: bytes) -> None:
        if self._git_code(repository, "ls-files", "--error-unmatch", "ENVIRONMENT") == 0:
            raise MigrationError(f"{repository.title} still tracks ENVIRONMENT")
        if self._git_code(repository, "check-ignore", "-q", "ENVIRONMENT") != 0:
            raise MigrationError(f"{repository.title} does not ignore ENVIRONMENT")
        if not environment.is_file() or environment.is_symlink():
            raise MigrationError(f"{repository.title} did not preserve machine ENVIRONMENT")
        if environment.read_bytes() != expected:
            raise MigrationError(f"{repository.title} update did not preserve machine ENVIRONMENT")
        self._require_clean(repository)

    def _verify_hub(self, repository: _Repository) -> None:
        if self._git_code(repository, "check-ignore", "-q", ".enrollment_repair_journal.json") != 0:
            raise MigrationError("Studio Hub does not ignore the repair journal")
        self._require_clean(repository)

    def _require_clean(self, repository: _Repository) -> None:
        status = self._status(repository)
        if status:
            raise MigrationError(self._unknown_status(repository, status))

    def _status(self, repository: _Repository) -> tuple[tuple[str, str], ...]:
        data = self._git_bytes(repository, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
        entries: list[tuple[str, str]] = []
        fields = data.split(b"\0")
        index = 0
        while index < len(fields):
            raw = fields[index]
            index += 1
            if not raw:
                continue
            if len(raw) < 4 or raw[2:3] != b" ":
                raise MigrationError(f"{repository.title} returned malformed Git porcelain")
            code = raw[:2].decode("ascii", "replace")
            path = raw[3:].decode("utf-8", "surrogateescape")
            if "R" in code or "C" in code:
                if index >= len(fields) or not fields[index]:
                    raise MigrationError(f"{repository.title} returned truncated Git rename porcelain")
                original = fields[index].decode("utf-8", "surrogateescape")
                index += 1
                path = f"{path} -> {original}"
            entries.append((code, path))
        return tuple(entries)

    def _unknown_status(self, repository: _Repository, status: tuple[tuple[str, str], ...]) -> str:
        paths = ", ".join(path for _code, path in status)
        return f"{repository.title} has unapproved local changes: {paths}"

    def _git_bytes(self, repository: _Repository, *arguments: str) -> bytes:
        result = self.runner(("git", "-C", str(repository.path), *arguments), None)
        if result.returncode:
            raise MigrationError(
                f"{repository.title} Git inspection failed: "
                f"{result.stderr.decode('utf-8', 'replace').strip()}"
            )
        return result.stdout

    def _git_text(self, repository: _Repository, *arguments: str) -> str:
        return self._git_bytes(repository, *arguments).decode("utf-8", "surrogateescape")

    def _git_optional_text(self, repository: _Repository, *arguments: str) -> str:
        result = self.runner(("git", "-C", str(repository.path), *arguments), None)
        return result.stdout.decode("utf-8", "surrogateescape") if result.returncode == 0 else ""

    def _git_path(self, repository: _Repository, path: str) -> Path:
        value = self._git_text(repository, "rev-parse", "--git-path", path).strip()
        if not value:
            raise MigrationError(f"{repository.title} returned no Git path for {path}")
        candidate = Path(value)
        return candidate if candidate.is_absolute() else repository.path / candidate

    def _git_code(self, repository: _Repository, *arguments: str) -> int:
        return self.runner(("git", "-C", str(repository.path), *arguments), None).returncode

    def _require_success(self, repository: _Repository, *arguments: str) -> None:
        result = self.runner(("git", "-C", str(repository.path), *arguments), None)
        if result.returncode:
            raise MigrationError(
                f"{repository.title} git {' '.join(arguments)} failed: "
                f"{result.stderr.decode('utf-8', 'replace').strip()}"
            )

    @staticmethod
    def _active_work(repository: _Repository) -> str | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{repository.port}/api/health", timeout=2
            ) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            payload = None
        if isinstance(payload, dict) and repository.name == "voicestudio-mac" and payload.get("busy") is True:
            return "customer work"
        if isinstance(payload, dict) and repository.name == "imagestudio-mac":
            generation = payload.get("generation")
            if isinstance(generation, dict) and (
                generation.get("busy") is True
                or any(
                    type(generation.get(key)) is int and generation[key] > 0
                    for key in ("queued", "running")
                )
            ):
                return "customer work"
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{repository.port}/api/auto-update/status", timeout=2
            ) as response:
                update = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(update, dict):
            return None
        if update.get("state") in {
            "checking", "updating", "installing", "restarting"
        }:
            return "an update or maintenance operation"
        return None

    @staticmethod
    def _post_update_state(repository: _Repository) -> str | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{repository.port}/api/health", timeout=10
            ) as response:
                health = json.loads(response.read())
            with urllib.request.urlopen(
                f"http://127.0.0.1:{repository.port}/api/auto-update/status", timeout=10
            ) as response:
                update = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return f"local health/update status unavailable ({exc})"
        if not isinstance(health, dict) or not isinstance(update, dict):
            return "local health/update status was malformed"
        if health.get("ok") is not True:
            return "local health status is not ok"
        if update.get("state") in {"checking", "updating", "installing", "restarting"}:
            return f"update remains {update['state']}"
        capabilities = update.get("capabilities")
        if not isinstance(capabilities, dict):
            return "local auto-update capabilities were malformed"
        convergence = capabilities.get("dependency_convergence")
        if type(convergence) is not int or convergence != 1:
            return "dependency convergence capability is unavailable"
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect without changing local checkouts")
    args = parser.parse_args(argv)
    report = MigrationEngine(dry_run=args.dry_run).run()
    for result in report.repositories:
        detail = result.refusal_reason or (str(result.backup_path) if result.backup_path else "")
        print(f"{result.name}: {result.status}{': ' + detail if detail else ''}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
