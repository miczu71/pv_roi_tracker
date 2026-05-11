"""
Google Drive backup via OAuth2 device flow.

User authorizes once by visiting a short Google URL and entering a code —
no credentials are ever entered into the add-on. After authorization a
refresh token is stored in /data/gdrive_token.json and all future uploads
are fully automatic.

Setup (one-time):
  1. console.cloud.google.com → select/create a project
  2. APIs & Services → Library → search "Google Drive API" → Enable
  3. APIs & Services → Credentials → Create Credentials → OAuth client ID
     → Application type: Desktop app → give any name → Create
  4. Copy the Client ID and Client Secret into the add-on config
  5. Create a folder on Google Drive, copy its ID from the URL
     (https://drive.google.com/drive/folders/<FOLDER_ID>)
  6. Set gdrive_folder_id in the add-on config
  7. Click "Autoryzuj Google Drive" in the add-on web UI
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TOKEN_PATH = Path('/data/gdrive_token.json')
_SCOPE = 'https://www.googleapis.com/auth/drive.file'
_DEVICE_CODE_URL = 'https://oauth2.googleapis.com/device/code'
_TOKEN_URL = 'https://oauth2.googleapis.com/token'
_DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'

_state: dict = {
    'flow': 'idle',                  # idle | pending | authorized | error
    'user_code': None,
    'verification_url': None,
    'verification_url_complete': None,  # pre-filled URL — open this directly
    'expires_at': None,
    'error': None,
}
_lock = threading.Lock()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _update(**kw) -> None:
    with _lock:
        _state.update(kw)


def is_authorized() -> bool:
    return TOKEN_PATH.exists() and bool(_load_token())


# ── Token helpers ─────────────────────────────────────────────────────────────

def _load_token() -> Optional[dict]:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_token(token: dict) -> None:
    TOKEN_PATH.write_text(json.dumps(token, indent=2))


def _get_access_token(client_id: str, client_secret: str) -> Optional[str]:
    token = _load_token()
    if token is None:
        return None
    if time.time() < token.get('expires_at', 0):
        return token['access_token']
    # Refresh
    try:
        r = requests.post(_TOKEN_URL, data={
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': token['refresh_token'],
            'grant_type': 'refresh_token',
        }, timeout=30)
        r.raise_for_status()
        d = r.json()
        token['access_token'] = d['access_token']
        token['expires_at'] = time.time() + d.get('expires_in', 3600) - 60
        _save_token(token)
        return token['access_token']
    except Exception as exc:
        logger.error('GDrive token refresh failed: %s', exc)
        return None


# ── Device flow ───────────────────────────────────────────────────────────────

def start_device_flow(client_id: str) -> None:
    """Start OAuth2 device flow in background. Poll get_state() for progress."""
    _update(flow='starting', user_code=None, verification_url=None, error=None)
    threading.Thread(target=_device_flow_worker, args=(client_id,), daemon=True).start()


def _device_flow_worker(client_id: str) -> None:
    try:
        r = requests.post(_DEVICE_CODE_URL, data={
            'client_id': client_id,
            'scope': _SCOPE,
        }, timeout=30)
        r.raise_for_status()
        d = r.json()
        if 'error' in d:
            _update(flow='error', error=d.get('error_description', d['error']))
            return

        device_code = d['device_code']
        user_code = d['user_code']
        verification_url = d.get('verification_url', 'https://google.com/device')
        verification_url_complete = d.get('verification_url_complete') or f'{verification_url}?user_code={user_code}'
        expires_at = time.time() + d.get('expires_in', 1800)
        interval = d.get('interval', 5)

        _update(flow='pending', user_code=user_code,
                verification_url=verification_url,
                verification_url_complete=verification_url_complete,
                expires_at=expires_at)
        logger.info('GDrive auth: go to %s and enter %s', verification_url, user_code)

        while time.time() < expires_at:
            time.sleep(interval)
            r = requests.post(_TOKEN_URL, data={
                'client_id': client_id,
                'device_code': device_code,
                'grant_type': _DEVICE_GRANT,
            }, timeout=30)
            d = r.json()
            if 'access_token' in d:
                _save_token({
                    'access_token': d['access_token'],
                    'refresh_token': d.get('refresh_token'),
                    'expires_at': time.time() + d.get('expires_in', 3600) - 60,
                })
                _update(flow='authorized', user_code=None, verification_url=None)
                logger.info('GDrive: authorization successful')
                return
            err = d.get('error', '')
            if err == 'authorization_pending':
                continue
            if err == 'slow_down':
                interval += 5
                continue
            _update(flow='error', error=d.get('error_description', err))
            return

        _update(flow='error', error='Czas autoryzacji wygasł')
    except Exception as exc:
        _update(flow='error', error=str(exc))
        logger.exception('GDrive device flow failed')


def revoke() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
    _update(flow='idle', user_code=None, verification_url=None, error=None)
    logger.info('GDrive: token revoked')


# ── Upload ────────────────────────────────────────────────────────────────────

def _find_file_id(headers: dict, name: str, folder_id: str) -> Optional[str]:
    r = requests.get('https://www.googleapis.com/drive/v3/files', headers=headers,
                     params={'q': f"name='{name}' and '{folder_id}' in parents and trashed=false",
                             'fields': 'files(id)', 'pageSize': '1'}, timeout=30)
    r.raise_for_status()
    files = r.json().get('files', [])
    return files[0]['id'] if files else None


def _upload_one(headers: dict, path: Path, folder_id: str) -> None:
    content = path.read_bytes()
    name = path.name
    file_id = _find_file_id(headers, name, folder_id)

    if file_id:
        r = requests.patch(
            f'https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media',
            headers={**headers, 'Content-Type': 'application/json'},
            data=content, timeout=60)
    else:
        boundary = 'pvroitracker'
        meta = json.dumps({'name': name, 'parents': [folder_id]}).encode()
        body = (
            f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
            + meta
            + f'\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n'.encode()
            + content
            + f'\r\n--{boundary}--'.encode()
        )
        r = requests.post(
            'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
            headers={**headers, 'Content-Type': f'multipart/related; boundary={boundary}'},
            data=body, timeout=60)
    r.raise_for_status()
    logger.debug('GDrive: uploaded %s', name)


def upload_files(files: list[Path], folder_id: str,
                 client_id: str, client_secret: str) -> bool:
    """Upload files to Google Drive. Returns True on success, False if skipped."""
    if not (folder_id and client_id and client_secret):
        return False
    access_token = _get_access_token(client_id, client_secret)
    if access_token is None:
        logger.warning('GDrive: not authorized — run authorization from the web UI')
        return False
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        count = sum(1 for f in files if f.exists() and (_upload_one(headers, f, folder_id) or True))
        logger.info('GDrive: %d file(s) uploaded to folder %s', count, folder_id)
        return True
    except Exception as exc:
        logger.error('GDrive upload failed: %s', exc)
        return False
