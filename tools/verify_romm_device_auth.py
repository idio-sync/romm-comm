#!/usr/bin/env python3
"""Probe RomM's device authorization grant, for the per-user auth design.

Answers the questions docs/superpowers/specs/2026-09-13-per-user-romm-auth-design.md
lists as unverifiable from the OpenAPI document, because the spec declares
only the success responses:

  1. What POST /api/auth/device/token returns while pending, on denial, and
     after expiry.              <- the polling loop's entire control flow
  2. Whether GET /api/client-tokens lists the just-minted token with a
     matching device_id.        <- whether /unpair can revoke at all
  3. Whether GET /api/streaming/sessions identifies the holding user.
  6. The format and timezone of expires_at.
  8. Whether re-pairing with a stable client_device_identifier reuses the
     device row, and whether client tokens accumulate.

Verifications 4, 5 and 7 need a human looking at RomM's web UI or a second
account holding a session; this script prints exactly what to look for
rather than pretending to check them.

Run it from the same place the bot runs, so it picks up the same .env:

    python tools/verify_romm_device_auth.py

THIS SCRIPT HAS SIDE EFFECTS, unlike verify_romm_api.py. Phase A mints a
pending device request (harmless, expires on its own). Phase B waits for you
to approve it in a browser, and approving mints a REAL client token on your
account. Pass --cleanup to admin-delete that token when the run finishes.

No token material is ever printed. Credentials are described by length and
shape, never by value - anyone reading this output would otherwise be able
to spend them.
"""

import argparse
import asyncio
import json
import os
import sys
import time

import aiohttp
from dateutil import parser as date_parser
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# What the design asks for, and nothing more. If RomM grants something else,
# that is itself a finding.
REQUESTED_SCOPES = ['me.read', 'roms.user.write']

# Scopes the *bot's* credential needs for the admin-side checks here.
BOT_SCOPES = ('roms.read platforms.read firmware.read users.read users.write '
              'me.write roms.user.write')

CLIENT_NAME = 'romm-comm-verify'


def section(title):
    print()
    print(title)
    print('-' * len(title))


def show(label, value, note=''):
    suffix = f'   <- {note}' if note else ''
    print(f'  {label:<34} {value}{suffix}')


def describe_secret(value):
    """Say enough to recognise a credential's shape without leaking it."""
    if not isinstance(value, str) or not value:
        return repr(value)
    prefix = value[:4] if len(value) > 12 else ''
    return f'<{len(value)} chars{", prefix " + prefix + "..." if prefix else ""}>'


def describe_timestamp(label, raw):
    """Verification 6: can we parse it, and is it aware?"""
    show(f'{label} raw', repr(raw))
    if raw is None:
        show(f'{label} meaning', 'null = never expires',
             'the design must handle this, not assume a date')
        return
    try:
        parsed = date_parser.parse(str(raw))
    except Exception as e:
        show(f'{label} dateutil', f'NO - {e}',
             'the whole expiry ladder breaks; find the real format')
        return
    show(f'{label} dateutil', 'parses')
    show(f'{label} tzinfo', parsed.tzinfo,
         'None means naive - compare against utcnow(), never now()')


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
    data.add_field('scope', BOT_SCOPES)

    async with session.post(f'{base_url}/api/token', data=data,
                            headers={'Accept': 'application/json'}) as resp:
        if resp.status != 200:
            raise SystemExit(f'Auth failed: HTTP {resp.status} {await resp.text()}')
        return (await resp.json())['access_token'], 'password grant'


async def call(session, method, url, *, headers=None, json_body=None):
    """One request, returning (status, parsed body or raw text)."""
    kwargs = {'headers': headers or {'Accept': 'application/json'}}
    if json_body is not None:
        kwargs['json'] = json_body
    async with session.request(method, url, **kwargs) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return resp.status, text


