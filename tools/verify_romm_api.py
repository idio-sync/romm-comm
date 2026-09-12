#!/usr/bin/env python3
"""Read-only probe of a live RomM server.

Answers the questions romm-comm's code currently assumes rather than knows:
what the server's timestamps look like, whether the endpoints we call behave
the way the docs say, and how this bot's HTTP layer reports a missing route.

Run it from the same place the bot runs, so it picks up the same .env:

    python tools/verify_romm_api.py

Every check is a GET. Nothing is created, modified or deleted. The one probe
with a side effect - minting an invite token - is opt-in behind --invite, and
is called out again where it runs.
"""

import argparse
import asyncio
import json
import os
import sys

import aiohttp
from dateutil import parser as date_parser
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCOPES = 'roms.read platforms.read firmware.read users.read users.write me.write'


def section(title):
    print()
    print(title)
    print('-' * len(title))


def show(label, value, note=''):
    suffix = f'   <- {note}' if note else ''
    print(f'  {label:<34} {value}{suffix}')


async def authenticate(session, base_url):
    """Mirror the bot: a static client token if set, otherwise a password grant."""
    client_token = os.getenv('ROMM_CLIENT_TOKEN')
    if client_token:
        return client_token, 'ROMM_CLIENT_TOKEN'

    user = os.getenv('ROMM_USER') or os.getenv('ROMM_USERNAME')
    password = os.getenv('ROMM_PASS') or os.getenv('ROMM_PASSWORD')
    if not user or not password:
        raise SystemExit(
            'No ROMM_CLIENT_TOKEN and no ROMM_USER/ROMM_PASS found. '
            'Run this where the bot runs, or set them in the environment.'
        )

    data = aiohttp.FormData()
    data.add_field('grant_type', 'password')
    data.add_field('username', user)
    data.add_field('password', password)
    data.add_field('scope', SCOPES)

    async with session.post(f'{base_url}/api/token', data=data,
                            headers={'Accept': 'application/json'}) as resp:
        if resp.status != 200:
            raise SystemExit(f'Auth failed: HTTP {resp.status} {await resp.text()}')
        return (await resp.json())['access_token'], 'password grant'


async def get(session, base_url, headers, path):
    """GET one API path, returning (status, parsed body or raw text)."""
    async with session.get(f'{base_url}/api/{path}', headers=headers) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return resp.status, text


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--invite', action='store_true',
                    help='also mint a real single-use invite token to inspect its shape')
    args = ap.parse_args()

    load_dotenv()
    base_url = (os.getenv('API_URL') or '').rstrip('/')
    if not base_url:
        raise SystemExit('API_URL is not set. Run this where the bot runs.')

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        token, how = await authenticate(session, base_url)
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        print(f'Authenticated against {base_url} via {how}')

        # --- 1. What are we actually talking to? ---------------------------
        section('1. Server version')
        status, body = await get(session, base_url, headers, 'heartbeat')
        version = None
        if isinstance(body, dict):
            version = (body.get('VERSION') or body.get('version')
                       or (body.get('SYSTEM') or {}).get('VERSION'))
        show('GET /heartbeat', f'HTTP {status}')
        show('version', version or '(not found - see raw below)')
        if not version and isinstance(body, dict):
            print(f'    raw keys: {sorted(body.keys())}')

        # --- 2. Timestamps the reconciler compares against -----------------
        section('2. GET /users - created_at, used by invite reconciliation')
        status, users = await get(session, base_url, headers, 'users')
        show('status', f'HTTP {status}')
        if isinstance(users, list) and users:
            sample = users[0]
            raw = sample.get('created_at')
            show('field present', 'created_at' in sample)
            show('raw value', repr(raw))
            try:
                parsed = date_parser.parse(str(raw))
                show('dateutil parses it', 'yes')
                show('tzinfo', parsed.tzinfo,
                     'None means we assume UTC - confirm that is right')
            except Exception as e:
                show('dateutil parses it', f'NO - {e}', 'reconciler would skip every invite')
            show('user count', len(users))
        else:
            show('users', repr(users)[:120])

        # --- 3. The endpoint /random now depends on ------------------------
        section('3. GET /roms/random - added in RomM 5.2.0')
        status, rom = await get(session, base_url, headers, 'roms/random')
        show('status', f'HTTP {status}',
             '404 means pre-5.2.0 and the fallback path is what runs')
        if status == 200 and isinstance(rom, dict):
            show('id / name', f"{rom.get('id')} / {rom.get('name')}")
            show('platform_id present', 'platform_id' in rom)
            status2, rom2 = await get(session, base_url, headers, 'roms/random')
            show('second call picks', f"id {rom2.get('id') if isinstance(rom2, dict) else rom2}",
                 'should usually differ on a library of any size')
        elif status == 200:
            show('body', repr(rom), 'null means the scope holds no ROMs')

        # --- 4. Platform-scoped pick --------------------------------------
        section('4. GET /roms/random?platform_ids=N')
        status, platforms = await get(session, base_url, headers, 'platforms')
        if isinstance(platforms, list) and platforms:
            target = max(platforms, key=lambda p: p.get('rom_count') or 0)
            pid = target.get('id')
            show('platform used', f"{target.get('name')} (id {pid}, {target.get('rom_count')} roms)")
            status, rom = await get(session, base_url, headers, f'roms/random?platform_ids={pid}')
            show('status', f'HTTP {status}')
            if isinstance(rom, dict):
                got = rom.get('platform_id')
                show('returned platform_id', got,
                     'MISMATCH - the filter is not applied' if got != pid else 'filter works')
        else:
            show('platforms', repr(platforms)[:120])

        # --- 5. How this bot sees a missing route -------------------------
        section('5. Unknown route - how a 404 surfaces')
        status, body = await get(session, base_url, headers, 'roms/definitely-not-a-route')
        show('status', f'HTTP {status}',
             'the bot logs this and returns None, which is what /random falls back on')

        # --- 6. with_total, the remaining suggested change ----------------
        section('6. GET /roms?limit=1&with_total=false')
        status, body = await get(session, base_url, headers, 'roms?limit=1&with_total=false')
        show('status', f'HTTP {status}')
        if isinstance(body, dict):
            show('total', repr(body.get('total')),
                 'null confirms the count was skipped')
            show('items returned', len(body.get('items') or []))

        # --- 7. Invite link shape (opt-in: this creates a token) ----------
        section('7. POST /users/invite-link')
        if not args.invite:
            show('skipped', 'pass --invite to run',
                 'it mints a real single-use token')
        else:
            print('  NOTE: this creates a real single-use invite token.')
            async with session.post(f'{base_url}/api/users/invite-link',
                                    params={'role': 'user'}, headers=headers) as resp:
                status = resp.status
                body = await resp.json() if resp.status == 200 else await resp.text()
            show('status', f'HTTP {status}')
            if isinstance(body, dict):
                show("'url' key present", 'url' in body, 'absent means pre-5.2.0')
                show("'url' value", repr(body.get('url')),
                     'null means ROMM_BASE_URL is unset or loopback')
                show('token length', len(body.get('token') or ''))

    print()
    print('Done. Paste the output back for interpretation.')


if __name__ == '__main__':
    asyncio.run(main())
