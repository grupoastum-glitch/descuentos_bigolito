"""Lock entre corridas del scraper, commiteado en el mismo repo de git que ya usa
git_publish.py — evita que dos corridas (ej. un "Run Now" manual pisando al cron de la hora,
o dos manuales seguidos) scrapeen y publiquen al mismo tiempo.

El lock vive como scraper/run_lock.json en el repo publicado, con un run_id y timestamp. Se
vence solo a los config.RUN_LOCK_TIMEOUT_SEGUNDOS para no quedar trabado para siempre si una
corrida cuelga o crashea sin liberarlo.
"""
from __future__ import annotations

import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config
import git_publish

log = logging.getLogger("scraper.run_lock")


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True, timeout=config.GIT_TIMEOUT_SEGUNDOS
    )


def _parsear_lock(contenido: str | None) -> dict | None:
    if not contenido:
        return None
    try:
        return json.loads(contenido)
    except json.JSONDecodeError:
        return None


def _lock_vigente(lock: dict | None) -> bool:
    if not lock:
        return False
    try:
        iniciado_en = datetime.fromisoformat(lock["iniciado_en"])
    except (KeyError, ValueError):
        return False
    edad_segundos = (datetime.now(timezone.utc) - iniciado_en).total_seconds()
    return edad_segundos < config.RUN_LOCK_TIMEOUT_SEGUNDOS


def _leer_lock_local(repo_dir: Path) -> dict | None:
    ruta = repo_dir / config.RUTA_RUN_LOCK
    if not ruta.exists():
        return None
    return _parsear_lock(ruta.read_text(encoding="utf-8"))


def _leer_lock_remoto(repo_dir: Path) -> dict | None:
    """Lee el lock tal como quedó publicado en origin/<branch>, sin rebasar el commit propio
    encima — necesario para decidir honestamente quién ganó una carrera real, en vez de que el
    que pierde el push termine "ganando" el archivo al reescribirlo por encima del otro."""
    try:
        resultado = _run(
            ["git", "show", f"origin/{config.GITHUB_BRANCH}:{config.RUTA_RUN_LOCK}"], cwd=str(repo_dir)
        )
    except subprocess.CalledProcessError:
        return None
    return _parsear_lock(resultado.stdout)


def _escribir_lock(repo_dir: Path, run_id: str) -> None:
    ruta = repo_dir / config.RUTA_RUN_LOCK
    contenido = {"run_id": run_id, "iniciado_en": datetime.now(timezone.utc).isoformat()}
    ruta.write_text(json.dumps(contenido), encoding="utf-8")


def adquirir_lock(repo_dir: Path) -> str | None:
    """Intenta tomar el lock de corrida sobre el clon ya hecho por git_publish.clonar_repo().
    Devuelve el run_id propio si lo consiguió, o None si ya hay otra corrida activa — en ese
    caso el caller debe abortar sin scrapear ni publicar nada."""
    lock_actual = _leer_lock_local(repo_dir)
    if _lock_vigente(lock_actual):
        log.error(
            "Ya hay una corrida activa (run_id=%s, iniciada %s) — se aborta esta corrida.",
            lock_actual["run_id"], lock_actual["iniciado_en"],
        )
        return None
    if lock_actual:
        log.warning(
            "Lock vencido (run_id=%s, iniciada %s) — se asume que la corrida anterior colgó o "
            "crasheó sin liberarlo, se pisa y se sigue.",
            lock_actual["run_id"], lock_actual["iniciado_en"],
        )

    run_id = str(uuid.uuid4())
    _escribir_lock(repo_dir, run_id)
    _run(["git", "add", config.RUTA_RUN_LOCK], cwd=str(repo_dir))
    _run([
        "git",
        "-c", f"user.name={config.GIT_AUTHOR_NAME}",
        "-c", f"user.email={config.GIT_AUTHOR_EMAIL}",
        "commit", "-m", f"chore(lock): inicio de corrida {run_id}",
    ], cwd=str(repo_dir))

    try:
        _run(["git", "push", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
        return run_id
    except subprocess.CalledProcessError:
        log.warning("Push del lock rechazado, probablemente otra corrida arrancó a la vez — se revisa quién ganó")

    _run(["git", "fetch", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
    lock_remoto = _leer_lock_remoto(repo_dir)
    if _lock_vigente(lock_remoto) and lock_remoto["run_id"] != run_id:
        log.error(
            "Se perdió la carrera por el lock contra otra corrida (run_id=%s) — se aborta esta corrida.",
            lock_remoto["run_id"],
        )
        return None

    # el remoto también está vencido/ausente (ej. dos corridas arrancaron casi juntas y ambas
    # vieron el mismo estado vacío antes de pushear): nos rebasamos encima y reintentamos una
    # sola vez, mismo límite que usa git_publish.publicar_cambios.
    try:
        _run(["git", "pull", "--rebase", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
        _run(["git", "push", "origin", config.GITHUB_BRANCH], cwd=str(repo_dir))
    except subprocess.CalledProcessError:
        log.error("No se pudo confirmar el lock tras reintentar — se aborta esta corrida por las dudas.")
        return None

    return run_id


def liberar_lock(repo_dir: Path, run_id: str) -> None:
    """Libera el lock si sigue siendo el nuestro. Si ya no coincide (otra corrida lo pisó tras
    vencerse), no toca nada — evita borrarle el lock a una corrida ajena."""
    lock_actual = _leer_lock_local(repo_dir)
    if not lock_actual or lock_actual.get("run_id") != run_id:
        log.warning("El lock ya no es de esta corrida (run_id=%s) — no se libera nada.", run_id)
        return

    (repo_dir / config.RUTA_RUN_LOCK).unlink()
    try:
        git_publish.publicar_cambios(repo_dir, [config.RUTA_RUN_LOCK], f"chore(lock): fin de corrida {run_id}")
    except subprocess.CalledProcessError:
        log.warning(
            "No se pudo liberar el lock (run_id=%s) — se autolibera solo en %d min.",
            run_id, config.RUN_LOCK_TIMEOUT_SEGUNDOS // 60,
        )
