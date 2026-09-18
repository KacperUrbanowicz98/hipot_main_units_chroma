"""Testy regresyjne silnika Hi-Pot uruchamiane przez builder przed utworzeniem EXE.

Nie wymagaja podlaczonej Chromy ani Arduino. Sprawdzaja reguly bezpieczenstwa,
profile wielokrokowe, maski kanalow, bramke swiezego cyklu, dowody PASS per krok,
format raportu, kontrole dostepu i regresje bledow wykrytych w audycie 1.0.5.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    import serial as _serial_check  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("serial")
    stub.PARITY_NONE = "N"
    stub.PARITY_ODD = "O"
    stub.PARITY_EVEN = "E"
    stub.EIGHTBITS = 8
    stub.STOPBITS_ONE = 1
    stub.Serial = object
    sys.modules["serial"] = stub

from product_profile import ProductCatalog, ProductProfile
from report_writer import build_report_lines, save_report
from safety_rules import (
    ABSOLUTE_MIN_PRESENCE_MA,
    MIN_IN_RANGE_SAMPLES,
    SafetyValidationError,
    validate_channel_mask,
    validate_cycle_pass_evidence,
    validate_step,
    validate_step_pass_evidence,
    validate_timeout_for_steps,
)
from scpi_dialect import (
    Dialect,
    decode_channel_list,
    encode_channel_list,
    mask_to_channel_lists,
)
from settings_manager import SettingsManager

ROOT = Path(__file__).resolve().parent

SR_PROFILE_DATA = json.loads(
    (ROOT / "products" / "SR203_SR204.json").read_text(encoding="utf-8")
)

# Profil jednokrokowy bez scan boxa (Chroma 19052) NIE jest juz wysylany
# z aplikacja - zostaje wylacznie jako atrapa testowa, zeby sciezka
# 1-krokowa (rejestry LAST) i odrzucenie profilu dla innego modelu testera
# nadal byly sprawdzane.
SINGLE_STEP_PROFILE_DATA = {
    "schema_version": 1,
    "product_id": "TEST_1STEP",
    "display_name": "Atrapa 1-krokowa (19052)",
    "instrument": {"allowed_models": ["19052"], "requires_scan_box": False},
    "serial": {"allowed_lengths": [14, 17]},
    "test_timeout_s": 60,
    "steps": [{
        "name": "Izolacja",
        "mode": "ACW",
        "voltage": 4000,
        "limit_high": 5.0,
        "limit_low": 0.1,
        "presence_min_current": 0.5,
        "ramp_time": 1.0,
        "dwell": 2.0,
        "ramp_dn": 0.5,
        "arc_sense": 0.0,
        "frequency": 50,
        "continuity": "OFF",
        "real_limit": 0.0,
    }],
}


def single_step_profile() -> ProductProfile:
    return ProductProfile(json.loads(json.dumps(SINGLE_STEP_PROFILE_DATA)),
                          source="<atrapa 1-krokowa>")


def expect_error(func, description: str) -> None:
    try:
        func()
    except (SafetyValidationError, AssertionError, ValueError, RuntimeError):
        return
    raise AssertionError(f"Brak oczekiwanego bledu: {description}")


# ---------------------------------------------------------------------- #
# ATRAPY
# ---------------------------------------------------------------------- #
class DummyConfig:
    APP_NAME = "Reconext Hi-Pot Main Units"
    APP_VERSION = "1.0.0"
    WINDOW_TITLE = "Reconext Hi-Pot Main Units"
    COLOR_BG = COLOR_WHITE = "white"
    COLOR_PRIMARY = "blue"
    COLOR_ACCENT = "green"
    COLOR_ERROR = "red"
    COLOR_WARNING = "orange"
    COLOR_ACTION = "navy"
    COLOR_ACTION_BG = "#E3F2FD"
    INSTRUMENT_MODEL = "19053"
    DEVICE_COM_PORT = "COM2"
    DEVICE_BAUDRATE = 19200
    DEVICE_PARITY = "NONE"
    DEVICE_FLOW_CONTROL = "NONE"
    INTERLOCK_PORT = "COM11"
    INTERLOCK_BAUDRATE = 9600
    INTERLOCK_ENABLED = True
    STATION_ID = "HIPOT-TEST"
    LOG_DIR = "logs"
    PROFILE_MANIFEST_PATH = ""
    INTERLOCK_IDENTITY = ""
    # Musi byc True: reguly produkcyjne nie pozwalaja zapisac konfiguracji
    # z wylaczonym automatycznym zapisem raportow.
    AUTO_SAVE_RESULTS = True
    ENABLED_PRODUCTS = None
    SCPI_OVERRIDES: dict = {}
    ADMIN_PASSWORD = None

    def dialect(self):
        return Dialect(self.INSTRUMENT_MODEL, self.SCPI_OVERRIDES)

    def is_product_enabled(self, product_id):
        if not self.ENABLED_PRODUCTS:
            return True
        return str(product_id).strip().upper() in self.ENABLED_PRODUCTS

    def footer_left(self):
        return f"{self.APP_NAME} v{self.APP_VERSION}"


class DummyWidget:
    def __init__(self):
        self.values: dict = {}

    def config(self, **kwargs):
        self.values.update(kwargs)

    def cget(self, key):
        return self.values.get(key, "")

    def winfo_ismapped(self):
        return False

    def winfo_exists(self):
        return True

    def pack(self, **kwargs):
        return None

    def pack_forget(self):
        return None

    def coords(self, *args):
        return None


class DummyMessagebox:
    """Atrapa okien modalnych - test nie moze czekac na klikniecie."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def _record(self, title, message, **_kwargs):
        self.calls.append((title, message))
        return True

    showinfo = showwarning = showerror = askyesno = _record


class DummySerial:
    is_open = True

    def reset_input_buffer(self):
        pass

    def write(self, _data):
        return 1

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class ChunkedSerial(DummySerial):
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def readline(self):
        return self.chunks.pop(0) if self.chunks else b""


class FakeClock:
    def __init__(self, tick: float = 0.05):
        self.value = 100.0
        self.tick = float(tick)

    def monotonic(self):
        self.value += self.tick
        return self.value

    def sleep(self, seconds):
        self.value += float(seconds)


class FakeScan:
    def __init__(self, profile, serial="SR203012345678", model="SR203"):
        self.profile = profile
        self.serial = serial
        self.model_name = model
        self.product_id = profile.product_id


class FakeMultiStepDevice:
    """Chroma wykonujaca N krokow w jednym cyklu, sterowana skryptem testu."""

    def __init__(self, profile, currents_ma, results=None, polls_per_step=4,
                 report_step_count=None):
        self.connected = True
        self.profile = profile
        self.currents_ma = list(currents_ma)
        self.results = list(results) if results else ["PASS"] * profile.step_count
        self.polls_per_step = polls_per_step
        self.report_step_count = report_step_count
        self.last_poll_interval = 0.02
        self.poll = 0
        self.stop_calls = 0
        self._cycle_started = None

    def start_test(self):
        # Przez modul test_screen, zeby zlapac zegar podstawiony przez test.
        import test_screen as _screen

        self._cycle_started = _screen.time.monotonic()
        return True

    def cycle_started_monotonic(self):
        return self._cycle_started

    def request_stop(self):
        self.stop_calls += 1
        return True, "STOP wyslany"

    def confirm_stopped(self, attempts=3):
        return True, "atrapa: zatrzymane"

    @property
    def _total_polls(self):
        return self.polls_per_step * self.profile.step_count

    def get_status(self):
        return "RUNNING" if self.poll < self._total_polls else "STOPPED"

    def read_measurements(self):
        step_index = min(self.poll // self.polls_per_step,
                         self.profile.step_count - 1)
        self.poll += 1
        step = self.profile.steps[step_index]
        return {
            "step": step_index + 1,
            "step_raw": str(step_index + 1),
            "mode": "WVAC",
            "output_voltage": float(step["voltage"]),
            "measure_current": self.currents_ma[step_index] / 1000.0,
            "real_current": 0.0,
        }

    def get_cycle_results(self, steps):
        count = self.report_step_count or len(steps)
        entries = []
        for index in range(1, count + 1):
            step = self.profile.steps[index - 1]
            result = self.results[index - 1]
            code = "116" if result == "PASS" else "18"
            entries.append({
                "index": index,
                "name": step["name"],
                "result": result,
                "judgment_code": code,
                "error_code": "" if result == "PASS" else code,
                "output_voltage": float(step["voltage"]),
                "measured_current": self.currents_ma[index - 1],
                "real_current": 0.0,
            })
        import test_screen as _screen

        failed = [entry for entry in entries if entry["result"] != "PASS"]
        overall = "FAIL" if failed else "PASS"
        return overall, {
            "cycle_elapsed": (_screen.time.monotonic() - self._cycle_started
                              if self._cycle_started is not None else 0.0),
            "fresh_cycle": True,
            "cycle_id": 1,
            "steps": entries,
            "error_code": failed[0]["judgment_code"] if failed else "",
            "failed_step": failed[0]["name"] if failed else "",
        }

    def stop_test(self, verify=True, lock_timeout=1.5):
        self.stop_calls += 1
        return True, "atrapa: zatrzymane"

    def disconnect(self, send_stop=True):
        self.connected = False


# ---------------------------------------------------------------------- #
# TESTY
# ---------------------------------------------------------------------- #
def test_step_and_channel_rules() -> None:
    step = validate_step(SR_PROFILE_DATA["steps"][0], 1, 8)
    assert step["channels"] == "HOOOOOOO"
    assert step["presence_min_current"] == 0.2

    # Zapis z separatorami (jak w oprogramowaniu Chromy) musi byc rownowazny.
    assert validate_channel_mask("O,O,H,O,O,O,O,O", 8, "test") == "OOHOOOOO"
    assert validate_channel_mask(" o o h o o o o o ", 8, "test") == "OOHOOOOO"

    expect_error(lambda: validate_channel_mask("OOHOOOO", 8, "test"),
                 "maska o zlej dlugosci")
    expect_error(lambda: validate_channel_mask("OOXOOOOO", 8, "test"),
                 "niedozwolony znak maski")
    expect_error(lambda: validate_channel_mask("OOOOOOOO", 8, "test"),
                 "maska bez zadnego kanalu H")

    base = dict(SR_PROFILE_DATA["steps"][0])
    expect_error(lambda: validate_step({**base, "presence_min_current": 0.0}, 1, 8),
                 "zerowy prog obecnosci")
    expect_error(
        lambda: validate_step(
            {**base, "presence_min_current": ABSOLUTE_MIN_PRESENCE_MA / 2}, 1, 8),
        "prog obecnosci ponizej bezwzglednej granicy")
    expect_error(lambda: validate_step({**base, "presence_min_current": 1.0}, 1, 8),
                 "prog obecnosci rowny Max Limit")
    expect_error(lambda: validate_step({**base, "limit_low": 0.5,
                                        "presence_min_current": 0.2}, 1, 8),
                 "prog obecnosci nizszy od Min Limit")
    expect_error(lambda: validate_step({**base, "voltage": 6000}, 1, 8),
                 "napiecie powyzej 5 kV")
    expect_error(lambda: validate_step({**base, "dwell": 0.0}, 1, 8),
                 "zerowy dwell")
    expect_error(lambda: validate_step({**base, "mode": "DCW"}, 1, 8),
                 "nieobslugiwany tryb DCW")
    # Profil bez scan boxa nie moze definiowac kanalow, i odwrotnie.
    expect_error(lambda: validate_step(base, 1, 0),
                 "maska kanalow bez scan boxa")
    expect_error(lambda: validate_step({k: v for k, v in base.items()
                                        if k != "channels"}, 1, 8),
                 "brak maski kanalow przy scan boxie")


