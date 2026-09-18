# security.py
"""Kontrola dostepu do panelu konfiguracji i dziennik audytowy zmian.

Wersja 1.0.5 porownywala haslo z jawnym literalem w ``gui.py``. Aplikacja jest
budowana bez podpisu cyfrowego, wiec taki literal jest odzyskiwalny z folderu
``_internal`` trywialnym ``strings``. Modul zastepuje go rekordem PBKDF2
przechowywanym w ``station_config.json`` oraz dodaje:

* limit prob i czasowa blokada po serii bledow,
* porownanie w czasie stalym (``hmac.compare_digest``),
* dziennik audytowy: kto wszedl do panelu i jaka wartosc zmienil.

Haslo standardowe stanowiska jest ustalone przez wlasciciela aplikacji i opisane
w dokumentacji wdrozeniowej. Skrot dostarczany jest w ``station_config.json``;
w plikach aplikacji hasla NIE ma w postaci jawnej, wiec nie da sie go odczytac
ze zbudowanego EXE ani z folderu ``_internal``.

Ograniczenie, ktore trzeba znac: skrot lezy w pliku obok EXE. Chroni przed
ODCZYTANIEM hasla z binarki, nie przed jego PODMIANA przez osobe majaca prawo
zapisu do folderu stanowiska. Uprawnienia NTFS ustawione przez IT (zapis tylko
dla technologa) pozostaja warunkiem koniecznym.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from safety_rules import SafetyValidationError

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16

MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 30.0
MIN_PASSWORD_LENGTH = 8

_AUDIT_DIR_NAME = "app_runtime_logs"
_AUDIT_FILE_NAME = "config_audit.log"
_AUDIT_LOCK = threading.Lock()
# Ostatni nieudany zapis do dziennika audytowego (S8).
_AUDIT_FAILED: Optional[str] = None


# ---------------------------------------------------------------------- #
# HASLO
# ---------------------------------------------------------------------- #
def hash_password(password: str) -> dict[str, Any]:
    """Tworzy rekord hasla gotowy do zapisu w ``station_config.json``.

    Aplikacja nie wywoluje tej funkcji w czasie pracy - haslo panelu jest
    stale, a zakladka do jego zmiany zostala usunieta. Funkcja zostaje jako
    JEDYNY sposob wygenerowania rekordu hasla: gdyby zostala skasowana,
    nie byloby czym zastapic wpisu ADMIN_PASSWORD po jego utracie.
    Uzywana przez release_selftest.py i przy przygotowaniu konfiguracji
    stanowiska.
    """
    text = str(password or "")
    if len(text) < MIN_PASSWORD_LENGTH:
        raise SafetyValidationError(
            f"Haslo musi miec co najmniej {MIN_PASSWORD_LENGTH} znakow"
        )
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), salt, _ITERATIONS)
    return {
        "algo": _ALGORITHM,
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def validate_password_record(record: Any) -> Optional[dict[str, Any]]:
    """Waliduje rekord z JSON. ``None`` oznacza brak hasla (tryb migracji)."""
    if record is None:
        return None
    if not isinstance(record, Mapping):
        raise SafetyValidationError("ADMIN_PASSWORD musi byc obiektem JSON")

    algo = str(record.get("algo", "")).strip()
    if algo != _ALGORITHM:
        raise SafetyValidationError(f"Nieobslugiwany algorytm hasla: {algo!r}")

    try:
        iterations = int(record["iterations"])
        salt = bytes.fromhex(str(record["salt"]))
        digest = bytes.fromhex(str(record["hash"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SafetyValidationError(f"Uszkodzony rekord ADMIN_PASSWORD: {exc}") from exc

    if iterations < 100_000:
        raise SafetyValidationError(
            "ADMIN_PASSWORD: liczba iteracji ponizej wymaganego minimum 100000"
        )
    if len(salt) < 8 or len(digest) != 32:
        raise SafetyValidationError("ADMIN_PASSWORD: nieprawidlowa dlugosc soli lub hasha")

    return {
        "algo": algo,
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def verify_password(password: str, record: Any) -> bool:
    """Sprawdza haslo panelu inzynieryjnego wzgledem rekordu z konfiguracji.

    Brak rekordu NIE oznacza dostepu awaryjnego - bez niego panel jest
    zamkniety, bo inaczej skasowanie jednego pola w JSON otwieraloby
    konfiguracje testu.
    """
    text = str(password or "")

    if record is None:
        return False

    normalized = validate_password_record(record)
    assert normalized is not None
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        text.encode("utf-8"),
        bytes.fromhex(normalized["salt"]),
        normalized["iterations"],
    )
    return hmac.compare_digest(candidate, bytes.fromhex(normalized["hash"]))


class AccessGate:
    """Licznik prob z czasowa blokada. Jedna instancja na proces."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS,
                 lockout_seconds: float = LOCKOUT_SECONDS):
        self.max_attempts = int(max_attempts)
        self.lockout_seconds = float(lockout_seconds)
        self._failures = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def seconds_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._locked_until - time.monotonic())

    def is_locked(self) -> bool:
        return self.seconds_remaining() > 0.0

    def register_failure(self) -> float:
        """Zwraca liczbe sekund blokady (0.0 gdy jeszcze nie zablokowano)."""
        with self._lock:
            self._failures += 1
            if self._failures >= self.max_attempts:
                self._failures = 0
                self._locked_until = time.monotonic() + self.lockout_seconds
                return self.lockout_seconds
            return 0.0

    def attempts_left(self) -> int:
        with self._lock:
            return max(0, self.max_attempts - self._failures)

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._locked_until = 0.0


# ---------------------------------------------------------------------- #
# DZIENNIK AUDYTOWY
# ---------------------------------------------------------------------- #
def _application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def audit_log_path() -> Path:
    return _application_dir() / _AUDIT_DIR_NAME / _AUDIT_FILE_NAME


def audit(event: str, detail: str = "") -> None:
    """Dopisuje wpis audytowy. Awaria zapisu nie moze przerwac aplikacji."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        user = os.getlogin()
    except OSError:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "?"
    line = f"{timestamp}\t{host}\t{user}\t{event}\t{detail}\n"

    try:
        path = audit_log_path()
        with _AUDIT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError as exc:
        # S8: dziennik audytowy jest jedynym zapisem tego, kto zmienil
        # nastawy. Cichy brak zapisu (np. plik ustawiony tylko-do-odczytu)
        # wyciszal audyt na dobre. Flaga jest odczytywana przez aplikacje.
        global _AUDIT_FAILED
        _AUDIT_FAILED = f"{exc}"
        print(f"[AUDIT] NIE ZAPISANO WPISU AUDYTOWEGO: {exc}")
    print(f"[AUDIT] {event} {detail}".rstrip())


def audit_failure() -> Optional[str]:
    """Komunikat ostatniego nieudanego zapisu do dziennika albo ``None``."""
    return _AUDIT_FAILED


def audit_changes(section: str, before: Mapping[str, Any],
                  after: Mapping[str, Any]) -> None:
    """Loguje wylacznie pola, ktore faktycznie sie zmienily."""
    changed = [
        f"{key}: {before.get(key)!r} -> {after[key]!r}"
        for key in after
        if before.get(key) != after[key]
    ]
    if changed:
        audit(f"ZMIANA/{section}", "; ".join(changed))
