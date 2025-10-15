#!/usr/bin/env python3
"""
DVB-T/T2 Scanner w Pythonie - WERSJA ULEPSZONA Z PEŁNYM WYKRYWANIEM PIDÓW
- Pełne wykrywanie PIDów: wideo, audio, napisy, teletekst, EPG, itp.
- Inteligentne grupowanie PIDów według typu
- Poprawiona logika składania tabel DVB
- Generowanie M3U z kompletnymi listami PIDów
- Ulepszone wykrywanie nazw kanałów z SDT
- Automatyczne wykrywanie typu kodowania audio
- Obsługa multipleksów z wieloma usługami
"""

import sys
import time
import struct
import requests
import argparse
from datetime import datetime
from collections import defaultdict
from io import BytesIO
import re
import json


# ============================================================================
# PARSOWANIE MPEG-TS
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
# PARSERY DVB - ULEPSZONE WERSJE
# ============================================================================

class PATParser:
    """Parser Program Association Table"""
    
    def __init__(self):
        self.programs = {}
    
    def parse_section(self, data):
        if len(data) < 8:
            return {}
        
        table_id = data[0]
        if table_id != 0x00:
            return {}
        
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        pos = 8
        programs = {}
        
        while pos < min(len(data) - 4, 3 + section_length):
            if pos + 4 > len(data):
                break
            
            program_number = (data[pos] << 8) | data[pos + 1]
            pid = ((data[pos + 2] & 0x1F) << 8) | data[pos + 3]
            
            if program_number != 0:
                programs[program_number] = pid
            
            pos += 4
        
        return programs


class PMTParser:
    """Parser Program Map Table - ULEPSZONA WERSJA Z PEŁNYM WYKRYWANIEM STRUMIENI"""
    
    # Mapowanie typów strumieni
    STREAM_TYPES = {
        0x01: ('MPEG-1 Video', 'video'),
        0x02: ('MPEG-2 Video', 'video'),
        0x03: ('MPEG-1 Audio', 'audio'),
        0x04: ('MPEG-2 Audio', 'audio'),
        0x05: ('Private Sections', 'data'),
        0x06: ('Private PES Data', 'data'),
        0x0F: ('MPEG-4 AAC Audio', 'audio'),
        0x10: ('MPEG-4 Video', 'video'),
        0x11: ('MPEG-4 LATM AAC Audio', 'audio'),
        0x13: ('MPEG-4 Text', 'subtitle'),
        0x1B: ('H.264/AVC Video', 'video'),
        0x1C: ('MPEG-4 AAC Audio', 'audio'),
        0x1D: ('MPEG-4 Text', 'subtitle'),
        0x24: ('H.265/HEVC Video', 'video'),
        0x80: ('DigiCipher II Video', 'video'),
        0x81: ('A52/AC-3 Audio', 'audio'),
        0x82: ('HDMV DTS Audio', 'audio'),
        0x83: ('LPCM Audio', 'audio'),
        0x84: ('SDDS Audio', 'audio'),
        0x86: ('DTS-HD Audio', 'audio'),
        0x87: ('E-AC-3 Audio', 'audio'),
        0x8A: ('DTS Audio', 'audio'),
        0xD1: ('DigiCipher II Audio', 'audio'),
        0xDA: ('DTS Audio', 'audio'),
        0xDB: ('Dolby Digital Plus', 'audio'),
        0xEA: ('VC-1 Video', 'video'),
    }
    
    def __init__(self):
        self.streams = {}
    
    def parse_section(self, data):
        if len(data) < 12:
            return {}
        
        table_id = data[0]
        if table_id != 0x02:
            return {}
        
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        program_number = (data[3] << 8) | data[4]
        version_number = (data[5] >> 1) & 0x1F
        current_next = data[5] & 0x01
        
        if not current_next:
            return {}
        
        pcr_pid = ((data[8] & 0x1F) << 8) | data[9]
        program_info_length = ((data[10] & 0x0F) << 8) | data[11]
        
        pos = 12 + program_info_length
        streams = defaultdict(list)
        
        # Dodaj PCR PID jeśli nie jest zerowy
        if pcr_pid != 0x1FFF:
            streams['pcr'].append(pcr_pid)
        
        while pos < min(len(data) - 4, 3 + section_length):
            if pos + 5 > len(data):
                break
            
            stream_type = data[pos]
            elementary_pid = ((data[pos + 1] & 0x1F) << 8) | data[pos + 2]
            es_info_length = ((data[pos + 3] & 0x0F) << 8) | data[pos + 4]
            
            # Pobierz deskryptory strumienia
            descriptors = {}
            if es_info_length > 0 and pos + 5 + es_info_length <= len(data):
                desc_data = data[pos + 5:pos + 5 + es_info_length]
                descriptors = self.parse_stream_descriptors(desc_data)
            
            # Określ kategorię strumienia
            stream_info = {
                'pid': elementary_pid,
                'type': stream_type,
                'type_name': self.STREAM_TYPES.get(stream_type, (f'Unknown {stream_type:02X}', 'data'))[0],
                'category': self.STREAM_TYPES.get(stream_type, (f'Unknown {stream_type:02X}', 'data'))[1],
                'descriptors': descriptors
            }
            
            # Dodaj do odpowiedniej kategorii
            category = stream_info['category']
            streams[category].append(stream_info)
            
            pos += 5 + es_info_length
        
        return dict(streams)
    
    def parse_stream_descriptors(self, data):
        """Parsuje deskryptory strumienia"""
        pos = 0
        descriptors = {}
        
        while pos < len(data) - 2:
            desc_tag = data[pos]
            desc_length = data[pos + 1]
            
            if pos + 2 + desc_length > len(data):
                break
            
            desc_data = data[pos + 2:pos + 2 + desc_length]
            
            # Subtitling descriptor (0x59) - napisy DVB
            if desc_tag == 0x59 and len(desc_data) >= 8:
                lang = desc_data[0:3].decode('ascii', errors='ignore')
                sub_type = desc_data[3]
                composition_page = (desc_data[4] << 8) | desc_data[5]
                ancillary_page = (desc_data[6] << 8) | desc_data[7]
                descriptors['subtitle'] = {
                    'language': lang,
                    'type': sub_type,
                    'composition_page': composition_page,
                    'ancillary_page': ancillary_page
                }
            
            # Teletext descriptor (0x56) - teletekst
            elif desc_tag == 0x56 and len(desc_data) >= 5:
                lang = desc_data[0:3].decode('ascii', errors='ignore')
                teletext_type = desc_data[3]
                teletext_magazine = desc_data[4] & 0x07
                teletext_page = (desc_data[4] >> 3) & 0x1F
                descriptors['teletext'] = {
                    'language': lang,
                    'type': teletext_type,
                    'magazine': teletext_magazine,
                    'page': teletext_page
                }
            
            # Language descriptor (0x0A) - język audio
            elif desc_tag == 0x0A and len(desc_data) >= 4:
                lang = desc_data[0:3].decode('ascii', errors='ignore')
                audio_type = desc_data[3]
                descriptors['language'] = lang
                descriptors['audio_type'] = audio_type
            
            pos += 2 + desc_length
        
        return descriptors


