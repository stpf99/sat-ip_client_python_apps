#!/usr/bin/env python3
"""
EPG Grabber z analizą w czasie rzeczywistym
- Parsowanie na bieżąco podczas zbierania
- Cache zapisywany równolegle
- Natychmiastowe zatrzymanie przy braku nowych wydarzeń
- Automatyczna zmiana muxa gdy liczba duplikatów równa liczbie nowych
"""

import re
import sys
import time
import struct
import requests
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
import xml.etree.ElementTree as ET
from io import BytesIO
import os
import json
import threading
from concurrent.futures import ThreadPoolExecutor


# ============================================================================
# PARSOWANIE MPEG-TS (bez zmian)
# ============================================================================

class TSPacket:
    """Reprezentacja pakietu MPEG-TS (188 bajtów)"""
    SYNC_BYTE = 0x47
    PACKET_SIZE = 188
    
    def __init__(self, data):
        if len(data) != self.PACKET_SIZE:
            raise ValueError(f"Nieprawidłowy rozmiar pakietu TS: {len(data)}")
        
        if data[0] != self.SYNC_BYTE:
            raise ValueError(f"Brak bajtu synchronizacji: 0x{data[0]:02x}")
        
        self.data = data
        self.parse_header()
    
    def parse_header(self):
        """Parsuje nagłówek pakietu TS"""
        b1 = self.data[1]
        b2 = self.data[2]
        
        self.transport_error = (b1 & 0x80) != 0
        self.payload_unit_start = (b1 & 0x40) != 0
        self.transport_priority = (b1 & 0x20) != 0
        self.pid = ((b1 & 0x1F) << 8) | b2
        
        b3 = self.data[3]
        self.scrambling = (b3 & 0xC0) >> 6
        self.adaptation_field = (b3 & 0x30) >> 4
        self.continuity_counter = b3 & 0x0F
        
        self.payload_offset = 4
        
        if self.adaptation_field in [2, 3]:
            adaptation_length = self.data[4]
            self.payload_offset += adaptation_length + 1
    
    def get_payload(self):
        """Zwraca payload pakietu"""
        if self.adaptation_field in [1, 3]:
            return self.data[self.payload_offset:]
        return b''


# ============================================================================
# REALTIME STREAM ANALYZER - NOWA KLASA
# ============================================================================