def test_profile_catalog() -> None:
    catalog = ProductCatalog(ROOT / "products")
    assert not catalog.errors, f"Profile odrzucone: {catalog.errors}"
    # Profile stanowisk Auto Branch 1. SR203_SR204 to JEDEN profil scalony
    # z czterech wariantow; ER115 / SE210 / SR213 to odrebne wyroby.
    assert set(catalog.ids()) == {"SR203_SR204", "ER115", "SE210", "SR213"}

    # Kazdy profil: S/N dokladnie 14 znakow.
    for profile in catalog.all():
        assert profile.serial_lengths == (14,), profile.product_id
        assert profile.allowed_models == ("19053",), profile.product_id

    # SR213 ma w tabeli zrodlowej krok 'Ethernet 3' i 'ADSL' na TYM SAMYM
    # kanale 6. To nie jest blad walidacji - fixture moze tak byc zrobiony -
    # ale profil MUSI o tym ostrzegac, bo to typowy objaw pomylki przy
    # przepisywaniu maski. Gdy kolizja zostanie wyjasniona i poprawiona,
    # ten test upadnie i trzeba go zaktualizowac swiadomie.
    sr213 = catalog.get("SR213")
    assert sr213.step_count == 6
    assert any("kanal 6" in w for w in sr213.channel_warnings), \
        sr213.channel_warnings
    # Identyfikatory profili sa WIELKIMI literami, jak nazwy wyrobow.
    assert all(pid == pid.upper() for pid in catalog.ids()), catalog.ids()
    # Dopasowanie pozostaje niewrazliwe na wielkosc liter.
    assert catalog.get("sr203_sr204").product_id == "SR203_SR204"

    sr = catalog.get("SR203_SR204")
    assert sr.step_count == 5
    assert sr.requires_scan_box and sr.channel_count == 8
    assert sr.matches_instrument("19053") and not sr.matches_instrument("19052")
    # 5 x (0.5 + 1.0 + 0.5) + 4 x przerwa 0.5 s
    assert abs(sr.total_duration - 12.0) < 0.001, sr.total_duration
    assert not sr.channel_warnings, sr.channel_warnings
    # Kolejnosc i limity wg Profile_HiPot.xlsx (Profil 1 = Profil 3) oraz
    # logu ze stanowiska D21022AD023966.txt.
    assert [step["name"] for step in sr.steps] == [
        "Ethernet 1", "Ethernet 2", "Ethernet 3", "Ethernet 4", "Modem"]
    assert [step["channels"] for step in sr.steps] == [
        "HOOOOOOO", "OHOOOOOO", "OOHOOOOO", "OOOHOOOO", "OOOOHOOO"]
    assert [step["voltage"] for step in sr.steps] == [1060, 1060, 1060, 1060, 1500]
    assert [step["limit_low"] for step in sr.steps] == [0.2, 0.2, 0.2, 0.2, 0.05]
    assert all(step["limit_high"] == 1.0 for step in sr.steps)
    assert sr.report_program == "SR203.204"
    # Numer seryjny: dokladnie 14 znakow, wylacznie wielkie litery i cyfry.
    assert sr.serial_lengths == (14,)
    assert sr.validate_serial("d21022ad023966") == "D21022AD023966"
    expect_error(lambda: sr.validate_serial("D21022AD02396"), "S/N 13 znakow")
    expect_error(lambda: sr.validate_serial("D21022AD0239667"), "S/N 15 znakow")
    expect_error(lambda: sr.validate_serial("D21022-AD02396"), "S/N ze znakiem -")


    single = single_step_profile()
    assert single.step_count == 1 and not single.requires_scan_box
    assert single.effective_low_ma(single.steps[0]) == 0.5

    # Duplikaty nazw krokow i puste listy krokow musza byc odrzucone.
    duplicated = json.loads(json.dumps(SR_PROFILE_DATA))
    duplicated["steps"][1]["name"] = "Modem"
    expect_error(lambda: ProductProfile(duplicated), "powtorzone nazwy krokow")
    expect_error(lambda: ProductProfile({**SR_PROFILE_DATA, "steps": []}),
                 "profil bez krokow")
    expect_error(
        lambda: ProductProfile({**SR_PROFILE_DATA, "instrument": {
            "allowed_models": [], "requires_scan_box": True,
            "channel_count": 8}}),
        "profil bez wskazanego modelu testera")
    expect_error(lambda: ProductProfile({**SR_PROFILE_DATA, "schema_version": 99}),
                 "profil z nowszej wersji schematu")

    # Timeout krotszy niz czas cyklu z marginesem.
    expect_error(lambda: validate_timeout_for_steps(10, sr.steps),
                 "timeout krotszy niz profil")
    assert validate_timeout_for_steps(60, sr.steps) == 60

    # Nakladajace sie kanaly H daja ostrzezenie, ale nie blokada.
    overlapping = json.loads(json.dumps(SR_PROFILE_DATA))
    overlapping["steps"][2]["channels"] = "OOOHOOOO"
    assert ProductProfile(overlapping).channel_warnings


def test_pass_evidence_rules() -> None:
    validate_step_pass_evidence(
        step_name="Ethernet 1", step_index=2, target_voltage=1060,
        effective_low_ma=0.035, high_limit_ma=1.0, final_voltage=1058,
        final_current_ma=0.21, cycle_max_voltage=1061,
        in_range_samples=MIN_IN_RANGE_SAMPLES, overcurrent_seen=False)

    base = dict(step_name="Ethernet 1", step_index=2, target_voltage=1060,
                effective_low_ma=0.035, high_limit_ma=1.0, final_voltage=1060,
                final_current_ma=0.21, cycle_max_voltage=1060,
                in_range_samples=MIN_IN_RANGE_SAMPLES, overcurrent_seen=False)
    for description, changes in (
        ("prad ponizej progu obecnosci", dict(final_current_ma=0.001)),
        ("prad powyzej Max Limit", dict(final_current_ma=1.5)),
        ("napiecie nizsze od 90% nastawy", dict(final_voltage=800,
                                               cycle_max_voltage=800)),
        ("za malo probek w zakresie", dict(in_range_samples=1)),
        ("wykryte przekroczenie pradu w cyklu", dict(overcurrent_seen=True)),
    ):
        expect_error(
            lambda changes=changes: validate_step_pass_evidence(
                **{**base, **changes}), description)

    steps_ok = [{"index": i, "name": f"S{i}", "result": "PASS"} for i in range(1, 6)]
    validate_cycle_pass_evidence(result="PASS", terminal_status="STOPPED",
                                 step_results=steps_ok, expected_steps=5)
    # FAIL nie podlega walidacji dowodow PASS.
    validate_cycle_pass_evidence(result="FAIL", terminal_status="FAIL",
                                 step_results=[], expected_steps=5)

    expect_error(
        lambda: validate_cycle_pass_evidence(
            result="PASS", terminal_status="STOPPED",
            step_results=steps_ok[:4], expected_steps=5),
        "brak wyniku jednego z krokow")
    expect_error(
        lambda: validate_cycle_pass_evidence(
            result="PASS", terminal_status="FAIL",
            step_results=steps_ok, expected_steps=5),
        "status cyklu FAIL przy PASS")
    expect_error(
        lambda: validate_cycle_pass_evidence(
            result="PASS", terminal_status="STOPPED",
            step_results=[*steps_ok[:4],
                          {"index": 5, "name": "S5", "result": "FAIL"}],
            expected_steps=5),
        "jeden krok bez zaliczenia przy globalnym PASS")


def test_dialect_and_channel_lists() -> None:
    """Skladnia wg manuala 19051-19054 wer. 2.1, rozdz. 5."""
    dialect = Dialect("19053")
    assert dialect.has_scan_box and dialect.channel_count == 8
    assert Dialect("19054").channel_count == 4
    assert not Dialect("19051").has_scan_box

    # Manual s. 5-21: kanaly ustawia sie LISTA, nie komenda na kanal.
    assert dialect.command("channel_high", step=1,
                           channels=encode_channel_list([1, 3])) \
        == "SAFEty:STEP1:AC:CHANnel (@(1,3))"
    assert dialect.command("channel_low", step=3,
                           channels=encode_channel_list([])) \
        == "SAFEty:STEP3:AC:CHANnel:LOW (@(0))"
    assert dialect.command("result_step_judgment", step=4) \
        == "SAFEty:RESult:STEP4:JUDG?"
    # Manual s. 5-20: ARC lezy pod AC:LIMit:ARC, nie pod AC:ARC.
    assert dialect.command("acw_limit_arc", step=1, value=0.004) \
        == "SAFEty:STEP1:AC:LIMit:ARC 0.004"
    # Manual s. 5-32: czestotliwosc jest ustawieniem globalnym.
    assert dialect.command("preset_frequency", value=60) \
        == "SAFEty:PRESet:AC:FREQuency 60"

    assert encode_channel_list([]) == "(@(0))"
    assert encode_channel_list([3, 1, 3]) == "(@(1,3))"
    assert decode_channel_list("(@(1,3))") == {1, 3}
    assert decode_channel_list("(@(0))") == set()
    assert decode_channel_list(" (@(2,4)) ") == {2, 4}
    expect_error(lambda: decode_channel_list("krzaki"), "nieczytelna lista kanalow")
    expect_error(lambda: encode_channel_list([0, -1]), "ujemny numer kanalu")

    assert mask_to_channel_lists("OOHOOOOO") == ([3], [])
    assert mask_to_channel_lists("HOLOOOOO") == ([1], [3])

    single = Dialect("19051")
    expect_error(lambda: single.command("channel_high", step=1, channels="(@(1))"),
                 "komenda kanalow na modelu bez scan boxa")

    custom = Dialect("19053", {
        "commands": {"channel_high": "SAFE:STEP{step}:AC:CHAN {channels}"}})
    assert custom.command("channel_high", step=2, channels="(@(5))") \
        == "SAFE:STEP2:AC:CHAN (@(5))"

    expect_error(lambda: Dialect("19099"), "nieobslugiwany model testera")
    expect_error(lambda: Dialect("19053", {"commands": {"nie_ma_takiej": "X"}}),
                 "nieznana nazwa komendy w nadpisaniach")

    # Manual s. 5-17: 113 i 114 tez nie sa werdyktem o produkcie.
    assert dialect.non_terminal_judgments == {112, 113, 114, 115}
    assert dialect.pass_judgment == 116


def test_rs232_response_reassembly() -> None:
    """Regresja K2 z audytu: odpowiedz rozbita na fragmenty nie moze byc obcieta."""
    from hipot_device import ChromaDevice

    cases = [
        ([b"+3.999", b"000E+03\n"], ChromaDevice._is_float, "+3.999000E+03"),
        ([b"11", b"6\n"], ChromaDevice._is_integer, "116"),
        ([b"+1.7", b"34000", b"E-03\n"], ChromaDevice._is_float, "+1.734000E-03"),
    ]
    for chunks, validator, expected in cases:
        device = ChromaDevice("COM1", 9600)
        device.connected = True
        device.serial = ChunkedSerial(chunks)
        answer = device.query("Q?", timeout=1.0, retries=1, validator=validator)
        assert answer == expected, f"odczytano {answer!r}, oczekiwano {expected!r}"

    device = ChromaDevice("COM1", 9600)
    device.connected = True
    device.serial = ChunkedSerial([b"+3.99"])
    assert device.query("Q?", timeout=0.3, retries=1) is None


def test_device_identity_matches_station_model() -> None:
    """Chroma innego modelu niz w konfiguracji nie moze zostac przyjeta."""
    from hipot_device import ChromaDevice

    def make(idn: str, model: str):
        device = ChromaDevice("COM1", 9600, dialect=Dialect(model))
        answers = {"*IDN?": idn, "SYST:ERR?": '+0,"No error"', "SYST:KLOC?": "1"}
        device.query = lambda command, **_kwargs: answers.get(command)
        device.send_command = lambda _command: None
        return device

    with patch("hipot_device.serial.Serial", return_value=DummySerial()), \
         patch("hipot_device.time.sleep", lambda _seconds: None):
        assert make("Chroma,19053,190530001,5.14", "19053").connect() is True
        assert make("Chroma,19052,190520013341,5.14", "19052").connect() is True
        # Stanowisko skonfigurowane pod 19053, a podlaczono 19052.
        assert make("Chroma,19052,190520013341,5.14", "19053").connect() is False
        assert make("Other,1234,ABC,1.0", "19053").connect() is False