class SDTParser:
    """Parser Service Description Table - WERSJA ULEPSZONA"""
    
    def __init__(self):
        self.services = {}
    
    def parse_section(self, data):
        if len(data) < 11:
            return {}
        
        table_id = data[0]
        if table_id not in [0x42, 0x46]: # 0x42: SDT, 0x46: SDT other
            return {}
        
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        pos = 11
        services = {}
        
        while pos < min(len(data) - 4, 3 + section_length):
            if pos + 5 > len(data):
                break
            
            service_id = (data[pos] << 8) | data[pos + 1]
            descriptors_length = ((data[pos + 3] & 0x0F) << 8) | data[pos + 4]
            
            if pos + 5 + descriptors_length > len(data):
                break
            
            desc_data = data[pos + 5:pos + 5 + descriptors_length]
            service_info = self.parse_service_descriptors(desc_data)
            
            if service_info and service_info.get('name'):
                services[service_id] = service_info
            
            pos += 5 + descriptors_length
        
        return services
    
    def parse_service_descriptors(self, data):
        pos = 0
        service_info = {}
        
        while pos + 1 < len(data):
            desc_tag = data[pos]
            desc_length = data[pos + 1]
            
            if pos + 2 + desc_length > len(data):
                break
            
            desc_data = data[pos + 2:pos + 2 + desc_length]
            
            # Service Descriptor (tag 0x48) zawiera nazwę
            if desc_tag == 0x48 and len(desc_data) >= 1:
                try:
                    service_type = desc_data[0]
                    pos_in_desc = 1
                    
                    # Provider name
                    if pos_in_desc < len(desc_data):
                        provider_name_len = desc_data[pos_in_desc]
                        pos_in_desc += 1
                        
                        # Sprawdź, czy jest bajt kodowania znaków
                        if provider_name_len > 0 and pos_in_desc < len(desc_data) and desc_data[pos_in_desc] < 0x20:
                            pos_in_desc += 1 # Pomiń bajt kodowania
                            provider_name_len -= 1
                        
                        if pos_in_desc + provider_name_len <= len(desc_data):
                            provider_name = desc_data[pos_in_desc:pos_in_desc + provider_name_len].decode('utf-8', errors='ignore').strip()
                            pos_in_desc += provider_name_len
                        else:
                            provider_name = ""
                    else:
                        provider_name = ""
                    
                    # Service name
                    if pos_in_desc < len(desc_data):
                        service_name_len = desc_data[pos_in_desc]
                        pos_in_desc += 1
                        
                        if service_name_len > 0 and pos_in_desc < len(desc_data) and desc_data[pos_in_desc] < 0x20:
                            pos_in_desc += 1 # Pomiń bajt kodowania
                            service_name_len -= 1
                        
                        if pos_in_desc + service_name_len <= len(desc_data):
                            service_name = desc_data[pos_in_desc:pos_in_desc + service_name_len].decode('utf-8', errors='ignore').strip()
                        else:
                            service_name = ""
                    else:
                        service_name = ""
                    
                    if service_name:
                        service_info = {
                            'type': service_type,
                            'provider': provider_name,
                            'name': service_name
                        }
                        break  # Znaleziono nazwę, kończymy
                except Exception:
                    # Ignoruj błędy parsowania jednego deskryptora, spróbuj z następnym
                    pass
            
            pos += 2 + desc_length
        
        return service_info