class RealtimeStreamAnalyzer:
    """Analizator strumienia w czasie rzeczywistym"""
    
    PID_PAT = 0x0000
    PID_NIT = 0x0010
    PID_SDT = 0x0011
    PID_EIT = 0x0012
    PID_TDT = 0x0014
    
    def __init__(self, cache_file=None, existing_events=None):
        self.cache_file = cache_file
        self.cache_fh = None
        
        # Bufory per PID
        self.pid_buffers = defaultdict(BytesIO)
        self.pid_continuity = {}
        
        # Parsery
        self.eit_parser = EITParser()
        self.sdt_parser = SDTParser()
        
        # Statystyki
        self.total_packets = 0
        self.pid_packet_count = defaultdict(int)
        self.analysis_count = 0
        
        # Monitor wydarzeń
        self.seen_events = set()  # (service_id, event_id, start_time)
        self.existing_events = existing_events or set()
        self.new_events_count = 0
        self.duplicate_count = 0
        self.last_new_event_time = time.time()
        
        # Wyniki
        self.events = []
        self.services = {}
        
        # Kontrola
        self._lock = threading.Lock()
        self.should_stop = False
        
        # Konfiguracja zatrzymania
        self.min_duration = 30  # Minimalne 30s zbierania
        self.no_new_timeout = 60  # Zatrzymaj po 60s bez nowych wydarzeń
        self.max_duplicates = 100  # Zatrzymaj po 100 duplikatach z rzędu
        self.duplication_ratio = 1.0  # Stosunek duplikatów do nowych wydarzeń
        self.consecutive_duplicates = 0
        
        self.start_time = time.time()
    
    def open_cache(self):
        """Otwiera plik cache"""
        if self.cache_file:
            self.cache_fh = open(self.cache_file, 'wb')
            return True
        return False
    
    def close_cache(self):
        """Zamyka plik cache"""
        if self.cache_fh:
            self.cache_fh.close()
    
    def process_stream(self, stream_url, max_duration=300):
        """Przetwarza strumień z analizą w czasie rzeczywistym"""
        print(f"  📡 Łączenie ze strumieniem...")
        print(f"  ⚡ Analiza na żywo - zatrzymanie przy braku nowych")
        
        if self.cache_file:
            self.open_cache()
            print(f"  💾 Cache: {os.path.basename(self.cache_file)}")
        
        try:
            response = requests.get(stream_url, stream=True, timeout=10)
            
            if response.status_code != 200:
                print(f"  ✗ Błąd HTTP: {response.status_code}")
                return False
            
            buffer = b''
            last_log_time = time.time()
            last_check_time = time.time()
            
            for chunk in response.iter_content(chunk_size=TSPacket.PACKET_SIZE * 100):
                # Sprawdź flagę zatrzymania
                if self.should_stop:
                    print(f"\n  ⏹ Zatrzymano (sygnał zakończenia)")
                    break
                
                elapsed = time.time() - self.start_time
                
                # Sprawdź maksymalny czas
                if elapsed > max_duration:
                    print(f"\n  ⏰ Osiągnięto maksymalny czas {max_duration}s")
                    break
                
                # Status co sekundę
                if time.time() - last_log_time > 1.0:
                    self._print_status(elapsed, max_duration)
                    last_log_time = time.time()
                
                # Sprawdź warunki zatrzymania co 5s
                if time.time() - last_check_time > 5.0:
                    if self._should_stop_collection(elapsed):
                        break
                    last_check_time = time.time()
                
                buffer += chunk
                
                # Zapisz do cache
                if self.cache_fh:
                    self.cache_fh.write(chunk)
                
                # Przetwarzaj pakiety
                while len(buffer) >= TSPacket.PACKET_SIZE:
                    try:
                        packet = TSPacket(buffer[:TSPacket.PACKET_SIZE])
                        buffer = buffer[TSPacket.PACKET_SIZE:]
                        
                        with self._lock:
                            self.total_packets += 1
                            self.pid_packet_count[packet.pid] += 1
                            
                            # Przetwórz tylko pakiety EPG
                            if packet.pid in [self.PID_EIT, self.PID_SDT]:
                                self._process_packet_realtime(packet)
                    
                    except ValueError:
                        sync_pos = buffer.find(bytes([TSPacket.SYNC_BYTE]), 1)
                        if sync_pos > 0:
                            buffer = buffer[sync_pos:]
                        else:
                            buffer = b''
            
            response.close()
            self._print_final_status()
            
        except requests.RequestException as e:
            print(f"\n  ✗ Błąd: {e}")
            return False
        
        finally:
            self.close_cache()
        
        return True
    
    def _process_packet_realtime(self, packet):
        """Przetwarza pakiet i analizuje sekcje na bieżąco"""
        pid = packet.pid
        
        # Sprawdź ciągłość
        if pid in self.pid_continuity:
            expected = (self.pid_continuity[pid] + 1) & 0x0F
            if packet.continuity_counter != expected and self.pid_continuity[pid] != packet.continuity_counter:
                self.pid_buffers[pid] = BytesIO()
        
        self.pid_continuity[pid] = packet.continuity_counter
        
        payload = packet.get_payload()
        if not payload:
            return
        
        # Składanie sekcji
        if packet.payload_unit_start:
            pointer = payload[0]
            
            # Dokończ poprzednią sekcję
            if pointer > 0 and len(payload) > pointer:
                self.pid_buffers[pid].write(payload[1:1+pointer])
                section_data = self.pid_buffers[pid].getvalue()
                
                if len(section_data) > 3:
                    # ANALIZA NA ŻYWO!
                    self._analyze_section_immediately(pid, bytes(section_data))
            
            # Rozpocznij nową sekcję
            self.pid_buffers[pid] = BytesIO()
            if len(payload) > 1 + pointer:
                self.pid_buffers[pid].write(payload[1+pointer:])
        else:
            self.pid_buffers[pid].write(payload)
    
    def _analyze_section_immediately(self, pid, section_data):
        """Natychmiastowa analiza sekcji"""
        self.analysis_count += 1
        
        if pid == self.PID_EIT:
            # Parsuj EIT
            events = self.eit_parser.parse_section_single(section_data)
            
            for event in events:
                event_key = (event['service_id'], event['event_id'], event['start'])
                
                # Sprawdź czy nowe
                if event_key in self.existing_events or event_key in self.seen_events:
                    self.duplicate_count += 1
                    self.consecutive_duplicates += 1
                else:
                    # NOWE WYDARZENIE!
                    self.seen_events.add(event_key)
                    self.events.append(event)
                    self.new_events_count += 1
                    self.consecutive_duplicates = 0
                    self.last_new_event_time = time.time()
        
        elif pid == self.PID_SDT:
            # Parsuj SDT
            services = self.sdt_parser.parse_section_single(section_data)
            self.services.update(services)
    
    def _should_stop_collection(self, elapsed):
        """Sprawdza czy zatrzymać zbieranie"""
        # Minimalna długość
        if elapsed < self.min_duration:
            return False
        
        # Zbyt wiele duplikatów z rzędu
        if self.consecutive_duplicates > self.max_duplicates:
            print(f"\n  🛑 STOP: {self.consecutive_duplicates} duplikatów z rzędu")
            self.should_stop = True
            return True
        
        # NOWY WARUNEK: Jeśli stosunek duplikatów do nowych wydarzeń osiągnął próg
        if self.new_events_count > 0:
            ratio = self.consecutive_duplicates / self.new_events_count
            if ratio >= self.duplication_ratio:
                print(f"\n  🔄 ZMIANA MUXA: Stosunek duplikatów do nowych = {ratio:.1f} (próg: {self.duplication_ratio})")
                self.should_stop = True
                return True
        
        # Brak nowych wydarzeń od dawna
        time_since_last = time.time() - self.last_new_event_time
        if time_since_last > self.no_new_timeout:
            print(f"\n  🛑 STOP: Brak nowych wydarzeń od {time_since_last:.0f}s")
            self.should_stop = True
            return True
        
        return False
    
    def _print_status(self, elapsed, max_duration):
        """Wyświetla status"""
        progress = min(100, int(elapsed / max_duration * 100))
        time_since_last = time.time() - self.last_new_event_time
        
        # Dodaj informację o stosunku nowych do duplikatów
        if self.new_events_count > 0:
            ratio = self.consecutive_duplicates / self.new_events_count
            ratio_info = f"| 📊 {ratio:.1f}x"
        else:
            ratio_info = ""
        
        print(f"\r  ⏱ {elapsed:.0f}s | "
              f"📦 {self.total_packets} pkt | "
              f"🔍 {self.analysis_count} sekcji | "
              f"✨ {self.new_events_count} nowych | "
              f"♻️ {self.duplicate_count} dupl | "
              f"🔄 {self.consecutive_duplicates} z rzędu {ratio_info} | "
              f"⏳ {time_since_last:.0f}s", end='', flush=True)
    
    def _print_final_status(self):
        """Wyświetla finalne statystyki"""
        elapsed = time.time() - self.start_time
        print(f"\n  ✓ Zakończono po {elapsed:.1f}s")
        print(f"  📊 Pakiety: {self.total_packets}, Sekcje: {self.analysis_count}")
        print(f"  ✨ Nowe wydarzenia: {self.new_events_count}")
        print(f"  ♻️ Duplikaty: {self.duplicate_count}")
        print(f"  🔄 Ostatnie duplikaty z rzędu: {self.consecutive_duplicates}")
        
        # Dodaj informację o powodzie zatrzymania
        if self.new_events_count > 0 and self.consecutive_duplicates >= self.new_events_count:
            print(f"  📝 Zatrzymano: liczba duplikatów równa liczbie nowych wydarzeń")
        
        print(f"  📺 Kanały: {len(self.services)}")
    
    def get_results(self):
        """Zwraca wyniki analizy"""
        return self.events, self.services


