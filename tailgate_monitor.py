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
import json
import webbrowser
import pystray
from PIL import Image, ImageDraw

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
NETBOX_CONFIG_PATH = 'netbox_config.json'

def save_netbox_config(cfg):
    with open(NETBOX_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f)

def load_netbox_config():
    try:
        with open(NETBOX_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return None

# Load Netbox config from file if it exists
loaded_cfg = load_netbox_config()
if loaded_cfg:
    app.config['NETBOX_CONFIG'] = loaded_cfg
else:
    app.config['NETBOX_CONFIG'] = {
        'url': default_url,
        'username': default_user,
        'password': default_pass,
        'enabled': False
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
        # Find recent unlocks within the window
        recent_unlocks = [t for t in UNLOCK_EVENTS if 0 <= (now_utc - t).total_seconds() <= WINDOW]
        print(f"[DEBUG] Processing tailgating event at {now_utc}. Needed count: {count}")
        print(f"[DEBUG] UNLOCK_EVENTS: {[t.isoformat() for t in UNLOCK_EVENTS]}")
        print(f"[DEBUG] recent_unlocks: {[t.isoformat() for t in recent_unlocks]}")
        if len(recent_unlocks) >= count:
            verdict = 'NO TAILGATE'
            print(f"[DEBUG] Verdict: NO TAILGATE. Consuming {count} unlocks.")
            # Consume (remove) the matched unlocks so they can't be reused
            for _ in range(count):
                # Remove the oldest unlock in the window
                oldest = min(recent_unlocks)
                UNLOCK_EVENTS.remove(oldest)
                recent_unlocks.remove(oldest)
        else:
            verdict = 'TAILGATING'
            print(f"[DEBUG] Verdict: TAILGATING. Not enough unlocks.")
        response['classification'] = verdict
        # Update the most recent camera event in the log with the correct verdict
        with EVENT_LOCK:
            for event in EVENT_LOG:
                if event.get('type') == 'camera' and event.get('time') == now_local.isoformat() and event.get('event') == data.get('EventName', '<unnamed>'):
                    event['verdict'] = verdict
                    # Also update the verdict in the database
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute('''UPDATE events SET verdict = ? WHERE time = ? AND event = ? AND type = 'camera' ''', (verdict, now_local.isoformat(), data.get('EventName', '<unnamed>')))
                    conn.commit()
                    conn.close()
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
        print(f"[DEBUG] Added unlock at {now_utc}. UNLOCK_EVENTS now: {[t.isoformat() for t in UNLOCK_EVENTS]}")
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
            'password': '*' * len(cfg.get('password', '')) if cfg.get('password') else '',
            'enabled': cfg.get('enabled', False)
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
    enabled = data.get('enabled', False)
    cfg = {'url': url, 'username': username, 'password': password, 'enabled': enabled}
    app.config['NETBOX_CONFIG'] = cfg
    save_netbox_config(cfg)
    print(f"[DEBUG] Netbox config updated. Enabled: {enabled}")
    if enabled:
        print("[DEBUG] Netbox integration enabled. Starting Netbox thread...")
        start_netbox_thread()
    else:
        print("[DEBUG] Netbox integration disabled. (Stopping not implemented)")
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
    cfg = app.config.get('NETBOX_CONFIG', {})
    if not cfg.get('enabled', False):
        print("[DEBUG] Netbox thread not started because integration is disabled.")
        return
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
        try:
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
        except Exception as ex:
            print(f"[DEBUG] Netbox thread error: {ex}")
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

@app.route('/clear_db', methods=['POST'])
def clear_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM events')
    conn.commit()
    conn.close()
    with EVENT_LOCK:
        EVENT_LOG.clear()
    return jsonify(status='ok', message='Database cleared.')

if __name__ == '__main__':
    import threading
    import webbrowser
    import pystray
    from PIL import Image, ImageDraw
    
    def run_flask():
        # Use 127.0.0.1 to avoid firewall popups on Windows
        app.run(host='127.0.0.1', port=5000, debug=False)

    def create_image():
        # Simple blue circle icon
        image = Image.new('RGB', (64, 64), color=(92, 225, 230))
        d = ImageDraw.Draw(image)
        d.ellipse((8, 8, 56, 56), fill=(0, 0, 0))
        return image

    def on_open(icon, item):
        webbrowser.open('http://127.0.0.1:5000')

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    icon = pystray.Icon(
        "TailgateMonitor",
        create_image(),
        "Tailgating Monitor",
        menu=pystray.Menu(
            pystray.MenuItem("Open Dashboard", on_open),
            pystray.MenuItem("Quit", on_quit)
        )
    )
    icon.run() 