"""Dialekty SCPI dla testerow Chroma serii 190xx.

Wszystkie ciagi komend sa w JEDNYM miejscu, zeby dodanie kolejnego modelu
testera nie wymagalo dotykania logiki sterowania.

STAN WERYFIKACJI
----------------
Wszystkie komendy sa zgodne z:
    "HIPOT Tester 19051/19052/19053/19054 User's Manual",
    wersja 2.1, grudzien 2009, P/N A11 000893, rozdzial 5 (GPIB/SCPI).

Odwolania do stron manuala sa podane przy komendach, ktore odbiegaja od
intuicji - zwlaszcza przy kanalach scan boxa, ktore uzywaja LISTY kanalow
``(@(1,3))``, a nie osobnej komendy na kanal.

Sonda ``probe_dialect`` zostaje mimo potwierdzenia skladni: firmware bywa
starszy niz manual, a odrzucona komenda wymagana do konfiguracji musi
zablokowac test, zamiast po cichu wykonac go z bledna maska kanalow.
"""

from __future__ import annotations

from typing import Any, Mapping

from safety_rules import SafetyValidationError

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"


# Komendy wspolne dla calej serii 190xx (potwierdzone na 19052 fw 5.14).
_COMMON: dict[str, str] = {
    "idn": "*IDN?",
    "error": "SYST:ERR?",
    "keylock_on": "SYST:KLOC ON",
    "keylock_off": "SYST:KLOC OFF",
    "keylock_query": "SYST:KLOC?",
    "step_count_query": "SAFEty:SNUMber?",
    "step_delete": "SAFEty:STEP{step}:DELete",
    "acw_level": "SAFEty:STEP{step}:AC:LEVel {value}",
    "acw_limit_high": "SAFEty:STEP{step}:AC:LIMit:HIGH {value}",
    "acw_limit_low": "SAFEty:STEP{step}:AC:LIMit:LOW {value}",
    "acw_time_test": "SAFEty:STEP{step}:AC:TIME:TEST {value}",
    "acw_time_ramp": "SAFEty:STEP{step}:AC:TIME:RAMP {value}",
    "acw_time_fall": "SAFEty:STEP{step}:AC:TIME:FALL {value}",
    # Manual s. 5-20: ARC lezy pod AC:LIMit:ARC, a NIE pod AC:ARC. Aplikacja
    # Amidala uzywala blednej sciezki i dostawala -113, przez co Arc Sense byl
    # ustawiany recznie na panelu.
    "acw_limit_arc": "SAFEty:STEP{step}:AC:LIMit:ARC {value}",
    "acw_limit_arc_query": "SAFEty:STEP{step}:AC:LIMit:ARC?",
    # Manual s. 5-20: limit pradu rzeczywistego (kolumna "Real Current" w
    # oprogramowaniu Chromy). 0 = wylaczony.
    "acw_limit_real": "SAFEty:STEP{step}:AC:LIMit:REAL {value}",
    "acw_limit_real_query": "SAFEty:STEP{step}:AC:LIMit:REAL?",
    # Manual s. 5-18: jedno zapytanie zwraca WSZYSTKIE nastawy kroku wraz z
    # obiema listami kanalow. Zastepuje 8 osobnych zapytan przy odczycie
    # zwrotnym - istotne, bo RS232 konczy sie na 19200 bodach.
    "step_settings_query": "SAFEty:STEP{step}:SET?",
    "step_mode_query": "SAFEty:STEP{step}:MODE?",
    # Manual s. 5-32: czestotliwosc jest ustawieniem GLOBALNYM (PRESet),
    # nie parametrem kroku.
    "preset_frequency": "SAFEty:PRESet:AC:FREQuency {value}",
    "preset_frequency_query": "SAFEty:PRESet:AC:FREQuency?",
    "acw_level_query": "SAFEty:STEP{step}:AC?",
    "acw_limit_high_query": "SAFEty:STEP{step}:AC:LIMit?",
    "acw_limit_low_query": "SAFEty:STEP{step}:AC:LIMit:LOW?",
    "acw_time_test_query": "SAFEty:STEP{step}:AC:TIME?",
    "acw_time_ramp_query": "SAFEty:STEP{step}:AC:TIME:RAMP?",
    "acw_time_fall_query": "SAFEty:STEP{step}:AC:TIME:FALL?",
    "start": "SAFEty:STARt",
    "stop": "SAFEty:STOP",
    "status_query": "SAFEty:STATus?",
    "fetch": "SAFEty:FETCh? STEP,MODE,OMET,MMET,RMET",
    "result_last_judgment": "SAFEty:RESult:LAST:JUDG?",
    "result_last_voltage": "SAFEty:RESult:LAST:OMET?",
    "result_last_current": "SAFEty:RESult:LAST:MMET?",
    "result_last_real_current": "SAFEty:RESult:LAST:RMET?",
}

