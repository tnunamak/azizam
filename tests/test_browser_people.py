"""Several sign-ins share a browser, never a request identity."""
from contextlib import closing
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
import proxy
from gateway import accounts, admin_store, browser_people, browser_sessions
from gateway.admin_oidc import decode_session_cookie
from test_exposure_oidc_security import _client, oidc_gateway
from test_sec_auth_login import login_claims


def session(client):
    return decode_session_cookie(client.cookies.get(proxy._browser_admin_cookie_config().name),
                                 signing_key=proxy._browser_admin_session_signing_key())


def action(client, kind, person_id=""):
    csrf = proxy._csrf_token_for_oidc_session(session(client))
    return client.post('/auth/api/browser-people/' + kind, json={'person_id': person_id},
                       headers={'x-azizam-csrf': csrf}, follow_redirects=False)


def people(client):
    response = client.get('/auth/api/browser-people')
    assert response.status_code == 200, response.text
    return response.json()['people']


@pytest.mark.parametrize('endpoint', ['https://provider.example/end', ''])
def test_provider_add_keeps_existing_session(oidc_gateway, monkeypatch, endpoint):
    with closing(_client()) as client:
        login_claims(monkeypatch, client, subject='first', email='first@example.test')
        original = session(client)
        async def discovery(issuer):
            return {'end_session_endpoint': endpoint}
        monkeypatch.setattr(proxy, '_provider_discovery', discovery)
        response = client.get('/login/provider-sign-out?add=1', follow_redirects=False)
        assert response.status_code == 302
        if endpoint:
            query = parse_qs(urlsplit(response.headers['location']).query)
            assert query['client_id'] == ['gateway']
            # Providers accept only registered return addresses: the address stays the sign-in page,
            # and the continuation rides in the logout state (web addContinuation reads it).
            assert query['post_logout_redirect_uri'][0].endswith('/sign-in/?after=provider-sign-out')
            assert query['state'] == ['add']
            next_url = '/login?adding=1'
        else:
            next_url = response.headers['location']
        authorize = client.get(next_url, follow_redirects=False)
        query = parse_qs(urlsplit(authorize.headers['location']).query)
        assert query['max_age'] == ['0']
        assert query['prompt'] == ['login select_account']
        browser_sessions.read(client.cookies.get(proxy._browser_admin_cookie_config().name),
                              signing_key=proxy._browser_admin_session_signing_key(), path=oidc_gateway)
        assert session(client).family_id == original.family_id
        login_claims(monkeypatch, client, subject='second', email='second@example.test')
        assert len(people(client)) == 2
        # Provider logout never ends the first relying-party sign-in.
        browser_sessions._check(original, browser_sessions.policy(oidc_gateway), time.time(), oidc_gateway)


def test_unknown_session_person_falls_back_to_a_plain_sign_in(oidc_gateway, monkeypatch):
    with closing(_client()) as client:
        login_claims(monkeypatch, client, subject='first', email='first@example.test')
        response = client.get('/login?adding=1&session_person=usr_not_here', follow_redirects=False)
        assert response.status_code in {302, 303}, response.text
        query = parse_qs(urlsplit(response.headers['location']).query)
        assert query['prompt'] == ['login select_account']


def test_switch_remove_fallback_all_and_cross_group(oidc_gateway, monkeypatch):
    with closing(_client()) as client, closing(_client()) as other:
        first = login_claims(monkeypatch, client, subject='one', email='one@example.test')
        first_session = session(client)
        login_claims(monkeypatch, client, subject='two', email='two@example.test')
        second = session(client)
        # A rotation can be saved before the browser receives its cookie. Reading the menu is not proof
        # that this inactive successor was delivered; recording use would defeat lost-response recovery.
        successor = browser_sessions._rotate(first_session, first_session, path=oidc_gateway, now=time.time())
        assert successor is not None
        with admin_store.transaction(oidc_gateway) as tx:
            tx.conn.execute('UPDATE browser_people SET session_json=? WHERE person_id=?',
                            (json.dumps(successor.__dict__), first['id']))
        people(client)
        with accounts.connection(oidc_gateway) as conn:
            assert conn.execute('SELECT used_at FROM account_session_lineage WHERE session_id=?',
                                (successor.session_id,)).fetchone()[0] is None
        stranger = login_claims(monkeypatch, other, subject='other', email='other@example.test')
        assert action(client, 'switch', stranger['id']).status_code == 404
        assert action(client, 'switch', first['id']).json() == {'next': '/'}
        assert session(client).subject == first['id']
        assert session(client).family_id != second.family_id
        out = client.post('/logout', headers={'x-azizam-csrf': proxy._csrf_token_for_oidc_session(session(client)), 'accept': 'application/json'})
        # Signing out the active person never makes the next person active on a shared screen.
        assert out.json() == {'next': '/sign-in/'}
        assert client.cookies.get(proxy._browser_admin_cookie_config().name) is None
        # Without an active sign-in the group cookie shows no names.
        assert people(client) == []
        # The other person is still signed in here: signing in again finds them in the menu.
        login_claims(monkeypatch, client, subject='three', email='three@example.test')
        assert {person['id'] for person in people(client)} >= {second.subject}
        assert action(client, 'switch', second.subject).json() == {'next': '/'}
        assert session(client).subject == second.subject
        assert action(client, 'all').json() == {'next': '/sign-in/'}
        assert people(client) == []
        assert len(people(other)) == 1


