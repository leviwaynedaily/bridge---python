#!/usr/bin/env python3
import threading
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for
import argparse
from collections import deque
import os
import sys
import sqlite3

# --- Auto-install required packages if missing ---
REQUIRED = ['flask', 'requests', 'jinja2']
try:
    import flask, requests, jinja2
except ImportError:
    import subprocess
    print('Missing dependencies. Installing...')
    subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + REQUIRED)
    print('Dependencies installed. Restarting...')
    os.execv(sys.executable, [sys.executable] + sys.argv)
# --- End auto-installer ---

app = Flask(__name__)
UNLOCK_EVENTS = []
WINDOW = 10  # seconds

# Store recent events for dashboard (thread-safe)
EVENT_LOG = deque(maxlen=100)
EVENT_LOCK = threading.Lock()

# SQLite setup
DB_PATH = 'events.db'
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        type TEXT,
        portal TEXT,
        desc TEXT,
        camera TEXT,
        event TEXT,
        count TEXT,
        verdict TEXT
    )''')
    conn.commit()
    conn.close()
init_db()

# Add mode selection and people counter for line crossing
PEOPLE_COUNT = 0
PEOPLE_LOCK = threading.Lock()

# NetBox config management
default_url = 'http://10.13.1.180/nbws/goforms/nbapi'
default_user = 'admin'
default_pass = 'Csg5841!#'
app.config['NETBOX_CONFIG'] = {
    'url': default_url,
    'username': default_user,
    'password': default_pass
}

def prune_unlocks():
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WINDOW)
    global UNLOCK_EVENTS
    UNLOCK_EVENTS = [ts for ts in UNLOCK_EVENTS if ts >= cutoff]

def log_event(event):
    with EVENT_LOCK:
        EVENT_LOG.appendleft(event)
    # Insert into SQLite
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO events (time, type, portal, desc, camera, event, count, verdict)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', (
        event.get('time'),
        event.get('type'),
        event.get('portal', ''),
        event.get('desc', ''),
        event.get('camera', ''),
        event.get('event', ''),
        event.get('count', ''),
        event.get('verdict', '')
    ))
    # Prune events older than 7 days
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute('DELETE FROM events WHERE time < ?', (seven_days_ago,))
    conn.commit()
    conn.close()

def get_events():
    # Fetch only events from the last 7 days, newest first
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''SELECT time, type, portal, desc, camera, event, count, verdict FROM events
                 WHERE time >= ? ORDER BY time DESC LIMIT 100''', (seven_days_ago,))
    rows = c.fetchall()
    conn.close()
    events = []
    for row in rows:
        events.append({
            'time': row[0],
            'type': row[1],
            'portal': row[2],
            'desc': row[3],
            'camera': row[4],
            'event': row[5],
            'count': row[6],
            'verdict': row[7]
        })
    return events

@app.route('/camera', methods=['POST'])
def camera():
    global PEOPLE_COUNT
    data = request.json or {}
    now_local = datetime.now().astimezone()
    now_utc   = now_local.astimezone(timezone.utc)

    event_type = data.get('EventType', '').lower()
    response = {'status': 'ok'}

    # Always log the event for dashboard visibility
    log_event({
        'type': 'camera',
        'time': now_local.isoformat(),
        'camera': data.get('CameraName', '<unknown>'),
        'event': data.get('EventName', '<unnamed>'),
        'count': data.get('EventCaption', ''),
        'verdict': event_type.upper() if event_type else 'UNKNOWN',
        'raw': data
    })

    # Always process line crossing events
    if event_type == 'linecrossing':
        with PEOPLE_LOCK:
            PEOPLE_COUNT += 1
            response['people_count'] = PEOPLE_COUNT
    # Always process tailgating events
    if event_type == 'tailgating':
        count = int(data.get('EventCaption', '2').split()[0] or 2)
        prune_unlocks()
        recent_unlocks = [
            t for t in UNLOCK_EVENTS
            if 0 <= (now_utc - t).total_seconds() <= WINDOW
        ]
        if len(recent_unlocks) >= count:
            verdict = 'NO TAILGATE'
        else:
            verdict = 'TAILGATE'
        response['classification'] = verdict
        # Update the most recent camera event in the log with the correct verdict
        with EVENT_LOCK:
            for event in EVENT_LOG:
                if event.get('type') == 'camera' and event.get('time') == now_local.isoformat() and event.get('event') == data.get('EventName', '<unnamed>'):
                    event['verdict'] = verdict
                    break
    return jsonify(response)

# Optional dashboard UI
@app.route('/')
def root():
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html', events=get_events())

@app.route('/events')
def events():
    events = get_events()
    people_count = 0
    with PEOPLE_LOCK:
        people_count = PEOPLE_COUNT
    return jsonify({'events': events, 'people_count': people_count, 'window': WINDOW})