async def device_init(session, base_url, device_identifier):
    """Verification 1, part one: start a grant and read the pending shape."""
    payload = {
        'client_device_identifier': device_identifier,
        'name': f'{CLIENT_NAME} probe',
        'client': CLIENT_NAME,
        'platform': 'discord',
        'client_version': '0.0.0-probe',
        'requested_scopes': REQUESTED_SCOPES,
    }
    status, body = await call(session, 'POST', f'{base_url}/api/auth/device/init',
                              json_body=payload)
    show('POST /auth/device/init', f'HTTP {status}',
         'spec says 201; anything else is a finding')
    if status not in (200, 201) or not isinstance(body, dict):
        show('body', repr(body)[:300])
        return None
    show('device_code', describe_secret(body.get('device_code')),
         'never leaves the bot process in the real design')
    show('user_code', body.get('user_code'), 'this one is shown to the user')
    show('verification_path', body.get('verification_path'))
    show('verification_path_complete', body.get('verification_path_complete'))
    show('expires_in', body.get('expires_in'))
    show('interval', body.get('interval'), 'the poll cadence the bot must honour')
    return body


async def poll_once(session, base_url, device_code):
    """One device/token call. The undeclared responses are the whole point."""
    return await call(session, 'POST', f'{base_url}/api/auth/device/token',
                      json_body={'device_code': device_code})


async def phase_pending(session, base_url, init_body):
    """Verification 1: the pending response, captured before any approval."""
    section('1a. device/token BEFORE approval (the "pending" branch)')
    status, body = await poll_once(session, base_url, init_body['device_code'])
    show('status', f'HTTP {status}')
    show('body', json.dumps(body) if isinstance(body, (dict, list)) else repr(body)[:300])
    print()
    print('    This is the response the polling loop sees on every tick until the')
    print('    user acts. Note whether the error is carried by the status code, a')
    print('    body field, or both - the loop has to tell it apart from a denial.')


async def phase_wait_for_approval(session, base_url, init_body, pair_origin, timeout):
    """Verification 1 + 6: poll to completion, exactly as the bot would."""
    section('1b. device/token polling until approved (or denied / expired)')

    url = f"{pair_origin}{init_body['verification_path_complete']}"
    print(f'  Approve (or deny) this request in a browser:')
    print()
    print(f'      {url}')
    print()
    print(f"  user_code: {init_body['user_code']}")
    print()
    print('  While you are there, note for the spec:')
    print('    - verification 4: does the approve screen show the device NAME')
    print(f'      ("{CLIENT_NAME} probe"), the client, and the requested scopes?')
    print('    - verification 5: what is the URL of your own API-token list?')
    print('      (/unpair links users there)')
    print()

    interval = max(int(init_body.get('interval') or 5), 1)
    deadline = time.monotonic() + timeout
    ticks = 0
    seen = set()

    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        ticks += 1
        status, body = await poll_once(session, base_url, init_body['device_code'])

        if status in (200, 201) and isinstance(body, dict) and body.get('access_token'):
            show('outcome', f'approved after {ticks} poll(s)')
            return body

        # Confirmed empirically: pending is HTTP 400 with
        # {"detail": "authorization_pending"}. The status alone therefore
        # cannot tell pending from denied or expired - the detail string is
        # the discriminator, which is what the real loop must branch on.
        detail = body.get('detail') if isinstance(body, dict) else None
        if detail not in seen:
            seen.add(detail)
            show(f'tick {ticks}', f'HTTP {status} detail={detail!r}',
                 'first sighting of this response')

        if detail == 'authorization_pending':
            continue
        if detail == 'slow_down':
            interval += 5
            show('slow_down', f'backing off to {interval}s',
                 'the real loop must honour this too')
            continue

        # Anything else is terminal: access_denied, expired_token, or a
        # string this design has not seen. Record it verbatim either way.
        show('outcome', f'terminal after {ticks} poll(s)')
        show('status', f'HTTP {status}')
        show('body', json.dumps(body) if isinstance(body, (dict, list)) else repr(body)[:300])
        return None

    show('outcome', f'no decision within {timeout}s',
         'nothing was minted; details seen: ' + repr(sorted(str(d) for d in seen)))
    return None