class NITParser:
    """Parser Network Information Table"""
    
    def __init__(self):
        self.transport_streams = []
    
    def parse_section(self, data):
        if len(data) < 12:
            return []
        
        table_id = data[0]
        if table_id not in [0x40, 0x41]:
            return []
        
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        network_descriptors_length = ((data[8] & 0x0F) << 8) | data[9]
        pos = 10 + network_descriptors_length
        
        if pos + 2 > len(data):
            return []
        
        transport_stream_loop_length = ((data[pos] & 0x0F) << 8) | data[pos + 1]
        pos += 2
        transport_streams = []
        end_pos = min(pos + transport_stream_loop_length, len(data) - 4)
        
        while pos < end_pos:
            if pos + 6 > len(data):
                break
            
            ts_id = (data[pos] << 8) | data[pos + 1]
            original_network_id = (data[pos + 2] << 8) | data[pos + 3]
            ts_descriptors_length = ((data[pos + 4] & 0x0F) << 8) | data[pos + 5]
            pos += 6
            
            if pos + ts_descriptors_length > len(data):
                break
            
            desc_data = data[pos:pos + ts_descriptors_length]
            descriptors = self.parse_descriptors(desc_data)
            
            if descriptors:
                transport_streams.append({
                    'ts_id': ts_id,
                    'onid': original_network_id,
                    'descriptors': descriptors
                })
            
            pos += ts_descriptors_length
        
        return transport_streams
    
    def parse_descriptors(self, data):
        pos = 0
        descriptors = {}
        
        while pos < len(data) - 2:
            desc_tag = data[pos]
            desc_length = data[pos + 1]
            
            if pos + 2 + desc_length > len(data):
                break
            
            desc_data = data[pos + 2:pos + 2 + desc_length]
            
            if desc_tag == 0x5A and len(desc_data) >= 11:
                centre_freq = (desc_data[0] << 24) | (desc_data[1] << 16) | (desc_data[2] << 8) | desc_data[3]
                centre_freq = centre_freq * 10
                
                bandwidth = (desc_data[4] >> 5) & 0x07
                bw_map = {0: 8, 1: 7, 2: 6, 3: 5}
                bw_mhz = bw_map.get(bandwidth, 8)
                
                constellation = (desc_data[5] >> 6) & 0x03
                const_map = {0: 'QPSK', 1: 'QAM16', 2: 'QAM64', 3: 'QAM256'}
                
                guard_interval = (desc_data[6] >> 3) & 0x03
                gi_map = {0: '1/32', 1: '1/16', 2: '1/8', 3: '1/4'}
                
                transmission_mode = (desc_data[6] >> 1) & 0x03
                tm_map = {0: '2k', 1: '8k', 2: '4k', 3: '1k'}
                
                descriptors['frequency'] = centre_freq
                descriptors['bandwidth'] = bw_mhz
                descriptors['constellation'] = const_map.get(constellation, 'QAM64')
                descriptors['guard_interval'] = gi_map.get(guard_interval, '1/4')
                descriptors['transmission_mode'] = tm_map.get(transmission_mode, '8k')
            
            pos += 2 + desc_length
        
        return descriptors


# ============================================================================
# SKANER MULTIPLEKSÓW - WERSJA Z PEŁNYM WYKRYWANIEM PIDÓW
# ============================================================================

