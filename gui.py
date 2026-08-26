"""Okno glowne aplikacji: skanowanie numeru seryjnego.

Profil testowy wskazuje jedno z dwoch zrodel, zaleznie od pola
``serial.identify_by`` w profilu:

* ``hwid``     - pierwsze 6 znakow S/N (mapa HWID). Wyrob sam mowi, czym jest;
                 wybor z listy musi sie z tym zgadzac, inaczej skan jest
                 odrzucany. Uzywane tam, gdzie numer seryjny niesie model.
* ``operator`` - lista rozwijana na ekranie startowym. Uzywane dla SR203/SR204,
                 gdzie numery seryjne nie niosa informacji o wyrobie. S/N jest
                 wtedy sprawdzany co do dlugosci (14 znakow) i zestawu znakow
                 (wielkie litery A-Z, cyfry 0-9); male litery sa podnoszone
                 juz przy wpisywaniu.

Profile moga byc wlaczane i wylaczane per stanowisko (``ENABLED_PRODUCTS``).
Zeskanowanie wyrobu przypisanego do wylaczonego profilu konczy sie czytelna
odmowa - nie cichym testem na niewlasciwych nastawach.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from admin_panel import AdminPanel
from product_profile import ProductCatalog
from security import AccessGate, audit, verify_password
from station_config import StationConfig


class HiPotApp:

    def __init__(self, root):
        self.root = root
        self.config = StationConfig()
        self.catalog = ProductCatalog()
        self.current_test_screen = None

        self._scan_pending = False
        self._scan_after_id = None
        self._admin_gate = AccessGate()
        self._shortcut_presses = 0
        self._shortcut_timer = None

        self._setup_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._build_screen()

    def _setup_window(self) -> None:
        self.root.title(self.config.WINDOW_TITLE)
        try:
            # "zoomed" istnieje tylko w Windows; brak obslugi nie moze
            # przewrocic startu aplikacji.
            self.root.state("zoomed")
        except tk.TclError:
            self.root.attributes("-fullscreen", False)
        self.root.configure(bg=self.config.COLOR_BG)

    def _on_closing(self) -> None:
        screen = self.current_test_screen
        busy = bool(screen is not None
                    and (screen.test_running
                         or getattr(screen, "_result_pending", False)))
        if busy and not messagebox.askyesno(
            "Cykl Hi-Pot w toku",
            "Zamknięcie aplikacji zatrzyma aktywny test lub przerwie "
            "finalizację wyniku. Zamknąć aplikację?",
            parent=self.root,
        ):
            return
        if screen is not None:
            screen.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # BUDOWA EKRANU
    # ------------------------------------------------------------------ #
    def _build_screen(self) -> None:
        self._create_header()
        self.main_frame = tk.Frame(self.root, bg=self.config.COLOR_BG)
        self.main_frame.pack(expand=True, fill=tk.BOTH, padx=20, pady=(16, 50))

        self._create_scan_panel(self.main_frame)
        self._create_catalog_warnings(self.main_frame)
        self._create_footer()
        self._bind_admin_shortcut()

    def show_scan_screen(self) -> None:
        self.current_test_screen = None
        self._scan_pending = False
        if self._scan_after_id:
            try:
                self.root.after_cancel(self._scan_after_id)
            except Exception:
                pass
            self._scan_after_id = None
        for widget in self.root.winfo_children():
            widget.destroy()
        self._build_screen()

    def _create_header(self) -> None:
        header = tk.Frame(self.root, bg=self.config.COLOR_PRIMARY, height=68)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=self.config.WINDOW_TITLE,
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 21, "bold")).pack(side=tk.LEFT, padx=20, pady=14)

    def _create_footer(self) -> None:
        footer = tk.Frame(self.root, bg=self.config.COLOR_PRIMARY, height=38)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(footer, text=self.config.footer_left(),
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=20, pady=9)
        tk.Label(footer, text="Autor: Kacper Urbanowicz",
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 10, "bold")).pack(side=tk.RIGHT, padx=20, pady=9)

    def _create_catalog_warnings(self, parent) -> None:
        """Odrzucone profile MUSZA byc widoczne, a nie cicho pominiete."""
        if not self.catalog.errors:
            return
        frame = tk.Frame(parent, bg="#ffebee", relief=tk.RAISED, borderwidth=2)
        frame.pack(fill=tk.X, pady=(10, 0))
        tk.Label(frame, text="⚠ Profile produktów odrzucone przy wczytaniu",
                 bg="#ffebee", fg=self.config.COLOR_ERROR,
                 font=("Arial", 10, "bold")).pack(anchor="w", padx=12, pady=(6, 2))
        for name, error in sorted(self.catalog.errors.items()):
            tk.Label(frame, text=f"• {name}: {error}", bg="#ffebee", fg="#b71c1c",
                     font=("Arial", 9), justify="left", wraplength=900).pack(
                         anchor="w", padx=22, pady=1)
        tk.Label(frame,
                 text="Wyroby przypisane do tych profili nie zostaną "
                      "przetestowane do czasu poprawienia plików.",
                 bg="#ffebee", fg="#b71c1c", font=("Arial", 8, "italic")).pack(
                     anchor="w", padx=12, pady=(2, 6))

    # ------------------------------------------------------------------ #
    # SKANOWANIE S/N
    # ------------------------------------------------------------------ #
    def _create_scan_panel(self, parent) -> None:
        center = tk.Frame(parent, bg=self.config.COLOR_BG)
        center.pack(expand=True)
        panel = tk.Frame(center, bg=self.config.COLOR_WHITE,
                         relief=tk.RAISED, borderwidth=2)
        panel.pack(padx=50, pady=30)

        tk.Label(panel, text="Skanowanie numeru seryjnego",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 20, "bold")).pack(pady=(26, 10))

        self._create_profile_selector(panel)

        # Numer seryjny jest ZAWSZE wielkimi literami. Podnoszenie w locie,
        # a nie dopiero przy walidacji, zeby operator widzial na ekranie
        # dokladnie to, co trafi do raportu i do nazwy pliku.
        self._uppercase_pending = False
        self._serial_var = tk.StringVar()
        self._serial_var.trace_add("write", self._force_uppercase_serial)
        self.serial_entry = tk.Entry(panel, font=("Arial", 18, "bold"), width=28,
                                     justify="center", relief=tk.SOLID,
                                     borderwidth=2,
                                     textvariable=self._serial_var)
        self.serial_entry.pack(pady=12, padx=50)
        self.serial_entry.focus()
        self.serial_entry.bind("<Return>", lambda event: self._process_serial())

        self.scan_status_label = tk.Label(panel, text="",
                                         bg=self.config.COLOR_WHITE,
                                         font=("Arial", 11))
        self.scan_status_label.pack(pady=5)

        self.confirm_btn = tk.Button(
            panel, text="POTWIERDŹ", bg=self.config.COLOR_ACCENT,
            fg=self.config.COLOR_WHITE, font=("Arial", 14, "bold"),
            width=20, height=2, relief=tk.FLAT, cursor="hand2",
            command=self._process_serial)
        self.confirm_btn.pack(pady=(8, 30), padx=50)

    def _create_profile_selector(self, parent) -> None:
        """Lista rozwijana z profilami WLACZONYMI na tym stanowisku.

        Lista jest scisle powiazana z zakladka Profile w panelu inzynieryjnym:
        pokazuje dokladnie te profile, ktore sa tam zaznaczone. Po zamknieciu
        panelu ekran jest przebudowywany, wiec lista nie moze sie rozjechac
        z konfiguracja.

        Dla profilu z ``identify_by == "hwid"`` wybor z listy NIE nadpisuje
        rozpoznania z HWID - musi sie z nim zgadzac. Dwa niezalezne zrodla
        wskazujace ten sam profil sa mocniejszym zabezpieczeniem niz kazde
        z nich osobno.

        Dla profilu z ``identify_by == "operator"`` (np. SR203_SR204) numery
        seryjne nie nios informacji o wyrobie, wiec lista JEST zrodlem wyboru
        profilu, a numer seryjny sprawdzany jest tylko co do dlugosci
        i zestawu znakow.
        """
        self._profile_choices: dict[str, Any] = {}
        labels: list[str] = []
        for profile in self.catalog.all():
            if not self.config.is_product_enabled(profile.product_id):
                continue
            label = profile.display_name
            if label in self._profile_choices:
                label = f"{profile.display_name} ({profile.product_id})"
            self._profile_choices[label] = profile
            labels.append(label)

        row = tk.Frame(parent, bg=self.config.COLOR_WHITE)
        row.pack(pady=(0, 6), padx=50, fill=tk.X)
        tk.Label(row, text="Profil testowy:", bg=self.config.COLOR_WHITE,
                 fg="#444444", font=("Arial", 11, "bold")).pack(side=tk.LEFT,
                                                                padx=(0, 10))

        self._selected_profile_var = tk.StringVar(
            value=labels[0] if labels else "")
        self.profile_combo = ttk.Combobox(
            row, textvariable=self._selected_profile_var, values=labels,
            state="readonly" if len(labels) > 1 else "disabled",
            font=("Arial", 12), width=30)
        self.profile_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.profile_combo.bind("<<ComboboxSelected>>",
                                lambda event: self._on_profile_selected())

        self.profile_info_label = tk.Label(
            parent, text="", bg=self.config.COLOR_WHITE, font=("Arial", 9),
            justify="center", wraplength=420)
        self.profile_info_label.pack(pady=(0, 8), padx=50)
        self._on_profile_selected()

    def _force_uppercase_serial(self, *_args) -> None:
        """Podnosi zawartosc pola S/N do wielkich liter.

        Praca idzie przez ``after_idle`` i przez SAM WIDGET, nie przez
        zmienna: ustawienie StringVar wewnatrz jego wlasnego trace'a
        aktualizuje zmienna, ale Tk nadpisuje potem tresc pola stara
        wartoscia - operator widzialby male litery mimo poprawnej zmiennej.
        """
        widget = getattr(self, "serial_entry", None)
        if widget is None or self._uppercase_pending:
            return
        self._uppercase_pending = True
        try:
            self.root.after_idle(self._apply_uppercase_serial)
        except tk.TclError:
            self._uppercase_pending = False

    def _apply_uppercase_serial(self) -> None:
        self._uppercase_pending = False
        widget = getattr(self, "serial_entry", None)
        if widget is None:
            return
        try:
            current = widget.get()
            upper = current.upper()
            if upper == current:
                return
            cursor = widget.index(tk.INSERT)
            state = widget.cget("state")
            if state != "normal":
                widget.config(state="normal")
            widget.delete(0, tk.END)
            widget.insert(0, upper)
            widget.icursor(cursor)
            if state != "normal":
                widget.config(state=state)
        except tk.TclError:
            # Okno moglo zostac zamkniete zanim doszlo do bezczynnosci.
            return

    def _selected_profile(self):
        return self._profile_choices.get(self._selected_profile_var.get())

    def _on_profile_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.profile_info_label.config(
                text="⛔ Żaden profil nie jest włączony na tym stanowisku — "
                     "włącz profil w panelu inżynieryjnym",
                fg=self.config.COLOR_ERROR)
            return

        if not profile.matches_instrument(self.config.INSTRUMENT_MODEL):
            self.profile_info_label.config(
                text=f"⛔ Ten profil wymaga Chromy "
                     f"{'/'.join(profile.allowed_models)}, a stanowisko ma "
                     f"{self.config.INSTRUMENT_MODEL}",
                fg=self.config.COLOR_ERROR)
            return

        lengths = " lub ".join(str(x) for x in profile.serial_lengths)
        self.profile_info_label.config(
            text=f"{profile.step_count} krok(ów) · "
                 f"{profile.total_duration:.1f} s · Chroma "
                 f"{'/'.join(profile.allowed_models)} · "
                 f"S/N {lengths} znaków",
            fg="#888888")

    def _process_serial(self) -> None:
        if self._scan_pending:
            return
        raw = self.serial_entry.get().strip().upper()
        if not raw:
            self.scan_status_label.config(text="Wprowadź numer seryjny!",
                                          fg=self.config.COLOR_ERROR)
            return

        selected = self._selected_profile()
        if selected is None:
            self._reject_scan(
                "Żaden profil nie jest włączony na tym stanowisku — "
                "włącz profil w panelu inżynieryjnym")
            return

        if selected.requires_hwid:
            scan = self._resolve_by_hwid(raw, selected)
        else:
            scan = self._resolve_by_operator_choice(raw, selected)
        if scan is None:
            return

        if not self.config.is_product_enabled(scan.product_id):
            self._reject_scan(
                f"Profil {scan.profile.display_name} jest wyłączony na tym "
                f"stanowisku — włącz go w panelu inżynieryjnym")
            return

        if not scan.profile.matches_instrument(self.config.INSTRUMENT_MODEL):
            self._reject_scan(
                f"{scan.profile.display_name} wymaga Chromy "
                f"{'/'.join(scan.profile.allowed_models)}, a to stanowisko ma "
                f"{self.config.INSTRUMENT_MODEL}")
            return

        self.scan_status_label.config(
            text=f"✓ {scan.profile.display_name} | model {scan.model_name} | "
                 f"{scan.profile.step_count} krok(ów)",
            fg=self.config.COLOR_ACCENT)

        self._scan_pending = True
        self.serial_entry.config(state="disabled")
        self.confirm_btn.config(state="disabled")
        self._scan_after_id = self.root.after(
            300, lambda: self._show_test_screen(scan))

    def _resolve_by_hwid(self, raw: str, selected):
        """Profil rozpoznany z mapy HWID; wybor z listy musi sie zgadzac."""
        from hwid_map import HwidMap

        # Niedostepna mapa HWID nie moze przewrocic aplikacji przez fatalny
        # handler Tk - operator musi dostac komunikat i moc powtorzyc skan.
        try:
            valid, outcome = HwidMap(self.catalog).resolve(raw)
        except Exception as exc:
            self._reject_scan(f"Mapa HWID niedostępna: {exc}")
            return None

        if not valid:
            self._reject_scan(str(outcome))
            return None

        scan = outcome
        if scan.product_id != selected.product_id:
            # Wybor z listy i HWID musza wskazac ten sam profil. Rozbieznosc
            # oznacza albo zla sztuke na stanowisku, albo zle wybrany profil -
            # w obu przypadkach test bylby wykonany na niewlasciwych nastawach.
            self._reject_scan(
                f"Zeskanowano {scan.profile.display_name}, a wybrany profil to "
                f"{selected.display_name} — popraw wybór albo weź właściwy wyrób")
            return None
        return scan

    def _resolve_by_operator_choice(self, raw: str, selected):
        """Profil wskazany przez operatora z listy; S/N tylko walidowany.

        Uzywane dla wyrobow, ktorych numer seryjny nie niesie informacji
        o modelu (SR203/SR204). Numer musi miec dlugosc z profilu i skladac
        sie wylacznie z wielkich liter A-Z i cyfr 0-9 - male litery zostaly
        juz podniesione przy wprowadzaniu.

        Mapa HWID jest mimo to sprawdzana: jesli prefiks sztuki jest w niej
        opisany i wskazuje INNY profil, skan zostaje odrzucony. Kosztuje to
        nic, a chroni stanowisko mieszane przed testem na zlych nastawach.
        """
        from hwid_map import resolve_for_profile

        valid, outcome = resolve_for_profile(selected, raw, self.catalog)
        if not valid:
            self._reject_scan(str(outcome))
            return None
        return outcome

    def _reject_scan(self, message: str) -> None:
        self.scan_status_label.config(text=f"✗ {message}",
                                      fg=self.config.COLOR_ERROR)
        self.serial_entry.delete(0, tk.END)
        self.serial_entry.focus()

    def _show_test_screen(self, scan) -> None:
        self._scan_after_id = None
        screen = None
        try:
            from test_screen import TestScreen

            screen = TestScreen(parent=self.root, config=self.config, scan=scan,
                                app_ref=self)
            self.current_test_screen = screen
            screen.show()
        except Exception as exc:
            if screen is not None:
                try:
                    screen.shutdown()
                except Exception:
                    pass
            self.current_test_screen = None
            self._scan_pending = False
            messagebox.showerror(
                "Błąd otwierania ekranu testowego",
                f"Ekran testowy nie został uruchomiony:\n{exc}",
                parent=self.root)
            self.show_scan_screen()

    # ------------------------------------------------------------------ #
    # PANEL ADMINISTRATORA
    # ------------------------------------------------------------------ #
    def _bind_admin_shortcut(self) -> None:
        self._shortcut_presses = 0
        self._shortcut_timer = None
        self.root.bind("<Control-Alt-d>", self._on_config_shortcut)
        self.root.bind("<Control-Alt-D>", self._on_config_shortcut)

    def _on_config_shortcut(self, event=None) -> None:
        if self.current_test_screen is not None or self._scan_pending:
            return
        self._shortcut_presses += 1
        if self._shortcut_timer:
            self.root.after_cancel(self._shortcut_timer)
        self._shortcut_timer = self.root.after(
            1000, lambda: setattr(self, "_shortcut_presses", 0))
        if self._shortcut_presses >= 3:
            self._shortcut_presses = 0
            self._show_password_dialog()

    def _show_password_dialog(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Panel inżynieryjny")
        window.geometry("420x240")
        window.configure(bg=self.config.COLOR_BG)
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        frame = tk.Frame(window, bg=self.config.COLOR_WHITE, relief=tk.RAISED,
                         borderwidth=2)
        frame.pack(expand=True, fill=tk.BOTH, padx=20, pady=20)
        tk.Label(frame, text="Panel inżynieryjny",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 14, "bold")).pack(pady=(18, 8))
        tk.Label(frame, text="Wprowadź hasło:", bg=self.config.COLOR_WHITE,
                 fg="#333333", font=("Arial", 11)).pack(pady=(6, 4))

        entry = tk.Entry(frame, font=("Arial", 12), width=22, justify="center",
                         show="*", relief=tk.SOLID, borderwidth=2)
        entry.pack(pady=8)
        entry.focus()

        error_label = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                               fg=self.config.COLOR_ERROR, font=("Arial", 9))
        error_label.pack()

        if self._admin_gate.is_locked():
            error_label.config(
                text=f"Dostęp zablokowany na "
                     f"{self._admin_gate.seconds_remaining():.0f} s")
            entry.config(state="disabled")

        def check_password() -> None:
            if self._admin_gate.is_locked():
                error_label.config(
                    text=f"Dostęp zablokowany na "
                         f"{self._admin_gate.seconds_remaining():.0f} s")
                entry.delete(0, tk.END)
                return
            try:
                ok = verify_password(
                    entry.get(), getattr(self.config, "ADMIN_PASSWORD", None))
            except Exception as exc:
                audit("PANEL/BLAD_REKORDU_HASLA", str(exc))
                error_label.config(text=f"Konfiguracja hasła uszkodzona: {exc}")
                entry.delete(0, tk.END)
                return

            if ok:
                self._admin_gate.reset()
                audit("PANEL/WEJSCIE", "")
                window.destroy()
                self._show_admin_panel()
                return

            lockout = self._admin_gate.register_failure()
            if lockout:
                audit("PANEL/BLOKADA", f"{lockout:.0f} s po nieudanych probach")
                error_label.config(text=f"Zbyt wiele prób — blokada "
                                        f"{lockout:.0f} s")
                entry.config(state="disabled")
                window.after(int(lockout * 1000), lambda: (
                    entry.config(state="normal"), error_label.config(text=""),
                    entry.focus()) if window.winfo_exists() else None)
            else:
                audit("PANEL/BLEDNE_HASLO",
                      f"pozostalo prob: {self._admin_gate.attempts_left()}")
                error_label.config(
                    text=f"Nieprawidłowe hasło! (pozostało prób: "
                         f"{self._admin_gate.attempts_left()})")
            entry.delete(0, tk.END)
            entry.focus()

        entry.bind("<Return>", lambda event: check_password())
        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=10)
        tk.Button(buttons, text="OK", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  width=10, relief=tk.FLAT, cursor="hand2",
                  command=check_password).pack(side=tk.LEFT, padx=5)
        tk.Button(buttons, text="Anuluj", bg="#999999",
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  width=10, relief=tk.FLAT, cursor="hand2",
                  command=window.destroy).pack(side=tk.LEFT, padx=5)

    def _show_admin_panel(self) -> None:
        if self.current_test_screen is not None or self._scan_pending:
            messagebox.showwarning(
                "Panel zablokowany",
                "Ustawień nie można zmieniać na ekranie testowym. "
                "Wróć do menu głównego.",
                parent=self.root)
            return
        panel = AdminPanel(self.root, self.config, self.catalog)
        panel.show()
        # Zestaw wlaczonych profili moze sie zmienic w panelu - po jego
        # zamknieciu przebudowujemy ekran, zeby lista rozwijana zawsze
        # odpowiadala zakladce Profile.
        if panel.window is not None:
            self.root.wait_window(panel.window)
        self.show_scan_screen()
