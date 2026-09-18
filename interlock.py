# interlock.py
"""Monitor interlocka Arduino — klapa otwarta/zamknięta.

Monitor działa fail-safe: brak prawidłowego komunikatu OPEN/CLOSED przez określony
czas jest traktowany jako utrata interlocka i zgłaszany do aplikacji jako ``None``.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import serial

from safety_rules import validate_interlock_settings


class InterlockMonitor:
    # Firmware stanowiska wysyla wyłącznie te dwa komunikaty.
    # Inne wartosci sa odrzucane zamiast zgadywania stanu klapy.
    CLOSED_SIGNALS = {"CLOSED"}
    OPEN_SIGNALS = {"OPEN"}

    # Podpis stanowiska. Szkic wysyla go raz na sekunde jako "ID:<podpis>".
    # Sam protokol OPEN/CLOSED nie odroznia plyty interlocka od dowolnego
    # innego urzadzenia na porcie szeregowym - podstawienie com0com
    # i piecioliniowego skryptu spelnialo nawet wymog przejscia OPEN->CLOSED.
    # Podpis nie jest zabezpieczeniem kryptograficznym (jest jawny w szkicu),
    # ale wyklucza PRZYPADKOWE podlaczenie sie pod zly port COM i wymaga
    # swiadomego dzialania, zeby go obejsc.
    IDENTITY_PREFIX = "ID:"

    # Maksymalna dlugosc akumulowanej ramki. Chroni przed zapchaniem pamieci,
    # gdy uszkodzony kabel generuje strumien bajtow bez znaku konca linii.
    MAX_LINE_BYTES = 64

    def __init__(
        self,
        port: str = "COM5",
        baudrate: int = 9600,
        heartbeat_timeout: float = 2.0,
        expected_identity: str = "",
    ):
        self.expected_identity = str(expected_identity or "").strip().upper()
        self.identity_seen: Optional[str] = None
        self.port, self.baudrate, _ = validate_interlock_settings(
            port, baudrate, True
        )
        self.heartbeat_timeout = max(0.5, float(heartbeat_timeout))
        self.serial: Optional[serial.Serial] = None
        self.connected = False

        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._on_change: Optional[Callable[[Optional[bool]], None]] = None
        self._last_state: Optional[bool] = None
        self._last_message_time: Optional[float] = None
        self._loss_reported = False
        # Bufor skladania ramki. readline() z timeoutem moze zwrocic fragment
        # bez terminatora; sklejamy go zamiast odrzucac jako nieznany sygnal.
        self._rx_buffer = bytearray()
        # Ostatnie odebrane bajty - do diagnostyki utraty sygnalu.
        self._last_raw = bytearray()

    def connect(self) -> bool:
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.25,
            )
            time.sleep(1.5)  # reset Arduino po otwarciu portu
            self.serial.reset_input_buffer()
            self.connected = True
            self._last_state = None
            self._last_message_time = time.monotonic()
            self._loss_reported = False
            self._rx_buffer.clear()
            print(f"[INTERLOCK] Połączono: {self.port} @ {self.baudrate}")
            return True
        except Exception as exc:
            print(f"[INTERLOCK] Błąd połączenia: {exc}")
            try:
                if self.serial and self.serial.is_open:
                    self.serial.close()
            except Exception:
                pass
            self.connected = False
            return False

    def disconnect(self):
        self._stop_flag.set()
        self._on_change = None
        try:
            if self.serial and self.serial.is_open:
                self.serial.close()
        finally:
            self.connected = False
            thread = self._thread
            if (
                thread
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=0.5)
            print("[INTERLOCK] Rozłączono")

    def set_on_change(self, callback: Callable[[Optional[bool]], None]):
        self._on_change = callback

    def start_monitoring(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def _notify(self, state: Optional[bool]):
        if not self._on_change:
            return
        try:
            self._on_change(state)
        except Exception as callback_error:
            print(f"[INTERLOCK] Błąd callback: {callback_error}")

    def _report_connection_loss(self, reason: str):
        if self._loss_reported:
            return
        self._loss_reported = True
        self.connected = False
        self._last_state = None
        try:
            if self.serial and self.serial.is_open:
                self.serial.close()
        except Exception:
            pass
        print(f"[INTERLOCK] Utrata komunikacji: {reason}")
        self._notify(None)

    def _extract_lines(self, chunk: bytes) -> list[str]:
        """Skleja fragmenty ramek i zwraca wylacznie KOMPLETNE linie.

        ``readline()`` z ustawionym timeoutem zwraca to, co zdazylo dotrzec, takze
        bez terminatora. Bez akumulacji ``CLOSED\\n`` rozbite na ``CLO`` + ``SED\\n``
        dawaloby dwa "nieznane sygnaly" zamiast jednej poprawnej zmiany stanu.
        """
        if chunk:
            self._rx_buffer.extend(chunk)

        lines: list[str] = []
        while True:
            index = self._rx_buffer.find(b"\n")
            if index < 0:
                break
            raw_line = bytes(self._rx_buffer[:index])
            del self._rx_buffer[: index + 1]
            text = raw_line.decode("ascii", errors="ignore").strip().upper()
            if text:
                lines.append(text)

        # Strumien bez terminatora nie moze rosnac w nieskonczonosc.
        if len(self._rx_buffer) > self.MAX_LINE_BYTES:
            del self._rx_buffer[: -self.MAX_LINE_BYTES]

        return lines

    def _heartbeat_expired(self, now: float) -> bool:
        return (
            self._last_message_time is not None
            and now - self._last_message_time > self.heartbeat_timeout
        )

    def _monitor_loop(self):
        consecutive_errors = 0
        max_errors = 5

        while not self._stop_flag.is_set():
            try:
                if not self.serial or not self.serial.is_open:
                    raise RuntimeError("Port zamknięty")

                raw = self.serial.readline()
                now = time.monotonic()

                # Kontrola heartbeatu MUSI byc wykonana w kazdej iteracji, a nie
                # tylko przy pustym odczycie. Strumien smieci (zly baudrate,
                # uszkodzony kabel, zawieszony szkic) rowniez oznacza utrate
                # wiarygodnego sygnalu klapy i musi zablokowac stanowisko.
                messages = self._extract_lines(raw)
                valid_state = None
                for message in messages:
                    if message in self.CLOSED_SIGNALS:
                        valid_state = True
                    elif message in self.OPEN_SIGNALS:
                        valid_state = False
                    elif message.startswith(self.IDENTITY_PREFIX):
                        self._handle_identity(message[len(self.IDENTITY_PREFIX):])
                    else:
                        print(f"[INTERLOCK] Nieznany sygnał: '{message}'")

                if raw:
                    # Ostatnie odebrane bajty sa jedynym dowodem, czy plyta
                    # milczy, czy nadaje smieci (zly baudrate, uszkodzony
                    # kabel). Bez tego komunikat o utracie nie daje sie
                    # zdiagnozowac na stanowisku.
                    self._last_raw = raw[-32:]

                if valid_state is None:
                    if self._heartbeat_expired(now):
                        gap = (now - self._last_message_time
                               if self._last_message_time else 0.0)
                        if messages:
                            detail = (f"odebrano {len(messages)} ramek, zadna "
                                      f"nie byla OPEN/CLOSED")
                        elif self._last_raw:
                            detail = f"ostatnie bajty: {bytes(self._last_raw)!r}"
                        else:
                            detail = "brak jakichkolwiek bajtow"
                        self._report_connection_loss(
                            f"przerwa {gap:.1f} s (limit "
                            f"{self.heartbeat_timeout:.1f} s); {detail}")
                        break
                    continue

                new_state = valid_state
                self._last_message_time = now
                self.connected = True
                self._loss_reported = False
                consecutive_errors = 0

                # Arduino może wysyłać ten sam stan wiele razy na sekundę.
                # Callback wykonujemy wyłącznie przy rzeczywistej zmianie.
                if new_state == self._last_state:
                    continue

                self._last_state = new_state
                state_text = "CLOSED" if new_state else "OPEN"
                print(f"[INTERLOCK] Zmiana stanu: '{state_text}'")
                self._notify(new_state)

            except Exception as exc:
                if self._stop_flag.is_set():
                    break

                consecutive_errors += 1
                print(f"[INTERLOCK] Błąd pętli ({consecutive_errors}): {exc}")

                if consecutive_errors >= max_errors:
                    self._report_connection_loss(
                        f"{consecutive_errors} kolejnych błędów portu"
                    )
                    break

                time.sleep(0.5)

    def _handle_identity(self, value: str) -> None:
        value = value.strip().upper()
        if self.identity_seen != value:
            self.identity_seen = value
            print(f"[INTERLOCK] Podpis plyty: '{value}'")
        if self.expected_identity and value != self.expected_identity:
            self._report_connection_loss(
                f"podpis plyty '{value}' zamiast '{self.expected_identity}' - "
                "podlaczono niewlasciwe urzadzenie albo zly port COM"
            )

    def identity_ok(self) -> bool:
        """Czy plyta przedstawila sie oczekiwanym podpisem."""
        if not self.expected_identity:
            return True
        return self.identity_seen == self.expected_identity

    def reconnect(self) -> bool:
        """Zamyka port i uruchamia monitor od nowa.

        Utrata sygnalu konczy watek monitora na stale (fail-safe). Bez tej
        metody jedynym wyjsciem byl powrot do menu i przekonfigurowanie
        Chromy od zera - a przyczyna bywa trywialna, np. przelozony wtyk USB.

        Bezpieczenstwo nie jest tym oslabione: stan startowy to brak wiedzy
        o klapie, a start testu i tak wymaga swiezego przejscia OPEN -> CLOSED.
        """
        # disconnect() zeruje callback - trzymamy go i przywracamy, inaczej
        # po ponownym polaczeniu ekran testowy nie dostawalby zmian stanu.
        callback = self._on_change
        self.disconnect()
        self._on_change = callback

        # S2: disconnect() robi join z timeoutem 0,5 s. Gdy stary watek wisi
        # w readline(), join wypada po czasie i watek ZYJE. Wyzerowanie
        # _stop_flag spod niego sprawialo, ze start_monitoring() widzial
        # zywy watek i nie startowal nowego, a stary - z odziedziczonym
        # licznikiem bledow - zamykal swiezo otwarty port.
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                print("[INTERLOCK] Stary watek monitora nie zakonczyl sie - "
                      "nie ponawiam polaczenia")
                return False
        self._thread = None
        self._stop_flag.clear()
        self._rx_buffer = bytearray()
        self._last_raw = bytearray()
        self._last_state = None
        self._last_message_time = None
        self._loss_reported = False
        self.connected = False
        if not self.connect():
            return False
        self.start_monitoring()
        return True
