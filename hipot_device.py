"""Komunikacja z testerami Chroma 190xx (19052 jednokanalowy, 19053 ze scan boxem).

Warstwa transportowa jest wspolna dla obu modeli; roznice sa wylacznie w
``scpi_dialect``. Zabezpieczenia przeniesione z aplikacji Amidala 1.0.x:

* kazda transakcja pod jedna blokada (STOP z GUI nie wejdzie miedzy zapytanie
  a odczyt odpowiedzi),
* czyszczenie bufora przed zapytaniem,
* skladanie odpowiedzi do KOMPLETNEJ linii (poprawka bledu K2 z audytu),
* bramka "swiezego cyklu" - wynik LAST z poprzedniej sztuki nie moze zostac
  uznany za wynik biezacego testu,
* blokada klawiatury panelu na czas sterowania z aplikacji.

Nowe wzgledem 1.0.x:

* programowanie N krokow zamiast jednego,
* maski kanalow scan boxa z obowiazkowym odczytem zwrotnym,
* wyniki per krok,
* sonda dialektu wykrywajaca nagłowki odrzucane przez firmware.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import serial

from safety_rules import validate_rs232_settings
from scpi_dialect import (
    REQUIRED_COMMANDS,
    REQUIRED_SCAN_COMMANDS,
    Dialect,
    decode_channel_list,
    encode_channel_list,
    mask_to_channel_lists,
)

OVERFLOW = 9.0e37
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")

TERMINAL_STATUSES = {"STOPPED", "STOP", "PASS", "FAIL"}
ACTIVE_STATUSES = {"TESTING", "RUNNING"}


class DeviceError(RuntimeError):
    """Blad komunikacji lub konfiguracji testera."""


class ChromaDevice:

    MAX_RESPONSE_BYTES = 512

    def __init__(self, port: str = "COM6", baudrate: int = 9600,
                 parity: str = "NONE", flow_control: str = "NONE",
                 dialect: Optional[Dialect] = None):
        (self.port, self.baudrate, self.parity,
         self.flow_control) = validate_rs232_settings(
            port, baudrate, parity, flow_control
        )
        self.dialect = dialect or Dialect("19052")
        self.serial: Optional[serial.Serial] = None
        self.connected = False
        self.identification = ""

        self._io_lock = threading.RLock()
        # K1: przerwanie trwajacej wymiany I/O. query() trzyma _io_lock przez
        # caly cykl ponowien (do ~3 s przy milczacym testerze), a STOP po
        # otwarciu klapy czeka na te sama blokade. Flaga pozwala biezacemu
        # odczytowi wyjsc w ciagu jednego timeoutu portu (0,20 s), zamiast
        # trzymac wysokie napiecie na wyrobie przez pelny cykl ponowien.
        self._abort_flag = threading.Event()
        # Powod ostatniego nieudanego polaczenia. Jeden komunikat na cztery
        # rozne sytuacje ("Brak polaczenia z Chroma") kazal technologowi
        # zgadywac, czy to kabel, port zajety przez inna instancje, zly
        # baudrate, czy tester innego modelu.
        self.last_connect_error = ""
        self._rx_buffer = bytearray()
        self._cycle_id = 0
        self._cycle_active_confirmed = False
        self._cycle_started_monotonic: Optional[float] = None
        self._configured_step_count = 0
        # Zmierzony czas jednej iteracji odpytywania - potrzebny, zeby ocenic,
        # czy przy zadanym dwell da sie zebrac wymagane probki obciazenia.
        self.last_poll_interval = 0.0

    # ------------------------------------------------------------------ #
    # POLACZENIE
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        try:
            parity_map = {
                "NONE": serial.PARITY_NONE,
                "ODD": serial.PARITY_ODD,
                "EVEN": serial.PARITY_EVEN,
            }
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=parity_map[self.parity],
                stopbits=serial.STOPBITS_ONE,
                timeout=0.20,
                write_timeout=2.0,
                rtscts=self.flow_control == "RTS/CTS",
                xonxoff=self.flow_control == "XON/XOFF",
            )
            time.sleep(0.5)
            self.connected = True
            self._clear_input()

            response = self.query(self.dialect.command("idn"), timeout=2.0, retries=2)
            if not response:
                self.last_connect_error = (
                    f"Port {self.port} otwarty, ale tester nie odpowiada na "
                    f"*IDN?. Najczestsza przyczyna: baudrate stanowiska "
                    f"({self.baudrate}) inny niz w menu SYSTEM testera, "
                    f"albo kabel wpiety w zly port."
                )
                print("[IDN] Brak odpowiedzi na *IDN?")
                self.disconnect(send_stop=False)
                return False

            parts = [part.strip() for part in response.split(",")]
            if len(parts) < 2 or parts[0].upper() != "CHROMA":
                self.last_connect_error = (
                    f"Na porcie {self.port} odpowiada urzadzenie, ktore nie "
                    f"jest testerem Chroma: '{response}'."
                )
                print(f"[IDN] To nie jest tester Chroma: {response}")
                self.disconnect(send_stop=False)
                return False
            if parts[1].upper() != self.dialect.model:
                self.last_connect_error = (
                    f"Podlaczony tester to Chroma {parts[1]}, a stanowisko jest "
                    f"skonfigurowane pod {self.dialect.model}. Popraw model "
                    f"w panelu (zakladka Stanowisko) albo wepnij wlasciwy kabel."
                )
                print(
                    f"[IDN] Model {parts[1]!r} nie zgadza sie z konfiguracja "
                    f"stanowiska ({self.dialect.model}). Sprawdz INSTRUMENT_MODEL."
                )
                self.disconnect(send_stop=False)
                return False

            self.identification = response
            print(f"[IDN] Polaczono z: {response}")

            if not self._lock_local_keys():
                self.disconnect(send_stop=False)
                return False
            return True

        except Exception as exc:
            text = str(exc)
            if "PermissionError" in type(exc).__name__ or "Access is denied" in text:
                self.last_connect_error = (
                    f"Port {self.port} jest zajety przez inny program. "
                    f"Zamknij druga instancje aplikacji albo terminal "
                    f"szeregowy i sprobuj ponownie."
                )
            elif "could not open port" in text.lower():
                self.last_connect_error = (
                    f"Port {self.port} nie istnieje w systemie. Sprawdz numer "
                    f"portu w Menedzerze urzadzen i w panelu (Stanowisko)."
                )
            else:
                self.last_connect_error = f"Blad otwarcia portu {self.port}: {exc}"
            print(f"[POLACZENIE] Blad: {exc}")
            try:
                if self.serial and self.serial.is_open:
                    self.serial.close()
            except Exception:
                pass
            self.connected = False
            return False

    def _lock_local_keys(self) -> bool:
        """Blokuje klawisze panelu, zeby nastawy nie zmienily sie przed STARTem."""
        self.send_command(self.dialect.command("keylock_on"))
        time.sleep(0.15)
        error = self.query(self.dialect.command("error"), timeout=2.0, retries=2)
        state = self.query(
            self.dialect.command("keylock_query"), timeout=2.0, retries=2,
            validator=self._is_integer,
        )
        print(f"[KEYLOCK] ERR='{error}', STATE='{state}'")
        if not self._error_ok(error) or state is None or int(state) != 1:
            return False
        return True

    def disconnect(self, send_stop: bool = True) -> None:
        with self._io_lock:
            try:
                if self.serial and self.serial.is_open:
                    if send_stop:
                        try:
                            self._write_unlocked(self.dialect.command("stop"))
                            time.sleep(0.08)
                        except Exception:
                            pass
                    try:
                        self._write_unlocked(self.dialect.command("keylock_off"))
                        time.sleep(0.08)
                    except Exception:
                        pass
                    self.serial.close()
            finally:
                self.connected = False

    # ------------------------------------------------------------------ #
    # TRANSPORT
    # ------------------------------------------------------------------ #
    def _require_connection(self) -> None:
        if not self.connected or not self.serial or not self.serial.is_open:
            raise DeviceError("Tester nie jest polaczony")

    def _clear_input(self) -> None:
        self._rx_buffer.clear()
        if self.serial and self.serial.is_open:
            self.serial.reset_input_buffer()

    def _write_unlocked(self, command: str) -> None:
        self._require_connection()
        payload = command.rstrip("\r\n") + "\n"
        self.serial.write(payload.encode("ascii"))
        self.serial.flush()

    def send_command(self, command: str) -> None:
        with self._io_lock:
            self._write_unlocked(command)
            time.sleep(0.08)

    def _pop_complete_line(self) -> Optional[str]:
        while True:
            index = self._rx_buffer.find(b"\n")
            if index < 0:
                return None
            raw_line = bytes(self._rx_buffer[:index])
            del self._rx_buffer[: index + 1]
            text = raw_line.decode("ascii", errors="ignore").strip()
            if text:
                return text

    def _read_one_line_unlocked(self, timeout: float = 2.0) -> Optional[str]:
        """Zwraca wylacznie KOMPLETNA linie zakonczona znakiem nowej linii.

        ``serial.readline()`` z timeoutem oddaje to, co dotarlo do tej pory -
        takze bez terminatora. Traktowanie takiego fragmentu jak pelnej
        odpowiedzi bylo bledem K2 z audytu wersji 1.0.5.
        """
        self._require_connection()
        deadline = time.monotonic() + timeout

        ready = self._pop_complete_line()
        if ready is not None:
            return ready

        while time.monotonic() < deadline:
            if self._abort_flag.is_set():
                # Zatrzymanie awaryjne czeka na _io_lock - oddajemy go od razu.
                return None
            raw = self.serial.readline()
            if not raw:
                continue
            self._rx_buffer.extend(raw)
            ready = self._pop_complete_line()
            if ready is not None:
                return ready
            if len(self._rx_buffer) > self.MAX_RESPONSE_BYTES:
                print(f"[RS232] Odrzucono strumien bez konca linii "
                      f"({len(self._rx_buffer)} B)")
                self._rx_buffer.clear()

        if self._rx_buffer:
            print(f"[RS232] Odrzucono niekompletna odpowiedz: "
                  f"{bytes(self._rx_buffer)!r}")
            self._rx_buffer.clear()
        return None

    def query(self, command: str, timeout: float = 2.0, retries: int = 1,
              validator: Optional[Callable[[str], bool]] = None) -> Optional[str]:
        attempts = max(1, retries)
        with self._io_lock:
            for attempt in range(attempts):
                if self._abort_flag.is_set():
                    return None
                self._clear_input()
                self._write_unlocked(command)
                response = self._read_one_line_unlocked(timeout)

                if response is not None and (validator is None or validator(response)):
                    return response
                if response is not None:
                    print(f"[RS232] Odrzucono odpowiedz dla '{command}': '{response}'")
                if attempt + 1 < attempts:
                    time.sleep(0.12)
        return None

    # ------------------------------------------------------------------ #
    # POMOCNICZE
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(response: Optional[str]) -> list[str]:
        if not response:
            return []
        separator = ";" if ";" in response else ","
        return [part.strip() for part in response.split(separator)]

    @staticmethod
    def _is_float(value: str) -> bool:
        return bool(_FLOAT_RE.fullmatch(value.strip()))

    @staticmethod
    def _is_integer(value: str) -> bool:
        return bool(_INT_RE.fullmatch(value.strip()))

    @staticmethod
    def _error_ok(error: Optional[str]) -> bool:
        return bool(error) and error.lstrip().startswith("+0")

    def _check_error(self, context: str) -> None:
        error = self.query(self.dialect.command("error"), timeout=2.0, retries=2)
        if not error:
            raise DeviceError(f"Brak potwierdzenia po: {context}")
        if not self._error_ok(error):
            raise DeviceError(f"Chroma odrzucila '{context}': {error}")

    def _query_float(self, command: str) -> Optional[float]:
        response = self.query(command, timeout=2.0, retries=3, validator=self._is_float)
        return float(response) if response is not None else None

    @staticmethod
    def _assert_close(label: str, actual: Optional[float], expected: float,
                      tolerance: float) -> None:
        if actual is None:
            raise DeviceError(f"Brak odczytu zwrotnego parametru: {label}")
        if abs(float(actual) - float(expected)) > float(tolerance):
            raise DeviceError(
                f"Odczyt zwrotny {label} nie zgadza sie z konfiguracja: "
                f"oczekiwano {expected}, odczytano {actual}"
            )

    # ------------------------------------------------------------------ #
    # SONDA DIALEKTU
    # ------------------------------------------------------------------ #
    def probe_dialect(self) -> list[dict[str, Any]]:
        """Sprawdza, ktore naglowki SCPI firmware faktycznie przyjmuje.

        Wysyla tylko zapytania i komendy nieszkodliwe (bez STARt). Wynik trafia
        do zakladki diagnostyki w panelu administratora. Nie zgadujemy skladni:
        odrzucona komenda wymagana do konfiguracji blokuje testowanie.
        """
        self._require_connection()
        results: list[dict[str, Any]] = []

        probes: list[tuple[str, dict[str, Any]]] = [
            ("idn", {}),
            ("error", {}),
            ("keylock_query", {}),
            ("step_count_query", {}),
            ("status_query", {}),
            ("step_settings_query", {"step": 1}),
            ("step_mode_query", {"step": 1}),
            ("acw_level_query", {"step": 1}),
            ("acw_limit_high_query", {"step": 1}),
            ("acw_limit_low_query", {"step": 1}),
            ("acw_limit_arc_query", {"step": 1}),
            ("acw_limit_real_query", {"step": 1}),
            ("acw_time_test_query", {"step": 1}),
            ("acw_time_ramp_query", {"step": 1}),
            ("acw_time_fall_query", {"step": 1}),
            ("preset_frequency_query", {}),
            ("result_last_judgment", {}),
            ("result_last_voltage", {}),
            ("result_last_current", {}),
        ]
        if self.dialect.has("result_step_judgment"):
            probes.append(("result_step_judgment", {"step": 1}))
        if self.dialect.has("channel_high_query"):
            probes.append(("channel_high_query", {"step": 1}))
            probes.append(("channel_low_query", {"step": 1}))

        for name, fields in probes:
            command = self.dialect.command(name, **fields)
            # Wyczysc kolejke bledow przed proba, zeby nie zaraportowac cudzego.
            self.query(self.dialect.command("error"), timeout=1.0, retries=1)
            response = self.query(command, timeout=1.5, retries=1)
            error = self.query(self.dialect.command("error"), timeout=1.5, retries=2)
            accepted = self._error_ok(error) and response is not None
            results.append({
                "name": name,
                "command": command,
                "verification": self.dialect.verification(name),
                "response": response,
                "error": error,
                "accepted": accepted,
                "required": name in REQUIRED_COMMANDS,
            })
            print(f"[SONDA] {name:26} {'OK ' if accepted else 'ODRZUCONA'} "
                  f"'{command}' -> resp={response!r} err={error!r}")
        return results

    # ------------------------------------------------------------------ #
    # KONFIGURACJA
    # ------------------------------------------------------------------ #
    def clear_steps(self) -> None:
        response = self.query(
            self.dialect.command("step_count_query"), timeout=2.0, retries=2
        )
        parts = self._parse(response)
        if not parts or not self._is_float(parts[0]):
            raise DeviceError("Nie udalo sie odczytac liczby krokow testera")

        count = int(float(parts[0]))
        for step in range(count, 0, -1):
            self.send_command(self.dialect.command("step_delete", step=step))
            time.sleep(0.10)
            self._check_error(f"usuniecie kroku {step}")
        self._configured_step_count = 0

    def configure_profile(self, profile) -> None:
        """Programuje wszystkie kroki profilu i weryfikuje odczyt zwrotny.

        Kazda niezgodnosc odczytu zwrotnego przerywa konfiguracje. Test na
        stanowisku, ktore nie potwierdzilo swoich nastaw, jest bez wartosci.
        """
        if not profile.matches_instrument(self.dialect.model):
            raise DeviceError(
                f"Profil {profile.display_name} jest przeznaczony dla "
                f"{', '.join(profile.allowed_models)}, a stanowisko ma "
                f"Chroma {self.dialect.model}"
            )
        if profile.requires_scan_box and not self.dialect.has_scan_box:
            raise DeviceError(
                f"Profil wymaga scan boxa, ktorego model {self.dialect.model} "
                "nie obsluguje"
            )
        if profile.step_count > self.dialect.max_steps:
            raise DeviceError(
                f"Profil ma {profile.step_count} krokow, a model "
                f"{self.dialect.model} obsluguje maksymalnie "
                f"{self.dialect.max_steps}"
            )
        if profile.requires_scan_box:
            missing = [
                name for name in REQUIRED_SCAN_COMMANDS if not self.dialect.has(name)
            ]
            if missing:
                raise DeviceError(
                    f"Model {self.dialect.model} nie definiuje komend scan boxa: "
                    + ", ".join(missing)
                )

        # Manual s. 5-32: czestotliwosc jest ustawieniem globalnym, nie
        # parametrem kroku. Profil moze zadac tylko jednej wartosci dla
        # wszystkich krokow - walidacja profilu tego pilnuje.
        self._configure_frequency(profile)

        for index, step in enumerate(profile.steps, start=1):
            self._configure_step(index, step, profile)

        steps_response = self.query(
            self.dialect.command("step_count_query"), timeout=2.0, retries=2,
            validator=lambda response: bool(self._parse(response))
            and self._is_float(self._parse(response)[0]),
        )
        if not steps_response:
            raise DeviceError("Brak potwierdzenia liczby krokow po konfiguracji")

        count = int(float(self._parse(steps_response)[0]))
        if count != profile.step_count:
            raise DeviceError(
                f"Po konfiguracji Chroma raportuje {count} krokow zamiast "
                f"{profile.step_count}. Sprawdz, czy firmware tworzy kroki "
                "automatycznie przy ustawianiu parametrow."
            )

        self._configured_step_count = profile.step_count
        print(f"[CONFIG] Zaprogramowano {count} krokow profilu "
              f"{profile.display_name}")

    def _configure_frequency(self, profile) -> None:
        """Ustawia globalna czestotliwosc AC i potwierdza ja odczytem."""
        frequencies = {int(step["frequency"]) for step in profile.steps}
        if len(frequencies) != 1:
            raise DeviceError(
                f"Profil zada roznych czestotliwosci {sorted(frequencies)}, a "
                "tester ma jedno globalne ustawienie AC FREQuency"
            )
        frequency = frequencies.pop()
        command = self.dialect.command("preset_frequency", value=frequency)
        self.send_command(command)
        time.sleep(0.10)
        self._check_error(command)

        readback = self._query_float(
            self.dialect.command("preset_frequency_query")
        )
        self._assert_close("Frequency", readback, float(frequency), 0.5)
        print(f"[CONFIG] Czestotliwosc AC potwierdzona: {frequency} Hz")

    def _configure_step(self, index: int, step: Mapping[str, Any], profile) -> None:
        i_high = float(step["limit_high"]) / 1000.0
        effective_low_ma = profile.effective_low_ma(step)
        i_low = effective_low_ma / 1000.0

        print(f"[CONFIG] Krok {index} ({step['name']}): "
              f"{step['voltage']} V, LOW {effective_low_ma:.3f} mA, "
              f"HIGH {step['limit_high']:.3f} mA")

        commands = [
            ("acw_level", {"step": index, "value": step["voltage"]}),
            ("acw_limit_high", {"step": index, "value": i_high}),
            ("acw_limit_low", {"step": index, "value": i_low}),
            ("acw_time_test", {"step": index, "value": step["dwell"]}),
            ("acw_time_ramp", {"step": index, "value": step["ramp_time"]}),
            ("acw_time_fall", {"step": index, "value": step["ramp_dn"]}),
            # Manual s. 5-20: ARC i REAL sa parametrami kroku i przyjmuja
            # wartosc w amperach. Aplikacja Amidala ich nie programowala,
            # bo uzywala blednej sciezki SCPI - tu sa ustawiane i weryfikowane.
            ("acw_limit_arc",
             {"step": index, "value": float(step.get("arc_sense", 0.0)) / 1000.0}),
            ("acw_limit_real",
             {"step": index, "value": float(step.get("real_limit", 0.0)) / 1000.0}),
        ]
        for name, fields in commands:
            command = self.dialect.command(name, **fields)
            self.send_command(command)
            time.sleep(0.10)
            self._check_error(command)

        if step.get("channels"):
            self._configure_channels(index, step["channels"])

        self._verify_step_readback(
            index=index, step=step, i_high=i_high, i_low=i_low
        )

    def _configure_channels(self, index: int, mask: str) -> None:
        """Ustawia listy kanalow scan boxa (HIGH i LOW/RTN).

        Manual s. 5-21: kanaly ustawia sie LISTAMI, a nie osobna komenda na
        kanal - ``SAFE:STEP1:AC:CHAN (@(1,3))``. Puste listy zapisuje sie jako
        ``(@(0))``, zeby jawnie skasowac ustawienie z poprzedniego profilu.
        Odczyt zwrotny wykonuje ``_verify_step_readback`` przez ``STEP:SET?``.
        """
        if not self.dialect.has("channel_high"):
            raise DeviceError(
                f"Model {self.dialect.model} nie definiuje komend scan boxa"
            )

        high, low = mask_to_channel_lists(mask)
        for name, channels in (("channel_high", high), ("channel_low", low)):
            command = self.dialect.command(
                name, step=index, channels=encode_channel_list(channels)
            )
            self.send_command(command)
            time.sleep(0.05)
            try:
                self._check_error(command)
            except DeviceError as exc:
                raise DeviceError(
                    f"Krok {index}: firmware odrzucil komende kanalow "
                    f"({command}). Sprawdz sonde SCPI w panelu administratora "
                    f"i w razie potrzeby popraw SCPI_OVERRIDES. Szczegoly: {exc}"
                ) from exc
        print(f"[CONFIG] Krok {index}: kanaly HIGH={high or '-'} "
              f"LOW={low or '-'} (maska {mask})")

    @staticmethod
    def _split_settings(response: str) -> list[str]:
        """Dzieli odpowiedz ``STEP:SET?`` po przecinkach POZA nawiasami.

        Listy kanalow same zawieraja przecinki (``(@(1,3))``), wiec zwykle
        ``split(",")`` rozerwaloby je na kawalki.
        """
        parts: list[str] = []
        current: list[str] = []
        depth = 0
        for character in str(response or ""):
            if character == "(":
                depth += 1
            elif character == ")":
                depth = max(0, depth - 1)
            if character == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
            current.append(character)
        parts.append("".join(current).strip())
        # NIE filtrujemy pustych pol: pozycje sa czytane pozycyjnie, wiec
        # wyciecie pustego ARC przesuwalo TIME na miejsce ARC, RAMP na TIME
        # i tak dalej (S4 z audytu). Puste pole zostaje pustym stringiem.
        return parts

    def read_step_settings(self, index: int) -> dict[str, Any]:
        """Odczytuje WSZYSTKIE nastawy kroku jednym zapytaniem ``STEP:SET?``.

        Manual s. 5-18, kolejnosc pol:
            STEP, MODE, VOLT, HIGH, LOW, ARC, TIME, RAMP, FALL, REAL,
            SCAN HI, SCAN LOW

        Jedno zapytanie zamiast osmiu ma znaczenie praktyczne: RS232 tego
        testera konczy sie na 19200 bodach, a profil SR203/SR204 ma piec krokow.
        """
        response = self.query(
            self.dialect.command("step_settings_query", step=index),
            timeout=3.0, retries=3,
        )
        if not response:
            raise DeviceError(f"Krok {index}: brak odpowiedzi na STEP:SET?")

        fields = self._split_settings(response)
        if len(fields) < 10:
            raise DeviceError(
                f"Krok {index}: nieoczekiwana odpowiedz STEP:SET? "
                f"({len(fields)} pol): {response!r}"
            )

        def number(position: int, name: str) -> float:
            text = fields[position]
            if not self._is_float(text):
                raise DeviceError(
                    f"Krok {index}: pole {name} nie jest liczba: {text!r}"
                )
            return float(text)

        settings: dict[str, Any] = {
            "step": fields[0],
            "mode": fields[1].upper(),
            "voltage": number(2, "VOLT"),
            "limit_high": number(3, "HIGH"),
            "limit_low": number(4, "LOW"),
            "arc": number(5, "ARC"),
            "dwell": number(6, "TIME"),
            "ramp_time": number(7, "RAMP"),
            "ramp_dn": number(8, "FALL"),
            "real": number(9, "REAL"),
        }
        if len(fields) >= 12:
            settings["channels_high"] = decode_channel_list(fields[10])
            settings["channels_low"] = decode_channel_list(fields[11])
        return settings

    def read_program(self) -> dict[str, Any]:
        """Odczytuje CALY program zaladowany w testerze - tylko zapytania.

        Uzywane do porownania profilu aplikacji z tym, co faktycznie stoi
        na stanowisku. Nie programuje niczego i nie podaje napiecia.

        Kanaly czytamy osobnym zapytaniem CHANnel?, a nie tylko z pol 11/12
        odpowiedzi SET? - starsze firmware potrafi zwrocic krotsza ramke.
        """
        response = self.query(self.dialect.command("step_count_query"),
                              timeout=2.0, retries=2)
        parts = self._parse(response)
        if not parts or not self._is_float(parts[0]):
            raise DeviceError("Nie udalo sie odczytac liczby krokow testera")
        count = int(float(parts[0]))

        frequency = None
        if self.dialect.has("preset_frequency_query"):
            answer = self.query(self.dialect.command("preset_frequency_query"),
                                timeout=2.0, retries=2)
            values = self._parse(answer)
            if values and self._is_float(values[0]):
                frequency = float(values[0])

        steps: list[dict[str, Any]] = []
        for index in range(1, count + 1):
            settings = self.read_step_settings(index)
            if self.dialect.has("channel_high_query"):
                high = self.query(
                    self.dialect.command("channel_high_query", step=index),
                    timeout=2.0, retries=2)
                low = self.query(
                    self.dialect.command("channel_low_query", step=index),
                    timeout=2.0, retries=2)
                if high:
                    settings["channels_high"] = decode_channel_list(high)
                if low:
                    settings["channels_low"] = decode_channel_list(low)
            steps.append(settings)

        return {"step_count": count, "frequency": frequency, "steps": steps,
                "identity": getattr(self, "identification", "")}

    def _check_step_number(self, settings: Mapping[str, Any], index: int) -> None:
        """Numer kroku z SET? musi odpowiadac krokowi, ktory wlasnie badamy.

        S10: pole bylo parsowane i nigdy nie sprawdzane. Szablon
        w SCPI_OVERRIDES bez pola {step} (np. "SAFEty:STEP1:SET?") jest
        przyjmowany bez bledu, bo str.format ignoruje nadmiarowe argumenty -
        kazdy krok byl wtedy weryfikowany wzgledem nastaw kroku 1.
        """
        raw = settings.get("step")
        if raw in (None, ""):
            return
        try:
            reported = int(float(str(raw)))
        except (TypeError, ValueError):
            raise DeviceError(
                f"Krok {index}: tester zwrocil nieczytelny numer kroku {raw!r}"
            ) from None
        if reported != index:
            raise DeviceError(
                f"Odczyt zwrotny dotyczy kroku {reported}, a badany jest krok "
                f"{index} - sprawdz SCPI_OVERRIDES (szablon bez pola {{step}}?)"
            )

    def _verify_step_readback(self, *, index: int, step: Mapping[str, Any],
                              i_high: float, i_low: float) -> None:
        settings = self.read_step_settings(index)
        self._check_step_number(settings, index)
        label = f"Krok {index} / "

        if settings["mode"] != "AC":
            raise DeviceError(
                f"{label}tester raportuje tryb {settings['mode']!r} zamiast AC"
            )

        voltage = float(step["voltage"])
        self._assert_close(label + "Voltage", settings["voltage"], voltage,
                           max(1.0, voltage * 0.001))
        self._assert_close(label + "Max Limit", settings["limit_high"], i_high,
                           max(2e-6, i_high * 0.01))
        self._assert_close(label + "Min Limit", settings["limit_low"], i_low,
                           max(2e-6, i_low * 0.01))
        self._assert_close(label + "Dwell", settings["dwell"],
                           float(step["dwell"]), 0.05)
        self._assert_close(label + "Ramp Time", settings["ramp_time"],
                           float(step["ramp_time"]), 0.05)
        self._assert_close(label + "Ramp Down", settings["ramp_dn"],
                           float(step["ramp_dn"]), 0.05)

        arc_a = float(step.get("arc_sense", 0.0)) / 1000.0
        self._assert_close(label + "Arc Sense", settings["arc"], arc_a,
                           max(2e-6, arc_a * 0.01))
        real_a = float(step.get("real_limit", 0.0)) / 1000.0
        self._assert_close(label + "Real Current", settings["real"], real_a,
                           max(2e-6, real_a * 0.01))

        # Maska kanalow MUSI zostac potwierdzona. Test na nieznanych kanalach
        # mierzylby cos innego niz zaklada instrukcja testowa.
        mask = step.get("channels")
        if mask:
            if "channels_high" not in settings:
                raise DeviceError(
                    f"{label}tester nie zwrocil list kanalow w STEP:SET? - "
                    "nie mozna potwierdzic maski scan boxa"
                )
            expected_high, expected_low = mask_to_channel_lists(mask)
            for side, expected, actual in (
                ("HIGH", set(expected_high), settings["channels_high"]),
                ("LOW", set(expected_low), settings["channels_low"]),
            ):
                if expected != actual:
                    raise DeviceError(
                        f"{label}kanaly {side}: ustawiono "
                        f"{sorted(expected) or '-'}, odczytano "
                        f"{sorted(actual) or '-'} - maska kanalow nie zostala "
                        "przyjeta"
                    )
            print(f"[VERIFY] Krok {index}: maska kanalow potwierdzona {mask}")

        print(f"[VERIFY] Krok {index} potwierdzony odczytem zwrotnym")

    # ------------------------------------------------------------------ #
    # CYKL
    # ------------------------------------------------------------------ #
    def start_test(self) -> bool:
        """Uruchamia NOWY cykl i potwierdza, ze tester faktycznie zaczal test.

        Sam brak bledu po STARcie nie wystarcza - poprzedni wynik LAST moze
        nadal zawierac PASS. Wymagamy stanu TESTING/RUNNING albo swiezego
        narastania napiecia powyzej 50 V wzgledem pomiaru bazowego.
        """
        self._cycle_active_confirmed = False
        self._cycle_started_monotonic = None

        if self._configured_step_count <= 0:
            print("[START] Brak potwierdzonej konfiguracji krokow")
            return False

        try:
            with self._io_lock:
                self._clear_input()
                self._write_unlocked(self.dialect.command("stop"))
                time.sleep(0.20)
            stop_error = self.query(self.dialect.command("error"),
                                    timeout=2.0, retries=2)
            if not self._error_ok(stop_error):
                print(f"[START] STOP przed cyklem odrzucony: '{stop_error}'")
                return False

            baseline = self.read_measurements()
            baseline_valid = baseline is not None
            baseline_voltage = (
                float(baseline.get("output_voltage", 0.0)) if baseline else 0.0
            )

            with self._io_lock:
                self._clear_input()
                self._write_unlocked(self.dialect.command("keylock_on"))
                time.sleep(0.08)
                command_started = time.monotonic()
                self._write_unlocked(self.dialect.command("start"))
                time.sleep(0.20)

            error = self.query(self.dialect.command("error"), timeout=2.0, retries=2)
            keylock = self.query(self.dialect.command("keylock_query"),
                                 timeout=2.0, retries=2, validator=self._is_integer)
            print(f"[START] ERR='{error}', KLOC='{keylock}'")
            if (not self._error_ok(error) or keylock is None
                    or int(keylock) != 1):
                self.stop_test(verify=False)
                return False

            deadline = time.monotonic() + 3.0
            active_confirmed = False
            while time.monotonic() < deadline:
                if self.get_status() in ACTIVE_STATUSES:
                    active_confirmed = True
                    break
                measurement = self.read_measurements()
                if measurement:
                    voltage = float(measurement.get("output_voltage", 0.0))
                    if baseline_valid and (
                        (baseline_voltage < 50.0 and voltage >= 50.0)
                        or voltage >= baseline_voltage + 50.0
                    ):
                        active_confirmed = True
                        break
                time.sleep(0.10)

            if not active_confirmed:
                print("[START] Brak potwierdzenia nowego aktywnego cyklu - "
                      "odrzucam mozliwy stary wynik LAST")
                self.stop_test(verify=False)
                return False

            self._cycle_id += 1
            self._cycle_active_confirmed = True
            self._cycle_started_monotonic = command_started
            print(f"[START] Potwierdzono nowy cykl #{self._cycle_id}")
            return True

        except Exception as exc:
            print(f"[START] Blad rozpoczecia testu: {exc}")
            try:
                self.stop_test(verify=False)
            except Exception:
                pass
            return False

    # Ponizej tego napiecia uznajemy, ze wysokie napiecie zgaslo.
    HV_OFF_VOLTAGE = 50.0

    def cycle_started_monotonic(self) -> Optional[float]:
        """Znacznik czasu zapisu SAFEty:STARt dla POTWIERDZONEGO cyklu.

        Punkt odniesienia dla bramek czasu w petli testowej. Liczenie ich od
        momentu utworzenia watku wliczalo cala sekwencje startowa i psulo
        oba progi naraz (K6).
        """
        if not self._cycle_active_confirmed:
            return None
        return self._cycle_started_monotonic

    def request_stop(self, lock_timeout: float = 1.5) -> tuple[bool, str]:
        """Wysyla STOP tak szybko, jak sie da. NIE weryfikuje skutku.

        Przeznaczone do wywolania z watku interfejsu przy otwarciu klapy:
        musi wrocic w ulamku sekundy, bo kazda milisekunda to wysokie
        napiecie na wyrobie. Potwierdzenie zatrzymania (``confirm_stopped``)
        wymaga kilku zapytan do testera i MUSI isc w tle - inaczej zamrozi
        okno na czas, ktory chcemy wlasnie skrocic.
        """
        acquired = False
        try:
            # K1: najpierw przerywamy trwajaca wymiane, potem bierzemy blokade.
            self._abort_flag.set()
            acquired = self._io_lock.acquire(timeout=max(0.1, lock_timeout))
            try:
                self._write_unlocked(self.dialect.command("stop"))
                time.sleep(0.15)
            finally:
                self._abort_flag.clear()
                if acquired:
                    self._io_lock.release()
                    acquired = False
            return True, "STOP wyslany"
        except Exception as exc:
            self._abort_flag.clear()
            if acquired:
                self._io_lock.release()
            message = f"Nie udalo sie wyslac STOP: {exc}"
            print(f"[STOP] {message}")
            return False, message
        finally:
            self._cycle_active_confirmed = False
            self._cycle_started_monotonic = None

    def stop_test(self, verify: bool = True,
                  lock_timeout: float = 1.5) -> tuple[bool, str]:
        """Zatrzymuje test i POTWIERDZA, ze tester faktycznie stanal.

        Zwraca ``(potwierdzone, komunikat)``. Poprzednia wersja zwracala samo
        ``True`` po udanym ``serial.write()`` i nikt tej wartosci nie sprawdzal
        (K2 z audytu). Przy wypietym kablu DB9 zapis konczy sie sukcesem -
        UART nadaje w prozanie - wiec samo powodzenie zapisu niczego nie
        dowodzi. Jedynym dowodem jest odczyt z testera po STOPie.

        ``verify=False`` sluzy sciezkom, ktore i tak zaraz rozlaczaja port.
        """
        sent, message = self.request_stop(lock_timeout)
        if not sent or not verify:
            return sent, message
        return self.confirm_stopped()

    def confirm_stopped(self, attempts: int = 3) -> tuple[bool, str]:
        """Sprawdza w testerze, czy cykl stanal i czy napiecie zgaslo."""
        last_status = "?"
        last_voltage = None
        for _ in range(max(1, attempts)):
            status = self.get_status()
            last_status = status
            if status not in ACTIVE_STATUSES and status != "COMM_ERROR":
                return True, f"Tester potwierdzil zatrzymanie (status {status})"

            measurement = self.read_measurements()
            if measurement is not None:
                last_voltage = float(measurement.get("output_voltage", 0.0))
                if last_voltage < self.HV_OFF_VOLTAGE:
                    return True, (f"Napiecie zgaslo ({last_voltage:.0f} V), "
                                  f"status {status}")
            time.sleep(0.25)

        detail = (f"status {last_status}"
                  + (f", napiecie {last_voltage:.0f} V"
                     if last_voltage is not None else ", brak odczytu napiecia"))
        message = f"NIE POTWIERDZONO zatrzymania testera ({detail})"
        print(f"[STOP] {message}")
        return False, message

    # ------------------------------------------------------------------ #
    # STATUS I POMIARY
    # ------------------------------------------------------------------ #
    @staticmethod
    def _valid_status(response: str) -> bool:
        return response.strip().upper() in (
            TERMINAL_STATUSES | ACTIVE_STATUSES | {"READY", "WAIT"}
        )

    def get_status(self) -> str:
        try:
            response = self.query(
                self.dialect.command("status_query"), timeout=1.5, retries=2,
                validator=self._valid_status,
            )
            return response.strip().upper() if response else "COMM_ERROR"
        except Exception as exc:
            print(f"[STATUS] Blad pobierania statusu: {exc}")
            return "COMM_ERROR"

    def _valid_measurement(self, response: str) -> bool:
        parts = self._parse(response)
        return (
            len(parts) >= 5
            and self._is_float(parts[2])
            and self._is_float(parts[3])
            and self._is_float(parts[4])
        )

    def read_measurements(self) -> Optional[Dict[str, Any]]:
        """Zwraca biezacy pomiar wraz z NUMEREM KROKU.

        Numer kroku pozwala przypisac probki obciazenia do wlasciwego kroku
        profilu wielokrokowego - bez tego nie da sie udowodnic, ze kazdy port
        faktycznie byl obciazony.
        """
        started = time.monotonic()
        try:
            response = self.query(
                self.dialect.command("fetch"), timeout=1.5, retries=2,
                validator=self._valid_measurement,
            )
            if not response:
                return None

            parts = self._parse(response)
            raw_step = parts[0]
            try:
                step_number = int(float(raw_step))
            except (TypeError, ValueError):
                step_number = 0

            raw_v = float(parts[2])
            raw_i = float(parts[3])
            raw_r = float(parts[4])
            return {
                "step": step_number,
                "step_raw": raw_step,
                "mode": parts[1],
                "output_voltage": raw_v if raw_v < OVERFLOW else 0.0,
                "measure_current": raw_i if raw_i < OVERFLOW else 0.0,
                "real_current": raw_r if raw_r < OVERFLOW else 0.0,
            }
        except Exception as exc:
            print(f"[FETCH] Blad odczytu pomiarow: {exc}")
            return None
        finally:
            self.last_poll_interval = time.monotonic() - started

    def measure_poll_cycle(self) -> float:
        """Mierzy RZECZYWISTY czas jednej iteracji odpytywania w tescie.

        Iteracja to zapytanie statusu + FETCh - dokladnie to, co robi petla
        testowa. Wczesniej estymata liczby probek opierala sie na
        ``last_poll_interval``, ktore przed pierwszym testem jest zerowe;
        aplikacja meldowala wtedy ~500 probek na krok, a na stanowisku przy
        9600 bodach zbierala 2-5. Ostrzezenie mialo chronic technologa przed
        seria odrzuconych PASS-ow i nie dzialalo.

        Same zapytania - nie uruchamia testu i nie podaje napiecia.
        """
        durations: list[float] = []
        for _ in range(3):
            started = time.monotonic()
            self.get_status()
            self.read_measurements()
            durations.append(time.monotonic() - started)
        # Mediana, zeby jednorazowy retry RS232 nie zawyzyl wyniku.
        durations.sort()
        return durations[len(durations) // 2]

    # ------------------------------------------------------------------ #
    # WYNIKI
    # ------------------------------------------------------------------ #
    def _judgment_to_result(self, judgment: Optional[str],
                            context: str) -> tuple[str, str]:
        if judgment is None:
            raise DeviceError(f"{context}: brak poprawnego kodu wyniku JUDG")
        value = int(judgment)
        if value <= 0 or value in self.dialect.non_terminal_judgments:
            raise DeviceError(
                f"{context}: kod JUDG {value} nie jest koncowym wynikiem produktu"
            )
        code = str(value)
        result = "PASS" if value == self.dialect.pass_judgment else "FAIL"
        return result, code

    def get_cycle_results(self, steps: Sequence[Mapping[str, Any]]
                          ) -> tuple[str, dict[str, Any]]:
        """Pobiera wyniki WSZYSTKICH krokow biezacego, potwierdzonego cyklu.

        Dla profilu jednokrokowego korzysta z rejestru LAST (jak Amidala).
        Dla profilu wielokrokowego wymaga wynikow per krok - brak takiej
        komendy w dialekcie jest bledem konfiguracji, nie powodem do zgadywania.
        """
        try:
            if not self._cycle_active_confirmed or self._cycle_started_monotonic is None:
                raise DeviceError(
                    "Brak potwierdzonego nowego cyklu - wynik LAST moze byc stary"
                )

            cycle_id = self._cycle_id
            cycle_elapsed = time.monotonic() - self._cycle_started_monotonic

            time.sleep(0.30)
            with self._io_lock:
                self._clear_input()

            step_count = len(steps)
            per_step: list[dict[str, Any]] = []

            if step_count == 1:
                per_step.append(self._read_last_result(steps[0], 1))
            else:
                if not self.dialect.has("result_step_judgment"):
                    raise DeviceError(
                        f"Model {self.dialect.model} nie definiuje odczytu wyniku "
                        "per krok, a profil ma wiecej niz jeden krok"
                    )
                for index, step in enumerate(steps, start=1):
                    per_step.append(self._read_step_result(step, index))

            overall = "PASS" if all(
                entry["result"] == "PASS" for entry in per_step
            ) else "FAIL"

            failed = [entry for entry in per_step if entry["result"] != "PASS"]
            data = {
                "fresh_cycle": True,
                "cycle_id": cycle_id,
                "cycle_elapsed": cycle_elapsed,
                "steps": per_step,
                "error_code": failed[0]["judgment_code"] if failed else "",
                "failed_step": failed[0]["name"] if failed else "",
            }
            for entry in per_step:
                print(f"[WYNIK] Krok {entry['index']} ({entry['name']}): "
                      f"{entry['result']} kod={entry['judgment_code']} "
                      f"{entry['output_voltage']:.0f} V "
                      f"{entry['measured_current']:.4f} mA")
            return overall, data

        except Exception as exc:
            print(f"[WYNIK] Blad pobierania wyniku: {exc}")
            return "UNKNOWN", {"error": str(exc)}
        finally:
            # Wynik moze zostac odczytany tylko raz dla tego STARTu.
            self._cycle_active_confirmed = False
            self._cycle_started_monotonic = None

    def _read_last_result(self, step: Mapping[str, Any], index: int) -> dict[str, Any]:
        judgment = self.query(
            self.dialect.command("result_last_judgment"), timeout=2.0, retries=3,
            validator=self._is_integer,
        )
        output_v = self._query_float(self.dialect.command("result_last_voltage"))
        measured_i = self._query_float(self.dialect.command("result_last_current"))
        real_i = self._query_float(
            self.dialect.command("result_last_real_current")
        ) if self.dialect.has("result_last_real_current") else None
        return self._build_step_result(step, index, judgment, output_v,
                                       measured_i, real_i)

    def _read_step_result(self, step: Mapping[str, Any], index: int) -> dict[str, Any]:
        judgment = self.query(
            self.dialect.command("result_step_judgment", step=index),
            timeout=2.0, retries=3, validator=self._is_integer,
        )
        output_v = self._query_float(
            self.dialect.command("result_step_voltage", step=index))
        measured_i = self._query_float(
            self.dialect.command("result_step_current", step=index))
        real_i = self._query_float(
            self.dialect.command("result_step_real_current", step=index)
        ) if self.dialect.has("result_step_real_current") else None
        return self._build_step_result(step, index, judgment, output_v,
                                       measured_i, real_i)

    def _build_step_result(self, step: Mapping[str, Any], index: int,
                           judgment: Optional[str], output_v: Optional[float],
                           measured_i: Optional[float],
                           real_i: Optional[float]) -> dict[str, Any]:
        context = f"Krok {index} ({step['name']})"
        result, code = self._judgment_to_result(judgment, context)
        if output_v is None or measured_i is None:
            raise DeviceError(f"{context}: brak kompletnych pomiarow OMET/MMET")

        return {
            "index": index,
            "name": step["name"],
            "result": result,
            "judgment_code": code,
            "error_code": "" if result == "PASS" else code,
            "output_voltage": output_v if output_v < OVERFLOW else 0.0,
            "measured_current": (
                measured_i * 1000.0 if measured_i < OVERFLOW else 0.0
            ),
            "real_current": (
                (real_i or 0.0) * 1000.0
                if real_i is not None and real_i < OVERFLOW else 0.0
            ),
        }
