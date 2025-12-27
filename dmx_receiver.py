import sacn
import threading
import time
from typing import Dict, Callable, Optional

class DMXReceiver:
    """E1.31 (sACN) DMX receiver"""
    
    def __init__(self, bind_ip: Optional[str] = None):
        self.bind_ip = bind_ip
        self.receiver = None
        self.universe_callbacks: Dict[int, Callable] = {}
        self.running = False
        self.stats = {
            'packets_received': 0,
            'last_packet_time': None,
            'active_universes': set(),
            'packets_per_universe': {}
        }
        self.stats_lock = threading.Lock()
        self._start_receiver()
    
    def _start_receiver(self):
        """Start the sACN receiver"""
        if self.receiver is not None:
            try:
                self.receiver.stop()
            except:
                pass
        
        self.receiver = sacn.sACNreceiver(self.bind_ip) if self.bind_ip else sacn.sACNreceiver()
        self.receiver.start()
        
    def listen_to_universe(self, universe: int, callback: Callable):
        """Register a callback for a specific DMX universe"""
        if not self.receiver:
            raise RuntimeError("Receiver not initialized")
        
        self.universe_callbacks[universe] = callback
        print(f"Registering listener for universe {universe}")
        
        # Create a handler function for this universe
        def handle_dmx(packet):
            try:
                if not self.running:
                    return  # Don't process if not running
                
                # Update statistics
                with self.stats_lock:
                    self.stats['packets_received'] += 1
                    self.stats['last_packet_time'] = time.time()
                    self.stats['active_universes'].add(universe)
                    if universe not in self.stats['packets_per_universe']:
                        self.stats['packets_per_universe'][universe] = 0
                    self.stats['packets_per_universe'][universe] += 1
                
                # Extract DMX data from packet
                dmx_data = None
                if hasattr(packet, 'dmxData'):
                    dmx_data = packet.dmxData
                elif hasattr(packet, 'dmx_data'):
                    dmx_data = packet.dmx_data
                elif hasattr(packet, 'dmx'):
                    dmx_data = packet.dmx
                elif isinstance(packet, (list, tuple)):
                    dmx_data = list(packet)
                elif hasattr(packet, '__iter__') and not isinstance(packet, (str, bytes)):
                    dmx_data = list(packet)
                else:
                    print(f"Warning: Could not extract DMX data from packet for universe {universe}. Packet type: {type(packet)}, attributes: {dir(packet)[:10]}")
                    return
                
                # Call the callback with DMX data
                if dmx_data is not None:
                    callback(dmx_data, universe)
            except Exception as e:
                print(f"Error in handle_dmx for universe {universe}: {e}")
                import traceback
                traceback.print_exc()
        
        # Register the listener using register_listener method (more reliable than decorator pattern)
        try:
            self.receiver.register_listener('universe', handle_dmx, universe=universe)
        except Exception as e:
            print(f"Error registering listener for universe {universe}: {e}")
            raise
        
        # Join multicast for this universe
        try:
            self.receiver.join_multicast(universe)
        except Exception as e:
            print(f"Error joining multicast for universe {universe}: {e}")
            # Continue anyway as unicast might still work
    
    def get_stats(self) -> Dict:
        """Get current reception statistics"""
        with self.stats_lock:
            # Check if we're receiving data (packet received in last 2 seconds)
            receiving = False
            if self.stats['last_packet_time']:
                time_since_last = time.time() - self.stats['last_packet_time']
                receiving = time_since_last < 2.0 and self.running
            
            return {
                'packets_received': self.stats['packets_received'],
                'last_packet_time': self.stats['last_packet_time'],
                'active_universes': sorted(list(self.stats['active_universes'])),
                'packets_per_universe': dict(self.stats['packets_per_universe']),
                'running': self.running,
                'receiving': receiving
            }
    
    def reset_stats(self):
        """Reset statistics"""
        with self.stats_lock:
            self.stats = {
                'packets_received': 0,
                'last_packet_time': None,
                'active_universes': set(),
                'packets_per_universe': {}
            }
    
    def start(self):
        """Start receiving DMX data"""
        self.running = True
    
    def stop(self):
        """Stop receiving DMX data"""
        self.running = False
        self.receiver.stop()
    
    def close(self):
        """Close the receiver"""
        self.stop()

