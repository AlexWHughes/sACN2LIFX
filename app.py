#!/usr/bin/env python3
"""
sACN2LIFX - Control LIFX lights via sACN/E1.31
"""

VERSION = "281225 047"

import json
import os
import threading
import time
import colorsys
import socket
import logging
from typing import Optional, Dict, List
from flask import Flask, render_template, jsonify, request
from lifx_client import LifxLanClient, LifxLight
from dmx_receiver import DMXReceiver

# Set up logging for DMX to LIFX traffic (controlled via environment variables)
# ENABLE_DMX_LOG: Enable basic DMX frame logging (default: false)
# ENABLE_PERF_LOGGING: Enable performance/timing logging (default: false)
# PERF_LOG_SAMPLE_RATE: Log every N frames when sampling (default: 100)
# PERF_SEND_THRESHOLD_MS: Log sends slower than this (default: 5ms)
# PERF_PROCESS_THRESHOLD_MS: Log processing slower than this (default: 10ms)

enable_dmx_log = os.getenv('ENABLE_DMX_LOG', 'false').lower() in ('true', '1', 'yes')
enable_perf_logging = os.getenv('ENABLE_PERF_LOGGING', 'false').lower() in ('true', '1', 'yes')

if enable_dmx_log or enable_perf_logging:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    dmx_logger = logging.getLogger('dmx_lifx')
    dmx_logger.setLevel(logging.INFO)
else:
    dmx_logger = None  # Disabled by default

# Performance logging configuration
try:
    PERF_LOG_SAMPLE_RATE = max(1, int(os.getenv('PERF_LOG_SAMPLE_RATE', '100')))  # Log every N frames, minimum 1
    PERF_SEND_THRESHOLD_MS = max(0.0, float(os.getenv('PERF_SEND_THRESHOLD_MS', '5.0')))  # Log slow sends
    PERF_PROCESS_THRESHOLD_MS = max(0.0, float(os.getenv('PERF_PROCESS_THRESHOLD_MS', '10.0')))  # Log slow processing
except ValueError as e:
    print(f"Warning: Invalid performance logging configuration: {e}. Using defaults.")
    PERF_LOG_SAMPLE_RATE = 100
    PERF_SEND_THRESHOLD_MS = 5.0
    PERF_PROCESS_THRESHOLD_MS = 10.0

# Frame counter for sampling (thread-local would be better, but simple counter works for single-threaded DMX processing)
_dmx_frame_counter = 0

try:
    import netifaces
    HAS_NETIFACES = True
except ImportError:
    HAS_NETIFACES = False

app = Flask(__name__)

# Global state
lifx_client: Optional[LifxLanClient] = None
dmx_receiver: Optional[DMXReceiver] = None
light_mappings: Dict[str, Dict] = {}  # light_id -> {universe, start_channel, brightness}
running = False
dmx_thread: Optional[threading.Thread] = None
lifx_interface: Optional[str] = None  # Network interface IP for LIFX
sacn_interface: Optional[str] = None  # Network interface IP for sACN

# Thread synchronization for DMX state mutations
dmx_lock = threading.Lock()

# Configuration
CONFIG_FILE = "config.json"
MAX_BRIGHTNESS = 1.0  # Stored as 0-1 internally, displayed as 0-100%
OVERRIDE_MAX_BRIGHT_TOTAL_RGB = 200 * 3
MAX_BRIGHT_OVERRIDE = 1.0
MAX_RGB_PER_COLOUR = 255
DEFAULT_KELVIN = 3500
MAX_HUE = 360  # Degrees
MAX_SATURATION = 100  # Percentage
MAX_INTENSITY = 100  # Percentage
FADE_DURATION_MS = 20  # Smooth fade duration for color transitions (milliseconds) - optimized for 40Hz sACN
VALUE_CHANGE_THRESHOLD = 1  # Only update if DMX value changed by this much (0-255) - lower for smoother transitions


def load_config():
    """Load configuration (mappings and settings) from file"""
    global light_mappings, lifx_interface, sacn_interface
    try:
        with open(CONFIG_FILE, 'r') as f:
            content = f.read().strip()
            # Handle empty file
            if not content:
                light_mappings = {}
                lifx_interface = None
                sacn_interface = None
                return
            
            config = json.loads(content)
            light_mappings = config.get('mappings', {})
            settings = config.get('settings', {})
            lifx_interface = settings.get('lifx_interface', None)
            sacn_interface = settings.get('sacn_interface', None)
    except FileNotFoundError:
        light_mappings = {}
        lifx_interface = None
        sacn_interface = None
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Warning: Error parsing config.json: {e}. Using empty configuration.")
        light_mappings = {}
        lifx_interface = None
        sacn_interface = None


def save_config():
    """Save configuration (mappings and settings) to file"""
    config = {
        'mappings': light_mappings,
        'settings': {
            'lifx_interface': lifx_interface,
            'sacn_interface': sacn_interface
        }
    }
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


# Deprecated functions removed - use load_config() and save_config() directly


def _normalize_interface_ip(ip: Optional[str]) -> str:
    """Normalize interface IP: return '0.0.0.0' if None or '0.0.0.0', otherwise return the IP"""
    return '0.0.0.0' if not ip or ip == '0.0.0.0' else ip