# ============================================================================
# PARSERY DVB (zmodyfikowane dla single section)
# ============================================================================

class EITParser:
    """Parser tabel EIT z obsługą pojedynczych sekcji"""
    
    EIT_ACTUAL_PF = 0x4E
    EIT_OTHER_PF = 0x4F
    EIT_ACTUAL_SCHEDULE_START = 0x50
    EIT_ACTUAL_SCHEDULE_END = 0x5F
    
    def __init__(self):
        self.events = []
    
    def parse_section_single(self, data):
        """Parsuje pojedynczą sekcję EIT i zwraca wydarzenia"""
        events = []
        
        if len(data) < 14:
            return events
        
        table_id = data[0]
        
        if not (table_id == self.EIT_ACTUAL_PF or 
                table_id == self.EIT_OTHER_PF or
                (self.EIT_ACTUAL_SCHEDULE_START <= table_id <= self.EIT_ACTUAL_SCHEDULE_END)):
            return events
        
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        service_id = (data[3] << 8) | data[4]
        current_next = data[5] & 0x01
        
        if not current_next:
            return events
        
        pos = 14
        while pos < min(len(data) - 4, 3 + section_length):
            if pos + 12 > len(data):
                break
            
            event = self.parse_event(data[pos:], service_id)
            if event:
                events.append(event)
                pos += event['_length']
            else:
                break
        
        return events
    
    def parse_event(self, data, service_id):
        """Parsuje wydarzenie z EIT"""
        if len(data) < 12:
            return None
        
        event_id = (data[0] << 8) | data[1]
        start_time = self.parse_dvb_time(data[2:7])
        duration = self.parse_dvb_duration(data[7:10])
        
        if not start_time or not duration:
            return None
        
        descriptors_length = ((data[10] & 0x0F) << 8) | data[11]
        
        if len(data) < 12 + descriptors_length:
            return None
        
        desc_data = data[12:12+descriptors_length]
        descriptors = self.parse_descriptors(desc_data)
        
        stop_time = start_time + duration
        
        event = {
            'service_id': service_id,
            'event_id': event_id,
            'start': start_time.strftime('%Y%m%d%H%M%S +0000'),
            'stop': stop_time.strftime('%Y%m%d%H%M%S +0000'),
            'start_dt': start_time,  # Dla klucza
            'title': descriptors.get('title', 'Brak tytułu'),
            'desc': descriptors.get('description', ''),
            '_length': 12 + descriptors_length
        }
        
        return event
    
    def parse_dvb_time(self, data):
        """Parsuje czas DVB (MJD + BCD)"""
        if len(data) != 5:
            return None
        
        mjd = (data[0] << 8) | data[1]
        
        if mjd == 0xFFFF:
            return None
        
        y_prime = int((mjd - 15078.2) / 365.25)
        m_prime = int((mjd - 14956.1 - int(y_prime * 365.25)) / 30.6001)
        day = mjd - 14956 - int(y_prime * 365.25) - int(m_prime * 30.6001)
        
        k = 1 if (m_prime == 14 or m_prime == 15) else 0
        year = y_prime + k + 1900
        month = m_prime - 1 - k * 12
        
        hour = self.bcd_to_int(data[2])
        minute = self.bcd_to_int(data[3])
        second = self.bcd_to_int(data[4])
        
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    
    def parse_dvb_duration(self, data):
        """Parsuje czas trwania (BCD)"""
        if len(data) != 3:
            return None
        
        hours = self.bcd_to_int(data[0])
        minutes = self.bcd_to_int(data[1])
        seconds = self.bcd_to_int(data[2])
        
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
    
    def bcd_to_int(self, bcd):
        """BCD -> int"""
        return ((bcd >> 4) * 10) + (bcd & 0x0F)
    
    def parse_descriptors(self, data):
        """Parsuje deskryptory DVB z ulepszoną obsługą opisów"""
        descriptors = {}
        pos = 0
        
        while pos < len(data) - 2:
            desc_tag = data[pos]
            desc_length = data[pos + 1]
            
            if pos + 2 + desc_length > len(data):
                break
            
            desc_data = data[pos + 2:pos + 2 + desc_length]
            
            # Short Event Descriptor (0x4D) - tytuł i krótki opis
            if desc_tag == 0x4D and len(desc_data) >= 4:
                event_name_length = desc_data[3]
                
                if len(desc_data) >= 4 + event_name_length:
                    event_name = desc_data[4:4+event_name_length].decode('utf-8', errors='ignore')
                    descriptors['title'] = event_name
                    
                    text_pos = 4 + event_name_length
                    if len(desc_data) > text_pos:
                        text_length = desc_data[text_pos]
                        if len(desc_data) >= text_pos + 1 + text_length:
                            text = desc_data[text_pos+1:text_pos+1+text_length].decode('utf-8', errors='ignore')
                            # Upewnij się, że opis jest dodawany poprawnie
                            if 'description' not in descriptors:
                                descriptors['description'] = text
                            else:
                                # Jeśli już jest opis, dołącz nowy tekst
                                descriptors['description'] += ' ' + text
            
            # Extended Event Descriptor (0x4E) - długi opis (może być w wielu częściach)
            elif desc_tag == 0x4E and len(desc_data) >= 5:
                # Wyciągnij numer części i tekst
                descriptor_number = desc_data[0] >> 4
                last_descriptor_number = desc_data[0] & 0x0F
                
                # Pomiń ISO_639_language_code (3 bajty) i length_of_items
                text_pos = 5
                
                # Przetwórz items (pary item_description_length + item + item_length + item)
                items_length = desc_data[4]
                text_pos = 5 + items_length
                
                # Długość tekstu
                if text_pos < len(desc_data):
                    text_length = desc_data[text_pos]
                    if len(desc_data) >= text_pos + 1 + text_length:
                        extended_text = desc_data[text_pos+1:text_pos+1+text_length].decode('utf-8', errors='ignore')
                        
                        # Dołącz do istniejącego opisu
                        if 'description' in descriptors:
                            descriptors['description'] += ' ' + extended_text
                        else:
                            descriptors['description'] = extended_text
            
            # Content Descriptor (0x54) - kategoria programu
            elif desc_tag == 0x54 and len(desc_data) >= 2:
                content_nibble_level_1 = (desc_data[0] >> 4) & 0x0F
                content_nibble_level_2 = desc_data[0] & 0x0F
                
                # Mapowanie kategorii
                categories = {
                    0x1: 'Film/Drama',
                    0x2: 'Wiadomości',
                    0x3: 'Show/Gra',
                    0x4: 'Sport',
                    0x5: 'Dla dzieci',
                    0x6: 'Muzyka',
                    0x7: 'Sztuka/Kultura',
                    0x8: 'Społeczeństwo',
                    0x9: 'Edukacja',
                    0xA: 'Rozrywka',
                }
                
                if content_nibble_level_1 in categories:
                    descriptors['category'] = categories[content_nibble_level_1]
            
            # Parental Rating Descriptor (0x55) - ocena wiekowa
            elif desc_tag == 0x55 and len(desc_data) >= 4:
                rating = desc_data[3]
                if rating > 0 and rating < 0x10:
                    descriptors['rating'] = f"{rating + 3}+"
            
            pos += 2 + desc_length
        
        # Upewnij się, że opis nie jest pusty
        if 'description' in descriptors and not descriptors['description'].strip():
            del descriptors['description']
        
        return descriptors


