import sqlite3
import time
import threading

class DatabaseService:
    """
    Handles SQLite database interactions for connections and latency history.
    """
    
    def __init__(self, db_path="nettrace.db", app=None):
        self.db_path = db_path
        self.app = app
        self._lock = threading.Lock()
        self.init_db()

    def get_connection(self):
        """Creates a new database connection."""
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """Initializes the database schema if tables do not exist."""
        with self._lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Connections table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS connections (
                    ip TEXT PRIMARY KEY,
                    first_seen REAL,
                    last_seen REAL,
                    protocol TEXT,
                    city TEXT,
                    isp TEXT,
                    org TEXT,
                    country TEXT,
                    lat REAL,
                    lon REAL,
                    port INTEGER
                )
            ''')
            
            # Check if 'country' column exists (migration)
            cursor.execute("PRAGMA table_info(connections)")
            columns = [info[1] for info in cursor.fetchall()]
            if 'country' not in columns:
                cursor.execute("ALTER TABLE connections ADD COLUMN country TEXT")

            # Latency history table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS latency_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT,
                    timestamp REAL,
                    rtt REAL,
                    FOREIGN KEY(ip) REFERENCES connections(ip)
                )
            ''')
            
            conn.commit()
            conn.close()

    def update_connection(self, ip, port=None, protocol=None, geo_data=None):
        """
        Updates or inserts connection details.
        
        Args:
            ip (str): IP address.
            port (int): Port number.
            protocol (str): Protocol name.
            geo_data (dict): Geolocation information.
        """
        with self._lock, self.app.app_context():
            conn = self.get_connection()
            cursor = conn.cursor()
            
            now = time.time()
            
            # Check if exists
            cursor.execute("SELECT first_seen FROM connections WHERE ip = ?", (ip,))
            row = cursor.fetchone()
            
            if row:
                # Update last_seen and other fields if provided
                update_fields = ["last_seen = ?"]
                params = [now]
                
                if protocol:
                    update_fields.append("protocol = ?")
                    params.append(protocol)
                if port:
                    update_fields.append("port = ?")
                    params.append(port)
                if geo_data:
                    update_fields.append("city = ?")
                    params.append(geo_data.get('city'))
                    update_fields.append("isp = ?")
                    params.append(geo_data.get('isp'))
                    update_fields.append("org = ?")
                    params.append(geo_data.get('org'))
                    update_fields.append("country = ?")
                    params.append(geo_data.get('country'))
                    update_fields.append("lat = ?")
                    params.append(geo_data.get('lat'))
                    update_fields.append("lon = ?")
                    params.append(geo_data.get('lon'))
                
                params.append(ip)
                cursor.execute(f"UPDATE connections SET {', '.join(update_fields)} WHERE ip = ?", params)
            else:
                # Insert new
                city = geo_data.get('city') if geo_data else None
                isp = geo_data.get('isp') if geo_data else None
                org = geo_data.get('org') if geo_data else None
                country = geo_data.get('country') if geo_data else None
                lat = geo_data.get('lat') if geo_data else None
                lon = geo_data.get('lon') if geo_data else None
                
                cursor.execute('''
                    INSERT INTO connections (ip, first_seen, last_seen, protocol, city, isp, org, country, lat, lon, port)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (ip, now, now, protocol, city, isp, org, country, lat, lon, port))
            
            conn.commit()
            conn.close()

    def add_latency_sample(self, ip, rtt):
        """Adds a new latency (RTT) sample for a connection."""
        with self._lock, self.app.app_context():
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO latency_history (ip, timestamp, rtt) VALUES (?, ?, ?)", (ip, time.time(), rtt))
            conn.commit()
            conn.close()

    def get_all_connections(self):
        """Returns all connections from the database."""
        # Read-only, no lock strictly needed but good practice if mixed with writes
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connections ORDER BY last_seen DESC")
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            results.append(dict(row))
        return results

    def get_latency_history(self, ip, limit=20):
        """
        Retrieves recent latency history for an IP.
        
        Returns:
            list: List of dicts [{'rtt': float, 'timestamp': float}]
        """
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT rtt, timestamp FROM latency_history WHERE ip = ? ORDER BY timestamp DESC LIMIT ?", (ip, limit))
        rows = cursor.fetchall()
        conn.close()
        
        # Return reversed (oldest to newest) for graphing
        return [{"rtt": row['rtt'], "timestamp": row['timestamp']} for row in rows][::-1]

    def clear_history(self, older_than_seconds=None):
        """
        Clears history data.
        
        Args:
            older_than_seconds (int, optional): If set, only clears data older than this age.
        """
        with self._lock, self.app.app_context():
            conn = self.get_connection()
            cursor = conn.cursor()
            
            if older_than_seconds:
                cutoff = time.time() - older_than_seconds
                # Delete latency history for old connections
                cursor.execute("DELETE FROM latency_history WHERE ip IN (SELECT ip FROM connections WHERE last_seen < ?)", (cutoff,))
                # Delete connections
                cursor.execute("DELETE FROM connections WHERE last_seen < ?", (cutoff,))
            else:
                # Clear all
                cursor.execute("DELETE FROM latency_history")
                cursor.execute("DELETE FROM connections")
            
            conn.commit()
            conn.close()
