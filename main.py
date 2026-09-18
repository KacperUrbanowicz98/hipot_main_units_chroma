"""Punkt wejscia silnika Hi-Pot Reconext.

Aplikacja celowo NIE uruchamia sie z ustawieniami domyslnymi. Brak
``station_config.json`` albo poprawnego profilu produktu
konczy sie czytelnym bledem startowym - lepiej nie wystartowac niz wykonac test
na przypadkowym porcie albo z niezweryfikowanym profilem.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _application_dir() -> Path:
    return (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )


def _write_startup_error(text: str) -> None:
    try:
        Path("startup_error.log").write_text(text, encoding="utf-8")
    except Exception:
        pass


REQUIRED_MODULES = {
    "serial": (
        "pyserial",
        "Bez niej aplikacja nie otworzy portu COM testera ani interlocka.",
    ),
    "tkinter": (
        "python3-tk / instalacja Pythona z opcja tcl-tk",
        "Bez niej nie da sie zbudowac interfejsu.",
    ),
}


def _check_dependencies() -> list[str]:
    """Zwraca opisy brakujacych bibliotek.

    Sprawdzamy je PRZED zbudowaniem okna. Wczesniej brak pyserial ujawnial sie
    dopiero przy otwieraniu ekranu testowego jako "No module named 'serial'" -
    komunikat, z ktorego operator nie mial jak wywnioskowac, co zainstalowac.
    W buildzie EXE PyInstaller pakuje te biblioteki, wiec dotyczy to uruchomienia
    ze zrodel.
    """
    import importlib.util

    missing = []
    for module, (package, why) in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module) is None:
            missing.append(
                f"- {module} (pakiet: {package})\n    {why}"
            )
    return missing


def main() -> int:
    app_dir = _application_dir()
    os.chdir(app_dir)

    # Musi byc przed pierwszym printem: w buildzie --windowed sys.stdout moze
    # nie istniec, a wtedy print() sam rzuca wyjatek.
    from runtime_logging import configure_runtime_logging

    runtime_log = configure_runtime_logging(app_dir)

    missing = _check_dependencies()
    if missing:
        details = "\n".join(missing)
        message = (
            "Brakuje bibliotek wymaganych do uruchomienia aplikacji:\n\n"
            f"{details}\n\n"
            "Instalacja (w tym samym Pythonie, ktorym uruchamiasz aplikacje):\n"
            f"    {sys.executable} -m pip install pyserial==3.5\n\n"
            "Uruchomienie z gotowego EXE nie wymaga instalacji - PyInstaller "
            "pakuje biblioteki do folderu aplikacji."
        )
        print("[STARTUP] " + message, file=sys.stderr)
        _write_startup_error(message)
        try:
            import tkinter as _tk
            from tkinter import messagebox as _messagebox

            probe = _tk.Tk()
            probe.withdraw()
            _messagebox.showerror("Brak wymaganych bibliotek", message,
                                  parent=probe)
            probe.destroy()
        except Exception:
            pass
        return 2

    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    app = None
    fatal_handled = False

    def fatal_tk_callback(exc_type, exc_value, exc_traceback) -> None:
        """Nieobsluzony blad Tk zatrzymuje test i zamyka aplikacje fail-safe."""
        nonlocal fatal_handled
        if fatal_handled:
            return
        fatal_handled = True

        text = "".join(traceback.format_exception(exc_type, exc_value,
                                                 exc_traceback))
        print("[FATAL UI] Nieobsluzony wyjatek callbacku Tk:", file=sys.stderr)
        print(text, file=sys.stderr)
        _write_startup_error(text)

        try:
            screen = getattr(app, "current_test_screen", None)
            if screen is not None:
                screen.shutdown()
        except Exception:
            print("[FATAL UI] Blad podczas awaryjnego STOP:", file=sys.stderr)
            traceback.print_exc()

        try:
            messagebox.showerror(
                "Krytyczny blad aplikacji",
                "Wystapil nieobsluzony blad. Aktywny test zostal zatrzymany, "
                "a dalsze testy sa zablokowane.\n\n"
                f"{exc_value}\n\nSzczegoly zapisano w logu uruchomieniowym.",
                parent=root)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    root.report_callback_exception = fatal_tk_callback

    try:
        from gui import HiPotApp
        from station_config import APP_NAME, APP_VERSION

        print(f"[RUNTIME] {APP_NAME} {APP_VERSION}")
        if runtime_log:
            print(f"[RUNTIME] Diagnostyka sesji: {runtime_log}")
        app = HiPotApp(root)
        print(f"[RUNTIME] Stanowisko: {app.config.describe()}")
        print(f"[RUNTIME] Profile: {', '.join(app.catalog.ids())}")
        if app.catalog.errors:
            print(f"[RUNTIME] UWAGA - odrzucone profile: {app.catalog.errors}")
    except Exception as exc:
        text = "".join(traceback.format_exception(type(exc), exc,
                                                 exc.__traceback__))
        print("[STARTUP] Blad uruchomienia:", file=sys.stderr)
        print(text, file=sys.stderr)
        _write_startup_error(text)
        try:
            messagebox.showerror(
                "Blad uruchomienia Reconext Hi-Pot",
                "Aplikacja nie zostala uruchomiona, aby uniknac testu z bledna "
                f"konfiguracja.\n\n{exc}\n\nSzczegoly: startup_error.log",
                parent=root)
        except Exception:
            pass
        root.destroy()
        return 1

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