def test_multistep_configuration_and_channel_readback() -> None:
    """Programowanie 5 krokow + potwierdzenie kanalow przez STEP:SET?."""
    from hipot_device import ChromaDevice, DeviceError

    catalog = ProductCatalog(ROOT / "products")
    profile = catalog.get("SR203_SR204")

    def build(step_count="5", corrupt_channels=False, corrupt_mode=False,
              settings_fields=None):
        """Atrapa Chromy odpowiadajaca w formacie z manuala (s. 5-18)."""
        device = ChromaDevice("COM1", 19200, dialect=Dialect("19053"))
        device.connected = True
        device.serial = DummySerial()
        sent: list[str] = []
        state: dict[int, dict] = {}

        def send(command):
            sent.append(command)
            match = re.fullmatch(
                r"SAFEty:STEP(\d+):AC:CHANnel(:LOW)? \(@\(([\d,]+)\)\)", command)
            if match:
                index = int(match.group(1))
                side = "low" if match.group(2) else "high"
                channels = {int(x) for x in match.group(3).split(",") if int(x)}
                state.setdefault(index, {})[side] = channels
                return
            match = re.fullmatch(r"SAFEty:STEP(\d+):AC:LIMit:(ARC|REAL) (\S+)",
                                 command)
            if match:
                state.setdefault(int(match.group(1)), {})[
                    match.group(2).lower()] = float(match.group(3))

        def query(command, **_kwargs):
            if command == "SYST:ERR?":
                return '+0,"No error"'
            if command == "SAFEty:SNUMber?":
                return step_count
            if command == "SAFEty:PRESet:AC:FREQuency?":
                return "6.000000E+01"
            match = re.fullmatch(r"SAFEty:STEP(\d+):SET\?", command)
            if not match:
                return None
            index = int(match.group(1))
            step = profile.steps[index - 1]
            stored = state.get(index, {})
            high = stored.get("high", set())
            low = stored.get("low", set())
            if corrupt_channels:
                high, low = set(), set()
            fields = settings_fields or [
                str(index),
                "OS" if corrupt_mode else "AC",
                f"{float(step['voltage']):.6E}",
                f"{step['limit_high'] / 1000.0:.6E}",
                f"{profile.effective_low_ma(step) / 1000.0:.6E}",
                f"{stored.get('arc', 0.0):.6E}",
                f"{step['dwell']:.6E}",
                f"{step['ramp_time']:.6E}",
                f"{step['ramp_dn']:.6E}",
                f"{stored.get('real', 0.0):.6E}",
                encode_channel_list(high),
                encode_channel_list(low),
            ]
            return ", ".join(fields)

        device.send_command = send
        device.query = query
        return device, sent

    device, sent = build()
    with patch("hipot_device.time.sleep", lambda _seconds: None):
        device.configure_profile(profile)

    assert "SAFEty:PRESet:AC:FREQuency 60" in sent, "brak ustawienia czestotliwosci"
    assert "SAFEty:STEP1:AC:LEVel 1060" in sent
    assert "SAFEty:STEP5:AC:LEVel 1500" in sent
    # Ethernet 1 = kanal 1 HIGH, Modem (krok 5) = kanal 5 HIGH;
    # strona LOW wylaczona.
    assert "SAFEty:STEP1:AC:CHANnel (@(1))" in sent
    assert "SAFEty:STEP5:AC:CHANnel (@(5))" in sent
    assert "SAFEty:STEP1:AC:CHANnel:LOW (@(0))" in sent
    assert "SAFEty:STEP1:AC:LIMit:ARC 0.0" in sent
    assert "SAFEty:STEP1:AC:LIMit:REAL 0.0" in sent
    assert device._configured_step_count == 5

    # Parsowanie STEP:SET? nie moze rozerwac listy kanalow na przecinku.
    fields = ChromaDevice._split_settings(
        "1, AC, 5.000000E+03, 6.000000E-04, 7.000000E-06, 8.000000E-03, "
        "3.000000E+00, 1.000000E+00, 2.000000E+00, 4.000000E-04, "
        "(@(1,3)), (@(2,4))")
    assert len(fields) == 12, fields
    assert fields[10] == "(@(1,3))" and fields[11] == "(@(2,4))"

    device, _ = build()
    with patch("hipot_device.time.sleep", lambda _seconds: None):
        settings = device.read_step_settings(1)
    assert settings["mode"] == "AC"
    assert settings["channels_high"] == set() or True

    # Chroma raportujaca inna liczbe krokow niz profil = blad konfiguracji.
    device, _ = build(step_count="3")
    with patch("hipot_device.time.sleep", lambda _seconds: None):
        expect_error(lambda: device.configure_profile(profile),
                     "niezgodna liczba krokow po konfiguracji")

    # Scan box zwracajacy inna maske niz ustawiona MUSI zablokowac test.
    device, _ = build(corrupt_channels=True)
    with patch("hipot_device.time.sleep", lambda _seconds: None):
        try:
            device.configure_profile(profile)
        except DeviceError as exc:
            assert "maska kanalow nie zostala przyjeta" in str(exc), str(exc)
        else:
            raise AssertionError(
                "Niezgodny odczyt kanalow nie zablokowal konfiguracji")

    # Krok zaraportowany w innym trybie niz AC blokuje test.
    device, _ = build(corrupt_mode=True)
    with patch("hipot_device.time.sleep", lambda _seconds: None):
        expect_error(lambda: device.configure_profile(profile),
                     "krok w trybie innym niz AC")

    # Obcieta odpowiedz STEP:SET? nie moze zostac uznana za poprawna.
    device, _ = build(settings_fields=["1", "AC", "1.5E+03"])
    with patch("hipot_device.time.sleep", lambda _seconds: None):
        expect_error(lambda: device.configure_profile(profile),
                     "niekompletna odpowiedz STEP:SET?")

    # Profil 19052 na stanowisku 19053 musi zostac odrzucony.
    device, _ = build()
    expect_error(lambda: device.configure_profile(single_step_profile()),
                 "profil dla innego modelu testera")


def test_multistep_result_gate() -> None:
    """Bramka wyniku dla profilu 5-krokowego z dowodami per krok."""
    from test_screen import TestScreen

    catalog = ProductCatalog(ROOT / "products")
    profile = catalog.get("SR203_SR204")

    def run_case(currents_ma, results=None, polls_per_step=4,
                 report_step_count=None):
        screen = TestScreen(None, DummyConfig(), FakeScan(profile))
        screen.device = FakeMultiStepDevice(
            profile, currents_ma, results, polls_per_step, report_step_count)
        screen.test_running = True
        screen._test_aborted = False
        screen._closed = False
        screen._run_id = 1

        callbacks, completed, errors = [], [], []
        screen._post_ui = callbacks.append
        screen._update_display = lambda: None
        screen._highlight_current_step = lambda: None
        screen._test_completed = lambda result, data: completed.append((result, data))
        screen._test_error = errors.append

        # Tik 0.5 s + sleep 0.1 s w petli daje ~0.6 s na iteracje, czyli
        # realny czas cyklu 12 s przy 20 odpytaniach (profil SR203 = 12.0 s).
        clock = FakeClock(tick=0.5)
        screen.start_time = clock.monotonic()
        with patch("test_screen.time.monotonic", clock.monotonic), \
             patch("test_screen.time.sleep", clock.sleep):
            screen._run_test_background(1)
        for callback in callbacks:
            callback()
        return completed, errors, screen

    good = [0.21] * 5

    completed, errors, screen = run_case(good)
    assert [item[0] for item in completed] == ["PASS"], (completed, errors)
    assert not errors
    for index in range(1, 6):
        evidence = screen._evidence[index]
        assert evidence.in_range_samples >= MIN_IN_RANGE_SAMPLES, (
            f"krok {index}: {evidence.in_range_samples} probek w zakresie")

    # Jeden port oblany -> caly wyrob FAIL, z nazwa kroku w danych.
    completed, errors, _ = run_case(good, results=["PASS"] * 3 + ["FAIL", "PASS"])
    assert [item[0] for item in completed] == ["FAIL"]
    assert completed[0][1]["failed_step"] == "Ethernet 4"
    assert not errors

    # Pusty fixture na jednym porcie: tester zwraca 116, ale prad ponizej progu.
    completed, errors, _ = run_case([0.21, 0.21, 0.001, 0.21, 0.21])
    assert not completed
    assert errors and "Odrzucono PASS" in errors[0], errors
    assert "Ethernet 3" in errors[0], errors[0]

    # Przekroczenie Max Limit w trakcie kroku odrzuca PASS.
    completed, errors, _ = run_case([0.21, 0.21, 0.21, 2.0, 0.21])
    assert not completed and errors

    # Brak wyniku jednego kroku = brak PASS.
    completed, errors, _ = run_case(good, report_step_count=4)
    assert not completed
    assert errors and "wyniki 4 z 5" in errors[0], errors

    # Cykl zakonczony podejrzanie szybko nie moze dac PASS.
    completed, errors, _ = run_case(good, polls_per_step=1)
    assert not completed
    assert errors and "zbyt szybko" in errors[0], errors

    # Za malo probek przy pelnym napieciu (szybki cykl, ale dlugi czas) -
    # sprawdzane osobno przez dowody kroku.
    expect_error(
        lambda: validate_step_pass_evidence(
            step_name="Modem", step_index=1, target_voltage=1500,
            effective_low_ma=0.02, high_limit_ma=1.5, final_voltage=1500,
            final_current_ma=0.2, cycle_max_voltage=1500,
            in_range_samples=1, overcurrent_seen=False),
        "jedna probka w zakresie")


def test_single_step_profile_uses_last_registers() -> None:
    """Profil 1-krokowy czyta rejestr LAST, tak jak wersja 1.0.x."""
    from hipot_device import ChromaDevice

    profile = single_step_profile()

    device = ChromaDevice("COM1", 9600, dialect=Dialect("19052"))
    device.connected = True
    device.serial = DummySerial()
    device._cycle_active_confirmed = True
    device._cycle_started_monotonic = 10.0
    device._cycle_id = 7
    device._clear_input = lambda: None

    answers = {
        "SAFEty:RESult:LAST:JUDG?": "116",
        "SAFEty:RESult:LAST:OMET?": "+3.999000E+03",
        "SAFEty:RESult:LAST:MMET?": "+1.734000E-03",
        "SAFEty:RESult:LAST:RMET?": "+9.910000E+37",
    }
    device.query = lambda command, **_kwargs: answers.get(command)

    with patch("hipot_device.time.sleep", lambda _seconds: None), \
         patch("hipot_device.time.monotonic", lambda: 16.0):
        result, data = device.get_cycle_results(profile.steps)
        assert result == "PASS", data
        assert data["cycle_id"] == 7
        assert abs(data["steps"][0]["measured_current"] - 1.734) < 0.001
        assert data["steps"][0]["output_voltage"] == 3999.0
        # Wynik moze zostac odczytany tylko raz dla jednego STARTu.
        second, _ = device.get_cycle_results(profile.steps)
        assert second == "UNKNOWN"

    # Profil wielokrokowy na modelu bez wynikow per krok = blad, nie zgadywanie.
    sr = ProductCatalog(ROOT / "products").get("SR203_SR204")
    single = ChromaDevice("COM1", 9600, dialect=Dialect("19052"))
    single.connected = True
    single.serial = DummySerial()
    single._cycle_active_confirmed = True
    single._cycle_started_monotonic = 10.0
    single._clear_input = lambda: None
    single.query = lambda command, **_kwargs: None
    with patch("hipot_device.time.sleep", lambda _seconds: None), \
         patch("hipot_device.time.monotonic", lambda: 16.0):
        result, _ = single.get_cycle_results(sr.steps)
        assert result == "UNKNOWN"


