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
        self.universe_callbacks[universe] = callback
        
        # Create a handler function for this universe
        def handle_dmx(packet):
            if self.running:
                # Update statistics
                with self.stats_lock:
                    self.stats['packets_received'] += 1
                    self.stats['last_packet_time'] = time.time()
                    self.stats['active_universes'].add(universe)
                    if universe not in self.stats['packets_per_universe']:
                        self.stats['packets_per_universe'][universe] = 0
                    self.stats['packets_per_universe'][universe] += 1
                
                callback(packet.dmxData, universe)
        
        # Register the listener using the decorator pattern
        self.receiver.listen_on('universe', universe=universe)(handle_dmx)
        
        # Join multicast for this universe
        self.receiver.join_multicast(universe)
    
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

