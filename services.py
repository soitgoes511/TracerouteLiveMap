import psutil
import threading
import time
import requests
from icmplib import traceroute as icmp_traceroute, multiping
from collections import deque

class GeoIPService:
    """Service to handle IP geolocation requests with rate limiting."""
    
    def __init__(self):
        self.cache = {}
        self.request_timestamps = deque()
        self.RATE_LIMIT = 45 # requests per minute
        self.WINDOW = 60 # seconds

    def _can_make_request(self):
        """Checks if a new request is allowed under the rate limit."""
        now = time.time()
        while self.request_timestamps and self.request_timestamps[0] < now - self.WINDOW:
            self.request_timestamps.popleft()
        
        return len(self.request_timestamps) < self.RATE_LIMIT

    def get_location(self, ip):
        """
        Fetches geolocation data for an IP address.
        
        Args:
            ip (str): The IP address to locate.
            
        Returns:
            dict: Location data (lat, lon, etc.) or error info.
        """
        # Quick filter for private ranges
        if ip.startswith('192.168.') or ip.startswith('10.') or ip.startswith('127.'):
            return None
            
        if ip in self.cache:
            return self.cache[ip]

        if not self._can_make_request():
            return {"error": "rate_limited"}

        try:
            self.request_timestamps.append(time.time())
            # Request 'org', 'as', and 'countryCode'
            response = requests.get(f'http://ip-api.com/json/{ip}?fields=status,message,lat,lon,city,isp,org,as,query,countryCode', timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data['status'] == 'success':
                    result = {
                        'lat': data['lat'], 
                        'lon': data['lon'], 
                        'isp': data['isp'], 
                        'city': data['city'],
                        'org': data.get('org', ''),
                        'asn': data.get('as', ''),
                        'country': data.get('countryCode', '')
                    }
                    self.cache[ip] = result
                    return result
        except Exception as e:
            print(f"GeoIP error for {ip}: {e}")
        return None
    
    def get_rate_limit_status(self):
         """Returns current rate limit status (requests remaining)."""
         now = time.time()
         while self.request_timestamps and self.request_timestamps[0] < now - self.WINDOW:
            self.request_timestamps.popleft()
         return {
             "remaining": self.RATE_LIMIT - len(self.request_timestamps),
             "reset_in": int(self.WINDOW - (now - self.request_timestamps[0])) if self.request_timestamps else 0
         }

class TracerouteEngine:
    """Handles background traceroute operations using ICMP."""
    
    def __init__(self, socketio, geo_service, app, db):
        self.socketio = socketio
        self.geo_service = geo_service
        self.app = app
        self.db = db
        self.queue = deque()
        self.processed_ips = set()
        self.running = False

    def add_target(self, ip):
        """Adds a new IP to the traceroute queue."""
        if ip not in self.processed_ips and ip not in self.queue:
            self.queue.append(ip)

    def start(self):
        """Starts the background traceroute worker."""
        self.running = True
        self.socketio.start_background_task(self._run)

    def _run(self):
        """Main worker loop processing the queue."""
        with self.app.app_context():
            while self.running:
                if self.queue:
                    ip = self.queue.popleft()
                    self.perform_traceroute(ip)
                    self.processed_ips.add(ip)
                else:
                    self.socketio.sleep(1)

    def perform_traceroute(self, target_ip):
        """
        Executes a traceroute to the target IP and emits results.
        
        Args:
            target_ip (str): Destination IP Address.
        """
        print(f"Tracerouting {target_ip}...")
        try:
            hops = icmp_traceroute(target_ip, count=1, interval=0.05, timeout=1, max_hops=20, fast=True)
            
            path_data = []
            for hop in hops:
                hop_info = {
                    'distance': hop.distance,
                    'address': hop.address,
                    'avg_rtt': hop.avg_rtt,
                }
                geo = self.geo_service.get_location(hop.address)
                if geo and 'error' not in geo:
                    hop_info.update(geo)
                
                path_data.append(hop_info)
            
            final_geo = self.geo_service.get_location(target_ip)
            
            # Persist Latency Sample (using the RTT of the last successful hop or a specific ping if needed)
            # icmplib traceroute gives avg_rtt for each hop. The last hop represents the target RTT if reached.
            if hops:
                last_hop = hops[-1]
                if last_hop.address == target_ip:
                     self.db.add_latency_sample(target_ip, last_hop.avg_rtt)

            # Update DB with Geo Data
            if final_geo and 'error' not in final_geo:
                self.db.update_connection(target_ip, geo_data=final_geo)

            self.socketio.emit('traceroute_result', {
                'target': target_ip,
                'path': path_data,
                'target_geo': final_geo,
                'latest_rtt': hops[-1].avg_rtt if hops and hops[-1].address == target_ip else None,
                # Send history for sparkline
                'latency_history': self.db.get_latency_history(target_ip)
            })
            
        except Exception as e:
            print(f"Traceroute failed for {target_ip}: {e}")

class ConnectionMonitor:
    """Monitors active system connections and triggers updates."""
    
    def __init__(self, socketio, traceroute_engine, app, db):
        self.socketio = socketio
        self.traceroute_engine = traceroute_engine
        self.app = app
        self.db = db
        self.running = False
        self.seen_connections = set()

    def start(self):
        """Starts the monitoring background tasks."""
        self.running = True
        self.socketio.start_background_task(self._monitor_loop)
        self.socketio.start_background_task(self._rate_limit_emitter)

    def _monitor_loop(self):
        """
        Background loop that:
        1. Loads history from DB.
        2. Periodically scans for new connections.
        3. Measures latency for active connections.
        """
        with self.app.app_context():
            # Load existing connections from DB on start
            history = self.db.get_all_connections()
            for row in history:
                ip = row['ip']
                self.seen_connections.add(ip)
                # Emit to frontend so it populates sidebar immediately
                self.socketio.emit('new_connection', {
                    'ip': ip, 
                    'history': True,
                    'first_seen': row['first_seen'],
                    'geo': {
                        'city': row['city'], 'isp': row['isp'], 'org': row['org'], 
                        'lat': row['lat'], 'lon': row['lon'], 'country': row.get('country') # Assuming we might add country later or get it now
                    } if row['lat'] else None
                })
                # Re-queue for traceroute to get fresh RTT? Maybe optional.
                # For now, let's just show history.

            while self.running:
                self.scan()
                self.measure_latencies()
                self.socketio.sleep(2) # Faster updates

    def measure_latencies(self):
        """Sends ICMP pings to all active targets to get live RTT."""
        if not self.seen_connections:
            return

        targets = list(self.seen_connections)
        # Use multiping for efficiency
        try:
            results = multiping(targets, count=1, interval=0.1, timeout=1)
            for res in results:
                if res.is_alive:
                    self.db.add_latency_sample(res.address, res.avg_rtt)
                    self.socketio.emit('latency_update', {
                        'ip': res.address,
                        'rtt': res.avg_rtt
                    })
        except Exception as e:
            print(f"Latency measure error: {e}")

    def _rate_limit_emitter(self):
        """Emits GeoIP rate limit status to frontend periodically."""
        with self.app.app_context():
            while self.running:
                status = self.traceroute_engine.geo_service.get_rate_limit_status()
                self.socketio.emit('rate_limit_status', status)
                self.socketio.sleep(1)

    def trigger_scan(self):
        """Manually triggers a connection scan."""
        self.socketio.start_background_task(self.scan)

    def _identify_protocol(self, port):
        """Maps port numbers to common protocol names."""
        common_ports = {
            80: 'HTTP', 443: 'HTTPS', 22: 'SSH', 53: 'DNS', 
            21: 'FTP', 25: 'SMTP', 3306: 'MySQL', 5432: 'PostgreSQL',
            8080: 'HTTP-Alt', 8443: 'HTTPS-Alt'
        }
        return common_ports.get(port, 'TCP')

    def scan(self):
        """Scans system connections using psutil."""
        try:
            connections = psutil.net_connections(kind='inet')
            active_remote_ips = set()
            
            # Map IP to details for this scan
            current_scan_details = {} 

            for conn in connections:
                if conn.status == 'ESTABLISHED' and conn.raddr:
                    ip = conn.raddr.ip
                    port = conn.raddr.port
                    if ip != '127.0.0.1' and ip != '::1': 
                        active_remote_ips.add(ip)
                        current_scan_details[ip] = {
                            'port': port,
                            'protocol': self._identify_protocol(port)
                        }
            
            if len(active_remote_ips) == 0 and '8.8.8.8' not in self.seen_connections:
                 active_remote_ips.add('8.8.8.8')
                 current_scan_details['8.8.8.8'] = {'port': 53, 'protocol': 'DNS'}

            for ip in active_remote_ips:
                # Update DB every time seen (updates last_seen timestamp)
                details = current_scan_details.get(ip, {})
                self.db.update_connection(ip, port=details.get('port'), protocol=details.get('protocol'))

                if ip not in self.seen_connections:
                    self.seen_connections.add(ip)
                    self.socketio.emit('new_connection', {
                        'ip': ip, 
                        'protocol': details.get('protocol'),
                        'first_seen': time.time()
                    })
                    self.traceroute_engine.add_target(ip)

        except Exception as e:
            print(f"Scan error: {e}")