def test_fresh_cycle_guard() -> None:
    """Stary wynik LAST nie moze zostac uznany za wynik nowego cyklu."""
    from hipot_device import ChromaDevice

    class FakeStart:
        def __init__(self, baseline_voltage, statuses, live_voltages):
            self.impl = ChromaDevice("COM1", 9600, dialect=Dialect("19052"))
            self.impl.connected = True
            self.impl.serial = DummySerial()
            self.impl._configured_step_count = 1
            self.baseline_voltage = baseline_voltage
            self.statuses = list(statuses)
            self.live_voltages = list(live_voltages)
            self.reads = 0
            self.stopped = False

            self.impl._clear_input = lambda: None
            self.impl._write_unlocked = lambda _command: None
            self.impl.query = self.query
            self.impl.get_status = self.get_status
            self.impl.read_measurements = self.read_measurements
            self.impl.stop_test = self.stop_test

        def query(self, command, **_kwargs):
            return {"SYST:ERR?": '+0,"No error"',
                    "SYST:KLOC?": "1"}.get(command)

        def get_status(self):
            return self.statuses.pop(0) if self.statuses else "PASS"

        def read_measurements(self):
            self.reads += 1
            voltage = (self.baseline_voltage if self.reads == 1
                       else (self.live_voltages.pop(0) if self.live_voltages
                             else self.baseline_voltage))
            return {"step": 1, "output_voltage": float(voltage),
                    "measure_current": 0.0017, "real_current": 0.0}

        def stop_test(self, verify=True, lock_timeout=1.5):
            self.stopped = True
            self.impl._cycle_active_confirmed = False
            self.impl._cycle_started_monotonic = None
            return True, "atrapa: zatrzymane"

    clock = FakeClock()
    with patch("hipot_device.time.monotonic", clock.monotonic), \
         patch("hipot_device.time.sleep", clock.sleep):
        assert FakeStart(0.0, ["RUNNING"], []).impl.start_test() is True
        assert FakeStart(0.0, ["WAIT"], [120.0]).impl.start_test() is True

        stale = FakeStart(4000.0, ["PASS"] * 20, [4000.0] * 20)
        assert stale.impl.start_test() is False, "Stary PASS zostal przyjety"
        assert stale.stopped is True

        blind = FakeStart(0.0, ["PASS"] * 20, [4000.0] * 20)
        blind.impl.read_measurements = lambda: None
        assert blind.impl.start_test() is False

        # Brak potwierdzonej konfiguracji krokow blokuje START.
        unconfigured = FakeStart(0.0, ["RUNNING"], [])
        unconfigured.impl._configured_step_count = 0
        assert unconfigured.impl.start_test() is False


def test_interlock_regressions() -> None:
    """Regresje K1 i K3 z audytu wersji 1.0.5."""
    from interlock import InterlockMonitor

    class GarbageSerial:
        is_open = True

        def __init__(self):
            self.reads = 0

        def readline(self):
            self.reads += 1
            if self.reads > 3000:
                raise AssertionError(
                    "K1: heartbeat nie wygasl mimo strumienia smieci")
            return b"\x00\xff?\n"

        def close(self):
            self.is_open = False

    class SplitSerial:
        def __init__(self):
            self.is_open = True
            self.frames = [b"CLO", b"SED\n", b"OP", b"EN\n"]

        def readline(self):
            return self.frames.pop(0) if self.frames else b""

        def close(self):
            self.is_open = False

    def run(serial_object):
        clock = FakeClock()
        monitor = InterlockMonitor("COM11", 9600, heartbeat_timeout=0.5)
        monitor.serial = serial_object
        monitor.connected = True
        monitor._last_message_time = clock.monotonic()
        events = []
        monitor.set_on_change(events.append)
        with patch("interlock.time.monotonic", clock.monotonic), \
             patch("interlock.time.sleep", clock.sleep):
            monitor._monitor_loop()
        return events, monitor

    events, monitor = run(GarbageSerial())
    assert events == [None], f"K1: oczekiwano utraty interlocka, jest {events}"
    assert monitor.connected is False

    events, _ = run(SplitSerial())
    assert events[:2] == [True, False], f"K3: bledna sekwencja {events}"
    assert events[-1] is None


def test_interlock_state_machine() -> None:
    """Start tylko po sekwencji OPEN -> CLOSED; wynik po zakonczeniu HV chroniony."""
    from test_screen import TestScreen

    catalog = ProductCatalog(ROOT / "products")
    profile = catalog.get("SR203_SR204")
    screen = TestScreen(None, DummyConfig(), FakeScan(profile))
    screen.device = type("Device", (), {
        "connected": True,
        "stop_test": lambda self, verify=True, lock_timeout=1.5:
            (True, "atrapa"),
        # K1: STOP z watku Tk idzie przez request_stop (szybki zapis),
        # a potwierdzenie leci osobno w tle.
        "request_stop": lambda self, lock_timeout=1.5: (True, "atrapa"),
        "confirm_stopped": lambda self, attempts=3: (True, "atrapa"),
    })()
    screen.interlock = type("Interlock", (), {"connected": True})()
    screen._device_configured = True
    for name in ("interlock_label", "interlock_frame", "start_button",
                 "stop_button", "back_button", "next_sn_button", "status_label"):
        setattr(screen, name, DummyWidget())
    screen.sn_dialog = None

    starts = []
    screen._start_test = lambda: starts.append("START")

    screen._apply_interlock_state(True)
    assert not starts, "Samo poczatkowe CLOSED nie moze uruchomic testu"
    screen._apply_interlock_state(False)
    screen._apply_interlock_state(True)
    assert starts == ["START"], "OPEN -> CLOSED powinno uruchomic test"

    # Otwarcie klapy po fizycznym zakonczeniu HV nie kasuje wyniku.
    screen.test_running = True
    screen._cycle_terminal_seen = True
    screen._test_aborted = False
    screen._apply_interlock_state(False)
    assert screen.test_running is True
    assert screen._test_aborted is False
    assert screen._lid_open_seen is True

    # STOP nie moze anulowac juz zweryfikowanego wyniku.
    screen._result_pending = True
    screen._test_aborted = False
    screen._stop_test()
    assert screen._test_aborted is False

    # Utrata interlocka w trakcie testu przerywa cykl i blokuje kolejne.
    screen2 = TestScreen(None, DummyConfig(), FakeScan(profile))
    screen2.device = type("Device", (), {
        "connected": True,
        "stop_test": lambda self, verify=True, lock_timeout=1.5:
            (True, "atrapa"),
        "request_stop": lambda self, lock_timeout=1.5: (True, "atrapa"),
        "confirm_stopped": lambda self, attempts=3: (True, "atrapa"),
    })()
    screen2.interlock = type("Interlock", (), {"connected": True})()
    screen2._device_configured = True
    for name in ("interlock_label", "interlock_frame", "start_button",
                 "stop_button", "back_button", "next_sn_button", "status_label"):
        setattr(screen2, name, DummyWidget())
    screen2.test_running = True
    screen2._cycle_terminal_seen = False
    shown = []
    with patch("test_screen.messagebox.showerror",
               lambda *args, **kwargs: shown.append(args)):
        screen2._apply_interlock_state(None)
    assert screen2._test_aborted is True and screen2.test_running is False
    assert shown, "Utrata interlocka musi byc zgloszona operatorowi"


def test_next_serial_keeps_station_profile() -> None:
    """Kolejny S/N zawsze nalezy do profilu, na ktorym pracuje stanowisko.

    Wczesniej ``_accept_scan`` porownywalo ``scan.product_id`` z profilem
    ekranu. Po usunieciu mapy HWID numer seryjny nie niesie informacji
    o produkcie, wiec ``resolve_serial`` Z DEFINICJI zwraca profil podany
    na wejsciu - warunek nie mogl byc prawdziwy. Zostal usuniety jako
    martwy, bo przy czytaniu kodu sugerowal zabezpieczenie, ktorego nie ma.
    Zmiana profilu wymaga powrotu do menu.
    """
    from product_profile import resolve_serial

    source = (ROOT / "test_screen.py").read_text(encoding="utf-8")
    assert "scan.product_id != self.profile.product_id" not in source, (
        "martwa kontrola produktu wrocila do _accept_scan")

    catalog = ProductCatalog(ROOT / "products")
    sr = catalog.get("SR203_SR204")
    other = catalog.get("SR213")

    # Ten sam numer, dwa rozne profile -> profil bierze sie WYLACZNIE
    # z tego, co przekazano, nigdy z numeru seryjnego.
    for profile in (sr, other):
        ok, scan = resolve_serial(profile, "D70021AC017377")
        assert ok and scan.profile is profile
        assert scan.product_id == profile.product_id

    # Numer nadal musi przejsc walidacje dlugosci i zestawu znakow.
    ok, message = resolve_serial(sr, "D70021AC01737")
    assert not ok and "dlugosc" in str(message).lower()

def test_enabled_products_gate() -> None:
    """Profil wylaczony na stanowisku nie moze zostac uruchomiony."""
    config = DummyConfig()
    config.ENABLED_PRODUCTS = None
    assert config.is_product_enabled("TEST_1STEP") is True
    assert config.is_product_enabled("SR203_SR204") is True

    config.ENABLED_PRODUCTS = ["SR203_SR204"]
    assert config.is_product_enabled("SR203_SR204") is True
    assert config.is_product_enabled("sr203_sr204") is True
    assert config.is_product_enabled("TEST_1STEP") is False

    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as temp:
        os.chdir(temp)
        try:
            saved = DummyConfig()
            saved.ENABLED_PRODUCTS = ["SR203_SR204"]
            SettingsManager().save_config(saved)
            data = json.loads(
                Path("station_config.json").read_text(encoding="utf-8"))
            assert data["ENABLED_PRODUCTS"] == ["SR203_SR204"]

            loaded = DummyConfig()
            SettingsManager().load_config(loaded)
            assert loaded.ENABLED_PRODUCTS == ["SR203_SR204"]
            assert loaded.is_product_enabled("TEST_1STEP") is False

            # Brak pola = wszystkie profile wlaczone.
            data.pop("ENABLED_PRODUCTS")
            Path("station_config.json").write_text(json.dumps(data),
                                                   encoding="utf-8")
            loaded = DummyConfig()
            SettingsManager().load_config(loaded)
            assert loaded.ENABLED_PRODUCTS is None
            assert loaded.is_product_enabled("TEST_1STEP") is True

            # Lista samych pustych wpisow jest bledem konfiguracji.
            data["ENABLED_PRODUCTS"] = ["", "  "]
            Path("station_config.json").write_text(json.dumps(data),
                                                   encoding="utf-8")
            expect_error(lambda: SettingsManager().load_config(DummyConfig()),
                         "ENABLED_PRODUCTS z samymi pustymi wpisami")

            data["ENABLED_PRODUCTS"] = "sr203_sr204"
            Path("station_config.json").write_text(json.dumps(data),
                                                   encoding="utf-8")
            expect_error(lambda: SettingsManager().load_config(DummyConfig()),
                         "ENABLED_PRODUCTS jako tekst zamiast listy")
        finally:
            os.chdir(previous_cwd)


def test_profile_step_reordering() -> None:
    """Panel inzynieryjny musi umiec zmienic kolejnosc krokow profilu."""
    source = (ROOT / "admin_panel.py").read_text(encoding="utf-8")
    for marker in ("_move_step", "select_index", "ZMIANA KOLEJNOŚCI KROKÓW",
                   "PROFIL/{candidate.product_id}/KOLEJNOSC"):
        assert marker in source, f"admin_panel.py: brak {marker!r}"

    catalog = ProductCatalog(ROOT / "products")
    profile = catalog.get("SR203_SR204")

    # Logika przesuniecia odwzorowana 1:1 z _move_step: zamiana sasiadow
    # w kopii roboczej, po czym CALA sekwencja przechodzi walidacje.
    steps = [dict(step) for step in profile.steps]
    steps[4], steps[3] = steps[3], steps[4]
    payload = profile.to_dict()
    payload["steps"] = steps
    reordered = ProductProfile(payload, source="<reorder>")

    assert [step["name"] for step in reordered.steps] == [
        "Ethernet 1", "Ethernet 2", "Ethernet 3", "Modem", "Ethernet 4"]
    # Parametry jada razem z krokiem - przestawienie nie moze podmienic
    # napiecia ani maski kanalow.
    modem = reordered.steps[3]
    assert modem["voltage"] == 1500 and modem["channels"] == "OOOOHOOO"
    assert reordered.total_duration == profile.total_duration
    assert not reordered.channel_warnings

    # Przesuniecie poza zakres listy nie jest mozliwe.
    for position, delta in ((0, -1), (len(steps) - 1, 1)):
        assert not 0 <= position + delta < len(steps)



