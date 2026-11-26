"""HOMEGUARD MySQL backend. Replace database.py only after testing."""
import os
import uuid
from datetime import datetime, timezone
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

class MySQLConnection:
    """Small compatibility layer for the existing HOMEGUARD database functions."""
    def __init__(self):
        self.conn = mysql.connector.connect(
            host=os.getenv('MYSQL_HOST', 'localhost'),
            port=int(os.getenv('MYSQL_PORT', '3306')),
            user=os.getenv('MYSQL_USER', 'homeguard_app'),
            password=os.environ['MYSQL_PASSWORD'],
            database=os.getenv('MYSQL_DATABASE', 'homeguard_db'),
            connection_timeout=10,
        )

    def execute(self, sql, params=()):
        # Existing queries use SQLite placeholders; MySQL Connector uses %s.
        sql = sql.replace('?', '%s')
        cursor = self.conn.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(sql, tuple(params))
        except Exception:
            cursor.close()
            raise
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False


def connect():
    return MySQLConnection()

SYNC_TABLES = ('sensor_logs', 'alert_logs', 'device_status_logs', 'sleep_logs')

def init_db():
    """Validate pre-created MySQL tables and seed only missing settings."""
    required = ('app_settings', 'sensor_logs', 'sleep_logs',
                'alert_logs', 'device_status_logs', 'sync_health')
    with connect() as con:
        cur = con.execute('SHOW TABLES')
        existing = {next(iter(row.values())) for row in cur.fetchall()}
        cur.close()
        missing = set(required) - existing
        if missing:
            raise RuntimeError('Missing MySQL tables: ' + ', '.join(sorted(missing)))
        con.execute("INSERT IGNORE INTO app_settings(`key`,`value`) VALUES ('mode','AUTO')").close()
        con.execute('INSERT IGNORE INTO sync_health(id) VALUES (1)').close()
        for key, value in DEFAULT_THRESHOLDS.items():
            con.execute('INSERT IGNORE INTO app_settings(`key`,`value`) VALUES (%s,%s)',
                        ('alert_' + key, str(value))).close()
        for device in ('led', 'fan', 'buzzer'):
            con.execute('INSERT IGNORE INTO app_settings(`key`,`value`) VALUES (%s,%s)',
                        (f'requested_{device}', '0')).close()

# Demonstration-only values; calibrate for the actual sensors before use.
DEFAULT_THRESHOLDS = {'temperature_max': 40.0, 'humidity_max': 85.0,
                      'gas_max': 500.0, 'distance_min': 10.0,
                      'cooldown_seconds': 60}

def get_alert_settings():
    with connect() as con:
        rows = con.execute("SELECT `key`,`value` FROM app_settings WHERE `key` LIKE 'alert_%'").fetchall()
    values = {r['key'][6:]: r['value'] for r in rows}
    return {key: int(values[key]) if key == 'cooldown_seconds' else float(values[key])
            for key in DEFAULT_THRESHOLDS}

def set_alert_settings(settings):
    with connect() as con:
        for key, value in settings.items():
            if key not in DEFAULT_THRESHOLDS:
                raise ValueError('Unknown alert setting')
            con.execute("INSERT INTO app_settings(`key`,`value`) VALUES (?,?) ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)",
                        ('alert_' + key, str(value)))
    return get_alert_settings()

def get_alert_history(limit=100):
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM alert_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

