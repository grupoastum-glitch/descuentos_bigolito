"""Clona el repo al arrancar, y más tarde commitea + pushea los archivos actualizados."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import config

log = logging.getLogger("scraper.git_publish")


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True, timeout=config.GIT_TIMEOUT_SEGUNDOS
    )


def clonar_repo() -> Path:
    destino = Path(config.GIT_CLONE_DIR)
    if destino.exists():
        shutil.rmtree(destino)
    url = f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{config.GITHUB_REPO}.git"
    _run(["git", "clone", "--branch", config.GITHUB_BRANCH, "--single-branch", url, str(destino)])
    log.info("Repo clonado en %s", destino)
    return destino


def publicar_cambios(repo_dir: Path, rutas: list[str], mensaje: str) -> bool:
    """Commitea y pushea las rutas indicadas (relativas a repo_dir). Devuelve True si hubo algo para publicar."""
    _run(["git", "add", *rutas], cwd=str(repo_dir))

    resultado = _run(["git", "status", "--porcelain"], cwd=str(repo_dir))
    if not resultado.stdout.strip():
        log.info("Sin cambios para publicar")
        return False

    _run([
        "git",
        "-c", f"user.name={config.GIT_AUTHOR_NAME}",
        "-c", f"user.email={config.GIT_AUTHOR_EMAIL}",
        "commit", "-m", mensaje,
    ], cwd=str(repo_dir))

    try:
        _run(["git", "push", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
    except subprocess.CalledProcessError as error:
        log.warning(
            "Push rechazado (%s), intentando 'pull --rebase' y reintentando una vez",
            _detalle_error(error),
        )
        try:
            _run(["git", "pull", "--rebase", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
            _run(["git", "push", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
        except subprocess.CalledProcessError as error_reintento:
            log.error(
                "Push falló incluso después de reintentar (%s) — se descarta este commit, la "
                "corrida sigue sin cortarse.",
                _detalle_error(error_reintento),
            )
            return False

    log.info("Cambios publicados en %s", config.GITHUB_BRANCH)
    return True


def _detalle_error(error: subprocess.CalledProcessError) -> str:
    return (error.stderr or error.stdout or str(error)).strip()