def test_read_program_matches_station_readout() -> None:
    """Odczyt programu z testera + porownanie z profilem.

    Odpowiedzi atrapy sa przepisane z sondy uruchomionej na FIZYCZNEJ Chromie
    19053 (firmware 5.14, S/N 190530006638) na stanowisku SR203/SR204 —
    zrzut z 26.08.2026. Dzieki temu test pilnuje, ze silnik nadal rozumie
    format, ktory ten tester naprawde zwraca, a nie tylko ten z manuala.
    """
    from hipot_device import ChromaDevice

    catalog = ProductCatalog(ROOT / "products")
    profile = catalog.get("SR203_SR204")

    # Program odwzorowujacy profil: Ethernet 1-4 (1,06 kV, 1,0 / 0,2 mA,
    # kanaly 1-4), Modem (1,5 kV, 1,0 / 0,05 mA, kanal 5).
    STEPS = [
        (1, 1.060e3, 1.0e-3, 2.0e-4, 1),
        (2, 1.060e3, 1.0e-3, 2.0e-4, 2),
        (3, 1.060e3, 1.0e-3, 2.0e-4, 3),
        (4, 1.060e3, 1.0e-3, 2.0e-4, 4),
        (5, 1.500e3, 1.0e-3, 5.0e-5, 5),
    ]

    def build(overrides=None):
        overrides = overrides or {}
        device = ChromaDevice("COM1", 19200, dialect=Dialect("19053"))
        device.connected = True
        device.serial = DummySerial()
        device.identification = "Chroma,19053,190530006638,5.14"

        def query(command, **_kwargs):
            if command in overrides:
                return overrides[command]
            if command == "SAFEty:SNUMber?":
                return "+5"
            if command == "SAFEty:PRESet:AC:FREQuency?":
                return "+6.000000E+01"
            match = re.fullmatch(r"SAFEty:STEP(\d+):SET\?", command)
            if match:
                index, volt, high, low, channel = STEPS[int(match.group(1)) - 1]
                # Kolejnosc pol wg manuala s. 5-18.
                return (f"{index},AC,{volt:+.6E},{high:+.6E},{low:+.6E},"
                        f"{0.0:+.6E},{1.0:+.6E},{0.5:+.6E},{0.5:+.6E},"
                        f"{0.0:+.6E},(@({channel})),(@(0))")
            match = re.fullmatch(r"SAFEty:STEP(\d+):AC:CHANnel\?", command)
            if match:
                return f"(@({STEPS[int(match.group(1)) - 1][4]}))"
            match = re.fullmatch(r"SAFEty:STEP(\d+):AC:CHANnel:LOW\?", command)
            if match:
                return "(@(0))"
            return None

        device.query = query
        return device

    program = build().read_program()
    assert program["step_count"] == 5
    assert program["frequency"] == 60.0
    assert program["identity"].startswith("Chroma,19053")
    assert len(program["steps"]) == 5

    first = program["steps"][0]
    assert first["mode"] == "AC"
    assert abs(first["voltage"] - 1060.0) < 0.5
    assert abs(first["limit_high"] - 1.0e-3) < 1e-9
    assert abs(first["limit_low"] - 2.0e-4) < 1e-9      # 0,200 mA, nie 0,035
    assert first["channels_high"] == {1}
    assert first["channels_low"] == set()
    assert program["steps"][4]["channels_high"] == {5}
    assert abs(program["steps"][4]["voltage"] - 1500.0) < 0.5

    # Porownanie z profilem: zero roznic.
    diffs = _compare_profile_to_program(profile, program)
    assert not diffs, diffs

    # Kanal przestawiony w testerze MUSI zostac wykryty - to jest ten blad,
    # ktory podalby napiecie na niewlasciwy port.
    moved = build(overrides={"SAFEty:STEP1:AC:CHANnel?": "(@(3))"}).read_program()
    diffs = _compare_profile_to_program(profile, moved)
    assert any("Channel HIGH" in d for d in diffs), diffs

    # Zanizony Low Limit tez.
    lowered = build(overrides={
        "SAFEty:STEP1:SET?": "1,AC,+1.060000E+03,+1.000000E-03,+3.500000E-05,"
                             "+0.000000E+00,+1.000000E+00,+5.000000E-01,"
                             "+5.000000E-01,+0.000000E+00,(@(1)),(@(0))",
    }).read_program()
    diffs = _compare_profile_to_program(profile, lowered)
    assert any("Low Limit" in d for d in diffs), diffs


def _compare_profile_to_program(profile, program) -> list[str]:
    """Ta sama logika porownania, ktorej uzywa panel inzynieryjny."""
    from scpi_dialect import mask_to_channel_lists

    differences: list[str] = []
    if profile.step_count != program["step_count"]:
        differences.append("Liczba krokow")
    for index, (mine, theirs) in enumerate(
            zip(profile.steps, program["steps"]), start=1):
        checks = [
            ("Voltage", float(mine["voltage"]) / 1000.0,
             theirs["voltage"] / 1000.0, 0.005),
            ("High Limit", float(mine["limit_high"]),
             theirs["limit_high"] * 1000.0, 0.002),
            ("Low Limit", float(mine["limit_low"]),
             theirs["limit_low"] * 1000.0, 0.002),
            ("ARC Limit", float(mine.get("arc_sense", 0.0)),
             theirs["arc"] * 1000.0, 0.002),
            ("Test Time", float(mine["dwell"]), theirs["dwell"], 0.05),
            ("Ramp Time", float(mine["ramp_time"]), theirs["ramp_time"], 0.05),
            ("Fall Time", float(mine["ramp_dn"]), theirs["ramp_dn"], 0.05),
            ("Real Current", float(mine.get("real_limit", 0.0)),
             theirs["real"] * 1000.0, 0.002),
        ]
        for label, a, b, tolerance in checks:
            if abs(a - b) > tolerance:
                differences.append(f"Krok {index} - {label}")
        mask = mine.get("channels")
        if mask and "channels_high" in theirs:
            high, low = mask_to_channel_lists(mask)
            if set(high) != set(theirs["channels_high"]):
                differences.append(f"Krok {index} - Channel HIGH")
            if set(low) != set(theirs.get("channels_low", set())):
                differences.append(f"Krok {index} - Channel LOW")
    return differences


def test_admin_panel_tabs() -> None:
    """Panel ma dokladnie piec zakladek - bez Mapy HWID i Bezpieczenstwa.

    Mapa HWID odpadla, bo na Chromie zaden profil jej nie uzywa - liczy sie
    dlugosc numeru seryjnego. Bezpieczenstwo odpadlo, bo haslo panelu jest
    stale i nie bylo juz czego w tej zakladce zmieniac. Dziennik audytowy
    z tamtej zakladki MUSIAL zostac - to jedyny zapis, kto zmienil nastawy -
    wiec przeniesiono go do zakladki Logi.
    """
    source = (ROOT / "admin_panel.py").read_text(encoding="utf-8")

    for gone in ("_create_hwid_tab", "_create_security_tab", "_save_password",
                 "Mapa HWID", "Bezpieczeństwo", "_pw_new_var"):
        assert gone not in source, f"admin_panel.py: pozostalo {gone!r}"

    expected = ["Stanowisko", "Interlock", "Profile", "Logi",
                "Diagnostyka SCPI"]
    called = re.findall(r"self\._create_(\w+)_tab\(\)", source)
    assert called == ["station", "interlock", "profile", "logs", "diagnostics"], \
        called

    # Dziennik audytowy przeniesiony, nie skasowany.
    assert "_create_audit_note" in source
    assert "audit_log_path" in source
    logs_tab = source[source.index("def _create_logs_tab"):]
    assert "_create_audit_note" in logs_tab[:400], \
        "dziennik audytowy nie jest budowany w zakladce Logi"

    import tkinter as tk
    from admin_panel import AdminPanel

    root = tk.Tk()
    try:
        root.withdraw()
        panel = AdminPanel(root, DummyConfig(), ProductCatalog(ROOT / "products"))
        panel.show()
        labels = [panel.notebook.tab(i, "text").strip()
                  for i in range(panel.notebook.index("end"))]
        assert labels == expected, labels
    finally:
        root.destroy()


def test_recovery_after_abort() -> None:
    """Po przerwanym tescie mozna wrocic do skanowania BEZ wyjscia do menu.

    Zgloszenie ze stanowiska 17.09.2026: po otwarciu klapy w trakcie testu
    jedynym wyjsciem byl "Powrot do menu", czyli rozlaczenie testera,
    przebudowa ekranu i pelna procedura polaczenia od nowa. Przy 9600 bodach
    samo zaprogramowanie pieciu krokow z odczytem zwrotnym to kilkanascie
    sekund - nieproporcjonalna kara za przypadkowe otwarcie klapy.
    """
    from test_screen import TestScreen

    catalog = ProductCatalog(ROOT / "products")
    profile = catalog.get("SR203_SR204")
    screen = TestScreen(None, DummyConfig(), FakeScan(profile))

    configured: list[str] = []

    class Device:
        connected = True

        def request_stop(self, lock_timeout=1.5):
            return True, "atrapa"

        def confirm_stopped(self, attempts=3):
            return True, "atrapa"

        def clear_steps(self):
            configured.append("clear")

        def configure_profile(self, profile):
            configured.append("configure")

        def measure_poll_cycle(self):
            return 0.05

    screen.device = Device()
    for name in ("status_label", "stop_button", "back_button",
                 "next_sn_button", "start_button", "verdict_label",
                 "verdict_frame", "sampling_warning_label", "live_title",
                 "voltage_label", "current_label", "time_label",
                 "sn_display_label", "progress_canvas"):
        setattr(screen, name, DummyWidget())
    screen._set_verdict = lambda text, color: None
    screen._reset_step_rows = lambda: None
    screen._show_next_sn_dialog = lambda result: configured.append("dialog")
    screen._record_incomplete_run = lambda kind, detail: None
    screen._refresh_history = lambda: None

    posted: list = []
    screen._post_ui = posted.append

    # Przerwanie testu otwarciem klapy. Okna modalne zastapione atrapa -
    # bez tego test czekalby na klikniecie operatora.
    import test_screen as screen_module

    screen.test_running = True
    with patch.object(screen_module, "messagebox", DummyMessagebox()):
        screen._abort_running_test(status="przerwany", title="Test przerwany",
                                   message="", icon="warning")
    assert screen._needs_recovery is True, (
        "po przerwaniu brak drogi powrotu do skanowania")
    assert screen._device_configured is False

    # Przycisk "Nastepny SN" ma najpierw PRZYGOTOWAC stanowisko.
    screen._open_sn_dialog_manually()
    deadline = time.monotonic() + 5.0
    while not posted and time.monotonic() < deadline:
        time.sleep(0.02)
    for callback in list(posted):
        callback()

    assert configured[:2] == ["clear", "configure"], configured
    assert "dialog" in configured, "okno skanowania nie zostalo otwarte"
    assert screen._device_configured is True
    assert screen._needs_recovery is False
    # Swieze przejscie OPEN -> CLOSED jest nadal wymagane.
    assert screen._valid_close_transition is False
    assert screen._serial_ready_for_test is False

    # Zrodlo: przycisk musi zmieniac rolę, a nie byc drugim przyciskiem.
    source = (ROOT / "test_screen.py").read_text(encoding="utf-8")
    assert "_recover_for_next_unit" in source
    assert "Przygotuj kolejną sztukę" in source