class MultiplexScanner:
    """Skaner pojedynczego multipleksu z pełnym wykrywaniem PIDów"""
    
    PID_PAT = 0x0000
    PID_NIT = 0x0010
    PID_SDT = 0x0011
    PID_ALL = 8192
    
    def __init__(self, host='192.168.1.1', port=8080):
        self.host = host
        self.port = port
        self.base_url = f"http://{self.host}:{self.port}"
    
    def scan_frequency(self, freq_mhz, msys='dvbt2', bw=8, tmode='8k', gi='1/4', 
                       detection_time=5, full_scan_time=20):
        """Skanuje pojedynczą częstotliwość z pełnym wykrywaniem PIDów"""
        print(f"\n📡 Skanowanie: {freq_mhz} MHz ({msys.upper()})")
        
        if not self._detect_signal(freq_mhz, msys, bw, tmode, gi, detection_time):
            print("  ✗ Brak sygnału - częstotliwość pusta")
            return None
        
        print("  ✓ Sygnał wykryty - rozpoczynam pełny skan z wykrywaniem PIDów")
        
        result = self._full_scan(freq_mhz, msys, bw, tmode, gi, full_scan_time)
        
        if result and result.get('services'):
            print(f"  ✓ Znaleziono {len(result['services'])} stacji")
            for service in result['services'][:3]:
                pids_info = []
                if 'streams' in service:
                    for category, streams in service['streams'].items():
                        if isinstance(streams, list) and streams:
                            pids_info.append(f"{category}:{len(streams)}")
                        elif isinstance(streams, dict):
                            for cat, items in streams.items():
                                if items:
                                    pids_info.append(f"{cat}:{len(items)}")
                
                pids_str = ", ".join(pids_info) if pids_info else "brak PIDów"
                print(f"     • {service['name']} ({pids_str})")
            
            if len(result['services']) > 3:
                print(f"     ... (+{len(result['services'])-3} więcej)")
        else:
            print("  ⚠ Sygnał jest, ale brak dekodowalnych stacji")
        
        return result
    
    def _detect_signal(self, freq_mhz, msys, bw, tmode, gi, timeout):
        """Szybka detekcja sygnału"""
        url = self._build_stream_url(freq_mhz, msys, bw, tmode, gi, pids=[self.PID_ALL])
        
        try:
            response = requests.get(url, stream=True, timeout=5)
            if response.status_code != 200:
                return False
            
            start_time = time.time()
            packet_count = 0
            buffer = b''
            
            for chunk in response.iter_content(chunk_size=TSPacket.PACKET_SIZE * 50):
                buffer += chunk
                
                while len(buffer) >= TSPacket.PACKET_SIZE:
                    if buffer[0] == TSPacket.SYNC_BYTE:
                        packet_count += 1
                        buffer = buffer[TSPacket.PACKET_SIZE:]
                    else:
                        sync_pos = buffer.find(bytes([TSPacket.SYNC_BYTE]), 1)
                        if sync_pos > 0:
                            buffer = buffer[sync_pos:]
                        else:
                            buffer = b''
                
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    break
            
            response.close()
            return packet_count > 100
            
        except requests.RequestException:
            return False
    
    def _full_scan(self, freq_mhz, msys, bw, tmode, gi, timeout):
        """Pełny skan z wykrywaniem wszystkich PIDów"""
        pids_to_scan = [self.PID_ALL]
        
        url = self._build_stream_url(
            freq_mhz, msys, bw, tmode, gi, 
            pids=pids_to_scan
        )
        
        print(f"  🔗 URL: {url}")
        
        pat_parser = PATParser()
        nit_parser = NITParser()
        sdt_parser = SDTParser()
        pmt_parser = PMTParser()
        
        # Bufory na dane dla każdego PID
        pid_buffers = defaultdict(BytesIO)
        
        programs = {}
        transport_streams = []
        services = {}
        pmt_pids = set()
        pmt_streams = {}
        
        total_packets = 0
        pid_counts = defaultdict(int)
        
        pat_sections = 0
        sdt_sections = 0
        nit_sections = 0
        pmt_sections = 0
        
        # Zmienne do monitorowania stabilności danych
        previous_programs = {}
        previous_services = {}
        previous_pmt_streams = {}
        stable_cycles = 0
        min_stable_cycles = 3
        
        try:
            response = requests.get(url, stream=True, timeout=10)
            
            if response.status_code != 200:
                return None
            
            start_time = time.time()
            last_log = time.time()
            last_stability_check = time.time()
            buffer = b''
            
            print(f"  ⏳ Zbieranie danych i wykrywanie PIDów...")
            
            for chunk in response.iter_content(chunk_size=TSPacket.PACKET_SIZE * 100):
                buffer += chunk
                
                while len(buffer) >= TSPacket.PACKET_SIZE:
                    try:
                        packet = TSPacket(buffer[:TSPacket.PACKET_SIZE])
                        buffer = buffer[TSPacket.PACKET_SIZE:]
                        
                        total_packets += 1
                        pid_counts[packet.pid] += 1
                        
                        # Filtrujemy tylko interesujące nas PIDy
                        if packet.pid not in [self.PID_PAT, self.PID_NIT, self.PID_SDT] and \
                           packet.pid not in pmt_pids:
                            continue
                        
                        payload = packet.get_payload()
                        if not payload:
                            continue
                        
                        # Logika składania sekcji
                        if packet.payload_unit_start:
                            # Zakończ poprzednią sekcję i spróbuj ją sparsować
                            section_data = pid_buffers[packet.pid].getvalue()
                            if len(section_data) > 3:
                                self._parse_section_data(packet.pid, section_data, pat_parser, nit_parser, sdt_parser, pmt_parser,
                                                         programs, transport_streams, services, pmt_pids, pmt_streams,
                                                         pat_sections, sdt_sections, nit_sections, pmt_sections)
                            
                            # Zacznij nową sekcję
                            pid_buffers[packet.pid] = BytesIO()
                            pointer = payload[0]
                            
                            # Dane po pointer_field to początek nowej sekcji
                            if len(payload) > 1 + pointer:
                                pid_buffers[packet.pid].write(payload[1+pointer:])
                        else:
                            # Doklej dane do istniejącej sekcji
                            pid_buffers[packet.pid].write(payload)
                    
                    except ValueError:
                        sync_pos = buffer.find(bytes([TSPacket.SYNC_BYTE]), 1)
                        if sync_pos > 0:
                            buffer = buffer[sync_pos:]
                        else:
                            buffer = b''
                
                now = time.time()
                
                # Sprawdzanie stabilności danych co 2 sekundy
                if now - last_stability_check > 2.0:
                    has_data = len(programs) > 0 or len(services) > 0 or len(pmt_streams) > 0

                    if has_data:
                        if programs == previous_programs and services == previous_services and pmt_streams == previous_pmt_streams:
                            stable_cycles += 1
                            print(f"  ⏱ {now-start_time:.0f}s | 📦 {total_packets} pkt | "
                                  f"📋 PAT:{len(programs)}({pat_sections}) "
                                  f"SDT:{len(services)}({sdt_sections}) "
                                  f"PMT:{len(pmt_streams)}({pmt_sections}) | "
                                  f"🔄 Stabilność: {stable_cycles}/{min_stable_cycles}", 
                                  end='\r', flush=True)
                            
                            if stable_cycles >= min_stable_cycles:
                                print(f"\n  ✓ Dane stabilne przez {min_stable_cycles} cykle - kończę skanowanie")
                                break
                        else:
                            stable_cycles = 0
                            previous_programs = programs.copy()
                            previous_services = services.copy()
                            previous_pmt_streams = pmt_streams.copy()
                            
                            print(f"  ⏱ {now-start_time:.0f}s | 📦 {total_packets} pkt | "
                                  f"📋 PAT:{len(programs)}({pat_sections}) "
                                  f"SDT:{len(services)}({sdt_sections}) "
                                  f"PMT:{len(pmt_streams)}({pmt_sections}) | "
                                  f"🔄 Nowe dane", 
                                  end='\r', flush=True)
                    else:
                        stable_cycles = 0
                        previous_programs = {}
                        previous_services = {}
                        previous_pmt_streams = {}
                        print(f"  ⏱ {now-start_time:.0f}s | 📦 {total_packets} pkt | "
                              f"📋 PAT:{len(programs)}({pat_sections}) "
                              f"SDT:{len(services)}({sdt_sections}) "
                              f"PMT:{len(pmt_streams)}({pmt_sections}) | "
                              f"🔄 Oczekiwanie na dane...", 
                              end='\r', flush=True)

                    last_stability_check = now
                elif now - last_log > 2.0:
                    print(f"  ⏱ {now-start_time:.0f}s | 📦 {total_packets} pkt | "
                          f"📋 PAT:{len(programs)}({pat_sections}) "
                          f"SDT:{len(services)}({sdt_sections}) "
                          f"PMT:{len(pmt_streams)}({pmt_sections})", 
                          end='\r', flush=True)
                    last_log = now
                
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    print(f"\n  ⏱ Osiągnięto maksymalny czas skanowania ({timeout}s)")
                    break
            
            response.close()
            
            # Przetwórz ostatnie dane pozostałe w buforach
            for pid, buf in pid_buffers.items():
                section_data = buf.getvalue()
                if len(section_data) > 3:
                    self._parse_section_data(pid, section_data, pat_parser, nit_parser, sdt_parser, pmt_parser,
                                             programs, transport_streams, services, pmt_pids, pmt_streams,
                                             pat_sections, sdt_sections, nit_sections, pmt_sections)

            end_reason = "przekroczenie limitu czasu"
            if stable_cycles >= min_stable_cycles:
                end_reason = f"stabilność danych ({stable_cycles} cykle)"

            print(f"\n  ✓ Zebrano {total_packets} pakietów (koniec: {end_reason})")
            print(f"  📊 PIDs: PAT={pid_counts[self.PID_PAT]} "
                  f"NIT={pid_counts[self.PID_NIT]} "
                  f"SDT={pid_counts[self.PID_SDT]}")
            print(f"  📋 Sekcje: PAT={pat_sections} SDT={sdt_sections} NIT={nit_sections} PMT={pmt_sections}")
            
            if not services and programs:
                print("  ⚠ Znaleziono programy w PAT, ale nie udało się odczytać nazw z SDT.")
                print("     Może to być problem z implementacją SAT>IP lub niestandardowym kodowaniem znaków.")
                return None

            if not services:
                print("  ⚠ Brak danych SDT - nie wykryto stacji")
                print(f"  💡 Tip: Spróbuj zwiększyć --scan-time (obecnie {timeout}s)")
                return None
            
            if not programs:
                print("  ⚠ Brak danych PAT - nie wykryto programów")
                return None
            
            result = {
                'frequency': freq_mhz,
                'bandwidth': bw,
                'system': msys,
                'transmission_mode': tmode,
                'guard_interval': gi,
                'services': [],
                'programs': programs,
                'transport_streams': transport_streams,
                'debug_info': {
                    'total_packets': total_packets,
                    'pat_sections': pat_sections,
                    'sdt_sections': sdt_sections,
                    'nit_sections': nit_sections,
                    'pmt_sections': pmt_sections,
                    'pid_counts': dict(pid_counts),
                    'stable_cycles': stable_cycles
                }
            }
            
            # Przetwarzaj usługi z pełnymi informacjami o PIDach
            for service_id, service_info in services.items():
                pmt_pid = programs.get(service_id, 0)
                
                service_data = {
                    'id': service_id,
                    'name': service_info['name'],
                    'provider': service_info.get('provider', ''),
                    'type': service_info.get('type', 0),
                    'pmt_pid': pmt_pid,
                    'streams': {}
                }
                
                # Dodaj informacje o strumieniach z PMT
                if pmt_pid in pmt_streams:
                    service_data['streams'] = pmt_streams[pmt_pid]
                    
                    # Wyświetl szczegóły PIDów dla debugowania
                    if len(result['services']) < 3:  # Tylko dla pierwszych 3 usług
                        print(f"  🔍 {service_info['name']} PIDy:")
                        for category, streams in service_data['streams'].items():
                            if isinstance(streams, list) and streams:
                                for stream in streams:
                                    if isinstance(stream, dict):
                                        pid = stream.get('pid', 'N/A')
                                        type_name = stream.get('type_name', 'Unknown')
                                        lang = stream.get('descriptors', {}).get('language', '')
                                        lang_str = f" [{lang}]" if lang else ""
                                        print(f"     {category.upper()}: PID {pid} ({type_name}){lang_str}")
                                    else:
                                        print(f"     {category.upper()}: PID {stream}")
                
                result['services'].append(service_data)
            
            return result
            
        except requests.RequestException as e:
            print(f"\n  ✗ Błąd połączenia: {e}")
            return None

    def _parse_section_data(self, pid, data, pat_parser, nit_parser, sdt_parser, pmt_parser,
                            programs, transport_streams, services, pmt_pids, pmt_streams,
                            pat_sections, sdt_sections, nit_sections, pmt_sections):
        """Parsuje dane sekcji i aktualizuje odpowiednie zmienne"""
        try:
            if pid == self.PID_PAT:
                new_programs = pat_parser.parse_section(data)
                if new_programs:
                    programs.update(new_programs)
                    pmt_pids.update(new_programs.values())
                    pat_sections += 1
            
            elif pid == self.PID_SDT:
                new_services = sdt_parser.parse_section(data)
                if new_services:
                    for sid, s_info in new_services.items():
                        if sid not in services:
                            print(f"\n  🔍 Znaleziono SDT: SID={sid}, Nazwa='{s_info.get('name', 'N/A')}'")
                    services.update(new_services)
                    sdt_sections += 1
            
            elif pid == self.PID_NIT:
                new_ts = nit_parser.parse_section(data)
                if new_ts:
                    transport_streams.extend(new_ts)
                    nit_sections += 1
            
            elif pid in pmt_pids:
                streams = pmt_parser.parse_section(data)
                if streams:
                    if pid not in pmt_streams:
                        pmt_streams[pid] = {}
                    pmt_streams[pid].update(streams)
                    pmt_sections += 1
        except Exception:
            pass # Ignoruj błędy parsowania pojedynczej sekcji

    def _build_stream_url(self, freq_mhz, msys, bw, tmode, gi, pids):
        """Buduje URL strumienia SAT>IP"""
        params = {
            'freq': int(freq_mhz),
            'msys': msys,
            'bw': int(bw),
            'tmode': tmode,
            'gi': gi,
            'pids': ','.join([str(pid) for pid in pids])
        }
        
        query = '&'.join([f"{k}={v}" for k, v in params.items()])
        return f"http://{self.host}:{self.port}/?{query}"


