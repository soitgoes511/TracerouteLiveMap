from flask import Flask, render_template
from flask_socketio import SocketIO
import threading
import time
from services import ConnectionMonitor, TracerouteEngine, GeoIPService
from database import DatabaseService

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global services
db_service = DatabaseService(app=app)
geo_service = GeoIPService()
traceroute_engine = TracerouteEngine(socketio, geo_service, app, db_service)
monitor = ConnectionMonitor(socketio, traceroute_engine, app, db_service)

@app.route('/')
def index():
    """Renders the main dashboard page."""
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    """Handles new client connections via SocketIO."""
    print('Client connected')
    # Send initial data if available or trigger a scan
    monitor.trigger_scan()

@socketio.on('clear_history')
def handle_clear_history(data):
    """
    Handles request to clear connection history from the database.
    
    Args:
        data (dict): May contain 'older_than' (int) in seconds.
    """
    # data can be {'older_than': 86400} or empty for all
    older_than = data.get('older_than')
    db_service.clear_history(older_than)
    # Notify all clients to clear their UI
    socketio.emit('history_cleared')
    # Trigger a rescan to populate currently active connections
    monitor.trigger_scan()

if __name__ == '__main__':
    # Start background threads
    monitor.start()
    traceroute_engine.start()
    
    socketio.run(app, host='0.0.0.0', port=5000)
