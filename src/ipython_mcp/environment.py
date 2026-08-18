"""Startup-only task environment preparation for the in-process shell."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import ServerConfig


logger = logging.getLogger(__name__)
_METADATA = ".ipython-mcp-environment.json"
_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")


class StartupPhaseError(RuntimeError):
    """A bounded, redacted startup phase failed before a shell existed."""

    def __init__(self, phase: str, message: str):
        self.phase = phase
        super().__init__(f"task environment startup failed during {phase}: {message}")


class StartupEnvironment:
    """Apply and later restore process inputs needed by one shell lifespan."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.environment_dir: Path | None = None
        self.site_packages: Path | None = None
        self.requirements_fingerprint: str | None = None
        self._added_paths: list[str] = []
        self._previous_environment: dict[str, str | None] = {}

    def prepare(self) -> None:
        try:
            if self.config.active_environment is not None:
                self.environment_dir, self.site_packages = self._prepare_uv_environment()
            self._apply_environment_variables()
            self._apply_paths()
        except StartupPhaseError:
            self.restore()
            raise
        except Exception as exc:
            self.restore()
            raise self._error("configuration", exc) from exc

    def restore(self) -> None:
        for path in self._added_paths:
            with contextlib.suppress(ValueError):
                sys.path.remove(path)
        self._added_paths.clear()
        for name, previous in self._previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        self._previous_environment.clear()

    def _prepare_uv_environment(self) -> tuple[Path, Path]:
        workspace = self._workspace()
        assert self.config.active_environment is not None
        target = workspace / self.config.active_environment
        if target.is_symlink():
            raise StartupPhaseError(
                "path_validation", "selected environment may not be a symlink"
            )
        if target.exists():
            if not target.is_dir() or target.resolve().parent != workspace:
                raise StartupPhaseError(
                    "path_validation", "selected environment escapes its workspace"
                )
            return target, self._validate_existing(target)

        uv = shutil.which("uv")
        if uv is None:
            raise StartupPhaseError("provisioning", "uv is not available on PATH")
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=workspace)
        )
        try:
            self._run(
                [
                    uv,
                    "venv",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(temporary),
                ],
                workspace,
                "provisioning",
            )
            if self.config.environment_requirements:
                self._run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(self._environment_python(temporary)),
                        *self.config.environment_requirements,
                    ],
                    workspace,
                    "dependency_install",
                )
            (temporary / _METADATA).write_text(
                json.dumps(self._expected_metadata(), sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            site_packages = self._site_packages(temporary)
            if not site_packages.is_dir():
                raise StartupPhaseError(
                    "provisioning", "created environment has no site-packages directory"
                )
            try:
                temporary.rename(target)
            except FileExistsError:
                return target, self._validate_existing(target)
            logger.info("phase=task_environment_provisioning outcome=ok")
            return target, self._site_packages(target)
        except StartupPhaseError:
            raise
        except Exception as exc:
            raise self._error("provisioning", exc) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _workspace(self) -> Path:
        assert self.config.environment_workspace is not None
        configured = self.config.environment_workspace
        if _has_symlink_component(configured):
            raise StartupPhaseError(
                "path_validation", "environment workspace may not contain symlinks"
            )
        project = Path.cwd().resolve()
        prospective = configured.resolve(strict=False)
        if prospective == project or project in prospective.parents:
            raise StartupPhaseError(
                "path_validation", "environment workspace must be outside the project"
            )
        try:
            configured.mkdir(parents=True, exist_ok=True)
            workspace = configured.resolve(strict=True)
        except OSError as exc:
            raise self._error("path_validation", exc) from exc
        return workspace

    def _validate_existing(self, target: Path) -> Path:
        metadata_path = target / _METADATA
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise self._error("reuse_validation", exc) from exc
        if metadata != self._expected_metadata():
            raise StartupPhaseError(
                "reuse_validation",
                "existing environment metadata does not match this startup configuration",
            )
        site_packages = self._site_packages(target)
        if not site_packages.is_dir():
            raise StartupPhaseError(
                "reuse_validation", "existing environment has no site-packages directory"
            )
        return site_packages

    def _expected_metadata(self) -> dict[str, Any]:
        requirements = list(self.config.environment_requirements)
        fingerprint = hashlib.sha256(
            json.dumps(
                requirements, ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.requirements_fingerprint = fingerprint
        return {
            "format": 1,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "requirements": requirements,
            "requirements_fingerprint": fingerprint,
            "system_site_packages": False,
        }

    @staticmethod
    def _environment_python(environment: Path) -> Path:
        return environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )

    @staticmethod
    def _site_packages(environment: Path) -> Path:
        if os.name == "nt":
            return environment / "Lib" / "site-packages"
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        return environment / "lib" / version / "site-packages"

    def _run(self, command: list[str], cwd: Path, phase: str) -> None:
        process_environment = os.environ.copy()
        process_environment["UV_NO_CONFIG"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=process_environment,
                capture_output=True,
                text=True,
                timeout=self.config.environment_setup_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise StartupPhaseError(
                phase, "operation exceeded the configured timeout"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "uv failed"
            raise StartupPhaseError(phase, self._redact(detail))

    def _apply_environment_variables(self) -> None:
        for name, value in self.config.environment_variables.items():
            self._previous_environment[name] = os.environ.get(name)
            os.environ[name] = value

    def _apply_paths(self) -> None:
        paths: list[Path] = []
        if self.site_packages is not None:
            paths.append(self.site_packages)
        for configured in self.config.library_paths:
            if _has_symlink_component(configured):
                raise StartupPhaseError(
                    "library_paths", "configured library path may not contain symlinks"
                )
            path = configured.expanduser().resolve()
            if not path.is_dir():
                raise StartupPhaseError(
                    "library_paths", "configured library path does not exist"
                )
            paths.append(path)
        for path in reversed(paths):
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
                self._added_paths.append(text)

    def _error(self, phase: str, exc: BaseException) -> StartupPhaseError:
        message = str(exc) or type(exc).__name__
        return StartupPhaseError(phase, self._redact(message))

    def _redact(self, value: str) -> str:
        redacted = _CREDENTIALS.sub(r"\g<scheme>***@", value)
        for secret in self.config.environment_variables.values():
            if secret:
                redacted = redacted.replace(secret, "***")
        limit = self.config.max_text_chars
        if len(redacted) > limit:
            redacted = redacted[: max(0, limit - 1)] + "…"
        return redacted


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            return True
    return False