def evaluate_alerts(reading):
    """Evaluate each received AUTO reading, independent of 3-minute log interval."""
    settings = get_alert_settings()
    rules = (
        ('TEMPERATURE_HIGH', 'temperature', '>', 'temperature_max', '°C'),
        ('HUMIDITY_HIGH', 'humidity', '>', 'humidity_max', '%'),
        ('GAS_HIGH', 'gas', '>', 'gas_max', 'raw'),
        ('DISTANCE_LOW', 'distance', '<', 'distance_min', 'cm'),
    )
    now = datetime.now(timezone.utc)
    created = []
    with connect() as con:
        for kind, field, op, threshold_key, unit in rules:
            value = float(reading[field])
            threshold = settings[threshold_key]
            triggered = value > threshold if op == '>' else value < threshold
            if not triggered:
                continue
            previous = con.execute('SELECT timestamp FROM alert_logs WHERE alert_type=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
            if previous:
                elapsed = (now - datetime.fromisoformat(previous['timestamp'])).total_seconds()
                if elapsed < settings['cooldown_seconds']:
                    continue
            message = f'{field.capitalize()} {value:g} {unit} {op} test threshold {threshold:g} {unit}'
            con.execute('INSERT INTO alert_logs(timestamp,alert_type,message,record_uuid) VALUES (?,?,?,?)',
                        (now.isoformat(), kind, message, str(uuid.uuid4())))
            created.append({'alert_type': kind, 'message': message})
    return created

def save_sensor(reading):
    with connect() as con:
        con.execute('INSERT INTO sensor_logs(timestamp,temperature,humidity,gas,distance,record_uuid) VALUES (?,?,?,?,?,?)', tuple(reading[k] for k in ('last_update','temperature','humidity','gas','distance')) + (str(uuid.uuid4()),))

def get_logs(limit=100):
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM sensor_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

def get_mode():
    with connect() as con:
        row = con.execute("SELECT `value` FROM app_settings WHERE `key`='mode'").fetchone()
        return row['value'] if row else 'AUTO'

def set_mode(mode):
    """Atomically transition modes and track backend-requested sleep sessions."""
    with connect() as con:
        row = con.execute("SELECT `value` FROM app_settings WHERE `key`='mode'").fetchone()
        previous = row['value'] if row else 'AUTO'
        if previous == mode:
            return
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        if previous == 'SLEEP' and mode != 'SLEEP':
            active = con.execute('SELECT id, sleep_start FROM sleep_logs WHERE wake_time IS NULL ORDER BY id DESC LIMIT 1').fetchone()
            if active:
                start = datetime.fromisoformat(active['sleep_start'])
                seconds = max(0, (now - start).total_seconds())
                con.execute('UPDATE sleep_logs SET wake_time=?, duration_seconds=? WHERE id=?', (timestamp, seconds, active['id']))
        if previous != 'SLEEP' and mode == 'SLEEP':
            con.execute('INSERT INTO sleep_logs(sleep_start,record_uuid) VALUES (?,?)', (timestamp, str(uuid.uuid4())))
        con.execute("INSERT INTO app_settings(`key`,`value`) VALUES ('mode',?) ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)", (mode,))
        if mode != 'MAINTENANCE':
            for device in ('led','fan','buzzer'):
                con.execute("INSERT INTO app_settings(`key`,`value`) VALUES (?, '0') ON DUPLICATE KEY UPDATE `value`='0'", (f'requested_{device}',))
            _save_device_status(con, mode)


def get_sleep_history(limit=100):
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM sleep_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

def _requested_states(con):
    rows = con.execute("SELECT `key`,`value` FROM app_settings WHERE `key` IN ('requested_led','requested_fan','requested_buzzer')").fetchall()
    values = {r['key']: r['value'] == '1' for r in rows}
    return {d: values.get(f'requested_{d}', False) for d in ('led','fan','buzzer')}

def get_requested_states():
    with connect() as con:
        return _requested_states(con)

def _save_device_status(con, mode):
    states = _requested_states(con)
    con.execute('INSERT INTO device_status_logs(timestamp,mode,led,fan,buzzer,record_uuid) VALUES (?,?,?,?,?,?)', (datetime.now(timezone.utc).isoformat(), mode, int(states['led']), int(states['fan']), int(states['buzzer']), str(uuid.uuid4())))

def set_requested_output(device, enabled):
    if device not in ('led','fan','buzzer'):
        raise ValueError('Invalid output')
    with connect() as con:
        mode = con.execute("SELECT `value` FROM app_settings WHERE `key`='mode'").fetchone()
        if not mode or mode['value'] != 'MAINTENANCE':
            return None
        con.execute("INSERT INTO app_settings(`key`,`value`) VALUES (?,?) ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)", (f'requested_{device}', '1' if enabled else '0'))
        _save_device_status(con, 'MAINTENANCE')
        return _requested_states(con)

def get_device_history(limit=100):
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM device_status_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]