# ============================================================================
# AUTOSKAN
# ============================================================================

class AutoScanner:
    """Autoskan pełnego pasma DVB-T/T2"""
    
    VHF_CHANNELS = {
        5: 177.5, 6: 184.5, 7: 191.5, 8: 198.5, 9: 205.5,
        10: 212.5, 11: 219.5, 12: 226.5
    }
    
    UHF_CHANNELS = {
        ch: 306 + (ch - 21) * 8
        for ch in range(21, 70)
    }
    
    def __init__(self, host='192.168.1.1', port=8080):
        self.scanner = MultiplexScanner(host, port)
        self.found_muxes = []
    
    def scan_all(self, vhf=True, uhf=True, msys='dvbt2', 
                 detection_time=3, full_scan_time=20):
        """Skanuje pełne pasmo VHF/UHF"""
        channels_to_scan = []
        
        if vhf:
            channels_to_scan.extend(sorted(self.VHF_CHANNELS.items()))
        
        if uhf:
            channels_to_scan.extend(sorted(self.UHF_CHANNELS.items()))
        
        if not channels_to_scan:
            print("✗ Nie wybrano żadnych kanałów do skanowania")
            return []
        
        total = len(channels_to_scan)
        systems = ['dvbt2', 'dvbt'] if msys == 'both' else [msys]
        
        print(f"\n{'='*70}")
        print(f"  AUTOSKAN DVB-T/T2 - PEŁNE WYKRYWANIE PIDÓW")
        print(f"{'='*70}")
        print(f"Zakres: {'VHF ' if vhf else ''}{'UHF' if uhf else ''}")
        print(f"Systemy: {', '.join(s.upper() for s in systems)}")
        print(f"Kanałów do przeskanowania: {total * len(systems)}")
        print(f"Timeout pełnego skanu: {full_scan_time}s (zwiększ jeśli brak wyników!)")
        print(f"Szacowany czas: ~{total * len(systems) * (detection_time + 2) / 60:.1f} min")
        print(f"{'='*70}\n")
        
        found_count = 0
        
        for idx, (ch_num, freq) in enumerate(channels_to_scan, 1):
            band = 'VHF' if freq < 300 else 'UHF'
            print(f"[{idx}/{total}] Kanał {ch_num} ({band})")
            
            for sys in systems:
                bw = 7 if freq < 300 else 8
                
                result = self.scanner.scan_frequency(
                    freq, sys, bw, '8k', '1/4',
                    detection_time, full_scan_time
                )
                
                if result:
                    self.found_muxes.append(result)
                    found_count += 1
        
        print(f"\n{'='*70}")
        print(f"  WYNIKI SKANOWANIA")
        print(f"{'='*70}")
        print(f"Przeskanowano: {total} kanałów")
        print(f"Znaleziono: {found_count} multipleksów")
        
        if self.found_muxes:
            total_services = sum(len(mux['services']) for mux in self.found_muxes)
            print(f"Łączna liczba stacji: {total_services}")
        
        print(f"{'='*70}\n")
        
        return self.found_muxes
    
    def export_to_m3u(self, output_file='channels.m3u'):
        """Eksportuje znalezione kanały do M3U z pełnymi listami PIDów"""
        if not self.found_muxes:
            print("Brak multipleksów do eksportu")
            return
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
            
            for mux in self.found_muxes:
                freq = int(mux['frequency'])
                msys = mux['system']
                bw = int(mux['bandwidth'])
                tmode = mux['transmission_mode']
                gi = mux['guard_interval']
                
                for service in mux['services']:
                    service_id = service['id']
                    name = service['name']
                    pmt_pid = service.get('pmt_pid', 0)
                    
                    # Zbierz wszystkie PIDy dla tej usługi
                    all_pids = set()
                    
                    # Dodaj podstawowe PIDy
                    all_pids.add(0)  # PAT
                    
                    # Dodaj PMT PID
                    if pmt_pid > 0:
                        all_pids.add(pmt_pid)
                    
                    # Dodaj wszystkie strumienie z PMT
                    if 'streams' in service:
                        for category, streams in service['streams'].items():
                            if isinstance(streams, list):
                                for stream in streams:
                                    if isinstance(stream, dict):
                                        all_pids.add(stream.get('pid', 0))
                                    else:
                                        all_pids.add(stream)
                            elif isinstance(streams, dict):
                                for stream in streams.values():
                                    if isinstance(stream, dict):
                                        all_pids.add(stream.get('pid', 0))
                                    else:
                                        all_pids.add(stream)
                    
                    # Dodaj standardowe PIDy EPG
                    all_pids.update([16, 17, 18, 20])  # EIT, SDT, TDT, NIT
                    
                    # Usuń PID 0 z listy (już dodany na początku)
                    all_pids.discard(0)
                    
                    # Konwertuj na posortowaną listę
                    pids_list = sorted([p for p in all_pids if p > 0])
                    
                    # Dodaj 0 na początku listy
                    pids_str = ','.join(['0'] + [str(p) for p in pids_list])
                    
                    # Zapisz do M3U
                    f.write(f'#EXTINF:-1 tvg-id="{service_id}",{name}\n')
                    
                    # Buduj URL SAT>IP
                    url = (f"rtsp://{self.scanner.host}:554/"
                           f"?freq={freq}&msys={msys}&bw={bw}&tmode={tmode}&gi={gi}"
                           f"&pids={pids_str}")
                    
                    f.write(f'{url}\n')
        
        print(f"💾 Zapisano do: {output_file}")
        
        # Statystyki
        total_services = sum(len(mux['services']) for mux in self.found_muxes)
        print(f"   Multipleksy: {len(self.found_muxes)}")
        print(f"   Stacje: {total_services}")
        
        # Zapisz szczegółowe informacje o PIDach do pliku JSON
        json_file = output_file.replace('.m3u', '_pids.json')
        self.export_pids_info(json_file)
    
    def export_pids_info(self, json_file):
        """Eksportuje szczegółowe informacje o PIDach do pliku JSON"""
        pids_info = {
            'scan_time': datetime.now().isoformat(),
            'multiplexes': []
        }
        
        for mux in self.found_muxes:
            mux_info = {
                'frequency': mux['frequency'],
                'system': mux['system'],
                'bandwidth': mux['bandwidth'],
                'services': []
            }
            
            for service in mux['services']:
                service_info = {
                    'id': service['id'],
                    'name': service['name'],
                    'provider': service.get('provider', ''),
                    'pmt_pid': service.get('pmt_pid', 0),
                    'streams': service.get('streams', {})
                }
                mux_info['services'].append(service_info)
            
            pids_info['multiplexes'].append(mux_info)
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(pids_info, f, indent=2, ensure_ascii=False)
        
        print(f"   Szczegóły PIDów: {json_file}")


