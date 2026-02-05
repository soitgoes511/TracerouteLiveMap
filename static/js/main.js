var map, globe;
var currentView = '2d'; // '2d' or '3d'
var socket = io();
var connections = {}; // key: ip, value: { layer: LayerGroup, info: object, chart: ChartInstance }
var activeIp = null;

// --- Initialization ---

function initMap() {
    map = L.map('map', { zoomControl: false }).setView([20, 0], 2);
    L.control.zoom({ position: 'topright' }).addTo(map);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; OpenStreetMap &copy; CARTO', subdomains: 'abcd', maxZoom: 19
    }).addTo(map);
}

function initGlobe() {
    const elem = document.getElementById('globe-container');
    globe = Globe()
        (elem)
        .globeImageUrl('https://unpkg.com/three-globe/example/img/earth-dark.jpg')
        .bumpImageUrl('https://unpkg.com/three-globe/example/img/earth-topology.png')
        .backgroundColor('#000000')
        .pointColor(() => '#58a6ff')
        .pointAltitude(0.01)
        .pointRadius(0.5)
        .arcColor(() => '#58a6ff')
        .arcDashLength(0.4)
        .arcDashGap(0.2)
        .arcDashAnimateTime(1500)
        .onPointHover(point => {
            elem.style.cursor = point ? 'pointer' : null;
        });

    // Auto-rotate
    globe.controls().autoRotate = true;
    globe.controls().autoRotateSpeed = 0.5;
}

initMap();
// Delay globe init slightly to ensure container is ready or wait for toggle
// But better to init it so data can be populated
setTimeout(initGlobe, 100);

// --- View Toggle ---
document.getElementById('btn-2d').addEventListener('click', () => switchView('2d'));
document.getElementById('btn-3d').addEventListener('click', () => switchView('3d'));

// --- Settings / Clear History ---
document.getElementById('btn-settings').addEventListener('click', () => {
    if (confirm("Are you sure you want to clear ALL connection history?")) {
        socket.emit('clear_history', {}); // Empty object = clear all
    }
});

function switchView(mode) {
    currentView = mode;
    document.getElementById('btn-2d').classList.toggle('active', mode === '2d');
    document.getElementById('btn-3d').classList.toggle('active', mode === '3d');

    const mapDiv = document.getElementById('map');
    const globeDiv = document.getElementById('globe-container');

    if (mode === '2d') {
        mapDiv.style.display = 'block';
        mapDiv.style.zIndex = 1;
        globeDiv.style.display = 'none';
    } else {
        mapDiv.style.display = 'none';
        globeDiv.style.display = 'block';
        // Resize globe to fit
        if (globe) {
            globe.width(globeDiv.clientWidth).height(globeDiv.clientHeight);
        }
    }
}

// --- Socket Handlers ---

socket.on('connect', () => console.log('Connected to server'));

socket.on('rate_limit_status', (data) => {
    var el = document.getElementById('rate-limit-text');
    if (el) {
        el.innerText = `GeoIP Requests: ${data.remaining} left (Resets in ${data.reset_in}s)`;
        el.style.color = data.remaining < 5 ? '#f85149' : '#8b949e';
    }
});

socket.on('new_connection', (data) => {
    var ip = data.ip;
    if (!connections[ip]) {
        connections[ip] = {
            layer: null,
            info: {
                ip: ip,
                status: data.history ? 'traced (history)' : 'pending',
                protocol: data.protocol,
                geo: data.geo,
                first_seen: data.first_seen || (Date.now() / 1000),
                latest_rtt: null
            }
        };
        addSidebarItem(ip, data.protocol);

        // If history data came with it
        if (data.geo) {
            updateViz(ip);
        }
        updateStats();
    }
});

socket.on('traceroute_result', (data) => {
    var target = data.target;
    var targetGeo = data.target_geo;

    if (!connections[target]) {
        connections[target] = {
            layer: null,
            info: {
                ip: target,
                first_seen: Date.now() / 1000
            }
        };
        addSidebarItem(target);
    }

    connections[target].info.geo = targetGeo;
    connections[target].info.status = 'traced';
    connections[target].info.path = data.path;

    connections[target].info.latest_rtt = data.latest_rtt;

    updateSidebarItem(target, data.latency_history);
    updateViz(target);
    updateStats();
});

// Timer for Duration
setInterval(() => {
    const now = Date.now() / 1000;
    Object.keys(connections).forEach(ip => {
        const info = connections[ip].info;
        if (info.first_seen) {
            const duration = now - info.first_seen;
            const el = document.getElementById(`duration-${ip.replace(/:/g, '_')}`);
            if (el) el.innerText = formatDuration(duration);
        }
    });
}, 1000);