# Wyniki per krok - potrzebne przy profilach wielokrokowych.
_PER_STEP_RESULTS: dict[str, str] = {
    "result_step_judgment": "SAFEty:RESult:STEP{step}:JUDG?",
    "result_step_voltage": "SAFEty:RESult:STEP{step}:OMET?",
    "result_step_current": "SAFEty:RESult:STEP{step}:MMET?",
    "result_step_real_current": "SAFEty:RESult:STEP{step}:RMET?",
}

# Scan box 19053/19054. Manual s. 5-21/5-22.
# Kanaly ustawia sie LISTAMI, osobno dla strony HIGH i strony LOW/RTN:
#     SAFE:STEP1:AC:CHAN (@(1,3))        -> kanaly 1 i 3 jako HIGH
#     SAFE:STEP1:AC:CHAN (@(0))          -> zadnego kanalu HIGH
#     SAFE:STEP1:AC:CHAN:LOW (@(2,4))    -> kanaly 2 i 4 jako LOW
#     SAFE:STEP1:AC:CHAN?                -> "(@(1,3))"
_SCAN_BOX: dict[str, str] = {
    "channel_high": "SAFEty:STEP{step}:AC:CHANnel {channels}",
    "channel_high_query": "SAFEty:STEP{step}:AC:CHANnel?",
    "channel_low": "SAFEty:STEP{step}:AC:CHANnel:LOW {channels}",
    "channel_low_query": "SAFEty:STEP{step}:AC:CHANnel:LOW?",
}


# Manual s. 5-17: kody, ktore NIE sa werdyktem o produkcie.
#   112 STOP | 113 USER STOP | 114 CAN NOT TEST | 115 TESTING
# Wersja sprzed lektury manuala pomijala 113 i 114, wiec przerwanie testu
# przez operatora mogloby zostac zapisane jako FAIL wyrobu.
NON_TERMINAL_JUDGMENTS = [112, 113, 114, 115]
PASS_JUDGMENT = 116

# Manual s. 5-19 (rozdz. 6.2): RS232 obsluguje wylacznie te predkosci.
SUPPORTED_BAUDRATES = (300, 600, 1200, 2400, 4800, 9600, 19200)
# Manual s. 5-19: FLOW CTRL. = NONE / SOFTWARE. Sprzetowy RTS/CTS nie istnieje.
SUPPORTED_FLOW_CONTROL = ("NONE", "XON/XOFF")


def _model(name: str, description: str, *, channels: int) -> dict[str, Any]:
    scan = channels > 0
    commands = {**_COMMON, **_PER_STEP_RESULTS}
    if scan:
        commands.update(_SCAN_BOX)
    return {
        "model": name,
        "description": description,
        "has_scan_box": scan,
        "channel_count": channels,
        # Manual s. 5-18: <n> kroku miesci sie w zakresie 1-99.
        "max_steps": 99,
        "commands": commands,
        "verification": {key: VERIFIED for key in commands},
        "non_terminal_judgments": list(NON_TERMINAL_JUDGMENTS),
        "pass_judgment": PASS_JUDGMENT,
    }


DIALECTS: dict[str, dict[str, Any]] = {
    "19051": _model("19051", "Chroma 19051 - tester bez scan boxa", channels=0),
    "19052": _model("19052", "Chroma 19052 - tester bez scan boxa", channels=0),
    "19053": _model("19053", "Chroma 19053 - scan box 8-kanalowy", channels=8),
    "19054": _model("19054", "Chroma 19054 - scan box 4-kanalowy", channels=4),
}

# Komendy, bez ktorych nie wolno rozpoczac testu.
REQUIRED_COMMANDS = (
    "idn",
    "error",
    "keylock_on",
    "keylock_query",
    "step_count_query",
    "step_delete",
    "acw_level",
    "acw_limit_high",
    "acw_limit_low",
    "acw_time_test",
    "acw_time_ramp",
    "acw_time_fall",
    "acw_level_query",
    "acw_limit_high_query",
    "acw_limit_low_query",
    "acw_time_test_query",
    "acw_time_ramp_query",
    "acw_time_fall_query",
    "start",
    "stop",
    "status_query",
    "fetch",
    "result_last_judgment",
    "result_last_voltage",
    "result_last_current",
    "step_settings_query",
)

# Komendy wymagane dodatkowo, gdy profil korzysta ze scan boxa.
REQUIRED_SCAN_COMMANDS = (
    "channel_high",
    "channel_high_query",
    "channel_low",
    "channel_low_query",
)


