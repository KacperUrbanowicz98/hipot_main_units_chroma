"""Raporty testow w formacie zgodnym z oprogramowaniem Chroma Hipot Tester.

Rozszerzenie raportu z Amidali o sekcje per krok. Dla profilu jednokrokowego
plik jest strukturalnie identyczny z tym, ktory generuje aplikacja 1.0.x - to
warunek, zeby istniejacy watcher zbierajacy raporty nie wymagal zmian.

Publikacja jest atomowa: obserwator katalogu widzi dopiero kompletny plik.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from typing import Any, Mapping, Sequence

_FALLBACK_DIR = "logs_pending"

# Kody wyniku wg manuala 19051/19052/19053/19054 s. 5-17.
# Wersja w aplikacji Amidala 1.0.x miala te tabele w wiekszosci bledna:
# poprawne byly tylko 17, 18 i 116, a np. ARC/ADV/ADI mialy wartosci
# z zupelnie innej rodziny urzadzen. Skutkiem byly raporty FAIL z mylacym
# opisem przyczyny.
ERROR_CODES = {
    # Stany wspolne
    "112": "Stop",
    "113": "User Stop",
    "114": "Can Not Test",
    "115": "Testing",
    "116": "Pass",
    # AC MODE
    "17": "AC High Fail",
    "18": "AC Low Fail",
    "19": "AC Arc Fail",
    "20": "AC I/O Fail",
    "22": "AC ADV Over",
    "23": "AC ADI Over",
    "26": "AC Real Current High Fail",
    "27": "AC I/O-F Fail",
    # DC MODE
    "33": "DC High Fail",
    "34": "DC Low Fail",
    "35": "DC Arc Fail",
    "36": "DC I/O Fail",
    "37": "DC Check Low Fail",
    "38": "DC ADV Over",
    "39": "DC ADI Over",
    "43": "DC I/O-F Fail",
    # IR MODE
    "49": "IR High Fail",
    "50": "IR Low Fail",
    "52": "IR I/O Fail",
    "54": "IR ADV Over",
    "55": "IR ADI Over",
    # Wspolne dla wszystkich trybow
    "120": "Ground Continuity Fail",
    "121": "Tripped",
}


def describe_error_code(error_code: Any) -> str:
    if not error_code:
        return ""
    return ERROR_CODES.get(str(error_code).strip(), f"Error code {error_code}")


def _application_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_fallback_log_dir() -> str:
    return os.path.join(_application_dir(), _FALLBACK_DIR)


def _cap(result: Any) -> str:
    return "Pass" if str(result).upper() == "PASS" else "Fail"


def _mode_label(mode: Any) -> str:
    """Nazwa trybu tak, jak zapisuje ja oprogramowanie Chromy."""
    return {"ACW": "WVAC", "DCW": "WVDC", "IR": "IR"}.get(
        str(mode or "ACW").strip().upper(), "WVAC")


def build_report_lines(*, instrument_model: str, program: str, serial: str,
                       overall_result: str,
                       steps: Sequence[Mapping[str, Any]],
                       profile_steps: Sequence[Mapping[str, Any]],
                       effective_low_ma: Sequence[float],
                       now: datetime) -> list[str]:
    """Buduje tresc raportu 1:1 z formatem oprogramowania Chroma Hipot Tester.

    Wzorzec: raport wygenerowany na stanowisku SR203/SR204 (D21022AD023966.txt).
    Kolejnosc i separatory (tabulatory) sa odwzorowane doslownie, zeby istniejacy
    watcher zbierajacy raporty nie wymagal zmian.

    Uwagi do pol:
    * ``ARC Result`` - Chroma nie udostepnia wyniku detekcji luku przez SCPI,
      wiec zapisujemy ``---`` tak jak jej wlasne oprogramowanie przy ARC = OFF.
    * ``Error Description`` wystepuje RAZ, na koncu pliku - opisuje pierwszy
      krok bez zaliczenia.
    """
    lines = [
        f"Chroma {instrument_model} Test report",
        "",
        f"Program:\t{program}",
        f"S/N:\t\t{serial}",
        f"TIME:\t\t{now.strftime('%Y/%m/%d %H:%M:%S')}",
        f"Total result:\t{_cap(overall_result)}",
    ]

    first_error_code = ""
    for position, entry in enumerate(steps):
        profile_step = (
            profile_steps[position] if position < len(profile_steps) else {}
        )
        low = (
            effective_low_ma[position] if position < len(effective_low_ma) else 0.0
        )
        error_code = str(entry.get("error_code", "") or "")
        if error_code and not first_error_code:
            first_error_code = error_code

        lines.extend([
            "",
            f"STEP:\t\t{entry.get('index', position + 1)}",
            f"MODE:\t\t{_mode_label(profile_step.get('mode'))}",
            f"EXT Name:\t{entry.get('name', '')}",
            f"Vtm:\t\t{float(entry.get('output_voltage', 0.0)) / 1000.0:.3f}\tKV",
            f"Im:\t\t{float(entry.get('measured_current', 0.0)):.3f}\tmA",
            f"Low:\t\t{float(low):.3f}\tmA",
            f"High:\t\t{float(profile_step.get('limit_high', 0.0)):.3f}\tmA",
            "ARC Result:\t---",
            f"Result:\t\t{_cap(entry.get('result'))}",
            f"Error Code:\t{error_code}",
        ])

    lines.extend(["", f"Error Description: {describe_error_code(first_error_code)}"])
    return lines


def _write_report(directory: str, filename: str, lines: Sequence[str]) -> str:
    os.makedirs(directory, exist_ok=True)
    filepath = os.path.join(directory, filename)
    handle_fd, temp_path = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("\r\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, filepath)
        return filepath
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def save_report(*, instrument_model: str, program: str, serial: str,
                overall_result: str,
                steps: Sequence[Mapping[str, Any]],
                profile_steps: Sequence[Mapping[str, Any]],
                effective_low_ma: Sequence[float],
                log_dir: str) -> str:
    """Zapisuje raport; przy niedostepnej sciezce podstawowej uzywa awaryjnej.

    Nazwa pliku to SAM numer seryjny - tak jak w oprogramowaniu Chromy.
    Powtorny test tej samej sztuki NADPISUJE poprzedni plik.
    """
    now = datetime.now()
    filename = f"{serial}.txt"
    lines = build_report_lines(
        instrument_model=instrument_model,
        program=program,
        serial=serial,
        overall_result=overall_result,
        steps=steps,
        profile_steps=profile_steps,
        effective_low_ma=effective_low_ma,
        now=now,
    )

    try:
        filepath = _write_report(log_dir, filename, lines)
        print(f"[LOG] Zapisano: {filepath}")
        return filepath
    except OSError as primary_error:
        fallback_dir = get_fallback_log_dir()
        filepath = _write_report(fallback_dir, filename, lines)
        print(f"[LOG] Sciezka podstawowa niedostepna: {log_dir} ({primary_error})")
        print(f"[LOG] Zapis awaryjny: {filepath}")
        return filepath
