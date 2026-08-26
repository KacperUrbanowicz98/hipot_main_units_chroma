# runtime_logging.py
"""Niezawodne logowanie konsoli w wersji zbudowanej przez PyInstaller.

W aplikacji ``--windowed`` Windows moze nie udostepniac ``sys.stdout`` ani
``sys.stderr``. Ten modul podstawia bezpieczne strumienie zapisujace diagnostyke
do pliku dziennego obok aplikacji. W uruchomieniu ze zrodla zachowuje rowniez
normalny wydruk do konsoli.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TextIO


_LOG_DIR_NAME = "app_runtime_logs"
_RETENTION_DAYS = 30
_LOCK = threading.RLock()
_STATE: Optional["_RuntimeLogState"] = None


class _TeeTextStream:
    """Tekstowy strumien zapisujacy do pliku i opcjonalnie do konsoli."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logfile: TextIO, mirror: Optional[TextIO],
                 tag: str = "OUT"):
        self._logfile = logfile
        self._mirror = mirror
        self._tag = tag
        self._at_line_start = True

    def _stamp(self, text: str) -> str:
        """Dokleja czas i kanal na poczatku kazdej linii w pliku dziennika.

        Bez tego korelacja "co dokladnie dzialo sie w chwili FAIL-a" wymagala
        recznego zestawiania logu z raportem testu.
        """
        prefix = f"[{datetime.now():%H:%M:%S.%f}][{self._tag}] "
        out = []
        for index, part in enumerate(text.split("\n")):
            if index:
                out.append("\n")
                self._at_line_start = True
            if part and self._at_line_start:
                out.append(prefix[:-1] + " ")
                self._at_line_start = False
            out.append(part)
        return "".join(out)

    def write(self, text: str) -> int:
        if not text:
            return 0
        with _LOCK:
            self._logfile.write(self._stamp(text))
            self._logfile.flush()
            if self._mirror is not None:
                try:
                    self._mirror.write(text)
                    self._mirror.flush()
                except Exception:
                    # Awaria konsoli nie moze przerwac pracy aplikacji.
                    pass
        return len(text)

    def flush(self) -> None:
        with _LOCK:
            try:
                self._logfile.flush()
            except Exception:
                pass
            if self._mirror is not None:
                try:
                    self._mirror.flush()
                except Exception:
                    pass

    def isatty(self) -> bool:
        if self._mirror is None:
            return False
        try:
            return bool(self._mirror.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        if self._mirror is not None:
            try:
                return self._mirror.fileno()
            except Exception:
                pass
        return self._logfile.fileno()


class _RuntimeLogState:
    def __init__(
        self,
        old_stdout: Optional[TextIO],
        old_stderr: Optional[TextIO],
        logfile: TextIO,
        log_path: Path,
    ):
        self.old_stdout = old_stdout
        self.old_stderr = old_stderr
        self.logfile = logfile
        self.log_path = log_path
        self.stdout_stream = _TeeTextStream(logfile, old_stdout, tag="OUT")
        self.stderr_stream = _TeeTextStream(logfile, old_stderr, tag="ERR")
        self.restored = False


def _remove_old_logs(log_dir: Path) -> None:
    cutoff = datetime.now() - timedelta(days=_RETENTION_DAYS)
    for path in log_dir.glob("hipot_*.log"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if modified < cutoff:
                path.unlink()
        except OSError:
            pass


def _install_devnull_streams() -> None:
    """Chroni ``print`` przed awaria, nawet gdy katalog logow jest niedostepny."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def configure_runtime_logging(app_dir: str | os.PathLike[str]) -> Optional[Path]:
    """Konfiguruje dzienny log uruchomieniowy i zwraca jego sciezke.

    Funkcja jest idempotentna. W razie problemu z utworzeniem logu zabezpiecza
    ``stdout``/``stderr`` strumieniem ``os.devnull`` zamiast przerywac aplikacje.
    """
    global _STATE

    with _LOCK:
        if _STATE is not None and not _STATE.restored:
            return _STATE.log_path

        log_dir = Path(app_dir).resolve() / _LOG_DIR_NAME
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            _remove_old_logs(log_dir)
            log_path = log_dir / f"hipot_{datetime.now():%Y%m%d}.log"
            logfile = log_path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            _install_devnull_streams()
            return None

        state = _RuntimeLogState(sys.stdout, sys.stderr, logfile, log_path)
        _STATE = state
        sys.stdout = state.stdout_stream
        sys.stderr = state.stderr_stream
        atexit.register(restore_runtime_logging)

        separator = "=" * 72
        print(f"\n{separator}")
        print(f"[RUNTIME] Start sesji: {datetime.now().astimezone().isoformat(timespec='seconds')}")
        print(f"[RUNTIME] PID: {os.getpid()}")
        print(f"[RUNTIME] Python: {sys.version.split()[0]} | frozen={getattr(sys, 'frozen', False)}")
        print(f"[RUNTIME] Log: {log_path}")
        print(separator)
        return log_path


def restore_runtime_logging() -> None:
    """Przywraca pierwotne strumienie i zamyka log. Bezpieczna wielokrotnie."""
    global _STATE

    with _LOCK:
        state = _STATE
        if state is None or state.restored:
            return
        state.restored = True

        try:
            state.stdout_stream.flush()
            state.stderr_stream.flush()
        except Exception:
            pass

        if sys.stdout is state.stdout_stream:
            sys.stdout = state.old_stdout
        if sys.stderr is state.stderr_stream:
            sys.stderr = state.old_stderr

        try:
            state.logfile.flush()
            state.logfile.close()
        except Exception:
            pass
        _STATE = None