# STEP 11: Database-only synchronization preparation. No network requests are made here.
def get_pending_sync_batch(limit=100):
    """Return pending rows with stable UUIDs for an eventual authenticated uploader."""
    limit = max(1, min(int(limit), 1000))
    with connect() as con:
        result = {}
        for table in SYNC_TABLES:
            result[table] = [dict(r) for r in con.execute(
                f"SELECT * FROM {table} WHERE sync_status='pending' ORDER BY id LIMIT ?", (limit,))]
        return result


def get_sync_counts():
    with connect() as con:
        return {table: {status: con.execute(
            f'SELECT COUNT(*) FROM {table} WHERE sync_status=?', (status,)).fetchone()['COUNT(*)']
            for status in ('pending', 'synced')} for table in SYNC_TABLES}


def record_sync_failure(table, record_uuids, error):
    """Track failures without losing pending records."""
    if table not in SYNC_TABLES:
        raise ValueError('Unknown sync table')
    with connect() as con:
        for record_uuid in set(record_uuids):
            con.execute(f"""UPDATE {table}
                SET sync_attempts=sync_attempts+1, last_sync_error=?
                WHERE record_uuid=? AND sync_status='pending'""", (str(error)[:500], record_uuid))


def mark_sync_confirmed(table, record_uuids):
    """Only call after remote storage acknowledges the exact UUIDs."""
    if table not in SYNC_TABLES:
        raise ValueError('Unknown sync table')
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        for record_uuid in set(record_uuids):
            con.execute(f"""UPDATE {table}
                SET sync_status='synced', synced_at=?, last_sync_error=NULL
                WHERE record_uuid=? AND sync_status='pending'""", (now, record_uuid))


# Local uploader heartbeat. This does not make network calls.
def update_sync_health(run_id, *, status=None, cycle_start=False,
                       cycle_finish=False, success=False, error=None,
                       uploaded=None, failed=None):
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        row = con.execute('SELECT run_id FROM sync_health WHERE id=1').fetchone()
        if row is None:
            con.execute('INSERT INTO sync_health(id) VALUES (1)')
        # Prevent an older process from overwriting a newer runner's state.
        if not cycle_start and row and row['run_id'] not in (None, run_id):
            return
        updates = ['run_id=?', 'heartbeat_at=?']
        args = [run_id, now]
        if status is not None:
            updates.append('status=?'); args.append(status)
        if cycle_start:
            updates += ['cycle_started_at=?', 'cycle_finished_at=NULL',
                        'cycle_uploaded=0', 'cycle_failed=0', 'last_error=NULL']
            args.append(now)
        if cycle_finish:
            updates.append('cycle_finished_at=?'); args.append(now)
        if success:
            updates.append('last_success_at=?'); args.append(now)
        if error is not None:
            updates.append('last_error=?'); args.append(str(error)[:300])
        if uploaded is not None:
            updates.append('cycle_uploaded=?'); args.append(uploaded)
        if failed is not None:
            updates.append('cycle_failed=?'); args.append(failed)
        args.append(1)
        con.execute('UPDATE sync_health SET '+', '.join(updates)+' WHERE id=?', args)


def get_sync_health(stale_seconds=120):
    with connect() as con:
        row = con.execute('SELECT * FROM sync_health WHERE id=1').fetchone()
    if row is None or not row['heartbeat_at']:
        return {'status': 'not_started', 'active': False, 'heartbeat_at': None,
                'last_success_at': None, 'last_error': None,
                'cycle_uploaded': 0, 'cycle_failed': 0}
    result = dict(row)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(result['heartbeat_at'])).total_seconds()
    except (ValueError, TypeError):
        age = float('inf')
    result['active'] = 0 <= age <= stale_seconds
    if not result['active']:
        result['status'] = 'inactive'
    return result