class SDTParser:
    """Parser tabel SDT z obsługą pojedynczych sekcji"""
    
    SDT_ACTUAL = 0x42
    SDT_OTHER = 0x46
    
    def __init__(self):
        self.services = {}
    
    def parse_section_single(self, data):
        """Parsuje pojedynczą sekcję SDT"""
        services = {}
        
        if len(data) < 11:
            return services
        
        table_id = data[0]
        
        if table_id != self.SDT_ACTUAL and table_id != self.SDT_OTHER:
            return services
        
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        
        pos = 11
        while pos < min(len(data) - 4, 3 + section_length):
            if pos + 5 > len(data):
                break
            
            service_id = (data[pos] << 8) | data[pos + 1]
            descriptors_length = ((data[pos + 3] & 0x0F) << 8) | data[pos + 4]
            
            if pos + 5 + descriptors_length > len(data):
                break
            
            desc_data = data[pos + 5:pos + 5 + descriptors_length]
            service_info = self.parse_service_descriptors(desc_data)
            
            if service_info:
                services[service_id] = service_info
            
            pos += 5 + descriptors_length
        
        return services
    
    def parse_service_descriptors(self, data):
        """Parsuje deskryptory usługi"""
        pos = 0
        service_info = {}
        
        while pos < len(data) - 2:
            desc_tag = data[pos]
            desc_length = data[pos + 1]
            
            if pos + 2 + desc_length > len(data):
                break
            
            desc_data = data[pos + 2:pos + 2 + desc_length]
            
            # Service Descriptor (0x48)
            if desc_tag == 0x48 and len(desc_data) >= 3:
                service_type = desc_data[0]
                provider_name_length = desc_data[1]
                
                if len(desc_data) >= 2 + provider_name_length:
                    provider_name = desc_data[2:2+provider_name_length].decode('utf-8', errors='ignore')
                    
                    service_name_pos = 2 + provider_name_length
                    if len(desc_data) > service_name_pos:
                        service_name_length = desc_data[service_name_pos]
                        if len(desc_data) >= service_name_pos + 1 + service_name_length:
                            service_name = desc_data[service_name_pos+1:service_name_pos+1+service_name_length].decode('utf-8', errors='ignore')
                            
                            service_info = {
                                'type': service_type,
                                'provider': provider_name,
                                'name': service_name
                            }
            
            pos += 2 + desc_length
        
        return service_info