# Test access event
@app.route('/test_access', methods=['POST'])
def test_access():
    global PEOPLE_COUNT
    data = request.json or {}
    desc = data.get('desc', 'Door Unlock')
    now_local = datetime.now().astimezone()
    log_event({
        'type': 'access',
        'time': now_local.isoformat(),
        'portal': 'Test Portal',
        'desc': desc
    })
    # Only append to UNLOCK_EVENTS for tailgate logic if this is an unlock event
    if 'unlock' in desc.lower():
        now_utc = now_local.astimezone(timezone.utc)
        UNLOCK_EVENTS.append(now_utc)
        prune_unlocks()
        # Decrement people count for unlock (allow negative)
        with PEOPLE_LOCK:
            PEOPLE_COUNT -= 1
    # Reset people count on lock
    if 'lock' in desc.lower():
        with PEOPLE_LOCK:
            PEOPLE_COUNT = 0
    return jsonify(status="ok", message=f"Test access event logged: {desc}")

@app.route('/set_mode', methods=['POST'])
def set_mode():
    data = request.json or {}
    mode = data.get('mode', '').lower()
    if mode in ['tailgating', 'linecrossing']:
        app.config['MODE'] = mode
        return jsonify(status='ok', mode=mode)
    else:
        return jsonify(status='error', message='Invalid mode'), 400

@app.route('/set_window', methods=['POST'])
def set_window():
    global WINDOW
    data = request.json or {}
    try:
        new_window = int(data.get('window', WINDOW))
        if new_window < 1 or new_window > 60:
            return jsonify(status='error', message='Window must be between 1 and 60 seconds'), 400
        WINDOW = new_window
        return jsonify(status='ok', window=WINDOW)
    except Exception as e:
        return jsonify(status='error', message=str(e)), 400

# NetBox config management
def get_netbox_config(mask=True):
    cfg = app.config.get('NETBOX_CONFIG', {})
    if mask:
        return {
            'url': cfg.get('url', ''),
            'username': cfg.get('username', ''),
            'password': '*' * len(cfg.get('password', '')) if cfg.get('password') else ''
        }
    return cfg

@app.route('/get_netbox_config')
def get_netbox_config_route():
    return jsonify(get_netbox_config(mask=True))

@app.route('/set_netbox_config', methods=['POST'])
def set_netbox_config():
    data = request.json or {}
    url = data.get('url', default_url)
    username = data.get('username', default_user)
    password = data.get('password', default_pass)
    app.config['NETBOX_CONFIG'] = {'url': url, 'username': username, 'password': password}
    # Optionally restart NetBox thread here if needed
    return jsonify(status='ok', config=get_netbox_config(mask=True))

@app.route('/test_netbox', methods=['POST'])
def test_netbox():
    data = request.json or {}
    url = data.get('url', default_url)
    username = data.get('username', default_user)
    password = data.get('password', default_pass)
    login_payload = f"""
<NETBOX-API>
  <COMMAND name="Login">
    <PARAMS>
      <USERNAME>{username}</USERNAME>
      <PASSWORD>{password}</PASSWORD>
    </PARAMS>
  </COMMAND>
</NETBOX-API>
"""
    try:
        r = requests.post(url, data=login_payload, headers={'Content-Type': 'application/xml'}, timeout=5)
        if r.status_code != 200:
            return jsonify(status='error', message=f'HTTP {r.status_code}'), 400
        if 'sessionid' in r.text:
            return jsonify(status='ok', message='Connection successful!')
        else:
            return jsonify(status='error', message='Login failed or sessionid not found.'), 400
    except Exception as e:
        return jsonify(status='error', message=str(e)), 400

# NetBox config and thread management
def start_netbox_thread():
    if hasattr(app, 'netbox_thread') and app.netbox_thread and app.netbox_thread.is_alive():
        return  # Already running
    def netbox_wrapper():
        config = app.config.get('NETBOX_CONFIG', {})
        url = config.get('url', default_url)
        username = config.get('username', default_user)
        password = config.get('password', default_pass)
        print("Logging in to NetBox...")
        login_payload = f"""
<NETBOX-API>
  <COMMAND name="Login">
    <PARAMS>
      <USERNAME>{username}</USERNAME>
      <PASSWORD>{password}</PASSWORD>
    </PARAMS>
  </COMMAND>
</NETBOX-API>
"""
        r = requests.post(url, data=login_payload, headers={'Content-Type': 'application/xml'})
        print("Login response:", r.text)
        root = ET.fromstring(r.text)
        session = root.attrib.get('sessionid', None)
        if not session:
            print("No sessionid found in NetBox login response.")
            return
        print("Subscribing to event stream...")
        stream_payload = f"""
<NETBOX-API sessionid=\"{session}\">
  <COMMAND name="StreamEvents">
    <PARAMS>
      <TAGNAMES>
        <DESCNAME/><PORTALNAME/>
      </TAGNAMES>
    </PARAMS>
  </COMMAND>
</NETBOX-API>
"""
        with requests.post(url, data=stream_payload, headers={'Content-Type': 'application/xml'}, stream=True) as resp:
            print("Event stream started, waiting for events...")
            for line in resp.iter_lines():
                if not line:
                    continue
                txt = line.decode(errors='ignore')
                print("Received line:", txt)
                if '<EVENT' not in txt:
                    continue
                # parse minimal XML
                xml = txt.split('--Boundary')[0]
                evt = ET.fromstring(xml).find('.//EVENT')
                desc = evt.find('DESCNAME').text or ''
                portal = evt.find('PORTALNAME').text or ''
                now_local = datetime.now().astimezone()
                now_utc   = now_local.astimezone(timezone.utc)
                print(f"[{now_local:%H:%M:%S %Z}] ACCESS: {portal} → {desc}")
                log_event({
                    'type': 'access',
                    'time': now_local.isoformat(),
                    'portal': portal,
                    'desc': desc
                })
                if 'unlock' in desc.lower():
                    UNLOCK_EVENTS.append(now_utc)
                    prune_unlocks()
    app.netbox_thread = threading.Thread(target=netbox_wrapper, daemon=True)
    app.netbox_thread.start()

