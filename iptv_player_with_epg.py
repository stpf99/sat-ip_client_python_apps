#!/usr/bin/env python3
"""
IPTV Player z pełną obsługą EPG
- Integracja z epg.py
- Oś czasu programów
- Widok gazety
- Przypomnienia
- Harmonogram nagrań (kompatybilny z gnome-dvb-daemon)
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, Gst, Gio, GLib, GObject, Pango
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


class EPGDialog(Gtk.Window):
    """Okno EPG z widokiem osi czasu i gazetą"""
    
    def __init__(self, parent, epg_manager, current_channel_id=None, m3u_file=None, app_dir=None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_title("Program TV (EPG)")
        self.set_default_size(1200, 700)
        
        self.epg_manager = epg_manager
        self.current_channel_id = current_channel_id
        self.m3u_file = m3u_file
        self.app_dir = app_dir
        self.selected_channel_filter = None  # None = wszystkie kanały
        
        # Główny layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        vbox.set_margin_start(5)
        vbox.set_margin_end(5)
        vbox.set_margin_top(5)
        vbox.set_margin_bottom(5)
        
        # Header z przyciskami
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)
        
        # Przycisk aktualizacji EPG
        update_btn = Gtk.Button(label="🔄 Aktualizuj EPG")
        update_btn.connect("clicked", self.on_update_epg_clicked)
        header.pack_start(update_btn)
        
        # Dropdown wyboru kanału
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        filter_label = Gtk.Label(label="Kanał:")
        filter_box.append(filter_label)
        
        # Fix: Use StringObject instead of GObject.TYPE_STRING
        self.channel_filter_store = Gio.ListStore(item_type=StringObject)
        self.channel_filter_dropdown = Gtk.DropDown(model=self.channel_filter_store)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.setup_filter_item)
        factory.connect("bind", self.bind_filter_item)
        self.channel_filter_dropdown.set_factory(factory)
        self.channel_filter_dropdown.connect("notify::selected", self.on_channel_filter_changed)
        filter_box.append(self.channel_filter_dropdown)
        
        header.pack_start(filter_box)
        
        # Przełącznik widoku
        view_switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        view_switch.add_css_class("linked")
        
        self.timeline_btn = Gtk.ToggleButton(label="Oś czasu")
        self.timeline_btn.set_active(True)
        self.timeline_btn.connect("toggled", self.on_view_changed, "timeline")
        
        self.grid_btn = Gtk.ToggleButton(label="Gazeta")
        self.grid_btn.set_group(self.timeline_btn)
        self.grid_btn.connect("toggled", self.on_view_changed, "grid")
        
        view_switch.append(self.timeline_btn)
        view_switch.append(self.grid_btn)
        header.pack_end(view_switch)
        
        # Stack z widokami
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        
        # Widok osi czasu
        self.timeline_view = self.create_timeline_view()
        self.stack.add_named(self.timeline_view, "timeline")
        
        # Widok gazety
        self.grid_view = self.create_grid_view()
        self.stack.add_named(self.grid_view, "grid")
        
        vbox.append(self.stack)
        
        # Panel szczegółów na dole
        self.details_panel = self.create_details_panel()
        vbox.append(self.details_panel)
        
        self.set_child(vbox)
        
        # Załaduj dane
        self.populate_channel_filter()
        self.refresh_views()
    
    def populate_channel_filter(self):
        """Wypełnia dropdown z kanałami"""
        self.channel_filter_store.remove_all()
        
        # Dodaj "Wszystkie kanały"
        self.channel_filter_store.append(StringObject("Wszystkie kanały"))
        
        # Dodaj posortowane kanały
        channels_sorted = sorted(self.epg_manager.channels.items(), 
                                key=lambda x: x[1])
        
        for channel_id, channel_name in channels_sorted:
            self.channel_filter_store.append(StringObject(f"{channel_name}"))
        
        self.channel_filter_dropdown.set_selected(0)
    
    def setup_filter_item(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0)
        list_item.set_child(label)
    
    def bind_filter_item(self, factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        label.set_label(item.get_string())
    
    def on_channel_filter_changed(self, dropdown, param):
        """Zmieniono filtr kanału"""
        selected = dropdown.get_selected()
        
        if selected == 0:
            # Wszystkie kanały
            self.selected_channel_filter = None
        else:
            # Konkretny kanał - znajdź jego ID
            channel_name = self.channel_filter_store[selected].get_string()
            
            # Znajdź channel_id dla tej nazwy
            for ch_id, ch_name in self.epg_manager.channels.items():
                if ch_name == channel_name:
                    self.selected_channel_filter = ch_id
                    break
        
        # Odśwież widoki
        self.refresh_views()
    
    def create_timeline_view(self):
        """Tworzy widok osi czasu"""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        # Lista kanałów z programami
        self.timeline_listbox = Gtk.ListBox()
        self.timeline_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.timeline_listbox.connect("row-selected", self.on_timeline_row_selected)
        
        scrolled.set_child(self.timeline_listbox)
        return scrolled
    
    def create_grid_view(self):
        """Tworzy widok gazety TV"""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        # Grid z programami
        self.grid_listbox = Gtk.ListBox()
        self.grid_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.grid_listbox.connect("row-selected", self.on_grid_row_selected)
        
        scrolled.set_child(self.grid_listbox)
        return scrolled
    
    def create_details_panel(self):
        """Panel szczegółów wybranego programu"""
        frame = Gtk.Frame()
        frame.set_margin_top(5)
        
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        vbox.set_margin_start(10)
        vbox.set_margin_end(10)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)
        
        # Tytuł
        self.details_title = Gtk.Label()
        self.details_title.set_markup("<b>Wybierz program aby zobaczyć szczegóły</b>")
        self.details_title.set_xalign(0)
        self.details_title.set_wrap(True)
        vbox.append(self.details_title)
        
        # Informacje
        self.details_info = Gtk.Label()
        self.details_info.set_xalign(0)
        self.details_info.set_wrap(True)
        vbox.append(self.details_info)
        
        # Opis
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(80)
        scroll.set_vexpand(True)
        
        self.details_desc = Gtk.TextView()
        self.details_desc.set_editable(False)
        self.details_desc.set_wrap_mode(Gtk.WrapMode.WORD)
        scroll.set_child(self.details_desc)
        vbox.append(scroll)
        
        # Przyciski akcji
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        actions.set_margin_top(5)
        
        self.remind_btn = Gtk.Button(label="Dodaj przypomnienie")
        self.remind_btn.connect("clicked", self.on_add_reminder)
        actions.append(self.remind_btn)
        
        self.record_btn = Gtk.Button(label="Zaplanuj nagranie")
        self.record_btn.connect("clicked", self.on_add_recording)
        actions.append(self.record_btn)
        
        self.series_btn = Gtk.Button(label="Śledź serial")
        self.series_btn.connect("clicked", self.on_track_series)
        actions.append(self.series_btn)
        
        vbox.append(actions)
        
        frame.set_child(vbox)
        return frame
    
    def refresh_views(self):
        """Odświeża wszystkie widoki"""
        self.refresh_timeline()
        self.refresh_grid()
    
    def refresh_timeline(self):
        """Odświeża widok osi czasu z pełną datą"""
        # Wyczyść
        child = self.timeline_listbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.timeline_listbox.remove(child)
            child = next_child
        
        now = datetime.now()
        
        # Filtruj kanały jeśli wybrano konkretny
        channels_to_show = {}
        if self.selected_channel_filter:
            if self.selected_channel_filter in self.epg_manager.channels:
                channels_to_show[self.selected_channel_filter] = \
                    self.epg_manager.channels[self.selected_channel_filter]
        else:
            channels_to_show = self.epg_manager.channels
        
        # Dla każdego kanału
        for channel_id in sorted(channels_to_show.keys(), 
                                key=lambda x: channels_to_show[x]):
            channel_name = channels_to_show[channel_id]
            events = self.epg_manager.get_events_for_channel(channel_id, now, 6)
            
            if not events:
                continue
            
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_margin_start(5)
            hbox.set_margin_end(5)
            hbox.set_margin_top(5)
            hbox.set_margin_bottom(5)
            
            # Nazwa kanału
            channel_label = Gtk.Label()
            channel_label.set_markup(f"<b>{channel_name}</b>")
            channel_label.set_width_chars(20)
            channel_label.set_xalign(0)
            hbox.append(channel_label)
            
            # Timeline programów
            timeline_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            
            for event in events[:4]:  # Pokaż max 4 programy
                event_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                
                # Dodaj dzień tygodnia jeśli program jest w innym dniu
                time_label = Gtk.Label()
                if event.start.date() != now.date():
                    weekday_names = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"]
                    weekday = weekday_names[event.start.weekday()]
                    time_label.set_markup(f"<small>{weekday} {event.start.strftime('%H:%M')}</small>")
                else:
                    time_label.set_markup(f"<small>{event.start.strftime('%H:%M')}</small>")
                event_box.append(time_label)
                
                title_label = Gtk.Label()
                title_label.set_text(event.title[:30])
                title_label.set_ellipsize(Pango.EllipsizeMode.END)
                title_label.set_max_width_chars(30)
                
                # Podświetl aktualny program
                if event.start <= now < event.stop:
                    title_label.set_markup(f"<b>▶ {event.title[:30]}</b>")
                
                event_box.append(title_label)
                
                timeline_box.append(event_box)
            
            hbox.append(timeline_box)
            row.set_child(hbox)
            row.channel_id = channel_id
            self.timeline_listbox.append(row)
    
    def refresh_grid(self):
        """Odświeża widok gazety"""
        # Wyczyść
        child = self.grid_listbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.grid_listbox.remove(child)
            child = next_child
        
        now = datetime.now()
        
        # Filtruj kanały jeśli wybrano konkretny
        channels_to_show = {}
        if self.selected_channel_filter:
            if self.selected_channel_filter in self.epg_manager.channels:
                channels_to_show[self.selected_channel_filter] = \
                    self.epg_manager.channels[self.selected_channel_filter]
        else:
            channels_to_show = self.epg_manager.channels
        
        # Wszystkie programy z wybranych kanałów, posortowane po czasie
        all_events = []
        for channel_id in channels_to_show.keys():
            events = self.epg_manager.get_events_for_channel(channel_id, now, 12)
            for event in events:
                all_events.append((channel_id, event))
        
        all_events.sort(key=lambda x: x[1].start)
        
        # Wyświetl
        for channel_id, event in all_events[:100]:  # Max 100
            channel_name = self.epg_manager.channels.get(channel_id, f"Service {channel_id}")
            
            row = Gtk.ListBoxRow()
            grid = Gtk.Grid()
            grid.set_column_spacing(10)
            grid.set_row_spacing(5)
            grid.set_margin_start(10)
            grid.set_margin_end(10)
            grid.set_margin_top(5)
            grid.set_margin_bottom(5)
            
            # Czas
            time_label = Gtk.Label()
            time_str = f"{event.start.strftime('%H:%M')} - {event.stop.strftime('%H:%M')}"
            time_label.set_markup(f"<b>{time_str}</b>")
            time_label.set_xalign(0)
            grid.attach(time_label, 0, 0, 1, 1)
            
            # Kanał
            channel_label = Gtk.Label()
            channel_label.set_markup(f"<small>{channel_name}</small>")
            channel_label.set_xalign(0)
            grid.attach(channel_label, 0, 1, 1, 1)
            
            # Tytuł
            title_label = Gtk.Label()
            is_current = event.start <= now < event.stop
            if is_current:
                title_label.set_markup(f"<b>▶ {event.title}</b>")
            else:
                title_label.set_text(event.title)
            title_label.set_xalign(0)
            title_label.set_wrap(True)
            grid.attach(title_label, 1, 0, 1, 2)
            
            row.set_child(grid)
            row.event_data = (channel_id, event)
            self.grid_listbox.append(row)
    
    def on_timeline_row_selected(self, listbox, row):
        """Wybrano wiersz w osi czasu"""
        if row is None:
            return
        
        channel_id = row.channel_id
        current_event = self.epg_manager.get_current_event(channel_id)
        
        if current_event:
            self.show_event_details(channel_id, current_event)
    
    def on_grid_row_selected(self, listbox, row):
        """Wybrano wiersz w gazecie"""
        if row is None:
            return
        
        channel_id, event = row.event_data
        self.show_event_details(channel_id, event)
    
    def show_event_details(self, channel_id, event):
        """Wyświetla szczegóły programu z pełną datą"""
        channel_name = self.epg_manager.channels.get(channel_id, "Nieznany")
        
        self.details_title.set_markup(f"<b>{event.title}</b>")
        
        # Dodaj dzień tygodnia, pełną datę i czas
        weekday_names = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
        month_names = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", 
                      "lipca", "sierpnia", "września", "października", "listopada", "grudnia"]
        
        weekday = weekday_names[event.start.weekday()]
        day = event.start.day
        month = month_names[event.start.month - 1]
        year = event.start.year
        
        time_str = f"{event.start.strftime('%H:%M')} - {event.stop.strftime('%H:%M')}"
        date_str = f"{weekday}, {day} {month} {year}"
        duration = event.stop - event.start
        duration_min = int(duration.total_seconds() / 60)
        
        info_text = f"Kanał: {channel_name}\nData: {date_str}\nCzas: {time_str} ({duration_min} min)"
        self.details_info.set_text(info_text)
        
        buffer = self.details_desc.get_buffer()
        buffer.set_text(event.desc if event.desc else "Brak opisu")
        
        self.selected_event = (channel_id, event)
    
    def on_add_reminder(self, button):
        """Dodaje przypomnienie"""
        if not hasattr(self, 'selected_event'):
            return
        
        channel_id, event = self.selected_event
        # TODO: Dodaj do listy przypomnień
        print(f"Przypomnienie: {event.title} o {event.start}")
        
        notify2.Notification(
            "Przypomnienie dodane",
            f"Przypomnimy o: {event.title}",
            "dialog-information"
        ).show()
    
    def on_add_recording(self, button):
        """Dodaje do harmonogramu nagrań"""
        if not hasattr(self, 'selected_event'):
            return
        
        channel_id, event = self.selected_event
        # TODO: Dodaj do harmonogramu nagrań
        print(f"Nagranie: {event.title} o {event.start}")
        
        notify2.Notification(
            "Nagranie zaplanowane",
            f"Nagra się: {event.title}",
            "dialog-information"
        ).show()
    
    def on_track_series(self, button):
        """Śledzi serial (wszystkie odcinki)"""
        if not hasattr(self, 'selected_event'):
            return
        
        channel_id, event = self.selected_event
        # TODO: Znajdź wszystkie odcinki serialu
        print(f"Śledzenie serialu: {event.title}")
        
        notify2.Notification(
            "Serial śledzony",
            f"Będziemy przypominać o kolejnych odcinkach: {event.title}",
            "dialog-information"
        ).show()
    
    def on_update_epg_clicked(self, button):
        """Uruchamia aktualizację EPG"""
        if not self.m3u_file or not self.app_dir:
            dialog = Gtk.AlertDialog()
            dialog.set_message("Nie można zaktualizować EPG")
            dialog.set_detail("Brak informacji o playliście lub katalogu konfiguracji")
            dialog.show(self)
            return
        
        dialog = EPGUpdateDialog(self, self.m3u_file, self.app_dir, self.epg_manager)
        dialog.present()
    
    def on_view_changed(self, button, view_name):
        """Zmienia widok"""
        if button.get_active():
            self.stack.set_visible_child_name(view_name)


class EPGUpdateDialog(Gtk.Window):
    """Dialog aktualizacji EPG - uruchamia epg.py"""
    
    def __init__(self, parent, m3u_file, app_dir, epg_manager):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Aktualizacja EPG")
        self.set_default_size(700, 500)
        
        self.m3u_file = m3u_file
        self.app_dir = app_dir
        self.epg_manager = epg_manager
        self.process = None
        self.days = 1  # Domyślnie 1 dzień
        
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
        options_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        options_label = Gtk.Label(label="Liczba dni EPG:")
        options_box.append(options_label)
        
        self.days_spin = Gtk.SpinButton()
        self.days_spin.set_adjustment(Gtk.Adjustment(value=1, lower=1, upper=7, step_increment=1))
        self.days_spin.connect("value-changed", self.on_days_changed)
        options_box.append(self.days_spin)
        
        vbox.append(options_box)
        
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
        self.days = spin_button.get_value_as_int()
    
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
        
        # Usuń stary plik EPG
        epg_file = os.path.join(self.app_dir, "epg.xml")
        if os.path.exists(epg_file):
            os.remove(epg_file)
            self.append_log(f"✓ Usunięto stary plik EPG\n")
        
        # Przygotuj komendę - nowy format wywołania
        cmd = [
            sys.executable,  # Użyj tego samego Pythona
            os.path.join(os.path.dirname(__file__), "epg.py"),
            "-m", self.m3u_file,
            "-o", epg_file
        ]
        
        # Dodaj dodatkowe parametry jeśli potrzebne
        if self.days > 1:
            cmd.extend(["--days", str(self.days)])
        
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


class IPTVPlayer(Gtk.Application):
    """Główna aplikacja IPTV Player z EPG"""
    
    def __init__(self):
        super().__init__(application_id="com.example.IPTVPlayerEPG")
        self.app_dir = os.path.expanduser("~/.config/iptv_player")
        os.makedirs(self.app_dir, exist_ok=True)
        
        self.window = None
        self.player = None
        self.stream_start_time = None
        self.current_channel = ""
        self.current_selection = Gtk.INVALID_LIST_POSITION
        self.initial_width = 0
        self.initial_height = 0
        self.file_chooser_dialog = None
        
        # EPG Manager
        self.epg_manager = EPGManager()
        self.epg_file = os.path.join(self.app_dir, "epg.xml")
        self.m3u_file = os.path.join(self.app_dir, "playlist_with_tvgid.m3u")  # Domyślna playlista
        self.epg_mapping_file = os.path.join(self.app_dir, "epg_mapping.json")  # Plik mapowania
        
        # Przypomnienia
        self.reminder_timer = None
    
    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("IPTV Player z EPG")
        
        # Inicjalizacja widgetów
        self.play_button = Gtk.Button(label="Play")
        self.stop_button = Gtk.Button(label="Stop")
        self.mute_button = Gtk.ToggleButton(label="Mute")
        self.filechooser_button = Gtk.Button(label="Wybierz playlistę")
        self.epg_button = Gtk.Button(label="📺 Program TV (EPG)")
        self.update_epg_button = Gtk.Button(label="🔄 Aktualizuj EPG")
        self.close_button = Gtk.Button(label="Zamknij")
        self.status_label = Gtk.Label(label="")
        self.current_program_label = Gtk.Label(label="")
        
        # Playlist dropdown
        self.playlist_store = Gio.ListStore(item_type=PlaylistItem)
        self.playlist_dropdown = Gtk.DropDown(model=self.playlist_store)
        self.playlist_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.setup_dropdown_item)
        factory.connect("bind", self.bind_dropdown_item)
        self.playlist_dropdown.set_factory(factory)
        
        # Połącz sygnały
        self.play_button.connect("clicked", self.on_play_button_clicked)
        self.stop_button.connect("clicked", self.on_stop_button_clicked)
        self.mute_button.connect("clicked", self.on_mute_button_clicked)
        self.filechooser_button.connect("clicked", self.on_filechooser_button_clicked)
        self.epg_button.connect("clicked", self.on_epg_button_clicked)
        self.update_epg_button.connect("clicked", self.on_update_epg_clicked)
        self.close_button.connect("clicked", self.on_close_button_clicked)
        self.playlist_dropdown.connect("notify::selected", self.on_channel_changed)
        
        # Style
        for button in [self.play_button, self.stop_button, self.mute_button, 
                       self.filechooser_button, self.epg_button, self.update_epg_button, self.close_button]:
            button.set_css_classes(["flat"])
            button.set_margin_start(0)
            button.set_margin_end(0)
            button.set_margin_top(0)
            button.set_margin_bottom(0)
        
        # Layout początkowy
        self.initial_layout = Gtk.Grid()
        self.initial_layout.set_row_spacing(2)
        self.initial_layout.set_column_spacing(2)
        self.initial_layout.set_margin_start(2)
        self.initial_layout.set_margin_end(2)
        self.initial_layout.set_margin_top(2)
        self.initial_layout.set_margin_bottom(2)
        
        self.initial_layout.attach(self.filechooser_button, 0, 0, 2, 1)
        self.initial_layout.attach(self.epg_button, 2, 0, 1, 1)
        self.initial_layout.attach(self.playlist_dropdown, 0, 1, 3, 1)
        self.initial_layout.attach(self.current_program_label, 0, 2, 3, 1)
        self.initial_layout.attach(self.play_button, 0, 3, 1, 1)
        self.initial_layout.attach(self.stop_button, 1, 3, 1, 1)
        self.initial_layout.attach(self.mute_button, 2, 3, 1, 1)
        self.initial_layout.attach(self.update_epg_button, 0, 4, 3, 1)
        self.initial_layout.attach(self.status_label, 0, 5, 3, 1)
        self.initial_layout.attach(self.close_button, 0, 6, 3, 1)
        
        # Rozmiar początkowy
        min_w, nat_w, _, _ = self.initial_layout.measure(Gtk.Orientation.HORIZONTAL, -1)
        min_h, nat_h, _, _ = self.initial_layout.measure(Gtk.Orientation.VERTICAL, nat_w)
        self.initial_width = nat_w + 20
        self.initial_height = nat_h + 20
        
        # Toolbar dla odtwarzania
        self.toolbar = Gtk.HeaderBar()
        self.toolbar.set_css_classes(["flat"])
        
        # Main layout dla video + toolbar
        self.main_layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_layout.set_margin_start(0)
        self.main_layout.set_margin_end(0)
        self.main_layout.set_margin_top(0)
        self.main_layout.set_margin_bottom(0)
        
        # GStreamer
        self.player = Gst.ElementFactory.make("playbin", "player")
        
        self.sink = Gst.ElementFactory.make("gtk4paintablesink", "sink")
        if self.sink is not None:
            self.player.set_property("video-sink", self.sink)
            paintable = self.sink.get_property("paintable")
            self.video_picture = Gtk.Picture()
            self.video_picture.set_paintable(paintable)
            self.main_layout.append(self.video_picture)
        else:
            print("Ostrzeżenie: gtk4paintablesink niedostępny")
            self.video_label = Gtk.Label(label="Video w osobnym oknie\n(Zainstaluj gstreamer1.0-plugins-bad)")
            self.main_layout.append(self.video_label)
        
        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)
        
        # Ustaw początkowy layout
        self.window.set_child(self.initial_layout)
        self.window.set_default_size(self.initial_width, self.initial_height)
        
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
        
        self.window.present()
    
    def load_default_playlist(self):
        """Wczytuje domyślną playlistę przy starcie"""
        if os.path.exists(self.m3u_file):
            print(f"Wczytywanie domyślnej playlisty: {self.m3u_file}")
            self.load_playlist(self.m3u_file)
        else:
            print(f"Domyślna playlista nie istnieje: {self.m3u_file}")
    
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
        self.file_chooser_dialog = Gtk.FileDialog()
        self.file_chooser_dialog.set_title("Wybierz playlistę")
        
        m3u_filter = Gtk.FileFilter()
        m3u_filter.set_name("Pliki M3U")
        m3u_filter.add_pattern("*.m3u")
        m3u_filter.add_pattern("*.m3u8")
        
        all_filter = Gtk.FileFilter()
        all_filter.set_name("Wszystkie pliki")
        all_filter.add_pattern("*")
        
        filters_store = Gio.ListStore.new(Gtk.FileFilter)
        filters_store.append(m3u_filter)
        filters_store.append(all_filter)
        
        self.file_chooser_dialog.set_filters(Gtk.FilterListModel.new(filters_store, None))
        self.file_chooser_dialog.set_default_filter(m3u_filter)
        
        self.file_chooser_dialog.open(self.window, None, self.on_filechooser_open_response, self)
    
    def on_filechooser_open_response(self, dialog, result, app):
        try:
            file = dialog.open_finish(result)
            if file is not None:
                playlist_path = file.get_path()
                if playlist_path:
                    app.m3u_file = playlist_path
                    app.load_playlist(playlist_path)
        except GLib.GError as error:
            if error.domain != GLib.IOError:
                print(f"Błąd: {error}")
        finally:
            app.file_chooser_dialog = None
    
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
                return
        
        self.current_program_label.set_text("")
    
    def on_channel_changed(self, dropdown, param):
        """Zmieniono kanał"""
        self.update_current_program_info()
    
    def on_epg_button_clicked(self, widget):
        """Otwiera okno EPG"""
        selected = self.playlist_dropdown.get_selected()
        channel_id = None
        
        if selected != Gtk.INVALID_LIST_POSITION:
            item = self.playlist_store[selected]
            channel_id = item.channel_id
        
        # Przekazanie ścieżki playlisty i katalogu aplikacji
        epg_dialog = EPGDialog(self.window, self.epg_manager, channel_id, 
                              self.m3u_file, self.app_dir)
        epg_dialog.present()
    
    def on_update_epg_clicked(self, widget):
        """Otwiera dialog aktualizacji EPG"""
        dialog = EPGUpdateDialog(self.window, self.m3u_file, self.app_dir, self.epg_manager)
        dialog.present()
    
    def on_play_button_clicked(self, widget):
        selected = self.playlist_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return
        
        if self.window.get_child() != self.main_layout:
            for w in [self.play_button, self.stop_button, self.mute_button, 
                      self.playlist_dropdown, self.status_label, self.epg_button,
                      self.current_program_label]:
                if w.get_parent():
                    w.get_parent().remove(w)
            
            self.toolbar.pack_start(self.play_button)
            self.toolbar.pack_start(self.stop_button)
            self.toolbar.pack_start(self.mute_button)
            self.toolbar.pack_start(self.playlist_dropdown)
            self.toolbar.pack_start(self.epg_button)
            self.toolbar.pack_end(self.current_program_label)
            self.toolbar.pack_end(self.status_label)
            self.main_layout.append(self.toolbar)
            self.window.set_child(self.main_layout)
        
        current_state = self.player.get_state(0)[1]
        
        if selected != self.current_selection:
            self.player.set_state(Gst.State.NULL)
            uri = self.playlist_store[selected].uri
            self.player.set_property("uri", uri)
            ret = self.player.set_state(Gst.State.PLAYING)
            
            if ret == Gst.StateChangeReturn.FAILURE:
                self.status_label.set_label("Błąd odtwarzania")
                self.restore_initial_layout()
                return
            
            self.stream_start_time = time.time()
            self.current_channel = self.playlist_store[selected].name
            self.current_selection = selected
            self.status_label.set_label(f"▶ {self.current_channel}")
            self.play_button.set_label("Pauza")
            self.update_current_program_info()
            self.show_now_playing_notification()
        else:
            if current_state == Gst.State.NULL:
                uri = self.playlist_store[selected].uri
                self.player.set_property("uri", uri)
                ret = self.player.set_state(Gst.State.PLAYING)
                
                if ret == Gst.StateChangeReturn.FAILURE:
                    self.status_label.set_label("Błąd")
                    self.restore_initial_layout()
                    return
                
                self.stream_start_time = time.time()
                self.status_label.set_label(f"▶ {self.current_channel}")
                self.play_button.set_label("Pauza")
                self.show_now_playing_notification()
            elif current_state == Gst.State.PAUSED:
                self.player.set_state(Gst.State.PLAYING)
                self.status_label.set_label(f"▶ {self.current_channel}")
                self.play_button.set_label("Pauza")
            elif current_state == Gst.State.PLAYING:
                self.player.set_state(Gst.State.PAUSED)
                self.status_label.set_label("⏸ Pauza")
                self.play_button.set_label("Wznów")
    
    def on_stop_button_clicked(self, widget):
        self.player.set_state(Gst.State.NULL)
        self.stream_start_time = None
        self.status_label.set_label("⏹ Zatrzymano")
        self.play_button.set_label("Play")
        self.restore_initial_layout()
    
    def on_mute_button_clicked(self, widget):
        new_mute_state = widget.get_active()
        self.player.set_property("mute", new_mute_state)
        self.mute_button.set_label("🔊" if new_mute_state else "🔇")
    
    def on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            self.player.set_state(Gst.State.NULL)
            self.stream_start_time = None
            self.status_label.set_label("Koniec strumienia")
            self.restore_initial_layout()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")
            self.status_label.set_label(f"Błąd: {err}")
            self.restore_initial_layout()
    
    def restore_initial_layout(self):
        """Przywraca początkowy layout"""
        if self.window.get_child() == self.main_layout:
            # Usuń widgety z toolbar
            for w in [self.play_button, self.stop_button, self.mute_button, 
                      self.playlist_dropdown, self.status_label, self.epg_button,
                      self.current_program_label]:
                if w.get_parent():
                    w.get_parent().remove(w)
            
            # Dodaj z powrotem do initial_layout
            self.initial_layout.attach(self.filechooser_button, 0, 0, 2, 1)
            self.initial_layout.attach(self.epg_button, 2, 0, 1, 1)
            self.initial_layout.attach(self.playlist_dropdown, 0, 1, 3, 1)
            self.initial_layout.attach(self.current_program_label, 0, 2, 3, 1)
            self.initial_layout.attach(self.play_button, 0, 3, 1, 1)
            self.initial_layout.attach(self.stop_button, 1, 3, 1, 1)
            self.initial_layout.attach(self.mute_button, 2, 3, 1, 1)
            self.initial_layout.attach(self.update_epg_button, 0, 4, 3, 1)
            self.initial_layout.attach(self.status_label, 0, 5, 3, 1)
            self.initial_layout.attach(self.close_button, 0, 6, 3, 1)
            
            self.window.set_child(self.initial_layout)
            self.window.set_default_size(self.initial_width, self.initial_height)
    
    def on_close_button_clicked(self, widget):
        self.window.close()
    
    def show_now_playing_notification(self):
        """Pokazuje powiadomienie o aktualnie odtwarzanym kanale"""
        notify2.Notification(
            "IPTV Player",
            f"Odtwarzanie: {self.current_channel}",
            "media-playback-start"
        ).show()
    
    def update_timer_callback(self):
        """Callback dla timera aktualizacji"""
        # Aktualizuj info o programie
        self.update_current_program_info()
        
        # Sprawdź przypomnienia
        self.check_reminders()
        
        return True  # Kontynuuj timer
    
    def check_reminders(self):
        """Sprawdza czy są przypomnienia do pokazania"""
        now = datetime.now()
        
        # Sprawdź programy w ciągu najbliższych 5 minut
        for channel_id, events in self.epg_manager.events.items():
            for event in events:
                # Jeśli program zaczyna się w ciągu 5 minut i jeszcze nie przypomniano
                if now < event.start <= now + timedelta(minutes=5):
                    if not event.is_reminder:
                        event.is_reminder = True
                        channel_name = self.epg_manager.channels.get(channel_id, "Nieznany")
                        
                        notify2.Notification(
                            "Przypomnienie o programie",
                            f"Za chwilę: {event.title}\nKanał: {channel_name}",
                            "appointment-soon"
                        ).show()


if __name__ == '__main__':
    # Inicjalizacja GStreamer
    Gst.init(None)
    
    app = IPTVPlayer()
    app.run()