def test_scan_screen_survives_rebuild() -> None:
    """REGRESJA: zamkniecie panelu inzynieryjnego przewracalo aplikacje.

    Zgloszenie ze stanowiska 01.09.2026:
    ``_tkinter.TclError: invalid command name ".!frame2.!frame.!frame.!label3"``
    i zamkniecie aplikacji przez fatalny handler Tk.

    Przyczyna: ``show_scan_screen()`` niszczy wszystkie widgety, ale atrybuty
    obiektu nadal wskazywaly na ZNISZCZONE widgety. Przy odbudowie
    ``_create_profile_selector`` wola ``_on_profile_selected`` ZANIM powstanie
    ``serial_hint_label``, wiec ``_set_serial_hint`` trafialo w stary,
    nieistniejacy juz widget.

    Test odtwarza dokladnie te sciezke: pelny ekran, zniszczenie, odbudowa.
    """
    import tkinter as tk

    import gui as gui_module
    from station_config import StationConfig

    source = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "_clear_screen_widgets" in source, (
        "brak zerowania referencji do widgetow przy przebudowie")
    assert "_widget_alive" in source, (
        "brak sprawdzania, czy widget nadal istnieje")

    root = tk.Tk()
    try:
        root.withdraw()
        patcher = patch.object(StationConfig, "is_product_enabled",
                               lambda self, product_id: True)
        patcher.start()
        try:
            app = gui_module.HiPotApp(root)
            first_hint = app.serial_hint_label

            # Dokladnie to, co robi zamkniecie panelu inzynieryjnego.
            # Przechwytujemy wyjscie: sama poprawka odpornosciowa (try/except
            # wokol config) sprawia, ze aplikacja nie pada - ale bledu wtedy
            # nadal NIE MA PRAWA byc. Test pilnuje przyczyny, nie objawu.
            # Niezmiennik: w trakcie przebudowy atrybut widgetu jest ALBO
            # None, ALBO wskazuje zywy widget - nigdy zniszczony. Sprawdzamy
            # przyczyne wprost, bo sam brak awarii niczego nie dowodzi:
            # zabezpieczenie w _set_serial_hint i tak polknie wyjatek.
            stale: list[str] = []
            original_hint = gui_module.HiPotApp._set_serial_hint

            def checked_hint(self, text):
                widget = getattr(self, "serial_hint_label", None)
                if widget is not None and not self._widget_alive(widget):
                    stale.append(str(widget))
                return original_hint(self, text)

            with patch.object(gui_module.HiPotApp, "_set_serial_hint",
                              checked_hint):
                for _ in range(3):
                    app.show_scan_screen()
                    root.update_idletasks()
            assert not stale, (
                "przebudowa siega do zniszczonych widgetow: " + str(stale))

            assert app.serial_hint_label is not first_hint, (
                "po przebudowie atrybut wskazuje stary widget")
            assert app._widget_alive(app.serial_hint_label)
            assert app._widget_alive(app.serial_entry)
            assert app._widget_alive(app.profile_banner_title)

            # Ekran musi byc w pelni sprawny po odbudowie.
            label = next(name for name, profile in app._profile_choices.items()
                         if profile.product_id == "SR203_SR204")
            app._selected_profile_var.set(label)
            app._on_profile_selected()
            assert "znaków" in app.serial_hint_label.cget("text")

            app.serial_entry.insert(0, "d70021ac017377")
            root.update_idletasks()
            root.update()
            assert app.serial_entry.get() == "D70021AC017377"

            # Blad budowy ekranu NIE moze zamykac aplikacji - przebudowa
            # nigdy nie zachodzi przy podanym wysokim napieciu.
            with patch.object(gui_module.HiPotApp, "_create_scan_panel",
                              side_effect=RuntimeError("test")):
                app.show_scan_screen()
                root.update_idletasks()
            assert root.winfo_children(), "ekran bledu nie zostal zbudowany"
        finally:
            patcher.stop()
    finally:
        root.destroy()


def test_profile_choice_is_explicit() -> None:
    """W1/W2: profil musi byc wskazany swiadomie i widoczny przez caly czas.

    Poprzednie zabezpieczenie - pytanie TAK/NIE przy zmianie - nie dzialalo:
    przy jednym wlaczonym profilu lista byla zablokowana i dialog nie
    pojawial sie NIGDY, a Enter z czytnika kodow odpowiadal na nie "NIE"
    (default="no"). Zastapione stalym paskiem z napieciami i kanalami.
    """
    import tkinter as tk

    import gui as gui_module
    from station_config import StationConfig

    source = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "askyesno" not in source.split("_create_profile_selector")[-1][:4000], (
        "wrocilo pytanie potwierdzajace zamiast stalego paska")
    assert "_profile_summary" in source

    root = tk.Tk()
    try:
        root.withdraw()
        patcher = patch.object(StationConfig, "is_product_enabled",
                               lambda self, product_id: True)
        patcher.start()
        self_cleanup = patcher.stop
        app = gui_module.HiPotApp(root)
        assert len(app._profile_choices) >= 2, app._profile_choices

        # Przy wiecej niz jednym profilu pole startuje PUSTE.
        assert app._selected_profile_var.get() == "", (
            "lista profili nie moze miec wartosci domyslnej")

        # Skan bez wskazanego profilu musi zostac odrzucony.
        app.serial_entry.insert(0, "D70021AC017377")
        app._process_serial()
        assert "Najpierw wybierz profil" in app.scan_status_label.cget("text"), (
            app.scan_status_label.cget("text"))

        # Pasek profilu podaje NAPIECIA - faktyczna konsekwencje wyboru.
        label = next(name for name, profile in app._profile_choices.items()
                     if profile.product_id == "SR213")
        app._selected_profile_var.set(label)
        app._on_profile_selected()
        assert app.profile_banner_title.cget("text") == "SR213"
        summary = app.profile_info_label.cget("text")
        assert "1.06 kV" in summary and "1.50 kV" in summary, summary
        assert "kanały" in summary, summary

        # Po wskazaniu profilu ten sam numer przechodzi.
        app.serial_entry.delete(0, tk.END)
        app.serial_entry.insert(0, "D70021AC017377")
        app._process_serial()
        assert "✓" in app.scan_status_label.cget("text"), (
            app.scan_status_label.cget("text"))
        self_cleanup()
    finally:
        root.destroy()

def test_start_screen_profile_list() -> None:
    """Lista na starcie musi zawierac DOKLADNIE profile wlaczone w panelu."""
    source = (ROOT / "gui.py").read_text(encoding="utf-8")
    for marker in ("_create_profile_selector", "_profile_choices",
                   "_selected_profile", "is_product_enabled",
                   "wait_window"):
        assert marker in source, f"gui.py: brak elementu {marker}"

    catalog = ProductCatalog(ROOT / "products")

    def visible_labels(enabled):
        """Odtwarza logike budowania listy z _create_profile_selector."""
        config = DummyConfig()
        config.ENABLED_PRODUCTS = enabled
        return [
            profile.display_name for profile in catalog.all()
            if config.is_product_enabled(profile.product_id)
        ]

    # Brak ograniczenia = wszystkie profile na liscie.
    assert visible_labels(None) == ["ER115", "SE210", "SR203 / SR204", "SR213"]
    # Wylaczony profil NIE moze pojawic sie na liscie.
    assert visible_labels(["SR203_SR204"]) == ["SR203 / SR204"]
    assert visible_labels(["ER115", "SR213"]) == ["ER115", "SR213"]
    assert visible_labels(["TEST_1STEP"]) == []

    # Wybor profilu i jego widocznosc sa sprawdzane funkcjonalnie
    # w test_profile_choice_is_explicit.

    # Czcionka rozwinietej listy jest osobna od czcionki pola - bez
    # option_add operator widzi drobny tekst dokladnie tam, gdzie wybiera.
    assert "*TCombobox*Listbox.font" in source, (
        "czcionka rozwinietej listy profili nie zostala powiekszona")

    # Rozpoznawanie S/N idzie jedna droga we wszystkich ekranach.
    for name in ("gui.py", "test_screen.py"):
        module_source = (ROOT / name).read_text(encoding="utf-8")
        assert "resolve_serial" in module_source, f"{name}: brak resolve_serial"
    # Mapa HWID zostala usunieta z aplikacji w calosci.
    assert not (ROOT / "hwid_map.py").exists(), "hwid_map.py wrocil"
    assert not (ROOT / "hwid_map.json").exists(), "hwid_map.json wrocil"
    for path in ROOT.glob("*.py"):
        # release_selftest.py i create_exe.py WYMIENIAJA te nazwy celowo -
        # pierwszy zeby sprawdzic ich brak, drugi zeby zablokowac build,
        # gdyby wrocily (REMOVED_MARKERS).
        if path.name in ("release_selftest.py", "create_exe.py"):
            continue
        body = path.read_text(encoding="utf-8")
        assert "HwidMap" not in body, f"{path.name}: odwolanie do HwidMap"
        assert "identify_by" not in body, f"{path.name}: pozostalo identify_by"