function formatDuration(seconds) {
    if (seconds < 60) return Math.floor(seconds) + "s";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    const m_rem = m % 60;
    return `${h}h ${m_rem}m`;
}

socket.on('history_cleared', () => {
    // Clear local state
    Object.values(connections).forEach(c => {
        if (c.layer) map.removeLayer(c.layer);
        if (c.chart) c.chart.destroy();
    });
    connections = {};
    activeIp = null;

    // Clear UI
    document.getElementById('connection-list').innerHTML = '';
    updateStats();

    // Globe reset
    if (globe) {
        globe.pointsData([]).arcsData([]);
    }
});

socket.on('latency_update', (data) => {
    var ip = data.ip;
    if (!connections[ip]) return;

    // Update Latest RTT text
    var rttEl = document.getElementById(`rtt-${ip.replace(/:/g, '_')}`);
    if (rttEl) {
        rttEl.innerText = Math.round(data.rtt) + " ms";
        if (data.rtt > 150) rttEl.className = 'latency-badge list-slow';
        else if (data.rtt > 50) rttEl.className = 'latency-badge list-med';
        else rttEl.className = 'latency-badge list-fast';
    }

    // Update Sparkline
    if (connections[ip].chart) {
        var chart = connections[ip].chart;
        var rttData = chart.data.datasets[0].data;

        rttData.push(data.rtt);
        if (rttData.length > 20) { // Keep last 20
            rttData.shift();
        }
        // Labels also need update
        chart.data.labels = rttData.map(() => '');
        chart.update('none'); // Efficient update
    } else {
        // Create if missing
        renderSparkline(ip, [{ rtt: data.rtt }]);
    }
});

// --- Visualization Updates ---

function updateViz(ip) {
    var info = connections[ip].info;
    if (!info.geo || info.geo.error) return;

    // 2D Map Update
    if (connections[ip].layer) map.removeLayer(connections[ip].layer);

    var layerGroup = L.layerGroup();
    var latlngs = [];

    // Path
    if (info.path) {
        info.path.forEach(hop => {
            if (hop.lat && hop.lon) {
                latlngs.push([hop.lat, hop.lon]);
                L.circleMarker([hop.lat, hop.lon], {
                    color: '#30363d', fillColor: '#8b949e', fillOpacity: 0.5, radius: 3
                }).bindPopup(`Hop: ${hop.address}`).addTo(layerGroup);
            }
        });
    }

    // Target
    latlngs.push([info.geo.lat, info.geo.lon]);
    L.circleMarker([info.geo.lat, info.geo.lon], {
        color: '#58a6ff', fillColor: '#58a6ff', fillOpacity: 0.9, radius: 6
    }).bindPopup(`<b>${ip}</b><br>${info.geo.org || info.geo.isp || 'Unknown'}`).addTo(layerGroup);

    if (latlngs.length > 1) {
        L.polyline(latlngs, {
            color: '#58a6ff', weight: 2, opacity: 0.5, dashArray: '5, 10'
        }).addTo(layerGroup);
    }

    connections[ip].layer = layerGroup;
    layerGroup.addTo(map);

    // 3D Globe Update
    updateGlobeData();
}

function updateGlobeData() {
    if (!globe) return;

    const arcs = [];
    const points = [];

    // Simple logic: Draw arc from User (approx) to Target.
    // Ideally we get user lat/lon, but for now let's assume a default or use first hop?
    // Let's just draw arcs for all known targets from... where?
    // If we don't know "my" location, we can't draw the arc start easily unless we fake it.
    // Let's assume user is around [20, 0] view or better, use the first hop if local?
    // Doing "star" topology from a center point looks cool.
    // Let's use a fixed "Home" point for visual stability if unknown.
    // Or better, if we have path data, draw hops!

    const homeLat = 0; // Equator/Greenwich as abstract home
    const homeLon = 0;

    Object.values(connections).forEach(c => {
        if (c.info.geo && !c.info.geo.error) {
            points.push({
                lat: c.info.geo.lat,
                lng: c.info.geo.lon,
                size: 0.5,
                color: '#58a6ff'
            });

            // Draw full path if available
            if (c.info.path && c.info.path.length > 0) {
                let prevLat = null, prevLon = null;
                // Try to find first valid hop
                // If path[0] has lat/lon

                c.info.path.forEach(hop => {
                    if (hop.lat && hop.lon) {
                        if (prevLat !== null) {
                            arcs.push({
                                startLat: prevLat, startLng: prevLon,
                                endLat: hop.lat, endLng: hop.lon,
                                color: '#58a6ff'
                            });
                        }
                        prevLat = hop.lat;
                        prevLon = hop.lon;
                        points.push({ lat: hop.lat, lng: hop.lon, size: 0.2, color: '#8b949e' });
                    }
                });

                // Last hop to target
                if (prevLat !== null) {
                    arcs.push({
                        startLat: prevLat, startLng: prevLon,
                        endLat: c.info.geo.lat, endLng: c.info.geo.lon,
                        color: '#58a6ff'
                    });
                }
            } else {
                // Direct arc if no path (e.g. from history)
                // Just draw point, maybe no arc to avoid clutter
            }
        }
    });

    globe.pointsData(points)
        .arcsData(arcs);
}


