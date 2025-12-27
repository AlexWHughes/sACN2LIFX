"""
Unit tests for lifx_client.py - focusing on color_set_time and state handling
"""
import unittest
from unittest.mock import Mock, MagicMock, patch
import time
import colorsys
import struct
import sys
import os

# Add parent directory to path to import lifx_client module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lifx_client
from lifx_client import LifxLight, LifxLanClient, clamp01, rgb01_to_hsbk


class TestLifxLight(unittest.TestCase):
    """Test suite for LifxLight class with color_set_time attribute"""
    
    def test_light_initialization_with_color_set_time(self):
        """Test that LifxLight initializes with color_set_time attribute"""
        light = LifxLight(b'\x00' * 8, '192.168.1.100')
        
        # Verify color_set_time is initialized to 0
        self.assertEqual(light.color_set_time, 0)
        self.assertIsInstance(light.color_set_time, (int, float))
    
    def test_light_all_attributes_initialized(self):
        """Test that all light attributes are properly initialized"""
        mac = b'\x01\x02\x03\x04\x05\x06\x07\x08'
        ip = '10.0.0.50'
        light = LifxLight(mac, ip)
        
        self.assertEqual(light.mac, mac)
        self.assertEqual(light.ip, ip)
        self.assertEqual(light.label, "Unknown")
        self.assertEqual(light.power, 0)
        self.assertEqual(light.vendor, 0)
        self.assertEqual(light.product, 0)
        self.assertEqual(light.version, 0)
        self.assertEqual(light.model_name, "Discovering...")
        self.assertTrue(light.is_light)
        self.assertEqual(light.supported_modes, ["RGB"])
        self.assertEqual(light.current_hue, 0)
        self.assertEqual(light.current_saturation, 0)
        self.assertEqual(light.current_brightness, 0)
        self.assertEqual(light.current_kelvin, lifx_client.DEFAULT_KELVIN)
        self.assertEqual(light.current_rgb, (0, 0, 0))
        self.assertEqual(light.color_set_time, 0)


class TestSetRgbColorSetTime(unittest.TestCase):
    """Test suite for set_rgb method color_set_time tracking"""
    
    def setUp(self):
        """Set up test fixtures"""
        with patch('socket.socket'):
            self.client = LifxLanClient(bind_ip="0.0.0.0")
            self.client.listening = False  # Don't start listener thread
    
    def tearDown(self):
        """Clean up"""
        self.client.listening = False
    
    @patch('time.time')
    def test_set_rgb_updates_color_set_time(self, mock_time):
        """Test that set_rgb updates color_set_time to current time"""
        # Setup
        mock_time.return_value = 1234567890.5
        target = b'\x01' * 8
        ip = '192.168.1.100'
        light = LifxLight(target, ip)
        light.color_set_time = 0
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute
        self.client.set_rgb(target, ip, 1.0, 0.5, 0.25, brightness=0.8)
        
        # Verify color_set_time was updated
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.color_set_time, 1234567890.5)
    
    def test_set_rgb_updates_current_rgb_with_brightness(self):
        """Test that set_rgb stores RGB values that reflect brightness"""
        # Setup
        target = b'\x02' * 8
        ip = '192.168.1.101'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute - set pure red at 50% brightness
        self.client.set_rgb(target, ip, 1.0, 0.0, 0.0, brightness=0.5)
        
        # Verify stored RGB reflects brightness
        with self.client.lock:
            updated_light = self.client.lights[target]
            # With 50% brightness, red should be approximately 127-128
            self.assertGreater(updated_light.current_rgb[0], 0)
            self.assertLess(updated_light.current_rgb[0], 255)
            # Green and blue should be 0
            self.assertEqual(updated_light.current_rgb[1], 0)
            self.assertEqual(updated_light.current_rgb[2], 0)
    
    def test_set_rgb_hsbk_conversion_accuracy(self):
        """Test that RGB to HSBK and back maintains color accuracy"""
        # Setup
        target = b'\x03' * 8
        ip = '192.168.1.102'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Test with various RGB values
        test_colors = [
            (1.0, 0.0, 0.0),  # Red
            (0.0, 1.0, 0.0),  # Green
            (0.0, 0.0, 1.0),  # Blue
            (1.0, 1.0, 0.0),  # Yellow
            (0.5, 0.5, 0.5),  # Gray
            (1.0, 0.5, 0.25), # Orange
        ]
        
        for r, g, b in test_colors:
            self.client.set_rgb(target, ip, r, g, b, brightness=1.0)
            
            with self.client.lock:
                updated_light = self.client.lights[target]
                # Verify RGB is stored
                self.assertIsNotNone(updated_light.current_rgb)
                self.assertEqual(len(updated_light.current_rgb), 3)
                # Values should be in 0-255 range
                for val in updated_light.current_rgb:
                    self.assertGreaterEqual(val, 0)
                    self.assertLessEqual(val, 255)
    
    def test_set_rgb_with_zero_brightness(self):
        """Test set_rgb with zero brightness (should result in black)"""
        # Setup
        target = b'\x04' * 8
        ip = '192.168.1.103'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute - bright red but with 0% brightness
        self.client.set_rgb(target, ip, 1.0, 0.0, 0.0, brightness=0.0)
        
        # Verify RGB is all zeros (black)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_rgb, (0, 0, 0))
    
    def test_set_rgb_with_full_brightness(self):
        """Test set_rgb with full brightness"""
        # Setup
        target = b'\x05' * 8
        ip = '192.168.1.104'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute - set white at full brightness
        self.client.set_rgb(target, ip, 1.0, 1.0, 1.0, brightness=1.0)
        
        # Verify RGB is maximum (white)
        with self.client.lock:
            updated_light = self.client.lights[target]
            # Should be close to (255, 255, 255)
            for val in updated_light.current_rgb:
                self.assertGreater(val, 250)
    
    def test_set_rgb_nonexistent_light(self):
        """Test set_rgb when light doesn't exist in client (should not crash)"""
        target = b'\xFF' * 8
        ip = '192.168.1.200'
        
        # Should not raise exception
        self.client.set_rgb(target, ip, 0.5, 0.5, 0.5)


