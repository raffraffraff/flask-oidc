from functools import wraps
import os
import json
from base64 import b64encode
import time as time_module
from copy import copy
import logging

from six.moves.urllib.parse import urlencode
from flask import request, session, redirect, url_for, g, current_app
from oauth2client.client import flow_from_clientsecrets, OAuth2WebServerFlow,\
    AccessTokenRefreshError, OAuth2Credentials
import httplib2
from itsdangerous import JSONWebSignatureSerializer, BadSignature, \
    TimedJSONWebSignatureSerializer, SignatureExpired

__all__ = ['OpenIDConnect', 'MemoryCredentials']

logger = logging.getLogger(__name__)


class MemoryCredentials(dict):
    """
    Non-persistent local credentials store.
    Use this if you only have one app server, and don't mind making everyone
    log in again after a restart.
    """
    pass


GOOGLE_ISSUERS = ['accounts.google.com', 'https://accounts.google.com']


class OpenIDConnect(object):
    """
    @see: https://developers.google.com/api-client-library/python/start/get_started
    @see: https://developers.google.com/api-client-library/python/samples/authorized_api_web_server_calendar.py
    """
    def __init__(self, app=None, credentials_store=None, http=None, time=None,
                 urandom=None):
        self.credentials_store = credentials_store\
            if credentials_store is not None\
            else MemoryCredentials()

        # stuff that we might want to override for tests
        self.http = http if http is not None else httplib2.Http()
        self.time = time if time is not None else time_module.time
        self.urandom = urandom if urandom is not None else os.urandom

        # get stuff from the app's config, which may override stuff set above
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """
        Do setup that requires a Flask app.
        """
        # Load client_secrets.json to pre-initialize some configuration
        self.client_secrets = json.loads(
            open(app.config['OIDC_CLIENT_SECRETS'],
                 'r').read()).values()[0]

        # Set some default configuration options
        app.config.setdefault('OIDC_SCOPES', ['openid', 'email'])
        app.config.setdefault('OIDC_GOOGLE_APPS_DOMAIN', None)
        app.config.setdefault('OIDC_ID_TOKEN_COOKIE_NAME', 'oidc_id_token')
        app.config.setdefault('OIDC_ID_TOKEN_COOKIE_TTL', 7 * 86400)  # 7 days
        # should ONLY be turned off for local debugging
        app.config.setdefault('OIDC_ID_TOKEN_COOKIE_SECURE', True)
        app.config.setdefault('OIDC_VALID_ISSUERS',
                              self.client_secrets.get('issuer')
                              or GOOGLE_ISSUERS)
        app.config.setdefault('OIDC_CLOCK_SKEW', 60)  # 1 minute
        app.config.setdefault('OIDC_REQUIRE_VERIFIED_EMAIL', True)

        # register callback route and cookie-setting decorator
        app.route('/oidc_callback')(self.oidc_callback)
        app.before_request(self.before_request)
        app.after_request(self.after_request)

        # Initialize oauth2client
        self.flow = flow_from_clientsecrets(
            app.config['OIDC_CLIENT_SECRETS'],
            scope=app.config['OIDC_SCOPES'])
        assert isinstance(self.flow, OAuth2WebServerFlow)

        # create signers using the Flask secret key
        self.destination_serializer = JSONWebSignatureSerializer(
            app.config['SECRET_KEY'])
        self.cookie_serializer = TimedJSONWebSignatureSerializer(
            app.config['SECRET_KEY'])

        try:
            self.credentials_store = app.config['OIDC_CREDENTIALS_STORE']
        except KeyError:
            pass

    def get_cookie_id_token(self):
        try:
            id_token_cookie = request.cookies[current_app.config['OIDC_ID_TOKEN_COOKIE_NAME']]
            return self.cookie_serializer.loads(id_token_cookie)
        except (KeyError, SignatureExpired):
            logger.debug("Missing or invalid ID token cookie", exc_info=True)
            return None

    def set_cookie_id_token(self, id_token):
        """
        Cooperates with @after_request to set a new ID token cookie.
        """
        g.oidc_id_token = id_token
        g.oidc_id_token_dirty = True

    def after_request(self, response):
        """
        Set a new ID token cookie if the ID token has changed.
        """
        if getattr(g, 'oidc_id_token_dirty', False):
            signed_id_token = self.cookie_serializer.dumps(g.oidc_id_token)
            response.set_cookie(
                current_app.config['OIDC_ID_TOKEN_COOKIE_NAME'], signed_id_token,
                secure=current_app.config['OIDC_ID_TOKEN_COOKIE_SECURE'],
                httponly=True,
                max_age=current_app.config['OIDC_ID_TOKEN_COOKIE_TTL'])
        return response

    def before_request(self):
        g.oidc_id_token = None
        self.authenticate_or_redirect()

    def authenticate_or_redirect(self):
        """
        Helper function suitable for @app.before_request and @check (below).
        Sets g.oidc_id_token to the ID token if the user has successfully
        authenticated, else returns a redirect object so they can go try
        to authenticate.
        :return: A redirect, or None if the user is authenticated.
        """
        # the auth callback and error pages don't need user to be authenticated
        if request.endpoint in frozenset(['oidc_callback', 'oidc_error']):
            return None

        # retrieve signed ID token cookie
        id_token = self.get_cookie_id_token()
        if id_token is None:
            return self.redirect_to_auth_server(request.url)

        # ID token expired
        # when Google is the IdP, this happens after one hour
        if self.time() >= id_token['exp']:
            # get credentials from store
            try:
                credentials = OAuth2Credentials.from_json(
                    self.credentials_store[id_token['sub']])
            except KeyError:
                logger.debug("Expired ID token, credentials missing",
                             exc_info=True)
                return self.redirect_to_auth_server(request.url)

            # refresh and store credentials
            try:
                credentials.refresh(self.http)
                id_token = credentials.id_token
                self.credentials_store[id_token['sub']] = credentials.to_json()
                self.set_cookie_id_token(id_token)
            except AccessTokenRefreshError:
                # Can't refresh. Wipe credentials and redirect user to IdP
                # for re-authentication.
                logger.debug("Expired ID token, can't refresh credentials",
                             exc_info=True)
                del self.credentials_store[id_token['sub']]
                return self.redirect_to_auth_server(request.url)

        # make ID token available to views
        g.oidc_id_token = id_token

        return None

    def require_login(self, view_func):
        """
        Use this to decorate view functions if only some of your app's views
        require authentication.
        """
        @wraps(view_func)
        def decorated(*args, **kwargs):
            if g.oidc_id_token is None:
                return self.redirect_to_auth_server(request.url)
            return view_func(*args, **kwargs)
        return decorated
    # Backwards compatibility
    check = require_login

    def flow_for_request(self):
        """
        Build a flow with the correct absolute callback URL for this request.
        :return:
        """
        flow = copy(self.flow)
        flow.redirect_uri = url_for('oidc_callback', _external=True)
        return flow

    def redirect_to_auth_server(self, destination):
        """
        Set a CSRF token in the session, and redirect to the IdP.
        :param destination: the page that the user was going to,
                            before we noticed they weren't logged in
        :return: a redirect response
        """
        destination = self.destination_serializer.dumps(destination).decode(
            'utf-8')
        csrf_token = b64encode(self.urandom(24)).decode('utf-8')
        session['oidc_csrf_token'] = csrf_token
        state = {
            'csrf_token': csrf_token,
            'destination': destination,
        }
        extra_params = {
            'state': json.dumps(state),
        }
        if current_app.config['OIDC_GOOGLE_APPS_DOMAIN']:
            extra_params['hd'] = current_app.config['OIDC_GOOGLE_APPS_DOMAIN']
        flow = self.flow_for_request()
        auth_url = '{url}&{extra_params}'.format(
            url=flow.step1_get_authorize_url(),
            extra_params=urlencode(extra_params))
        # if the user has an ID token, it's invalid, or we wouldn't be here
        self.set_cookie_id_token(None)
        return redirect(auth_url)

    def is_id_token_valid(self, id_token):
        """
        Check if `id_token` is a current ID token for this application,
        was issued by the Apps domain we expected,
        and that the email address has been verified.

        @see: http://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation
        """
        if not id_token:
            return False

        # step 2: check issuer
        if id_token['iss'] not in current_app.config['OIDC_VALID_ISSUERS']:
            logger.error('id_token issued by non-trusted issuer: %s'
                         % id_token['iss'])
            return False

        if isinstance(id_token['aud'], list):
            # step 3 for audience list
            if self.flow.client_id not in id_token['aud']:
                logger.error('We are not a valid audience')
                return False
            # step 4
            if 'azp' not in id_token:
                logger.error('Multiple audiences and not authorized party')
                return False
        else:
            # step 3 for single audience
            if id_token['aud'] != self.flow.client_id:
                logger.error('We are not the audience')
                return False

        # step 5
        if 'azp' in id_token and id_token['azp'] != self.flow.client_id:
            logger.error('Authorized Party is not us')
            return False

        # step 6-8: TLS checked

        # step 9: check exp
        if int(self.time()) >= int(id_token['exp']):
            logger.error('Token has expired')
            return False

        # step 10: check iat
        if id_token['iat'] < (self.time() - current_app.config['OIDC_CLOCK_SKEW']):
            logger.error('Token issued in the past')
            return False

        # (not required if using HTTPS?) step 11: check nonce

        # step 12-13: not requested acr or auth_time, so not needed to test

        # additional steps specific to our usage
        if current_app.config['OIDC_GOOGLE_APPS_DOMAIN'] and \
                id_token.get('hd') != current_app.config['OIDC_GOOGLE_APPS_DOMAIN']:
            logger.error('Invalid google apps domain')
            return False

        if not id_token.get('email_verified', False) and \
                current_app.config['OIDC_REQUIRE_VERIFIED_EMAIL']:
            logger.error('Email not verified')
            return False

        return True

    WRONG_GOOGLE_APPS_DOMAIN = 'WRONG_GOOGLE_APPS_DOMAIN'

    def oidc_callback(self):
        """
        Exchange the auth code for actual credentials,
        then redirect to the originally requested page.
        """
        # retrieve session and callback variables
        try:
            session_csrf_token = session.pop('oidc_csrf_token')

            state = json.loads(request.args['state'])
            csrf_token = state['csrf_token']
            destination = state['destination']

            code = request.args['code']
        except (KeyError, ValueError):
            logger.debug("Can't retrieve CSRF token, state, or code",
                         exc_info=True)
            return self.oidc_error()

        # check callback CSRF token passed to IdP
        # against session CSRF token held by user
        if csrf_token != session_csrf_token:
            logger.debug("CSRF token mismatch")
            return self.oidc_error()

        # make a request to IdP to exchange the auth code for OAuth credentials
        flow = self.flow_for_request()
        credentials = flow.step2_exchange(code, http=self.http)
        id_token = credentials.id_token
        if not self.is_id_token_valid(id_token):
            logger.debug("Invalid ID token")
            if id_token.get('hd') != current_app.config['OIDC_GOOGLE_APPS_DOMAIN']:
                return self.oidc_error(
                    "You must log in with an account from the {0} domain."
                    .format(current_app.config['OIDC_GOOGLE_APPS_DOMAIN']),
                    self.WRONG_GOOGLE_APPS_DOMAIN)
            return self.oidc_error()

        # store credentials by subject
        # when Google is the IdP, the subject is their G+ account number
        self.credentials_store[id_token['sub']] = credentials.to_json()

        # Check whether somebody messed with the destination
        destination = destination
        try:
            response = redirect(self.destination_serializer.loads(destination))
        except BadSignature:
            logger.error('Destination signature did not match. Rogue IdP?')
            response = redirect('/')

        # set a persistent signed cookie containing the ID token
        # and redirect to the final destination
        self.set_cookie_id_token(id_token)
        return response

    def oidc_error(self, message='Not Authorized', code=None):
        return (message, 401, {
            'Content-Type': 'text/plain',
        })

    # Below here is for resource servers to validate tokens
    def accept_token(self, require_token=False, scopes_required=None):
        """
        Use this to decorate view functions that should accept OAuth2 tokens,
        this will most likely apply to API functions.

        Note that this only works if a token introspection url is configured,
        as that URL will be queried for the validity and scopes of a token.
        """
        if scopes_required is None:
            scopes_required = []
        scopes_required = set(scopes_required)
        def wrapper(view_func):
            @wraps(view_func)
            def decorated(*args, **kwargs):
                token = None
                # TODO: Accept Authorization header
                if 'access_token' in request.form:
                    token = request.form['access_token']
                elif 'access_token' in request.args:
                    token = request.args['access_token']

                token_info = None
                valid_token = False
                has_required_scopes = False
                if token:
                    try:
                        token_info = self.get_token_info(token)
                    except Exception as ex:
                        token_info = {'active': False}
                        logger.error('ERROR: Unable to get token info')
                        logger.error(str(ex))
                    valid_token = token_info['active']

                    if 'aud' in token_info:
                        valid_audience = False
                        aud = token_info['aud']
                        clid = self.client_secrets['client_id']
                        if isinstance(aud, list):
                            valid_audience = clid in aud
                        else:
                            valid_audience = clid == aud

                        if not valid_audience:
                            logger.error('Refused token because of invalid '
                                         'audience')
                            valid_token = False

                    if valid_token:
                        token_scopes = token_info.get('scope', '').split(' ')
                    else:
                        token_scopes = []
                    has_required_scopes = scopes_required.issubset(
                        set(token_scopes))

                    if not has_required_scopes:
                        logger.debug('Token missed required scopes')

                if not require_token or (valid_token and has_required_scopes):
                    g.oidc_token_info = token_info
                    return view_func(*args, **kwargs)

                if not valid_token:
                    return (json.dumps(
                        {'error': 'invalid_token',
                         'error_description': 'Token required but invalid'}),
                        401, {'WWW-Authenticate': 'Bearer'})
                elif not has_required_scopes:
                    return (json.dumps(
                        {'error': 'invalid_token',
                         'error_description':
                             'Token does not have required scopes'}),
                        401, {'WWW-Authenticate': 'Bearer'})
                else:
                    return (json.dumps(
                        {'error': 'invalid_token',
                         'error_description':
                            'Something went wrong checking your token'}),
                        401, {'WWW-Authenticate': 'Bearer'})
            return decorated
        return wrapper

    def get_token_info(self, token):
        # We hardcode to use client_secret_post, because that's what the Google
        # oauth2client library defaults to
        request = {'token': token,
                   'token_type_hint': 'Bearer',
                   'client_id': self.client_secrets['client_id'],
                   'client_secret': self.client_secrets['client_secret']}
        headers = {'Content-type': 'application/x-www-form-urlencoded'}

        resp, content = self.http.request(
            self.client_secrets['token_introspection_uri'], 'POST',
            urlencode(request), headers=headers)
        # TODO: Cache this reply
        return json.loads(content)
