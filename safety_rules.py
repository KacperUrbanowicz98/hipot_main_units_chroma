"""Centralne reguly bezpieczenstwa silnika Hi-Pot Reconext.

Rozszerzenie regul z aplikacji Amidala 1.0.x na:

* profile WIELOKROKOWE (N krokow wykonywanych w jednym cyklu Chromy),
* maski kanalow scan boxa (Chroma 19053),
* progi obecnosci liczone per krok zamiast jednej stalej globalnej.

Uwaga o progu obecnosci
-----------------------
Amidala miala stala ``MIN_PRESENCE_CURRENT_MA = 0.500`` dobrana do testu 4 kV.
Dla SR203/SR204 (1,06 kV, Max Limit 1,0 mA) taka stala jest bez sensu - tam rolÄ™
detekcji obecnosci pelni Low Limit rzedu 0,02-0,035 mA. Prog jest wiec
parametrem KROKU, ale nadal obwarowany trzema twardymi regulami:

1. nigdy nie moze byc zerowy ani ujemny,
2. nie moze byc nizszy niz ``ABSOLUTE_MIN_PRESENCE_MA`` (ponizej tej wartosci
   pomiar tonie w szumie wlasnym miernika i przestaje odrozniac produkt od
   pustego fixture),
3. nie moze byc nizszy od Low Limit kroku ani wyzszy/rowny Max Limit.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence


class SafetyValidationError(ValueError):
    """Blad konfiguracji lub dowodow cyklu, ktory musi zablokowac test."""


_SERIAL_RE = re.compile(r"^[A-Z0-9]+$")
# Manual 19051-19054 rozdz. 6.2: RS232 obsluguje 300-19200 bodow, parity
# NONE/ODD/EVEN, flow control NONE albo SOFTWARE (XON/XOFF). Sprzetowego
# RTS/CTS ten tester nie ma - dopuszczenie go konczyloby sie martwym laczem.
_ALLOWED_BAUDRATES = {300, 600, 1200, 2400, 4800, 9600, 19200}
_ALLOWED_PARITY = {"NONE", "ODD", "EVEN"}
_ALLOWED_FLOW_CONTROL = {"NONE", "XON/XOFF"}
_ALLOWED_MODES = {"ACW"}
_CHANNEL_STATES = {"O", "H", "L"}

# Bezwzgledna dolna granica progu obecnosci. Nie da sie jej obejsc z panelu ani
# z pliku profilu - zmiana wymaga edycji tego pliku i przejscia release.
ABSOLUTE_MIN_PRESENCE_MA = 0.010

# Minimalna liczba probek "w zakresie" wymagana do uznania PASS w danym kroku.
MIN_IN_RANGE_SAMPLES = 2

# PASS wczesniejszy niz ten ulamek zaprogramowanego czasu jest odrzucany jako
# podejrzany (najczestsza przyczyna: odczyt wyniku z poprzedniego cyklu).
PASS_MIN_RUNTIME_FRACTION = 0.80

# Zapas czasu doliczany do profilu przy sprawdzaniu TEST_TIMEOUT.
TIMEOUT_MARGIN_S = 10.0
TIMEOUT_MINIMUM_S = 15.0

# Przerwa miedzy krokami po stronie Chromy - uwzgledniana w budzecie czasu.
INTER_STEP_OVERHEAD_S = 0.5


# ---------------------------------------------------------------------- #
# PRYMITYWY
# ---------------------------------------------------------------------- #
def _finite_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SafetyValidationError(f"{field}: oczekiwano liczby") from exc
    if not math.isfinite(number):
        raise SafetyValidationError(f"{field}: wartosc musi byc skonczona")
    return number


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SafetyValidationError(f"{field}: oczekiwano liczby calkowitej")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise SafetyValidationError(f"{field}: oczekiwano liczby calkowitej")
    text = str(value or "").strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise SafetyValidationError(f"{field}: oczekiwano liczby calkowitej")
    return int(text)


def validate_test_timeout(value: Any) -> int:
    timeout = _strict_int(value, "TEST_TIMEOUT")
    if not 5 <= timeout <= 3600:
        raise SafetyValidationError("TEST_TIMEOUT musi byc w zakresie 5-3600 s")
    return timeout


def validate_serial(serial: str,
                    allowed_lengths: Sequence[int] = (14, 17)) -> str:
    value = str(serial or "").strip().upper()
    lengths = tuple(int(length) for length in allowed_lengths)
    if not lengths:
        raise SafetyValidationError("Nie zdefiniowano dozwolonych dlugosci S/N")
    if len(value) not in lengths:
        expected = " lub ".join(str(length) for length in lengths)
        raise SafetyValidationError(
            f"Nieprawidlowa dlugosc S/N: {len(value)} znakow (wymagane {expected})"
        )
    if not _SERIAL_RE.fullmatch(value):
        raise SafetyValidationError("S/N moze zawierac tylko litery A-Z i cyfry 0-9")
    return value


# ---------------------------------------------------------------------- #
# TRANSPORT
# ---------------------------------------------------------------------- #
def validate_rs232_settings(port: Any, baudrate: Any, parity: Any = "NONE",
                            flow_control: Any = "NONE") -> tuple[str, int, str, str]:
    normalized_port = str(port or "").strip().upper()
    if not normalized_port:
        raise SafetyValidationError("Port COM Hi-Pot nie moze byc pusty")

    normalized_baudrate = _strict_int(baudrate, "Baudrate Hi-Pot")
    if normalized_baudrate not in _ALLOWED_BAUDRATES:
        raise SafetyValidationError(
            f"Nieobslugiwany baudrate Hi-Pot: {normalized_baudrate}"
        )

    normalized_parity = str(parity or "").strip().upper()
    if normalized_parity not in _ALLOWED_PARITY:
        raise SafetyValidationError(
            f"Nieobslugiwane parity: {normalized_parity or '<puste>'}"
        )

    normalized_flow = str(flow_control or "").strip().upper()
    if normalized_flow not in _ALLOWED_FLOW_CONTROL:
        raise SafetyValidationError(
            f"Nieobslugiwany Flow Control: {normalized_flow or '<puste>'}"
        )
    return normalized_port, normalized_baudrate, normalized_parity, normalized_flow


def validate_interlock_settings(port: Any, baudrate: Any,
                                enabled: Any) -> tuple[str, int, bool]:
    if not isinstance(enabled, bool):
        raise SafetyValidationError("INTERLOCK_ENABLED musi byc wartoscia true/false")
    if enabled is not True:
        raise SafetyValidationError(
            "INTERLOCK_ENABLED musi pozostac true w wersji produkcyjnej"
        )
    normalized_port = str(port or "").strip().upper()
    if not normalized_port:
        raise SafetyValidationError("Port COM interlocka nie moze byc pusty")
    normalized_baudrate = _strict_int(baudrate, "Baudrate interlocka")
    if normalized_baudrate not in _ALLOWED_BAUDRATES:
        raise SafetyValidationError(
            f"Nieobslugiwany baudrate interlocka: {normalized_baudrate}"
        )
    return normalized_port, normalized_baudrate, True


# ---------------------------------------------------------------------- #
# KANALY SCAN BOXA
# ---------------------------------------------------------------------- #
def validate_channel_mask(mask: Any, channel_count: int, field: str) -> str:
    """Sprawdza maske kanalow scan boxa, np. ``OOHOOOOO``.

    Akceptuje takze zapis z separatorami (``O,O,H,O,O,O,O,O``), bo w takiej
    postaci kanaly sa pokazywane w oprogramowaniu Chromy.
    """
    text = str(mask if mask is not None else "").strip().upper()
    text = re.sub(r"[\s,;]", "", text)
    if not text:
        raise SafetyValidationError(f"{field}: maska kanalow nie moze byc pusta")

    count = int(channel_count)
    if len(text) != count:
        raise SafetyValidationError(
            f"{field}: maska ma {len(text)} pozycji, scan box ma {count} kanalow"
        )

    invalid = sorted({char for char in text if char not in _CHANNEL_STATES})
    if invalid:
        raise SafetyValidationError(
            f"{field}: niedozwolone znaki maski {invalid} "
            f"(dozwolone: O=Open, H=High, L=Low)"
        )

    if "H" not in text:
        raise SafetyValidationError(
            f"{field}: maska bez zadnego kanalu H nie podaje napiecia na produkt "
            "- taki krok zawsze zmierzylby prad bliski zeru"
        )
    return text


def channel_masks_overlap(masks: Mapping[str, str]) -> list[str]:
    """Zwraca ostrzezenia o krokach uzywajacych tego samego kanalu H."""
    seen: dict[int, str] = {}
    warnings: list[str] = []
    for name, mask in masks.items():
        for index, state in enumerate(mask, start=1):
            if state != "H":
                continue
            if index in seen:
                warnings.append(
                    f"kanal {index} jest kanalem H w krokach "
                    f"'{seen[index]}' i '{name}'"
                )
            else:
                seen[index] = name
    return warnings


# ---------------------------------------------------------------------- #
# KROK TESTOWY
# ---------------------------------------------------------------------- #
def validate_step(step: Mapping[str, Any], index: int,
                  channel_count: int = 0) -> dict[str, Any]:
    """Waliduje i normalizuje pojedynczy krok profilu produktu."""
    if not isinstance(step, Mapping):
        raise SafetyValidationError(f"Krok {index}: oczekiwano obiektu JSON")

    label = f"Krok {index}"
    name = str(step.get("name", f"Step {index}")).strip()
    if not name:
        raise SafetyValidationError(f"{label}: nazwa kroku nie moze byc pusta")
    label = f"Krok {index} ({name})"

    mode = str(step.get("mode", "ACW")).strip().upper()
    if mode not in _ALLOWED_MODES:
        raise SafetyValidationError(
            f"{label}: obslugiwany jest wylacznie tryb ACW, podano {mode!r}"
        )

    voltage = _finite_float(step.get("voltage"), f"{label} / Voltage")
    limit_high = _finite_float(step.get("limit_high"), f"{label} / Max Limit")
    limit_low = _finite_float(step.get("limit_low"), f"{label} / Min Limit")
    # Brak jawnego progu obecnosci oznacza, ze rolÄ™ detekcji pelni Low Limit.
    presence_min = _finite_float(
        step.get("presence_min_current", limit_low), f"{label} / Prog obecnosci"
    )
    ramp_time = _finite_float(step.get("ramp_time", 0.0), f"{label} / Ramp Time")
    dwell = _finite_float(step.get("dwell"), f"{label} / Dwell")
    ramp_dn = _finite_float(step.get("ramp_dn", 0.0), f"{label} / Ramp Down")
    # Arc Sense i Real Current sa PRADAMI w mA (0 = wylaczone), nie flagami.
    arc_sense = _finite_float(step.get("arc_sense", 0.0), f"{label} / Arc Sense")
    real_limit = _finite_float(step.get("real_limit", 0.0), f"{label} / Real Current")
    frequency = _strict_int(step.get("frequency", 60), f"{label} / Frequency")
    continuity = str(step.get("continuity", "OFF")).strip().upper()

    errors: list[str] = []
    if not 100.0 <= voltage <= 5000.0:
        errors.append("Voltage musi byc w zakresie 100-5000 V")
    if not 0.001 <= limit_high <= 30.0:
        errors.append("Max Limit musi byc w zakresie 0.001-30.0 mA")
    if not 0.0 <= limit_low < limit_high:
        errors.append("Min Limit musi byc >= 0 i mniejszy od Max Limit")
    if presence_min < ABSOLUTE_MIN_PRESENCE_MA:
        errors.append(
            f"Prog obecnosci musi wynosic co najmniej "
            f"{ABSOLUTE_MIN_PRESENCE_MA:.3f} mA - ponizej tej wartosci pomiar "
            "nie odroznia produktu od pustego fixture"
        )
    if presence_min >= limit_high:
        errors.append("Prog obecnosci musi byc mniejszy od Max Limit")
    if presence_min < limit_low:
        errors.append("Prog obecnosci nie moze byc nizszy od Min Limit")
    if not 0.0 <= ramp_time <= 999.0:
        errors.append("Ramp Time musi byc w zakresie 0-999 s")
    if not 0.1 <= dwell <= 999.0:
        errors.append("Dwell musi byc w zakresie 0.1-999 s")
    if not 0.0 <= ramp_dn <= 999.0:
        errors.append("Ramp Down musi byc w zakresie 0-999 s")
    if not 0.0 <= arc_sense <= 15.0:
        errors.append("Arc Sense musi byc w zakresie 0-15 mA (0 = wylaczony)")
    if arc_sense and arc_sense <= limit_high:
        errors.append(
            "Arc Sense musi byc wiekszy od Max Limit albo wylaczony (0)"
        )
    if not 0.0 <= real_limit <= 30.0:
        errors.append("Real Current musi byc w zakresie 0-30 mA (0 = wylaczony)")
    if real_limit and real_limit <= presence_min:
        errors.append(
            "Real Current musi byc wiekszy od progu obecnosci albo wylaczony (0)"
        )
    if frequency not in (50, 60):
        errors.append("Frequency musi wynosic 50 lub 60 Hz")
    if continuity != "OFF":
        errors.append("Continuity musi pozostac OFF dla profilu ACW")

    if errors:
        raise SafetyValidationError(f"{label}: " + " | ".join(errors))

    normalized: dict[str, Any] = {
        "name": name,
        "mode": mode,
        "voltage": int(round(voltage)),
        "limit_high": limit_high,
        "limit_low": limit_low,
        "presence_min_current": presence_min,
        "ramp_time": ramp_time,
        "dwell": dwell,
        "ramp_dn": ramp_dn,
        "arc_sense": arc_sense,
        "real_limit": real_limit,
        "frequency": frequency,
        "continuity": continuity,
    }

    raw_channels = step.get("channels")
    if channel_count:
        if raw_channels is None:
            raise SafetyValidationError(
                f"{label}: profil dla instrumentu ze scan boxem wymaga maski "
                f"kanalow ({channel_count} pozycji)"
            )
        normalized["channels"] = validate_channel_mask(
            raw_channels, channel_count, label
        )
    elif raw_channels not in (None, ""):
        raise SafetyValidationError(
            f"{label}: podano maske kanalow, ale instrument nie ma scan boxa"
        )
    return normalized


def step_duration(step: Mapping[str, Any]) -> float:
    return (
        float(step["ramp_time"]) + float(step["dwell"]) + float(step["ramp_dn"])
    )


def profile_duration(steps: Sequence[Mapping[str, Any]]) -> float:
    """Laczny zaprogramowany czas cyklu wraz z przerwami miedzy krokami."""
    if not steps:
        return 0.0
    total = sum(step_duration(step) for step in steps)
    return total + INTER_STEP_OVERHEAD_S * (len(steps) - 1)


def validate_timeout_for_steps(timeout_value: Any,
                               steps: Sequence[Mapping[str, Any]]) -> int:
    timeout = validate_test_timeout(timeout_value)
    total = profile_duration(steps)
    required = int(math.ceil(max(total + TIMEOUT_MARGIN_S, TIMEOUT_MINIMUM_S)))
    if timeout < required:
        raise SafetyValidationError(
            f"TEST_TIMEOUT musi wynosic co najmniej {required} s dla profilu "
            f"o lacznym czasie {total:.1f} s"
        )
    return timeout


# ---------------------------------------------------------------------- #
# DOWODY WYNIKU
# ---------------------------------------------------------------------- #
def validate_step_pass_evidence(*, step_name: str, step_index: int,
                                target_voltage: float, effective_low_ma: float,
                                high_limit_ma: float, final_voltage: float,
                                final_current_ma: float, cycle_max_voltage: float,
                                in_range_samples: int,
                                overcurrent_seen: bool) -> None:
    """Odrzuca PASS pojedynczego kroku bez dowodow z biezacego cyklu."""
    label = f"Krok {step_index} ({step_name})"

    effective_voltage = max(float(final_voltage), float(cycle_max_voltage))
    if effective_voltage < float(target_voltage) * 0.90:
        raise SafetyValidationError(
            f"Odrzucono PASS - {label}: nie potwierdzono wymaganego napiecia "
            f"testowego (zmierzone max {effective_voltage:.0f} V, "
            f"wymagane >= {float(target_voltage) * 0.90:.0f} V)"
        )

    if int(in_range_samples) < MIN_IN_RANGE_SAMPLES:
        raise SafetyValidationError(
            f"Odrzucono PASS - {label}: tylko {int(in_range_samples)} pomiarow "
            f"obciazenia w prawidlowym zakresie (wymagane "
            f"{MIN_IN_RANGE_SAMPLES})"
        )

    if overcurrent_seen:
        raise SafetyValidationError(
            f"Odrzucono PASS - {label}: podczas cyklu wykryto przekroczenie "
            "Max Limit"
        )

    if not float(effective_low_ma) <= float(final_current_ma) <= float(high_limit_ma):
        raise SafetyValidationError(
            f"Odrzucono PASS - {label}: prad koncowy "
            f"{float(final_current_ma):.3f} mA jest poza zakresem "
            f"{float(effective_low_ma):.3f}-{float(high_limit_ma):.3f} mA"
        )


def validate_cycle_pass_evidence(*, result: str, terminal_status: str | None,
                                 step_results: Sequence[Mapping[str, Any]],
                                 expected_steps: int) -> None:
    """Odrzuca PASS calego cyklu, gdy ktorykolwiek krok nie ma dowodow.

    Regula produktowa uzgodniona dla SR203/SR204: PASS wymaga PASS na WSZYSTKICH
    krokach. Brak wyniku kroku jest traktowany jak jego brak zaliczenia, a nie
    jak "krok pominiety".
    """
    if str(result).upper() != "PASS":
        return

    if terminal_status == "FAIL":
        raise SafetyValidationError(
            "Odrzucono PASS - status biezacego cyklu wskazal FAIL"
        )

    if len(step_results) != int(expected_steps):
        raise SafetyValidationError(
            f"Odrzucono PASS - otrzymano wyniki {len(step_results)} z "
            f"{int(expected_steps)} zaprogramowanych krokow"
        )

    failed = [
        str(entry.get("name", entry.get("index", "?")))
        for entry in step_results
        if str(entry.get("result", "")).upper() != "PASS"
    ]
    if failed:
        raise SafetyValidationError(
            "Odrzucono PASS - kroki bez zaliczenia: " + ", ".join(failed)
        )
