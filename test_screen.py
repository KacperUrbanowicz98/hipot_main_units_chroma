"""Ekran testowy silnika Hi-Pot: sekwencja N krokow w jednym cyklu Chromy.

Roznica wzgledem Amidali 1.0.x: Chroma wykonuje wszystkie kroki profilu w jednym
STARcie, wiec dowody obciazenia trzeba zbierac PER KROK. Numer aktualnego kroku
pochodzi z pola STEP odpowiedzi ``SAFEty:FETCh?``, dzieki czemu kazdy port
(Modem, Ethernet 1-4) ma wlasny licznik probek w zakresie, wlasne maksimum
napiecia i wlasna flage przekroczenia pradu.

Bramki bezpieczeństwa zachowane z 1.0.x:
* start wylacznie po sekwencji interlocka OPEN -> CLOSED,
* odrzucenie wyniku bez potwierdzonego nowego cyklu,
* odrzucenie podejrzanie szybkiego PASS,
* utrata interlocka w trakcie testu = natychmiastowy STOP i blokada.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from tkinter import messagebox
from typing import Any, Optional

from report_writer import (
    count_pending_reports,
    flush_pending_reports,
    save_report,
)
from security import audit
from safety_rules import (
    MIN_IN_RANGE_SAMPLES,
    PASS_MIN_RUNTIME_FRACTION,
    validate_cycle_pass_evidence,
    validate_step_pass_evidence,
)


class StepEvidence:
    """Dowody zebrane na zywo dla jednego kroku biezacego cyklu."""

    # S3: pojedyncza probka nadmiaru NIE unieważnia wyniku. Prad ladowania
    # pojemnosci wyrobu potrafi chwilowo przekroczyc Max Limit w chwili
    # dojscia rampy do napiecia docelowego - Chroma to widzi i orzeka PASS,
    # a aplikacja odrzucala poprawny wynik i blokowala stanowisko.
    # Dopiero seria kolejnych probek oznacza rzeczywiste przekroczenie.
    OVERCURRENT_STREAK_REQUIRED = 3

    __slots__ = ("max_voltage", "max_current_ma", "in_range_samples",
                 "overcurrent_streak", "overcurrent_seen", "samples")

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.max_voltage = 0.0
        self.max_current_ma = 0.0
        self.in_range_samples = 0
        self.overcurrent_streak = 0
        self.overcurrent_seen = False
        self.samples = 0

    def note_overcurrent(self, over: bool) -> None:
        if not over:
            self.overcurrent_streak = 0
            return
        self.overcurrent_streak += 1
        if self.overcurrent_streak >= self.OVERCURRENT_STREAK_REQUIRED:
            self.overcurrent_seen = True


class _NullWidget:
    """Zaslepka po usunietym przycisku START TEST.

    Przycisk byl ustawiany na state="disabled" w 17 miejscach i ani razu na
    "normal" - start jest wylacznie automatyczny po zamknieciu klapy.
    Zamiast wycinac kilkanascie wywolan po jednym (i ryzykowac przeoczenie
    jednego), zaslepka przyjmuje je i nic nie robi.
    """

    def config(self, **_kwargs) -> None:
        return None

    def winfo_ismapped(self) -> bool:
        return False


class TestScreen:

    # Atrybut klasy: istnieje zawsze, takze zanim powstana widgety.
    start_button = _NullWidget()


    def __init__(self, parent, config, scan, app_ref=None):
        self.parent = parent
        self.config = config
        self.app_ref = app_ref

        # Profil jest ZAMROZONY na czas sesji ekranu. Zmiana w panelu nie moze
        # przesunac progow w trakcie trwajacego cyklu.
        self.profile = scan.profile
        self.serial = scan.serial
        self.model_name = scan.model_name

        self.device = None
        self.interlock = None
        self.test_running = False
        self._result_pending = False
        self.test_thread = None
        self.start_time = None
        self._device_configured = False
        self._test_aborted = False
        self._closed = False
        self._run_id = 0
        self._last_display_refresh = 0.0
        # Po przerwanym tescie przycisk "Nastepny SN" najpierw przygotowuje
        # stanowisko, dopiero potem otwiera okno skanowania.
        self._needs_recovery = False
        self._test_completed_called = False

        self._evidence: dict[int, StepEvidence] = {
            index: StepEvidence()
            for index in range(1, self.profile.step_count + 1)
        }
        self._current_step = 0
        self._cycle_terminal_seen = False
        self._cycle_active_seen = False

        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_poll_after_id = None
        self._next_dialog_after_id = None
        self._report_threads: list[threading.Thread] = []

        self.current_voltage = 0.0
        self.current_current = 0.0
        self.elapsed_time = 0.0
        self.test_result = None

        self._prev_interlock_closed = None
        self._current_interlock_closed = None
        self._lid_open_seen = False
        self._valid_close_transition = False
        self._serial_ready_for_test = True

        self.sn_dialog = None
        self.sn_entry = None
        self.sn_result_label = None
        self.sn_status_lbl = None

        self._recent_results: list[dict[str, Any]] = []
        self._history_frame = None
        self._step_rows: dict[int, dict[str, tk.Label]] = {}

    # ------------------------------------------------------------------ #
    # CYKL ZYCIA
    # ------------------------------------------------------------------ #
    def show(self) -> None:
        if self.app_ref is not None:
            self.app_ref.current_test_screen = self
        self._start_ui_poll()
        for widget in self.parent.winfo_children():
            widget.destroy()

        self._create_header()
        self.main_frame = tk.Frame(self.parent, bg=self.config.COLOR_BG)
        self.main_frame.pack(expand=True, fill=tk.BOTH, padx=16, pady=(12, 50))

        self._create_device_info()
        self._create_step_table()
        self._create_live_display()
        self._create_verdict_bar()
        self._create_progress_bar()
        self._create_interlock_status()
        self._create_control_buttons()
        self._create_history_panel()

        self._connect_device()
        self._connect_interlock()

    def _start_ui_poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                if self._closed:
                    continue
                try:
                    callback()
                except tk.TclError:
                    if not self._closed:
                        print("[UI] Callback pominiety po zamknieciu okna")
                except Exception as exc:
                    print(f"[UI] Krytyczny blad callbacku: {exc}")
                    traceback.print_exc()
                    try:
                        if self.device:
                            self.device.request_stop()
                    except Exception as stop_error:
                        print(f"[UI] STOP po bledzie callbacku: {stop_error}")
                    self._test_error(f"Wewnetrzny blad interfejsu: {exc}")
        except queue.Empty:
            pass
        if not self._closed:
            self._ui_poll_after_id = self.parent.after(25, self._start_ui_poll)

    def _post_ui(self, callback) -> None:
        if not self._closed:
            self._ui_queue.put(callback)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._test_aborted = True
        self.test_running = False
        self._result_pending = False
        self._run_id += 1

        for after_id in (self._ui_poll_after_id, self._next_dialog_after_id):
            if after_id:
                try:
                    self.parent.after_cancel(after_id)
                except Exception:
                    pass
        self._ui_poll_after_id = None
        self._next_dialog_after_id = None

        if self.sn_dialog is not None:
            try:
                if self.sn_dialog.winfo_exists():
                    self.sn_dialog.grab_release()
                    self.sn_dialog.destroy()
            except Exception:
                pass
            self.sn_dialog = None

        for action in (
            lambda: self.device and self.device.stop_test(verify=False),
            lambda: self.interlock and self.interlock.disconnect(),
            lambda: self.device and self.device.disconnect(send_stop=False),
        ):
            try:
                action()
            except Exception:
                pass

        self._report_threads = [t for t in self._report_threads if t.is_alive()]
        deadline = time.monotonic() + 1.0
        for thread in self._report_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        if self.app_ref is not None and self.app_ref.current_test_screen is self:
            self.app_ref.current_test_screen = None

    # ------------------------------------------------------------------ #
    # WIDOK
    # ------------------------------------------------------------------ #
    def _create_header(self) -> None:
        header = tk.Frame(self.parent, bg=self.config.COLOR_PRIMARY, height=64)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=self.config.WINDOW_TITLE,
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 20, "bold")).pack(side=tk.LEFT, padx=20, pady=14)
        tk.Label(header, text=f"Stanowisko: {self.config.STATION_ID}",
                 bg=self.config.COLOR_PRIMARY, fg="#C5CAE9",
                 font=("Arial", 12, "bold")).pack(side=tk.LEFT, padx=6, pady=14)

        border = tk.Frame(header, bg=self.config.COLOR_WHITE, padx=1, pady=1)
        border.pack(side=tk.RIGHT, padx=10, pady=14)
        self.back_button = tk.Button(
            border, text="← Powrót do menu",
            bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
            font=("Arial", 10, "bold"), relief=tk.FLAT, cursor="hand2",
            padx=10, pady=4, command=self._go_back)
        self.back_button.pack()

    def _create_device_info(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.config.COLOR_WHITE,
                         relief=tk.RAISED, borderwidth=2)
        frame.pack(fill=tk.X, pady=(0, 8))
        row = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        row.pack(padx=20, pady=10)

        for column, (label, value) in enumerate((
            ("Produkt:", self.profile.display_name),
            ("Model:", self.model_name),
        )):
            tk.Label(row, text=label, bg=self.config.COLOR_WHITE,
                     fg=self.config.COLOR_PRIMARY,
                     font=("Arial", 11, "bold")).grid(
                         row=0, column=column * 2, sticky="w", padx=(0, 8))
            tk.Label(row, text=value, bg=self.config.COLOR_WHITE, fg="#333333",
                     font=("Arial", 11)).grid(
                         row=0, column=column * 2 + 1, sticky="w", padx=(0, 28))

        tk.Label(row, text="S/N:", bg=self.config.COLOR_WHITE,
                 fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 11, "bold")).grid(row=0, column=4,
                                                  sticky="w", padx=(0, 8))
        self.sn_display_label = tk.Label(
            row, text=self.serial, bg=self.config.COLOR_WHITE, fg="#333333",
            font=("Arial", 11, "bold"))
        self.sn_display_label.grid(row=0, column=5, sticky="w")

    def _create_step_table(self) -> None:
        """Tabela krokow - operator widzi, ktory port jest testowany i jak wypadl."""
        frame = tk.Frame(self.main_frame, bg=self.config.COLOR_WHITE,
                         relief=tk.RAISED, borderwidth=2)
        frame.pack(fill=tk.X, pady=(0, 8))

        tk.Label(frame,
                 text=f"Sekwencja testowa — {self.profile.step_count} "
                      f"{'krok' if self.profile.step_count == 1 else 'kroków'} "
                      f"({self.profile.total_duration:.1f} s)",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 11, "bold")).pack(pady=(10, 6))

        table = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        table.pack(padx=16, pady=(0, 10), fill=tk.X)

        headers = ["#", "Nazwa", "Napięcie", "Limit [mA]", "Czas",
                   "Kanały", "Wynik"]
        widths = [3, 14, 9, 15, 13, 11, 8]
        for column, (text, width) in enumerate(zip(headers, widths)):
            tk.Label(table, text=text, bg=self.config.COLOR_PRIMARY,
                     fg=self.config.COLOR_WHITE, font=("Arial", 9, "bold"),
                     width=width, pady=3).grid(row=0, column=column, sticky="we")

        for index, step in enumerate(self.profile.steps, start=1):
            background = "#f7f7f7" if index % 2 else self.config.COLOR_WHITE
            low = self.profile.effective_low_ma(step)
            values = [
                str(index),
                step["name"],
                f"{step['voltage'] / 1000:.2f} kV",
                f"{low:.3f} – {step['limit_high']:.3f}",
                f"{step['ramp_time']:.1f}/{step['dwell']:.1f}/"
                f"{step['ramp_dn']:.1f} s",
                step.get("channels", "—"),
            ]
            row_labels: dict[str, tk.Label] = {}
            for column, (value, width) in enumerate(zip(values, widths)):
                label = tk.Label(table, text=value, bg=background, fg="#333333",
                                 font=("Arial", 9), width=width, pady=2)
                label.grid(row=index, column=column, sticky="we")
                row_labels[headers[column]] = label
            result_label = tk.Label(table, text="—", bg=background, fg="#999999",
                                    font=("Arial", 9, "bold"), width=widths[-1],
                                    pady=2)
            result_label.grid(row=index, column=len(headers) - 1, sticky="we")
            row_labels["Wynik"] = result_label
            row_labels["_bg"] = background
            self._step_rows[index] = row_labels

    def _create_live_display(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.config.COLOR_WHITE,
                         relief=tk.RAISED, borderwidth=2)
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.live_title = tk.Label(
            frame, text="Pomiary na żywo", bg=self.config.COLOR_WHITE,
            fg=self.config.COLOR_PRIMARY, font=("Arial", 11, "bold"))
        self.live_title.pack(pady=(10, 4))

        grid = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        grid.pack(expand=True, pady=10)
        for column, (title, attribute, color) in enumerate((
            ("NAPIĘCIE", "voltage_label", self.config.COLOR_PRIMARY),
            ("PRĄD", "current_label", self.config.COLOR_ACCENT),
            ("CZAS", "time_label", "#333333"),
        )):
            cell = tk.Frame(grid, bg=self.config.COLOR_WHITE)
            cell.grid(row=0, column=column, padx=36)
            tk.Label(cell, text=title, bg=self.config.COLOR_WHITE, fg="#666666",
                     font=("Arial", 9)).pack()
            label = tk.Label(cell, text="0", bg=self.config.COLOR_WHITE,
                             fg=color, font=("Arial", 30, "bold"))
            label.pack(pady=4)
            setattr(self, attribute, label)

        self.voltage_label.config(text="0 V")
        self.current_label.config(text="0.00 mA")
        self.time_label.config(text="0.0 s")

    # Odswiezanie licznikow na ekranie (nie dotyczy zbierania probek).
    DISPLAY_REFRESH_S = 0.2

    def _create_verdict_bar(self) -> None:
        """Wynik testu na pelna szerokosc, 64 pkt.

        W3/W4 z audytu: wczesniej werdykt byl najmniejszym waznym tekstem na
        ekranie (11 pkt), a najwieksze byly liczniki napiecia, ktore w chwili
        odczytu wyniku pokazuja 0 V. Operator odczytywal wynik wylacznie
        z koloru - przy kontrascie 2,78:1 dla PASS.
        """
        self.verdict_frame = tk.Frame(self.main_frame, bg=self.config.COLOR_BG,
                                      height=96)
        self.verdict_frame.pack(fill=tk.X, pady=(0, 6))
        self.verdict_frame.pack_propagate(False)
        self.verdict_label = tk.Label(
            self.verdict_frame, text="GOTOWY", bg=self.config.COLOR_BG,
            fg="#9E9E9E", font=("Arial", 64, "bold"))
        self.verdict_label.pack(expand=True, fill=tk.BOTH)

    def _set_verdict(self, text: str, color: str) -> None:
        frame = getattr(self, "verdict_frame", None)
        if frame is None:
            return
        try:
            background = (self.config.COLOR_BG if color == "#9E9E9E" else color)
            foreground = ("#9E9E9E" if color == "#9E9E9E"
                          else self.config.COLOR_WHITE)
            frame.config(bg=background)
            self.verdict_label.config(text=text, bg=background, fg=foreground)
        except tk.TclError:
            pass

    def _create_progress_bar(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.config.COLOR_BG)
        frame.pack(fill=tk.X, pady=(0, 8))
        self.status_label = tk.Label(frame, text="Gotowy do rozpoczęcia testu",
                                     bg=self.config.COLOR_BG, fg="#424242",
                                     font=("Arial", 13, "bold"),
                                     wraplength=1200, justify="center")
        self.status_label.pack(pady=(0, 5))
        # W6: ostrzezenie o zbyt wolnym probkowaniu bylo pisane do
        # status_label i kasowane w NASTEPNEJ linii - nigdy nie bylo widoczne.
        self.sampling_warning_label = tk.Label(
            frame, text="", bg=self.config.COLOR_BG,
            fg=self.config.COLOR_WARNING, font=("Arial", 11, "bold"),
            wraplength=1200, justify="center")
        # W5: stan zapisu raportow. Pakowany dopiero, gdy jest co pokazac.
        self.storage_warning_label = tk.Label(
            frame, text="", bg="#FFF3E0", fg=self.config.COLOR_WARNING,
            font=("Arial", 12, "bold"), wraplength=1200, justify="center")
        self.progress_canvas = tk.Canvas(
            frame, height=26, bg=self.config.COLOR_WHITE,
            highlightthickness=1, highlightbackground="#cccccc")
        self.progress_canvas.pack(fill=tk.X)
        self.progress_rect = self.progress_canvas.create_rectangle(
            0, 0, 0, 26, fill=self.config.COLOR_ACCENT, outline="")

    def _create_interlock_status(self) -> None:
        self.interlock_frame = tk.Frame(self.main_frame, bg="#fff8e1",
                                        relief=tk.RAISED, borderwidth=2)
        self.interlock_frame.pack(fill=tk.X, pady=(0, 8))
        self.interlock_label = tk.Label(
            self.interlock_frame, text="⏳ Łączenie z interlockiem (Arduino)...",
            bg="#fff8e1", fg=self.config.COLOR_WARNING, font=("Arial", 11, "bold"))
        self.interlock_label.pack(pady=7)

        # Przycisk pojawia sie TYLKO po utracie sygnalu. Utrata konczy watek
        # monitora na stale, wiec bez tego jedynym wyjsciem byl powrot do menu
        # i przekonfigurowanie Chromy od zera - przy przypadkowo ruszonym
        # wtyku USB to nieproporcjonalna kara.
        self.interlock_retry_btn = tk.Button(
            self.interlock_frame, text="↻ Połącz ponownie z interlockiem",
            bg="#607D8B", fg=self.config.COLOR_WHITE,
            font=("Arial", 10, "bold"), relief=tk.FLAT, cursor="hand2",
            command=self._retry_interlock)

    def _create_control_buttons(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.config.COLOR_BG)
        frame.pack(fill=tk.X, pady=(0, 8))

        # Przycisk "START TEST" zostal usuniety: byl ustawiany na
        # state="disabled" w 17 miejscach i ani razu na "normal". Start jest
        # wylacznie automatyczny po zamknieciu klapy. Przycisk, ktory nigdy
        # nie dziala, uczy operatora ignorowac przyciski.
        self.stop_button = tk.Button(
            frame, text="STOP", bg=self.config.COLOR_ERROR,
            fg=self.config.COLOR_WHITE, font=("Arial", 15, "bold"), height=2,
            relief=tk.FLAT, cursor="hand2", state="disabled",
            command=self._stop_test)
        self.stop_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        self.next_sn_button = tk.Button(
            frame, text="➜ Następny SN", bg="#607D8B",
            fg=self.config.COLOR_WHITE, font=("Arial", 15, "bold"), height=2,
            relief=tk.FLAT, cursor="hand2", state="disabled",
            command=self._open_sn_dialog_manually)
        self.next_sn_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))



    def _create_history_panel(self) -> None:
        outer = tk.Frame(self.main_frame, bg=self.config.COLOR_WHITE,
                         relief=tk.RAISED, borderwidth=2)
        outer.pack(fill=tk.X)
        tk.Label(outer, text="Ostatnie wyniki", bg=self.config.COLOR_WHITE,
                 fg=self.config.COLOR_PRIMARY, font=("Arial", 9, "bold")).pack(
                     anchor="w", padx=10, pady=(4, 2))

        header = tk.Frame(outer, bg=self.config.COLOR_PRIMARY)
        header.pack(fill=tk.X, padx=10)
        for text, width in (("Czas", 8), ("Numer seryjny", 20),
                            ("Model", 14), ("Wynik", 6), ("Uwaga", 18)):
            tk.Label(header, text=text, bg=self.config.COLOR_PRIMARY,
                     fg=self.config.COLOR_WHITE, font=("Arial", 8, "bold"),
                     width=width, pady=2).pack(side=tk.LEFT)

        self._history_frame = tk.Frame(outer, bg=self.config.COLOR_WHITE)
        self._history_frame.pack(fill=tk.X, padx=10, pady=(1, 5))
        self._refresh_history()

    def _refresh_history(self) -> None:
        if not self._history_frame or not self._history_frame.winfo_exists():
            return
        for widget in self._history_frame.winfo_children():
            widget.destroy()
        if not self._recent_results:
            tk.Label(self._history_frame, text="Brak wyników w tej sesji",
                     bg=self.config.COLOR_WHITE, fg="#aaaaaa",
                     font=("Arial", 8, "italic")).pack(pady=3)
            return
        for position, entry in enumerate(reversed(self._recent_results)):
            background = "#f5f5f5" if position % 2 == 0 else self.config.COLOR_WHITE
            row = tk.Frame(self._history_frame, bg=background)
            row.pack(fill=tk.X)
            result_color = (self.config.COLOR_ACCENT
                            if entry["result"] == "PASS"
                            else self.config.COLOR_ERROR)
            for text, width, color in (
                (entry["time"], 8, "#666666"),
                (entry["serial"], 20, "#333333"),
                (entry["model"], 14, "#333333"),
                (entry["result"], 6, result_color),
                (entry["note"], 18, self.config.COLOR_ERROR),
            ):
                tk.Label(row, text=text, bg=background, fg=color,
                         font=("Arial", 8), width=width, pady=2).pack(side=tk.LEFT)

    # ------------------------------------------------------------------ #
    # POLACZENIA
    # ------------------------------------------------------------------ #
    def _connect_device(self) -> None:
        try:
            from hipot_device import ChromaDevice

            self.device = ChromaDevice(
                port=self.config.DEVICE_COM_PORT,
                baudrate=self.config.DEVICE_BAUDRATE,
                parity=self.config.DEVICE_PARITY,
                flow_control=self.config.DEVICE_FLOW_CONTROL,
                dialect=self.config.dialect(),
            )
            self.status_label.config(
                text=f"Łączenie z Chroma {self.config.INSTRUMENT_MODEL}...",
                fg=self.config.COLOR_WARNING)
            if self.device.connect():
                self._configure_device()
            else:
                reason = (getattr(self.device, "last_connect_error", "")
                          or f"Brak połączenia z Chroma na "
                             f"{self.config.DEVICE_COM_PORT}")
                self._set_verdict("BRAK TESTERA", self.config.COLOR_ERROR)
                self.status_label.config(
                    text=f"⛔ {reason}  —  zawołaj technologa",
                    fg=self.config.COLOR_ERROR)
                self._block_all_controls()
        except Exception as exc:
            print(f"[TEST_SCREEN] {exc!r}")
            traceback.print_exc()
            self.status_label.config(text=f"✗ Błąd: {exc}",
                                     fg=self.config.COLOR_ERROR)
            self.start_button.config(state="disabled")

    def _configure_device(self) -> None:
        self._device_configured = False
        self.start_button.config(state="disabled")
        try:
            self.device.clear_steps()
            self.device.configure_profile(self.profile)
            self._device_configured = True
            self._warn_if_sampling_too_slow()
            self.status_label.config(
                text=f"✓ Chroma skonfigurowana — {self.profile.step_count} "
                     f"kroków profilu {self.profile.display_name}",
                fg=self.config.COLOR_ACCENT)
            self._attempt_safe_start()
        except Exception as exc:
            print(f"[TEST_SCREEN] {exc!r}")
            traceback.print_exc()
            self._device_configured = False
            self.status_label.config(
                text=f"⛔ Błąd konfiguracji — test zablokowany: {exc}",
                fg=self.config.COLOR_ERROR)
            self.start_button.config(state="disabled")

    def _warn_if_sampling_too_slow(self) -> None:
        """Ostrzega, gdy dwell jest za krotki na zebranie wymaganych probek.

        Kazda iteracja petli to zapytanie statusu + FETCh. Przy 9600 bodach i
        dwell rzedu 1 s margines na ``MIN_IN_RANGE_SAMPLES`` jest cienki -
        technolog musi o tym wiedzieć ZANIM zacznie produkcje, a nie po serii
        odrzuconych PASS-ow.
        """
        try:
            poll = self.device.measure_poll_cycle()
        except Exception as exc:
            print(f"[SAMPLING] Nie udalo sie zmierzyc cyklu odpytania: {exc}")
            poll = max(self.device.last_poll_interval, 0.001) * 2.0
        poll = max(poll, 0.001)

        shortest = min(float(step["dwell"]) for step in self.profile.steps)
        estimated = shortest / poll
        print(f"[SAMPLING] Zmierzony cykl odpytania: {poll * 1000:.0f} ms -> "
              f"~{estimated:.1f} próbek na krok (dwell {shortest:.2f} s, "
              f"wymagane {MIN_IN_RANGE_SAMPLES})")

        # Zapas x2 nad minimum, nie +1: przy zapasie rownym jednej probce
        # jedno zgubione FETCh wystarczy, zeby poprawny PASS zostal odrzucony
        # jako niepotwierdzony.
        if estimated < MIN_IN_RANGE_SAMPLES * 2:
            # WLASNA etykieta - wczesniej ostrzezenie szlo do status_label
            # i bylo kasowane w nastepnej linii, wiec nigdy nie bylo widoczne.
            self.sampling_warning_label.config(
                text=f"⚠ Przy {self.config.DEVICE_BAUDRATE} bodach zdążę zebrać "
                     f"~{estimated:.0f} próbek na krok (wymagane "
                     f"{MIN_IN_RANGE_SAMPLES}, bez zapasu) — podnieś baudrate "
                     f"w menu SYSTEM testera i w panelu Stanowisko. "
                     f"Poprawne sztuki mogą być odrzucane.")
            if not self.sampling_warning_label.winfo_ismapped():
                self.sampling_warning_label.pack(pady=(0, 4))
        elif self.sampling_warning_label.winfo_ismapped():
            self.sampling_warning_label.pack_forget()

    def _connect_interlock(self) -> None:
        if not self._interlock_enforced():
            self._block_interlock("⛔ Interlock programowy wyłączony — test zablokowany")
            return
        port = getattr(self.config, "INTERLOCK_PORT", None)
        if not port:
            self._block_interlock("⛔ Brak portu Arduino — test zablokowany")
            return

        try:
            from interlock import InterlockMonitor

            self.interlock = InterlockMonitor(
                port=port,
                baudrate=getattr(self.config, "INTERLOCK_BAUDRATE", 9600),
                heartbeat_timeout=2.0,
                expected_identity=getattr(self.config,
                                          "INTERLOCK_IDENTITY", ""),
            )
        except Exception as exc:
            print(f"[TEST_SCREEN] {exc!r}")
            traceback.print_exc()
            # Kazda awaria tutaj (brak biblioteki, zly baudrate w konfiguracji)
            # musi zablokowac test z czytelnym komunikatem, a nie przewrocic
            # calego ekranu testowego.
            self.interlock = None
            self._block_interlock(
                f"⛔ Nie można uruchomić interlocka — test zablokowany: {exc}")
            return

        if self.interlock.connect():
            self.interlock.set_on_change(self._on_interlock_change)
            self.interlock.start_monitoring()
            self.interlock_label.config(
                text="⏳ Oczekiwanie na aktualny stan klapy...",
                fg=self.config.COLOR_WARNING, bg="#fff8e1")
            self.start_button.config(state="disabled")
        else:
            self._block_interlock(
                f"⛔ Brak komunikacji z Arduino ({port}) — test zablokowany")

    def _block_interlock(self, message: str, *, offer_retry: bool = False
                         ) -> None:
        self.interlock_label.config(text=message, fg=self.config.COLOR_ERROR,
                                    bg="#ffebee")
        self.interlock_frame.config(bg="#ffebee")
        self.start_button.config(state="disabled")
        button = getattr(self, "interlock_retry_btn", None)
        if button is None:
            return
        if offer_retry and self.interlock is not None:
            if not button.winfo_ismapped():
                button.pack(pady=(0, 8))
            button.config(state="normal", text="↻ Połącz ponownie z interlockiem")
        elif button.winfo_ismapped():
            button.pack_forget()

    def _retry_interlock(self) -> None:
        """Ponowne polaczenie z Arduino bez powrotu do menu.

        Nie oslabia blokady: po ponownym polaczeniu stan klapy jest nieznany,
        a start i tak wymaga swiezego przejscia OPEN -> CLOSED.
        """
        if self.interlock is None or self.test_running:
            return
        self.interlock_retry_btn.config(state="disabled", text="↻ Łączenie...")
        self._current_interlock_closed = None
        self._valid_close_transition = False
        self._lid_open_seen = False
        self._prev_interlock_closed = None

        def worker() -> None:
            try:
                ok = self.interlock.reconnect()
            except Exception as exc:
                ok = False
                print(f"[INTERLOCK] Ponowne połączenie nieudane: {exc}")
            self._post_ui(lambda: self._after_interlock_retry(ok))

        threading.Thread(target=worker, daemon=True).start()

    def _after_interlock_retry(self, ok: bool) -> None:
        if ok:
            if self.interlock_retry_btn.winfo_ismapped():
                self.interlock_retry_btn.pack_forget()
            self.interlock_label.config(
                text="⏳ Połączono ponownie — oczekiwanie na stan klapy...",
                fg=self.config.COLOR_WARNING, bg="#fff8e1")
            self.interlock_frame.config(bg="#fff8e1")
        else:
            self._block_interlock(
                f"⛔ Nadal brak połączenia z Arduino "
                f"({getattr(self.config, 'INTERLOCK_PORT', '?')}) — "
                "sprawdź kabel USB i port w panelu",
                offer_retry=True)

    # ------------------------------------------------------------------ #
    # INTERLOCK
    # ------------------------------------------------------------------ #
    def _on_interlock_change(self, closed) -> None:
        self._post_ui(lambda: self._apply_interlock_state(closed))

    def _interlock_enforced(self) -> bool:
        return bool(getattr(self.config, "INTERLOCK_ENABLED", True))

    def _interlock_ready(self) -> bool:
        return bool(
            self._interlock_enforced()
            and self.interlock
            and self.interlock.connected
            and self._current_interlock_closed is not None
        )

    def _attempt_safe_start(self) -> bool:
        """Jedyna bramka automatycznego startu testu."""
        if self.test_running or self._result_pending or self._test_aborted:
            return False
        if not self.device or not self.device.connected:
            self.status_label.config(
                text="⛔ Start zablokowany — brak połączenia z Chroma",
                fg=self.config.COLOR_ERROR)
            return False
        if not self._device_configured:
            self.status_label.config(
                text="⛔ Start zablokowany — Chroma nie jest poprawnie skonfigurowana",
                fg=self.config.COLOR_ERROR)
            return False
        if not self._serial_ready_for_test:
            return False
        if not self._interlock_enforced():
            self.start_button.config(state="disabled")
            self.status_label.config(
                text="⛔ Start zablokowany — interlock programowy musi być aktywny",
                fg=self.config.COLOR_ERROR)
            return False
        if not self._interlock_ready():
            self.status_label.config(
                text="⛔ Start zablokowany — brak aktualnego sygnału interlocka",
                fg=self.config.COLOR_ERROR)
            return False
        if self._current_interlock_closed is not True:
            self.status_label.config(
                text="SN zaakceptowany — zamknij klapę, aby rozpocząć test",
                fg=self.config.COLOR_WARNING)
            return False
        if not self._valid_close_transition:
            self.status_label.config(
                text="⛔ Otwórz klapę, wymień urządzenie i zamknij ją ponownie",
                fg=self.config.COLOR_ERROR)
            return False

        self._start_test()
        return True

    def _apply_interlock_state(self, closed) -> None:
        self._current_interlock_closed = closed

        if closed is None:
            self._valid_close_transition = False
            self._lid_open_seen = False
            self.start_button.config(state="disabled")
            self._block_interlock(
                "⛔ Utracono komunikację z Arduino — test zablokowany",
                offer_retry=True)
            if self.test_running:
                self._abort_running_test(
                    status="⛔ Test przerwany — utrata komunikacji z interlockiem",
                    title="Utrata interlocka",
                    message=("Utracono komunikację z Arduino podczas testu.\n"
                             "Test został zatrzymany, a kolejne uruchomienie "
                             "jest zablokowane."),
                    icon="error")
            return

        if closed:
            if self._prev_interlock_closed is not False or not self._lid_open_seen:
                self._valid_close_transition = False
                self.interlock_label.config(
                    text="🔒 Klapa ZAMKNIĘTA — przed nowym testem otwórz ją "
                         "i zamknij ponownie",
                    fg=self.config.COLOR_ACTION,
                    bg=self.config.COLOR_ACTION_BG)
                self.interlock_frame.config(bg=self.config.COLOR_ACTION_BG)
                self.start_button.config(state="disabled")
                self._prev_interlock_closed = True
                return

            self._valid_close_transition = True
            self._lid_open_seen = False
            self.interlock_label.config(
                text="🔒 Klapa ZAMKNIĘTA — sprawdzam gotowość testu...",
                fg=self.config.COLOR_ACCENT, bg="#e8f5e9")
            self.interlock_frame.config(bg="#e8f5e9")
            self._prev_interlock_closed = True

            if self.sn_dialog is not None and self.sn_dialog.winfo_exists():
                if not self._try_auto_confirm_sn():
                    self._valid_close_transition = False
                    return
            self._attempt_safe_start()
            return

        # OPEN uzbraja dokladnie jedno nastepne zamkniecie.
        self._lid_open_seen = True
        self._valid_close_transition = False
        self._prev_interlock_closed = False
        # Klapa otwarta to stan NORMALNY przy kazdej wymianie sztuki.
        # Pokazywanie go czerwienia sprawialo, ze operator przestawal
        # reagowac na czerwony - a wtedy prawdziwy FAIL tez przestawal dzialac.
        self.interlock_label.config(
            text="🔓 Klapa OTWARTA — włóż urządzenie, zeskanuj SN i zamknij klapę",
            fg=self.config.COLOR_ACTION, bg=self.config.COLOR_ACTION_BG)
        self.interlock_frame.config(bg=self.config.COLOR_ACTION_BG)

        if self.test_running:
            if self._cycle_terminal_seen:
                # HV juz zgaszone. Nie kasujemy poprawnego wyniku tylko dlatego,
                # ze operator szybko otworzyl klape.
                self.start_button.config(state="disabled")
                self.stop_button.config(state="disabled")
                self.status_label.config(
                    text="⏳ Test zakończony — finalizuję świeży wynik...",
                    fg=self.config.COLOR_WARNING)
                return
            self._abort_running_test(
                status="⛔ Test przerwany — klapa została otwarta!",
                title="Test przerwany",
                message=("Klapa została otwarta podczas testu.\n"
                         "Test został automatycznie zatrzymany.\n\n"
                         "Wróć do menu, aby ponownie połączyć i skonfigurować "
                         "tester."),
                icon="warning")
        else:
            self.start_button.config(state="disabled")

    def _stop_device_now(self) -> None:
        """Wysyla STOP natychmiast, potwierdzenie sprawdza w tle.

        Wywolywane z watku Tk. Sam zapis STOP jest szybki (przerwanie
        trwajacej wymiany + krotka blokada), ale POTWIERDZENIE wymaga kilku
        zapytan do testera - gdyby szlo tu synchronicznie, zamrozilo by okno
        na czas, ktory wlasnie probujemy skrocic.
        """
        if not self.device:
            return
        try:
            sent, message = self.device.request_stop()
        except Exception as exc:
            sent, message = False, str(exc)
            print(f"[STOP] Blad wysylki STOP: {exc}")
            traceback.print_exc()
        if not sent:
            self._post_ui(lambda m=message: self._show_stop_not_confirmed(m))
            return

        def worker() -> None:
            try:
                confirmed, detail = self.device.confirm_stopped()
            except Exception as exc:
                confirmed, detail = False, str(exc)
                print(f"[STOP] Blad weryfikacji zatrzymania: {exc}")
                traceback.print_exc()
            if not confirmed:
                self._post_ui(lambda d=detail: self._show_stop_not_confirmed(d))
            else:
                print(f"[STOP] {detail}")

        threading.Thread(target=worker, daemon=True).start()

    def _show_stop_not_confirmed(self, detail: str) -> None:
        """Tester nie potwierdzil zatrzymania - operator musi zareagowac.

        To nie jest komunikat informacyjny. Dopoki nie ma potwierdzenia, nie
        wiadomo, czy na wyrobie nie ma nadal wysokiego napiecia.
        """
        self._block_all_controls()
        self.status_label.config(
            text="⛔ NIE POTWIERDZONO ZATRZYMANIA TESTERA — "
                 "naciśnij czerwony STOP na panelu Chromy, "
                 "nie otwieraj klapy do zgaśnięcia HV",
            fg=self.config.COLOR_ERROR)
        self._set_verdict("BRAK POTWIERDZENIA STOP", self.config.COLOR_ERROR)
        print(f"[STOP] BRAK POTWIERDZENIA: {detail}")
        messagebox.showerror(
            "Nie potwierdzono zatrzymania testera",
            "Aplikacja wysłała STOP, ale tester tego nie potwierdził.\n\n"
            f"Szczegóły: {detail}\n\n"
            "1. Naciśnij czerwony przycisk STOP na panelu Chromy.\n"
            "2. Odczekaj do zgaśnięcia wskaźnika wysokiego napięcia.\n"
            "3. Dopiero wtedy otwórz klapę.\n"
            "4. Zawołaj technologa — stanowisko wymaga sprawdzenia.",
            parent=self.parent)

    def _block_all_controls(self) -> None:
        for name in ("start_button", "stop_button", "next_sn_button"):
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.config(state="disabled")
                except tk.TclError:
                    pass

    def _abort_running_test(self, *, status: str, title: str, message: str,
                            icon: str) -> None:
        was_running = self.test_running
        self._test_aborted = True
        self.test_running = False
        self._device_configured = False
        self._serial_ready_for_test = False
        self._stop_device_now()
        if was_running:
            self._record_incomplete_run("PRZERWANY", title)
            self._set_verdict("PRZERWANY", self.config.COLOR_WARNING)
        self.stop_button.config(state="disabled")
        self.back_button.config(state="normal")
        self.status_label.config(text=status, fg=self.config.COLOR_ERROR)
        self._offer_recovery()
        show = messagebox.showerror if icon == "error" else messagebox.showwarning
        show(title, message, parent=self.parent)

    # ------------------------------------------------------------------ #
    # PRZEBIEG TESTU
    # ------------------------------------------------------------------ #
    def _start_test(self) -> None:
        if self._closed or self.test_running or self._result_pending or self._test_aborted:
            return

        guards = (
            (self.device and self.device.connected,
             "⛔ Start zablokowany — brak połączenia z Chroma"),
            (self._device_configured,
             "⛔ Start zablokowany — brak potwierdzonej konfiguracji Chromy"),
            (self._serial_ready_for_test,
             "⛔ Start zablokowany — brak zatwierdzonego SN"),
            (self._interlock_enforced(),
             "⛔ Start zablokowany — interlock programowy jest wyłączony"),
            (self._interlock_ready(),
             "⛔ Start zablokowany — brak aktualnego sygnału interlocka"),
            (self._current_interlock_closed is True,
             "⛔ Start zablokowany — klapa jest otwarta"),
            (self._valid_close_transition,
             "⛔ Start zablokowany — wymagane otwarcie i ponowne zamknięcie klapy"),
        )
        for condition, message in guards:
            if not condition:
                self.status_label.config(text=message, fg=self.config.COLOR_ERROR)
                return

        self._valid_close_transition = False
        self._reset_cycle_state()
        self._run_id += 1
        run_id = self._run_id
        self._last_display_refresh = 0.0
        self.test_running = True
        self.start_time = time.monotonic()

        self.sn_display_label.config(text=self.serial)
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.back_button.config(state="disabled")
        self.next_sn_button.config(state="disabled")
        self.status_label.config(text="🔄 Uruchamianie nowego cyklu Hi-Pot...",
                                 fg=self.config.COLOR_WARNING)
        self._reset_step_rows()

        self.test_thread = threading.Thread(
            target=self._run_test_background, args=(run_id,), daemon=True)
        self.test_thread.start()

    def _reset_cycle_state(self) -> None:
        self._test_completed_called = False
        self._test_aborted = False
        self._result_pending = False
        self._cycle_active_seen = False
        self._cycle_terminal_seen = False
        self._current_step = 0
        for evidence in self._evidence.values():
            evidence.reset()

    def _reset_step_rows(self) -> None:
        for labels in self._step_rows.values():
            labels["Wynik"].config(text="—", fg="#999999")
            labels["Nazwa"].config(bg=labels["_bg"])

    def _run_test_background(self, run_id: int) -> None:
        try:
            if not self.device.start_test():
                raise RuntimeError(
                    "Chroma nie potwierdziła rozpoczęcia NOWEGO cyklu testowego. "
                    "Wynik z poprzedniej sztuki nie został użyty."
                )

            steps = self.profile.steps
            total_time = self.profile.total_duration
            minimum_runtime = max(1.0, total_time * PASS_MIN_RUNTIME_FRACTION)
            configured_timeout = float(self.profile.test_timeout_s)
            max_runtime = min(configured_timeout, max(total_time + 10.0, 15.0))

            consecutive_comm_errors = 0
            terminal_status = None
            elapsed = 0.0

            # K6: okno mierzone bramkami czasu musi zaczynac sie od zapisu
            # SAFEty:STARt, a nie od momentu utworzenia watku. Cala sekwencja
            # startowa (STOP, SYST:ERR?, pomiar bazowy, ponowienia) potrafi
            # zajac kilkanascie sekund przy chwiejnym RS232. Liczona od
            # start_time bramka minimum_runtime przepuszczala wtedy cykl
            # trwajacy 1 s jako PASS, a max_runtime odrzucalo cykl poprawny.
            print(f"[TEST] Start cyklu | S/N {self.serial} | "
                  f"profil {self.profile.product_id}")
            cycle_started = self.device.cycle_started_monotonic()
            if cycle_started is None:
                raise RuntimeError(
                    "Brak znacznika czasu rozpoczecia cyklu w testerze")
            self.start_time = cycle_started

            while (self.test_running and not self._test_aborted
                   and not self._closed and run_id == self._run_id):
                now = time.monotonic()
                elapsed = now - cycle_started
                status = self.device.get_status()

                if status == "COMM_ERROR":
                    consecutive_comm_errors += 1
                    if consecutive_comm_errors >= 3:
                        raise RuntimeError(
                            "Utracono komunikację z Chroma podczas testu")
                    time.sleep(0.15)
                    continue
                consecutive_comm_errors = 0
                if status in ("TESTING", "RUNNING"):
                    # Ustawiane WYLACZNIE z obserwacji stanu testera.
                    # Wczesniej flaga byla ustawiana bezwarunkowo zaraz po
                    # start_test(), wiec warunek nizej nigdy nie byl prawdziwy
                    # i bramka byla atrapa.
                    self._cycle_active_seen = True

                measurements = self.device.read_measurements()
                if measurements:
                    self._record_sample(measurements, steps)

                self.elapsed_time = elapsed
                # Zbieranie probek zostaje bez zmian - od niego zaleza dowody.
                # Odswiezanie EKRANU dlawimy do ~5 Hz: przy petli co 20 ms
                # najwieksze liczby na ekranie migotaly 20-40 razy na sekunde
                # i byly nieczytelne.
                if now - self._last_display_refresh >= self.DISPLAY_REFRESH_S:
                    self._last_display_refresh = now
                    self._post_ui(self._update_display)

                if status in ("STOPPED", "STOP", "PASS", "FAIL"):
                    terminal_status = status
                    self._cycle_terminal_seen = True
                    if not self._cycle_active_seen:
                        raise RuntimeError(
                            "Odebrano wynik bez potwierdzenia aktywnego cyklu — "
                            "możliwy stary wynik")
                    break

                if elapsed > max_runtime:
                    raise TimeoutError(
                        f"Przekroczono maksymalny czas testu ({max_runtime:.1f} s)")
                # RS232 tego testera konczy sie na 19200 bodach, a krok profilu
                # SR203/SR204 trwa 2 s. Kazda zbedna przerwa w petli zabiera
                # probki potrzebne do udowodnienia obciazenia na danym porcie.
                time.sleep(0.02)

            if (self._test_aborted or not self.test_running or self._closed
                    or run_id != self._run_id):
                return
            if terminal_status is None:
                raise RuntimeError("Brak jednoznacznego zakończenia bieżącego cyklu")

            result, data = self.device.get_cycle_results(steps)
            if result not in ("PASS", "FAIL"):
                raise RuntimeError(
                    "Nie udało się pobrać jednoznacznego, świeżego wyniku testu: "
                    + str(data.get("error", ""))
                )
            if not data.get("fresh_cycle"):
                raise RuntimeError("Wynik nie został przypisany do bieżącego cyklu")
            # ``elapsed`` jest liczone od znacznika ZAPISU SAFEty:STARt
            # (patrz K6 wyzej), wiec mierzy okno samego cyklu, bez narzutu
            # sekwencji startowej.
            if result == "PASS" and elapsed < minimum_runtime:
                raise RuntimeError(
                    f"PASS pojawił się zbyt szybko ({elapsed:.1f} s; "
                    f"minimum {minimum_runtime:.1f} s) — wynik odrzucony")

            self._validate_evidence(result, terminal_status, data)

            self._result_pending = True
            self.test_running = False
            self._post_ui(lambda r=result, d=data: self._test_completed(r, d))

        except Exception as exc:
            print(f"[TEST_SCREEN] {exc!r}")
            traceback.print_exc()
            if self._test_aborted or self._closed or run_id != self._run_id:
                return
            try:
                confirmed, detail = self.device.stop_test()
                if not confirmed:
                    self._post_ui(
                        lambda d=detail: self._show_stop_not_confirmed(d))
            except Exception as stop_error:
                print(f"[STOP] Blad zatrzymania po wyjatku: {stop_error}")
                traceback.print_exc()
            self._post_ui(lambda message=str(exc): self._test_error(message))

    def _record_sample(self, measurements: dict, steps) -> None:
        """Przypisuje pomiar do kroku raportowanego przez Chrome."""
        step_number = int(measurements.get("step") or 0)
        voltage = float(measurements["output_voltage"])
        current_ma = float(measurements["measure_current"]) * 1000.0

        self.current_voltage = voltage
        self.current_current = current_ma
        if step_number and step_number != self._current_step:
            self._current_step = step_number
            self._post_ui(self._highlight_current_step)

        if voltage >= 50.0:
            self._cycle_active_seen = True
        if not 1 <= step_number <= len(steps):
            return

        step = steps[step_number - 1]
        evidence = self._evidence[step_number]
        evidence.samples += 1
        evidence.max_voltage = max(evidence.max_voltage, voltage)
        evidence.max_current_ma = max(evidence.max_current_ma, current_ma)

        target_voltage = float(step["voltage"])
        effective_low = self.profile.effective_low_ma(step)
        high_limit = float(step["limit_high"])

        # Dowod obciazenia musi pochodzic z pomiaru NA ZYWO przy pelnym
        # napieciu - nie z rejestru wyniku poprzedniej sztuki.
        if voltage >= target_voltage * 0.90:
            if effective_low <= current_ma <= high_limit:
                evidence.in_range_samples += 1
            evidence.note_overcurrent(current_ma > high_limit)
        else:
            # Ponizej 90% napiecia docelowego trwa rampa - tam nadmiar
            # nie znaczy nic. Seria musi zaczac sie od nowa.
            evidence.note_overcurrent(False)

    def _validate_evidence(self, result: str, terminal_status: Optional[str],
                           data: dict) -> None:
        step_results = data.get("steps", [])
        validate_cycle_pass_evidence(
            result=result,
            terminal_status=terminal_status,
            step_results=step_results,
            expected_steps=self.profile.step_count,
        )
        if result != "PASS":
            return
        for entry in step_results:
            index = int(entry["index"])
            step = self.profile.steps[index - 1]
            evidence = self._evidence[index]
            margin = evidence.in_range_samples - MIN_IN_RANGE_SAMPLES
            print(f"[DOWODY] Krok {index} ({step['name']}): próbek "
                  f"{evidence.samples}, w zakresie {evidence.in_range_samples}, "
                  f"max {evidence.max_voltage:.0f} V / "
                  f"{evidence.max_current_ma:.3f} mA"
                  + (f"  ⚠ ZAPAS {margin} nad wymaganym minimum "
                     f"{MIN_IN_RANGE_SAMPLES} - jedno zgubione FETCh odrzuci "
                     f"poprawny PASS" if margin <= 0 else ""))
            validate_step_pass_evidence(
                step_name=step["name"],
                step_index=index,
                target_voltage=float(step["voltage"]),
                effective_low_ma=self.profile.effective_low_ma(step),
                high_limit_ma=float(step["limit_high"]),
                final_voltage=float(entry["output_voltage"]),
                final_current_ma=float(entry["measured_current"]),
                cycle_max_voltage=evidence.max_voltage,
                in_range_samples=evidence.in_range_samples,
                overcurrent_seen=evidence.overcurrent_seen,
            )

    # ------------------------------------------------------------------ #
    # AKTUALIZACJA WIDOKU
    # ------------------------------------------------------------------ #
    def _update_display(self) -> None:
        self.voltage_label.config(text=f"{int(self.current_voltage)} V")
        self.current_label.config(text=f"{self.current_current:.2f} mA")
        self.time_label.config(text=f"{self.elapsed_time:.1f} s")
        total = self.profile.total_duration
        progress = min(self.elapsed_time / total, 1.0) if total > 0 else 0.0
        width = self.progress_canvas.winfo_width()
        self.progress_canvas.coords(self.progress_rect, 0, 0, width * progress, 26)

    def _highlight_current_step(self) -> None:
        for index, labels in self._step_rows.items():
            active = index == self._current_step
            labels["Nazwa"].config(
                bg="#fff59d" if active else labels["_bg"],
                font=("Arial", 9, "bold") if active else ("Arial", 9))
        if 1 <= self._current_step <= self.profile.step_count:
            name = self.profile.steps[self._current_step - 1]["name"]
            self.live_title.config(
                text=f"Pomiary na żywo — krok {self._current_step}/"
                     f"{self.profile.step_count}: {name}")

    def _apply_step_results(self, step_results) -> None:
        for entry in step_results:
            labels = self._step_rows.get(int(entry["index"]))
            if not labels:
                continue
            passed = entry["result"] == "PASS"
            labels["Wynik"].config(
                text=entry["result"],
                fg=self.config.COLOR_ACCENT if passed else self.config.COLOR_ERROR)
            labels["Nazwa"].config(bg=labels["_bg"], font=("Arial", 9))

    # ------------------------------------------------------------------ #
    # ZAKONCZENIE
    # ------------------------------------------------------------------ #
    def _test_completed(self, result: str, data: dict) -> None:
        if self._test_completed_called or self._test_aborted or self._closed:
            self._result_pending = False
            return
        self._test_completed_called = True
        self.test_running = False
        self._result_pending = False

        self._serial_ready_for_test = False
        self._valid_close_transition = False
        self._lid_open_seen = self._current_interlock_closed is False
        if self._lid_open_seen:
            self._prev_interlock_closed = False

        self.test_result = result
        print(f"[TEST] Wynik {result} | S/N {self.serial} | "
              f"profil {self.profile.product_id}")
        step_results = data.get("steps", [])
        self._apply_step_results(step_results)

        self._needs_recovery = False
        self.stop_button.config(state="disabled")
        self.back_button.config(state="normal")
        self.next_sn_button.config(state="normal", text="➜ Następny SN",
                                   bg="#607D8B")

        failed_step = data.get("failed_step", "")
        # W4: operator ma wiedziec TYLKO czy PASS czy FAIL - to wystarczy,
        # zeby wiedziec, co zrobic ze sztuka. Ktory krok oblal jest w tabeli
        # kroków i w raporcie, dla technologa.
        if result == "PASS":
            self._set_verdict("PASS", self.config.COLOR_ACCENT)
            self.status_label.config(
                text="Wyrób dobry — odłóż na OK i weź następny",
                fg=self.config.COLOR_ACCENT)
        else:
            self._set_verdict("FAIL", self.config.COLOR_ERROR)
            self.status_label.config(
                text="Wyrób wadliwy — odłóż na NOK",
                fg=self.config.COLOR_ERROR)

        if getattr(self.config, "AUTO_SAVE_RESULTS", True):
            self._queue_report(result, step_results)

        self._recent_results.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "serial": self.serial,
            "model": self.model_name,
            "result": result,
            "note": failed_step if result != "PASS" else "",
        })
        self._recent_results = self._recent_results[-5:]
        self._refresh_history()

        if self._interlock_enforced():
            # Stan normalny wymagajacy dzialania - kolor akcji, nie awarii.
            if self._current_interlock_closed is False:
                text = "🔓 Test zakończony — zeskanuj następny SN i zamknij klapę"
            else:
                text = "🔒 Test zakończony — otwórz klapę przed następnym testem"
            self.interlock_label.config(text=text,
                                        fg=self.config.COLOR_ACTION,
                                        bg=self.config.COLOR_ACTION_BG)
            self.interlock_frame.config(bg=self.config.COLOR_ACTION_BG)

        self._next_dialog_after_id = self.parent.after(
            300, lambda: None if self._closed else self._show_next_sn_dialog(result))

    def _queue_report(self, result: str, step_results) -> None:
        # Kopie wartosci chronia zapis przed zmiana SN w kolejnym cyklu.
        report_args = {
            "instrument_model": self.config.INSTRUMENT_MODEL,
            "program": self.profile.report_program,
            "serial": self.serial,
            "overall_result": result,
            "steps": [dict(entry) for entry in step_results],
            "profile_steps": [dict(step) for step in self.profile.steps],
            "effective_low_ma": [
                self.profile.effective_low_ma(step) for step in self.profile.steps
            ],
            "log_dir": self.config.LOG_DIR,
        }
        self._report_threads = [t for t in self._report_threads if t.is_alive()]
        thread = threading.Thread(target=self._save_report_background,
                                  args=(report_args,), daemon=True)
        self._report_threads.append(thread)
        thread.start()

    def _save_report_background(self, report_args: dict) -> None:
        log_dir = report_args["log_dir"]
        try:
            result = save_report(**report_args)
        except Exception as exc:
            print(f"[LOG] Błąd zapisu raportu: {exc}")
            traceback.print_exc()
            self._post_ui(lambda error=str(exc): self._report_write_failed(error))
            return

        fallback = bool(getattr(result, "fallback_used", False))
        reason = str(getattr(result, "reason", ""))
        if not fallback:
            # Udzial znowu odpowiada - dosylamy to, co zostalo lokalnie.
            try:
                sent, remaining, error = flush_pending_reports(log_dir)
            except Exception as exc:
                sent, remaining, error = 0, count_pending_reports(), str(exc)
                print(f"[LOG] Blad dosylki raportow: {exc}")
        else:
            sent, remaining, error = 0, count_pending_reports(), reason

        self._post_ui(lambda f=fallback, r=remaining, e=error, s=sent:
                      self._update_storage_state(f, r, e, s))

    def _report_write_failed(self, error: str) -> None:
        self.storage_warning_label.config(
            text="⛔ NIE ZAPISANO RAPORTU — ani na serwerze, ani lokalnie. "
                 "Zawołaj technologa.",
            fg=self.config.COLOR_ERROR, bg="#FFEBEE")
        if not self.storage_warning_label.winfo_ismapped():
            self.storage_warning_label.pack(fill=tk.X, pady=(2, 4))
        messagebox.showerror(
            "Błąd zapisu raportu",
            f"Nie udało się zapisać raportu ani kopii awaryjnej:\n{error}\n\n"
            "Zawołaj technologa — wyniki nie są zapisywane.",
            parent=self.parent)

    def _update_storage_state(self, fallback: bool, remaining: int,
                              error: str, sent: int = 0) -> None:
        """W5: stan zapisu raportow musi byc WIDOCZNY.

        Wczesniej zapis do katalogu awaryjnego zostawial slad wylacznie
        w konsoli, ktorej w buildzie --windowed nikt nie widzi. Awaria
        udzialu na jedna zmiane = kilkaset raportow lokalnie i nikt sie
        o tym nie dowiadywal.
        """
        label = getattr(self, "storage_warning_label", None)
        if label is None:
            return
        if sent:
            print(f"[LOG] Doslano {sent} zaleglych raportow")
        if fallback or remaining:
            detail = f" ({error})" if error else ""
            label.config(
                text=f"⚠ Raporty zapisują się LOKALNIE, nie na serwerze"
                     f"{detail}. Czeka na wysyłkę: {remaining}. "
                     f"Zawołaj technologa.",
                fg=self.config.COLOR_WARNING, bg="#FFF3E0")
            if not label.winfo_ismapped():
                label.pack(fill=tk.X, pady=(2, 4))
        elif label.winfo_ismapped():
            label.pack_forget()

    def _record_incomplete_run(self, kind: str, detail: str) -> None:
        """Rejestruje przebieg, ktory NIE dal wyniku.

        Swiadomie NIE zapisujemy pliku raportu na udziale: raporty sa
        zaciagane przez webservice do bazy jako wyniki testu, a przebieg
        przerwany albo odrzucony wynikiem nie jest - wpis oznaczylby dobra
        sztuke jako zla. Slad idzie tam, gdzie jego miejsce: do dziennika
        audytowego, do logu sesji i na ekran operatora.
        """
        serial = self.serial or "?"
        print(f"[PRZEBIEG] {kind} | S/N {serial} | {detail}")
        try:
            audit(f"TEST/{kind}",
                  f"S/N {serial}, profil {self.profile.product_id}: {detail}")
        except Exception as exc:
            print(f"[PRZEBIEG] Nie zapisano do dziennika: {exc}")

        self._recent_results.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "serial": serial,
            "model": self.model_name,
            "result": "—",
            "note": kind,
        })
        self._recent_results = self._recent_results[-5:]
        try:
            self._refresh_history()
        except Exception:
            pass

    def _classify_error(self, message: str) -> tuple[str, str]:
        """Rozdziela "test niewazny" od "awaria sprzetu".

        Odrzucenie PASS-a przez warstwe dowodowa to NIE awaria - test jest
        niewazny i sztuke trzeba przetestowac ponownie. Wczesniej operator
        dostawal w obu przypadkach ten sam komunikat jezykiem konstruktora
        i to samo zablokowane stanowisko.
        """
        lowered = message.lower()
        invalid_markers = ("odrzucono pass", "zbyt szybko", "dowod",
                           "pomiarow obciazenia", "pomiarów obciążenia",
                           "swiezego wyniku", "świeżego wyniku",
                           "biezacego cyklu", "bieżącego cyklu")
        if any(marker in lowered for marker in invalid_markers):
            return ("TEST_NIEWAZNY",
                    "Test nieważny — powtórz test tej samej sztuki.\n"
                    "Jeśli powtórzy się drugi raz, zawołaj technologa.")
        return ("AWARIA",
                "Awaria stanowiska — zawołaj technologa.\n"
                "Nie testuj dalej na tym stanowisku.")

    def _test_error(self, message: str) -> None:
        if self._closed:
            return
        self._test_aborted = True
        self.test_running = False
        self._result_pending = False
        self._device_configured = False
        self._serial_ready_for_test = False
        self._valid_close_transition = False
        self._cycle_terminal_seen = False

        for action in (lambda: self.device and self.device.disconnect(),
                       lambda: self.interlock and self.interlock.disconnect()):
            try:
                action()
            except Exception:
                pass

        kind, instruction = self._classify_error(message)
        self._record_incomplete_run(kind, message)

        self._block_all_controls()
        self.back_button.config(state="normal")
        if kind == "TEST_NIEWAZNY":
            self._set_verdict("BRAK WYNIKU", self.config.COLOR_WARNING)
            self.status_label.config(
                text="Test nieważny — powtórz test tej samej sztuki",
                fg=self.config.COLOR_WARNING)
            messagebox.showwarning(
                "Test nieważny",
                f"{instruction}\n\nSzczegóły dla technologa:\n{message}",
                parent=self.parent)
        else:
            self._set_verdict("AWARIA", self.config.COLOR_ERROR)
            self.status_label.config(
                text="Awaria stanowiska — zawołaj technologa",
                fg=self.config.COLOR_ERROR)
            messagebox.showerror(
                "Awaria stanowiska",
                f"{instruction}\n\nSzczegóły dla technologa:\n{message}",
                parent=self.parent)

    def _stop_test(self) -> None:
        if self._closed:
            return
        if self._result_pending:
            self.status_label.config(
                text="⏳ Wynik jest finalizowany — STOP nie jest już wymagany",
                fg=self.config.COLOR_WARNING)
            return
        self._test_aborted = True
        self.test_running = False
        self._device_configured = False
        self._serial_ready_for_test = False
        self._valid_close_transition = False
        self._cycle_terminal_seen = False
        self._stop_device_now()
        self._record_incomplete_run("PRZERWANY", "STOP nacisniety przez operatora")
        self._block_all_controls()
        self.back_button.config(state="normal")
        self._set_verdict("PRZERWANY", self.config.COLOR_WARNING)
        self.status_label.config(
            text="Test przerwany — powtórz test tej samej sztuki",
            fg=self.config.COLOR_WARNING)
        self._offer_recovery()

    # ------------------------------------------------------------------ #
    # NASTEPNY SN
    # ------------------------------------------------------------------ #
    def _go_back(self) -> None:
        if self.test_running or self._result_pending:
            messagebox.showwarning(
                "Cykl w toku",
                "Nie można wrócić do menu podczas testu ani finalizacji wyniku.")
            return
        self._cleanup_and_go_back()

    def _cleanup_and_go_back(self) -> None:
        self.shutdown()
        if self.app_ref:
            self.app_ref.show_scan_screen()

    def _offer_recovery(self) -> None:
        """Po przerwanym tescie daje droge powrotu BEZ wyjscia do menu.

        Przerwanie (otwarta klapa albo STOP) zerowalo ``_device_configured``
        i zostawialo operatorowi wylacznie "Powrot do menu" - czyli
        rozlaczenie testera, przebudowe ekranu i pelna procedure polaczenia
        od nowa. Przy przypadkowym otwarciu klapy to nieproporcjonalna kara.
        Tester jest nadal polaczony, wiec wystarczy przeprogramowac kroki.
        """
        if self._closed or not self.device or not self.device.connected:
            return
        self._needs_recovery = True
        try:
            self.next_sn_button.config(state="normal",
                                       text="➜ Przygotuj kolejną sztukę",
                                       bg=self.config.COLOR_ACCENT)
        except tk.TclError:
            pass

    def _open_sn_dialog_manually(self) -> None:
        if self._needs_recovery:
            self._recover_for_next_unit()
            return
        if self.sn_dialog is not None and self.sn_dialog.winfo_exists():
            self.sn_dialog.lift()
            self.sn_dialog.focus()
            return
        self._show_next_sn_dialog(self.test_result or "BRAK")

    def _recover_for_next_unit(self) -> None:
        """Przeprogramowuje kroki w testerze i wraca do skanowania S/N.

        Programowanie idzie w TLE - przy 9600 bodach pieciokrokowy profil
        z odczytem zwrotnym zajmuje kilkanascie sekund, a robione na watku
        Tk zamrazalo by okno na caly ten czas.
        """
        if self._closed or self.test_running or not self.device:
            return
        self._needs_recovery = False
        self._block_all_controls()
        self.back_button.config(state="normal")
        self._set_verdict("PRZYGOTOWANIE", "#9E9E9E")
        self.status_label.config(
            text="Przygotowuję stanowisko do kolejnej sztuki — chwilę...",
            fg=self.config.COLOR_ACTION)
        try:
            if self.parent is not None:
                self.parent.update_idletasks()
        except tk.TclError:
            pass

        def worker() -> None:
            try:
                self.device.clear_steps()
                self.device.configure_profile(self.profile)
            except Exception as exc:
                print(f"[TEST_SCREEN] Nieudane przygotowanie: {exc!r}")
                traceback.print_exc()
                self._post_ui(lambda e=str(exc): self._recovery_failed(e))
            else:
                self._post_ui(self._recovery_done)

        threading.Thread(target=worker, daemon=True).start()

    def _recovery_done(self) -> None:
        if self._closed:
            return
        self._device_configured = True
        self._serial_ready_for_test = False
        # Swieze przejscie OPEN -> CLOSED jest nadal wymagane - przerwanie
        # niczego tu nie skraca.
        self._valid_close_transition = False
        self._reset_cycle_state()
        self._reset_step_rows()
        self._set_verdict("GOTOWY", "#9E9E9E")
        self._warn_if_sampling_too_slow()
        self.next_sn_button.config(state="normal", text="➜ Następny SN",
                                   bg="#607D8B")
        self.status_label.config(
            text="Stanowisko gotowe — zeskanuj numer seryjny",
            fg=self.config.COLOR_ACTION)
        self._show_next_sn_dialog(self.test_result or "BRAK")

    def _recovery_failed(self, error: str) -> None:
        if self._closed:
            return
        self._device_configured = False
        self._set_verdict("AWARIA", self.config.COLOR_ERROR)
        self.status_label.config(
            text="Nie udało się przygotować stanowiska — wróć do menu "
                 "i połącz ponownie",
            fg=self.config.COLOR_ERROR)
        self._block_all_controls()
        self.back_button.config(state="normal")
        messagebox.showerror(
            "Nie przygotowano stanowiska",
            f"Nie udało się zaprogramować testera.\n\nSzczegóły dla "
            f"technologa:\n{error}",
            parent=self.parent)

    def _show_next_sn_dialog(self, result: str) -> None:
        if self.sn_dialog is not None and self.sn_dialog.winfo_exists():
            self._update_sn_dialog(result)
            self.sn_dialog.lift()
            return

        dialog = tk.Toplevel(self.parent)
        dialog.title("Wynik testu")
        dialog.geometry("560x420")
        dialog.configure(bg=self.config.COLOR_WHITE)
        dialog.transient(self.parent)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", self._back_from_dialog)
        dialog.update_idletasks()
        x = self.parent.winfo_screenwidth() // 2 - 280
        y = self.parent.winfo_screenheight() // 2 - 210
        dialog.geometry(f"+{x}+{y}")
        self.sn_dialog = dialog

        # W4: "Ostatni wynik" mylilo - brzmialo jak wynik POPRZEDNIEJ sztuki.
        # Teraz sam "Wynik", pelna szerokoscia i na kolorowym tle, zeby
        # operator wiedzial tylko jedno: co zrobic z ta sztuka.
        self.sn_result_frame = tk.Frame(dialog, bg=self.config.COLOR_ACCENT)
        self.sn_result_frame.pack(fill=tk.X)
        tk.Label(self.sn_result_frame, text="WYNIK",
                 bg=self.config.COLOR_ACCENT, fg=self.config.COLOR_WHITE,
                 font=("Arial", 11, "bold")).pack(pady=(10, 0))
        self.sn_result_label = tk.Label(
            self.sn_result_frame, text=result, bg=self.config.COLOR_ACCENT,
            fg=self.config.COLOR_WHITE, font=("Arial", 46, "bold"))
        self.sn_result_label.pack(pady=(0, 2))
        self.sn_action_label = tk.Label(
            self.sn_result_frame, text="", bg=self.config.COLOR_ACCENT,
            fg=self.config.COLOR_WHITE, font=("Arial", 15, "bold"))
        self.sn_action_label.pack(pady=(0, 12))

        self.sn_serial_label = tk.Label(
            dialog, text="", bg=self.config.COLOR_WHITE, fg="#555555",
            font=("Consolas", 12))
        self.sn_serial_label.pack(pady=(10, 0))

        tk.Label(dialog, text="Zeskanuj kolejny numer seryjny:",
                 bg=self.config.COLOR_WHITE, fg="#333333",
                 font=("Arial", 12, "bold")).pack(pady=(10, 5))

        self.sn_entry = tk.Entry(dialog, font=("Consolas", 18, "bold"), width=20,
                                 justify="center", relief=tk.SOLID, borderwidth=2)
        self.sn_entry.pack(pady=5, padx=30, ipady=4)
        self.sn_entry.focus_set()
        self.sn_entry.bind("<Return>", lambda event: self._confirm_next_sn())

        self.sn_status_lbl = tk.Label(
            dialog, text=self._sn_instruction(), bg=self.config.COLOR_WHITE,
            fg="#555555", font=("Arial", 10))
        self.sn_status_lbl.pack(pady=(2, 0))

        tk.Button(dialog, text="Powrót do menu", bg=self.config.COLOR_PRIMARY,
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  width=18, relief=tk.FLAT, cursor="hand2",
                  command=self._back_from_dialog).pack(pady=12)
        self._paint_sn_dialog(result)

    def _sn_instruction(self) -> str:
        return ("Otwórz klapę, wymień urządzenie, zeskanuj SN i zamknij klapę"
                if self._current_interlock_closed is True
                else "Zeskanuj SN i zamknij klapę, aby rozpocząć test")

    def _paint_sn_dialog(self, result: str) -> None:
        passed = result == "PASS"
        color = self.config.COLOR_ACCENT if passed else self.config.COLOR_ERROR
        action = ("Odłóż na OK" if passed else "Odłóż na NOK")
        for widget in (self.sn_result_frame, self.sn_result_label,
                       self.sn_action_label):
            widget.config(bg=color)
        for widget in self.sn_result_frame.winfo_children():
            widget.config(bg=color)
        self.sn_result_label.config(text=result)
        self.sn_action_label.config(text=action)
        self.sn_serial_label.config(text=f"S/N: {self.serial}")

    def _update_sn_dialog(self, result: str) -> None:
        self._paint_sn_dialog(result)
        self.sn_entry.config(state="normal")
        self.sn_entry.delete(0, tk.END)
        self.sn_status_lbl.config(text=self._sn_instruction(), fg="#555555")
        self.sn_entry.focus_set()

    def _resolve_serial(self, raw: str):
        """Rozpoznanie S/N wedlug regul profilu, na ktorym pracuje stanowisko.

        Wspolna funkcja z ekranem startowym - okno "nastepny numer seryjny"
        NIE moze miec wlasnej logiki, bo wtedy rozjezdza sie z ekranem
        startowym przy kazdej zmianie regul identyfikacji.
        """
        from product_profile import resolve_serial

        try:
            return resolve_serial(self.profile, raw)
        except Exception as exc:
            print(f"[TEST_SCREEN] {exc!r}")
            traceback.print_exc()
            return False, f"Nie udało się sprawdzić S/N: {exc}"

    def _try_auto_confirm_sn(self) -> bool:
        valid, outcome = self._resolve_serial(self.sn_entry.get())
        if not valid:
            self.sn_status_lbl.config(
                text=f"✗ {outcome} — popraw SN i zamknij klapę ponownie",
                fg=self.config.COLOR_ERROR)
            self.sn_entry.config(state="normal")
            self.sn_entry.focus()
            return False
        if not self._accept_scan(outcome):
            return False
        self._close_sn_dialog()
        return True

    def _confirm_next_sn(self) -> None:
        valid, outcome = self._resolve_serial(self.sn_entry.get())
        if not valid:
            self.sn_status_lbl.config(text=f"✗ {outcome}",
                                      fg=self.config.COLOR_ERROR)
            return
        if not self._accept_scan(outcome):
            return
        self._close_sn_dialog()
        self._attempt_safe_start()

    def _accept_scan(self, scan) -> bool:
        """Przyjmuje kolejny numer seryjny w ramach tego samego profilu.

        Porownanie identyfikatora produktu ze skanu z profilem ekranu
        zostalo usuniete jako martwe: po zlikwidowaniu mapy HWID numer nie
        niesie informacji o produkcie, wiec ``resolve_serial`` z definicji
        zwraca profil, na ktorym pracuje stanowisko. Warunek nie mogl byc
        prawdziwy, a przy czytaniu kodu sugerowal zabezpieczenie, ktorego
        nie ma. Zmiana profilu wymaga powrotu do menu - i tak jest.
        """
        self._apply_new_serial(scan)
        return True

    def _close_sn_dialog(self) -> None:
        if self.sn_dialog is not None:
            try:
                self.sn_dialog.grab_release()
                self.sn_dialog.destroy()
            except Exception:
                pass
            self.sn_dialog = None

    def _apply_new_serial(self, scan) -> None:
        self._needs_recovery = False
        self.serial = scan.serial
        self.model_name = scan.model_name
        self._reset_cycle_state()
        self._reset_step_rows()
        self._serial_ready_for_test = True

        self.start_button.config(state="disabled")
        self.next_sn_button.config(state="disabled")
        self.sn_display_label.config(text=self.serial)
        self.test_result = None
        self.elapsed_time = 0.0
        self.current_voltage = 0.0
        self.current_current = 0.0
        self.voltage_label.config(text="0 V")
        self.current_label.config(text="0.00 mA")
        self.time_label.config(text="0.0 s")
        self.live_title.config(text="Pomiary na żywo")
        self.progress_canvas.coords(self.progress_rect, 0, 0, 0, 26)

        if not self._interlock_ready():
            message = "SN zaakceptowany — oczekiwanie na interlock"
        elif self._current_interlock_closed is True and not self._valid_close_transition:
            message = "SN zaakceptowany — otwórz klapę i zamknij ją ponownie"
        elif self._current_interlock_closed is False:
            message = "SN zaakceptowany — zamknij klapę po włożeniu urządzenia"
        else:
            message = "SN zaakceptowany — gotowy do uruchomienia"
        self.status_label.config(text=message, fg=self.config.COLOR_WARNING)

    def _back_from_dialog(self) -> None:
        self._close_sn_dialog()
        self._cleanup_and_go_back()