def stop_netbox_thread():
    # Not trivial to kill a thread in Python, so just set a flag and let it die if possible (not implemented here)
    pass

# Restart endpoint
@app.route('/restart', methods=['POST'])
def restart():
    # For production, use a process manager (supervisor, systemd, etc.) to restart the process automatically.
    # This endpoint will just exit the process; the manager should restart it.
    import threading
    def do_exit():
        os._exit(0)
    threading.Timer(0.5, do_exit).start()
    return jsonify(status='ok', message='Server restarting...')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Tailgate Monitor')
    parser.add_argument('--dashboard', action='store_true', help='Enable dashboard UI')
    parser.add_argument('--host', default='0.0.0.0', help='Flask host')
    parser.add_argument('--port', type=int, default=8080, help='Flask port')
    parser.add_argument('--mode', choices=['tailgating', 'linecrossing'], default='tailgating', help='Operation mode: tailgating or linecrossing')
    parser.add_argument('--netbox_url', default='http://10.13.1.180/nbws/goforms/nbapi', help='NetBox API URL')
    parser.add_argument('--netbox_user', default='admin', help='NetBox username')
    parser.add_argument('--netbox_pass', default='Csg5841!#', help='NetBox password')
    args = parser.parse_args()

    app.config['MODE'] = args.mode
    app.config['NETBOX_CONFIG'] = {'url': args.netbox_url, 'username': args.netbox_user, 'password': args.netbox_pass}

    if args.mode == 'prod':
        start_netbox_thread()

    if args.dashboard:
        # Ensure templates folder exists
        if not os.path.exists('templates'):
            os.makedirs('templates')
        # Write a simple dashboard template if not present
        dashboard_path = os.path.join('templates', 'dashboard.html')
        if not os.path.exists(dashboard_path):
            with open(dashboard_path, 'w') as f:
                f.write('''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Tailgate Monitor Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 2em; }
        h1 { color: #333; }
        .tailgate { color: red; font-weight: bold; }
        .no-tailgate { color: green; font-weight: bold; }
        .event-log { margin-top: 2em; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
        th { background: #eee; }
    </style>
    <script>
        function fetchEvents() {
            fetch('/events').then(r => r.json()).then(events => {
                let tbody = document.getElementById('event-tbody');
                tbody.innerHTML = '';
                for (let e of events) {
                    let row = document.createElement('tr');
                    if (e.type === 'access') {
                        row.innerHTML = `<td>${e.time}</td><td>Access</td><td>${e.portal}</td><td>${e.desc}</td><td></td><td></td><td></td>`;
                    } else {
                        let verdictClass = e.verdict === 'TAILGATE' ? 'tailgate' : (e.verdict === 'NO TAILGATE' ? 'no-tailgate' : '');
                        row.innerHTML = `<td>${e.time}</td><td>Camera</td><td>${e.camera}</td><td>${e.event}</td><td>${e.count}</td><td class='${verdictClass}'>${e.verdict}</td><td></td>`;
                    }
                    tbody.appendChild(row);
                }
            });
        }
        setInterval(fetchEvents, 2000);
        window.onload = fetchEvents;
    </script>
</head>
<body>
    <h1>Tailgate Monitor Dashboard</h1>
    <div class="event-log">
        <table>
            <thead>
                <tr>
                    <th>Time</th><th>Type</th><th>Portal/Camera</th><th>Event</th><th>Count</th><th>Verdict</th><th></th>
                </tr>
            </thead>
            <tbody id="event-tbody">
            </tbody>
        </table>
    </div>
</body>
</html>''')
        app.run(host=args.host, port=args.port)
    else:
        app.run(host=args.host, port=args.port) 