#version 0.9
import socket
import struct
import time
import random
import colorsys
import threading
from typing import Dict, Optional, List, Tuple

# =========================
# LIFX CONSTANTS
# =========================

LIFX_PORT = 56700
PROTO = 1024
HEADER_SIZE = 36
MIN_SEND_INTERVAL = 0.05  # seconds (rate limit)
DEFAULT_KELVIN = 3500

# Message types
GET_SERVICE = 2
STATE_SERVICE = 3
GET_LABEL = 23
STATE_LABEL = 25
GET_POWER = 20
STATE_POWER = 22
GET_VERSION = 32
STATE_VERSION = 33
GET_LIGHT_STATE = 101
STATE_LIGHT = 107
SET_COLOR = 102
SET_POWER = 21

# =========================
# HELPERS
# =========================

def clamp01(x):
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def rgb01_to_hsbk(r, g, b, kelvin=DEFAULT_KELVIN):
    r = clamp01(r)
    g = clamp01(g)
    b = clamp01(b)

    h, s, v = colorsys.rgb_to_hsv(r, g, b)

    return (
        int(h * 65535) & 0xFFFF,
        int(s * 65535) & 0xFFFF,
        int(v * 65535) & 0xFFFF,
        int(kelvin) & 0xFFFF
    )


# =========================
# LIFX LIGHT REPRESENTATION
# =========================

class LifxLight:
    def __init__(self, target: bytes, ip: str, label: str = ""):
        self.target = target
        self.ip = ip
        self.label = label
        self.power = 0
        self.colour = None
        self.last_seen = time.time()
        self.vendor = 0
        self.product = 0
        self.version = 0
        self.model_name = "Discovering..."
        self.is_light = True  # Will be set based on product type
        self.supported_modes = ["RGB"]  # Default, will be updated based on product
        # Current state
        self.current_hue = 0
        self.current_saturation = 0
        self.current_brightness = 0
        self.current_kelvin = DEFAULT_KELVIN
        self.current_rgb = (0, 0, 0)  # (r, g, b) as 0-255
        self.color_set_time = 0  # Timestamp when color was last set via set_rgb

    def __repr__(self):
        return f"LifxLight(label='{self.label}', ip='{self.ip}', model='{self.model_name}')"


# =========================
# LIFX CLIENT
# =========================