# ============================================================================
# M3U PARSER (bez zmian - wyciąg)
# ============================================================================

class M3UParser:
    """Parser plików M3U z grupowaniem po multipleksach"""
    
    def __init__(self, m3u_file):
        self.m3u_file = m3u_file
        self.channels = []
        self.multiplexes = {}
    
    def parse(self):
        """Parsuje plik M3U"""
        with open(self.m3u_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        current_channel = {}
        
        for line in lines:
            line = line.strip()
            
            if line.startswith('#EXTINF:'):
                match = re.search(r'#EXTINF:-?\d+[^,]*,(.+)', line)
                if match:
                    current_channel['name'] = match.group(1).strip()
                
                tvg_id = re.search(r'tvg-id="([^"]+)"', line)
                if tvg_id:
                    current_channel['tvg_id'] = tvg_id.group(1)
                    
            elif line.startswith('rtsp://') or line.startswith('http://'):
                current_channel['url'] = line
                params = self.parse_satip_url(line)
                current_channel.update(params)
                
                if current_channel.get('name'):
                    self.channels.append(current_channel.copy())
                    
                    mux_key = self.get_mux_key(current_channel)
                    if mux_key not in self.multiplexes:
                        self.multiplexes[mux_key] = {
                            'params': current_channel.copy(),
                            'channels': []
                        }
                    self.multiplexes[mux_key]['channels'].append(current_channel['name'])
                
                current_channel = {}
        
        return self.channels
    
    def get_mux_key(self, channel):
        """Generuje klucz multipleksu"""
        if channel.get('msys') in ['dvbs', 'dvbs2']:
            return f"{channel.get('freq')}_{channel.get('pol')}_{channel.get('sr')}"
        else:
            return f"{channel.get('freq')}_{channel.get('bw')}_{channel.get('msys')}"
    
    def parse_satip_url(self, url):
        """Parsuje parametry SAT>IP z URL"""
        params = {}
        
        if '?' in url:
            query = url.split('?')[1]
            
            for param in query.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    params[key] = value
        
        return params
    
    def get_multiplexes(self):
        """Zwraca multipleksy"""
        return self.multiplexes


# ============================================================================
# GŁÓWNY GRABBER (zmodyfikowany)
# ============================================================================

class EPGGrabber:
    """Grabber EPG z analizą w czasie rzeczywistym"""
    
    PID_PAT = 0x0000
    PID_NIT = 0x0010
    PID_SDT = 0x0011
    PID_EIT = 0x0012
    PID_TDT = 0x0014
    
    EPG_PIDS = [PID_PAT, PID_NIT, PID_SDT, PID_EIT, PID_TDT]
    
    def __init__(self, host='192.168.1.1', port=8080):
        self.host = host
        self.port = port
        self.base_url = f"http://{self.host}:{self.port}"
        self.all_events = []
        self.all_services = {}
        self.m3u_channels = {}
    
    def set_m3u_channels(self, m3u_channels):
        """Ustawia mapowanie kanałów z M3U"""
        self.m3u_channels = m3u_channels
    
    def load_existing_epg(self, epg_file):
        """Wczytuje istniejące EPG do wykrywania duplikatów"""
        existing_events = set()
        
        if os.path.exists(epg_file):
            try:
                tree = ET.parse(epg_file)
                root = tree.getroot()
                
                for programme in root.findall('programme'):
                    channel = programme.get('channel')
                    start = programme.get('start')
                    
                    if channel and start:
                        # Konwertuj na format klucza
                        try:
                            service_id = int(channel)
                            # Wyciągnij z start timestamp (format: YYYYMMDDHHMMSS +ZZZZ)
                            start_str = start.split()[0]
                            event_key = (service_id, start_str)
                            existing_events.add(event_key)
                        except:
                            pass
                
                print(f"✓ Wczytano {len(existing_events)} istniejących wydarzeń")
                return existing_events
                
            except Exception as e:
                print(f"⚠ Błąd wczytywania istniejącego EPG: {e}")
        
        return existing_events
    
    def scan_multiplexes(self, multiplexes, cache_dir=None, existing_epg_file=None, max_duration=300):
        """Skanuje multipleksy z analizą realtime"""
        total = len(multiplexes)
        
        print(f"\n🔍 Znaleziono {total} unikalnych multipleksów")
        print(f"⚡ Analiza w czasie rzeczywistym\n")
        
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            print(f"💾 Katalog cache: {cache_dir}\n")
        
        # Wczytaj istniejące EPG
        existing_events = self.load_existing_epg(existing_epg_file) if existing_epg_file else set()
        
        for idx, (mux_key, mux_data) in enumerate(multiplexes.items(), 1):
            channels = mux_data['channels']
            params = mux_data['params']
            
            freq = params.get('freq', 'N/A')
            msys = params.get('msys', 'N/A')
            
            print(f"[{idx}/{total}] Multipleks: {freq} MHz ({msys.upper()})")
            print(f"  Kanały ({len(channels)}): {', '.join(channels[:3])}", end='')
            if len(channels) > 3:
                print(f" ... (+{len(channels)-3})")
            else:
                print()
            
            # Zbierz i analizuj natychmiast
            events, services = self.grab_and_analyze_realtime(
                params, cache_dir, existing_events, max_duration
            )
            
            if events:
                self.all_events.extend(events)
                self.all_services.update(services)
                unique_services = len(set(e['service_id'] for e in events))
                print(f"  ✓ Zebrano {len(events)} wydarzeń z {unique_services} kanałów")
            else:
                print(f"  ⚠ Brak danych EPG")
            
            if idx < total:
                print()
                time.sleep(0.5)
        
        print(f"\n✓ Skanowanie zakończone")
        return self.organize_epg()
    
    def grab_and_analyze_realtime(self, mux_params, cache_dir, existing_events, max_duration=300):
        """Zbiera strumień i analizuje w czasie rzeczywistym"""
        stream_url = self.build_stream_url(mux_params)
        
        cache_file = None
        if cache_dir:
            freq = mux_params.get('freq', 'unknown')
            timestamp = int(time.time())
            cache_file = os.path.join(cache_dir, f"mux_{freq}_{timestamp}.ts")
        
        # Analizator realtime
        analyzer = RealtimeStreamAnalyzer(cache_file, existing_events)
        
        # Ustaw parametry z globalnej konfiguracji
        analyzer.duplication_ratio = RealtimeStreamAnalyzer.duplication_ratio
        
        # Uruchom analizę
        success = analyzer.process_stream(stream_url, max_duration=max_duration)
        
        if not success:
            return [], {}
        
        # Pobierz wyniki
        events, services = analyzer.get_results()
        
        return events, services
    
    def build_stream_url(self, mux_params):
        """Buduje URL strumienia"""
        params = {
            'freq': mux_params.get('freq'),
            'bw': mux_params.get('bw'),
            'msys': mux_params.get('msys', 'dvbt2'),
            'tmode': mux_params.get('tmode'),
            'gi': mux_params.get('gi'),
            'pids': ','.join([str(pid) for pid in self.EPG_PIDS])
        }
        
        if mux_params.get('msys') in ['dvbs', 'dvbs2']:
            params.update({
                'src': mux_params.get('src', '1'),
                'pol': mux_params.get('pol'),
                'sr': mux_params.get('sr'),
                'mtype': mux_params.get('mtype', '8psk'),
                'plts': mux_params.get('plts', 'on'),
                'ro': mux_params.get('ro', '0.35'),
                'fec': mux_params.get('fec', '23')
            })
        
        params = {k: v for k, v in params.items() if v is not None}
        query = '&'.join([f"{k}={v}" for k, v in params.items()])
        
        return f"{self.base_url}/?{query}"
    
    def organize_epg(self):
        """Organizuje zebrane EPG per service_id"""
        epg_data = defaultdict(list)
        
        for event in self.all_events:
            service_id = event['service_id']
            epg_data[service_id].append(event)
        
        # Sortuj wydarzenia po czasie
        for service_id in epg_data:
            epg_data[service_id].sort(key=lambda e: e['start'])
        
        return epg_data
    
    def export_to_xmltv(self, output_file='epg.xml'):
        """Eksportuje EPG do XMLTV z pełnymi nazwami kanałów"""
        epg_data = self.organize_epg()
        
        tv = ET.Element('tv')
        tv.set('generator-info-name', 'minisatip-epg-grabber-realtime')
        tv.set('generator-info-url', 'https://github.com/catalinii/minisatip')
        
        # Dodaj kanały z pełnymi nazwami
        for service_id in epg_data.keys():
            channel_elem = ET.SubElement(tv, 'channel')
            channel_elem.set('id', str(service_id))
            
            display_name = ET.SubElement(channel_elem, 'display-name')
            
            # Najpierw spróbuj wziąć nazwę z SDT
            if service_id in self.all_services:
                name = self.all_services[service_id]['name']
                name = self.clean_xml_text(name)
                display_name.text = name
            else:
                # Jeśli nie ma w SDT, spróbuj znaleźć w M3U po service_id
                if str(service_id) in self.m3u_channels:
                    name = self.m3u_channels[str(service_id)]
                    name = self.clean_xml_text(name)
                    display_name.text = name
                else:
                    display_name.text = f"Service {service_id}"
        
        # Dodaj programy
        for service_id, events in epg_data.items():
            for event in events:
                programme = ET.SubElement(tv, 'programme')
                programme.set('channel', str(service_id))
                programme.set('start', event['start'])
                programme.set('stop', event['stop'])
                
                title = ET.SubElement(programme, 'title')
                title.set('lang', 'pl')
                title.text = self.clean_xml_text(event['title'])
                
                if event.get('desc'):
                    desc = ET.SubElement(programme, 'desc')
                    desc.set('lang', 'pl')
                    desc.text = self.clean_xml_text(event['desc'])
                
                # Dodaj kategorię jeśli jest
                if event.get('category'):
                    category = ET.SubElement(programme, 'category')
                    category.set('lang', 'pl')
                    category.text = event['category']
                
                # Dodaj rating jeśli jest
                if event.get('rating'):
                    rating = ET.SubElement(programme, 'rating')
                    rating.set('system', 'PL')
                    value = ET.SubElement(rating, 'value')
                    value.text = event['rating']
        
        # Zapisz
        tree = ET.ElementTree(tv)
        ET.indent(tree, space='  ')
        tree.write(output_file, encoding='utf-8', xml_declaration=True)
        
        print(f"\n💾 EPG zapisane do: {output_file}")
        print(f"   Kanały: {len(epg_data)}")
        total_events = sum(len(events) for events in epg_data.values())
        print(f"   Wydarzenia: {total_events}")
        
        # Zapisz mapowanie kanałów
        mapping_file = output_file.replace('.xml', '_mapping.json')
        self.export_channel_mapping(mapping_file)
    
    def export_channel_mapping(self, mapping_file):
        """Eksportuje mapowanie service_id -> nazwa kanału"""
        mapping = {}
        
        for service_id, service_info in self.all_services.items():
            mapping[str(service_id)] = {
                'name': service_info['name'],
                'provider': service_info.get('provider', ''),
                'type': service_info.get('type', 0)
            }
        
        with open(mapping_file, 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        
        print(f"   Mapowanie: {mapping_file}")
    
    def clean_xml_text(self, text):
        """Usuwa niepoprawne znaki XML"""
        if not text:
            return ""
        
        import re
        text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x84\x86-\x9F]', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='EPG Grabber z analizą w czasie rzeczywistym',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Przykłady:
  # Pokaż multipleksy
  %(prog)s -m playlist.m3u --show-muxes
  
  # Analiza realtime
  %(prog)s -m playlist.m3u -o epg.xml
  
  # Z cache strumienia
  %(prog)s -m playlist.m3u --cache /tmp/epg_cache
  
  # Zdalny serwer
  %(prog)s -m playlist.m3u -a 192.168.1.100:8080
  
  # Konfiguracja timeoutów
  %(prog)s -m playlist.m3u --min-duration 30 --no-new-timeout 60
  
  # Zmień muxa gdy duplikaty = nowe
  %(prog)s -m playlist.m3u --duplication-ratio 1.0
  
  # Zmień muxa gdy 2x więcej duplikatów niż nowych
  %(prog)s -m playlist.m3u --duplication-ratio 2.0
        """
    )
    parser.add_argument(
        '-m', '--m3u',
        required=True,
        help='Ścieżka do pliku M3U'
    )
    parser.add_argument(
        '-o', '--output',
        default='epg.xml',
        help='Plik wyjściowy XMLTV (domyślnie: epg.xml)'
    )
    parser.add_argument(
        '-H', '--host',
        default='192.168.1.1',
        help='Host/IP minisatip (domyślnie: 192.168.1.1)'
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=8080,
        help='Port minisatip HTTP (domyślnie: 8080)'
    )
    parser.add_argument(
        '-a', '--address',
        help='Pełny adres IP:PORT (zastępuje -H i -p), np. 192.168.1.1:8080'
    )
    parser.add_argument(
        '--cache',
        help='Katalog do zapisywania surowych strumieni TS (opcjonalne)'
    )
    parser.add_argument(
        '--show-muxes',
        action='store_true',
        help='Pokaż znalezione multipleksy i zakończ'
    )
    parser.add_argument(
        '--min-duration',
        type=int,
        default=30,
        help='Minimalny czas zbierania w sekundach (domyślnie: 30)'
    )
    parser.add_argument(
        '--no-new-timeout',
        type=int,
        default=60,
        help='Zatrzymaj po X sekundach bez nowych wydarzeń (domyślnie: 60)'
    )
    parser.add_argument(
        '--max-duplicates',
        type=int,
        default=100,
        help='Zatrzymaj po X duplikatach z rzędu (domyślnie: 100)'
    )
    parser.add_argument(
        '--days',
        type=int,
        default=1,
        help='Liczba dni EPG do zebrania (domyślnie: 1, max: 7)'
    )
    parser.add_argument(
        '--duplication-ratio',
        type=float,
        default=1.0,
        help='Zmień muxa gdy stosunek duplikatów do nowych wydarzeń osiągnie X (domyślnie: 1.0)'
    )
    
    args = parser.parse_args()
    
    # Obsługa adresu
    if args.address:
        if ':' in args.address:
            host, port = args.address.rsplit(':', 1)
            try:
                port = int(port)
            except ValueError:
                print(f"✗ Błędny format: {args.address}")
                print("  Użyj: IP:PORT, np. 192.168.1.1:8080")
                sys.exit(1)
        else:
            host = args.address
            port = args.port
    else:
        host = args.host
        port = args.port
    
    print("=" * 70)
    print("  EPG GRABBER - Analiza w czasie rzeczywistym")
    print("=" * 70)
    print(f"🌐 Serwer: {host}:{port}")
    
    # Parsuj M3U
    print(f"📄 Parsowanie: {args.m3u}")
    m3u_parser = M3UParser(args.m3u)
    channels = m3u_parser.parse()
    
    if not channels:
        print("✗ Nie znaleziono kanałów w pliku M3U")
        sys.exit(1)
    
    print(f"✓ Znaleziono {len(channels)} kanałów")
    
    # Stwórz mapowanie kanałów z M3U
    m3u_channels = {}
    for channel in channels:
        if channel.get('tvg_id'):
            m3u_channels[channel['tvg_id']] = channel['name']
    
    multiplexes = m3u_parser.get_multiplexes()
    print(f"✓ Pogrupowano do {len(multiplexes)} multipleksów")
    
    # Opcja: tylko pokaż multipleksy
    if args.show_muxes:
        print("\n" + "=" * 70)
        print("ZNALEZIONE MULTIPLEKSY:")
        print("=" * 70)
        
        for idx, (mux_key, mux_data) in enumerate(multiplexes.items(), 1):
            params = mux_data['params']
            channels_list = mux_data['channels']
            
            print(f"\n[{idx}] {mux_key}")
            print(f"    Częstotliwość: {params.get('freq')} MHz")
            print(f"    System: {params.get('msys', 'N/A').upper()}")
            
            if params.get('bw'):
                print(f"    Szerokość: {params.get('bw')} MHz")
            if params.get('pol'):
                print(f"    Polaryzacja: {params.get('pol')}")
            if params.get('sr'):
                print(f"    Symbol rate: {params.get('sr')}")
            
            print(f"    Kanały ({len(channels_list)}):")
            for ch in channels_list[:5]:
                print(f"      • {ch}")
            if len(channels_list) > 5:
                print(f"      ... (+{len(channels_list)-5} więcej)")
        
        print("\n" + "=" * 70)
        print("💡 Analiza w czasie rzeczywistym:")
        print(f"   - Minimalny czas: {args.min_duration}s")
        print(f"   - Timeout bez nowych: {args.no_new_timeout}s")
        print(f"   - Max duplikaty: {args.max_duplicates}")
        print(f"   - Próg zmiany muxa: {args.duplication_ratio}x")
        print("   - Natychmiastowe zatrzymanie gdy brak nowych wydarzeń")
        print("=" * 70)
        return
    
    # Sprawdź istniejące EPG
    existing_epg = args.output if os.path.exists(args.output) else None
    if existing_epg:
        print(f"📋 Wykryto istniejące EPG: {args.output}")
        print("   Będzie użyte do wykrywania duplikatów")
    
    # Ustaw parametry timeoutów globalnie
    RealtimeStreamAnalyzer.min_duration = args.min_duration
    RealtimeStreamAnalyzer.no_new_timeout = args.no_new_timeout
    RealtimeStreamAnalyzer.max_duplicates = args.max_duplicates
    RealtimeStreamAnalyzer.duplication_ratio = args.duplication_ratio
    
    # Oblicz maksymalny czas zbierania na podstawie liczby dni
    days = max(1, min(args.days, 7))  # Ogranicz do 1-7 dni
    max_duration = 120 + (days - 1) * 60  # 120s dla 1 dnia, +60s za każdy dodatkowy
    print(f"📅 Zbieranie EPG na {days} dni (max czas: {max_duration}s)")
    
    # Skanuj multipleksy
    print(f"\n⚡ Start analizy w czasie rzeczywistym")
    print(f"💡 Zatrzymanie automatyczne gdy brak nowych wydarzeń")
    
    grabber = EPGGrabber(host, port)
    grabber.set_m3u_channels(m3u_channels)
    
    # Modyfikacja: zbierz EPG wiele razy dla dłuższych zakresów
    for day_pass in range(days):
        if day_pass > 0:
            print(f"\n🔄 Przejście {day_pass + 1}/{days} - zbieranie dalszych dni...")
            time.sleep(5)
        
        epg_data = grabber.scan_multiplexes(multiplexes, args.cache, existing_epg, max_duration)
    
    # Eksportuj
    if epg_data and len(epg_data) > 0:
        grabber.export_to_xmltv(args.output)
        
        # Statystyki finalne
        print(f"\n📊 Podsumowanie:")
        print(f"   Multipleksy przeskanowane: {len(multiplexes)}")
        print(f"   Kanały z EPG: {len(epg_data)}")
        total_programs = sum(len(events) for events in epg_data.values())
        print(f"   Łączna liczba programów: {total_programs}")
        
        if args.cache:
            if os.path.exists(args.cache):
                cache_files = [f for f in os.listdir(args.cache) if f.endswith('.ts')]
                if cache_files:
                    total_size = sum(os.path.getsize(os.path.join(args.cache, f)) for f in cache_files)
                    print(f"   Cache TS: {len(cache_files)} plików ({total_size / 1024 / 1024:.1f} MB)")
    else:
        print("\n⚠ Nie zebrano żadnych danych EPG")
        print("  Możliwe przyczyny:")
        print("  1. Brak połączenia z minisatip")
        print("  2. Słaby sygnał lub błędy w strumieniu")
        print("  3. Transmisja nie zawiera danych EPG")
        print("  4. Wszystkie dane już istnieją w EPG")
        
        # Zapisz pusty plik aby zaznaczyć próbę
        if args.output:
            tv = ET.Element('tv')
            tv.set('generator-info-name', 'minisatip-epg-grabber-realtime')
            comment = ET.Comment(' Brak danych EPG - sprawdź logi powyżej ')
            tv.append(comment)
            tree = ET.ElementTree(tv)
            tree.write(args.output, encoding='utf-8', xml_declaration=True)
            print(f"  📝 Utworzono pusty {args.output}")
    
    print("\n✓ Gotowe!")
    print("=" * 70)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹ Przerwano przez użytkownika")
        sys.exit(0)