def get_network_interfaces():
    """Get list of available network interfaces with their IP addresses"""
    interfaces = []
    
    if HAS_NETIFACES:
        try:
            # Get all interfaces
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for addr_info in addrs[netifaces.AF_INET]:
                        ip = addr_info.get('addr')
                        if ip and not ip.startswith('127.'):  # Skip loopback
                            interfaces.append({
                                'name': iface,
                                'ip': ip,
                                'display': f"{iface} ({ip})"
                            })
        except Exception as e:
            print(f"Error getting network interfaces: {e}")
    
    # Fallback: try socket method if netifaces failed or not available
    if not interfaces:
        try:
            hostname = socket.gethostname()
            # Get all IP addresses
            addrinfo = socket.getaddrinfo(hostname, None)
            for info in addrinfo:
                ip = info[4][0]
                if ip and not ip.startswith('127.'):
                    interfaces.append({
                        'name': hostname,
                        'ip': ip,
                        'display': f"{hostname} ({ip})"
                    })
        except Exception as e:
            print(f"Error getting network interfaces (fallback): {e}")
    
    # Add "All Interfaces" option
    interfaces.insert(0, {
        'name': '0.0.0.0',
        'ip': '0.0.0.0',
        'display': 'All Interfaces (0.0.0.0)'
    })
    
    return interfaces


def light_id(light: LifxLight) -> str:
    """Generate unique ID for a light"""
    return light.target.hex()


# Store last sent values per light to implement change threshold
_last_sent_values: Dict[str, List[int]] = {}  # light_id -> list of channel values