def encode_channel_list(channels) -> str:
    """Zamienia numery kanalow na skladnie listy Chromy.

    Manual s. 5-21: ``(@(1,3))`` to kanaly 1 i 3, ``(@(0))`` oznacza brak
    kanalow po danej stronie.
    """
    numbers = sorted({int(channel) for channel in channels})
    if not numbers:
        return "(@(0))"
    if any(number < 1 for number in numbers):
        raise SafetyValidationError(
            f"Numery kanalow musza byc dodatnie: {numbers}"
        )
    return "(@(" + ",".join(str(number) for number in numbers) + "))"


def decode_channel_list(answer: str) -> set[int]:
    """Parsuje odpowiedz ``(@(1,3))`` na zbior numerow kanalow."""
    text = str(answer or "").strip().upper()
    inner = text.replace("(@", "").replace("(", "").replace(")", "").strip()
    if not inner:
        raise SafetyValidationError(f"Nieczytelna lista kanalow: {answer!r}")

    channels: set[int] = set()
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            number = int(float(part))
        except (TypeError, ValueError) as exc:
            raise SafetyValidationError(
                f"Nieczytelny numer kanalu {part!r} w odpowiedzi {answer!r}"
            ) from exc
        # 0 to znacznik "brak kanalow", a nie numer kanalu.
        if number:
            channels.add(number)
    return channels


def mask_to_channel_lists(mask: str) -> tuple[list[int], list[int]]:
    """Rozklada maske profilu ``OOHOOOOO`` na listy HIGH i LOW."""
    high = [index for index, state in enumerate(mask, start=1) if state == "H"]
    low = [index for index, state in enumerate(mask, start=1) if state == "L"]
    return high, low


class Dialect:
    """Zestaw komend jednego modelu testera, z mozliwoscia nadpisania z pliku."""

    def __init__(self, model: str, overrides: Mapping[str, Any] | None = None):
        normalized = str(model or "").strip().upper()
        if normalized not in DIALECTS:
            raise SafetyValidationError(
                f"Nieobslugiwany model testera: {normalized!r}. "
                f"Dostepne: {', '.join(sorted(DIALECTS))}"
            )
        base = DIALECTS[normalized]
        self.model = base["model"]
        self.description = base["description"]
        self.has_scan_box = bool(base["has_scan_box"])
        self.channel_count = int(base["channel_count"])
        self.max_steps = int(base["max_steps"])
        self.pass_judgment = int(base["pass_judgment"])
        self.non_terminal_judgments = set(base["non_terminal_judgments"])
        self._commands = dict(base["commands"])
        self._verification = dict(base["verification"])

        if overrides:
            self._apply_overrides(base, overrides)

    def _apply_overrides(self, base: Mapping[str, Any],
                         overrides: Mapping[str, Any]) -> None:
        """Pozwala poprawic skladnie SCPI bez przebudowy EXE."""
        custom_commands = overrides.get("commands", {})
        if custom_commands:
            if not isinstance(custom_commands, Mapping):
                raise SafetyValidationError("commands musi byc obiektem JSON")
            unknown = sorted(set(custom_commands) - set(self._commands))
            if unknown:
                raise SafetyValidationError(
                    f"Nieznane nazwy komend w station_config.json: {unknown}"
                )
            for name, template in custom_commands.items():
                if not str(template).strip():
                    raise SafetyValidationError(
                        f"Komenda {name!r} nie moze byc pusta"
                    )
                self._commands[name] = str(template).strip()
                self._verification[name] = "OVERRIDE"

        channel_count = overrides.get("channel_count")
        if channel_count is not None:
            count = int(channel_count)
            if not 0 <= count <= 64:
                raise SafetyValidationError(
                    "channel_count musi miescic sie w zakresie 0-64"
                )
            self.channel_count = count

    # ------------------------------------------------------------------ #
    def has(self, name: str) -> bool:
        return name in self._commands

    def command(self, name: str, **fields: Any) -> str:
        try:
            template = self._commands[name]
        except KeyError as exc:
            raise SafetyValidationError(
                f"Model {self.model} nie definiuje komendy {name!r}"
            ) from exc
        try:
            return template.format(**fields)
        except KeyError as exc:
            raise SafetyValidationError(
                f"Komenda {name!r} wymaga pola {exc.args[0]!r}"
            ) from exc

    def verification(self, name: str) -> str:
        return self._verification.get(name, UNVERIFIED)

    def unverified_commands(self) -> list[str]:
        return sorted(
            name for name, state in self._verification.items()
            if state == UNVERIFIED
        )

    def as_report(self) -> list[tuple[str, str, str]]:
        """(nazwa, szablon, stan_weryfikacji) - do zakladki diagnostyki."""
        return [
            (name, self._commands[name], self.verification(name))
            for name in sorted(self._commands)
        ]


def supported_models() -> list[str]:
    return sorted(DIALECTS)
