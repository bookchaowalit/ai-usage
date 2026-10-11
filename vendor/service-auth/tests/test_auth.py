import math
import time
import unittest
import os
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from service_auth import AuthError, AuthPolicy


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def setUp(self):
        self.policy = AuthPolicy('https://solo.example', 'accounts-api', self.public,
                                 frozenset({'oidc-owner'}), 'accounts:read')

    def token(self, changes=None, *, key=None, headers=None, omit=None):
        now = int(time.time())
        claims = dict(iss='https://solo.example', aud='accounts-api', sub='oidc-owner',
                      tenant_id='owner', scope='accounts:read', iat=now, nbf=now,
                      exp=now + 120, jti='test-request-id')
        claims.update(changes or {})
        if omit:
            claims.pop(omit)
        return jwt.encode(claims, key or self.key, algorithm='RS256', headers=headers)

    def rejected(self, token, status=401):
        with self.assertRaises(AuthError) as ctx:
            self.policy.authenticate({'Authorization': 'Bearer ' + token})
        self.assertEqual(ctx.exception.status, status)
        self.assertNotIn(token, str(ctx.exception))

    def test_identity_is_sanitized(self):
        identity = self.policy.authenticate({'authorization': 'Bearer ' + self.token({'private': 'sentinel'})})
        self.assertEqual(identity.subject, 'oidc-owner')
        self.assertEqual(identity.tenant_id, 'owner')
        self.assertFalse(hasattr(identity, 'private'))

    def test_signature_issuer_audience_and_algorithm(self):
        self.rejected(self.token(key=self.other_key))
        for changes in ({'iss': 'https://other.example'}, {'aud': 'knowledge-api'},
                        {'aud': ['accounts-api', 'knowledge-api']}):
            self.rejected(self.token(changes))
        self.rejected(jwt.encode({'sub': 'oidc-owner'}, 'test-secret-with-at-least-32-bytes', algorithm='HS256'))

    def test_time_bounds_and_types(self):
        now = int(time.time())
        for changes in ({'iat': now-200, 'nbf': now-200, 'exp': now-80},
                        {'iat': now+30}, {'nbf': now+30}, {'exp': now+121},
                        {'exp': now-1}, {'iat': True}, {'iat': str(now)},
                        {'iat': math.nan}, {'exp': math.inf}, {'nbf': now-30}):
            with self.subTest(changes=changes):
                self.rejected(self.token(changes))

    def test_all_claims_required(self):
        for claim in ('iss', 'aud', 'sub', 'tenant_id', 'scope', 'iat', 'nbf', 'exp', 'jti'):
            self.rejected(self.token(omit=claim))
        self.rejected(self.token({'jti': ''}))

    def test_owner_and_scope_authorization(self):
        for claims in ({'tenant_id': 'other'}, {'sub': 'other-owner'},
                       {'scope': 'knowledge:read'}, {'scope': 'accounts:read knowledge:read'}):
            self.rejected(self.token(claims), 403)

    def test_remote_key_headers_and_oversize(self):
        for header in ('jku', 'jwk', 'x5u', 'x5c'):
            self.rejected(self.token(headers={header: 'https://untrusted.invalid/key'}))
        self.rejected('x' * 9000)
        with self.assertRaises(AuthError):
            self.policy.authenticate({})

    def test_missing_or_invalid_configuration_fails_closed(self):
        for issuer, public, subjects in (
            ('https://solo.example', self.public, frozenset()),
            ('http://solo.example', self.public, frozenset({'oidc-owner'})),
            ('https://solo.example/path', self.public, frozenset({'oidc-owner'})),
            ('https://solo.example', 'PUBLIC_KEY_PLACEHOLDER', frozenset({'oidc-owner'})),
        ):
            policy = AuthPolicy(issuer, 'accounts-api', public, subjects, 'accounts:read')
            self.assertFalse(policy.ready)
            with self.assertRaises(AuthError) as ctx:
                policy.authenticate({})
            self.assertEqual(ctx.exception.status, 503)

    def test_key_rotation_rejects_previous_signature(self):
        rotated = self.other_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        policy = AuthPolicy('https://solo.example', 'accounts-api', rotated,
                            frozenset({'oidc-owner'}), 'accounts:read')
        with self.assertRaises(AuthError):
            policy.authenticate({'authorization': 'Bearer ' + self.token()})
        self.assertEqual(policy.authenticate({'authorization': 'Bearer ' + self.token(key=self.other_key)}).subject, 'oidc-owner')

    def test_environment_config_and_partial_config(self):
        with patch.dict(os.environ, {'TEST_AUTH_ISSUER': 'https://solo.example',
                                    'TEST_AUTH_PUBLIC_KEY': self.public.replace('\n', '\\n'),
                                    'TEST_AUTH_SUBJECTS': 'oidc-owner, another-owner'}, clear=True):
            policy = AuthPolicy.from_env('TEST_', 'accounts-api', 'accounts:read')
            self.assertTrue(policy.ready)
            self.assertEqual(policy.authenticate({'authorization': 'Bearer ' + self.token()}).subject, 'oidc-owner')
        with patch.dict(os.environ, {'TEST_AUTH_ISSUER': 'https://solo.example'}, clear=True):
            policy = AuthPolicy.from_env('TEST_', 'accounts-api', 'accounts:read')
            self.assertTrue(policy.configured)
            self.assertFalse(policy.ready)


if __name__ == '__main__':
    unittest.main(verbosity=2)