def process_dmx_data(dmx_data: list, universe: int):
    """Process incoming DMX data and update lights"""
    global lifx_client, light_mappings, _last_sent_values, _dmx_frame_counter
    
    if not lifx_client or not running:
        return
    
    # Performance logging: track start time and frame counter
    process_start = None
    should_log_frame = False
    if enable_perf_logging:
        process_start = time.time()
        _dmx_frame_counter += 1
        should_log_frame = (_dmx_frame_counter % PERF_LOG_SAMPLE_RATE == 0)
    
    if dmx_logger and enable_dmx_log:
        dmx_logger.info(f"DMX received: universe={universe}, data_len={len(dmx_data)}")
    
    # Build a lookup of lights by ID for faster access
    # First try filtered lights (excludes switches)
    lights = lifx_client.get_lights()
    lights_by_id = {light_id(light): light for light in lights}
    
    # Also check raw lights dictionary for configured lights that might be filtered out
    # This allows configured lights to work even if they're misidentified as switches
    # (Some devices may be incorrectly identified as switches but are actually lights)
    with lifx_client.lock:
        raw_lights = lifx_client.lights
        for target, light in raw_lights.items():
            lid = light_id(light)
            if lid not in lights_by_id and lid in light_mappings:
                # This is a configured light that was filtered out - include it anyway
                # We trust the user's configuration over automatic detection
                lights_by_id[lid] = light
    
    # Iterate through mappings to ensure correct light-channel mapping
    for mapped_light_id, mapping in light_mappings.items():
        if mapping.get('universe') != universe:
            continue
        
        # Find the light for this mapping
        light = lights_by_id.get(mapped_light_id)
        if not light:
            continue  # Light not currently discovered
        
        start_channel = mapping.get('start_channel', 0) - 1  # Convert to 0-based
        brightness = mapping.get('brightness', MAX_BRIGHTNESS)
        channel_mode = mapping.get('channel_mode', 'RGB (8bit)')
        
        # Determine number of channels based on mode
        channels_needed = {
            'RGB (8bit)': 3,      # 8-bit RGB (3 channels)
            'RGB (16bit)': 6,     # 16-bit RGB (6 channels: MSB+LSB per color)
            'RGBW (8bit)': 4,     # 8-bit RGBW (4 channels)
            'RGBW (16bit)': 8,    # 16-bit RGBW (8 channels: MSB+LSB per color)
            'HSBK (8bit)': 4,     # 8-bit HSBK (4 channels)
            'HSBK (16bit)': 8     # 16-bit HSBK (8 channels: MSB+LSB per parameter)
        }.get(channel_mode, 3)
        
        if start_channel < 0 or start_channel + channels_needed > len(dmx_data):
            continue
        
        # Extract channel values
        channel_values = dmx_data[start_channel:start_channel + channels_needed]
        
        # Check if values have changed significantly (to avoid spamming updates)
        last_values = _last_sent_values.get(mapped_light_id)
        if last_values is not None:
            has_significant_change = False
            max_change = 0
            
            # For 16-bit modes, check combined 16-bit values instead of individual channels
            if channel_mode == 'RGB (16bit)' and len(channel_values) >= 6 and len(last_values) >= 6:
                # Compare combined 16-bit RGB values
                for color_idx in range(3):  # R, G, B
                    msb_idx = color_idx * 2
                    lsb_idx = color_idx * 2 + 1
                    current_16bit = (channel_values[msb_idx] << 8) | channel_values[lsb_idx]
                    last_16bit = (last_values[msb_idx] << 8) | last_values[lsb_idx]
                    change = abs(current_16bit - last_16bit)
                    max_change = max(max_change, change)
                    # For 16-bit, use a threshold of 1 (any change in combined value triggers update)
                    # This allows fine-grained control while still filtering out identical values
                    if change >= 1:
                        has_significant_change = True
                        break
            elif channel_mode == 'RGBW (16bit)' and len(channel_values) >= 8 and len(last_values) >= 8:
                # Compare combined 16-bit RGBW values
                for color_idx in range(4):  # R, G, B, W
                    msb_idx = color_idx * 2
                    lsb_idx = color_idx * 2 + 1
                    current_16bit = (channel_values[msb_idx] << 8) | channel_values[lsb_idx]
                    last_16bit = (last_values[msb_idx] << 8) | last_values[lsb_idx]
                    change = abs(current_16bit - last_16bit)
                    max_change = max(max_change, change)
                    if change >= 1:
                        has_significant_change = True
                        break
            elif channel_mode == 'HSBK (16bit)' and len(channel_values) >= 8 and len(last_values) >= 8:
                # Compare combined 16-bit HSBK values
                for param_idx in range(4):  # H, S, B, K
                    msb_idx = param_idx * 2
                    lsb_idx = param_idx * 2 + 1
                    current_16bit = (channel_values[msb_idx] << 8) | channel_values[lsb_idx]
                    last_16bit = (last_values[msb_idx] << 8) | last_values[lsb_idx]
                    change = abs(current_16bit - last_16bit)
                    max_change = max(max_change, change)
                    if change >= 1:
                        has_significant_change = True
                        break
            else:
                # For 8-bit modes, check individual channel values
                for i, val in enumerate(channel_values):
                    if i >= len(last_values):
                        has_significant_change = True
                        break
                    change = abs(val - last_values[i])
                    max_change = max(max_change, change)
                    if change >= VALUE_CHANGE_THRESHOLD:
                        has_significant_change = True
                        break
            
            if not has_significant_change:
                if dmx_logger and enable_dmx_log:
                    dmx_logger.debug(f"  SKIP {light.label}: change={max_change:.1f} < threshold, values={channel_values}")
                continue  # Skip update if change is too small
        
        # Store current values
        _last_sent_values[mapped_light_id] = list(channel_values)
        
        # Convert based on channel mode
        if channel_mode == 'RGB (8bit)':
            r = channel_values[0] / MAX_RGB_PER_COLOUR
            g = channel_values[1] / MAX_RGB_PER_COLOUR
            b = channel_values[2] / MAX_RGB_PER_COLOUR
            
            # Clamp RGB values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            
            # Apply brightness multiplier from mapping
            # The brightness setting (0-1) acts as a maximum brightness cap
            bright_adj = brightness
            
            if dmx_logger and enable_dmx_log:
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(f"  → {light.label}: RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), DMX={channel_values}, brightness={bright_adj:.2f}, fade={FADE_DURATION_MS}ms")
            
            send_start = None
            if enable_perf_logging:
                send_start = time.time()
            
            lifx_client.set_rgb(
                light.target,
                light.ip,
                r, g, b,
                kelvin=DEFAULT_KELVIN,
                duration_ms=FADE_DURATION_MS,
                brightness=bright_adj
            )
            
            if enable_perf_logging and send_start is not None:
                send_duration = (time.time() - send_start) * 1000
                if send_duration > PERF_SEND_THRESHOLD_MS:
                    dmx_logger.warning(f"    SLOW send: {send_duration:.1f}ms")
        
        elif channel_mode == 'RGB (16bit)':
            # RGB16: 16-bit per channel (6 channels total: MSB+LSB for each color)
            # Channels: R_MSB, R_LSB, G_MSB, G_LSB, B_MSB, B_LSB
            r_16bit = (channel_values[0] << 8) | channel_values[1]  # 0-65535
            g_16bit = (channel_values[2] << 8) | channel_values[3]  # 0-65535
            b_16bit = (channel_values[4] << 8) | channel_values[5]  # 0-65535
            
            # Normalize 16-bit values (0-65535) to 0-1 range
            r = r_16bit / 65535.0
            g = g_16bit / 65535.0
            b = b_16bit / 65535.0
            
            # Clamp RGB values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            
            # Apply brightness multiplier from mapping
            bright_adj = brightness
            
            if dmx_logger and enable_dmx_log:
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(f"  → {light.label}: RGB16=({r_16bit},{g_16bit},{b_16bit}) → RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), brightness={bright_adj:.2f}, fade={FADE_DURATION_MS}ms")
            
            send_start = None
            if enable_perf_logging:
                send_start = time.time()
            
            lifx_client.set_rgb(
                light.target,
                light.ip,
                r, g, b,
                kelvin=DEFAULT_KELVIN,
                duration_ms=FADE_DURATION_MS,
                brightness=bright_adj
            )
            
            if enable_perf_logging and send_start is not None:
                send_duration = (time.time() - send_start) * 1000
                if send_duration > PERF_SEND_THRESHOLD_MS:
                    dmx_logger.warning(f"    SLOW send: {send_duration:.1f}ms")
        
        elif channel_mode == 'RGBW (8bit)':
            r = channel_values[0] / MAX_RGB_PER_COLOUR
            g = channel_values[1] / MAX_RGB_PER_COLOUR
            b = channel_values[2] / MAX_RGB_PER_COLOUR
            w = channel_values[3] / MAX_RGB_PER_COLOUR
            
            # Clamp values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            w = max(0.0, min(1.0, w))
            
            # For RGBW, blend white with RGB
            # Simple approach: mix white into RGB based on white channel
            r = min(1.0, r + w * 0.3)
            g = min(1.0, g + w * 0.3)
            b = min(1.0, b + w * 0.3)
            
            # Apply brightness multiplier from mapping
            bright_adj = brightness
            
            send_start = None
            if enable_perf_logging:
                send_start = time.time()
            
            lifx_client.set_rgb(
                light.target,
                light.ip,
                r, g, b,
                kelvin=DEFAULT_KELVIN,
                duration_ms=FADE_DURATION_MS,
                brightness=bright_adj
            )
            
            if enable_perf_logging and send_start is not None:
                send_duration = (time.time() - send_start) * 1000
                if send_duration > PERF_SEND_THRESHOLD_MS:
                    dmx_logger.warning(f"    SLOW send: {send_duration:.1f}ms")
        
        elif channel_mode == 'RGBW (16bit)':
            # RGBW16: 16-bit per channel (8 channels total: MSB+LSB for each color)
            # Channels: R_MSB, R_LSB, G_MSB, G_LSB, B_MSB, B_LSB, W_MSB, W_LSB
            r_16bit = (channel_values[0] << 8) | channel_values[1]  # 0-65535
            g_16bit = (channel_values[2] << 8) | channel_values[3]  # 0-65535
            b_16bit = (channel_values[4] << 8) | channel_values[5]  # 0-65535
            w_16bit = (channel_values[6] << 8) | channel_values[7]  # 0-65535
            
            # Normalize 16-bit values (0-65535) to 0-1 range
            r = r_16bit / 65535.0
            g = g_16bit / 65535.0
            b = b_16bit / 65535.0
            w = w_16bit / 65535.0
            
            # Clamp values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            w = max(0.0, min(1.0, w))
            
            # For RGBW, blend white with RGB
            # Simple approach: mix white into RGB based on white channel
            r = min(1.0, r + w * 0.3)
            g = min(1.0, g + w * 0.3)
            b = min(1.0, b + w * 0.3)
            
            # Apply brightness multiplier from mapping
            bright_adj = brightness
            
            send_start = None
            if enable_perf_logging:
                send_start = time.time()
            
            lifx_client.set_rgb(
                light.target,
                light.ip,
                r, g, b,
                kelvin=DEFAULT_KELVIN,
                duration_ms=FADE_DURATION_MS,
                brightness=bright_adj
            )
            
            if enable_perf_logging and send_start is not None:
                send_duration = (time.time() - send_start) * 1000
                if send_duration > PERF_SEND_THRESHOLD_MS:
                    dmx_logger.warning(f"    SLOW send: {send_duration:.1f}ms")
        
        elif channel_mode == 'HSBK (8bit)':
            # HSBK8: 8-bit HSBK - Hue (0-360), Saturation (0-100), Brightness (0-100), Kelvin (2500-9000)
            hue = (channel_values[0] / 255.0) * MAX_HUE
            saturation = (channel_values[1] / 255.0) * MAX_SATURATION
            brightness_val = (channel_values[2] / 255.0) * MAX_BRIGHTNESS
            kelvin = int(2500 + (channel_values[3] / 255.0) * (9000 - 2500))
            
            # Convert HS to RGB
            h = hue / MAX_HUE
            s = saturation / MAX_SATURATION
            v = brightness_val / MAX_BRIGHTNESS
            
            r, g, b = colorsys.hsv_to_rgb(h, s, v)
            
            bright_adj = brightness * v
            
            send_start = None
            if enable_perf_logging:
                send_start = time.time()
            
            lifx_client.set_rgb(
                light.target,
                light.ip,
                r, g, b,
                kelvin=kelvin,
                duration_ms=FADE_DURATION_MS,
                brightness=bright_adj
            )
            
            if enable_perf_logging and send_start is not None:
                send_duration = (time.time() - send_start) * 1000
                if send_duration > PERF_SEND_THRESHOLD_MS:
                    dmx_logger.warning(f"    SLOW send: {send_duration:.1f}ms")
        
        elif channel_mode == 'HSBK (16bit)':
            # HSBK16: 16-bit HSBK - 8 channels: MSB+LSB for each parameter
            # Channels: H_MSB, H_LSB, S_MSB, S_LSB, B_MSB, B_LSB, K_MSB, K_LSB
            hue_16bit = (channel_values[0] << 8) | channel_values[1]  # 0-65535 → 0-360°
            saturation_16bit = (channel_values[2] << 8) | channel_values[3]  # 0-65535 → 0-100%
            brightness_16bit = (channel_values[4] << 8) | channel_values[5]  # 0-65535 → 0-100%
            kelvin_16bit = (channel_values[6] << 8) | channel_values[7]  # 0-65535 → 2500-9000K
            
            # Convert 16-bit values to their full ranges
            hue = (hue_16bit / 65535.0) * MAX_HUE  # 0-360°
            saturation = (saturation_16bit / 65535.0) * MAX_SATURATION  # 0-100%
            brightness_val = (brightness_16bit / 65535.0) * MAX_BRIGHTNESS  # 0-100%
            kelvin = int(2500 + (kelvin_16bit / 65535.0) * (9000 - 2500))  # 2500-9000K
            
            # Convert HS to RGB
            h = hue / MAX_HUE
            s = saturation / MAX_SATURATION
            v = brightness_val / MAX_BRIGHTNESS
            
            r, g, b = colorsys.hsv_to_rgb(h, s, v)
            
            bright_adj = brightness * v
            
            send_start = None
            if enable_perf_logging:
                send_start = time.time()
            
            lifx_client.set_rgb(
                light.target,
                light.ip,
                r, g, b,
                kelvin=kelvin,
                duration_ms=FADE_DURATION_MS,
                brightness=bright_adj
            )
            
            if enable_perf_logging and send_start is not None:
                send_duration = (time.time() - send_start) * 1000
                if send_duration > PERF_SEND_THRESHOLD_MS:
                    dmx_logger.warning(f"    SLOW send: {send_duration:.1f}ms")
    
    # Log total processing time for this DMX frame (only if performance logging enabled)
    if enable_perf_logging and process_start is not None:
        process_duration = (time.time() - process_start) * 1000
        # Log if threshold exceeded OR if this is a sampled frame
        if process_duration > PERF_PROCESS_THRESHOLD_MS:
            dmx_logger.warning(f"SLOW process: {process_duration:.1f}ms total for universe {universe}")
        elif should_log_frame:
            dmx_logger.info(f"Frame process: {process_duration:.1f}ms total for universe {universe}")


