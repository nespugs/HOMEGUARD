"""HOMEGUARD FastAPI — Step 6 maintenance API; physical Arduino controls not yet connected."""
import os
import hmac
import jwt
from jwt import PyJWKClient
from pathlib import Path
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from database import (init_db, save_sensor, get_logs, get_mode, set_mode,
                      get_requested_states, set_requested_output, get_device_history, get_sleep_history,
                      get_alert_settings, set_alert_settings, get_alert_history, evaluate_alerts, get_sync_counts, get_sync_health)

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / 'website'
SENSOR_TOKEN = os.getenv('HOMEGUARD_SENSOR_TOKEN', '')
CONTROL_TOKEN = os.getenv('HOMEGUARD_CONTROL_TOKEN', '')

# Cloudflare Access authentication configuration
CF_ACCESS_TEAM_DOMAIN = os.getenv('CF_ACCESS_TEAM_DOMAIN', '')
CF_ACCESS_AUD = os.getenv('CF_ACCESS_AUD', '')
CF_ACCESS_ALLOWED_EMAIL = os.getenv('CF_ACCESS_ALLOWED_EMAIL', '')

cf_jwks_client = None

if CF_ACCESS_TEAM_DOMAIN:
    cf_jwks_client = PyJWKClient(
        f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"
    )

homeguard = {'temperature': None, 'humidity': None, 'gas': None, 'distance': None,
             'led': False, 'fan': False, 'buzzer': False, 'mode': 'AUTO', 'last_update': None}

@asynccontextmanager
async def lifespan(app):
    init_db()
    homeguard['mode'] = get_mode()
    yield

app = FastAPI(title='HOMEGUARD API', lifespan=lifespan)

class ModeChange(BaseModel):
    mode: str

class SensorData(BaseModel):
    temperature: float
    humidity: float
    gas: int
    distance: float

class AlertSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    temperature_max: float = Field(ge=-40, le=125)
    humidity_max: float = Field(ge=0, le=100)
    gas_max: float = Field(ge=0, le=1000000)
    distance_min: float = Field(ge=0, le=100000)
    cooldown_seconds: int = Field(ge=0, le=86400)

class OutputChange(BaseModel):
    enabled: bool

def verify_cloudflare_access(request: Request) -> bool:
    # Deny access unless all required settings exist.
    if not (
        CF_ACCESS_TEAM_DOMAIN
        and CF_ACCESS_AUD
        and CF_ACCESS_ALLOWED_EMAIL
        and cf_jwks_client
    ):
        return False

    # Cloudflare Access sends a signed JWT in this header.
    token = request.headers.get("Cf-Access-Jwt-Assertion")

    if not token:
        return False

    try:
        signing_key = cf_jwks_client.get_signing_key_from_jwt(token)

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=CF_ACCESS_AUD,
            issuer=f"https://{CF_ACCESS_TEAM_DOMAIN}",
            options={"require": ["exp", "iat", "iss", "aud"]}
        )

        # Only the configured operator may control hardware.
        email = payload.get("email", "")

        return hmac.compare_digest(
            email.strip().lower(),
            CF_ACCESS_ALLOWED_EMAIL.strip().lower()
        )

    except (jwt.PyJWTError, ValueError, KeyError, OSError):
        return False


def check_control_access(request: Request, x_control_token: str | None):
    # Allow a valid control token.
    if CONTROL_TOKEN and x_control_token:
        if hmac.compare_digest(x_control_token, CONTROL_TOKEN):
            return

    # Allow a verified Cloudflare Access operator.
    if verify_cloudflare_access(request):
        return

    # Allow localhost only when no control token is configured.
    if not CONTROL_TOKEN:
        if request.client and request.client.host in ('127.0.0.1', '::1'):
            return

    raise HTTPException(
        status_code=403,
        detail='Control access denied: valid authentication required'
    )

@app.get('/')
def home():
    return FileResponse(WEB_DIR / 'index.html')

app.mount('/static', StaticFiles(directory=WEB_DIR), name='static')

@app.get('/api/sensors')
def sensors():
    return homeguard

@app.post('/api/sensors')
def update_sensors(data: SensorData, x_sensor_token: str | None = Header(default=None)):
    if SENSOR_TOKEN and (not x_sensor_token or not hmac.compare_digest(x_sensor_token, SENSOR_TOKEN)):
        raise HTTPException(status_code=401, detail='Invalid sensor token')
    homeguard.update(data.model_dump())
    homeguard['last_update'] = datetime.now(timezone.utc).isoformat()
    if homeguard['mode'] == 'AUTO':
        triggered_alerts = evaluate_alerts(homeguard)
        logs = get_logs(1)
        last = datetime.fromisoformat(logs[0]['timestamp']) if logs else None
        now = datetime.fromisoformat(homeguard['last_update'])
        if last is None or now - last >= timedelta(minutes=3):
            save_sensor(homeguard)
    return {'message': 'Sensor readings received', 'data': homeguard,
            'new_alerts': triggered_alerts if homeguard['mode'] == 'AUTO' else []}