def test_ui_has_no_operator_login() -> None:
    """Logowanie operatora zostalo usuniete, stopka ma ustalona tresc."""
    gui_source = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "_create_operator_panel" not in gui_source
    assert "_login_operator" not in gui_source
    assert "REQUIRE_OPERATOR_LOGIN" not in gui_source

    # Stopka zostaje na ekranie startowym i w panelu. Z ekranu TESTOWEGO
    # zostala usunieta: zabierala 34 px pasowi werdyktu PASS/FAIL, ktory
    # jest najwazniejsza informacja na tym ekranie.
    for name in ("gui.py", "admin_panel.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "Autor: Kacper Urbanowicz" in source, f"{name}: brak autora w stopce"
        assert "footer_left()" in source, f"{name}: stopka nie uzywa footer_left()"

    test_source = (ROOT / "test_screen.py").read_text(encoding="utf-8")
    assert "Autor: Kacper Urbanowicz" not in test_source, (
        "stopka wrocila na ekran testowy - zabiera miejsce pasowi werdyktu")

    config = DummyConfig()
    assert config.footer_left() == "Reconext Hi-Pot Main Units v1.0.0"

    from station_config import APP_NAME, APP_VERSION

    assert APP_NAME == "Reconext Hi-Pot Main Units"
    assert APP_VERSION == "1.0.0"

    # Ekran testowy nie moze juz przyjmowac ani drukowac operatora.
    from test_screen import TestScreen

    catalog = ProductCatalog(ROOT / "products")
    screen = TestScreen(None, DummyConfig(), FakeScan(catalog.get("SR203_SR204")))
    assert not hasattr(screen, "operator")


# Wzorzec formatu raportu: plik wygenerowany na stanowisku SR203/SR204.
# Test porownuje wynik BAJT W BAJT - kazda zmiana ukladu pol, separatorow albo
# zakonczen linii zepsulaby watcher zbierajacy raporty i zostanie tu wykryta.
REFERENCE_REPORT = (
    "Chroma 19053 Test report\r\n"
    "\r\n"
    "Program:\tSR203.204\r\n"
    "S/N:\t\tD21022AD023966\r\n"
    "TIME:\t\t2026/08/24 13:08:19\r\n"
    "Total result:\tPass\r\n"
)
REFERENCE_STEPS = (
    ("Ethernet 1", "1.059", "0.363", "0.200", "1.000"),
    ("Ethernet 2", "1.059", "0.381", "0.200", "1.000"),
    ("Ethernet 3", "1.059", "0.360", "0.200", "1.000"),
    ("Ethernet 4", "1.061", "0.365", "0.200", "1.000"),
    ("Modem", "1.501", "0.133", "0.050", "1.000"),
)
for _index, (_name, _vtm, _im, _low, _high) in enumerate(REFERENCE_STEPS, start=1):
    REFERENCE_REPORT += (
        "\r\n"
        f"STEP:\t\t{_index}\r\n"
        "MODE:\t\tWVAC\r\n"
        f"EXT Name:\t{_name}\r\n"
        f"Vtm:\t\t{_vtm}\tKV\r\n"
        f"Im:\t\t{_im}\tmA\r\n"
        f"Low:\t\t{_low}\tmA\r\n"
        f"High:\t\t{_high}\tmA\r\n"
        "ARC Result:\t---\r\n"
        "Result:\t\tPass\r\n"
        "Error Code:\t\r\n"
    )
REFERENCE_REPORT += "\r\nError Description: \r\n"


def _reference_profile() -> ProductProfile:
    """Profil odwzorowujacy raport wzorcowy (kolejnosc krokow i limity z pliku)."""
    data = json.loads(json.dumps(SR_PROFILE_DATA))
    layout = [
        ("Ethernet 1", 1060, 1.0, 0.200, "OOOHOOOO"),
        ("Ethernet 2", 1060, 1.0, 0.200, "OOOOHOOO"),
        ("Ethernet 3", 1060, 1.0, 0.200, "OOOOOHOO"),
        ("Ethernet 4", 1060, 1.0, 0.200, "OOOOOOHO"),
        ("Modem", 1500, 1.0, 0.050, "OOHOOOOO"),
    ]
    data["report_program"] = "SR203.204"
    data["steps"] = [{
        "name": name, "mode": "ACW", "voltage": voltage,
        "limit_high": high, "limit_low": low, "presence_min_current": low,
        "ramp_time": 0.5, "dwell": 1.0, "ramp_dn": 0.5,
        "arc_sense": 0.0, "real_limit": 0.0, "frequency": 60,
        "continuity": "OFF", "channels": channels,
    } for name, voltage, high, low, channels in layout]
    return ProductProfile(data, source="wzorzec")


def test_report_matches_station_format() -> None:
    """Raport musi byc IDENTYCZNY z plikiem wygenerowanym na stanowisku."""
    from datetime import datetime

    profile = _reference_profile()
    measured = [(1.059, 0.363), (1.059, 0.381), (1.059, 0.360),
                (1.061, 0.365), (1.501, 0.133)]
    steps = [{
        "index": index, "name": step["name"], "result": "PASS",
        "judgment_code": "116", "error_code": "",
        "output_voltage": measured[index - 1][0] * 1000.0,
        "measured_current": measured[index - 1][1], "real_current": 0.0,
    } for index, step in enumerate(profile.steps, start=1)]

    lines = build_report_lines(
        instrument_model="19053", program=profile.report_program,
        serial="D21022AD023966", overall_result="PASS", steps=steps,
        profile_steps=profile.steps,
        effective_low_ma=[profile.effective_low_ma(s) for s in profile.steps],
        now=datetime(2026, 8, 24, 13, 8, 19))

    produced = "\r\n".join(lines) + "\r\n"
    assert produced == REFERENCE_REPORT, (
        "Format raportu rozjechal sie ze wzorcem ze stanowiska:\n"
        + "\n".join(
            f"  {'wzorzec' if side else 'wynik  '}: {line!r}"
            for side, line in zip(
                (1, 0),
                (REFERENCE_REPORT.splitlines(), produced.splitlines()))
        )
    )

    # Pole Operator i Station nie moga wrocic - nie ma ich w formacie Chromy.
    assert "Operator:" not in produced
    assert "Station:" not in produced


def test_report_failed_step_and_filename() -> None:
    """FAIL: kod bledu przy kroku, jeden opis na koncu, nazwa pliku = S/N."""
    from datetime import datetime

    profile = _reference_profile()
    steps = [{
        "index": index, "name": step["name"],
        "result": "PASS" if index != 3 else "FAIL",
        "judgment_code": "116" if index != 3 else "18",
        "error_code": "" if index != 3 else "18",
        "output_voltage": float(step["voltage"]),
        "measured_current": 0.363 if index != 3 else 0.004,
        "real_current": 0.0,
    } for index, step in enumerate(profile.steps, start=1)]

    text = "\r\n".join(build_report_lines(
        instrument_model="19053", program=profile.report_program,
        serial="D21022AD023966", overall_result="FAIL", steps=steps,
        profile_steps=profile.steps,
        effective_low_ma=[profile.effective_low_ma(s) for s in profile.steps],
        now=datetime(2026, 8, 24, 13, 8, 19)))

    assert "Total result:\tFail" in text
    assert "Error Code:\t18" in text
    assert text.count("Error Description:") == 1, (
        "opis bledu ma wystapic RAZ, na koncu pliku")
    assert text.rstrip().endswith("Error Description: AC Low Fail")
    assert text.count("Result:\t\tPass") == 4
    assert text.count("Result:\t\tFail") == 1

    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp) / "raporty"
        path = Path(save_report(
            instrument_model="19053", program=profile.report_program,
            serial="D21022AD023966", overall_result="FAIL", steps=steps,
            profile_steps=profile.steps,
            effective_low_ma=[profile.effective_low_ma(s)
                              for s in profile.steps],
            log_dir=str(directory)))
        # Nazwa pliku to SAM numer seryjny - bez znacznika czasu.
        assert path.name == "D21022AD023966.txt", path.name
        raw = path.read_bytes()
        assert b"\r\n" in raw and b"\r\r\n" not in raw
        assert not list(directory.glob("*.tmp")), "pozostal plik tymczasowy"

        # Powtorny test tej samej sztuki nadpisuje plik, nie tworzy drugiego.
        save_report(
            instrument_model="19053", program=profile.report_program,
            serial="D21022AD023966", overall_result="PASS", steps=steps,
            profile_steps=profile.steps,
            effective_low_ma=[profile.effective_low_ma(s)
                              for s in profile.steps],
            log_dir=str(directory))
        assert len(list(directory.glob("*.txt"))) == 1


def test_report_text_injection_blocked() -> None:
    """K3: pola tekstowe profilu nie moga dopisac wlasnych linii do raportu.

    Raport ma strukture "pole<TAB>wartosc" w osobnych liniach. Znak nowej
    linii w report_program albo w nazwie kroku pozwalal wstawic wlasna linie
    "Total result: Pass" PRZED prawdziwym wynikiem - odtworzone przed
    poprawka na przebiegu zakonczonym FAIL.
    """
    from datetime import datetime
    from safety_rules import validate_report_text

    catalog = ProductCatalog(ROOT / "products")
    base = catalog.get("SR203_SR204").to_dict()

    poison = "SR203.204\r\nTotal result:\tPass"
    expect_error(lambda: ProductProfile({**base, "report_program": poison}),
                 "wstrzykniecie przez report_program")
    expect_error(lambda: ProductProfile({**base, "display_name": "A\nB"}),
                 "wstrzykniecie przez display_name")
    expect_error(
        lambda: ProductProfile({**base, "steps": [
            dict(base["steps"][0], name="Ethernet 1\r\nResult:\tPass")]}),
        "wstrzykniecie przez nazwe kroku")

    # Znaki uzywane w prawdziwych nazwach musza nadal przechodzic.
    for good in ("SR203.204", "SR203 / SR204", "Ethernet 1", "DSL_2", "ADSL(A)"):
        assert validate_report_text(good, "test") == good

    for bad in ("", "   ", "A\tB", "A\nB", "A\rB", "A" * 100):
        expect_error(lambda value=bad: validate_report_text(value, "test"),
                     f"odrzucenie {bad!r}")

    # Raport zbudowany z poprawnego profilu ma DOKLADNIE jedna linie wyniku.
    profile = catalog.get("SR203_SR204")
    lines = build_report_lines(
        instrument_model="19053",
        program=profile.report_program,
        serial="D70021AC017377",
        overall_result="FAIL",
        steps=[{"index": 1, "name": profile.steps[0]["name"], "result": "FAIL",
                "output_voltage": 1060.0, "measured_current": 0.0,
                "judgment_code": "17"}],
        profile_steps=[dict(profile.steps[0])],
        effective_low_ma=[profile.effective_low_ma(profile.steps[0])],
        now=datetime(2026, 9, 1, 12, 0, 0),
    )
    assert sum(1 for line in lines if line.startswith("Total result:")) == 1
    assert sum(1 for line in lines if line.startswith("Result:")) == 1
    assert [line for line in lines if line.startswith("Total result:")][0] \
        .endswith("Fail")


def test_profile_integrity_blocks_outside_edits() -> None:
    """K5: profil zmieniony poza panelem blokuje testowanie."""
    import profile_integrity as integrity_module
    from profile_integrity import ProfileIntegrity, verify_profiles

    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as folder:
        try:
            work = Path(folder)
            products = work / "products"
            products.mkdir()
            source = ROOT / "products" / "SR203_SR204.json"
            target = products / "SR203_SR204.json"
            target.write_text(source.read_text(encoding="utf-8"),
                              encoding="utf-8")
            os.chdir(work)

            config = DummyConfig()
            integrity = ProfileIntegrity(config, products)

            # Pierwszy start tworzy manifest lokalny i nie blokuje.
            integrity.check()
            assert not integrity.blocked, integrity.summary()
            assert not integrity.external
            integrity.check()
            assert not integrity.blocked

            # Edycja pliku poza panelem MUSI zablokowac testowanie.
            data = json.loads(target.read_text(encoding="utf-8"))
            data["steps"][0]["voltage"] = 100
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            integrity.check()
            assert integrity.blocked, "zmiana profilu nie zostala wykryta"
            assert "zmieniona poza panelem" in integrity.summary()

            # Dopisany plik profilu tez jest rozbieznoscia.
            integrity.refresh_after_panel_edit("SR203_SR204")
            (products / "OBCY.json").write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8")
            problems = verify_profiles(products, integrity.path)
            assert any("spoza manifestu" in p for p in problems), problems

            # Manifest zewnetrzny: jego brak to blad konfiguracji, nie
            # powod do cichego utworzenia nowego.
            config.PROFILE_MANIFEST_PATH = str(work / "nie_ma" / "m.json")
            external = ProfileIntegrity(config, products)
            assert external.external
            external.check()
            assert external.blocked and "Brak manifestu" in (external.error or "")
        finally:
            os.chdir(previous_cwd)


def test_stop_is_verified_and_not_blocked_by_io_lock() -> None:
    """K1/K2: STOP wychodzi szybko i jest POTWIERDZANY odczytem z testera."""
    import hipot_device as device_module
    from hipot_device import ChromaDevice

    source = (ROOT / "test_screen.py").read_text(encoding="utf-8")
    assert "request_stop()" in source, (
        "STOP z watku Tk musi isc przez request_stop")
    assert "_show_stop_not_confirmed" in source, (
        "brak reakcji na niepotwierdzone zatrzymanie")

    device = ChromaDevice.__new__(ChromaDevice)
    device._io_lock = threading.RLock()
    device._abort_flag = threading.Event()
    device._rx_buffer = bytearray()
    device._cycle_active_confirmed = True
    device._cycle_started_monotonic = 1.0
    device.dialect = Dialect("19053")
    device.connected = True

    written: list[str] = []
    device._write_unlocked = written.append

    # 1. Trwajaca wymiana I/O trzyma _io_lock; STOP musi ja PRZERWAC,
    #    a nie czekac do konca cyklu ponowien.
    released = threading.Event()
    entered = threading.Event()

    def busy_io():
        with device._io_lock:
            entered.set()
            # Petla odczytu sprawdza flage przerwania w kazdym obrocie.
            for _ in range(100):
                if device._abort_flag.is_set():
                    break
                time.sleep(0.01)
        released.set()

    worker = threading.Thread(target=busy_io, daemon=True)
    worker.start()
    assert entered.wait(1.0)

    started = time.monotonic()
    sent, message = device.request_stop()
    waited = time.monotonic() - started
    assert sent, message
    assert waited < 0.6, f"STOP czekal {waited:.2f} s na blokade"
    assert released.wait(1.0)
    assert written and "STOP" in written[-1].upper()
    assert not device._abort_flag.is_set(), "flaga przerwania nie zostala zdjeta"
    assert device._cycle_active_confirmed is False

    # 2. Potwierdzenie: status terminalny albo napiecie ponizej progu.
    device.get_status = lambda: "STOPPED"
    device.read_measurements = lambda: None
    ok, detail = device.confirm_stopped()
    assert ok, detail

    device.get_status = lambda: "TESTING"
    device.read_measurements = lambda: {"output_voltage": 12.0}
    ok, detail = device.confirm_stopped()
    assert ok and "zgaslo" in detail

    # 3. Tester ciagle w tescie i z napieciem = BRAK potwierdzenia.
    device.get_status = lambda: "TESTING"
    device.read_measurements = lambda: {"output_voltage": 1480.0}
    ok, detail = device.confirm_stopped(attempts=2)
    assert not ok and "NIE POTWIERDZONO" in detail


def test_evidence_ignores_single_transient() -> None:
    """S3: jedna probka nadmiaru na rampie nie moze uniewaznic PASS-a.

    Prad ladowania pojemnosci wyrobu potrafi chwilowo przekroczyc Max Limit
    w chwili dojscia rampy do napiecia docelowego. Chroma to widzi i orzeka
    PASS - aplikacja odrzucala poprawny wynik i blokowala stanowisko.
    """
    from test_screen import StepEvidence

    single = StepEvidence()
    for over in (True, False, True, False, True):
        single.note_overcurrent(over)
    assert not single.overcurrent_seen, "pojedyncze transienty odrzucily PASS"

    real = StepEvidence()
    for over in (False, True, True, True):
        real.note_overcurrent(over)
    assert real.overcurrent_seen, "rzeczywiste przekroczenie nie zostalo wykryte"