def dmx_worker():
    """Background thread for DMX processing"""
    global dmx_receiver, running
    
    if not dmx_receiver:
        return
    
    # Start the receiver first
    dmx_receiver.start()
    
    # Get all unique universes from mappings
    universes = set()
    for mapping in light_mappings.values():
        universe = mapping.get('universe')
        if universe is not None:
            universes.add(universe)
    
    # Set up listeners for each universe
    for universe in universes:
        try:
            dmx_receiver.listen_to_universe(universe, process_dmx_data)
            print(f"Listening to universe {universe}")
        except Exception as e:
            print(f"Error setting up listener for universe {universe}: {e}")
    
    while running:
        time.sleep(0.1)


def _restart_dmx_if_running():
    """Restart DMX worker if it's currently running (to pick up mapping changes)"""
    global running, dmx_receiver, dmx_thread, dmx_lock
    
    # Acquire lock and check preconditions inside lock to avoid TOCTOU issues
    with dmx_lock:
        # Check preconditions inside lock
        if not running:
            return  # Not running, nothing to restart
        
        if not dmx_receiver:
            return  # No receiver, nothing to restart
        
        # Store references for operations outside lock
        old_receiver = dmx_receiver
        old_thread = dmx_thread
        
        # Update state atomically
        running = False
        dmx_receiver = None
        dmx_thread = None
    
    # Perform potentially blocking operations outside lock
    try:
        # Stop the old receiver (may block)
        if old_receiver:
            old_receiver.stop()
            old_receiver.reset_stats()
        
        # Wait for thread to finish (may block)
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=1.0)
            # Check if thread is still alive after timeout
            if old_thread.is_alive():
                print("Warning: DMX worker thread did not finish within timeout, continuing anyway")
        
        # Close old receiver (may block)
        if old_receiver:
            try:
                old_receiver.close()
            except Exception as e:
                print(f"Warning: Error closing DMX receiver: {e}")
        
        # Prepare new receiver (may block)
        sacn_bind_ip = None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface
        new_receiver = DMXReceiver(bind_ip=sacn_bind_ip)
        
        # Create new thread
        new_thread = threading.Thread(target=dmx_worker, daemon=True)
        
        # Acquire lock again for final state mutations
        with dmx_lock:
            # Double-check that no other thread has interfered (e.g., called stop_dmx)
            # If dmx_receiver is not None, another thread may have started/stopped it
            if dmx_receiver is not None:
                # Another thread has modified state, clean up and abort
                try:
                    new_receiver.close()
                except Exception as e:
                    print(f"Warning: Error closing new receiver after state conflict: {e}")
                return
            
            # Update state atomically
            dmx_receiver = new_receiver
            running = True
            dmx_thread = new_thread
        
        # Start thread outside lock (may block briefly)
        new_thread.start()
        print("DMX worker restarted successfully")
    except Exception as e:
        print(f"Error restarting DMX worker: {e}")
        import traceback
        traceback.print_exc()
        # Ensure state is consistent on error
        with dmx_lock:
            running = False
            if dmx_receiver:
                dmx_receiver = None
            if dmx_thread:
                dmx_thread = None