def test_expired_and_owner_stepup(oidc_gateway, monkeypatch):
    with closing(_client()) as client:
        owner = login_claims(monkeypatch, client, subject='owner', email='owner@example.test')
        old = session(client)
        login_claims(monkeypatch, client, subject='member', email='member@example.test')
        with admin_store.transaction(oidc_gateway) as tx:
            stale = replace(old, authenticated_at=time.time()-3600)
            tx.conn.execute('UPDATE browser_people SET session_json=? WHERE person_id=?', (json.dumps(stale.__dict__), owner['id']))
        result = action(client, 'switch', owner['id'])
        assert result.json()['next'] == '/login/provider-sign-out?add=1&session_person=' + owner['id']
        assert session(client).subject != owner['id']
        with admin_store.transaction(oidc_gateway) as tx:
            expired = replace(old, expires_at=time.time()-1)
            tx.conn.execute('UPDATE browser_people SET session_json=? WHERE person_id=?', (json.dumps(expired.__dict__), owner['id']))
        assert next(p for p in people(client) if p['id'] == owner['id'])['live'] is False
        assert action(client, 'switch', owner['id']).json()['next'].startswith('/login/provider-sign-out?add=1&session_person=')


def test_cap_and_csrf(oidc_gateway, monkeypatch):
    with closing(_client()) as client:
        for i in range(5):
            login_claims(monkeypatch, client, subject=f'person-{i}', email=f'person-{i}@example.test')
        assert len(people(client)) == 5
        with pytest.raises(AssertionError, match='browser_people_limit'):
            login_claims(monkeypatch, client, subject='sixth', email='sixth@example.test')
        assert len(people(client)) == 5
        # Test the transaction boundary, including rollback of a just-issued session.
        from starlette.requests import Request
        request = Request({'type': 'http', 'headers': [(b'cookie', '; '.join(f'{k}={v}' for k,v in client.cookies.items()).encode())]})
        with pytest.raises(accounts.AccountError, match='five people'):
            with admin_store.transaction(oidc_gateway) as tx:
                browser_people.join_in(tx.conn, request, replace(session(client), subject='sixth'),
                                       cookie=proxy._browser_admin_cookie_config(), signing_key=proxy._browser_admin_session_signing_key(), path=oidc_gateway)
        assert len(people(client)) == 5
        assert client.post('/auth/api/browser-people/all', json={}).status_code == 403


def test_current_role_window_and_revocation(oidc_gateway, monkeypatch):
    with closing(_client()) as client:
        first = login_claims(monkeypatch, client, subject='first', email='first@example.test')
        initial = session(client)
        admin_store.assign_admin_role(role='admin', subject_kind='user', subject_id=first['id'], path=oidc_gateway)
        assert not browser_people.needs_signin(initial, path=oidc_gateway)
        stale = replace(initial, authenticated_at=time.time()-3600)
        assert browser_people.needs_signin(stale, path=oidc_gateway)
        login_claims(monkeypatch, client, subject='second', email='second@example.test')
        assert action(client, 'switch', first['id']).json() == {'next': '/'}
        login_claims(monkeypatch, client, subject='second', email='second@example.test')
        browser_sessions.end(initial, path=oidc_gateway)
        assert next(p for p in people(client) if p['id'] == first['id'])['live'] is False
        before = session(client).subject
        assert action(client, 'switch', first['id']).json()['next'].startswith('/login/provider-sign-out')
        assert session(client).subject == before


def test_disabled_person_cannot_switch_and_missing_group_enrolls_active_person(oidc_gateway, monkeypatch):
    with closing(_client()) as client:
        first = login_claims(monkeypatch, client, subject='first', email='first@example.test')
        cookie = proxy._browser_admin_cookie_config()
        client.cookies.delete(browser_people.cookie_name(cookie), domain='gateway.example', path='/')
        # Existing signed-in browsers get a group on their first menu read.
        assert people(client)[0]['current'] is True
        assert client.cookies.get(browser_people.cookie_name(cookie))
        login_claims(monkeypatch, client, subject='second', email='second@example.test')
        with admin_store.transaction(oidc_gateway) as tx:
            accounts.ensure_access_row(tx.conn, first['id'])
            tx.conn.execute('UPDATE account_access SET disabled_at=? WHERE user_id=?', (int(time.time()), first['id']))
        assert next(p for p in people(client) if p['id'] == first['id'])['live'] is False
        assert action(client, 'switch', first['id']).json()['next'].startswith('/login/provider-sign-out')
        assert session(client).subject != first['id']
        active = session(client)
        assert action(client, 'remove', first['id']).json() == {'next': '/'}
        assert session(client).subject == active.subject
        assert [p['id'] for p in people(client)] == [active.subject]
        with accounts.connection(oidc_gateway) as conn:
            audit = conn.execute("SELECT actor_id, target_id FROM audit_events WHERE action='account.browser.remove' ORDER BY rowid DESC LIMIT 1").fetchone()
            assert tuple(audit) == (active.subject, first['id'])