@app.get('/api/status')
def status():
    return {k: homeguard[k] for k in ('mode','led','fan','buzzer','last_update')}

@app.get('/api/logs')
def logs(limit: int = 100):
    return {'logs': get_logs(min(max(limit, 1), 1000))}

@app.get('/api/mode')
def read_mode():
    return {'mode': get_mode(), 'hardware_connected': False, 'hardware_acknowledged': False}

@app.post('/api/mode')
def change_mode(change: ModeChange, request: Request, x_control_token: str | None = Header(default=None)):
    check_control_access(request, x_control_token)
    requested = change.mode.upper()
    if requested not in ('AUTO', 'MAINTENANCE', 'SLEEP'):
        raise HTTPException(status_code=422, detail='Mode must be AUTO, MAINTENANCE, or SLEEP')
    set_mode(requested)
    homeguard['mode'] = requested
    return {'mode': requested, 'hardware_connected': False, 'hardware_acknowledged': False,
            'message': 'Backend mode saved; Arduino has not been commanded'}

@app.get('/api/maintenance/outputs')
def read_outputs():
    return {'mode': get_mode(), 'requested_outputs': get_requested_states(),
            'hardware_connected': False, 'hardware_acknowledged': False}

@app.post('/api/maintenance/outputs/{device}')
def change_output(device: str, change: OutputChange, request: Request,
                  x_control_token: str | None = Header(default=None)):
    check_control_access(request, x_control_token)
    if device not in ('led','fan','buzzer'):
        raise HTTPException(status_code=404, detail='Unknown device')
    states = set_requested_output(device, change.enabled)
    if states is None:
        raise HTTPException(status_code=409, detail='Switch to MAINTENANCE mode first')
    return {'mode': 'MAINTENANCE', 'requested_outputs': states,
            'hardware_connected': False, 'hardware_acknowledged': False,
            'message': 'Requested state saved in SQLite; physical output not controlled'}

@app.get('/api/maintenance/check')
def maintenance_check():
    if get_mode() != 'MAINTENANCE':
        raise HTTPException(status_code=409, detail='Switch to MAINTENANCE mode first')
    return {'mode': 'MAINTENANCE', 'reading': {k: homeguard[k] for k in
            ('temperature','humidity','gas','distance','last_update')},
            'fresh_hardware_sample': False,
            'message': 'Returns latest received reading; no Arduino on-demand sampling yet'}

@app.get('/api/device-history')
def device_history(limit: int = 100):
    return {'logs': get_device_history(min(max(limit, 1), 1000))}

@app.get('/api/sleep-history')
def sleep_history(limit: int = 100):
    return {'logs': get_sleep_history(min(max(limit, 1), 1000)),
            'hardware_connected': False,
            'message': 'Backend-requested sleep sessions; not measured Arduino sleep'}


@app.get('/api/alert-settings')
def read_alert_settings():
    return {'settings': get_alert_settings(), 'purpose': 'prototype testing only'}

@app.put('/api/alert-settings')
def update_alert_settings(settings: AlertSettings, request: Request,
                          x_control_token: str | None = Header(default=None)):
    check_control_access(request, x_control_token)
    return {'settings': set_alert_settings(settings.model_dump()),
            'message': 'Test thresholds saved in SQLite'}

@app.get('/api/alerts')
def alerts(limit: int = 100):
    return {'logs': get_alert_history(min(max(limit, 1), 1000)),
            'message': 'Software-generated alerts from received AUTO sensor readings'}


@app.get("/api/sync/status")
def sync_status():
    return {"counts": get_sync_counts(), "health": get_sync_health(), "message": "Counts and uploader heartbeat come from local MySQL; cloud availability is inferred from recent upload results."}


@app.get('/api/device-commands')
def device_commands(x_sensor_token: str | None = Header(default=None)):
    """Read-only commands for ESP-01. Require an explicitly configured device token."""
    if not SENSOR_TOKEN or not x_sensor_token or not hmac.compare_digest(x_sensor_token, SENSOR_TOKEN):
        raise HTTPException(status_code=401, detail='Valid sensor token required')
    mode = get_mode()
    return {'mode': mode,
            'requested_outputs': get_requested_states() if mode == 'MAINTENANCE'
            else {'led': False, 'fan': False, 'buzzer': False}}