# =========================
# WEB UI ROUTES
# =========================

@app.route('/')
def index():
    """Main web interface"""
    return render_template('index.html', version=VERSION)


@app.route('/api/interfaces', methods=['GET'])
def get_interfaces():
    """Get list of available network interfaces"""
    interfaces = get_network_interfaces()
    return jsonify({
        'success': True,
        'interfaces': interfaces,
        'lifx_interface': lifx_interface,
        'sacn_interface': sacn_interface
    })


@app.route('/api/settings/interfaces', methods=['POST'])
def set_interfaces():
    """Set the network interfaces to use"""
    global lifx_interface, sacn_interface
    
    data = request.json
    lifx_ip = data.get('lifx_interface')
    sacn_ip = data.get('sacn_interface')
    
    if lifx_ip is None or sacn_ip is None:
        return jsonify({'success': False, 'error': 'lifx_interface and sacn_interface required'}), 400
    
    lifx_interface = lifx_ip
    sacn_interface = sacn_ip
    save_config()
    
    return jsonify({
        'success': True,
        'lifx_interface': lifx_interface,
        'sacn_interface': sacn_interface
    })


@app.route('/api/settings/interfaces/apply', methods=['POST'])
def apply_interfaces():
    """Apply the network interface settings (recreate clients)"""
    global lifx_client, dmx_receiver
    
    lifx_bind_ip = _normalize_interface_ip(lifx_interface)
    sacn_bind_ip = None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface
    
    # Recreate LIFX client with new interface if it exists
    if lifx_client:
        lifx_client.close()
        lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    
    # Recreate DMX receiver with new interface if it exists
    if dmx_receiver:
        dmx_receiver.close()
        dmx_receiver = DMXReceiver(bind_ip=sacn_bind_ip)
    
    return jsonify({
        'success': True,
        'message': 'Network interfaces applied successfully'
    })