async def phase_inspect_grant(session, base_url, grant):
    """Verifications 2 and 6, on the freshly minted credential."""
    section('2. What the grant actually contains')
    show('access_token', describe_secret(grant.get('access_token')))
    show('device_id', grant.get('device_id'))
    show('scopes granted', grant.get('scopes'),
         'compare against ' + str(REQUESTED_SCOPES))
    describe_timestamp('expires_at', grant.get('expires_at'))

    granted = set(grant.get('scopes') or [])
    missing = set(REQUESTED_SCOPES) - granted
    extra = granted - set(REQUESTED_SCOPES)
    if missing:
        show('MISSING scopes', sorted(missing),
             'the design discards the token in this case')
    if extra:
        show('EXTRA scopes', sorted(extra),
             'RomM granted more than asked - worth knowing')

    user_headers = {'Authorization': f"Bearer {grant['access_token']}",
                    'Accept': 'application/json'}

    section('2a. GET /users/me on the new token (identity binding)')
    status, me = await call(session, 'GET', f'{base_url}/api/users/me', headers=user_headers)
    show('status', f'HTTP {status}', 'me.read must be enough')
    if isinstance(me, dict):
        show('id / username', f"{me.get('id')} / {me.get('username')}")
        show('email present', 'email' in me,
             'confirms the PII note in the blast-radius section')

    section('2b. GET /client-tokens on the new token (can we find token_id?)')
    status, tokens = await call(session, 'GET', f'{base_url}/api/client-tokens',
                                headers=user_headers)
    show('status', f'HTTP {status}', 'me.read must be enough')
    if not isinstance(tokens, list):
        show('body', repr(tokens)[:200], 'VERIFICATION 2 FAILS - see the spec fallback')
        return None

    show('tokens on account', len(tokens))
    matching = [t for t in tokens if t.get('device_id') == grant.get('device_id')]
    show('matching device_id', len(matching),
         'VERIFICATION 2 FAILS if 0 - /unpair cannot revoke'
         if not matching else 'more than 1 means the created_at tiebreak is load-bearing')
    for t in matching:
        show(f"  token id {t.get('id')}", f"name={t.get('name')!r} created={t.get('created_at')!r}")
        describe_timestamp(f"  token {t.get('id')} expires_at", t.get('expires_at'))
    nulls = [t for t in tokens if t.get('device_id') is None]
    if nulls:
        show('tokens with null device_id', len(nulls),
             'the design has to tolerate this')
    return matching[0].get('id') if matching else None


async def phase_streaming(session, base_url, headers):
    """Verification 3, read-only, on the bot's own token."""
    section('3. Streaming: does the session list identify the holder?')
    status, config = await call(session, 'GET', f'{base_url}/api/streaming/config',
                                headers=headers)
    show('GET /streaming/config', f'HTTP {status}', 'roms.read should be enough')
    if isinstance(config, dict):
        show('enabled', config.get('enabled'))
        containers = config.get('containers') or []
        show('containers', len(containers))

    status, sessions = await call(session, 'GET', f'{base_url}/api/streaming/sessions',
                                  headers=headers)
    show('GET /streaming/sessions', f'HTTP {status}')
    if isinstance(sessions, dict) and sessions:
        print('    keys:', sorted(sessions.keys()))
        print('    shape:', json.dumps(sessions)[:400])
        blob = json.dumps(sessions).lower()
        hit = [k for k in ('user_id', 'username', 'user', 'owner') if k in blob]
        show('holder identifiable', hit or 'NO',
             'verification 3 - the queue needs this to map back to Discord')
    else:
        show('sessions', repr(sessions)[:200],
             'empty - claim a session from RomM\'s UI and re-run to see the shape')

    print()
    print('    Verification 7 cannot be automated safely: it needs a session held')
    print('    by a DIFFERENT user, then DELETE /api/streaming/sessions/{platform}')
    print('    with this bot token. If that 403s, admin force-reclaim needs a')
    print('    different mechanism and the spec section must change.')


async def phase_repair(session, base_url, device_identifier, user_headers):
    """Verification 8: does re-pairing reuse the device, or pile up tokens?"""
    section('8. Re-pairing with the same client_device_identifier')
    status, before = await call(session, 'GET', f'{base_url}/api/client-tokens',
                                headers=user_headers)
    count_before = len(before) if isinstance(before, list) else '?'
    show('tokens before second init', count_before)
    print('    Re-run this script with the same --device-id and approve again,')
    print('    then compare this count and the device_id. Same device_id plus a')
    print('    higher count means tokens accumulate per device, which is what')
    print('    makes step 6\'s created_at tiebreak necessary.')