class LifxLanClient:
    def __init__(self, bind_ip: str = "0.0.0.0"):
        self.source = random.randint(2, 0xFFFFFFFF)
        self.sequence = random.randint(0, 255)
        self.lights: Dict[bytes, LifxLight] = {}
        self.last_send = 0.0
        self.lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.5)
        self.sock.bind((bind_ip, 0))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Start listener thread
        self.listening = True
        self.listener_thread = None
        self._start_listener()
    
    def _start_listener(self):
        """Start the listener thread if not already running"""
        if self.listener_thread is None or not self.listener_thread.is_alive():
            self.listener_thread = threading.Thread(target=self._listen, daemon=True)
            self.listener_thread.start()

    def _next_seq(self):
        self.sequence = (self.sequence + 1) & 0xFF
        return self.sequence

    def _rate_limit(self):
        dt = time.time() - self.last_send
        if dt < MIN_SEND_INTERVAL:
            time.sleep(MIN_SEND_INTERVAL - dt)
        self.last_send = time.time()

    def _build_header(self, msg_type: int, target: Optional[bytes] = None, tagged: bool = False):
        addressable = 1
        origin = 0

        frame_bits = (
            (PROTO & 0x0FFF)
            | (addressable << 12)
            | ((1 if tagged else 0) << 13)
            | (origin << 14)
        )

        frame = struct.pack(
            "<HHI",
            HEADER_SIZE,
            frame_bits,
            self.source
        )

        target_bytes = target if target else b"\x00" * 8

        address = struct.pack(
            "<8s6sBB",
            target_bytes,
            b"\x00" * 6,
            0,
            self._next_seq()
        )

        protocol = struct.pack(
            "<QH2s",
            0,
            msg_type,
            b"\x00\x00"
        )

        return frame + address + protocol

    def _finalise(self, packet: bytes) -> bytes:
        size = len(packet)
        return struct.pack("<H", size) + packet[2:]

    def _listen(self):
        """Background thread to listen for LIFX responses"""
        while self.listening:
            try:
                data, (ip, port) = self.sock.recvfrom(4096)
                if len(data) < HEADER_SIZE:
                    continue

                # Parse header
                size = struct.unpack("<H", data[:2])[0]
                frame_bits = struct.unpack("<H", data[2:4])[0]
                source = struct.unpack("<I", data[4:8])[0]
                target = data[8:16]
                msg_type = struct.unpack("<H", data[32:34])[0]

                # Only process responses to our messages
                if source != self.source:
                    continue

                with self.lock:
                    # Update or create light entry
                    if target not in self.lights:
                        light = LifxLight(target, ip)
                        self.lights[target] = light
                    else:
                        light = self.lights[target]
                    light.last_seen = time.time()

                    # Handle different message types
                    if msg_type == STATE_SERVICE:
                        # Service response - light is responding
                        pass
                    elif msg_type == STATE_LABEL:
                        # Extract label (starts at byte 36)
                        if len(data) >= 36:
                            label_bytes = data[36:].split(b'\x00')[0]
                            light.label = label_bytes.decode('utf-8', errors='ignore')
                    elif msg_type == STATE_POWER:
                        # Extract power state (byte 36)
                        if len(data) >= 37:
                            light.power = struct.unpack("<H", data[36:38])[0]
                    elif msg_type == STATE_VERSION:
                        # Extract version info (vendor, product, version)
                        # STATE_VERSION payload: vendor (4 bytes), product (4 bytes), version (4 or 8 bytes)
                        # Total: 36 (header) + 4 + 4 + 4/8 = 48 or 52 bytes
                        if len(data) >= 48:
                            try:
                                vendor_bytes = data[36:40]
                                product_bytes = data[40:44]
                                # Version might be 4 or 8 bytes depending on device
                                if len(data) >= 52:
                                    version_bytes = data[44:52]
                                    light.version = struct.unpack("<Q", version_bytes)[0]
                                else:
                                    version_bytes = data[44:48]
                                    light.version = struct.unpack("<I", version_bytes)[0]
                                
                                light.vendor = struct.unpack("<I", vendor_bytes)[0]
                                light.product = struct.unpack("<I", product_bytes)[0]
                                light.model_name = self._get_model_name(light.vendor, light.product)
                                light.supported_modes = self._get_supported_modes(light.product)
                                # Filter out switches and other non-light devices
                                # Product IDs: 1=Original, 3=Color, 10=White, 11=Color 1000, etc.
                                # Switches have different product IDs - filter them out
                                # Switch product IDs: 68, 70, 71, 72, 73, 108, 109, 110, 111
                                switch_product_ids = [68, 70, 71, 72, 73, 108, 109, 110, 111]
                                is_switch = light.product in switch_product_ids
                                
                                # Also check model name as a fallback
                                if light.model_name and "Switch" in light.model_name:
                                    is_switch = True
                                
                                light.is_light = not is_switch
                                
                                # Remove switch from lights dictionary if it's a switch
                                if is_switch:
                                    if target in self.lights:
                                        del self.lights[target]
                            except Exception as e:
                                print(f"Error parsing STATE_VERSION for {light.ip}: {e}, data_len={len(data)}")
                    elif msg_type == STATE_LIGHT:
                        # STATE_LIGHT payload: HSBK (Hue, Saturation, Brightness, Kelvin) + reserved
                        # Format: reserved (1 byte), hue (2 bytes), saturation (2 bytes), brightness (2 bytes), kelvin (2 bytes)
                        # Total: 36 (header) + 9 = 45 bytes
                        if len(data) >= 45:
                            try:
                                hue = struct.unpack("<H", data[37:39])[0]
                                sat = struct.unpack("<H", data[39:41])[0]
                                bri = struct.unpack("<H", data[41:43])[0]
                                kel = struct.unpack("<H", data[43:45])[0]
                                
                                # Only update from STATE_LIGHT if we haven't set a color recently
                                # This prevents stale state responses from overwriting colors we just set via DMX
                                time_since_set = time.time() - getattr(light, 'color_set_time', 0)
                                if time_since_set > 1.0:  # Only update if color wasn't set in last second
                                    light.current_hue = hue
                                    light.current_saturation = sat
                                    light.current_brightness = bri
                                    light.current_kelvin = kel
                                    
                                    # Convert HSBK to RGB for display
                                    h = hue / 65535.0
                                    s = sat / 65535.0
                                    v = bri / 65535.0
                                    r, g, b = colorsys.hsv_to_rgb(h, s, v)
                                    light.current_rgb = (int(r * 255), int(g * 255), int(b * 255))
                            except Exception as e:
                                print(f"Error parsing STATE_LIGHT for {light.ip}: {e}, data_len={len(data)}")
                    else:
                        # Debug: log unhandled message types
                        if msg_type not in [STATE_SERVICE, STATE_LABEL, STATE_POWER, STATE_VERSION, STATE_LIGHT]:
                            print(f"Unhandled message type {msg_type} from {ip}")

            except socket.timeout:
                continue
            except Exception as e:
                if self.listening:
                    print(f"Error in listener: {e}")

    # =========================
    # DISCOVERY
    # =========================

    def discover_lights(self, timeout: float = 5.0) -> List[LifxLight]:
        """Discover all LIFX lights on the network"""
        # Clear existing lights at start of discovery to avoid duplicates
        with self.lock:
            self.lights.clear()
        
        # Send broadcast discovery
        header = self._build_header(GET_SERVICE, tagged=True)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, ("255.255.255.255", LIFX_PORT))

        # Wait for responses - increased timeout to allow more lights to respond
        time.sleep(timeout)

        # Request labels and version info for discovered lights
        with self.lock:
            lights_list = list(self.lights.values())
            for light in lights_list:
                self._request_label(light)
                time.sleep(0.05)  # Small delay between requests
                self._request_version(light)
                time.sleep(0.05)  # Small delay between requests

        # Wait longer for label and version responses - some lights respond slower
        time.sleep(1.5)

        # Filter out non-light devices (switches, etc.)
        # Also double-check by model name in case product ID wasn't set
        with self.lock:
            filtered_lights = []
            for light in self.lights.values():
                # Check product ID first
                if not light.is_light:
                    continue
                # Also check model name as fallback
                if light.model_name and "Switch" in light.model_name:
                    continue
                filtered_lights.append(light)
            return filtered_lights

    def _request_label(self, light: LifxLight):
        """Request label from a specific light"""
        header = self._build_header(GET_LABEL, target=light.target, tagged=False)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))
    
    def _request_version(self, light: LifxLight):
        """Request version info from a specific light"""
        header = self._build_header(GET_VERSION, target=light.target, tagged=False)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))
    
    def _request_light_state(self, light: LifxLight):
        """Request current light state (color) from a specific light"""
        header = self._build_header(GET_LIGHT_STATE, target=light.target, tagged=False)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))
    
    def refresh_light_states(self):
        """Request current state from all discovered lights"""
        with self.lock:
            lights_list = [light for light in self.lights.values() 
                          if light.is_light and not (light.model_name and "Switch" in light.model_name)]
            for light in lights_list:
                self._request_light_state(light)
                time.sleep(0.05)  # Small delay between requests
        # Wait for responses
        time.sleep(0.5)
    
    def probe_light_by_ip(self, ip: str, timeout: float = 2.0) -> Optional[LifxLight]:
        """Probe a specific IP address to discover a LIFX light"""
        # Ensure listener is running
        self._start_listener()
        
        # Send GET_SERVICE to specific IP (not broadcast)
        # We use tagged=True to get a response even if we don't know the target
        header = self._build_header(GET_SERVICE, tagged=True)
        packet = self._finalise(header)
        
        self._rate_limit()
        self.sock.sendto(packet, (ip, LIFX_PORT))
        
        # Wait for response
        time.sleep(timeout)
        
        # Check if we got a response for this IP
        with self.lock:
            for light in self.lights.values():
                if light.ip == ip:
                    # Request label and version info
                    self._request_label(light)
                    time.sleep(0.05)
                    self._request_version(light)
                    time.sleep(0.1)
                    # Return the light if it's actually a light (not a switch, etc.)
                    if light.is_light:
                        return light
        
        return None
    
    def _get_model_name(self, vendor: int, product: int) -> str:
        """Map product ID to model name"""
        # LIFX vendor ID is 1
        if vendor != 1:
            return f"Unknown (vendor={vendor})"
        
        # Product ID to model name mapping
        product_map = {
            1: "Original 1000",
            3: "Color 650",
            10: "White 800 (LV)",
            11: "White 900 BR30 (LV)",
            18: "Color 1000 BR30",
            20: "Color 1000",
            22: "LIFX A19",
            27: "LIFX BR30",
            28: "LIFX+ A19",
            29: "LIFX+ BR30",
            30: "LIFX Z",
            31: "LIFX Z 2",
            32: "LIFX Downlight",
            36: "LIFX Downlight",
            37: "LIFX Beam",
            38: "LIFX+ A19",
            39: "LIFX+ BR30",
            40: "LIFX Mini",
            43: "LIFX Mini Color",
            44: "LIFX Mini White to Warm",
            45: "LIFX Mini White",
            46: "LIFX GU10",
            49: "LIFX Tile",
            50: "LIFX Candle",
            51: "LIFX Candle Color",
            52: "LIFX Mini Color",
            55: "LIFX A19",
            57: "LIFX BR30",
            59: "LIFX A19 Night Vision",
            60: "LIFX BR30 Night Vision",
            61: "LIFX Mini White",
            62: "LIFX Mini White",
            63: "LIFX Mini White",
            64: "LIFX Mini White",
            65: "LIFX Tile",
            66: "LIFX Candle",
            68: "LIFX Switch",
            70: "LIFX Switch",
            71: "LIFX Switch",
            72: "LIFX Switch",
            73: "LIFX Switch",
            81: "LIFX Candle",
            82: "LIFX Candle",
            85: "LIFX Z",
            87: "LIFX Z",
            88: "LIFX Beam",
            89: "LIFX Beam",
            90: "LIFX Downlight",
            91: "LIFX Downlight",
            92: "LIFX Color",
            93: "LIFX Color",
            94: "LIFX A19",
            96: "LIFX BR30",
            97: "LIFX Colour A19 1200lm",
            98: "LIFX A19",
            99: "LIFX BR30",
            111: "LIFX Switch",
            100: "LIFX Clean",
            101: "LIFX Filament Clear",
            102: "LIFX Filament Amber",
            105: "LIFX Mini White",
            107: "LIFX Candle",
            108: "LIFX Switch",
            109: "LIFX Switch",
            110: "LIFX Switch",
            111: "LIFX Switch",
            112: "LIFX Beam",
            113: "LIFX Downlight",
            114: "LIFX A19",
            115: "LIFX BR30",
            116: "LIFX Downlight White to Warm",
            117: "LIFX A19 White to Warm",
            118: "LIFX BR30 White to Warm",
            119: "LIFX Mini White to Warm",
            120: "LIFX GU10 White to Warm",
        }
        
        return product_map.get(product, f"Unknown (product={product})")
    
    def _get_supported_modes(self, product: int) -> List[str]:
        """Determine supported channel modes based on product ID"""
        # All LIFX lights support RGB, RGBW, HSI, and HSBK
        # RGBW can be used on any light - the white channel will be blended into RGB
        modes = ["RGB", "RGBW", "HSI", "HSBK"]
        
        return modes

    def refresh_lights(self):
        """Refresh the list of discovered lights"""
        return self.discover_lights()

    def get_lights(self) -> List[LifxLight]:
        """Get current list of discovered lights"""
        with self.lock:
            return list(self.lights.values())

    # =========================
    # COLOR CONTROL
    # =========================

    def set_rgb(self, target: bytes, ip: str, r: float, g: float, b: float, 
                kelvin: int = DEFAULT_KELVIN, duration_ms: int = 0, brightness: float = 1.0):
        """Set RGB colour for a specific light"""
        # Clamp RGB values
        r = clamp01(r)
        g = clamp01(g)
        b = clamp01(b)
        
        # Convert RGB to HSBK
        hue, sat, bri, kel = rgb01_to_hsbk(r, g, b, kelvin)
        
        # Apply brightness multiplier (brightness parameter is 0-1, acts as a multiplier)
        # The brightness from HSV conversion (bri) is the color brightness
        # The brightness parameter is the overall brightness multiplier
        bri = int(bri * brightness) & 0xFFFF

        header = self._build_header(SET_COLOR, target=target, tagged=False)

        payload = struct.pack(
            "<BHHHHI",
            0,  # reserved
            hue,
            sat,
            bri,
            kel,
            int(duration_ms)
        )

        packet = self._finalise(header + payload)

        self._rate_limit()
        self.sock.sendto(packet, (ip, LIFX_PORT))
        
        # Update the light's current state so UI can display it
        with self.lock:
            if target in self.lights:
                light = self.lights[target]
                light.current_hue = hue
                light.current_saturation = sat
                light.current_brightness = bri
                light.current_kelvin = kel
                # Convert HSBK back to RGB to get the actual displayed color (with brightness applied)
                # This ensures the stored RGB matches what's actually displayed on the light
                h = hue / 65535.0
                s = sat / 65535.0
                v = bri / 65535.0
                r_displayed, g_displayed, b_displayed = colorsys.hsv_to_rgb(h, s, v)
                light.current_rgb = (int(r_displayed * 255), int(g_displayed * 255), int(b_displayed * 255))
                # Mark that we just set the color (prevent stale STATE_LIGHT responses from overwriting)
                light.color_set_time = time.time()

    def set_power(self, target: bytes, ip: str, power: bool):
        """Set power state for a specific light"""
        header = self._build_header(SET_POWER, target=target, tagged=False)
        power_value = 65535 if power else 0
        payload = struct.pack("<H", power_value)
        packet = self._finalise(header + payload)

        self._rate_limit()
        self.sock.sendto(packet, (ip, LIFX_PORT))

    def close(self):
        """Close the client and cleanup"""
        self.listening = False
        if self.sock:
            self.sock.close()