# ============================================================================
# SKAN Z PLIKU KONFIGURACYJNEGO
# ============================================================================

class ConfigScanner:
    """Scanner z pliku konfiguracyjnego (format Linux DVB)"""
    
    def __init__(self, host='192.168.1.1', port=8080):
        self.scanner = MultiplexScanner(host, port)
        self.found_muxes = []
    
    def parse_config(self, config_file):
        """
        Parsuje plik konfiguracyjny DVB
        Format: [CHANNEL] z parametrami
        """
        channels = []
        current_channel = {}
        
        with open(config_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                
                if not line or line.startswith('#'):
                    continue
                
                if line == '[CHANNEL]':
                    if current_channel:
                        channels.append(current_channel)
                    current_channel = {}
                    continue
                
                match = re.match(r'(\w+)\s*=\s*(.+)', line)
                if match:
                    key, value = match.groups()
                    current_channel[key] = value
        
        if current_channel:
            channels.append(current_channel)
        
        return channels
    
    def scan_from_config(self, config_file, detection_time=3, full_scan_time=10):
        """Skanuje multipleksy z pliku konfiguracyjnego"""
        print(f"\n{'='*70}")
        print(f"  SKANOWANIE Z PLIKU KONFIGURACYJNEGO - PEŁNE WYKRYWANIE PIDÓW")
        print(f"{'='*70}")
        print(f"Plik: {config_file}\n")
        
        channels = self.parse_config(config_file)
        
        if not channels:
            print("✗ Nie znaleziono kanałów w pliku konfiguracyjnym")
            return []
        
        print(f"✓ Znaleziono {len(channels)} definicji kanałów\n")
        
        found_count = 0
        
        for idx, channel_def in enumerate(channels, 1):
            freq_hz = int(channel_def.get('FREQUENCY', 0))
            freq_mhz = freq_hz / 1_000_000
            
            bw_hz = int(channel_def.get('BANDWIDTH_HZ', 8_000_000))
            bw = bw_hz / 1_000_000
            
            msys = channel_def.get('DELIVERY_SYSTEM', 'DVBT2').lower()
            if msys == 'dvbt':
                msys = 'dvbt'
            else:
                msys = 'dvbt2'
            
            tmode = channel_def.get('TRANSMISSION_MODE', '8K')
            if tmode == '8K':
                tmode = '8k'
            elif tmode == '2K':
                tmode = '2k'
            elif tmode == '4K':
                tmode = '4k'
            else:
                tmode = '8k'
            
            gi = channel_def.get('GUARD_INTERVAL', '1/4')
            
            print(f"[{idx}/{len(channels)}]")
            
            result = self.scanner.scan_frequency(
                freq_mhz, msys, int(bw), tmode, gi,
                detection_time, full_scan_time
            )
            
            if result:
                self.found_muxes.append(result)
                found_count += 1
        
        print(f"\n{'='*70}")
        print(f"  WYNIKI SKANOWANIA")
        print(f"{'='*70}")
        print(f"Przeskanowano: {len(channels)} definicji")
        print(f"Znaleziono: {found_count} multipleksów")
        
        if self.found_muxes:
            total_services = sum(len(mux['services']) for mux in self.found_muxes)
            print(f"Łączna liczba stacji: {total_services}")
        
        print(f"{'='*70}\n")
        
        return self.found_muxes
    
    def export_to_m3u(self, output_file='channels.m3u'):
        """Eksportuje znalezione kanały do M3U z pełnymi listami PIDów"""
        if not self.found_muxes:
            print("Brak multipleksów do eksportu")
            return
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
            
            for mux in self.found_muxes:
                freq = mux['frequency']
                msys = mux['system']
                bw = mux['bandwidth']
                tmode = mux['transmission_mode']
                gi = mux['guard_interval']
                
                for service in mux['services']:
                    service_id = service['id']
                    name = service['name']
                    pmt_pid = service.get('pmt_pid', 0)
                    
                    # Zbierz wszystkie PIDy dla tej usługi
                    all_pids = set()
                    
                    # Dodaj podstawowe PIDy
                    all_pids.add(0)  # PAT
                    
                    # Dodaj PMT PID
                    if pmt_pid > 0:
                        all_pids.add(pmt_pid)
                    
                    # Dodaj wszystkie strumienie z PMT
                    if 'streams' in service:
                        for category, streams in service['streams'].items():
                            if isinstance(streams, list):
                                for stream in streams:
                                    if isinstance(stream, dict):
                                        all_pids.add(stream.get('pid', 0))
                                    else:
                                        all_pids.add(stream)
                            elif isinstance(streams, dict):
                                for stream in streams.values():
                                    if isinstance(stream, dict):
                                        all_pids.add(stream.get('pid', 0))
                                    else:
                                        all_pids.add(stream)
                    
                    # Dodaj standardowe PIDy EPG
                    all_pids.update([16, 17, 18, 20])  # EIT, SDT, TDT, NIT
                    
                    # Usuń PID 0 z listy (już dodany na początku)
                    all_pids.discard(0)
                    
                    # Konwertuj na posortowaną listę
                    pids_list = sorted([p for p in all_pids if p > 0])
                    
                    # Dodaj 0 na początku listy
                    pids_str = ','.join(['0'] + [str(p) for p in pids_list])
                    
                    # Zapisz do M3U
                    f.write(f'#EXTINF:-1 tvg-id="{service_id}",{name}\n')
                    
                    # Buduj URL SAT>IP
                    url = (f"rtsp://{self.scanner.host}:{self.scanner.port}/"
                           f"?freq={freq}&msys={msys}&bw={bw}&tmode={tmode}&gi={gi}"
                           f"&pids={pids_str}")
                    
                    f.write(f'{url}\n')
        
        print(f"💾 Zapisano do: {output_file}")
        
        # Statystyki
        total_services = sum(len(mux['services']) for mux in self.found_muxes)
        print(f"   Multipleksy: {len(self.found_muxes)}")
        print(f"   Stacje: {total_services}")
        
        # Zapisz szczegółowe informacje o PIDach do pliku JSON
        json_file = output_file.replace('.m3u', '_pids.json')
        self.export_pids_info(json_file)
    
    def export_pids_info(self, json_file):
        """Eksportuje szczegółowe informacje o PIDach do pliku JSON"""
        pids_info = {
            'scan_time': datetime.now().isoformat(),
            'multiplexes': []
        }
        
        for mux in self.found_muxes:
            mux_info = {
                'frequency': mux['frequency'],
                'system': mux['system'],
                'bandwidth': mux['bandwidth'],
                'services': []
            }
            
            for service in mux['services']:
                service_info = {
                    'id': service['id'],
                    'name': service['name'],
                    'provider': service.get('provider', ''),
                    'pmt_pid': service.get('pmt_pid', 0),
                    'streams': service.get('streams', {})
                }
                mux_info['services'].append(service_info)
            
            pids_info['multiplexes'].append(mux_info)
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(pids_info, f, indent=2, ensure_ascii=False)
        
        print(f"   Szczegóły PIDów: {json_file}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='DVB-T/T2 Scanner z pełnym wykrywaniem PIDów',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Przykłady użycia:

  # Autoskan pełnego pasma VHF+UHF (DVB-T2)
  %(prog)s --auto --vhf --uhf -o channels.m3u
  
  # Autoskan tylko UHF, DVB-T i DVB-T2
  %(prog)s --auto --uhf --system both -o channels.m3u
  
  # Skanowanie z pliku konfiguracyjnego
  %(prog)s --config pl-Rzeszow_Baranowka -o channels.m3u
  
  # Szybkie skanowanie (krótsze timeouty)
  %(prog)s --auto --uhf --detect-time 2 --scan-time 8
  
  # Dokładne skanowanie (dłuższe timeouty)
  %(prog)s --config channels.conf --detect-time 5 --scan-time 20
  
  # Zdalny serwer
  %(prog)s --auto --uhf -a 192.168.1.100:8080
        """
    )
    
    # Tryb skanowania
    scan_group = parser.add_mutually_exclusive_group(required=True)
    scan_group.add_argument(
        '--auto',
        action='store_true',
        help='Autoskan pełnego pasma VHF/UHF'
    )
    scan_group.add_argument(
        '--config',
        metavar='FILE',
        help='Skanowanie z pliku konfiguracyjnego'
    )
    
    # Opcje autoSkanu
    parser.add_argument(
        '--vhf',
        action='store_true',
        help='Skanuj pasmo VHF (174-230 MHz) - tylko z --auto'
    )
    parser.add_argument(
        '--uhf',
        action='store_true',
        help='Skanuj pasmo UHF (470-862 MHz) - tylko z --auto'
    )
    parser.add_argument(
        '--system',
        choices=['dvbt', 'dvbt2', 'both'],
        default='dvbt2',
        help='System transmisji (domyślnie: dvbt2)'
    )
    
    # Parametry skanowania
    parser.add_argument(
        '--detect-time',
        type=int,
        default=3,
        help='Czas detekcji sygnału w sekundach (domyślnie: 3)'
    )
    parser.add_argument(
        '--scan-time',
        type=int,
        default=10,
        help='Czas pełnego skanu w sekundach (domyślnie: 10)'
    )
    
    # Serwer
    parser.add_argument(
        '-H', '--host',
        default='192.168.1.1',
        help='Host/IP serwera SAT>IP (domyślnie: 192.168.1.1)'
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=8080,
        help='Port HTTP serwera SAT>IP (domyślnie: 8080)'
    )
    parser.add_argument(
        '-a', '--address',
        help='Pełny adres IP:PORT (zastępuje -H i -p)'
    )
    
    # Wyjście
    parser.add_argument(
        '-o', '--output',
        default='channels.m3u',
        help='Plik wyjściowy M3U (domyślnie: channels.m3u)'
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
    print("  DVB-T/T2 SCANNER - PEŁNE WYKRYWANIE PIDÓW")
    print("=" * 70)
    print(f"🌐 Serwer: {host}:{port}")
    
    # Tryb autoskan
    if args.auto:
        if not args.vhf and not args.uhf:
            print("\n✗ Dla --auto musisz wybrać --vhf i/lub --uhf")
            sys.exit(1)
        
        scanner = AutoScanner(host, port)
        
        muxes = scanner.scan_all(
            vhf=args.vhf,
            uhf=args.uhf,
            msys=args.system,
            detection_time=args.detect_time,
            full_scan_time=args.scan_time
        )
        
        if muxes:
            scanner.export_to_m3u(args.output)
        else:
            print("\n⚠ Nie znaleziono żadnych multipleksów")
    
    # Tryb z pliku konfiguracyjnego
    elif args.config:
        import os
        
        if not os.path.exists(args.config):
            print(f"\n✗ Plik nie istnieje: {args.config}")
            sys.exit(1)
        
        scanner = ConfigScanner(host, port)
        
        muxes = scanner.scan_from_config(
            args.config,
            detection_time=args.detect_time,
            full_scan_time=args.scan_time
        )
        
        if muxes:
            scanner.export_to_m3u(args.output)
        else:
            print("\n⚠ Nie znaleziono żadnych multipleksów")
    
    print("\n✓ Gotowe!")
    print("=" * 70)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹ Przerwano przez użytkownika")
        sys.exit(0)