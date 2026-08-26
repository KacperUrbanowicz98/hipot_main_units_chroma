"""Panel inzynieryjny aplikacji Hi-Pot.

Zakladki:
  Stanowisko    - model testera, port RS232, identyfikator stanowiska
  Interlock     - port Arduino (wylaczenie interlocka jest niedostepne)
  Mapa HWID     - HWID -> profil produktu + nazwa modelu
  Profile       - wlaczanie/wylaczanie profili na stanowisku i edycja krokow
  Logi          - sciezka raportow
  Diagnostyka   - sonda SCPI wykrywajaca naglowki odrzucane przez firmware
  Bezpieczenstwo- haslo panelu i dziennik audytowy

Kazda zmiana parametru testowego trafia do dziennika audytowego wraz z wartoscia
poprzednia. Bez tego nie da sie odtworzyc, na jakich nastawach wykonano
konkretna partie.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

from product_profile import ProductProfile
from safety_rules import (
    ABSOLUTE_MIN_PRESENCE_MA,
    SafetyValidationError,
    validate_channel_mask,
    validate_interlock_settings,
    validate_rs232_settings,
    validate_step,
)
from scpi_dialect import supported_models
from security import (
    MIN_PASSWORD_LENGTH,
    audit,
    audit_changes,
    audit_log_path,
    hash_password,
)
from settings_manager import SettingsManager


class AdminPanel:

    def __init__(self, parent, config, catalog):
        self.parent = parent
        self.config = config
        self.catalog = catalog
        self.settings = SettingsManager()
        self.window: Optional[tk.Toplevel] = None
        self._pending_hwid_error: Optional[str] = None
        self._edited_steps: list[dict[str, Any]] = []
        self._edited_profile: Optional[ProductProfile] = None

    # ------------------------------------------------------------------ #
    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title("Panel inżynieryjny")
        self.window.geometry("980x740")
        self.window.configure(bg=self.config.COLOR_BG)
        self.window.transient(self.parent)
        self.window.grab_set()
        self.window.update_idletasks()
        x = self.parent.winfo_screenwidth() // 2 - 490
        y = max(0, self.parent.winfo_screenheight() // 2 - 370)
        self.window.geometry(f"980x740+{x}+{y}")

        self._create_header()
        container = tk.Frame(self.window, bg=self.config.COLOR_BG)
        container.pack(expand=True, fill=tk.BOTH, padx=10, pady=(8, 0))
        self.notebook = ttk.Notebook(container)
        self.notebook.pack(expand=True, fill=tk.BOTH)

        self._create_station_tab()
        self._create_interlock_tab()
        self._create_hwid_tab()
        self._create_profile_tab()
        self._create_logs_tab()
        self._create_diagnostics_tab()
        self._create_security_tab()
        self._create_footer()

    def _create_header(self) -> None:
        header = tk.Frame(self.window, bg=self.config.COLOR_PRIMARY, height=54)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="Panel inżynieryjny", bg=self.config.COLOR_PRIMARY,
                 fg=self.config.COLOR_WHITE, font=("Arial", 16, "bold")).pack(
                     side=tk.LEFT, padx=20)
        tk.Button(header, text="Zamknij", bg=self.config.COLOR_ERROR,
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  relief=tk.FLAT, cursor="hand2",
                  command=self.window.destroy).pack(side=tk.RIGHT, padx=15, pady=12)

    def _create_footer(self) -> None:
        footer = tk.Frame(self.window, bg=self.config.COLOR_PRIMARY, height=32)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(footer, text=self.config.footer_left(),
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=15, pady=7)
        tk.Label(footer, text="Autor: Kacper Urbanowicz",
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 9, "bold")).pack(side=tk.RIGHT, padx=15, pady=7)

    # ------------------------------------------------------------------ #
    # POMOCNICZE
    # ------------------------------------------------------------------ #
    def _card(self, parent) -> tk.Frame:
        card = tk.Frame(parent, bg=self.config.COLOR_WHITE, relief=tk.RAISED,
                        borderwidth=2)
        card.pack(padx=30, pady=(0, 10), fill=tk.X)
        inner = tk.Frame(card, bg=self.config.COLOR_WHITE)
        inner.pack(padx=22, pady=16, fill=tk.X)
        return inner

    def _field(self, parent, row: int, label: str, variable,
               values=None, width: int = 20) -> None:
        tk.Label(parent, text=label, bg=self.config.COLOR_WHITE, fg="#444444",
                 font=("Arial", 11), width=22, anchor="w").grid(
                     row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        if values:
            ttk.Combobox(parent, textvariable=variable, values=values,
                         state="readonly", font=("Arial", 11),
                         width=width - 2).grid(row=row, column=1, sticky="w",
                                               pady=6)
        else:
            tk.Entry(parent, textvariable=variable, font=("Arial", 11),
                     width=width, relief=tk.SOLID, borderwidth=1).grid(
                         row=row, column=1, sticky="w", pady=6)

    def _title(self, parent, text: str, subtitle: str = "") -> None:
        tk.Label(parent, text=text, bg=self.config.COLOR_WHITE,
                 fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 13, "bold")).pack(pady=(16, 4))
        if subtitle:
            tk.Label(parent, text=subtitle, bg=self.config.COLOR_WHITE,
                     fg="#666666", font=("Arial", 9, "italic"),
                     justify="center").pack(pady=(0, 10))

    def _tab(self, label: str) -> tk.Frame:
        frame = tk.Frame(self.notebook, bg=self.config.COLOR_WHITE)
        self.notebook.add(frame, text=f"  {label}  ")
        return frame

    # ================================================================== #
    # STANOWISKO
    # ================================================================== #
    def _create_station_tab(self) -> None:
        frame = self._tab("Stanowisko")
        self._title(frame, "Tester Hi-Pot i identyfikacja stanowiska",
                    "Model testera musi zgadzać się z odpowiedzią *IDN? — "
                    "niezgodność blokuje połączenie.")
        inner = self._card(frame)

        self._model_var = tk.StringVar(value=self.config.INSTRUMENT_MODEL)
        self._station_var = tk.StringVar(value=self.config.STATION_ID)
        self._port_var = tk.StringVar(value=self.config.DEVICE_COM_PORT)
        self._baud_var = tk.StringVar(value=str(self.config.DEVICE_BAUDRATE))
        self._parity_var = tk.StringVar(value=self.config.DEVICE_PARITY)
        self._flow_var = tk.StringVar(value=self.config.DEVICE_FLOW_CONTROL)

        self._field(inner, 0, "Model testera:", self._model_var, supported_models())
        self._field(inner, 1, "ID stanowiska:", self._station_var)
        self._field(inner, 2, "Port COM:", self._port_var)
        self._field(inner, 3, "Baudrate:", self._baud_var,
                    ["1200", "2400", "4800", "9600", "19200"])
        self._field(inner, 4, "Parity:", self._parity_var, ["NONE", "ODD", "EVEN"])
        self._field(inner, 5, "Flow Control:", self._flow_var,
                    ["NONE", "XON/XOFF"])

        tk.Label(inner,
                 text="Zalecane 19200 bodów — to maksimum tego testera "
                      "(manual, rozdz. 6.2).\nPrzy 9600 krótkie kroki "
                      "(dwell 1 s) mogą nie zdążyć zebrać wymaganych próbek "
                      "obciążenia.\nSprzętowy RTS/CTS nie jest obsługiwany.",
                 bg=self.config.COLOR_WHITE, fg="#E65100",
                 font=("Arial", 8, "italic"), justify="left").grid(
                     row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.station_status = tk.Label(frame, text="",
                                       bg=self.config.COLOR_WHITE,
                                       font=("Arial", 10))
        self.station_status.pack(pady=(0, 4))

        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=6)
        tk.Button(buttons, text="Zapisz ustawienia", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._save_station).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(buttons, text="Test połączenia", bg="#607D8B",
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._test_connection).pack(side=tk.LEFT)

        self.station_test_result = tk.Label(frame, text="",
                                           bg=self.config.COLOR_WHITE,
                                           font=("Arial", 10))
        self.station_test_result.pack(pady=4)

    def _save_station(self) -> None:
        previous = {
            "MODEL": self.config.INSTRUMENT_MODEL,
            "STATION": self.config.STATION_ID,
            "PORT": self.config.DEVICE_COM_PORT,
            "BAUD": self.config.DEVICE_BAUDRATE,
            "PARITY": self.config.DEVICE_PARITY,
            "FLOW": self.config.DEVICE_FLOW_CONTROL,
        }
        try:
            port, baud, parity, flow = validate_rs232_settings(
                self._port_var.get(), self._baud_var.get(),
                self._parity_var.get(), self._flow_var.get())
            model = self._model_var.get().strip().upper()
            if model not in supported_models():
                raise SafetyValidationError(f"Nieobsługiwany model: {model}")
            station = self._station_var.get().strip()
            if not station:
                raise SafetyValidationError("ID stanowiska nie może być puste")

            self.config.INSTRUMENT_MODEL = model
            self.config.STATION_ID = station
            self.config.DEVICE_COM_PORT = port
            self.config.DEVICE_BAUDRATE = baud
            self.config.DEVICE_PARITY = parity
            self.config.DEVICE_FLOW_CONTROL = flow
            self.settings.save_config(self.config)
        except Exception as exc:
            for key, value in (
                ("INSTRUMENT_MODEL", previous["MODEL"]),
                ("STATION_ID", previous["STATION"]),
                ("DEVICE_COM_PORT", previous["PORT"]),
                ("DEVICE_BAUDRATE", previous["BAUD"]),
                ("DEVICE_PARITY", previous["PARITY"]),
                ("DEVICE_FLOW_CONTROL", previous["FLOW"]),
            ):
                setattr(self.config, key, value)
            self.station_status.config(text=f"✗ Nie zapisano: {exc}",
                                       fg=self.config.COLOR_ERROR)
            return

        audit_changes("STANOWISKO", previous, {
            "MODEL": model, "STATION": station, "PORT": port,
            "BAUD": baud, "PARITY": parity, "FLOW": flow,
        })
        self.station_status.config(
            text="✓ Zapisano — zmiany aktywne od następnego połączenia",
            fg=self.config.COLOR_ACCENT)

    def _test_connection(self) -> None:
        try:
            port, baud, parity, flow = validate_rs232_settings(
                self._port_var.get(), self._baud_var.get(),
                self._parity_var.get(), self._flow_var.get())
            model = self._model_var.get().strip().upper()
        except Exception as exc:
            self.station_test_result.config(text=f"✗ Błąd ustawień: {exc}",
                                            fg=self.config.COLOR_ERROR)
            return

        self.station_test_result.config(text="⏳ Łączenie...", fg="#FF9800")

        def worker() -> None:
            try:
                from hipot_device import ChromaDevice
                from scpi_dialect import Dialect

                device = ChromaDevice(port=port, baudrate=baud, parity=parity,
                                      flow_control=flow,
                                      dialect=Dialect(model,
                                                      self.config.SCPI_OVERRIDES))
                if device.connect():
                    text = f"✓ Połączono: {device.identification}"
                    color = self.config.COLOR_ACCENT
                    device.disconnect()
                else:
                    text = (f"✗ Brak odpowiedzi lub inny model niż {model} "
                            f"na {port}")
                    color = self.config.COLOR_ERROR
            except Exception as exc:
                text, color = f"✗ Błąd: {exc}", self.config.COLOR_ERROR
            self._safe_update(self.station_test_result, text, color)

        threading.Thread(target=worker, daemon=True).start()

    def _safe_update(self, widget, text: str, color: str) -> None:
        """Aktualizacja etykiety z watku roboczego, odporna na zamkniete okno."""
        try:
            self.window.after(
                0, lambda: widget.config(text=text, fg=color))
        except (tk.TclError, RuntimeError, AttributeError):
            pass

    # ================================================================== #
    # INTERLOCK
    # ================================================================== #
    def _create_interlock_tab(self) -> None:
        frame = self._tab("Interlock")
        self._title(frame, "Hardware Interlock — Arduino",
                    "Arduino monitoruje stan klapy. Zamknięcie klapy startuje "
                    "test automatycznie.\nWyłączenie interlocka nie jest "
                    "dostępne w wersji produkcyjnej.")
        inner = self._card(frame)

        self._il_port_var = tk.StringVar(value=self.config.INTERLOCK_PORT)
        self._il_baud_var = tk.StringVar(value=str(self.config.INTERLOCK_BAUDRATE))

        self._field(inner, 0, "Port COM Arduino:", self._il_port_var)
        self._field(inner, 1, "Baudrate:", self._il_baud_var,
                    ["9600", "19200", "38400", "57600", "115200"])

        tk.Label(inner, text="Interlock aktywny:", bg=self.config.COLOR_WHITE,
                 fg="#444444", font=("Arial", 11), width=22, anchor="w").grid(
                     row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        tk.Checkbutton(inner, variable=tk.BooleanVar(value=True),
                       bg=self.config.COLOR_WHITE,
                       activebackground=self.config.COLOR_WHITE,
                       state="disabled").grid(row=2, column=1, sticky="w", pady=6)

        self.interlock_status = tk.Label(frame, text="",
                                         bg=self.config.COLOR_WHITE,
                                         font=("Arial", 10))
        self.interlock_status.pack(pady=(0, 4))

        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=6)
        tk.Button(buttons, text="Zapisz ustawienia", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._save_interlock).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(buttons, text="Test Arduino", bg="#607D8B",
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._test_interlock).pack(side=tk.LEFT)

        self.interlock_test_result = tk.Label(frame, text="",
                                              bg=self.config.COLOR_WHITE,
                                              font=("Arial", 10))
        self.interlock_test_result.pack(pady=4)

    def _save_interlock(self) -> None:
        previous = {"PORT": self.config.INTERLOCK_PORT,
                    "BAUD": self.config.INTERLOCK_BAUDRATE}
        try:
            port, baud, enabled = validate_interlock_settings(
                self._il_port_var.get(), self._il_baud_var.get(), True)
            self.config.INTERLOCK_PORT = port
            self.config.INTERLOCK_BAUDRATE = baud
            self.config.INTERLOCK_ENABLED = enabled
            self.settings.save_config(self.config)
        except Exception as exc:
            self.config.INTERLOCK_PORT = previous["PORT"]
            self.config.INTERLOCK_BAUDRATE = previous["BAUD"]
            self.interlock_status.config(text=f"✗ Nie zapisano: {exc}",
                                         fg=self.config.COLOR_ERROR)
            return
        audit_changes("INTERLOCK", previous, {"PORT": port, "BAUD": baud})
        self.interlock_status.config(
            text="✓ Zapisano — zmiany aktywne po restarcie aplikacji",
            fg=self.config.COLOR_ACCENT)

    def _test_interlock(self) -> None:
        try:
            port, baud, _ = validate_interlock_settings(
                self._il_port_var.get(), self._il_baud_var.get(), True)
        except Exception as exc:
            self.interlock_test_result.config(text=f"✗ Błąd ustawień: {exc}",
                                               fg=self.config.COLOR_ERROR)
            return

        self.interlock_test_result.config(
            text=f"⏳ Łączenie z Arduino na {port}...", fg="#FF9800")

        def worker() -> None:
            import time

            try:
                import serial

                with serial.Serial(port, baud, timeout=2) as connection:
                    time.sleep(1.5)
                    connection.reset_input_buffer()
                    deadline = time.time() + 2.5
                    buffer = bytearray()
                    line = ""
                    while time.time() < deadline:
                        chunk = connection.readline()
                        if chunk:
                            buffer.extend(chunk)
                            if b"\n" in buffer:
                                raw, _, rest = bytes(buffer).partition(b"\n")
                                candidate = raw.decode("ascii",
                                                       errors="ignore").strip()
                                buffer = bytearray(rest)
                                if candidate:
                                    line = candidate.upper()
                                    break
                        else:
                            time.sleep(0.05)
                if line in ("OPEN", "CLOSED"):
                    state = "🔒 ZAMKNIĘTA" if line == "CLOSED" else "🔓 OTWARTA"
                    text = f"✓ Arduino odpowiada — klapa: {state}"
                    color = self.config.COLOR_ACCENT
                elif line:
                    text = f"⚠ Nieznany format odpowiedzi: '{line}'"
                    color = "#FF9800"
                else:
                    text = "⚠ Podłączone, brak danych — sprawdź baudrate/szkic"
                    color = "#FF9800"
            except Exception as exc:
                text, color = f"✗ Błąd: {exc}", self.config.COLOR_ERROR
            self._safe_update(self.interlock_test_result, text, color)

        threading.Thread(target=worker, daemon=True).start()

    # ================================================================== #
    # MAPA HWID
    # ================================================================== #
    def _create_hwid_tab(self) -> None:
        frame = self._tab("Mapa HWID")
        self._title(frame, "Mapa HWID → produkt i model",
                    "Pierwsze 6 znaków S/N wyznacza profil testowy oraz nazwę "
                    "modelu w raporcie.\nDotyczy wyłącznie profili "
                    "z identyfikacją „HWID”. Profile identyfikowane wyborem "
                    "operatora (np. SR203_SR204)\nnie korzystają z mapy — "
                    "może zostać pusta.")

        table_frame = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=20)

        columns = ("HWID", "Profil produktu", "Model")
        self.hwid_tree = ttk.Treeview(table_frame, columns=columns,
                                      show="headings", selectmode="browse",
                                      height=12)
        for column, width in zip(columns, (120, 200, 220)):
            self.hwid_tree.heading(column, text=column)
            self.hwid_tree.column(column, width=width, anchor="center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical",
                                  command=self.hwid_tree.yview)
        self.hwid_tree.configure(yscrollcommand=scrollbar.set)
        self.hwid_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._reload_hwid_tree()

        editor = tk.Frame(frame, bg=self.config.COLOR_WHITE, relief=tk.RAISED,
                          borderwidth=2)
        editor.pack(fill=tk.X, padx=20, pady=8)
        inner = tk.Frame(editor, bg=self.config.COLOR_WHITE)
        inner.pack(padx=14, pady=10)

        self._new_hwid_var = tk.StringVar()
        self._new_product_var = tk.StringVar(
            value=self.catalog.ids()[0] if self.catalog.ids() else "")
        self._new_model_var = tk.StringVar()

        for column, (label, variable, values, width) in enumerate((
            ("HWID (6 znaków):", self._new_hwid_var, None, 10),
            ("Profil:", self._new_product_var, self.catalog.ids(), 16),
            ("Model:", self._new_model_var, None, 16),
        )):
            tk.Label(inner, text=label, bg=self.config.COLOR_WHITE, fg="#444444",
                     font=("Arial", 10)).grid(row=0, column=column * 2,
                                              sticky="w", padx=(0, 6), pady=4)
            if values:
                ttk.Combobox(inner, textvariable=variable, values=values,
                             state="readonly", font=("Arial", 10),
                             width=width).grid(row=0, column=column * 2 + 1,
                                               sticky="w", padx=(0, 14), pady=4)
            else:
                tk.Entry(inner, textvariable=variable, font=("Arial", 10),
                         width=width, relief=tk.SOLID, borderwidth=1).grid(
                             row=0, column=column * 2 + 1, sticky="w",
                             padx=(0, 14), pady=4)

        tk.Button(inner, text="Dodaj / zmień", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=13,
                  command=self._add_hwid).grid(row=0, column=6, padx=(0, 6),
                                               pady=4)
        tk.Button(inner, text="Usuń zaznaczony", bg=self.config.COLOR_ERROR,
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=15,
                  command=self._remove_hwid).grid(row=0, column=7, pady=4)

        self.hwid_status = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                                    font=("Arial", 9))
        self.hwid_status.pack(pady=(0, 6))
        if self._pending_hwid_error:
            self.hwid_status.config(
                text=f"✗ Nie udało się wczytać mapy HWID: "
                     f"{self._pending_hwid_error}",
                fg=self.config.COLOR_ERROR)
            self._pending_hwid_error = None

    def _reload_hwid_tree(self) -> None:
        """Bledy odczytu mapy MUSZA byc widoczne, nie polkniete."""
        try:
            from hwid_map import HwidMap

            for row in self.hwid_tree.get_children():
                self.hwid_tree.delete(row)
            for hwid, entry in sorted(HwidMap(self.catalog).get_all().items()):
                self.hwid_tree.insert("", tk.END, values=(
                    hwid, entry["product"], entry["model"]))
        except Exception as exc:
            if getattr(self, "hwid_status", None) is not None:
                self.hwid_status.config(
                    text=f"✗ Nie udało się wczytać mapy HWID: {exc}",
                    fg=self.config.COLOR_ERROR)
            else:
                self._pending_hwid_error = str(exc)

    def _add_hwid(self) -> None:
        from hwid_map import HwidMap

        try:
            ok, message = HwidMap(self.catalog).add(
                self._new_hwid_var.get(), self._new_product_var.get(),
                self._new_model_var.get())
        except Exception as exc:
            self.hwid_status.config(text=f"✗ Mapa HWID niedostępna: {exc}",
                                    fg=self.config.COLOR_ERROR)
            return
        if not ok:
            self.hwid_status.config(text=f"✗ {message}",
                                    fg=self.config.COLOR_ERROR)
            return
        hwid = self._new_hwid_var.get().strip().upper()
        self._new_hwid_var.set("")
        self._new_model_var.set("")
        self._reload_hwid_tree()
        self.hwid_status.config(text=f"✓ Zapisano mapowanie {hwid}",
                                fg=self.config.COLOR_ACCENT)

    def _remove_hwid(self) -> None:
        from hwid_map import HwidMap

        selection = self.hwid_tree.selection()
        if not selection:
            self.hwid_status.config(text="Zaznacz wiersz do usunięcia.",
                                    fg=self.config.COLOR_ERROR)
            return
        hwid = self.hwid_tree.item(selection[0], "values")[0]
        if not messagebox.askyesno("Potwierdź", f"Usunąć mapowanie '{hwid}'?",
                                   parent=self.window):
            return
        try:
            removed = HwidMap(self.catalog).remove(hwid)
        except Exception as exc:
            self.hwid_status.config(text=f"✗ Nie udało się zapisać mapy: {exc}",
                                    fg=self.config.COLOR_ERROR)
            return
        self._reload_hwid_tree()
        if removed:
            self.hwid_status.config(text=f"✓ Usunięto: {hwid}",
                                    fg=self.config.COLOR_ACCENT)
        else:
            self.hwid_status.config(text=f"✗ Nie znaleziono: {hwid}",
                                    fg=self.config.COLOR_ERROR)

    # ================================================================== #
    # PROFILE PRODUKTOW
    # ================================================================== #
    def _create_profile_tab(self) -> None:
        frame = self._tab("Profile")
        self._title(frame, "Profile testowe",
                    "Odznacz profile, których to stanowisko nie testuje — "
                    "zeskanowanie takiego wyrobu zostanie odrzucone.\nEdycja "
                    "kroków zapisuje się do products/<profil>.json i obowiązuje "
                    "od następnego testu.")

        self._create_enabled_products_box(frame)

        selector = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        selector.pack(fill=tk.X, padx=20, pady=(0, 6))
        tk.Label(selector, text="Profil:", bg=self.config.COLOR_WHITE,
                 fg="#444444", font=("Arial", 11, "bold")).pack(side=tk.LEFT,
                                                                padx=(0, 8))
        self._profile_var = tk.StringVar(
            value=self.catalog.ids()[0] if self.catalog.ids() else "")
        combo = ttk.Combobox(selector, textvariable=self._profile_var,
                             values=self.catalog.ids(), state="readonly",
                             font=("Arial", 11), width=24)
        combo.pack(side=tk.LEFT)
        combo.bind("<<ComboboxSelected>>", lambda event: self._load_profile())

        self.profile_meta = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                                     fg="#666666", font=("Arial", 9),
                                     justify="left")
        self.profile_meta.pack(anchor="w", padx=22, pady=(4, 4))

        table_frame = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=20)
        # Nazewnictwo kolumn jest 1:1 z oprogramowaniem Chromy (zakladka
        # Parameters), zeby technolog porownywal profil z ekranem testera
        # bez tlumaczenia nazw. "Obecność" to jedyne pole dodatkowe -
        # nie ma go w testerze, jest progiem wykrycia pustego fixture.
        columns = ("#", "Ext. Name", "Voltage [kV]", "High Limit [mA]",
                   "Low Limit [mA]", "ARC Limit [mA]", "Test Time [s]",
                   "Ramp Time [s]", "Fall Time [s]", "Real Current [mA]",
                   "Channel", "Obecność [mA]")
        widths = (28, 100, 84, 96, 92, 92, 82, 88, 82, 100, 92, 88)
        self.steps_tree = ttk.Treeview(table_frame, columns=columns,
                                       show="headings", selectmode="browse",
                                       height=9)
        for column, width in zip(columns, widths):
            self.steps_tree.heading(column, text=column)
            self.steps_tree.column(column, width=width, anchor="center")
        self.steps_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.steps_tree.bind("<Double-1>", lambda event: self._edit_step())

        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=8)
        for text, color, command, width in (
            ("▲ W górę", "#455A64", lambda: self._move_step(-1), 12),
            ("▼ W dół", "#455A64", lambda: self._move_step(1), 12),
            ("Edytuj krok", "#607D8B", self._edit_step, 16),
            ("Zapisz profil", self.config.COLOR_ACCENT, self._save_profile, 16),
        ):
            tk.Button(buttons, text=text, bg=color, fg=self.config.COLOR_WHITE,
                      font=("Arial", 11, "bold"), relief=tk.FLAT,
                      cursor="hand2", width=width, height=2,
                      command=command).pack(side=tk.LEFT, padx=6)

        self.profile_status = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                                       font=("Arial", 10), wraplength=900,
                                       justify="left")
        self.profile_status.pack(pady=(0, 4))

        tk.Label(frame,
                 text="Arc Sense i Real Current są programowane i weryfikowane "
                      "odczytem zwrotnym (manual s. 5-20).\nCzęstotliwość AC "
                      "jest ustawieniem globalnym testera — wszystkie kroki "
                      "profilu muszą mieć tę samą wartość.\nKolejność kroków "
                      "zmieniasz przyciskami ▲ / ▼ — zmiana wchodzi w życie "
                      "dopiero po „Zapisz profil”.",
                 bg=self.config.COLOR_WHITE, fg="#E65100",
                 font=("Arial", 8, "italic"), justify="left").pack(
                     anchor="w", padx=22, pady=(0, 8))

        if self.catalog.ids():
            self._load_profile()

    def _create_enabled_products_box(self, parent) -> None:
        """Wlaczanie/wylaczanie profili na TYM stanowisku.

        Lista trafia do ``station_config.json`` (ENABLED_PRODUCTS), a nie do
        plikow profili - profile maja byc identyczne na wszystkich stanowiskach,
        rozny jest tylko zestaw wyrobow testowanych w danym miejscu.
        """
        box = tk.Frame(parent, bg=self.config.COLOR_WHITE, relief=tk.RAISED,
                       borderwidth=2)
        box.pack(fill=tk.X, padx=20, pady=(0, 8))
        inner = tk.Frame(box, bg=self.config.COLOR_WHITE)
        inner.pack(padx=16, pady=10, fill=tk.X)

        tk.Label(inner, text="Aktywne na tym stanowisku:",
                 bg=self.config.COLOR_WHITE, fg="#444444",
                 font=("Arial", 11, "bold")).grid(
                     row=0, column=0, sticky="w", padx=(0, 16), pady=(0, 6))

        self._enabled_vars: dict[str, tk.BooleanVar] = {}
        for position, profile in enumerate(self.catalog.all()):
            variable = tk.BooleanVar(
                value=self.config.is_product_enabled(profile.product_id))
            self._enabled_vars[profile.product_id] = variable
            suffix = ("" if profile.matches_instrument(self.config.INSTRUMENT_MODEL)
                      else f"  (wymaga Chromy {'/'.join(profile.allowed_models)})")
            tk.Checkbutton(
                inner, variable=variable,
                text=f"{profile.display_name}{suffix}",
                bg=self.config.COLOR_WHITE,
                activebackground=self.config.COLOR_WHITE,
                fg=("#333333" if not suffix else "#999999"),
                font=("Arial", 10)).grid(
                    row=1 + position, column=0, columnspan=2, sticky="w", pady=1)

        self.enabled_status = tk.Label(inner, text="",
                                       bg=self.config.COLOR_WHITE,
                                       font=("Arial", 9))
        self.enabled_status.grid(row=1 + len(self._enabled_vars), column=0,
                                 columnspan=2, sticky="w", pady=(6, 0))

        tk.Button(inner, text="Zapisz zestaw profili",
                  bg=self.config.COLOR_ACCENT, fg=self.config.COLOR_WHITE,
                  font=("Arial", 10, "bold"), relief=tk.FLAT, cursor="hand2",
                  width=22, command=self._save_enabled_products).grid(
                      row=0, column=1, sticky="e", pady=(0, 6))

    def _save_enabled_products(self) -> None:
        selected = sorted(
            product_id for product_id, variable in self._enabled_vars.items()
            if variable.get()
        )
        if not selected:
            self.enabled_status.config(
                text="✗ Co najmniej jeden profil musi zostać włączony — "
                     "inaczej stanowisko nie przetestuje niczego",
                fg=self.config.COLOR_ERROR)
            return

        previous = self.config.ENABLED_PRODUCTS
        # Wszystkie zaznaczone = brak ograniczenia; zapisujemy None, zeby
        # dodanie nowego profilu nie wymagalo edycji kazdego stanowiska.
        new_value = None if len(selected) == len(self._enabled_vars) else selected
        self.config.ENABLED_PRODUCTS = new_value
        try:
            self.settings.save_config(self.config)
        except Exception as exc:
            self.config.ENABLED_PRODUCTS = previous
            self.enabled_status.config(text=f"✗ Nie zapisano: {exc}",
                                       fg=self.config.COLOR_ERROR)
            return

        audit_changes("PROFILE_AKTYWNE", {"LISTA": previous or "wszystkie"},
                      {"LISTA": new_value or "wszystkie"})
        self.enabled_status.config(
            text="✓ Zapisano — aktywne: "
                 + (", ".join(selected) if new_value else "wszystkie profile"),
            fg=self.config.COLOR_ACCENT)

    def _load_profile(self) -> None:
        try:
            profile = self.catalog.get(self._profile_var.get())
        except SafetyValidationError as exc:
            self.profile_status.config(text=f"✗ {exc}",
                                       fg=self.config.COLOR_ERROR)
            return
        self._edited_profile = profile
        self._edited_steps = [dict(step) for step in profile.steps]
        identification = ("mapa HWID" if profile.requires_hwid
                          else "wybór operatora z listy")
        lengths = " lub ".join(str(x) for x in profile.serial_lengths)
        self.profile_meta.config(
            text=f"{profile.display_name} | Chroma "
                 f"{'/'.join(profile.allowed_models)}"
                 + f" | identyfikacja: {identification}"
                 + f" | S/N {lengths} znaków (A-Z, 0-9)"
                 + (f" + scan box {profile.channel_count} kan."
                    if profile.channel_count else "")
                 + f" | czas cyklu {profile.total_duration:.1f} s"
                 + f" | timeout {profile.test_timeout_s} s"
                 + (f"\nUWAGA: {'; '.join(profile.channel_warnings)}"
                    if profile.channel_warnings else ""))
        self._refresh_steps_tree()
        self.profile_status.config(text="")

    def _refresh_steps_tree(self, select_index: int | None = None) -> None:
        for row in self.steps_tree.get_children():
            self.steps_tree.delete(row)
        for index, step in enumerate(self._edited_steps, start=1):
            self.steps_tree.insert("", tk.END, values=(
                index, step["name"], f"{step['voltage'] / 1000:.2f}",
                f"{step['limit_high']:.3f}", f"{step['limit_low']:.3f}",
                f"{step.get('arc_sense', 0.0):.3f}",
                f"{step['dwell']:.1f}",
                f"{step['ramp_time']:.1f}", f"{step['ramp_dn']:.1f}",
                f"{step.get('real_limit', 0.0):.3f}",
                step.get("channels", "—"),
                f"{step['presence_min_current']:.3f}"))
        if select_index is None:
            return
        rows = self.steps_tree.get_children()
        if 0 <= select_index < len(rows):
            self.steps_tree.selection_set(rows[select_index])
            self.steps_tree.focus(rows[select_index])
            self.steps_tree.see(rows[select_index])

    def _move_step(self, delta: int) -> None:
        """Przesuwa zaznaczony krok o ``delta`` pozycji w sekwencji testu.

        Zmiana dotyczy WYLACZNIE kopii roboczej ``_edited_steps``. Do pliku
        profilu trafia dopiero po "Zapisz profil", ktory przepuszcza cala
        sekwencje przez walidacje i pyta o potwierdzenie - kolejnosc krokow
        decyduje o tym, ktore napiecie trafi na ktory kanal scan boxa.
        """
        if self._edited_profile is None:
            return
        selection = self.steps_tree.selection()
        if not selection:
            self.profile_status.config(
                text="Zaznacz krok, który chcesz przesunąć.",
                fg=self.config.COLOR_ERROR)
            return

        position = self.steps_tree.index(selection[0])
        target = position + delta
        if not 0 <= target < len(self._edited_steps):
            self.profile_status.config(
                text="Krok jest już na skraju listy — nie ma go gdzie przesunąć.",
                fg=self.config.COLOR_WARNING)
            return

        steps = self._edited_steps
        steps[position], steps[target] = steps[target], steps[position]
        self._refresh_steps_tree(select_index=target)
        self.profile_status.config(
            text=f"Nowa kolejność (niezapisana): "
                 + " → ".join(step["name"] for step in steps)
                 + "\nKliknij „Zapisz profil”, żeby zapisać zmianę.",
            fg=self.config.COLOR_WARNING)

    def _edit_step(self) -> None:
        selection = self.steps_tree.selection()
        if not selection:
            self.profile_status.config(text="Zaznacz krok do edycji.",
                                       fg=self.config.COLOR_ERROR)
            return
        position = self.steps_tree.index(selection[0])
        step = dict(self._edited_steps[position])
        channel_count = (self._edited_profile.channel_count
                         if self._edited_profile else 0)

        dialog = tk.Toplevel(self.window)
        dialog.title(f"Krok {position + 1} — {step['name']}")
        dialog.configure(bg=self.config.COLOR_WHITE)
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.resizable(False, False)

        variables: dict[str, tk.StringVar] = {}
        fields = [
            ("name", "Ext. Name:", str(step["name"]), ""),
            ("voltage", "Voltage:", f"{step['voltage'] / 1000:.3f}", "kV"),
            ("limit_high", "High Limit:", f"{step['limit_high']:.3f}", "mA"),
            ("limit_low", "Low Limit:", f"{step['limit_low']:.3f}", "mA"),
            ("arc_sense", "ARC Limit:", f"{step.get('arc_sense', 0.0):.3f}",
             "mA (0 = wyłączony)"),
            ("dwell", "Test Time:", f"{step['dwell']:.2f}", "s"),
            ("ramp_time", "Ramp Time:", f"{step['ramp_time']:.2f}", "s"),
            ("ramp_dn", "Fall Time:", f"{step['ramp_dn']:.2f}", "s"),
            ("real_limit", "Real Current:", f"{step.get('real_limit', 0.0):.3f}",
             "mA (0 = wyłączony)"),
        ]
        if channel_count:
            fields.append(("channels", "Channel:", step.get("channels", ""),
                           f"{channel_count} znaków O/H/L"))
        # Pole spoza oprogramowania testera - na koncu, wizualnie oddzielone.
        fields.append(
            ("presence_min_current", "Próg obecności:",
             f"{step['presence_min_current']:.3f}",
             f"mA (min {ABSOLUTE_MIN_PRESENCE_MA:.3f}) — pole aplikacji"))

        grid = tk.Frame(dialog, bg=self.config.COLOR_WHITE)
        grid.pack(padx=22, pady=16)
        for row, (key, label, value, unit) in enumerate(fields):
            tk.Label(grid, text=label, bg=self.config.COLOR_WHITE, fg="#555555",
                     font=("Arial", 10), width=17, anchor="e").grid(
                         row=row, column=0, sticky="e", padx=(0, 8), pady=4)
            variable = tk.StringVar(value=value)
            variables[key] = variable
            tk.Entry(grid, textvariable=variable, font=("Arial", 10, "bold"),
                     width=18, justify="center", relief=tk.SOLID,
                     borderwidth=1).grid(row=row, column=1, sticky="w", pady=4)
            tk.Label(grid, text=unit, bg=self.config.COLOR_WHITE, fg="#888888",
                     font=("Arial", 9)).grid(row=row, column=2, sticky="w",
                                             padx=(6, 0), pady=4)

        error_label = tk.Label(dialog, text="", bg=self.config.COLOR_WHITE,
                               fg=self.config.COLOR_ERROR, font=("Arial", 9),
                               wraplength=380, justify="left")
        error_label.pack(padx=20, pady=(0, 6))

        def apply_changes() -> None:
            candidate = dict(step)
            try:
                candidate["name"] = variables["name"].get().strip()
                candidate["voltage"] = float(variables["voltage"].get()) * 1000.0
                for key in ("limit_high", "limit_low", "presence_min_current",
                            "arc_sense", "real_limit",
                            "ramp_time", "dwell", "ramp_dn"):
                    candidate[key] = float(variables[key].get())
                if channel_count:
                    candidate["channels"] = validate_channel_mask(
                        variables["channels"].get(), channel_count,
                        f"Krok {position + 1}")
                normalized = validate_step(candidate, position + 1, channel_count)
            except (ValueError, SafetyValidationError) as exc:
                error_label.config(text=f"✗ {exc}")
                return
            self._edited_steps[position] = normalized
            self._refresh_steps_tree()
            dialog.destroy()
            self.profile_status.config(
                text="Krok zmieniony w edytorze — kliknij „Zapisz profil”, "
                     "aby zapisać na dysk.",
                fg=self.config.COLOR_WARNING)

        row = tk.Frame(dialog, bg=self.config.COLOR_WHITE)
        row.pack(pady=(0, 16))
        tk.Button(row, text="Zastosuj", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  width=12, relief=tk.FLAT, cursor="hand2",
                  command=apply_changes).pack(side=tk.LEFT, padx=5)
        tk.Button(row, text="Anuluj", bg="#999999", fg=self.config.COLOR_WHITE,
                  font=("Arial", 10, "bold"), width=12, relief=tk.FLAT,
                  cursor="hand2", command=dialog.destroy).pack(side=tk.LEFT,
                                                               padx=5)

    def _save_profile(self) -> None:
        if self._edited_profile is None:
            return
        previous = self._edited_profile
        payload = previous.to_dict()
        payload["steps"] = [dict(step) for step in self._edited_steps]

        try:
            candidate = ProductProfile(payload, source=f"{previous.product_id}.json")
        except SafetyValidationError as exc:
            self.profile_status.config(text=f"✗ Nie zapisano: {exc}",
                                       fg=self.config.COLOR_ERROR)
            return

        lines = "\n".join(
            f"{index}. {step['name']}: {step['voltage'] / 1000:.2f} kV, "
            f"{candidate.effective_low_ma(step):.3f}–{step['limit_high']:.3f} mA"
            + (f", kanały {step['channels']}" if step.get("channels") else "")
            for index, step in enumerate(candidate.steps, start=1)
        )
        warning = ("\n\nUWAGA: " + "; ".join(candidate.channel_warnings)
                   if candidate.channel_warnings else "")

        previous_order = [step["name"] for step in previous.steps]
        new_order = [step["name"] for step in candidate.steps]
        if previous_order != new_order:
            warning += ("\n\nZMIANA KOLEJNOŚCI KROKÓW:"
                        f"\n  było:  {' → '.join(previous_order)}"
                        f"\n  będzie: {' → '.join(new_order)}")

        if not messagebox.askyesno(
            "Potwierdzenie profilu produkcyjnego",
            f"Profil {candidate.display_name} zostanie użyty dla WSZYSTKICH "
            f"HWID przypisanych do '{candidate.product_id}'.\n\n{lines}{warning}"
            "\n\nCzy wartości są zgodne z zatwierdzoną instrukcją testową?",
            parent=self.window,
        ):
            self.profile_status.config(text="Anulowano zapis profilu.",
                                       fg=self.config.COLOR_WARNING)
            return

        try:
            path = self.catalog.save(candidate)
        except Exception as exc:
            self.profile_status.config(text=f"✗ Nie zapisano: {exc}",
                                       fg=self.config.COLOR_ERROR)
            return

        if previous_order != new_order:
            audit_changes(f"PROFIL/{candidate.product_id}/KOLEJNOSC",
                          {"KROKI": " -> ".join(previous_order)},
                          {"KROKI": " -> ".join(new_order)})
        for index, (before, after) in enumerate(
            zip(previous.steps, candidate.steps), start=1
        ):
            audit_changes(f"PROFIL/{candidate.product_id}/krok{index}",
                          before, after)
        self._load_profile()
        self.profile_status.config(
            text=f"✓ Zapisano {path.name} | czas cyklu "
                 f"{candidate.total_duration:.1f} s",
            fg=self.config.COLOR_ACCENT)

    # ================================================================== #
    # LOGI
    # ================================================================== #
    def _create_logs_tab(self) -> None:
        frame = self._tab("Logi")
        self._title(frame, "Lokalizacja zapisu raportów",
                    "Obsługiwane są ścieżki lokalne i sieciowe UNC "
                    "(\\\\serwer\\folder).")
        inner = self._card(frame)

        self._log_dir_var = tk.StringVar(value=self.config.LOG_DIR)
        tk.Label(inner, text="Ścieżka zapisu raportów:",
                 bg=self.config.COLOR_WHITE, fg="#444444",
                 font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 6))
        row = tk.Frame(inner, bg=self.config.COLOR_WHITE)
        row.pack(fill=tk.X)
        tk.Entry(row, textvariable=self._log_dir_var, font=("Courier", 10),
                 relief=tk.SOLID, borderwidth=1).pack(
                     side=tk.LEFT, expand=True, fill=tk.X, ipady=5, padx=(0, 8))
        tk.Button(row, text="Przeglądaj...", bg=self.config.COLOR_PRIMARY,
                  fg=self.config.COLOR_WHITE, font=("Arial", 9, "bold"),
                  relief=tk.FLAT, cursor="hand2", padx=10, pady=4,
                  command=self._browse_log_dir).pack(side=tk.LEFT)

        self.log_active_label = tk.Label(
            inner, text=f"Aktualnie aktywna: {self.config.LOG_DIR}",
            bg=self.config.COLOR_WHITE, fg="#aaaaaa",
            font=("Arial", 8, "italic"))
        self.log_active_label.pack(anchor="w", pady=(6, 0))

        self.log_status = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                                   font=("Arial", 10))
        self.log_status.pack(pady=(0, 5))

        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=6)
        tk.Button(buttons, text="Sprawdź dostępność", bg="#FF9800",
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._check_log_dir).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(buttons, text="Zapisz ścieżkę", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._save_log_dir).pack(side=tk.LEFT)

    def _browse_log_dir(self) -> None:
        current = self._log_dir_var.get().strip()
        chosen = filedialog.askdirectory(
            title="Wybierz folder zapisu raportów",
            initialdir=current if os.path.isdir(current) else "C:\\",
            parent=self.window)
        if chosen:
            self._log_dir_var.set(chosen.replace("/", "\\"))
            self.log_status.config(text="")

    def _check_log_dir(self) -> None:
        path = self._log_dir_var.get().strip()
        if not path:
            self.log_status.config(text="✗ Ścieżka jest pusta!",
                                   fg=self.config.COLOR_ERROR)
            return
        if not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
                self.log_status.config(text=f"✓ Folder utworzony: {path}",
                                       fg=self.config.COLOR_ACCENT)
            except Exception as exc:
                self.log_status.config(
                    text=f"✗ Nie można utworzyć folderu: {exc}",
                    fg=self.config.COLOR_ERROR)
            return

        probe = os.path.join(path, "_hipot_write_test.tmp")
        try:
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            self.log_status.config(
                text=f"✓ Ścieżka dostępna i zapisywalna: {path}",
                fg=self.config.COLOR_ACCENT)
        except Exception as exc:
            self.log_status.config(text=f"✗ Brak uprawnień do zapisu: {exc}",
                                   fg=self.config.COLOR_ERROR)

    def _save_log_dir(self) -> None:
        path = self._log_dir_var.get().strip()
        if not path:
            self.log_status.config(text="✗ Ścieżka nie może być pusta!",
                                   fg=self.config.COLOR_ERROR)
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:
            self.log_status.config(text=f"✗ Nie można utworzyć folderu: {exc}",
                                   fg=self.config.COLOR_ERROR)
            return

        previous = self.config.LOG_DIR
        self.config.LOG_DIR = path
        try:
            self.settings.save_config(self.config)
        except Exception as exc:
            self.config.LOG_DIR = previous
            self.log_status.config(text=f"✗ Nie zapisano konfiguracji: {exc}",
                                   fg=self.config.COLOR_ERROR)
            return
        audit_changes("LOG_DIR", {"PATH": previous}, {"PATH": path})
        self.log_active_label.config(text=f"Aktualnie aktywna: {path}")
        self.log_status.config(text=f"✓ Zapisano ścieżkę raportów: {path}",
                               fg=self.config.COLOR_ACCENT)

    # ================================================================== #
    # DIAGNOSTYKA SCPI
    # ================================================================== #
    def _create_diagnostics_tab(self) -> None:
        frame = self._tab("Diagnostyka SCPI")
        self._title(frame, "Sonda komend SCPI",
                    "Sprawdza, które nagłówki firmware faktycznie przyjmuje. "
                    "Wysyłane są tylko zapytania —\nsonda nie uruchamia testu "
                    "ani nie podaje wysokiego napięcia.")

        tk.Label(frame,
                 text="Składnia zgodna z manualem 19051/19052/19053/19054 "
                      "wer. 2.1 (rozdz. 5).\nJeśli firmware jest starszy i "
                      "odrzuci którąś komendę, popraw ją w SCPI_OVERRIDES "
                      "w station_config.json.",
                 bg=self.config.COLOR_WHITE, fg="#E65100",
                 font=("Arial", 9, "italic"), justify="center").pack(pady=(0, 8))

        table_frame = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=20)
        columns = ("Komenda", "Nagłówek", "Status", "Odpowiedź", "SYST:ERR?")
        self.diag_tree = ttk.Treeview(table_frame, columns=columns,
                                      show="headings", height=14)
        for column, width in zip(columns, (170, 260, 110, 140, 170)):
            self.diag_tree.heading(column, text=column)
            self.diag_tree.column(column, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical",
                                  command=self.diag_tree.yview)
        self.diag_tree.configure(yscrollcommand=scrollbar.set)
        self.diag_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.diag_status = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                                    font=("Arial", 10), wraplength=900,
                                    justify="left")
        self.diag_status.pack(pady=(6, 4))

        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=6)
        tk.Button(buttons, text="Pokaż dialekt", bg="#607D8B",
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=18, height=2,
                  command=self._show_dialect).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(buttons, text="Uruchom sondę", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=18, height=2,
                  command=self._run_probe).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(buttons, text="Porównaj z testerem", bg="#00695C",
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=20, height=2,
                  command=self._compare_with_instrument).pack(side=tk.LEFT)

        self._show_dialect()

    def _show_dialect(self) -> None:
        for row in self.diag_tree.get_children():
            self.diag_tree.delete(row)
        try:
            dialect = self.config.dialect()
        except Exception as exc:
            self.diag_status.config(text=f"✗ {exc}", fg=self.config.COLOR_ERROR)
            return
        for name, template, state in dialect.as_report():
            self.diag_tree.insert("", tk.END,
                                  values=(name, template, state, "", ""))
        unverified = dialect.unverified_commands()
        self.diag_status.config(
            text=(f"Model {dialect.model} — {dialect.description}. "
                  f"Niepotwierdzonych komend: {len(unverified)}"
                  + (f" ({', '.join(unverified)})" if unverified else "")),
            fg="#E65100" if unverified else self.config.COLOR_ACCENT)

    def _run_probe(self) -> None:
        self.diag_status.config(text="⏳ Łączenie i sondowanie...", fg="#FF9800")

        def worker() -> None:
            try:
                from hipot_device import ChromaDevice

                device = ChromaDevice(
                    port=self.config.DEVICE_COM_PORT,
                    baudrate=self.config.DEVICE_BAUDRATE,
                    parity=self.config.DEVICE_PARITY,
                    flow_control=self.config.DEVICE_FLOW_CONTROL,
                    dialect=self.config.dialect())
                if not device.connect():
                    self._safe_update(
                        self.diag_status,
                        f"✗ Brak połączenia z Chroma "
                        f"{self.config.INSTRUMENT_MODEL} na "
                        f"{self.config.DEVICE_COM_PORT}",
                        self.config.COLOR_ERROR)
                    return
                try:
                    results = device.probe_dialect()
                finally:
                    device.disconnect()
            except Exception as exc:
                self._safe_update(self.diag_status, f"✗ Błąd sondy: {exc}",
                                  self.config.COLOR_ERROR)
                return

            try:
                self.window.after(0, lambda: self._apply_probe(results))
            except (tk.TclError, RuntimeError, AttributeError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # POROWNANIE PROFILU Z PROGRAMEM ZALADOWANYM W TESTERZE
    # ------------------------------------------------------------------ #
    def _compare_with_instrument(self) -> None:
        """Czyta program z Chromy i zestawia go z profilem aplikacji.

        Same zapytania - nie programuje testera i nie podaje napiecia.
        Sluzy do potwierdzenia, ze profil w pliku odpowiada temu, na czym
        stanowisko faktycznie testuje. Bez tego jedynym zrodlem jest
        przepisywanie wartosci z ekranu testera, a to najlatwiejsze miejsce
        na pomylke.
        """
        profile = self._edited_profile
        if profile is None:
            enabled = [p for p in self.catalog.all()
                       if self.config.is_product_enabled(p.product_id)]
            profile = enabled[0] if len(enabled) == 1 else None
        if profile is None:
            self.diag_status.config(
                text="✗ Najpierw wybierz profil w zakładce Profile — "
                     "nie wiem, z czym porównywać.",
                fg=self.config.COLOR_ERROR)
            return

        self.diag_status.config(
            text=f"⏳ Odczyt programu z testera i porównanie z "
                 f"{profile.display_name}...", fg="#FF9800")

        def worker() -> None:
            try:
                from hipot_device import ChromaDevice

                device = ChromaDevice(
                    port=self.config.DEVICE_COM_PORT,
                    baudrate=self.config.DEVICE_BAUDRATE,
                    parity=self.config.DEVICE_PARITY,
                    flow_control=self.config.DEVICE_FLOW_CONTROL,
                    dialect=self.config.dialect())
                if not device.connect():
                    self._safe_update(
                        self.diag_status,
                        f"✗ Brak połączenia z Chroma "
                        f"{self.config.INSTRUMENT_MODEL} na "
                        f"{self.config.DEVICE_COM_PORT}",
                        self.config.COLOR_ERROR)
                    return
                try:
                    program = device.read_program()
                finally:
                    device.disconnect()
            except Exception as exc:
                self._safe_update(self.diag_status,
                                  f"✗ Błąd odczytu programu: {exc}",
                                  self.config.COLOR_ERROR)
                return
            try:
                self.window.after(
                    0, lambda: self._apply_comparison(profile, program))
            except (tk.TclError, RuntimeError, AttributeError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _channels_text(numbers) -> str:
        return ",".join(str(n) for n in sorted(numbers)) if numbers else "—"

    def _apply_comparison(self, profile, program) -> None:
        from scpi_dialect import mask_to_channel_lists

        for row in self.diag_tree.get_children():
            self.diag_tree.delete(row)

        differences: list[str] = []

        def line(label, mine, theirs, same):
            nonlocal differences
            status = "ZGODNE" if same else "ROZNI SIE"
            if not same:
                differences.append(label)
            self.diag_tree.insert("", tk.END,
                                  values=(label, mine, status, theirs, ""))

        line("Liczba kroków", str(profile.step_count),
             str(program["step_count"]),
             profile.step_count == program["step_count"])

        frequency = program.get("frequency")
        if frequency is not None and profile.steps:
            mine = float(profile.steps[0].get("frequency", 0) or 0)
            line("Frequency [Hz]", f"{mine:.0f}", f"{frequency:.0f}",
                 abs(mine - frequency) < 0.5)

        pairs = zip(profile.steps, program["steps"])
        for index, (mine, theirs) in enumerate(pairs, start=1):
            tag = f"Krok {index}"
            checks = [
                ("Voltage [kV]", float(mine["voltage"]) / 1000.0,
                 theirs["voltage"] / 1000.0, 0.005, "{:.3f}"),
                ("High Limit [mA]", float(mine["limit_high"]),
                 theirs["limit_high"] * 1000.0, 0.002, "{:.3f}"),
                ("Low Limit [mA]", float(mine["limit_low"]),
                 theirs["limit_low"] * 1000.0, 0.002, "{:.3f}"),
                ("ARC Limit [mA]", float(mine.get("arc_sense", 0.0)),
                 theirs["arc"] * 1000.0, 0.002, "{:.3f}"),
                ("Test Time [s]", float(mine["dwell"]), theirs["dwell"],
                 0.05, "{:.2f}"),
                ("Ramp Time [s]", float(mine["ramp_time"]),
                 theirs["ramp_time"], 0.05, "{:.2f}"),
                ("Fall Time [s]", float(mine["ramp_dn"]), theirs["ramp_dn"],
                 0.05, "{:.2f}"),
                ("Real Current [mA]", float(mine.get("real_limit", 0.0)),
                 theirs["real"] * 1000.0, 0.002, "{:.3f}"),
            ]
            for label, a, b, tolerance, fmt in checks:
                line(f"{tag} · {label}", fmt.format(a), fmt.format(b),
                     abs(a - b) <= tolerance)

            if theirs.get("mode"):
                line(f"{tag} · Mode", str(mine.get("mode", "ACW")),
                     theirs["mode"], theirs["mode"].upper().startswith("AC"))

            mask = mine.get("channels")
            if mask and "channels_high" in theirs:
                high, low = mask_to_channel_lists(mask)
                line(f"{tag} · Channel HIGH", self._channels_text(high),
                     self._channels_text(theirs["channels_high"]),
                     set(high) == set(theirs["channels_high"]))
                line(f"{tag} · Channel LOW", self._channels_text(low),
                     self._channels_text(theirs.get("channels_low", set())),
                     set(low) == set(theirs.get("channels_low", set())))

        extra = program["step_count"] - profile.step_count
        if extra > 0:
            differences.append(f"{extra} krok(ow) wiecej w testerze")

        audit("DIAGNOSTYKA/POROWNANIE",
              f"profil={profile.product_id} roznice={len(differences)}")

        if differences:
            self.diag_status.config(
                text=f"⚠ {len(differences)} różnic(y) między profilem "
                     f"{profile.display_name} a programem w testerze: "
                     + ", ".join(differences[:6])
                     + (" ..." if len(differences) > 6 else "")
                     + "\nKtóre źródło jest obowiązujące — rozstrzyga "
                       "technolog. Aplikacja i tak przeprogramuje tester "
                       "wg profilu przed testem.",
                fg="#E65100")
        else:
            self.diag_status.config(
                text=f"✓ Profil {profile.display_name} zgadza się z programem "
                     f"załadowanym w testerze co do wszystkich pól "
                     f"({program.get('identity') or 'brak IDN'}).",
                fg=self.config.COLOR_ACCENT)

    def _apply_probe(self, results) -> None:
        for row in self.diag_tree.get_children():
            self.diag_tree.delete(row)
        rejected_required = []
        for entry in results:
            status = "OK" if entry["accepted"] else "ODRZUCONA"
            if not entry["accepted"] and entry["required"]:
                rejected_required.append(entry["name"])
            self.diag_tree.insert("", tk.END, values=(
                entry["name"], entry["command"], status,
                str(entry["response"] or "—"), str(entry["error"] or "—")))
        audit("DIAGNOSTYKA/SONDA",
              f"odrzucone={[e['name'] for e in results if not e['accepted']]}")
        if rejected_required:
            self.diag_status.config(
                text="⛔ Firmware odrzucił komendy WYMAGANE do testu: "
                     + ", ".join(rejected_required)
                     + ". Testowanie będzie zablokowane do czasu poprawienia "
                       "składni w SCPI_OVERRIDES.",
                fg=self.config.COLOR_ERROR)
        else:
            rejected = [e["name"] for e in results if not e["accepted"]]
            self.diag_status.config(
                text=("✓ Wszystkie sondowane komendy przyjęte"
                      if not rejected else
                      "⚠ Odrzucone komendy opcjonalne: " + ", ".join(rejected)),
                fg=(self.config.COLOR_ACCENT if not rejected else "#E65100"))

    # ================================================================== #
    # BEZPIECZENSTWO
    # ================================================================== #
    def _create_security_tab(self) -> None:
        frame = self._tab("Bezpieczeństwo")
        self._security_tab_index = self.notebook.index("end") - 1

        tk.Label(frame, text="Hasło panelu inżynieryjnego",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 13, "bold")).pack(pady=(16, 4))
        self.security_hint = tk.Label(
            frame,
            text=(f"W pliku station_config.json trzymany jest wyłącznie skrót "
                  f"PBKDF2-SHA256 hasła — w plikach aplikacji hasła nie ma,\n"
                  f"więc nie da się go odczytać z EXE. Obowiązujące hasło "
                  f"stanowiska jest opisane w dokumentacji wdrożeniowej.\n"
                  f"Minimalna długość przy zmianie: "
                  f"{MIN_PASSWORD_LENGTH} znaków."),
            bg=self.config.COLOR_WHITE, fg="#666666",
            font=("Arial", 9, "italic"), justify="center")
        self.security_hint.pack(pady=(0, 12))

        inner = self._card(frame)
        self._pw_new_var = tk.StringVar()
        self._pw_confirm_var = tk.StringVar()
        for row, (label, variable) in enumerate((
            ("Nowe hasło:", self._pw_new_var),
            ("Powtórz hasło:", self._pw_confirm_var),
        )):
            tk.Label(inner, text=label, bg=self.config.COLOR_WHITE, fg="#444444",
                     font=("Arial", 11), width=18, anchor="w").grid(
                         row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            tk.Entry(inner, textvariable=variable, show="*", font=("Arial", 11),
                     width=24, relief=tk.SOLID, borderwidth=1).grid(
                         row=row, column=1, sticky="w", pady=6)

        self.security_status = tk.Label(frame, text="",
                                        bg=self.config.COLOR_WHITE,
                                        font=("Arial", 10))
        self.security_status.pack(pady=(0, 4))
        tk.Button(frame, text="Ustaw nowe hasło", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", width=22, height=2,
                  command=self._save_password).pack(pady=6)

        tk.Label(frame,
                 text=(f"Dziennik audytowy zmian konfiguracji:\n"
                       f"{audit_log_path()}\n\nSkrót w pliku chroni przed "
                       "odczytaniem hasła, ale nie przed jego podmianą przez "
                       "osobę\nmającą prawo zapisu do folderu aplikacji. "
                       "Folder stanowiska musi mieć uprawnienia NTFS\n"
                       "ograniczone do technologa."),
                 bg=self.config.COLOR_WHITE, fg="#E65100",
                 font=("Arial", 8, "italic"), justify="left",
                 wraplength=640).pack(pady=(14, 0), padx=30, anchor="w")

    def _save_password(self) -> None:
        if self._pw_new_var.get() != self._pw_confirm_var.get():
            self.security_status.config(text="✗ Hasła nie są identyczne",
                                        fg=self.config.COLOR_ERROR)
            return
        previous = getattr(self.config, "ADMIN_PASSWORD", None)
        try:
            self.config.ADMIN_PASSWORD = hash_password(self._pw_new_var.get())
            self.settings.save_config(self.config)
        except Exception as exc:
            self.config.ADMIN_PASSWORD = previous
            self.security_status.config(text=f"✗ Nie zapisano: {exc}",
                                        fg=self.config.COLOR_ERROR)
            return

        audit("ZMIANA/HASLO", "ustawiono nowe haslo panelu inzynieryjnego")
        self._pw_new_var.set("")
        self._pw_confirm_var.set("")
        self.security_status.config(
            text="✓ Hasło zmienione — obowiązuje od następnego wejścia",
            fg=self.config.COLOR_ACCENT)