async def cleanup(session, base_url, headers, token_id):
    """Admin-delete the token this run minted. users.write, on the bot token."""
    section('Cleanup')
    if token_id is None:
        show('skipped', 'no token_id was resolved',
             'delete it by hand in RomM if a token was minted')
        return
    status, body = await call(session, 'DELETE',
                              f'{base_url}/api/client-tokens/{token_id}/admin',
                              headers=headers)
    show(f'DELETE /client-tokens/{token_id}/admin', f'HTTP {status}',
         'this is the same call /unpair makes' if status in (200, 204)
         else 'revocation path does NOT work - a finding in itself')
    if status not in (200, 204):
        show('body', repr(body)[:200])


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--device-id', default='romm-comm-verify:probe',
                    help='client_device_identifier to send (reuse it to test verification 8)')
    ap.add_argument('--timeout', type=int, default=300,
                    help='seconds to wait for you to approve in the browser (default 300)')
    ap.add_argument('--pending-only', action='store_true',
                    help='capture the pending response and stop; mints nothing durable')
    ap.add_argument('--cleanup', action='store_true',
                    help='admin-delete the client token this run mints')
    args = ap.parse_args()

    load_dotenv()
    base_url = (os.getenv('API_URL') or '').rstrip('/')
    if not base_url:
        raise SystemExit('API_URL is not set. Run this where the bot runs.')

    # The pairing URL the user opens is NOT necessarily the API URL - that is
    # the whole point of ROMM_PAIR_BASE_URL in the design. DOMAIN defaults to
    # a sentinel string, so it is only usable when it looks like an origin.
    domain = (os.getenv('DOMAIN') or '').rstrip('/')
    pair_origin = domain if domain.startswith(('http://', 'https://')) else base_url

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        token, how = await authenticate(session, base_url)
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        print(f'Authenticated against {base_url} via {how}')
        show('pairing origin', pair_origin,
             'from DOMAIN' if pair_origin == domain else 'DOMAIN unusable, fell back to API_URL')

        section('0. Device auth endpoints present?')
        status, _ = await call(session, 'POST', f'{base_url}/api/auth/device/init',
                               json_body={})
        show('init with empty body', f'HTTP {status}',
             '422 means the route exists and validated; 404 means no device grant')
        if status == 404:
            raise SystemExit('This server has no device authorization grant. Stop here.')

        init_body = await device_init(session, base_url, args.device_id)
        if not init_body:
            raise SystemExit('device/init did not return a grant; nothing further to probe.')

        await phase_pending(session, base_url, init_body)

        if args.pending_only:
            print()
            print('--pending-only: stopping before approval. The pending request')
            print(f"expires on its own in {init_body.get('expires_in')}s.")
            return

        print()
        print('  NOTE: approving the next step mints a REAL client token on the')
        print('  account you approve with. Pass --cleanup to delete it afterwards.')

        grant = await phase_wait_for_approval(session, base_url, init_body,
                                              pair_origin, args.timeout)

        token_id = None
        if grant:
            token_id = await phase_inspect_grant(session, base_url, grant)
            user_headers = {'Authorization': f"Bearer {grant['access_token']}",
                            'Accept': 'application/json'}
            await phase_repair(session, base_url, args.device_id, user_headers)

        await phase_streaming(session, base_url, headers)

        if args.cleanup and grant:
            await cleanup(session, base_url, headers, token_id)
        elif grant:
            section('Cleanup')
            show('skipped', 'pass --cleanup to admin-delete the minted token',
                 'otherwise delete it in RomM by hand')

        section('Still needs a human')
        print('  4. Does the approve screen render the device name? (seen above)')
        print('  5. The URL of a user\'s own API-token list, for /unpair copy.')
        print('  7. Can this bot token release a session it does not own?')
        print('  1c. The DENIED and EXPIRED responses: re-run, and either deny in')
        print('      the UI or leave it until expires_in elapses.')


if __name__ == '__main__':
    asyncio.run(main())
