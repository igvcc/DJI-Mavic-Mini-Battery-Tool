#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DJI Mavic Mini 1 (WM160 / bq9003 / BQ40z307) Battery Service Tool v2
Single-file desktop GUI for Windows:
- Checks & downloads/installs CH340/CH341 USB-UART drivers if missing
- Auto-connects to Arduino Nano running dji_battery_inspector
- Visual Battery Condition / Health (SOH %), Cell Balance bars, FET & Security badges
- Full Data Modification tab (Cycle Count, Design Capacity/Energy/Voltage, Mfg/Dev/Chem, PF2, 30074, Hex Editor)
- Full EEPROM backup (0x4000-0x5FFF) & Live Serial Console
"""

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import serial
import serial.tools.list_ports

CH340_DRIVER_URL = "https://sparks.gogo.co.nz/assets/_site_/downloads/CH34x_Install_Windows_v3_4.zip"
BAUD_RATE = 115200


def resource_path(rel_path: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)

# Modern Dark Palette
BG_MAIN = "#0d1117"
BG_CARD = "#161b22"
BG_SUB = "#1c222b"
BG_INPUT = "#21262d"
BORDER = "#30363d"
FG_TEXT = "#e6edf3"
FG_MUTED = "#8b949e"
ACCENT = "#2f81f7"
GREEN = "#3fb950"
LIME = "#7ee787"
YELLOW = "#d29922"
ORANGE = "#db6d28"
RED = "#f85149"


def check_ch340_driver() -> tuple[bool, str]:
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        out = subprocess.check_output(
            ["pnputil", "/enum-drivers"],
            stderr=subprocess.STDOUT,
            text=True,
            errors="ignore",
            startupinfo=si,
            timeout=10,
        )
        if re.search(r"ch341ser\.inf|wch\.cn|ch340", out, re.IGNORECASE):
            return True, "Sterownik CH340/CH341 OK"
    except Exception as e:
        return False, f"Błąd pnputil: {e}"

    for p in serial.tools.list_ports.comports():
        if "1A86:7523" in (p.hwid or "").upper() or "CH340" in (p.description or "").upper():
            return True, f"CH340 aktywny ({p.device})"

    return False, "Brak sterownika CH340/CH341"


def download_and_install_ch340(log_cb) -> tuple[bool, str]:
    tmp_dir = tempfile.mkdtemp(prefix="ch340_drv_")
    zip_path = os.path.join(tmp_dir, "ch340.zip")
    ext_dir = os.path.join(tmp_dir, "extracted")

    try:
        log_cb(f"Pobieranie sterownika CH340 z: {CH340_DRIVER_URL}")
        req = urllib.request.Request(CH340_DRIVER_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp, open(zip_path, "wb") as f:
            f.write(resp.read())
        log_cb("Rozpakowywanie paczki sterowników...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(ext_dir)
    except Exception as e:
        return False, f"Błąd pobierania/rozpakowania: {e}"

    setup_exe = None
    inf_file = None
    for root, _, files in os.walk(ext_dir):
        for fn in files:
            low = fn.lower()
            if low == "setup.exe" and setup_exe is None:
                setup_exe = os.path.join(root, fn)
            elif low.endswith(".inf") and "ch341" in low and inf_file is None:
                inf_file = os.path.join(root, fn)

    try:
        if inf_file:
            log_cb(f"Instalacja INF przez pnputil: {inf_file}")
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "pnputil.exe", f'/add-driver "{inf_file}" /install', None, 1
            )
            if rc <= 32:
                return False, f"Błąd uruchomienia instalatora (kod {rc})"
            time.sleep(3)
            return True, "Zainstalowano sterownik CH340 (pnputil)"
        elif setup_exe:
            log_cb(f"Uruchamianie SETUP.EXE: {setup_exe}")
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", setup_exe, "/S", None, 1)
            if rc <= 32:
                return False, f"Błąd uruchomienia SETUP.EXE (kod {rc})"
            time.sleep(4)
            return True, "Uruchomiono instalator CH340 SETUP.EXE"
        return False, "Nie znaleziono pliku .inf ani SETUP.EXE"
    except Exception as e:
        return False, f"Błąd instalacji: {e}"


class DjiBatteryApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DJI Mavic Mini 1 — Battery Service & Programmer Tool")
        self.root.geometry("1120x820")
        self.root.minsize(980, 720)
        self.root.configure(bg=BG_MAIN)

        self.ser: serial.Serial | None = None
        self.serial_lock = threading.Lock()
        self.busy = False
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.pullup_state = True
        self.seal_after_edit_var = tk.BooleanVar(value=True)

        self._configure_styles()
        self._build_ui()
        self.root.after(200, self._startup_check)

    def _configure_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG_MAIN, foreground=FG_TEXT, fieldbackground=BG_INPUT, bordercolor=BORDER)
        s.configure("TFrame", background=BG_MAIN)
        s.configure("Card.TFrame", background=BG_CARD)
        s.configure("Sub.TFrame", background=BG_SUB)
        s.configure("TLabel", background=BG_MAIN, foreground=FG_TEXT, font=("Segoe UI", 9))
        s.configure("Card.TLabel", background=BG_CARD, foreground=FG_TEXT, font=("Segoe UI", 9))
        s.configure("Sub.TLabel", background=BG_SUB, foreground=FG_TEXT, font=("Segoe UI", 9))
        s.configure("H1.TLabel", background=BG_CARD, foreground=FG_TEXT, font=("Segoe UI", 11, "bold"))
        s.configure("BigVal.TLabel", background=BG_SUB, foreground=FG_TEXT, font=("Segoe UI", 14, "bold"))
        s.configure("Val.TLabel", background=BG_SUB, foreground=ACCENT, font=("Consolas", 10, "bold"))
        s.configure("TButton", background=BG_INPUT, foreground=FG_TEXT, borderwidth=1, padding=(10, 5), font=("Segoe UI", 9))
        s.map("TButton", background=[("active", "#30363d")])
        s.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", font=("Segoe UI", 9, "bold"))
        s.map("Primary.TButton", background=[("active", "#1f6feb")])
        s.configure("Success.TButton", background="#238636", foreground="#ffffff", font=("Segoe UI", 9, "bold"))
        s.map("Success.TButton", background=[("active", "#2ea043")])
        s.configure("Danger.TButton", background="#9e2a2b", foreground="#ffffff", font=("Segoe UI", 9, "bold"))
        s.map("Danger.TButton", background=[("active", "#c93b3c")])
        s.configure("TCombobox", fieldbackground=BG_INPUT, background=BG_INPUT, foreground=FG_TEXT)
        s.configure("TCheckbutton", background=BG_CARD, foreground=FG_TEXT)
        s.configure("TNotebook", background=BG_MAIN, borderwidth=0)
        s.configure("TNotebook.Tab", background=BG_INPUT, foreground=FG_MUTED, padding=(14, 7), font=("Segoe UI", 9, "bold"))
        s.map("TNotebook.Tab", background=[("selected", BG_CARD)], foreground=[("selected", FG_TEXT)])
        s.configure("TProgressbar", troughcolor=BG_INPUT, background=ACCENT, thickness=14)
        s.configure("Health.Horizontal.TProgressbar", troughcolor=BG_INPUT, background=GREEN, thickness=18)

    def _build_ui(self):
        # ── Top Header Bar ────────────────────────────────────────────────────
        top = ttk.Frame(self.root, style="Card.TFrame", padding=10)
        top.pack(fill="x", padx=12, pady=(10, 6))

        self.drv_label = ttk.Label(top, text="Sterownik CH340: sprawdzanie...", style="Card.TLabel", font=("Segoe UI", 9, "bold"))
        self.drv_label.pack(side="left", padx=(0, 8))
        ttk.Button(top, text="Sterowniki CH340", command=self.on_install_drivers).pack(side="left", padx=(0, 16))

        ttk.Label(top, text="Port:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=28, state="readonly")
        self.port_combo.pack(side="left", padx=(0, 4))
        ttk.Button(top, text="Odśwież", command=self.refresh_ports).pack(side="left", padx=2)
        self.btn_conn = ttk.Button(top, text="Połącz", style="Primary.TButton", command=self.toggle_connect)
        self.btn_conn.pack(side="left", padx=4)
        ttk.Button(top, text="Skanuj I²C", command=lambda: self.run_async(self.cmd_scan)).pack(side="left", padx=4)

        self.btn_pullup = ttk.Button(top, text="Pull-up 5V: ON", command=lambda: self.run_async(self.cmd_toggle_pullup))
        self.btn_pullup.pack(side="right", padx=2)

        # ── Tabbed Notebook ───────────────────────────────────────────────────
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=4)

        self.tab_diag = ttk.Frame(self.nb, padding=8)
        self.tab_edit = ttk.Frame(self.nb, padding=8)
        self.tab_eeprom = ttk.Frame(self.nb, padding=8)
        self.tab_flash = ttk.Frame(self.nb, padding=8)

        self.nb.add(self.tab_diag, text="  Diagnostyka i Kondycja  ")
        self.nb.add(self.tab_edit, text="  Modyfikacja danych i Cykli  ")
        self.nb.add(self.tab_eeprom, text="  EEPROM & Konsola  ")
        self.nb.add(self.tab_flash, text="  Wgrywanie kodu & Schematy  ")

        self._build_diag_tab()
        self._build_edit_tab()
        self._build_eeprom_tab()
        self._build_flash_tab()

        # ── Compact Status Log Footer ─────────────────────────────────────────
        foot = ttk.Frame(self.root, style="Card.TFrame", padding=6)
        foot.pack(fill="x", padx=12, pady=(2, 10))
        self.mini_log = tk.Text(
            foot, height=4, bg="#0a0d12", fg="#9da7b3", font=("Consolas", 8),
            relief="flat", state="disabled"
        )
        self.mini_log.pack(fill="x")

    # ── TAB 1: Diagnostyka i Kondycja ─────────────────────────────────────────
    def _build_diag_tab(self):
        # Action toolbar
        bar = ttk.Frame(self.tab_diag, style="Card.TFrame", padding=10)
        bar.pack(fill="x", pady=(0, 8))

        ttk.Button(bar, text="Odczytaj stan baterii", style="Primary.TButton", command=lambda: self.run_async(self.cmd_diag)).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(bar, text="Auto-odświeżanie (2 s)", variable=self.auto_refresh_var, command=self._schedule_auto).pack(side="left", padx=(0, 16))
        ttk.Button(bar, text="PEŁNE ODBLOKOWANIE + 0 CYKLI (1-klik)", style="Success.TButton", command=lambda: self.run_async(self.cmd_full_unlock_and_zero)).pack(side="right")

        # Banner
        self.health_banner = tk.Label(
            self.tab_diag,
            text="Połącz Arduino z baterią (0x0B) i kliknij 'Odczytaj stan baterii'",
            bg=BG_INPUT, fg=FG_TEXT, font=("Segoe UI", 10, "bold"), anchor="w", padx=12, pady=8
        )
        self.health_banner.pack(fill="x", pady=(0, 8))

        # Main split: Left (Condition + Cells), Right (Telemetry + Security Badges)
        grid = ttk.Frame(self.tab_diag)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1)

        # Left column: Kondycja (Health) + Ogniwa (Cells)
        left = ttk.Frame(grid, style="Card.TFrame", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(left, text="KONDYCJA BATERII (Stan zdrowia / SOH)", style="H1.TLabel").pack(anchor="w")

        cond_box = ttk.Frame(left, style="Sub.TFrame", padding=10)
        cond_box.pack(fill="x", pady=(6, 10))

        self.soh_pct_lbl = tk.Label(cond_box, text="— %", bg=BG_SUB, fg=FG_TEXT, font=("Segoe UI", 24, "bold"))
        self.soh_pct_lbl.pack(side="left", padx=(0, 14))

        soh_right = ttk.Frame(cond_box, style="Sub.TFrame")
        soh_right.pack(side="left", fill="x", expand=True)
        self.soh_grade_lbl = tk.Label(soh_right, text="Oczekiwanie na odczyt...", bg=BG_SUB, fg=FG_MUTED, font=("Segoe UI", 11, "bold"), anchor="w")
        self.soh_grade_lbl.pack(fill="x")
        self.soh_bar = ttk.Progressbar(soh_right, style="Health.Horizontal.TProgressbar", maximum=100, value=0)
        self.soh_bar.pack(fill="x", pady=(4, 4))
        self.soh_detail_lbl = tk.Label(soh_right, text="FCC: — / Design: — mAh  |  Cykle: —", bg=BG_SUB, fg=FG_MUTED, font=("Segoe UI", 9), anchor="w")
        self.soh_detail_lbl.pack(fill="x")

        # Charge & Cells
        ttk.Label(left, text="NAPIĘCIA OGNIW I BALANS", style="H1.TLabel").pack(anchor="w", pady=(4, 6))
        cells_box = ttk.Frame(left, style="Sub.TFrame", padding=10)
        cells_box.pack(fill="x", pady=(0, 8))

        self.c1_bar, self.c1_lbl = self._make_cell_row(cells_box, "Cell 1 (0x3F):")
        self.c2_bar, self.c2_lbl = self._make_cell_row(cells_box, "Cell 2 (0x3E):")

        delta_row = ttk.Frame(cells_box, style="Sub.TFrame")
        delta_row.pack(fill="x", pady=(6, 0))
        ttk.Label(delta_row, text="Różnica napięć (Delta):", style="Sub.TLabel", foreground=FG_MUTED).pack(side="left")
        self.delta_badge = tk.Label(delta_row, text="— mV", bg=BG_INPUT, fg=FG_TEXT, font=("Consolas", 10, "bold"), padx=8, pady=2)
        self.delta_badge.pack(side="right")

        # Charge bar
        soc_box = ttk.Frame(left, style="Sub.TFrame", padding=10)
        soc_box.pack(fill="x")
        soc_top = ttk.Frame(soc_box, style="Sub.TFrame")
        soc_top.pack(fill="x")
        ttk.Label(soc_top, text="Naładowanie (RSOC / ASOC):", style="Sub.TLabel", foreground=FG_MUTED).pack(side="left")
        self.soc_lbl = ttk.Label(soc_top, text="— %", style="Val.TLabel")
        self.soc_lbl.pack(side="right")
        self.soc_bar = ttk.Progressbar(soc_box, maximum=100, value=0)
        self.soc_bar.pack(fill="x", pady=(4, 0))

        # Right column: Telemetry + Security Badges
        right = ttk.Frame(grid, style="Card.TFrame", padding=12)
        right.grid(row=0, column=1, sticky="nsew")

        ttk.Label(right, text="TELEMETRIA I DANE PAKIETU", style="H1.TLabel").pack(anchor="w", pady=(0, 6))
        tgrid = ttk.Frame(right, style="Card.TFrame")
        tgrid.pack(fill="x")
        tgrid.columnconfigure(0, weight=1)
        tgrid.columnconfigure(1, weight=1)

        self.tele_vars: dict[str, tk.StringVar] = {}
        t_items = [
            ("model", "Model / Producent", 0, 0),
            ("sn", "Numer seryjny DJI (S/N)", 0, 1),
            ("fw", "Firmware / Chemia", 1, 0),
            ("cycles", "Licznik cykli (0x17)", 1, 1),
            ("vpack", "Napięcie pakietu (0x09)", 2, 0),
            ("current", "Prąd (0x0A)", 2, 1),
            ("temp", "Temperatura (0x08)", 3, 0),
            ("cap", "Pojemność Rem / FCC / Des", 3, 1),
        ]
        for key, title, r, c in t_items:
            cell = ttk.Frame(tgrid, style="Sub.TFrame", padding=7)
            cell.grid(row=r, column=c, sticky="nsew", padx=3, pady=3)
            ttk.Label(cell, text=title, style="Sub.TLabel", foreground=FG_MUTED, font=("Segoe UI", 8)).pack(anchor="w")
            v = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=v, style="Val.TLabel").pack(anchor="w")
            self.tele_vars[key] = v

        ttk.Label(right, text="STAN ZABEZPIECZEŃ I TRANZYSTORÓW (BMS)", style="H1.TLabel").pack(anchor="w", pady=(10, 6))
        badge_grid = ttk.Frame(right, style="Card.TFrame")
        badge_grid.pack(fill="x")
        for c in range(3):
            badge_grid.columnconfigure(c, weight=1)

        self.badges: dict[str, tk.Label] = {}
        b_defs = [
            ("sec", "TRYB: —", 0, 0),
            ("dsg", "DSG FET: —", 0, 1),
            ("chg", "CHG FET: —", 0, 2),
            ("pf", "PF: —", 1, 0),
            ("xdsg", "XDSG / XCHG: —", 1, 1),
            ("fuse", "FUSE / SAF: —", 1, 2),
        ]
        for key, txt, r, c in b_defs:
            lbl = tk.Label(badge_grid, text=txt, bg=BG_INPUT, fg=FG_TEXT, font=("Segoe UI", 9, "bold"), pady=6)
            lbl.grid(row=r, column=c, sticky="ew", padx=3, pady=3)
            self.badges[key] = lbl

        # Quick expert buttons
        qrow = ttk.Frame(right, style="Card.TFrame")
        qrow.pack(fill="x", pady=(10, 0))
        ttk.Button(qrow, text="Unseal + FAS (u)", command=lambda: self.run_async(self.cmd_unseal)).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(qrow, text="Reset BMS (0x0041)", command=lambda: self.run_async(lambda: self.send_simple("r", 1.2))).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(qrow, text="Zaplombuj (0x0030)", command=lambda: self.run_async(lambda: self.send_simple("k", 0.7))).pack(side="left", expand=True, fill="x", padx=2)

    def _make_cell_row(self, parent, title: str):
        row = ttk.Frame(parent, style="Sub.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=title, style="Sub.TLabel", width=15).pack(side="left")
        bar = ttk.Progressbar(row, maximum=1800, value=0)  # 2500..4300 mV mapped to 0..1800
        bar.pack(side="left", fill="x", expand=True, padx=8)
        lbl = ttk.Label(row, text="— mV", style="Val.TLabel", width=9)
        lbl.pack(side="right")
        return bar, lbl

    # ── TAB 2: Modyfikacja danych i Cykli ────────────────────────────────────
    def _build_edit_tab(self):
        top = ttk.Frame(self.tab_edit, style="Card.TFrame", padding=10)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="Pobierz wszystkie dane z BMS", style="Primary.TButton", command=lambda: self.run_async(self.cmd_load_editable_fields)).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(top, text="Automatycznie zaplombuj (Seal) po zapisie", variable=self.seal_after_edit_var).pack(side="left", padx=8)
        ttk.Button(top, text="ZAPISZ ZMIANY DO BMS", style="Success.TButton", command=self.on_save_all_fields).pack(side="right")

        split = ttk.Frame(self.tab_edit)
        split.pack(fill="both", expand=True)
        split.columnconfigure(0, weight=1)
        split.columnconfigure(1, weight=1)
        split.rowconfigure(0, weight=1)

        # Left: Structured Data Flash Editor
        form = ttk.Frame(split, style="Card.TFrame", padding=12)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(form, text="EDYCJA PARAMETRÓW DATA FLASH (BA01WM160)", style="H1.TLabel").pack(anchor="w", pady=(0, 8))

        self.edit_vars: dict[str, tk.StringVar] = {}
        self.edit_checks: dict[str, tk.BooleanVar] = {}
        fields = [
            ("cycles", "Liczba cykli (0x4340, uint16)", "0"),
            ("dji_sn", "Numer seryjny DJI S/N (0x494C)", "1U5X0BBEXP05DB"),
            ("design_cap", "Design Capacity [mAh] (0x4726)", "2400"),
            ("actual_cap", "Actual Capacity / Qmax [mAh] (0x4300)", "2400"),
            ("design_energy", "Design Energy [cWh] (0x4728)", "1728"),
            ("design_volt", "Design Voltage [mV] (0x472A)", "7200"),
            ("mfg_name", "Nazwa producenta (0x4070, ASCII)", "SDI"),
            ("dev_name", "Nazwa urządzenia (0x4085, ASCII)", "BA01WM160"),
            ("chem", "Chemia / ID (0x409A, ASCII)", "2044"),
        ]
        for key, label, default in fields:
            r = ttk.Frame(form, style="Card.TFrame")
            r.pack(fill="x", pady=4)
            chk = tk.BooleanVar(value=(key == "cycles"))
            ttk.Checkbutton(r, variable=chk).pack(side="left")
            ttk.Label(r, text=label, style="Card.TLabel", width=33).pack(side="left", padx=(4, 6))
            v = tk.StringVar(value=default)
            ent = ttk.Entry(r, textvariable=v, width=18)
            ent.pack(side="right", fill="x", expand=True)
            self.edit_vars[key] = v
            self.edit_checks[key] = chk

        ttk.Label(
            form,
            text="Zaznacz pola, które chcesz zmodyfikować i kliknij 'ZAPISZ ZMIANY DO BMS'.\nAplikacja sama wejdzie w Full Access (SEC=1), zapisze wybrane pola i zresetuje BMS.",
            style="Card.TLabel", foreground=FG_MUTED, font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(10, 0))

        # Right: Flags + Raw Hex Editor
        rpanel = ttk.Frame(split, style="Card.TFrame", padding=12)
        rpanel.grid(row=0, column=1, sticky="nsew")

        ttk.Label(rpanel, text="KASOWANIE FLAG I ZATRZASKÓW", style="H1.TLabel").pack(anchor="w", pady=(0, 6))
        fbtns = [
            ("Wyczyść standardowe PF (0x0029)", lambda: self.run_async(lambda: self.send_simple("c", 0.7))),
            ("Wyczyść DJI PF2 (0x4062 -> 01 23 45 67)", lambda: self.run_async(self.cmd_clear_pf2)),
            ("Wyczyść błąd 30074 (0x4A60 -> 00 00)", lambda: self.run_async(lambda: self.send_simple("z", 0.7))),
            ("Reset Lifetime Data (0x0028)", lambda: self.run_async(lambda: self.send_simple("l", 0.7))),
        ]
        for txt, cb in fbtns:
            ttk.Button(rpanel, text=txt, command=cb).pack(fill="x", pady=2)

        ttk.Separator(rpanel, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(rpanel, text="BEZPOŚREDNI EDYTOR HEX DATA FLASH (0x4000–0x5FFF)", style="H1.TLabel").pack(anchor="w", pady=(0, 6))

        hrow1 = ttk.Frame(rpanel, style="Card.TFrame")
        hrow1.pack(fill="x", pady=3)
        ttk.Label(hrow1, text="Adres HEX:", style="Card.TLabel").pack(side="left")
        self.hex_addr = ttk.Entry(hrow1, width=8)
        self.hex_addr.insert(0, "4340")
        self.hex_addr.pack(side="left", padx=6)
        ttk.Button(hrow1, text="Odczytaj 16B (b)", command=lambda: self.run_async(self.cmd_read_hex)).pack(side="left", padx=2)

        hrow2 = ttk.Frame(rpanel, style="Card.TFrame")
        hrow2.pack(fill="x", pady=4)
        ttk.Label(hrow2, text="Bajty HEX:", style="Card.TLabel").pack(side="left")
        self.hex_bytes = ttk.Entry(hrow2)
        self.hex_bytes.insert(0, "00 00")
        self.hex_bytes.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(hrow2, text="Zapisz (w)", style="Danger.TButton", command=self.on_write_hex).pack(side="right")

        self.hex_preview = tk.Text(rpanel, height=5, bg="#0a0d12", fg="#7ee787", font=("Consolas", 9), relief="flat", state="disabled")
        self.hex_preview.pack(fill="both", expand=True, pady=(6, 0))

    # ── TAB 3: EEPROM & Konsola ───────────────────────────────────────────────
    def _build_eeprom_tab(self):
        top = ttk.Frame(self.tab_eeprom, style="Card.TFrame", padding=10)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="Zrzut pełnego EEPROM (0x4000–0x5FFF) do .bin + .txt", style="Primary.TButton", command=self.on_dump_eeprom).pack(side="left", padx=(0, 12))
        self.dump_prog = ttk.Progressbar(top, mode="determinate", maximum=512)
        self.dump_prog.pack(side="left", fill="x", expand=True)

        con = ttk.Frame(self.tab_eeprom, style="Card.TFrame", padding=10)
        con.pack(fill="both", expand=True)
        ttk.Label(con, text="KONSOLA SZEREGOWA ARDUINO (115200 baud)", style="H1.TLabel").pack(anchor="w", pady=(0, 4))

        self.log_box = tk.Text(con, bg="#0a0d12", fg="#c9d1d9", font=("Consolas", 9), relief="flat", state="disabled")
        self.log_box.pack(fill="both", expand=True)

        cbar = ttk.Frame(con, style="Card.TFrame")
        cbar.pack(fill="x", pady=(6, 0))
        self.raw_cmd_entry = ttk.Entry(cbar)
        self.raw_cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.raw_cmd_entry.bind("<Return>", lambda e: self.run_async(self.cmd_raw))
        ttk.Button(cbar, text="Wyślij komendę", command=lambda: self.run_async(self.cmd_raw)).pack(side="right")

    # ── TAB 4: Wgrywanie kodu & Schematy ──────────────────────────────────────
    def _build_flash_tab(self):
        top = ttk.Frame(self.tab_flash, style="Card.TFrame", padding=10)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="WBUDOWANY PROGRAMATOR ARDUINO NANO (ATmega328P + dji_battery_inspector)", style="H1.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(top, style="Card.TFrame")
        row.pack(fill="x")

        ttk.Label(row, text="Bootloader:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.boot_var = tk.StringVar(value="Auto (115200 -> 57600)")
        boot_cb = ttk.Combobox(
            row, textvariable=self.boot_var, width=26, state="readonly",
            values=["Auto (115200 -> 57600)", "Nowy Bootloader (115200)", "Stary Bootloader (57600)"]
        )
        boot_cb.pack(side="left", padx=(0, 12))

        self.btn_flash = ttk.Button(row, text="Wgraj wbudowany firmware na Arduino Nano", style="Primary.TButton", command=self.on_flash_arduino)
        self.btn_flash.pack(side="left", padx=(0, 12))

        self.flash_status = ttk.Label(row, text="Gotowy do wgrania (.hex wbudowany w .exe)", style="Card.TLabel", foreground=FG_MUTED)
        self.flash_status.pack(side="left")

        # Schematic Canvas Card
        sch = ttk.Frame(self.tab_flash, style="Card.TFrame", padding=10)
        sch.pack(fill="both", expand=True)
        ttk.Label(sch, text="SCHEMAT PODŁĄCZENIA — ARDUINO NANO <-> BATERIA DJI MAVIC MINI 1 (MB2, 6-PIN 2.0 mm)", style="H1.TLabel").pack(anchor="w", pady=(0, 6))

        cv = tk.Canvas(sch, bg="#0b0f16", highlightthickness=1, highlightbackground=BORDER, height=390)
        cv.pack(fill="both", expand=True)
        self._draw_wiring_diagram(cv)

    def _draw_wiring_diagram(self, cv: tk.Canvas):
        # Arduino Nano Box (Left)
        cv.create_rectangle(40, 40, 260, 330, fill="#162133", outline="#3b82f6", width=2)
        cv.create_text(150, 60, text="ARDUINO NANO (5V)", fill="#ffffff", font=("Segoe UI", 11, "bold"))
        cv.create_text(150, 80, text="USB -> PC (CH340)", fill="#8b949e", font=("Segoe UI", 8))

        nano_pins = [
            (115, "A5 (SCL)", "#facc15"),
            (155, "A4 (SDA)", "#4ade80"),
            (195, "GND", "#9ca3af"),
            (245, "3.3V (opcja pull-up)", "#60a5fa"),
        ]
        for y, txt, col in nano_pins:
            cv.create_rectangle(235, y - 8, 265, y + 8, fill=col, outline="#0d1117")
            cv.create_text(225, y, text=txt, fill=col, anchor="e", font=("Consolas", 9, "bold"))

        # Battery 6-pin Connector Box (Right)
        cv.create_rectangle(680, 40, 960, 330, fill="#1c1f26", outline="#f59e0b", width=2)
        cv.create_text(820, 60, text="BATERIA MAVIC MINI 1 (MB2)", fill="#ffffff", font=("Segoe UI", 11, "bold"))
        cv.create_text(820, 80, text="Złącze 6-pin (raster 2.0 mm)", fill="#8b949e", font=("Segoe UI", 8))

        bat_pins = [
            (115, "Pin 1: SCL (SMBC)", "#facc15"),
            (155, "Pin 2: GND (PACK-)", "#9ca3af"),
            (195, "Pin 3: B+ (PACK+ 7.2-8.4V)", "#ef4444"),
            (235, "Pin 4: B+ (PACK+ 7.2-8.4V)", "#ef4444"),
            (275, "Pin 5: GND (PACK-)", "#9ca3af"),
            (315, "Pin 6: SDA (SMBD)", "#4ade80"),
        ]
        for y, txt, col in bat_pins:
            cv.create_rectangle(670, y - 8, 700, y + 8, fill=col, outline="#0d1117")
            cv.create_text(710, y, text=txt, fill=col, anchor="w", font=("Consolas", 9, "bold"))

        # Wires: Arduino -> Battery
        # A5 (115) -> Pin 1 SCL (115)
        cv.create_line(265, 115, 670, 115, fill="#facc15", width=3)
        cv.create_text(465, 103, text="SCL (Zegar)", fill="#facc15", font=("Segoe UI", 8, "bold"))

        # A4 (155) -> Pin 6 SDA (315) via clean orthogonal routing
        cv.create_line(265, 155, 360, 155, 360, 315, 670, 315, fill="#4ade80", width=3)
        cv.create_text(500, 303, text="SDA (Dane)", fill="#4ade80", font=("Segoe UI", 8, "bold"))

        # GND (195) -> Pin 2 GND (155)
        cv.create_line(265, 195, 330, 195, 330, 155, 670, 155, fill="#9ca3af", width=3)
        cv.create_text(500, 143, text="Wspólna masa (GND)", fill="#9ca3af", font=("Segoe UI", 8, "bold"))

        # Optional 4.7k pull-ups box
        cv.create_rectangle(390, 180, 620, 240, fill="#111827", outline="#60a5fa", dash=(4, 2))
        cv.create_text(505, 198, text="Opcja przy braku komunikacji:", fill="#60a5fa", font=("Segoe UI", 8, "bold"))
        cv.create_text(505, 215, text="Rezystory 4.7 kOhm: 3.3V -> SCL i SDA", fill="#cbd5e1", font=("Consolas", 8))
        cv.create_text(505, 230, text="(wtedy wyłącz 'Pull-up 5V' w górnym pasku)", fill="#8b949e", font=("Segoe UI", 7))

        # Wake-up PSU Warning Box (Bottom)
        cv.create_rectangle(40, 345, 960, 400, fill="#2b1616", outline="#ef4444", width=1)
        cv.create_text(
            500, 362,
            text="UWAGA: NIGDY nie podłączaj Pin 3 / Pin 4 (B+) do Arduino! Grozi spaleniem portu USB!",
            fill="#fca5a5", font=("Segoe UI", 9, "bold")
        )
        cv.create_text(
            500, 383,
            text="Wybudzanie uśpionego BMS: podaj zasilaczem 8.0 - 8.4 V (limit <= 0.5 A) na Pin 3/4 (B+) i Pin 2/5 (GND) ze wspólną masą.",
            fill="#e5e7eb", font=("Segoe UI", 8)
        )

    # ── Logging & Startup ─────────────────────────────────────────────────────
    def log(self, msg: str):
        clean = msg.rstrip()
        def _append():
            for box in (self.log_box, self.mini_log):
                box.configure(state="normal")
                box.insert("end", clean + "\n")
                box.see("end")
                box.configure(state="disabled")
        self.root.after(0, _append)

    def _startup_check(self):
        self.refresh_ports()
        threading.Thread(target=self._check_drivers_worker, daemon=True).start()

    def _check_drivers_worker(self):
        ok, text = check_ch340_driver()
        self.root.after(0, lambda: self.drv_label.configure(text=text, foreground=GREEN if ok else RED))
        self.log(f"[Sterowniki] {text}")
        if not ok:
            def _ask():
                if messagebox.askyesno("Brak sterowników CH340", "Nie wykryto sterowników Arduino Nano (CH340).\nCzy pobrać i zainstalować je teraz automatycznie?"):
                    self.on_install_drivers()
            self.root.after(150, _ask)

    def on_install_drivers(self):
        def _worker():
            ok, msg = download_and_install_ch340(self.log)
            self.log(f"[Sterowniki] {msg}")
            if ok:
                ok2, text2 = check_ch340_driver()
                self.root.after(0, lambda: self.drv_label.configure(text=text2, foreground=GREEN if ok2 else YELLOW))
                self.root.after(0, self.refresh_ports)
            else:
                self.root.after(0, lambda: messagebox.showerror("Instalacja sterowników", msg))
        threading.Thread(target=_worker, daemon=True).start()

    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        items, best_idx = [], 0
        for idx, p in enumerate(ports):
            items.append(f"{p.device} — {p.description}")
            if "1A86:7523" in (p.hwid or "").upper() or "CH340" in (p.description or "").upper():
                best_idx = idx
        self.port_combo["values"] = items
        if items:
            self.port_combo.current(best_idx)
        else:
            self.port_var.set("Brak portów COM")

    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        sel = self.port_var.get()
        if not sel or sel.startswith("Brak"):
            messagebox.showwarning("Port COM", "Nie wykryto żadnego portu COM.")
            return
        port_name = sel.split("—")[0].strip()
        try:
            self.ser = serial.Serial(port_name, BAUD_RATE, timeout=1.5)
            time.sleep(1.8)
            self.ser.reset_input_buffer()
            self.btn_conn.configure(text="Rozłącz")
            self.log(f"[COM] Połączono z {port_name} @ {BAUD_RATE} baud.")
            self.run_async(self.cmd_diag)
        except Exception as e:
            self.ser = None
            messagebox.showerror("Błąd połączenia", f"Nie można otworzyć {port_name}:\n{e}")

    def _disconnect(self):
        self.auto_refresh_var.set(False)
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.btn_conn.configure(text="Połącz")
        self.log("[COM] Rozłączono.")

    # ── Serial Execution ──────────────────────────────────────────────────────
    def run_async(self, fn):
        if self.busy:
            self.log("[!] Trwa inna operacja, poczekaj...")
            return
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Brak połączenia", "Najpierw połącz się z Arduino.")
            return
        self.busy = True
        def _wrap():
            try:
                fn()
            except Exception as e:
                self.log(f"[BŁĄD] {e}")
            finally:
                self.busy = False
        threading.Thread(target=_wrap, daemon=True).start()

    def _send_and_collect(self, cmd: str, wait_sec: float = 1.0, stop_marker: str | None = None) -> list[str]:
        with self.serial_lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\n").encode("ascii"))
            lines = []
            t0 = time.time()
            while (time.time() - t0) < wait_sec:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode("utf-8", errors="ignore").rstrip("\r\n")
                    if line:
                        lines.append(line)
                        self.log(line)
                        if stop_marker and stop_marker in line:
                            break
                else:
                    time.sleep(0.03)
            return lines

    def send_simple(self, cmd: str, wait_sec: float = 0.8):
        self.log(f">>> {cmd}")
        self._send_and_collect(cmd, wait_sec)

    def cmd_scan(self):
        self.log(">>> s")
        self._send_and_collect("s", 1.2)

    def cmd_toggle_pullup(self):
        lines = self._send_and_collect("p", 0.6)
        for ln in lines:
            if "OFF" in ln: self.pullup_state = False
            elif "ON" in ln: self.pullup_state = True
        self.root.after(0, lambda: self.btn_pullup.configure(text=f"Pull-up 5V: {'ON' if self.pullup_state else 'OFF'}"))

    def cmd_diag(self):
        lines = self._send_and_collect("d", 1.6, stop_marker="------------------------------------------")
        time.sleep(0.25)
        with self.serial_lock:
            while self.ser.in_waiting:
                ln = self.ser.readline().decode("utf-8", errors="ignore").rstrip("\r\n")
                if ln:
                    lines.append(ln)
                    self.log(ln)
        self._parse_and_update_diag(lines)

    def _parse_and_update_diag(self, lines: list[str]):
        raw = "\n".join(lines)
        if "No response from 0x0B" in raw:
            self.root.after(0, lambda: self.health_banner.configure(
                text="Brak odpowiedzi baterii na 0x0B (wybudź pakiet 8,0–8,4 V na B+/GND lub sprawdź SDA/SCL)",
                bg="#5c2121", fg="#ffffff"
            ))
            return

        def grab(pat):
            m = re.search(pat, raw)
            return m.group(1).strip() if m else None

        mfg = grab(r"Manufacturer:\s*(.+)") or "?"
        dev = grab(r"Device Name:\s*(.+)") or "?"
        sn = grab(r"DJI S/N \(0xD8\):\s*(.+)") or "?"
        chem = grab(r"Chemistry:\s*(.+)") or "?"
        fw = grab(r"FW Ver \(0002\):\s*(.+)") or "?"
        vpack = grab(r"Pack Voltage:\s*(\d+\s*mV)") or "?"
        c1 = grab(r"Cell 1 \(0x3F\):\s*(\d+)")
        c2 = grab(r"Cell 2 \(0x3E\):\s*(\d+)")
        delta = grab(r"Cell Delta:\s*(\d+)")
        temp = grab(r"Temperature:\s*([\d.\-]+\s*C)") or "?"
        curr = grab(r"Current:\s*([\-\d]+\s*mA)") or "?"
        soc_str = grab(r"RSOC / ASOC:\s*(.+)") or "?"
        cap_m = re.search(r"Capacity:\s*(\d+)\s*/\s*(\d+)\s*mAh\s*\(Design:\s*(\d+)", raw)
        cyc = grab(r"Cycle Count:\s*(\d+)") or "?"
        pf_hex = grab(r"PFStatus:\s*(0x[0-9A-Fa-f]+)") or "0x0"
        ss_hex = grab(r"SafetyStatus:\s*(0x[0-9A-Fa-f]+)") or "0x0"
        op_hex = grab(r"OperationStat:\s*(0x[0-9A-Fa-f]+)")

        def _apply():
            self.tele_vars["model"].set(f"{dev} ({mfg})")
            self.tele_vars["sn"].set(sn)
            self.tele_vars["fw"].set(f"{chem} | {fw[:17]}")
            self.tele_vars["cycles"].set(cyc)
            self.tele_vars["vpack"].set(vpack)
            self.tele_vars["current"].set(curr)
            self.tele_vars["temp"].set(temp)

            # Health / Kondycja (SOH)
            if cap_m:
                rem, fcc, des = int(cap_m.group(1)), int(cap_m.group(2)), int(cap_m.group(3))
                self.tele_vars["cap"].set(f"{rem} / {fcc} / {des} mAh")
                if des > 0 and fcc > 0:
                    soh = min(100.0, (fcc * 100.0) / des)
                    if soh >= 90:
                        grade, col = "BARDZO DOBRA KONDYCJA", GREEN
                    elif soh >= 80:
                        grade, col = "DOBRA KONDYCJA", LIME
                    elif soh >= 70:
                        grade, col = "ZŁA KONDYCJA (< 80%)", ORANGE
                    else:
                        grade, col = "BARDZO ZŁA KONDYCJA (< 70%)", RED
                    self.soh_pct_lbl.configure(text=f"{soh:.1f}%", fg=col)
                    self.soh_grade_lbl.configure(text=grade, fg=col)
                    self.soh_bar.configure(value=soh)
                    self.soh_detail_lbl.configure(text=f"Pojemność: {fcc} / {des} mAh  |  Cykle: {cyc}")
                else:
                    self.soh_pct_lbl.configure(text="0%", fg=RED)
                    self.soh_grade_lbl.configure(text="BRAK KALIBRACJI / BLOKADA FCC", fg=RED)
                    self.soh_bar.configure(value=0)
                    self.soh_detail_lbl.configure(text=f"FCC: {fcc} / {des} mAh  |  Cykle: {cyc}")

            # Cells
            if c1 and c2:
                v1, v2 = int(c1), int(c2)
                self.c1_lbl.configure(text=f"{v1} mV")
                self.c2_lbl.configure(text=f"{v2} mV")
                self.c1_bar.configure(value=max(0, min(1800, v1 - 2500)))
                self.c2_bar.configure(value=max(0, min(1800, v2 - 2500)))
            if delta:
                dv = int(delta)
                dcol = GREEN if dv <= 50 else (YELLOW if dv <= 150 else RED)
                self.delta_badge.configure(text=f"{dv} mV", bg=dcol, fg="#000000" if dv <= 150 else "#ffffff")

            # RSOC
            self.soc_lbl.configure(text=soc_str)
            rsoc_m = re.match(r"(\d+)%", soc_str)
            if rsoc_m:
                self.soc_bar.configure(value=int(rsoc_m.group(1)))

            # Badges
            if op_hex:
                op = int(op_hex, 16)
                sec = (op >> 8) & 0x03
                sec_name = {3: "SEALED (3)", 2: "UNSEALED (2)", 1: "FULL ACCESS (1)"}.get(sec, f"SEC={sec}")
                dsg = bool(op & (1 << 1))
                chg = bool(op & (1 << 2))
                fuse = bool(op & (1 << 5))
                pf_bit = bool(op & (1 << 12))
                xdsg = bool(op & (1 << 13))
                xchg = bool(op & (1 << 14))

                self.badges["sec"].configure(text=f"TRYB: {sec_name}", bg=ACCENT if sec == 1 else BG_INPUT, fg="#ffffff")
                self.badges["dsg"].configure(text=f"DSG FET: {'ON' if dsg else 'OFF'}", bg="#1b4721" if dsg else "#5c2121", fg="#ffffff")
                self.badges["chg"].configure(text=f"CHG FET: {'ON' if chg else 'OFF'}", bg="#1b4721" if chg else "#5c2121", fg="#ffffff")
                self.badges["pf"].configure(text=f"PF: {'BŁĄD' if pf_bit else f'OK ({pf_hex})'}", bg="#6e1c1c" if pf_bit else "#1b4721", fg="#ffffff")
                self.badges["xdsg"].configure(
                    text=f"XDSG={int(xdsg)} | XCHG={int(xchg)}",
                    bg="#6e3b12" if (xdsg or xchg) else "#1b4721", fg="#ffffff"
                )
                self.badges["fuse"].configure(
                    text=f"FUSE={int(fuse)} | SAF={ss_hex}",
                    bg="#6e1c1c" if (fuse or ss_hex != "0x0") else "#1b4721", fg="#ffffff"
                )

                if not pf_bit and not xdsg and not xchg and not fuse and dsg and chg:
                    self.health_banner.configure(
                        text=f"BATERIA ODBLOKOWANA I SPRAWNA — DSG=ON, CHG=ON, Cykle={cyc}",
                        bg="#1b4721", fg="#ffffff"
                    )
                else:
                    self.health_banner.configure(
                        text=f"BATERIA ZABLOKOWANA ({op_hex}) — Delta cel: {delta or '?'} mV, Cykle: {cyc}",
                        bg="#6e1c1c" if (delta and int(delta) > 200) else "#6e3b12", fg="#ffffff"
                    )

        self.root.after(0, _apply)

    def _schedule_auto(self):
        if self.auto_refresh_var.get():
            if not self.busy and self.ser and self.ser.is_open:
                self.run_async(self.cmd_diag)
            self.root.after(2200, self._schedule_auto)

    # ── BMS Service & Edit Commands ───────────────────────────────────────────
    def cmd_unseal(self) -> bool:
        self.log(">>> u (Unseal + Full Access)")
        lines = self._send_and_collect("u", 7.0)
        return any("FULL ACCESS - SUCCESS" in ln for ln in lines)

    def cmd_clear_pf2(self):
        self.log(">>> Czyszczenie DJI PF2 (0x4062 -> 01 23 45 67)")
        self._send_and_collect("w 4062 01 23 45 67", 0.7)
        self._send_and_collect("b 4060", 0.6)

    def cmd_full_unlock_and_zero(self):
        self.log("=== START PEŁNEGO ODBLOKOWANIA + 0 CYKLI ===")
        if not self.cmd_unseal():
            self.log("[!] Przerwano: brak trybu Full Access (SEC=1).")
            return
        self._send_and_collect("w 4340 00 00", 0.6)
        self._send_and_collect("w 4062 01 23 45 67", 0.6)
        self._send_and_collect("z", 0.7)
        self._send_and_collect("c", 0.6)
        self._send_and_collect("l", 0.6)
        self._send_and_collect("r", 1.5)
        self._send_and_collect("k", 0.6)
        self.cmd_diag()

    def _read_flash_bytes(self, addr_hex: str) -> list[int]:
        lines = self._send_and_collect(f"b {addr_hex}", 0.7)
        for ln in lines:
            if ln.startswith("0x") and ":" in ln:
                return [int(x, 16) for x in ln.split(":")[1].strip().split()]
        return []

    def cmd_load_editable_fields(self):
        self.log("=== Odczyt edytowalnych pól z BMS ===")
        if not self.cmd_unseal():
            return
        b_cyc = self._read_flash_bytes("4340")
        b_qmax = self._read_flash_bytes("4300")
        b_sn  = self._read_flash_bytes("494B")
        b_des = self._read_flash_bytes("4726")
        b_mfg = self._read_flash_bytes("4070")
        b_dev = self._read_flash_bytes("4085")
        b_chm = self._read_flash_bytes("409A")

        def _upd():
            if len(b_cyc) >= 2:
                self.edit_vars["cycles"].set(str(b_cyc[0] | (b_cyc[1] << 8)))
            if len(b_qmax) >= 2:
                self.edit_vars["actual_cap"].set(str(b_qmax[0] | (b_qmax[1] << 8)))
            if len(b_sn) >= 2:
                n = min(b_sn[0], len(b_sn) - 1, 14)
                self.edit_vars["dji_sn"].set(bytes(b_sn[1:1+n]).decode("ascii", errors="ignore"))
            if len(b_des) >= 6:
                self.edit_vars["design_cap"].set(str(b_des[0] | (b_des[1] << 8)))
                self.edit_vars["design_energy"].set(str(b_des[2] | (b_des[3] << 8)))
                self.edit_vars["design_volt"].set(str(b_des[4] | (b_des[5] << 8)))
            if len(b_mfg) >= 2:
                n = min(b_mfg[0], len(b_mfg) - 1)
                self.edit_vars["mfg_name"].set(bytes(b_mfg[1:1+n]).decode("ascii", errors="ignore"))
            if len(b_dev) >= 2:
                n = min(b_dev[0], len(b_dev) - 1)
                self.edit_vars["dev_name"].set(bytes(b_dev[1:1+n]).decode("ascii", errors="ignore"))
            if len(b_chm) >= 2:
                n = min(b_chm[0], len(b_chm) - 1)
                self.edit_vars["chem"].set(bytes(b_chm[1:1+n]).decode("ascii", errors="ignore"))

        self.root.after(0, _upd)

    def on_save_all_fields(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Brak połączenia", "Najpierw połącz się z Arduino.")
            return
        selected = [k for k, v in self.edit_checks.items() if v.get()]
        if not selected:
            messagebox.showinfo("Brak zaznaczenia", "Zaznacz przynajmniej jedno pole do zapisu.")
            return
        if not messagebox.askyesno("Potwierdzenie modyfikacji", f"Zapisać zaznaczone pola ({', '.join(selected)}) do pamięci Data Flash BMS?"):
            return
        seal_flag = self.seal_after_edit_var.get()
        vals = {k: self.edit_vars[k].get().strip() for k in selected}
        self.run_async(lambda: self.cmd_save_all_fields(vals, seal_flag))

    def cmd_save_all_fields(self, vals: dict[str, str], seal_flag: bool):
        if not self.cmd_unseal():
            self.log("[!] Błąd wejścia w Full Access (SEC=1).")
            return

        def write_u16(addr_hex: str, num_str: str):
            n = int(num_str)
            self._send_and_collect(f"w {addr_hex} {n & 0xFF:02X} {(n >> 8) & 0xFF:02X}", 0.7)

        def write_sbs_str(addr_hex: str, text: str, max_chars: int):
            b = text.encode("ascii", errors="ignore")[:max_chars]
            padded = [len(b)] + list(b) + [0] * (max_chars - len(b))
            hex_str = " ".join(f"{x:02X}" for x in padded)
            self._send_and_collect(f"w {addr_hex} {hex_str}", 0.7)

        if "cycles" in vals:
            write_u16("4340", vals["cycles"])
            self._send_and_collect("l", 0.6)
        if "dji_sn" in vals:
            sn_bytes = vals["dji_sn"].encode("ascii", errors="ignore")[:14]
            # 0x494B: [len] + 14 chars (space-padded)
            p_sn = [len(sn_bytes)] + list(sn_bytes) + [0x20] * (14 - len(sn_bytes))
            self._send_and_collect("w 494B " + " ".join(f"{x:02X}" for x in p_sn), 0.7)
            # 0x4994: uint32 len, 0x4998: 14 chars (zero-padded)
            p_klv_len = [len(sn_bytes), 0x00, 0x00, 0x00]
            p_klv_sn = list(sn_bytes) + [0x00] * (14 - len(sn_bytes))
            self._send_and_collect("w 4994 " + " ".join(f"{x:02X}" for x in p_klv_len), 0.7)
            self._send_and_collect("w 4998 " + " ".join(f"{x:02X}" for x in p_klv_sn), 0.7)
        if "design_cap" in vals:
            write_u16("4726", vals["design_cap"])
        if "actual_cap" in vals:
            write_u16("4300", vals["actual_cap"])
            write_u16("4302", vals["actual_cap"])
            write_u16("4304", vals["actual_cap"])
        if "design_energy" in vals:
            write_u16("4728", vals["design_energy"])
        if "design_volt" in vals:
            write_u16("472A", vals["design_volt"])
        if "mfg_name" in vals:
            write_sbs_str("4070", vals["mfg_name"], 11)
        if "dev_name" in vals:
            write_sbs_str("4085", vals["dev_name"], 11)
        if "chem" in vals:
            write_sbs_str("409A", vals["chem"], 4)

        self._send_and_collect("r", 1.5)
        if seal_flag:
            self._send_and_collect("k", 0.6)
        self.cmd_diag()

    def cmd_read_hex(self):
        addr = self.hex_addr.get().strip()
        lines = self._send_and_collect(f"b {addr}", 0.7)
        out = "\n".join(lines)
        def _show():
            self.hex_preview.configure(state="normal")
            self.hex_preview.delete("1.0", "end")
            for ln in lines:
                if ln.startswith("0x") and ":" in ln:
                    bs = [int(x, 16) for x in ln.split(":")[1].strip().split()]
                    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in bs)
                    self.hex_preview.insert("end", f"{ln}\nASCII: {asc}\n")
                else:
                    self.hex_preview.insert("end", ln + "\n")
            self.hex_preview.configure(state="disabled")
        self.root.after(0, _show)

    def on_write_hex(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Brak połączenia", "Najpierw połącz się z Arduino.")
            return
        addr = self.hex_addr.get().strip()
        data = self.hex_bytes.get().strip()
        if not messagebox.askyesno("Zapis HEX Data Flash", f"Zapisać bajty [{data}] pod 0x{addr}?\nWymaga trybu Full Access (SEC=1)."):
            return
        self.run_async(lambda: self._do_write_hex(addr, data))

    def _do_write_hex(self, addr: str, data: str):
        self._send_and_collect(f"w {addr} {data}", 0.7)
        self.cmd_read_hex()

    def on_dump_eeprom(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Brak połączenia", "Najpierw połącz się z Arduino.")
            return
        save_path = filedialog.asksaveasfilename(
            title="Zapisz kopię EEPROM (Data Flash)",
            defaultextension=".bin",
            initialfile="dji_bms_eeprom_backup.bin",
            filetypes=[("Plik binarny", "*.bin"), ("Wszystkie pliki", "*.*")],
        )
        if not save_path:
            return
        self.run_async(lambda: self.cmd_dump_eeprom(save_path))

    def cmd_dump_eeprom(self, save_path: str):
        txt_path = os.path.splitext(save_path)[0] + ".txt"
        self.root.after(0, lambda: self.dump_prog.configure(value=0))
        self.log(">>> f (Zrzut Data Flash 0x4000-0x5FFF)...")

        with self.serial_lock:
            self.ser.reset_input_buffer()
            self.ser.write(b"f\n")
            lines, raw_bytes = [], bytearray()
            t0 = time.time()
            while (time.time() - t0) < 30.0:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                lines.append(line)
                if line.startswith("0x"):
                    parts = line.split(":")[1].strip().split()
                    raw_bytes.extend(int(x, 16) for x in parts)
                    cnt = len(raw_bytes) // 16
                    self.root.after(0, lambda v=cnt: self.dump_prog.configure(value=v))
                elif "END_FLASH_DUMP" in line:
                    break

        with open(txt_path, "w", encoding="utf-8") as ft:
            ft.write("\n".join(lines) + "\n")
        with open(save_path, "wb") as fb:
            fb.write(raw_bytes)

        self.log(f"[EEPROM] Zapisano {len(raw_bytes)} bajtów do {save_path}")
        self.root.after(0, lambda: messagebox.showinfo("Kopia EEPROM gotowa", f"Zapisano {len(raw_bytes)} bajtów:\n{save_path}"))

    def on_flash_arduino(self):
        if self.busy:
            messagebox.showwarning("Zajęty", "Trwa inna operacja, poczekaj chwilę.")
            return
        sel = self.port_var.get()
        if not sel or sel.startswith("Brak"):
            messagebox.showwarning("Brak portu", "Wybierz port COM Arduino Nano na górnym pasku.")
            return
        port_name = sel.split("—")[0].strip()

        avrdude = resource_path(os.path.join("fw", "avrdude.exe"))
        conf = resource_path(os.path.join("fw", "avrdude.conf"))
        hex_file = resource_path(os.path.join("fw", "dji_battery_inspector.ino.hex"))
        for f in (avrdude, conf, hex_file):
            if not os.path.exists(f):
                messagebox.showerror("Brak pliku firmware", f"Nie znaleziono zasobu:\n{f}")
                return

        if self.ser and self.ser.is_open:
            self._disconnect()

        boot_mode = self.boot_var.get()
        if "115200 -> 57600" in boot_mode:
            bauds = [115200, 57600]
        elif "57600" in boot_mode:
            bauds = [57600]
        else:
            bauds = [115200]

        self.busy = True
        self.btn_flash.configure(state="disabled")
        self.flash_status.configure(text=f"Wgrywanie na {port_name}...", foreground=YELLOW)
        threading.Thread(
            target=self._flash_worker,
            args=(port_name, avrdude, conf, hex_file, bauds),
            daemon=True,
        ).start()

    def _flash_worker(self, port_name: str, avrdude: str, conf: str, hex_file: str, bauds: list[int]):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        success = False
        try:
            for baud in bauds:
                self.log(f"[FLASH] Próba wgrania na {port_name} @ {baud} baud...")
                cmd = [
                    avrdude,
                    "-C", conf,
                    "-v",
                    "-p", "atmega328p",
                    "-c", "arduino",
                    "-P", port_name,
                    "-b", str(baud),
                    "-D",
                    f"-Uflash:w:{hex_file}:i",
                ]
                proc = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="ignore", startupinfo=si, timeout=30
                )
                for ln in proc.stdout.splitlines():
                    if any(k in ln.lower() for k in ("writing", "reading", "verified", "done", "error", "avrdude")):
                        self.log(ln)
                if proc.returncode == 0:
                    success = True
                    break
            if success:
                self.root.after(0, lambda: self.flash_status.configure(text="Wgrano pomyślnie! Możesz kliknąć 'Połącz'.", foreground=GREEN))
                self.root.after(0, lambda: messagebox.showinfo("Sukces", f"Firmware dji_battery_inspector został wgrany na Arduino ({port_name})!"))
            else:
                self.root.after(0, lambda: self.flash_status.configure(text="Błąd wgrywania — sprawdź port/kabel/konsolę.", foreground=RED))
                self.root.after(0, lambda: messagebox.showerror("Błąd wgrywania", "Nie udało się wgrać firmware. Sprawdź log w zakładce Konsola."))
        except Exception as e:
            self.log(f"[FLASH BŁĄD] {e}")
            self.root.after(0, lambda: self.flash_status.configure(text=f"Błąd: {e}", foreground=RED))
        finally:
            self.busy = False
            self.root.after(0, lambda: self.btn_flash.configure(state="normal"))

    def cmd_raw(self):
        cmd = self.raw_cmd_entry.get().strip()
        if not cmd:
            return
        self.raw_cmd_entry.delete(0, "end")
        self.log(f">>> {cmd}")
        self._send_and_collect(cmd, 1.5)


def main():
    root = tk.Tk()
    DjiBatteryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