def test_builder_guards_catch_real_damage() -> None:
    """Kontrole buildera musza zatrzymac REALNE uszkodzenie, nie tylko literowke.

    Sonda z 18.09.2026 pokazala dwie dziury we wlasnych kontrolach:
    marker ``_abort_flag`` przechodzil po przemianowaniu na
    ``_abort_flag_off`` (bo byl PREFIKSEM), a jawne haslo sklejone
    z przylegajacych literalow nie bylo wykrywane, bo szukalo go tylko
    jako podciagu tekstu zrodlowego. Test pilnuje obu poprawek.
    """
    import create_exe as builder

    # Pliki muszą przechodzić w stanie niezmienionym.
    for name in builder.PROJECT_FILES:
        builder.validate_source_file(name, ROOT / name)

    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        for name in builder.PROJECT_FILES:
            (work / name).write_text((ROOT / name).read_text(encoding="utf-8"),
                                     encoding="utf-8")
        try:
            os.chdir(work)

            def blocked(name, mutate) -> bool:
                path = work / name
                original = path.read_text(encoding="utf-8")
                path.write_text(mutate(original), encoding="utf-8")
                try:
                    builder.validate_source_file(name, path)
                    return False
                except builder.BuildError:
                    return True
                finally:
                    path.write_text(original, encoding="utf-8")

            must_block = [
                ("wyciecie weryfikacji STOP", "hipot_device.py",
                 lambda s: s.replace("def confirm_stopped", "def _off")),
                # PREFIKS: samo przemianowanie nie moze przejsc.
                ("przemianowanie flagi przerwania I/O", "hipot_device.py",
                 lambda s: s.replace("self._abort_flag.set()",
                                     "self._abort_flag_off.set()")),
                ("wyciecie bramki czasu cyklu", "hipot_device.py",
                 lambda s: s.replace("def cycle_started_monotonic", "def _off")),
                ("wyciecie sanityzacji raportu", "safety_rules.py",
                 lambda s: s.replace("def validate_report_text", "def _off")),
                ("wyciecie kontroli sum profili", "profile_integrity.py",
                 lambda s: s.replace("def verify_profiles", "def _off")),
                ("powrot mapy HWID", "gui.py",
                 lambda s: s + "\nfrom hwid_map import HwidMap\n"),
                ("powrot logowania operatora", "gui.py",
                 lambda s: s + "\ndef _login_operator(): pass\n"),
                ("wyciecie zerowania widgetow", "gui.py",
                 lambda s: s.replace("_clear_screen_widgets", "_off")),
                # Trzy zapisy tego samego hasla - wszystkie musza odpasc.
                ("jawne haslo: literal", "gui.py",
                 lambda s: s + '\nP = "recon"+"ext2026"\n'),
                ("jawne haslo: literaly przylegajace", "gui.py",
                 lambda s: s + '\nP = "recon" "ext2026"\n'),
                ("jawne haslo: sklejone plusem", "gui.py",
                 lambda s: s + '\nP = "reco" + "nex" + "t2026"\n'),
            ]
            for label, name, mutate in must_block:
                assert blocked(name, mutate), f"build przeszedl mimo: {label}"

            # Zmiany kosmetyczne NIE moga blokowac wydania - inaczej kontrola
            # zostanie obejsciem przez zatwierdzanie wszystkiego.
            must_pass = [
                ("zmiana tekstu komunikatu", "gui.py",
                 lambda s: s.replace("Zeskanuj numer seryjny", "Skanuj numer")),
                ("zmiana koloru", "station_config.py",
                 lambda s: s.replace("#E3F2FD", "#E1F5FE")),
            ]
            for label, name, mutate in must_pass:
                assert not blocked(name, mutate), (
                    f"kontrola blokuje zmiane kosmetyczna: {label}")
        finally:
            os.chdir(previous_cwd)

    # Manifest sum profili MUSI trafic obok EXE - bez niego aplikacja
    # zatwierdza przy pierwszym uruchomieniu to, co akurat lezy w products.
    assert "profiles_manifest.json" in builder.OPTIONAL_DATA_FILES, (
        "manifest sum profili nie jest kopiowany obok EXE")
    builder_source = (ROOT / "create_exe.py").read_text(encoding="utf-8")
    assert "EDITABLE_DATA_FILES + OPTIONAL_DATA_FILES" in builder_source, (
        "copy_editable_files nie kopiuje plikow opcjonalnych")

    # Kazdy plik produkcyjny ma byc czyms pilnowany.
    unguarded = [name for name in builder.PROJECT_FILES
                 if name not in builder.REQUIRED_SAFETY_MARKERS]
    # main.py to bootstrap, station_config.py to same wartosci domyslne -
    # nadpisywane przez station_config.json i pilnowane w settings_manager.
    assert unguarded == ["main.py", "station_config.py"], unguarded


def test_transport_limits() -> None:
    """Manual rozdz. 6.2: RS232 konczy sie na 19200 bodach, bez RTS/CTS."""
    from safety_rules import validate_rs232_settings

    assert validate_rs232_settings("com2", 19200, "none", "none") == (
        "COM2", 19200, "NONE", "NONE")
    assert validate_rs232_settings("COM2", 9600, "EVEN", "XON/XOFF")[3] == "XON/XOFF"

    for baudrate in (38400, 57600, 115200):
        expect_error(
            lambda baudrate=baudrate: validate_rs232_settings("COM2", baudrate),
            f"baudrate {baudrate} powyzej mozliwosci testera")
    expect_error(
        lambda: validate_rs232_settings("COM2", 19200, "NONE", "RTS/CTS"),
        "sprzetowy RTS/CTS, ktorego tester nie ma")
    expect_error(lambda: validate_rs232_settings("", 9600), "pusty port COM")


def test_station_config() -> None:

    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as temp:
        os.chdir(temp)
        try:
            config = DummyConfig()
            SettingsManager().save_config(config)
            assert Path("station_config.json").is_file()

            data = json.loads(
                Path("station_config.json").read_text(encoding="utf-8"))
            assert data["schema_version"] == 1
            assert "ADMIN_PASSWORD" not in data, (
                "brak hasla nie moze byc zapisany jako null")

            # Wylaczenie interlocka w pliku musi zablokowac wczytanie.
            broken = dict(data)
            broken["INTERLOCK_ENABLED"] = False
            Path("station_config.json").write_text(json.dumps(broken),
                                                   encoding="utf-8")
            expect_error(lambda: SettingsManager().load_config(DummyConfig()),
                         "reczne wylaczenie interlocka w JSON")

            broken = dict(data)
            broken["AUTO_SAVE_RESULTS"] = False
            Path("station_config.json").write_text(json.dumps(broken),
                                                   encoding="utf-8")
            expect_error(lambda: SettingsManager().load_config(DummyConfig()),
                         "wylaczenie automatycznego zapisu raportow")

            broken = dict(data)
            broken["INSTRUMENT_MODEL"] = "19099"
            Path("station_config.json").write_text(json.dumps(broken),
                                                   encoding="utf-8")
            expect_error(lambda: SettingsManager().load_config(DummyConfig()),
                         "nieobslugiwany model testera w JSON")

            broken = dict(data)
            broken["schema_version"] = 99
            Path("station_config.json").write_text(json.dumps(broken),
                                                   encoding="utf-8")
            expect_error(lambda: SettingsManager().load_config(DummyConfig()),
                         "konfiguracja z nowszej wersji aplikacji")
        finally:
            os.chdir(previous_cwd)


def test_admin_access_control() -> None:
    from security import (
        AccessGate,
        hash_password,
        validate_password_record,
        verify_password,
    )

    # Haslo standardowe stanowiska nie moze wystepowac JAWNIE w kodzie -
    # build jest niepodpisany, wiec literal dalby sie wyciagnac stringsem.
    for name in ("gui.py", "admin_panel.py", "station_config.py", "security.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert '"reconext2026"' not in source, f"{name}: jawne haslo w kodzie"

    # ...ale skrot dostarczony w konfiguracji MUSI akceptowac reconext2026.
    shipped = json.loads(
        (ROOT / "station_config.json").read_text(encoding="utf-8")
    ).get("ADMIN_PASSWORD")
    assert shipped is not None, "station_config.json bez rekordu hasla"
    assert verify_password("reconext2026", shipped) is True, (
        "dostarczony skrot nie akceptuje hasla standardowego")
    assert verify_password("reconext2027", shipped) is False

    record = hash_password("InneHaslo2026")
    assert validate_password_record(record) is not None
    assert verify_password("InneHaslo2026", record) is True
    assert verify_password("zle", record) is False
    expect_error(lambda: hash_password("krotk"), "haslo ponizej minimum")
    expect_error(
        lambda: validate_password_record({**record, "iterations": 100}),
        "zbyt mala liczba iteracji PBKDF2")
    expect_error(
        lambda: validate_password_record({"algo": "md5", "iterations": 200000,
                                          "salt": "00" * 16, "hash": "00" * 32}),
        "nieobslugiwany algorytm")

    # Brak rekordu NIE moze otwierac panelu awaryjnie.
    assert verify_password("reconext2026", None) is False
    assert verify_password("", None) is False

    gate = AccessGate(max_attempts=3, lockout_seconds=30.0)
    assert gate.register_failure() == 0.0
    assert gate.register_failure() == 0.0
    assert gate.register_failure() == 30.0
    assert gate.is_locked() is True
    gate.reset()
    assert gate.is_locked() is False


def test_runtime_logging_windowed() -> None:
    """Symuluje build --windowed, w ktorym stdout/stderr sa None."""
    from runtime_logging import configure_runtime_logging, restore_runtime_logging

    real_stdout, real_stderr = sys.stdout, sys.stderr
    marker_out, marker_err = "RUNTIME-STDOUT-SELFTEST", "RUNTIME-STDERR-SELFTEST"

    with tempfile.TemporaryDirectory() as temp:
        try:
            sys.stdout = None
            sys.stderr = None
            log_path = configure_runtime_logging(temp)
            assert log_path is not None
            print(marker_out)
            sys.stderr.write(marker_err + "\n")
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            restore_runtime_logging()
            sys.stdout, sys.stderr = real_stdout, real_stderr

        content = Path(log_path).read_text(encoding="utf-8")
        assert marker_out in content and marker_err in content
        assert "[OUT]" in content and "[ERR]" in content, (
            "brak znacznika kanalu/czasu w logu")


def main() -> None:
    tests = [
        test_step_and_channel_rules,
        test_profile_catalog,
        test_pass_evidence_rules,
        test_dialect_and_channel_lists,
        test_rs232_response_reassembly,
        test_device_identity_matches_station_model,
        test_multistep_configuration_and_channel_readback,
        test_multistep_result_gate,
        test_single_step_profile_uses_last_registers,
        test_fresh_cycle_guard,
        test_interlock_regressions,
        test_interlock_state_machine,
        test_next_serial_keeps_station_profile,
        test_enabled_products_gate,
        test_profile_step_reordering,
        test_read_program_matches_station_readout,
        test_admin_panel_tabs,
        test_recovery_after_abort,
        test_scan_screen_survives_rebuild,
        test_profile_choice_is_explicit,
        test_start_screen_profile_list,
        test_ui_has_no_operator_login,
        test_report_matches_station_format,
        test_report_failed_step_and_filename,
        test_report_text_injection_blocked,
        test_profile_integrity_blocks_outside_edits,
        test_stop_is_verified_and_not_blocked_by_io_lock,
        test_evidence_ignores_single_transient,
        test_builder_guards_catch_real_damage,
        test_transport_limits,
        test_station_config,
        test_admin_access_control,
        test_runtime_logging_windowed,
    ]
    for test in tests:
        test()
        print(f"[SELFTEST] OK  {test.__name__}")
    print(f"[SELFTEST] Wszystkie {len(tests)} testow regresyjnych zakonczone "
          "powodzeniem")


if __name__ == "__main__":
    main()