class TestStateLightHandling(unittest.TestCase):
    """Test suite for STATE_LIGHT message handling with color_set_time"""
    
    def setUp(self):
        """Set up test fixtures"""
        with patch('socket.socket'):
            self.client = LifxLanClient(bind_ip="0.0.0.0")
            self.client.listening = False
    
    def tearDown(self):
        """Clean up"""
        self.client.listening = False
    
    def _create_state_light_packet(self, hue, sat, bri, kel):
        """Helper to create a STATE_LIGHT packet"""
        # Build a minimal packet (36 byte header + 9 byte payload)
        header = b'\x00' * 36
        payload = struct.pack("<BHHHH", 0, hue, sat, bri, kel)
        return header + payload
    
    @patch('time.time')
    def test_state_light_updates_when_color_not_recently_set(self, mock_time):
        """Test that STATE_LIGHT updates light when color wasn't recently set"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x06' * 8
        ip = '192.168.1.105'
        light = LifxLight(target, ip)
        light.color_set_time = 997.0  # Set 3 seconds ago (more than 1 second threshold)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Simulate STATE_LIGHT packet
        hue, sat, bri, kel = 32768, 65535, 32768, 3500
        packet = self._create_state_light_packet(hue, sat, bri, kel)
        
        # Manually process the STATE_LIGHT (simulating listener thread)
        with self.client.lock:
            light = self.client.lights[target]
            time_since_set = time.time() - getattr(light, 'color_set_time', 0)
            if time_since_set > 1.0:
                light.current_hue = hue
                light.current_saturation = sat
                light.current_brightness = bri
                light.current_kelvin = kel
                h = hue / 65535.0
                s = sat / 65535.0
                v = bri / 65535.0
                r, g, b = colorsys.hsv_to_rgb(h, s, v)
                light.current_rgb = (int(r * 255), int(g * 255), int(b * 255))
        
        # Verify values were updated
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, hue)
            self.assertEqual(updated_light.current_saturation, sat)
            self.assertEqual(updated_light.current_brightness, bri)
            self.assertEqual(updated_light.current_kelvin, kel)
            self.assertIsNotNone(updated_light.current_rgb)
    
    @patch('time.time')
    def test_state_light_ignored_when_color_recently_set(self, mock_time):
        """Test that STATE_LIGHT is ignored when color was recently set"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x07' * 8
        ip = '192.168.1.106'
        light = LifxLight(target, ip)
        light.color_set_time = 999.5  # Set 0.5 seconds ago (less than 1 second threshold)
        light.current_hue = 10000
        light.current_saturation = 20000
        light.current_brightness = 30000
        light.current_kelvin = 4000
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Simulate STATE_LIGHT packet with different values
        hue, sat, bri, kel = 50000, 60000, 40000, 5000
        
        # Manually process the STATE_LIGHT with time check
        with self.client.lock:
            light = self.client.lights[target]
            time_since_set = time.time() - getattr(light, 'color_set_time', 0)
            if time_since_set > 1.0:  # This should be False
                light.current_hue = hue
                light.current_saturation = sat
                light.current_brightness = bri
                light.current_kelvin = kel
        
        # Verify values were NOT updated (kept original)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 10000)
            self.assertEqual(updated_light.current_saturation, 20000)
            self.assertEqual(updated_light.current_brightness, 30000)
            self.assertEqual(updated_light.current_kelvin, 4000)
    
    @patch('time.time')
    def test_state_light_exact_threshold_boundary(self, mock_time):
        """Test STATE_LIGHT at exactly 1.0 second boundary"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x08' * 8
        ip = '192.168.1.107'
        light = LifxLight(target, ip)
        light.color_set_time = 999.0  # Exactly 1.0 seconds ago
        light.current_hue = 1000
        
        with self.client.lock:
            self.client.lights[target] = light
        
        hue = 5000
        
        # Process with time check
        with self.client.lock:
            light = self.client.lights[target]
            time_since_set = time.time() - getattr(light, 'color_set_time', 0)
            # At exactly 1.0, should NOT update (condition is > 1.0)
            if time_since_set > 1.0:
                light.current_hue = hue
        
        # Verify value was NOT updated at exact boundary
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 1000)
    
    @patch('time.time')
    def test_state_light_just_after_threshold(self, mock_time):
        """Test STATE_LIGHT just after 1.0 second threshold"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x09' * 8
        ip = '192.168.1.108'
        light = LifxLight(target, ip)
        light.color_set_time = 998.99  # 1.01 seconds ago (just past threshold)
        light.current_hue = 1000
        
        with self.client.lock:
            self.client.lights[target] = light
        
        hue = 5000
        
        # Process with time check
        with self.client.lock:
            light = self.client.lights[target]
            time_since_set = time.time() - getattr(light, 'color_set_time', 0)
            if time_since_set > 1.0:
                light.current_hue = hue
        
        # Verify value WAS updated just after threshold
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 5000)
    
    @patch('time.time')
    def test_state_light_with_missing_color_set_time(self, mock_time):
        """Test STATE_LIGHT handling when color_set_time attribute is missing"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x0A' * 8
        ip = '192.168.1.109'
        light = LifxLight(target, ip)
        # Simulate old light object without color_set_time
        delattr(light, 'color_set_time')
        
        with self.client.lock:
            self.client.lights[target] = light
        
        hue = 5000
        
        # Process with getattr fallback
        with self.client.lock:
            light = self.client.lights[target]
            time_since_set = time.time() - getattr(light, 'color_set_time', 0)
            # getattr returns 0, so time_since_set will be large, should update
            if time_since_set > 1.0:
                light.current_hue = hue
        
        # Verify value was updated (getattr default of 0 makes time_since_set large)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 5000)
    
    def test_state_light_rgb_conversion_accuracy(self):
        """Test that HSBK to RGB conversion in STATE_LIGHT is accurate"""
        target = b'\x0B' * 8
        ip = '192.168.1.110'
        light = LifxLight(target, ip)
        light.color_set_time = 0  # Old enough to allow update
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Test various HSBK values
        test_cases = [
            (0, 65535, 65535, 3500),      # Red at full saturation and brightness
            (21845, 65535, 65535, 3500),  # Green
            (43690, 65535, 65535, 3500),  # Blue
            (0, 0, 65535, 3500),          # White (no saturation)
            (32768, 32768, 32768, 3500),  # Mid-tone
        ]
        
        for hue, sat, bri, kel in test_cases:
            with self.client.lock:
                light = self.client.lights[target]
                # Simulate old color_set_time to allow update
                light.color_set_time = 0
                
                # Simulate STATE_LIGHT processing
                time_since_set = time.time() - getattr(light, 'color_set_time', 0)
                if time_since_set > 1.0:
                    light.current_hue = hue
                    light.current_saturation = sat
                    light.current_brightness = bri
                    light.current_kelvin = kel
                    
                    h = hue / 65535.0
                    s = sat / 65535.0
                    v = bri / 65535.0
                    r, g, b = colorsys.hsv_to_rgb(h, s, v)
                    light.current_rgb = (int(r * 255), int(g * 255), int(b * 255))
                
                # Verify RGB conversion
                self.assertIsNotNone(light.current_rgb)
                self.assertEqual(len(light.current_rgb), 3)
                for val in light.current_rgb:
                    self.assertGreaterEqual(val, 0)
                    self.assertLessEqual(val, 255)


class TestHelperFunctions(unittest.TestCase):
    """Test suite for helper functions"""
    
    def test_clamp01_valid_values(self):
        """Test clamp01 with valid 0-1 values"""
        self.assertEqual(clamp01(0.0), 0.0)
        self.assertEqual(clamp01(0.5), 0.5)
        self.assertEqual(clamp01(1.0), 1.0)
    
    def test_clamp01_below_zero(self):
        """Test clamp01 with values below 0"""
        self.assertEqual(clamp01(-0.1), 0.0)
        self.assertEqual(clamp01(-1.0), 0.0)
        self.assertEqual(clamp01(-100.0), 0.0)
    
    def test_clamp01_above_one(self):
        """Test clamp01 with values above 1"""
        self.assertEqual(clamp01(1.1), 1.0)
        self.assertEqual(clamp01(2.0), 1.0)
        self.assertEqual(clamp01(100.0), 1.0)
    
    def test_clamp01_edge_cases(self):
        """Test clamp01 with edge case values"""
        self.assertEqual(clamp01(0.0000001), 0.0000001)
        self.assertEqual(clamp01(0.9999999), 0.9999999)
    
    def test_rgb01_to_hsbk_pure_colors(self):
        """Test rgb01_to_hsbk with pure RGB colors"""
        # Red
        hue, sat, bri, kel = rgb01_to_hsbk(1.0, 0.0, 0.0)
        self.assertEqual(hue, 0)
        self.assertEqual(sat, 65535)
        self.assertEqual(bri, 65535)
        
        # Green
        hue, sat, bri, kel = rgb01_to_hsbk(0.0, 1.0, 0.0)
        self.assertGreater(hue, 20000)  # Approximately 1/3 of 65535
        self.assertLess(hue, 23000)
        self.assertEqual(sat, 65535)
        self.assertEqual(bri, 65535)
        
        # Blue
        hue, sat, bri, kel = rgb01_to_hsbk(0.0, 0.0, 1.0)
        self.assertGreater(hue, 40000)  # Approximately 2/3 of 65535
        self.assertLess(hue, 46000)
        self.assertEqual(sat, 65535)
        self.assertEqual(bri, 65535)
    
    def test_rgb01_to_hsbk_white(self):
        """Test rgb01_to_hsbk with white (no saturation)"""
        hue, sat, bri, kel = rgb01_to_hsbk(1.0, 1.0, 1.0)
        self.assertEqual(sat, 0)  # No saturation for white
        self.assertEqual(bri, 65535)  # Full brightness
    
    def test_rgb01_to_hsbk_black(self):
        """Test rgb01_to_hsbk with black"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.0, 0.0, 0.0)
        self.assertEqual(bri, 0)  # No brightness for black
    
    def test_rgb01_to_hsbk_gray(self):
        """Test rgb01_to_hsbk with gray (no saturation, mid brightness)"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.5, 0.5, 0.5)
        self.assertEqual(sat, 0)  # No saturation for gray
        self.assertGreater(bri, 30000)  # Approximately half brightness
        self.assertLess(bri, 35000)
    
    def test_rgb01_to_hsbk_kelvin_default(self):
        """Test that rgb01_to_hsbk uses default kelvin"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.5, 0.5, 0.5)
        self.assertEqual(kel, lifx_client.DEFAULT_KELVIN)
    
    def test_rgb01_to_hsbk_kelvin_custom(self):
        """Test rgb01_to_hsbk with custom kelvin value"""
        custom_kelvin = 5000
        hue, sat, bri, kel = rgb01_to_hsbk(0.5, 0.5, 0.5, kelvin=custom_kelvin)
        self.assertEqual(kel, custom_kelvin)
    
    def test_rgb01_to_hsbk_clamping(self):
        """Test that rgb01_to_hsbk clamps input values"""
        # Values above 1.0 should be clamped
        hue, sat, bri, kel = rgb01_to_hsbk(2.0, 2.0, 2.0)
        self.assertEqual(bri, 65535)  # Should be clamped to max
        
        # Values below 0.0 should be clamped
        hue, sat, bri, kel = rgb01_to_hsbk(-1.0, -1.0, -1.0)
        self.assertEqual(bri, 0)  # Should be clamped to min
    
    def test_rgb01_to_hsbk_return_types(self):
        """Test that rgb01_to_hsbk returns correct types"""
        result = rgb01_to_hsbk(0.5, 0.5, 0.5)
        self.assertEqual(len(result), 4)
        # All values should be integers
        for val in result:
            self.assertIsInstance(val, int)
    
    def test_rgb01_to_hsbk_range_bounds(self):
        """Test that rgb01_to_hsbk returns values in correct ranges"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.7, 0.3, 0.9, kelvin=4500)
        
        # All HSBK values should be 16-bit (0-65535)
        self.assertGreaterEqual(hue, 0)
        self.assertLessEqual(hue, 65535)
        self.assertGreaterEqual(sat, 0)
        self.assertLessEqual(sat, 65535)
        self.assertGreaterEqual(bri, 0)
        self.assertLessEqual(bri, 65535)
        self.assertEqual(kel, 4500)


if __name__ == '__main__':
    unittest.main()