@app.route('/api/lights/discover', methods=['POST'])
def discover_lights():
    """Discover LIFX lights on the network"""
    global lifx_client
    
    lifx_bind_ip = _normalize_interface_ip(lifx_interface)
    
    if not lifx_client:
        lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    else:
        # Check if we need to recreate with new interface
        try:
            current_bind = lifx_client.sock.getsockname()[0]
            if current_bind != lifx_bind_ip and lifx_bind_ip != '0.0.0.0':
                # Interface changed, recreate client
                lifx_client.close()
                lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
        except Exception as e:
            # Socket might be closed, recreate
            print(f"Warning: Error checking LIFX client socket, recreating client: {e}")
            lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    
    try:
        lights = lifx_client.discover_lights(timeout=5.0)
        lights_data = [
            {
                'id': light_id(light),
                'label': light.label or f"Light {light.ip}",
                'ip': light.ip,
                'target': light.target.hex(),
                'model': light.model_name,
                'product_id': light.product,
                'supported_modes': getattr(light, 'supported_modes', ['RGB (8bit)', 'RGB (16bit)', 'RGBW (8bit)', 'RGBW (16bit)', 'HSBK (8bit)', 'HSBK (16bit)'])
            }
            for light in lights
        ]
        return jsonify({'success': True, 'lights': lights_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/lights', methods=['GET'])
def get_lights():
    """Get current list of discovered lights and configured lights"""
    global lifx_client
    
    lights_data = []
    
    def _build_light_data(light: LifxLight, discovered: bool = True) -> Dict:
        """Build light data dictionary for API response"""
        lid = light_id(light)
        
        return {
            'id': lid,
            'label': light.label or f"Light {light.ip}",
            'ip': light.ip,
            'target': light.target.hex(),
            'model': light.model_name,
            'product_id': light.product,
            'supported_modes': getattr(light, 'supported_modes', ['RGB (8bit)', 'RGB (16bit)', 'RGBW (8bit)', 'RGBW (16bit)', 'HSBK (8bit)', 'HSBK (16bit)']),
            'mapping': light_mappings.get(lid, {}),
            'discovered': discovered
        }
    
    # Use dict to handle deduplication - prefer discovered lights over undiscovered
    all_lights = {}
    
    # Get discovered lights first (these take priority)
    if lifx_client:
        lights = lifx_client.get_lights()
        for light in lights:
            lid = light_id(light)
            # Always prefer discovered lights
            all_lights[lid] = _build_light_data(light, discovered=True)
    
    # Add configured lights that aren't currently discovered
    for mapped_light_id, mapping in light_mappings.items():
        if mapped_light_id not in all_lights:
            # Create a minimal light object for undiscovered lights
            stored_label = mapping.get('label') or f"Light {mapped_light_id[:8]}..."
            stored_model = mapping.get('model') or 'Not discovered'
            stored_ip = mapping.get('ip') or 'Not discovered'
            
            # Create a minimal LifxLight object for the helper function
            # Handle placeholder IDs (manual_xxx) by creating a dummy target
            try:
                if mapped_light_id.startswith('manual_'):
                    # Create a dummy target from the hash part
                    target_bytes = bytes.fromhex(mapped_light_id.replace('manual_', '').ljust(32, '0')[:32])
                else:
                    target_bytes = bytes.fromhex(mapped_light_id)
                fake_light = LifxLight(target_bytes, stored_ip, stored_label)
            except (ValueError, TypeError):
                # Fallback: create a dummy target
                fake_light = LifxLight(b'\x00' * 6, stored_ip, stored_label)
            
            fake_light.model_name = stored_model
            fake_light.product = 0
            fake_light.supported_modes = ['RGB (8bit)', 'RGB (16bit)', 'RGBW (8bit)', 'RGBW (16bit)', 'HSBK (8bit)', 'HSBK (16bit)']
            
            all_lights[mapped_light_id] = _build_light_data(fake_light, discovered=False)
    
    
    # Separate into configured and unconfigured lights (backend handles all logic)
    configured_lights = []
    unconfigured_lights = []
    manual_lights_needing_config = []
    
    for light in all_lights.values():
        mapping = light.get('mapping', {})
        universe = mapping.get('universe')
        start_channel = mapping.get('start_channel')
        
        # Check if light has a complete mapping
        has_complete_mapping = (universe is not None and universe != '' and 
                               start_channel is not None and start_channel != '')
        
        # Check if it's a manual light with partial mapping
        has_partial_mapping = (mapping and len(mapping) > 0 and not has_complete_mapping and 
                               not light.get('discovered', True))
        
        if has_complete_mapping:
            configured_lights.append(light)
        elif has_partial_mapping:
            manual_lights_needing_config.append(light)
        elif light.get('discovered', False):
            # Only include discovered lights that aren't configured
            unconfigured_lights.append(light)
    
    # Combine configured and manual lights for backward compatibility
    all_configured = configured_lights + manual_lights_needing_config
    
    return jsonify({
        'success': True,
        'configured_lights': configured_lights,
        'unconfigured_lights': unconfigured_lights,
        'manual_lights': manual_lights_needing_config,
        'all_configured_lights': all_configured,  # For frontend convenience
        # Keep 'lights' for backward compatibility (all lights combined)
        'lights': list(all_lights.values())
    })


@app.route('/api/mappings', methods=['GET'])
def get_mappings():
    """Get current light mappings"""
    return jsonify({'success': True, 'mappings': light_mappings})


@app.route('/api/config/reload', methods=['POST'])
def reload_config():
    """Reload configuration from file"""
    global light_mappings, lifx_interface, sacn_interface, dmx_receiver, dmx_thread, running
    
    try:
        # Load config from file
        load_config()
        
        # Restart DMX worker if running to pick up mapping changes
        if running:
            _restart_dmx_if_running()
        
        return jsonify({
            'success': True,
            'message': 'Configuration reloaded successfully',
            'mappings_count': len(light_mappings)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mappings', methods=['POST'])
def update_mapping():
    """Update mapping for a light"""
    global light_mappings, dmx_receiver, dmx_thread, lifx_client
    
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Request must be JSON'}), 400
        
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'Request body is required'}), 400
        
        mapped_light_id = data.get('light_id')
        
        if not mapped_light_id:
            return jsonify({'success': False, 'error': 'light_id required'}), 400
        
        # Get existing mapping to preserve label/model/ip if light isn't currently discovered
        existing_mapping = light_mappings.get(mapped_light_id, {})
        
        # Try to get light info if available
        light_label = existing_mapping.get('label')  # Preserve existing if available
        light_model = existing_mapping.get('model')  # Preserve existing if available
        light_ip = existing_mapping.get('ip')  # Preserve existing if available
        
        if lifx_client:
            lights = lifx_client.get_lights()
            for light in lights:
                if light_id(light) == mapped_light_id:
                    # Update with current discovered info
                    light_label = light.label
                    light_model = light.model_name
                    light_ip = light.ip
                    break
        
        # Get values from request
        universe = data.get('universe')
        start_channel = data.get('start_channel')
        brightness = data.get('brightness')
        channel_mode = data.get('channel_mode')
        
        # Validate required fields - check for None, empty string, or 0
        if universe is None or universe == '' or universe == 0:
            return jsonify({'success': False, 'error': 'universe is required and must be greater than 0'}), 400
        if start_channel is None or start_channel == '' or start_channel == 0:
            return jsonify({'success': False, 'error': 'start_channel is required and must be greater than 0'}), 400
        
        # Convert values with error handling
        try:
            universe_int = int(universe)
            start_channel_int = int(start_channel)
        except (ValueError, TypeError) as e:
            return jsonify({'success': False, 'error': f'Invalid universe or start_channel: {str(e)}'}), 400
        
        try:
            brightness_float = float(brightness) if brightness is not None else existing_mapping.get('brightness', MAX_BRIGHTNESS)
        except (ValueError, TypeError):
            brightness_float = existing_mapping.get('brightness', MAX_BRIGHTNESS)
        
        # Build mapping with explicit values from request
        mapping = {
            'universe': universe_int,
            'start_channel': start_channel_int,
            'brightness': brightness_float,
            'channel_mode': str(channel_mode) if channel_mode else existing_mapping.get('channel_mode', 'RGB (8bit)'),
            'label': light_label,  # Store label for display when not discovered
            'model': light_model,  # Store model for display when not discovered
            'ip': light_ip  # Store IP for auto-discovery
        }
        
        light_mappings[mapped_light_id] = mapping
        save_config()
        
        # Restart DMX worker if running
        _restart_dmx_if_running()
        
        return jsonify({'success': True, 'mapping': mapping})
    
    except Exception as e:
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'}), 500


@app.route('/api/mappings/<light_id>', methods=['DELETE'])
def delete_mapping(light_id):
    """Delete mapping for a light"""
    global light_mappings
    
    if light_id in light_mappings:
        del light_mappings[light_id]
        save_config()
        
        # Restart DMX worker if running
        _restart_dmx_if_running()
        
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Mapping not found'}), 404


@app.route('/api/lights/manual', methods=['POST'])
def add_manual_light():
    """Manually add a light by IP address"""
    global lifx_client, light_mappings
    
    data = request.json
    ip = data.get('ip', '').strip()
    label = data.get('label', '').strip()
    
    if not ip:
        return jsonify({'success': False, 'error': 'IP address is required'}), 400
    
    # Validate IP format (basic check)
    try:
        socket.inet_aton(ip)
    except socket.error:
        return jsonify({'success': False, 'error': 'Invalid IP address format'}), 400
    
    # Check if this IP is already mapped
    for existing_id, mapping in light_mappings.items():
        if mapping.get('ip') == ip:
            return jsonify({'success': False, 'error': f'Light with IP {ip} is already configured'}), 400
    
    # Try to probe the light to get its target MAC address
    light = None
    if lifx_client:
        try:
            light = lifx_client.probe_light_by_ip(ip, timeout=3.0)
            if light:
                # Request label and version info
                lifx_client._request_label(light)
                lifx_client._request_version(light)
                time.sleep(0.5)  # Wait for responses
        except Exception as e:
            print(f"Error probing light at {ip}: {e}")
    
    # Generate light_id
    if light and light.target:
        # Use the actual target MAC address
        lid = light.target.hex()
        light_label = light.label or label or f"Light {ip}"
        light_model = light.model_name or 'Unknown Model'
        supported_modes = getattr(light, 'supported_modes', ['RGB (8bit)', 'RGB (16bit)', 'RGBW (8bit)', 'RGBW (16bit)', 'HSBK (8bit)', 'HSBK (16bit)'])
    else:
        # Create a placeholder ID based on IP (will be updated when discovered)
        # Use a hash of the IP to create a consistent ID
        import hashlib
        ip_hash = hashlib.md5(ip.encode()).hexdigest()[:16]
        lid = f"manual_{ip_hash}"
        light_label = label or f"Light {ip}"
        light_model = 'Not discovered'
        supported_modes = ['RGB (8bit)', 'RGB (16bit)', 'RGBW (8bit)', 'RGBW (16bit)', 'HSBK (8bit)', 'HSBK (16bit)']
    
    # Check if mapping already exists for this light_id
    if lid in light_mappings:
        return jsonify({'success': False, 'error': 'This light is already configured'}), 400
    
    # Create a basic mapping entry (user will need to configure universe/channel separately)
    mapping = {
        'ip': ip,
        'label': light_label,
        'model': light_model,
        'brightness': MAX_BRIGHTNESS,
        'channel_mode': 'RGB (8bit)',
        'universe': None,  # User needs to configure this
        'start_channel': None  # User needs to configure this
    }
    
    light_mappings[lid] = mapping
    save_config()
    
    # If light was discovered, add it to the client's lights list
    if light and lifx_client:
        lifx_client.lights[lid] = light
    
    return jsonify({
        'success': True,
        'light_id': lid,
        'light': {
            'id': lid,
            'label': light_label,
            'ip': ip,
            'model': light_model,
            'supported_modes': supported_modes,
            'discovered': light is not None
        }
    })


@app.route('/api/control/start', methods=['POST'])
def start_dmx():
    """Start DMX processing"""
    global running, dmx_receiver, dmx_thread, lifx_client, dmx_lock
    
    with dmx_lock:
        if running:
            return jsonify({'success': False, 'error': 'Already running'}), 400
        
        if not lifx_client:
            return jsonify({'success': False, 'error': 'No lights discovered'}), 400
    
    try:
        sacn_bind_ip = None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface
        
        # Close existing receiver if it exists (outside lock to avoid blocking)
        if dmx_receiver:
            try:
                dmx_receiver.close()
            except Exception as e:
                print(f"Warning: Error closing existing DMX receiver: {e}")
            dmx_receiver = None
        
        # Create new receiver
        dmx_receiver = DMXReceiver(bind_ip=sacn_bind_ip)
        
        with dmx_lock:
            running = True
            dmx_thread = threading.Thread(target=dmx_worker, daemon=True)
            dmx_thread.start()
        
        return jsonify({'success': True})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/control/stop', methods=['POST'])
def stop_dmx():
    """Stop DMX processing"""
    global running, dmx_receiver, dmx_lock
    
    with dmx_lock:
        running = False
    
    if dmx_receiver:
        dmx_receiver.stop()
        dmx_receiver.reset_stats()
    
    return jsonify({'success': True})


@app.route('/api/control/status', methods=['GET'])
def get_status():
    """Get current status"""
    dmx_stats = {}
    if dmx_receiver:
        stats = dmx_receiver.get_stats()
        dmx_stats = {
            'packets_received': stats['packets_received'],
            'last_packet_time': stats['last_packet_time'],
            'active_universes': stats['active_universes'],
            'packets_per_universe': stats['packets_per_universe'],
            'receiving': stats['running'] and stats['last_packet_time'] is not None and (time.time() - stats['last_packet_time']) < 2.0  # Receiving if packet in last 2 seconds
        }
    
    # Count discovered lights (not including configured but undiscovered)
    discovered_count = len(lifx_client.get_lights()) if lifx_client else 0
    
    return jsonify({
        'success': True,
        'running': running,
        'lights_count': discovered_count,
        'mappings_count': len(light_mappings),
        'dmx_stats': dmx_stats
    })


@app.route('/api/lights/test-rgb', methods=['POST'])
def test_rgb():
    """Test RGB values directly on a light (DMX-less testing)"""
    global lifx_client
    
    if not lifx_client:
        return jsonify({'success': False, 'error': 'LIFX client not initialized'}), 400
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Request body is required'}), 400
    
    requested_light_id = data.get('light_id')
    r = data.get('r', 0)
    g = data.get('g', 0)
    b = data.get('b', 0)
    brightness = data.get('brightness', 1.0)
    fade_ms = data.get('fade_ms', FADE_DURATION_MS)
    
    if not requested_light_id:
        return jsonify({'success': False, 'error': 'light_id is required'}), 400
    
    # Validate RGB values (0-255)
    if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
        return jsonify({'success': False, 'error': 'RGB values must be 0-255'}), 400
    
    # Validate brightness (0.0-1.0)
    if not (0.0 <= brightness <= 1.0):
        return jsonify({'success': False, 'error': 'Brightness must be 0.0-1.0'}), 400
    
    # Find the light
    lights = lifx_client.get_lights()
    light = None
    for l in lights:
        if light_id(l) == requested_light_id:
            light = l
            break
    
    # Also check raw lights for configured lights (e.g., LIFX Switch that was filtered out)
    if not light:
        with lifx_client.lock:
            for target, l in lifx_client.lights.items():
                if light_id(l) == requested_light_id:
                    light = l
                    break
    
    if not light:
        return jsonify({'success': False, 'error': 'Light not found'}), 404
    
    try:
        # Convert RGB from 0-255 to 0.0-1.0
        r_norm = r / 255.0
        g_norm = g / 255.0
        b_norm = b / 255.0
        
        # Send RGB to light
        lifx_client.set_rgb(
            light.target,
            light.ip,
            r_norm, g_norm, b_norm,
            kelvin=DEFAULT_KELVIN,
            duration_ms=fade_ms,
            brightness=brightness
        )
        
        return jsonify({
            'success': True,
            'message': f'Sent RGB({r},{g},{b}) to {light.label}',
            'light_label': light.label
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def auto_discover_configured_lights():
    """Automatically discover configured lights by their saved IP addresses"""
    global lifx_client, light_mappings
    
    if not light_mappings:
        return
    
    # Initialize LIFX client if needed
    if not lifx_client:
        lifx_bind_ip = _normalize_interface_ip(lifx_interface)
        lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    
    print(f"Auto-discovering {len(light_mappings)} configured light(s)...")
    
    # Probe each configured light by its saved IP
    for light_id, mapping in light_mappings.items():
        saved_ip = mapping.get('ip')
        if saved_ip and saved_ip != 'Not discovered':
            try:
                print(f"  Probing {saved_ip} ({mapping.get('label', light_id[:8])})...")
                discovered_light = lifx_client.probe_light_by_ip(saved_ip, timeout=1.5)
                if discovered_light:
                    print(f"    ✓ Found: {discovered_light.label} ({discovered_light.model_name})")
                else:
                    print(f"    ✗ Not found at {saved_ip}")
            except Exception as e:
                print(f"    ✗ Error probing {saved_ip}: {e}")
    
    print("Auto-discovery complete.")


if __name__ == '__main__':
    load_config()
    
    print(f"sACN2LIFX v{VERSION} Server starting...")
    
    # Auto-discover configured lights on startup
    auto_discover_configured_lights()
    
    print("Open http://localhost:5001 in your browser")
    
    app.run(host='0.0.0.0', port=5001, debug=True)