// --- Sidebar & UI ---

function addSidebarItem(ip, protocol) {
    var list = document.getElementById('connection-list');
    var div = document.createElement('div');
    div.id = `item-${ip.replace(/:/g, '_')}`;
    div.className = 'connection-item';

    var proto = protocol || 'TCP';
    var protoClass = `protocol-${proto}`;

    div.innerHTML = `
        <div class="conn-row-top">
            <div class="conn-info">
                <span class="conn-ip">${ip}</span>
                <span class="conn-detail">Initializing...</span>
            </div>
            <div class="conn-meta">
                <span class="protocol-tag ${protoClass}">${proto}</span>
                <div class="conn-status"></div>
            </div>
        </div>
        <div class="latency-chart-container">
            <canvas id="chart-${ip.replace(/:/g, '_')}"></canvas>
        </div>
    `;
    div.onclick = function () { selectConnection(ip); };
    list.appendChild(div);
}

function updateSidebarItem(ip, latencyHistory) {
    var item = document.getElementById(`item-${ip.replace(/:/g, '_')}`);
    if (item && connections[ip]) {
        var info = connections[ip].info;
        var geo = info.geo || {};

        // Prefer Org > City > Country
        var details = [];
        if (geo.org) details.push(geo.org);
        if (geo.city) details.push(geo.city);

        var text = details.join(', ') || 'Unknown Location';
        if (geo.error) text = "Locating Failed";

        item.querySelector('.conn-detail').innerText = text;

        var statusDot = item.querySelector('.conn-status');
        if (info.status === 'traced') {
            statusDot.classList.add('traced');
        }

        // Render Sparkline
        if (latencyHistory && latencyHistory.length > 0) {
            renderSparkline(ip, latencyHistory);
        }
    }
}

function renderSparkline(ip, data) {
    var container = document.getElementById(`item-${ip.replace(/:/g, '_')}`).querySelector('.latency-chart-container');
    container.style.display = 'block';

    var canvasId = `chart-${ip.replace(/:/g, '_')}`;
    var ctx = document.getElementById(canvasId).getContext('2d');

    // If chart already exists, update? Or destroy?
    // Chart.js requires destroy for re-use of canvas.
    if (connections[ip].chart) {
        connections[ip].chart.destroy();
    }

    connections[ip].chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: data.map(d => ''), // No labels
            datasets: [{
                data: data.map(d => d.rtt),
                borderColor: '#58a6ff',
                borderWidth: 1,
                pointRadius: 1, // Make small points visible so single-point data shows up
                fill: false,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: {
                x: { display: false },
                y: { display: false, min: 0 }
            },
            animation: false
        }
    });
}

function selectConnection(ip) {
    document.querySelectorAll('.connection-item').forEach(el => el.classList.remove('active'));
    var item = document.getElementById(`item-${ip.replace(/:/g, '_')}`);
    if (item) item.classList.add('active');

    activeIp = ip;

    var conn = connections[ip];
    if (conn && conn.info.geo && !conn.info.geo.error) {
        var lat = conn.info.geo.lat;
        var lon = conn.info.geo.lon;

        if (currentView === '2d') {
            map.flyTo([lat, lon], 8, { animate: true, duration: 1.5 });
        } else if (globe) {
            globe.pointOfView({ lat: lat, lng: lon, altitude: 1.5 }, 1500);
        }
    }
}

function updateStats() {
    var total = Object.keys(connections).length;
    var traced = Object.values(connections).filter(c => c.info.status.includes('traced')).length;

    document.getElementById('stat-total-connections').innerText = total;
    document.getElementById('stat-proxies').innerText = traced;
}
