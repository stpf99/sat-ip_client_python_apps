#!/usr/bin/env python3
"""
IPTV Player z pełną obsługą EPG - WERSJA ULEPSZONA
- Integracja z epg.py
- Oś czasu programów
- Widok gazety
- Przypomnienia
- Harmonogram nagrań (kompatybilny z gnome-dvb-daemon)
- Menu skanowania DVB
- Konfiguracja globalna z autozapisem
- Widok osi czasu dla aktualnie odtwarzanej stacji
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, Gst, Gio, GLib, GObject, Pango, Gdk, GdkPixbuf
import notify2
import time
import os
import sys
import subprocess
import configparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import json
import re
import math

# Inicjalizacja GStreamer
Gst.init(None)

class StringObject(GObject.Object):
    """Simple GObject to hold a string value"""
    def __init__(self, value):
        super().__init__()
        self.value = value
    
    def get_string(self):
        return self.value

class EPGEvent(GObject.Object):
    """Obiekt wydarzenia EPG"""
    def __init__(self, channel_id, event_id, title, start, stop, desc=""):
        super().__init__()
        self.channel_id = channel_id
        self.event_id = event_id
        self.title = title
        self.start = start  # datetime
        self.stop = stop    # datetime
        self.desc = desc
        self.is_reminder = False
        self.is_recording = False

class PlaylistItem(GObject.Object):
    """Obiekt kanału"""
    def __init__(self, name, uri, channel_id=None):
        super().__init__()
        self.name = name
        self.uri = uri
        self.channel_id = channel_id
        self.current_event = None
        self.next_event = None

class EPGManager:
    """Menedżer EPG - parsowanie XMLTV i zarządzanie danymi"""
    
    def __init__(self):
        self.events = {}  # channel_id -> [EPGEvent]
        self.channels = {}  # channel_id -> channel_name
        self.channel_id_to_name = {}  # service_id -> display_name z M3U
        self.name_to_channel_id = {}  # nazwa_z_m3u -> service_id
        self.reminders = []  # Lista przypomnień
        self.recordings = []  # Lista zaplanowanych nagrań
        self.last_update = None
    
    def set_channel_mapping(self, m3u_channels):
        """
        Ustawia mapowanie nazw kanałów z M3U
        m3u_channels: dict {service_id: channel_name}
        """
        self.channel_id_to_name = m3u_channels
        self.name_to_channel_id = {v: k for k, v in m3u_channels.items()}
        
        # Aktualizuj istniejące mapowanie kanałów
        for channel_id, channel_name in self.channel_id_to_name.items():
            if channel_id in self.channels:
                self.channels[channel_id] = channel_name
    
    def load_xmltv(self, xmltv_file):
        """Wczytuje plik XMLTV"""
        if not os.path.exists(xmltv_file):
            print(f"Plik EPG nie istnieje: {xmltv_file}")
            return False
        
        try:
            tree = ET.parse(xmltv_file)
            root = tree.getroot()
            
            print(f"Wczytywanie EPG z {xmltv_file}...")
            
            # Wczytaj kanały
            for channel in root.findall('channel'):
                channel_id = channel.get('id')
                display_names = channel.findall('display-name')
                if display_names:
                    # Weź pierwszą nazwę lub poszukaj po lang="pl"
                    name_from_epg = None
                    for dn in display_names:
                        if dn.get('lang') == 'pl':
                            name_from_epg = dn.text
                            break
                    if not name_from_epg:
                        name_from_epg = display_names[0].text
                    
                    # Użyj nazwy z mapowania M3U jeśli istnieje, inaczej z EPG
                    if channel_id in self.channel_id_to_name:
                        self.channels[channel_id] = self.channel_id_to_name[channel_id]
                    else:
                        self.channels[channel_id] = name_from_epg
                    print(f"  Kanał: {channel_id} -> {self.channels[channel_id]}")
            
            # Wczytaj programy
            self.events = {}
            programme_count = 0
            for programme in root.findall('programme'):
                channel_id = programme.get('channel')
                start_str = programme.get('start')
                stop_str = programme.get('stop')
                
                title_elem = programme.find('title')
                desc_elem = programme.find('desc')
                
                title = title_elem.text if title_elem is not None else "Brak tytułu"
                
                # Poprawione parsowanie opisu - szukaj wszystkich elementów desc
                desc = ""
                if desc_elem is not None:
                    desc = desc_elem.text or ""
                else:
                    # Sprawdź czy są inne elementy z opisem
                    for elem in programme:
                        if elem.tag == 'desc' and elem.text:
                            desc = elem.text
                            break
                
                # Parsuj czas (XMLTV format: YYYYMMDDHHmmss +ZZZZ)
                start = self.parse_xmltv_time(start_str)
                stop = self.parse_xmltv_time(stop_str)
                
                if start and stop:
                    event = EPGEvent(channel_id, None, title, start, stop, desc)
                    
                    if channel_id not in self.events:
                        self.events[channel_id] = []
                    self.events[channel_id].append(event)
                    programme_count += 1
            
            # Sortuj wydarzenia
            for channel_id in self.events:
                self.events[channel_id].sort(key=lambda e: e.start)
            
            self.last_update = datetime.now()
            print(f"✓ Wczytano {len(self.channels)} kanałów i {programme_count} programów")
            return True
            
        except Exception as e:
            print(f"Błąd wczytywania EPG: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def parse_xmltv_time(self, time_str):
        """Parsuje czas z formatu XMLTV"""
        try:
            # Format: 20231112180000 +0000
            if ' ' in time_str:
                time_part = time_str.split()[0]
            else:
                time_part = time_str
            return datetime.strptime(time_part, '%Y%m%d%H%M%S')
        except Exception as e:
            print(f"Błąd parsowania czasu '{time_str}': {e}")
            return None
    
    def get_current_event(self, channel_id):
        """Zwraca aktualny program dla kanału"""
        now = datetime.now()
        
        if channel_id not in self.events:
            return None
        
        for event in self.events[channel_id]:
            if event.start <= now < event.stop:
                return event
        
        return None
    
    def get_next_event(self, channel_id):
        """Zwraca następny program dla kanału"""
        now = datetime.now()
        
        if channel_id not in self.events:
            return None
        
        for event in self.events[channel_id]:
            if event.start > now:
                return event
        
        return None
    
    def get_events_for_channel(self, channel_id, start_time=None, duration_hours=24):
        """Zwraca wydarzenia dla kanału w okresie"""
        if start_time is None:
            start_time = datetime.now()
        
        end_time = start_time + timedelta(hours=duration_hours)
        
        if channel_id not in self.events:
            return []
        
        return [e for e in self.events[channel_id] 
                if e.start < end_time and e.stop > start_time]

class ConfigManager:
    """Zarządzanie konfiguracją aplikacji"""
    
    def __init__(self, config_file):
        self.config_file = config_file
        self.config = configparser.ConfigParser()
        self.default_config = {
            'general': {
                'minisatip_host': '192.168.1.1',
                'minisatip_port': '8080',
                'epg_days': '1',
                'epg_duplication_ratio': '1.0',
                'scan_detection_time': '3',
                'scan_full_time': '20',
                'last_m3u': '',
                'auto_save': 'true'
            },
            'ui': {
                'window_width': '900',
                'window_height': '700',
                'show_timeline': 'true'
            }
        }
        self.load_config()
    
    def load_config(self):
        """Wczytuje konfigurację z pliku"""
        if os.path.exists(self.config_file):
            try:
                self.config.read(self.config_file)
                print(f"Wczytano konfigurację z {self.config_file}")
            except Exception as e:
                print(f"Błąd wczytywania konfiguracji: {e}")
                self.config = configparser.ConfigParser()
        
        # Upewnij się, że wszystkie sekcje i opcje istnieją
        for section, options in self.default_config.items():
            if not self.config.has_section(section):
                self.config.add_section(section)
            for key, value in options.items():
                if not self.config.has_option(section, key):
                    self.config.set(section, key, value)
    
    def save_config(self):
        """Zapisuje konfigurację do pliku"""
        try:
            with open(self.config_file, 'w') as f:
                self.config.write(f)
            print(f"Zapisano konfigurację do {self.config_file}")
        except Exception as e:
            print(f"Błąd zapisu konfiguracji: {e}")
    
    def get(self, section, option, fallback=None):
        """Pobiera wartość z konfiguracji"""
        if not self.config.has_section(section):
            return fallback
        return self.config.get(section, option, fallback=fallback)
    
    def set(self, section, option, value):
        """Ustawia wartość w konfiguracji"""
        if not self.config.has_section(section):
            self.config.add_section(section)
        # Konwertuj na string i upewnij się, że to jest typ str
        value_str = str(value).replace('\n', ' ').replace('\r', ' ')
        # Poprawne wywołanie: set(section, option, value)
        self.config.set(section, option, value_str)
        if self.get('general', 'auto_save', 'true') == 'true':
            self.save_config()

class DVBScanDialog(Gtk.Window):
    """Dialog skanowania DVB"""
    
    def __init__(self, parent, config_manager, app_dir):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Skanowanie DVB")
        self.set_default_size(600, 500)
        
        self.config_manager = config_manager
        self.app_dir = app_dir
        self.scan_process = None
        self.scan_output = ""
        
        # Główny layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(10)
        vbox.set_margin_end(10)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)
        
        # Nagłówek
        header = Gtk.Label()
        header.set_markup("<b>Skanowanie kanałów DVB-T/T2</b>")
        vbox.append(header)
        
        # Zakładki - zapisz jako atrybut instancji
        self.notebook = Gtk.Notebook()
        vbox.append(self.notebook)
        
        # Zakładka autoskan
        auto_scan_page = self.create_auto_scan_page()
        self.notebook.append_page(auto_scan_page, Gtk.Label(label="Autoskan"))
        
        # Zakładka skan z pliku
        config_scan_page = self.create_config_scan_page()
        self.notebook.append_page(config_scan_page, Gtk.Label(label="Skan z pliku"))
        
        # Panel wyników
        results_frame = Gtk.Frame(label="Wyniki skanowania")
        vbox.append(results_frame)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(150)
        scrolled.set_vexpand(True)
        
        self.results_text = Gtk.TextView()
        self.results_text.set_editable(False)
        self.results_text.set_monospace(True)
        scrolled.set_child(self.results_text)
        results_frame.set_child(scrolled)
        
        # Przyciski
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        button_box.set_halign(Gtk.Align.END)
        
        self.scan_btn = Gtk.Button(label="Rozpocznij skanowanie")
        self.scan_btn.connect("clicked", self.on_scan_clicked)
        button_box.append(self.scan_btn)
        
        self.export_btn = Gtk.Button(label="Eksportuj M3U")
        self.export_btn.set_sensitive(False)
        self.export_btn.connect("clicked", self.on_export_clicked)
        button_box.append(self.export_btn)
        
        self.close_btn = Gtk.Button(label="Zamknij")
        self.close_btn.connect("clicked", lambda b: self.close())
        button_box.append(self.close_btn)
        
        vbox.append(button_box)
        
        self.set_child(vbox)
    
    def create_auto_scan_page(self):
        """Tworzy stronę autoskanu"""
        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        
        # Opcje skanowania
        row = 0
        
        # Zakres
        range_label = Gtk.Label(label="Zakres:")
        range_label.set_halign(Gtk.Align.START)
        grid.attach(range_label, 0, row, 1, 1)
        
        range_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.vhf_check = Gtk.CheckButton(label="VHF")
        self.vhf_check.set_active(True)
        self.uhf_check = Gtk.CheckButton(label="UHF")
        self.uhf_check.set_active(True)
        range_box.append(self.vhf_check)
        range_box.append(self.uhf_check)
        grid.attach(range_box, 1, row, 1, 1)
        
        row += 1
        
        # System
        system_label = Gtk.Label(label="System:")
        system_label.set_halign(Gtk.Align.START)
        grid.attach(system_label, 0, row, 1, 1)
        
        # Użyj ComboBoxText zamiast StringList
        self.system_combo = Gtk.ComboBoxText()
        self.system_combo.append_text("DVB-T2")
        self.system_combo.append_text("DVB-T")
        self.system_combo.append_text("Oba")
        self.system_combo.set_active(0)
        grid.attach(self.system_combo, 1, row, 1, 1)
        
        row += 1
        
        # Czas detekcji
        det_label = Gtk.Label(label="Czas detekcji (s):")
        det_label.set_halign(Gtk.Align.START)
        grid.attach(det_label, 0, row, 1, 1)
        
        self.det_spin = Gtk.SpinButton()
        self.det_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'scan_detection_time', '3')),
            lower=1, upper=10, step_increment=1))
        grid.attach(self.det_spin, 1, row, 1, 1)
        
        row += 1
        
        # Czas pełnego skanu
        full_label = Gtk.Label(label="Czas skanu (s):")
        full_label.set_halign(Gtk.Align.START)
        grid.attach(full_label, 0, row, 1, 1)
        
        self.full_spin = Gtk.SpinButton()
        self.full_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'scan_full_time', '20')),
            lower=10, upper=60, step_increment=5))
        grid.attach(self.full_spin, 1, row, 1, 1)
        
        return grid
    
    def create_config_scan_page(self):
        """Tworzy stronę skanowania z pliku konfiguracyjnego"""
        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        
        row = 0
        
        # Plik konfiguracyjny
        file_label = Gtk.Label(label="Plik konfiguracyjny:")
        file_label.set_halign(Gtk.Align.START)
        grid.attach(file_label, 0, row, 1, 1)
        
        file_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.file_entry = Gtk.Entry()
        self.file_entry.set_hexpand(True)
        file_box.append(self.file_entry)
        
        self.file_btn = Gtk.Button(label="Przeglądaj...")
        self.file_btn.connect("clicked", self.on_file_browse_clicked)
        file_box.append(self.file_btn)
        
        grid.attach(file_box, 1, row, 1, 1)
        
        row += 1
        
        # Czas detekcji
        det_label = Gtk.Label(label="Czas detekcji (s):")
        det_label.set_halign(Gtk.Align.START)
        grid.attach(det_label, 0, row, 1, 1)
        
        self.config_det_spin = Gtk.SpinButton()
        self.config_det_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'scan_detection_time', '3')),
            lower=1, upper=10, step_increment=1))
        grid.attach(self.config_det_spin, 1, row, 1, 1)
        
        row += 1
        
        # Czas pełnego skanu
        full_label = Gtk.Label(label="Czas skanu (s):")
        full_label.set_halign(Gtk.Align.START)
        grid.attach(full_label, 0, row, 1, 1)
        
        self.config_full_spin = Gtk.SpinButton()
        self.config_full_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'scan_full_time', '20')),
            lower=10, upper=60, step_increment=5))
        grid.attach(self.config_full_spin, 1, row, 1, 1)
        
        return grid
    
    def on_file_browse_clicked(self, button):
        """Wybór pliku konfiguracyjnego"""
        dialog = Gtk.FileChooserNative(
            title="Wybierz plik konfiguracyjny DVB",
            action=Gtk.FileChooserAction.OPEN,
            transient_for=self
        )
        
        filter_dvb = Gtk.FileFilter()
        filter_dvb.set_name("Pliki konfiguracyjne DVB")
        filter_dvb.add_pattern("*")
        dialog.add_filter(filter_dvb)
        
        def on_response(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                self.file_entry.set_text(dialog.get_file().get_path())
            dialog.destroy()
        
        dialog.connect("response", on_response)
        dialog.show()
    
    def on_scan_clicked(self, button):
        """Rozpoczęcie skanowania"""
        if self.scan_process and self.scan_process.poll() is None:
            # Skanowanie w toku - przerwij
            self.scan_process.terminate()
            self.scan_btn.set_label("Rozpocznij skanowanie")
            return
        
        # Zapisz ustawienia z jawnej konwersjęna string
        det_time = str(self.det_spin.get_value_as_int())
        full_time = str(self.full_spin.get_value_as_int())
        
        self.config_manager.set('general', 'scan_detection_time', det_time)
        self.config_manager.set('general', 'scan_full_time', full_time)
        
        # Wyczyść wyniki
        buffer = self.results_text.get_buffer()
        buffer.set_text("")
        
        # Przygotuj komendę - użyj self.notebook zamiast self.get_parent()
        current_page = self.notebook.get_current_page()
        
        if current_page == 0:  # Autoskan
            cmd = self.prepare_auto_scan_cmd()
        else:  # Skan z pliku
            cmd = self.prepare_config_scan_cmd()
        
        if not cmd:
            return
        
        # Uruchom skanowanie
        self.append_results(f"Uruchamianie: {' '.join(cmd)}\n")
        self.scan_btn.set_label("Przerwij")
        self.export_btn.set_sensitive(False)
        
        try:
            self.scan_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Czytaj output w tle
            GLib.io_add_watch(self.scan_process.stdout, GLib.IO_IN, self.on_scan_output)
            
        except Exception as e:
            self.append_results(f"Błąd uruchamiania: {e}\n")
            self.scan_btn.set_label("Rozpocznij skanowanie")
    
    def prepare_auto_scan_cmd(self):
        """Przygotowuje komendę dla autoskanu"""
        cmd = [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "dvb_scanner.py"),
            "--auto",
            "--host", self.config_manager.get('general', 'minisatip_host', '192.168.1.1'),
            "--port", self.config_manager.get('general', 'minisatip_port', '8080'),
            "--detect-time", str(self.det_spin.get_value_as_int()),
            "--scan-time", str(self.full_spin.get_value_as_int())
        ]
        
        # Zakres
        if self.vhf_check.get_active():
            cmd.append("--vhf")
        if self.uhf_check.get_active():
            cmd.append("--uhf")
        
        # System
        active = self.system_combo.get_active()
        if active == 0:  # DVB-T2
            cmd.extend(["--system", "dvbt2"])
        elif active == 1:  # DVB-T
            cmd.extend(["--system", "dvbt"])
        else:  # Oba
            cmd.extend(["--system", "both"])
        
        # Plik wyjściowy
        output_file = os.path.join(self.app_dir, f"scan_{int(time.time())}.m3u")
        cmd.extend(["-o", output_file])
        self.last_output_file = output_file
        
        return cmd
    
    def prepare_config_scan_cmd(self):
        """Przygotowuje komendę dla skanowania z pliku"""
        config_file = self.file_entry.get_text()
        if not config_file or not os.path.exists(config_file):
            self.append_results("Nie wybrano pliku konfiguracyjnego lub plik nie istnieje\n")
            return None
        
        cmd = [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "dvb_scanner.py"),
            "--config", config_file,
            "-H", self.config_manager.get('general', 'minisatip_host', '192.168.1.1'),
            "-p", self.config_manager.get('general', 'minisatip_port', '8080'),
            "--detect-time", str(self.config_det_spin.get_value_as_int()),
            "--scan-time", str(self.config_full_spin.get_value_as_int())
        ]
        
        # Plik wyjściowy
        output_file = os.path.join(self.app_dir, f"scan_{int(time.time())}.m3u")
        cmd.extend(["-o", output_file])
        self.last_output_file = output_file
        
        return cmd
    
    def on_scan_output(self, source, condition):
        """Czyta output z procesu skanowania"""
        if condition == GLib.IO_IN:
            line = source.readline()
            if line:
                self.append_results(line.rstrip())
                return True
            else:
                # Proces zakończony
                self.scan_process.wait()
                if self.scan_process.returncode == 0:
                    self.append_results("\n✓ Skanowanie zakończone pomyślnie\n")
                    self.export_btn.set_sensitive(True)
                else:
                    self.append_results(f"\n✗ Błąd: kod wyjścia {self.scan_process.returncode}\n")
                
                self.scan_btn.set_label("Rozpocznij skanowanie")
                return False
        return False
    
    def append_results(self, text):
        """Dodaje tekst do wyników"""
        buffer = self.results_text.get_buffer()
        end_iter = buffer.get_end_iter()
        buffer.insert(end_iter, text + "\n")
        
        # Przewiń na dół
        self.results_text.scroll_to_iter(end_iter, 0.0, False, 0.0, 0.0)
    
    def on_export_clicked(self, button):
        """Eksport wyników"""
        if not hasattr(self, 'last_output_file') or not os.path.exists(self.last_output_file):
            self.append_results("Brak pliku wynikowego do eksportu\n")
            return
        
        dialog = Gtk.FileChooserNative(
            title="Zapisz plik M3U",
            action=Gtk.FileChooserAction.SAVE,
            transient_for=self
        )
        
        dialog.set_current_name("channels.m3u")
        
        filter_m3u = Gtk.FileFilter()
        filter_m3u.set_name("Pliki M3U")
        filter_m3u.add_pattern("*.m3u")
        dialog.add_filter(filter_m3u)
        
        def on_response(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                dest_path = dialog.get_file().get_path()
                try:
                    import shutil
                    shutil.copy2(self.last_output_file, dest_path)
                    self.append_results(f"✓ Zapisano do: {dest_path}\n")
                except Exception as e:
                    self.append_results(f"Błąd zapisu: {e}\n")
            dialog.destroy()
        
        dialog.connect("response", on_response)
        dialog.show()

class EPGUpdateDialog(Gtk.Window):
    """Dialog aktualizacji EPG - uruchamia epg.py"""
    
    def __init__(self, parent, m3u_file, app_dir, epg_manager, config_manager):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Aktualizacja EPG")
        self.set_default_size(700, 500)
        
        self.m3u_file = m3u_file
        self.app_dir = app_dir
        self.epg_manager = epg_manager
        self.config_manager = config_manager
        self.process = None
        
        # Główny layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(10)
        vbox.set_margin_end(10)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)
        
        # Informacja
        label = Gtk.Label()
        label.set_markup("<b>Aktualizacja danych EPG</b>\n\nZbieranie programów z minisatip...")
        vbox.append(label)
        
        # Opcje
        options_grid = Gtk.Grid()
        options_grid.set_row_spacing(10)
        options_grid.set_column_spacing(10)
        vbox.append(options_grid)
        
        # Liczba dni
        days_label = Gtk.Label(label="Liczba dni EPG:")
        days_label.set_halign(Gtk.Align.START)
        options_grid.attach(days_label, 0, 0, 1, 1)
        
        self.days_spin = Gtk.SpinButton()
        self.days_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'epg_days', '1')),
            lower=1, upper=7, step_increment=1))
        self.days_spin.connect("value-changed", self.on_days_changed)
        options_grid.attach(self.days_spin, 1, 0, 1, 1)
        
        # Próg duplikacji
        dup_label = Gtk.Label(label="Próg zmiany muxa:")
        dup_label.set_halign(Gtk.Align.START)
        options_grid.attach(dup_label, 0, 1, 1, 1)
        
        self.dup_spin = Gtk.SpinButton()
        self.dup_spin.set_adjustment(Gtk.Adjustment(
            value=float(self.config_manager.get('general', 'epg_duplication_ratio', '1.0')),
            lower=0.5, upper=5.0, step_increment=0.5, page_increment=1.0))
        self.dup_spin.set_digits(1)
        options_grid.attach(self.dup_spin, 1, 1, 1, 1)
        
        # Progress
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("Uruchamianie...")
        vbox.append(self.progress)
        
        # Log
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        scrolled.set_child(self.log_view)
        vbox.append(scrolled)
        
        # Przyciski
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        button_box.set_halign(Gtk.Align.END)
        
        self.cancel_btn = Gtk.Button(label="Anuluj")
        self.cancel_btn.connect("clicked", self.on_cancel_clicked)
        button_box.append(self.cancel_btn)
        
        self.close_btn = Gtk.Button(label="Zamknij")
        self.close_btn.set_sensitive(False)
        self.close_btn.connect("clicked", lambda b: self.close())
        button_box.append(self.close_btn)
        
        vbox.append(button_box)
        
        self.set_child(vbox)
        
        # Uruchom aktualizację
        GLib.timeout_add(500, self.start_epg_update)
    
    def on_days_changed(self, spin_button):
        """Zmieniono liczbę dni"""
        self.config_manager.set('general', 'epg_days', str(spin_button.get_value_as_int()))
    
    def on_cancel_clicked(self, button):
        """Anuluje aktualizację"""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.append_log("❌ Anulowano\n")
        self.close()
    
    def start_epg_update(self):
        """Uruchamia proces epg.py"""
        self.append_log("Uruchamianie epg.py...\n")
        self.progress.set_fraction(0.1)
        self.progress.set_text("Usuwanie starego EPG...")
        
        # Zapisz ustawienia
        self.config_manager.set('general', 'epg_duplication_ratio', str(self.dup_spin.get_value()))
        
        # Usuń stary plik EPG
        epg_file = os.path.join(self.app_dir, "epg.xml")
        if os.path.exists(epg_file):
            os.remove(epg_file)
            self.append_log(f"✓ Usunięto stary plik EPG\n")
        
        # Przygotuj komendę
        cmd = [
            sys.executable,  # Użyj tego samego Pythona
            os.path.join(os.path.dirname(__file__), "epg.py"),
            "-m", self.m3u_file,
            "-o", epg_file,
            "--host", self.config_manager.get('general', 'minisatip_host', '192.168.1.1'),
            "--port", self.config_manager.get('general', 'minisatip_port', '8080')
        ]
        
        # Dodaj dodatkowe parametry
        days = self.days_spin.get_value_as_int()
        if days > 1:
            cmd.extend(["--days", str(days)])
        
        # Dodaj próg duplikacji
        dup_ratio = self.dup_spin.get_value()
        if dup_ratio != 1.0:
            cmd.extend(["--duplication-ratio", str(dup_ratio)])
        
        self.append_log(f"Komenda: {' '.join(cmd)}\n")
        self.progress.set_fraction(0.3)
        self.progress.set_text("Uruchamianie procesu...")
        
        # Uruchom proces
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Czytaj output w tle
            GLib.io_add_watch(self.process.stdout, GLib.IO_IN, self.on_process_output)
            
        except Exception as e:
            self.append_log(f"❌ Błąd uruchamiania: {e}\n")
            self.finish_update()
        
        return False
    
    def on_process_output(self, source, condition):
        """Czyta output z procesu"""
        if condition == GLib.IO_IN:
            line = source.readline()
            if line:
                self.append_log(line.rstrip())
                # Aktualizuj progress
                if "Zbieramy EPG" in line:
                    self.progress.set_fraction(0.5)
                    self.progress.set_text("Zbieranie danych EPG...")
                elif "✓ Skanowanie zakończone" in line:
                    self.progress.set_fraction(0.8)
                    self.progress.set_text("Zapisywanie EPG...")
                return True
            else:
                # Proces zakończony
                self.process.wait()
                if self.process.returncode == 0:
                    self.append_log("\n✓ Aktualizacja zakończona pomyślnie\n")
                    # Wczytaj nowe EPG
                    epg_file = os.path.join(self.app_dir, "epg.xml")
                    if self.epg_manager.load_xmltv(epg_file):
                        self.append_log("✓ Wczytano nowe EPG\n")
                else:
                    self.append_log(f"\n❌ Błąd: kod wyjścia {self.process.returncode}\n")
                self.finish_update()
                return False
        return False
    
    def finish_update(self):
        """Kończy aktualizację"""
        self.progress.set_fraction(1.0)
        self.progress.set_text("Gotowe!")
        self.cancel_btn.set_sensitive(False)
        self.close_btn.set_sensitive(True)
        return False
    
    def append_log(self, text):
        """Dodaje tekst do logu"""
        buffer = self.log_view.get_buffer()
        end_iter = buffer.get_end_iter()
        buffer.insert(end_iter, text + "\n")
        
        # Przewiń na dół
        self.log_view.scroll_to_iter(end_iter, 0.0, False, 0.0, 0.0)

class TimelineView(Gtk.Box):
    """Widok osi czasu dla aktualnie odtwarzanej stacji"""
    
    def __init__(self, epg_manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.epg_manager = epg_manager
        self.current_channel_id = None
        self.events = []
        
        # Nagłówek
        self.header_label = Gtk.Label()
        self.header_label.set_markup("<b>Programy na dziś</b>")
        self.header_label.set_halign(Gtk.Align.START)
        self.append(self.header_label)
        
        # Widok osi czasu
        self.timeline_drawing = Gtk.DrawingArea()
        self.timeline_drawing.set_vexpand(True)
        self.timeline_drawing.set_draw_func(self.on_timeline_draw)
        self.timeline_drawing.connect("query-tooltip", self.on_timeline_query_tooltip)
        self.timeline_drawing.set_has_tooltip(True)
        self.append(self.timeline_drawing)
        
        # Legenda
        legend_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        legend_box.set_halign(Gtk.Align.CENTER)
        
        past_label = Gtk.Label()
        past_label.set_markup("<span background='#cccccc'> Minione </span>")
        legend_box.append(past_label)
        
        current_label = Gtk.Label()
        current_label.set_markup("<span background='#4a90e2'> Aktualny </span>")
        legend_box.append(current_label)
        
        future_label = Gtk.Label()
        future_label.set_markup("<span background='#e0e0e0'> Nadchodzące </span>")
        legend_box.append(future_label)
        
        self.append(legend_box)
        
        # Timer do odświeżania
        GLib.timeout_add_seconds(30, self.update_timeline)
    
    def set_channel(self, channel_id):
        """Ustawia kanał do wyświetlenia"""
        self.current_channel_id = channel_id
        self.update_timeline()
    
    def update_timeline(self):
        """Aktualizuje dane osi czasu"""
        if not self.current_channel_id:
            return True
        
        # Pobierz wydarzenia na dziś
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.events = self.epg_manager.get_events_for_channel(
            self.current_channel_id, 
            start_of_day, 
            24
        )
        
        # Aktualizuj nagłówek
        channel_name = self.epg_manager.channels.get(self.current_channel_id, "Nieznany kanał")
        self.header_label.set_markup(f"<b>Programy na dziś - {channel_name}</b>")
        
        # Odśwież widok
        self.timeline_drawing.queue_draw()
        
        return True
    
    def on_timeline_draw(self, drawing_area, cr, width, height):
        """Rysuje oś czasu"""
        if not self.events:
            return
        
        # Ustawienia
        margin = 20
        timeline_y = height / 2
        timeline_height = 40
        hour_width = (width - 2 * margin) / 24  # 24 godziny
        
        # Rysuj oś czasu
        cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.set_line_width(1)
        cr.move_to(margin, timeline_y)
        cr.line_to(width - margin, timeline_y)
        cr.stroke()
        
        # Rysuj podziałki godzinowe
        for hour in range(25):
            x = margin + hour * hour_width
            cr.move_to(x, timeline_y - 5)
            cr.line_to(x, timeline_y + 5)
            cr.stroke()
            
            # Etykiety godzin
            if hour < 24:
                cr.set_source_rgb(0, 0, 0)
                cr.set_font_size(10)
                text_x = x + hour_width / 2
                cr.move_to(text_x - 10, timeline_y + 20)
                cr.show_text(f"{hour:02d}:00")
        
        # Rysuj wydarzenia
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        for event in self.events:
            # Oblicz pozycję i szerokość
            start_offset = (event.start - start_of_day).total_seconds() / 3600  # w godzinach
            end_offset = (event.stop - start_of_day).total_seconds() / 3600  # w godzinach
            
            x = margin + start_offset * hour_width
            w = (end_offset - start_offset) * hour_width
            
            # Ustaw kolor w zależności od statusu
            if event.stop <= now:
                # Minione
                cr.set_source_rgb(0.8, 0.8, 0.8)
            elif event.start <= now < event.stop:
                # Aktualne
                cr.set_source_rgb(0.29, 0.56, 0.89)
            else:
                # Nadchodzące
                cr.set_source_rgb(0.88, 0.88, 0.88)
            
            # Rysuj prostokąt wydarzenia
            cr.rectangle(x, timeline_y - timeline_height/2, w, timeline_height)
            cr.fill_preserve()
            cr.set_source_rgb(0.5, 0.5, 0.5)
            cr.stroke()
            
            # Dodaj tytuł jeśli jest miejsce
            if w > 30:
                cr.set_source_rgb(0, 0, 0)
                cr.set_font_size(9)
                text_width = w - 4
                title = event.title
                
                # Skróć tytuł jeśli jest za długi
                while title and cr.text_extents(title)[2] > text_width:
                    title = title[:-1]
                
                if title:
                    cr.move_to(x + 2, timeline_y + 3)
                    cr.show_text(title)
        
        # Rysuj wskaźnik aktualnego czasu
        now_offset = (now - start_of_day).total_seconds() / 3600  # w godzinach
        now_x = margin + now_offset * hour_width
        
        cr.set_source_rgb(1, 0, 0)
        cr.set_line_width(2)
        cr.move_to(now_x, timeline_y - timeline_height)
        cr.line_to(now_x, timeline_y + timeline_height)
        cr.stroke()
    
    def on_timeline_query_tooltip(self, widget, x, y, keyboard_tooltip, tooltip):
        """Obsługa podpowiedzi na osi czasu"""
        if not self.events:
            return False
        
        # Ustawienia
        margin = 20
        height = widget.get_allocated_height()
        timeline_y = height / 2
        hour_width = (widget.get_allocated_width() - 2 * margin) / 24  # 24 godziny
        
        # Sprawdź czy kursor jest na osi czasu
        if abs(y - timeline_y) > 20:
            return False
        
        # Oblicz godzinę dla pozycji x
        hour = (x - margin) / hour_width
        if hour < 0 or hour > 24:
            return False
        
        # Znajdź wydarzenie dla tej godziny
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        for event in self.events:
            start_offset = (event.start - start_of_day).total_seconds() / 3600  # w godzinach
            end_offset = (event.stop - start_of_day).total_seconds() / 3600  # w godzinach
            
            if start_offset <= hour <= end_offset:
                # Znaleziono wydarzenie - pokaż szczegóły
                time_str = f"{event.start.strftime('%H:%M')} - {event.stop.strftime('%H:%M')}"
                desc = event.desc[:100] + "..." if len(event.desc) > 100 else event.desc
                
                tooltip.set_markup(f"<b>{event.title}</b>\n{time_str}\n\n{desc}")
                return True
        
        return False

class IPTVPlayer(Gtk.Application):
    """Główna aplikacja IPTV Player z EPG - WERSJA ULEPSZONA"""
    
    def __init__(self):
        super().__init__(application_id="com.example.IPTVPlayerEPG")
        self.app_dir = os.path.expanduser("~/.config/iptv_player")
        os.makedirs(self.app_dir, exist_ok=True)
        
        # Konfiguracja
        self.config_file = os.path.join(self.app_dir, "config.ini")
        self.config_manager = ConfigManager(self.config_file)
        
        self.window = None
        self.player = None
        self.stream_start_time = None
        self.current_channel = ""
        self.current_selection = Gtk.INVALID_LIST_POSITION
        self.file_chooser_dialog = None
        
        # EPG Manager
        self.epg_manager = EPGManager()
        self.epg_file = os.path.join(self.app_dir, "epg.xml")
        self.m3u_file = self.config_manager.get('general', 'last_m3u', '') or os.path.join(self.app_dir, "playlist_with_tvgid.m3u")
        self.epg_mapping_file = os.path.join(self.app_dir, "epg_mapping.json")
        
        # Przypomnienia
        self.reminder_timer = None
    
    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("IPTV Player z EPG")
        
        # Ustaw rozmiar okna z konfiguracji
        width = int(self.config_manager.get('ui', 'window_width', '900'))
        height = int(self.config_manager.get('ui', 'window_height', '700'))
        self.window.set_default_size(width, height)
        
        # Inicjalizacja widgetów
        self.create_widgets()
        
        # GStreamer
        self.player = Gst.ElementFactory.make("playbin", "player")
        
        self.sink = Gst.ElementFactory.make("gtk4paintablesink", "sink")
        if self.sink is not None:
            self.player.set_property("video-sink", self.sink)
            paintable = self.sink.get_property("paintable")
            self.video_picture = Gtk.Picture()
            self.video_picture.set_paintable(paintable)
            self.video_picture.set_size_request(640, 360)  # 16:9 aspect ratio
        else:
            print("Ostrzeżenie: gtk4paintablesink niedostępny")
            self.video_label = Gtk.Label(label="Video w osobnym oknie\n(Zainstaluj gstreamer1.0-plugins-bad)")
            self.video_label.set_size_request(640, 360)
        
        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)
        
        # Główny layout
        self.create_main_layout()
        
        notify2.init("IPTV Player")
        self.app_start_time = time.time()
        
        # Załaduj domyślną playlistę i mapowanie przy starcie
        self.load_default_playlist()
        
        # Wczytaj EPG jeśli istnieje
        if os.path.exists(self.epg_file):
            if self.epg_manager.load_xmltv(self.epg_file):
                print(f"✓ Wczytano EPG z {self.epg_file}")
                self.update_current_program_info()
        
        # Timer dla aktualizacji EPG i przypomnień
        GLib.timeout_add_seconds(30, self.update_timer_callback)
        
        # Zapisz stan przy zamykaniu
        self.window.connect("close-request", self.on_window_close)
        
        self.window.present()
    
    def create_widgets(self):
        """Tworzy widgety aplikacji"""
        # Przyciski
        self.play_button = Gtk.Button(label="▶ Odtwarzaj")
        self.stop_button = Gtk.Button(label="⏹ Zatrzymaj")
        self.mute_button = Gtk.ToggleButton(label="🔇 Wycisz")
        self.filechooser_button = Gtk.Button(label="📁 Wybierz playlistę")
        self.epg_button = Gtk.Button(label="📺 Program TV (EPG)")
        self.update_epg_button = Gtk.Button(label="🔄 Aktualizuj EPG")
        self.scan_button = Gtk.Button(label="📡 Skanuj DVB")
        self.settings_button = Gtk.Button(label="⚙ Ustawienia")
        self.close_button = Gtk.Button(label="✖ Zamknij")
        
        # Playlist dropdown
        self.playlist_store = Gio.ListStore(item_type=PlaylistItem)
        self.playlist_dropdown = Gtk.DropDown(model=self.playlist_store)
        self.playlist_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.setup_dropdown_item)
        factory.connect("bind", self.bind_dropdown_item)
        self.playlist_dropdown.set_factory(factory)
        
        # Etykiety statusu
        self.status_label = Gtk.Label(label="Gotowy")
        self.status_label.set_halign(Gtk.Align.START)
        
        self.current_program_label = Gtk.Label(label="")
        self.current_program_label.set_halign(Gtk.Align.START)
        
        # Połącz sygnały
        self.play_button.connect("clicked", self.on_play_button_clicked)
        self.stop_button.connect("clicked", self.on_stop_button_clicked)
        self.mute_button.connect("clicked", self.on_mute_button_clicked)
        self.filechooser_button.connect("clicked", self.on_filechooser_button_clicked)
        self.epg_button.connect("clicked", self.on_epg_button_clicked)
        self.update_epg_button.connect("clicked", self.on_update_epg_clicked)
        self.scan_button.connect("clicked", self.on_scan_button_clicked)
        self.settings_button.connect("clicked", self.on_settings_button_clicked)
        self.close_button.connect("clicked", self.on_close_button_clicked)
        self.playlist_dropdown.connect("notify::selected", self.on_channel_changed)
    
    def create_main_layout(self):
        """Tworzy główny layout aplikacji"""
        # Główny kontener
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        
        # Pasek narzędzi
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        toolbar.set_margin_start(5)
        toolbar.set_margin_end(5)
        toolbar.set_margin_top(5)
        toolbar.set_margin_bottom(5)
        
        # Dodaj przyciski do paska narzędzi
        toolbar.append(self.filechooser_button)
        toolbar.append(self.playlist_dropdown)
        toolbar.append(self.epg_button)
        toolbar.append(self.update_epg_button)
        toolbar.append(self.scan_button)
        toolbar.append(self.settings_button)
        
        # Kontener wideo
        video_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        video_container.set_margin_start(10)
        video_container.set_margin_end(10)
        video_container.set_margin_top(10)
        
        # Wideo lub placeholder
        if hasattr(self, 'video_picture'):
            video_container.append(self.video_picture)
        else:
            video_container.append(self.video_label)
        
        # Placeholder tekstowy gdy nic nie jest odtwarzane
        self.no_video_label = Gtk.Label(label="Nic nie odtwarza się w tej chwili")
        self.no_video_label.set_halign(Gtk.Align.CENTER)
        self.no_video_label.set_valign(Gtk.Align.CENTER)
        self.no_video_label.add_css_class("dim-label")
        
        # Overlay dla wideo i placeholdera
        video_overlay = Gtk.Overlay()
        if hasattr(self, 'video_picture'):
            video_overlay.set_child(self.video_picture)
        else:
            video_overlay.set_child(self.video_label)
        video_overlay.add_overlay(self.no_video_label)
        video_container.append(video_overlay)
        
        # Panel sterowania
        control_panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        control_panel.set_margin_start(10)
        control_panel.set_margin_end(10)
        control_panel.set_margin_top(5)
        control_panel.set_margin_bottom(5)
        control_panel.set_halign(Gtk.Align.CENTER)
        
        control_panel.append(self.play_button)
        control_panel.append(self.stop_button)
        control_panel.append(self.mute_button)
        
        # Panel informacji o programie
        info_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        info_panel.set_margin_start(10)
        info_panel.set_margin_end(10)
        info_panel.set_margin_top(5)
        info_panel.set_margin_bottom(5)
        
        info_panel.append(self.current_program_label)
        info_panel.append(self.status_label)
        
        # Oś czasu dla aktualnego kanału
        self.timeline_view = TimelineView(self.epg_manager)
        self.timeline_view.set_visible(self.config_manager.get('ui', 'show_timeline', 'true') == 'true')
        
        # Skumuluj wszystko
        main_box.append(toolbar)
        main_box.append(video_container)
        main_box.append(control_panel)
        main_box.append(info_panel)
        main_box.append(self.timeline_view)
        
        self.window.set_child(main_box)
    
    def setup_dropdown_item(self, factory, list_item):
        label = Gtk.Label()
        list_item.set_child(label)
    
    def bind_dropdown_item(self, factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        
        # Pokaż nazwę + info o aktualnym programie
        display_text = item.name
        if item.current_event:
            display_text += f" • {item.current_event.title[:20]}..."
        
        label.set_label(display_text)
    
    def on_filechooser_button_clicked(self, widget):
        self.file_chooser_dialog = Gtk.FileChooserNative(
            title="Wybierz playlistę",
            action=Gtk.FileChooserAction.OPEN,
            transient_for=self.window
        )
        
        m3u_filter = Gtk.FileFilter()
        m3u_filter.set_name("Pliki M3U")
        m3u_filter.add_pattern("*.m3u")
        m3u_filter.add_pattern("*.m3u8")
        
        all_filter = Gtk.FileFilter()
        all_filter.set_name("Wszystkie pliki")
        all_filter.add_pattern("*")
        
        self.file_chooser_dialog.add_filter(m3u_filter)
        self.file_chooser_dialog.add_filter(all_filter)
        self.file_chooser_dialog.set_filter(m3u_filter)
        
        def on_response(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                playlist_path = dialog.get_file().get_path()
                if playlist_path:
                    self.m3u_file = playlist_path
                    self.config_manager.set('general', 'last_m3u', playlist_path)
                    self.load_playlist(playlist_path)
            self.file_chooser_dialog = None
        
        self.file_chooser_dialog.connect("response", on_response)
        self.file_chooser_dialog.show()
    
    def load_playlist(self, playlist_path):
        """Wczytuje playlistę M3U"""
        self.playlist_store.remove_all()
        
        if os.path.exists(playlist_path):
            with open(playlist_path, "r") as f:
                lines = f.readlines()
                
                for i, line in enumerate(lines):
                    if line.startswith("#EXTINF"):
                        channel_name = line.split(",", 1)[-1].strip()
                        
                        # Wyciągnij tvg-id jeśli jest
                        channel_id = None
                        tvg_match = re.search(r'tvg-id="([^"]+)"', line)
                        if tvg_match:
                            channel_id = tvg_match.group(1)
                        
                        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
                        if next_line.startswith(("http", "rtsp")):
                            item = PlaylistItem(channel_name, next_line, channel_id)
                            self.playlist_store.append(item)
        
        # Ustaw mapowanie kanałów dla EPG
        self.setup_epg_channel_mapping()
        
        # Aktualizuj info o programach
        self.update_playlist_epg_info()
    
    def setup_epg_channel_mapping(self):
        """Ustawia mapowanie kanałów z playlisty dla EPG"""
        # Najpierw spróbuj wczytać z pliku JSON
        if not self.load_epg_mapping():
            # Jeśli nie ma pliku, utwórz mapowanie z playlisty
            channel_mapping = {}
            n_items = self.playlist_store.get_n_items()
            
            for i in range(n_items):
                item = self.playlist_store.get_item(i)
                if item.channel_id:
                    channel_mapping[item.channel_id] = item.name
            
            if channel_mapping:
                self.epg_manager.set_channel_mapping(channel_mapping)
                print(f"Ustawiono mapowanie dla {len(channel_mapping)} kanałów z playlisty")
                
                # Zapisz mapowanie do pliku
                try:
                    with open(self.epg_mapping_file, 'w') as f:
                        json.dump(channel_mapping, f, indent=2, ensure_ascii=False)
                    print(f"Zapisano mapowanie do {self.epg_mapping_file}")
                except Exception as e:
                    print(f"Błąd zapisu mapowania: {e}")
    
    def load_epg_mapping(self):
        """Wczytuje mapowanie kanałów z pliku JSON"""
        if os.path.exists(self.epg_mapping_file):
            try:
                with open(self.epg_mapping_file, 'r') as f:
                    mapping = json.load(f)
                    self.epg_manager.set_channel_mapping(mapping)
                    print(f"✓ Wczytano mapowanie EPG z {self.epg_mapping_file}")
                    return True
            except Exception as e:
                print(f"Błąd wczytywania mapowania EPG: {e}")
        return False
    
    def update_playlist_epg_info(self):
        """Aktualizuje informacje EPG dla kanałów w playliście"""
        n_items = self.playlist_store.get_n_items()
        
        for i in range(n_items):
            item = self.playlist_store.get_item(i)
            if item.channel_id:
                item.current_event = self.epg_manager.get_current_event(item.channel_id)
                item.next_event = self.epg_manager.get_next_event(item.channel_id)
    
    def update_current_program_info(self):
        """Aktualizuje informację o aktualnym programie"""
        selected = self.playlist_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            self.current_program_label.set_text("")
            return
        
        item = self.playlist_store[selected]
        if item.channel_id:
            current = self.epg_manager.get_current_event(item.channel_id)
            if current:
                time_str = current.start.strftime('%H:%M')
                info = f"📺 Teraz: {current.title} (od {time_str})"
                self.current_program_label.set_markup(f"<small>{info}</small>")
                
                # Zaktualizuj oś czasu
                self.timeline_view.set_channel(item.channel_id)
                return
        
        self.current_program_label.set_text("")
    
    def on_channel_changed(self, dropdown, param):
        """Zmieniono kanał"""
        self.update_current_program_info()
    
    def on_play_button_clicked(self, widget):
        selected = self.playlist_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return
        
        item = self.playlist_store[selected]
        uri = item.uri
        
        # Ukryj placeholder gdy zaczynamy odtwarzanie
        self.no_video_label.set_visible(False)
        
        self.player.set_property("uri", uri)
        self.player.set_state(Gst.State.PLAYING)
        
        self.current_channel = item.name
        self.status_label.set_text(f"Odtwarzanie: {item.name}")
        
        # Zaktualizuj oś czasu
        if item.channel_id:
            self.timeline_view.set_channel(item.channel_id)
    
    def on_stop_button_clicked(self, widget):
        self.player.set_state(Gst.State.NULL)
        self.status_label.set_text("Zatrzymano")
        
        # Pokaż placeholder gdy zatrzymujemy
        self.no_video_label.set_visible(True)
    
    def on_mute_button_clicked(self, widget):
        mute = widget.get_active()
        self.player.set_property("mute", mute)
        
        if mute:
            widget.set_label("🔊 Włącz dźwięk")
        else:
            widget.set_label("🔇 Wycisz")
    
    def on_epg_button_clicked(self, widget):
        """Otwiera okno EPG"""
        selected = self.playlist_dropdown.get_selected()
        channel_id = None
        
        if selected != Gtk.INVALID_LIST_POSITION:
            item = self.playlist_store[selected]
            channel_id = item.channel_id
        
        # Importuj tutaj, aby uniknąć cyklicznych importów
        from iptv_player_with_epg import EPGDialog
        
        # Przekazanie ścieżki playlisty i katalogu aplikacji
        epg_dialog = EPGDialog(self.window, self.epg_manager, channel_id, 
                              self.m3u_file, self.app_dir)
        epg_dialog.present()
    
    def on_update_epg_clicked(self, widget):
        """Otwiera dialog aktualizacji EPG"""
        dialog = EPGUpdateDialog(self.window, self.m3u_file, self.app_dir, 
                                self.epg_manager, self.config_manager)
        dialog.present()
    
    def on_scan_button_clicked(self, widget):
        """Otwiera dialog skanowania DVB"""
        dialog = DVBScanDialog(self.window, self.config_manager, self.app_dir)
        dialog.present()
    
    def on_settings_button_clicked(self, widget):
        """Otwiera okno ustawień"""
        dialog = SettingsDialog(self.window, self.config_manager)
        dialog.present()
    
    def on_close_button_clicked(self, widget):
        self.window.close()
    
    def on_window_close(self, window):
        """Zapisz stan przy zamykaniu okna"""
        # Zapisz rozmiar okna
        width, height = window.get_default_size()
        self.config_manager.set('ui', 'window_width', str(width))
        self.config_manager.set('ui', 'window_height', str(height))
        
        # Zapisz konfigurację
        self.config_manager.save_config()
        
        # Zatrzymaj odtwarzanie
        self.player.set_state(Gst.State.NULL)
        
        # Zakończ aplikację
        self.quit()
    
    def on_bus_message(self, bus, message):
        """Obsługa komunikatów GStreamer"""
        t = message.type
        if t == Gst.MessageType.EOS:
            self.player.set_state(Gst.State.NULL)
            self.status_label.set_text("Zakończono odtwarzanie")
            self.no_video_label.set_visible(True)
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Błąd: {err}, {debug}")
            self.status_label.set_text(f"Błąd: {err}")
            self.player.set_state(Gst.State.NULL)
            self.no_video_label.set_visible(True)
    
    def load_default_playlist(self):
        """Wczytuje domyślną playlistę przy starcie"""
        if os.path.exists(self.m3u_file):
            print(f"Wczytywanie domyślnej playlisty: {self.m3u_file}")
            self.load_playlist(self.m3u_file)
        else:
            print(f"Domyślna playlista nie istnieje: {self.m3u_file}")
    
    def update_timer_callback(self):
        """Timer do aktualizacji informacji"""
        self.update_current_program_info()
        return True

class SettingsDialog(Gtk.Window):
    """Dialog ustawień aplikacji"""
    
    def __init__(self, parent, config_manager):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Ustawienia")
        self.set_default_size(500, 400)
        
        self.config_manager = config_manager
        
        # Główny layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(10)
        vbox.set_margin_end(10)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)
        
        # Zakładki
        notebook = Gtk.Notebook()
        vbox.append(notebook)
        
        # Zakładka ogólne
        general_page = self.create_general_page()
        notebook.append_page(general_page, Gtk.Label(label="Ogólne"))
        
        # Zakładka interfejs
        ui_page = self.create_ui_page()
        notebook.append_page(ui_page, Gtk.Label(label="Interfejs"))
        
        # Przyciski
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        button_box.set_halign(Gtk.Align.END)
        
        save_btn = Gtk.Button(label="Zapisz")
        save_btn.connect("clicked", self.on_save_clicked)
        button_box.append(save_btn)
        
        close_btn = Gtk.Button(label="Zamknij")
        close_btn.connect("clicked", lambda b: self.close())
        button_box.append(close_btn)
        
        vbox.append(button_box)
        
        self.set_child(vbox)
    
    def create_general_page(self):
        """Tworzy stronę ustawień ogólnych"""
        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        
        row = 0
        
        # Host minisatip
        host_label = Gtk.Label(label="Host minisatip:")
        host_label.set_halign(Gtk.Align.START)
        grid.attach(host_label, 0, row, 1, 1)
        
        self.host_entry = Gtk.Entry()
        self.host_entry.set_text(self.config_manager.get('general', 'minisatip_host', '192.168.1.1'))
        grid.attach(self.host_entry, 1, row, 1, 1)
        
        row += 1
        
        # Port minisatip
        port_label = Gtk.Label(label="Port minisatip:")
        port_label.set_halign(Gtk.Align.START)
        grid.attach(port_label, 0, row, 1, 1)
        
        self.port_spin = Gtk.SpinButton()
        self.port_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'minisatip_port', '8080')),
            lower=1, upper=65535, step_increment=1))
        grid.attach(self.port_spin, 1, row, 1, 1)
        
        row += 1
        
        # Domyślna liczba dni EPG
        epg_days_label = Gtk.Label(label="Domyślna liczba dni EPG:")
        epg_days_label.set_halign(Gtk.Align.START)
        grid.attach(epg_days_label, 0, row, 1, 1)
        
        self.epg_days_spin = Gtk.SpinButton()
        self.epg_days_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'epg_days', '1')),
            lower=1, upper=7, step_increment=1))
        grid.attach(self.epg_days_spin, 1, row, 1, 1)
        
        row += 1
        
        # Próg duplikacji EPG
        dup_ratio_label = Gtk.Label(label="Próg duplikacji EPG:")
        dup_ratio_label.set_halign(Gtk.Align.START)
        grid.attach(dup_ratio_label, 0, row, 1, 1)
        
        self.dup_ratio_spin = Gtk.SpinButton()
        self.dup_ratio_spin.set_adjustment(Gtk.Adjustment(
            value=float(self.config_manager.get('general', 'epg_duplication_ratio', '1.0')),
            lower=0.5, upper=5.0, step_increment=0.5))
        self.dup_ratio_spin.set_digits(1)
        grid.attach(self.dup_ratio_spin, 1, row, 1, 1)
        
        row += 1
        
        # Czas detekcji skanowania
        scan_det_label = Gtk.Label(label="Czas detekcji skanowania (s):")
        scan_det_label.set_halign(Gtk.Align.START)
        grid.attach(scan_det_label, 0, row, 1, 1)
        
        self.scan_det_spin = Gtk.SpinButton()
        self.scan_det_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'scan_detection_time', '3')),
            lower=1, upper=10, step_increment=1))
        grid.attach(self.scan_det_spin, 1, row, 1, 1)
        
        row += 1
        
        # Czas pełnego skanowania
        scan_full_label = Gtk.Label(label="Czas pełnego skanowania (s):")
        scan_full_label.set_halign(Gtk.Align.START)
        grid.attach(scan_full_label, 0, row, 1, 1)
        
        self.scan_full_spin = Gtk.SpinButton()
        self.scan_full_spin.set_adjustment(Gtk.Adjustment(
            value=int(self.config_manager.get('general', 'scan_full_time', '20')),
            lower=10, upper=60, step_increment=5))
        grid.attach(self.scan_full_spin, 1, row, 1, 1)
        
        return grid
    
    def create_ui_page(self):
        """Tworzy stronę ustawień interfejsu"""
        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        
        row = 0
        
        # Pokaż oś czasu
        show_timeline_label = Gtk.Label(label="Pokaż oś czasu:")
        show_timeline_label.set_halign(Gtk.Align.START)
        grid.attach(show_timeline_label, 0, row, 1, 1)
        
        self.show_timeline_check = Gtk.CheckButton()
        self.show_timeline_check.set_active(self.config_manager.get('ui', 'show_timeline', 'true') == 'true')
        grid.attach(self.show_timeline_check, 1, row, 1, 1)
        
        row += 1
        
        # Autozapis konfiguracji
        auto_save_label = Gtk.Label(label="Autozapis konfiguracji:")
        auto_save_label.set_halign(Gtk.Align.START)
        grid.attach(auto_save_label, 0, row, 1, 1)
        
        self.auto_save_check = Gtk.CheckButton()
        self.auto_save_check.set_active(self.config_manager.get('general', 'auto_save', 'true') == 'true')
        grid.attach(self.auto_save_check, 1, row, 1, 1)
        
        return grid
    
    def on_save_clicked(self, button):
        """Zapisuje ustawienia"""
        # Zapisz ustawienia ogólne
        self.config_manager.set('general', 'minisatip_host', self.host_entry.get_text())
        self.config_manager.set('general', 'minisatip_port', str(self.port_spin.get_value_as_int()))
        self.config_manager.set('general', 'epg_days', str(self.epg_days_spin.get_value_as_int()))
        self.config_manager.set('general', 'epg_duplication_ratio', str(self.dup_ratio_spin.get_value()))
        self.config_manager.set('general', 'scan_detection_time', str(self.scan_det_spin.get_value_as_int()))
        self.config_manager.set('general', 'scan_full_time', str(self.scan_full_spin.get_value_as_int()))
        
        # Zapisz ustawienia interfejsu
        self.config_manager.set('ui', 'show_timeline', 'true' if self.show_timeline_check.get_active() else 'false')
        self.config_manager.set('general', 'auto_save', 'true' if self.auto_save_check.get_active() else 'false')
        
        # Zapisz konfigurację
        self.config_manager.save_config()
        
        # Zamknij okno
        self.close()

if __name__ == '__main__':
    try:
        app = IPTVPlayer()
        app.run()
    except KeyboardInterrupt:
        print("\n\n⏹ Przerwano przez użytkownika")
        sys.exit(0)