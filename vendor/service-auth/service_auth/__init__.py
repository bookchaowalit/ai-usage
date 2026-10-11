"""Local-key verification for short-lived, owner-scoped backend JWTs."""

from __future__ import annotations

import ipaddress
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


class AuthError(Exception):
    """Safe public error; never contains a token or decoded claims."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Identity:
    subject: str
    tenant_id: str
    scope: str


@dataclass(frozen=True)
class AuthPolicy:
    issuer: str
    audience: str
    public_key: str = field(repr=False)
    subjects: frozenset[str] = field(repr=False)
    required_scope: str
    _key: RSAPublicKey | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        try:
            origin = urlsplit(self.issuer)
            valid_origin = (origin.scheme == 'https' and origin.hostname and
                            origin.path == '' and not origin.query and not origin.fragment and
                            not origin.username and not origin.password and
                            self.issuer == f'https://{origin.netloc}' and origin.port != 0)
            key = serialization.load_pem_public_key(self.public_key.encode())
            if (valid_origin and self.audience and self.required_scope and self.subjects and
                    all(isinstance(sub, str) and 0 < len(sub) <= 512 and sub.strip() == sub
                        for sub in self.subjects) and isinstance(key, RSAPublicKey) and key.key_size >= 2048):
                object.__setattr__(self, '_key', key)
        except (ValueError, TypeError, UnicodeError):
            pass

    @classmethod
    def from_env(cls, prefix: str, audience: str, required_scope: str) -> AuthPolicy:
        return cls(os.environ.get(prefix + 'AUTH_ISSUER', '').strip(), audience,
                   os.environ.get(prefix + 'AUTH_PUBLIC_KEY', '').strip().replace('\\n', '\n'),
                   frozenset(s.strip() for s in os.environ.get(prefix + 'AUTH_SUBJECTS', '').split(',') if s.strip()),
                   required_scope)

    @property
    def configured(self) -> bool:
        return bool(self.issuer or self.public_key or self.subjects)

    @property
    def ready(self) -> bool:
        return self._key is not None

    def authenticate(self, headers: Mapping[str, str]) -> Identity:
        if not self.ready:
            raise AuthError(503, 'reads_disabled', 'Scoped read authorization is not configured')
        supplied = next((str(v) for k, v in headers.items() if k.lower() == 'authorization'), '')
        scheme, _, token = supplied.partition(' ')
        invalid = AuthError(401, 'unauthorized', 'Valid scoped bearer authorization is required')
        if scheme.lower() != 'bearer' or not token or len(token) > 8192 or token.strip() != token:
            raise invalid
        try:
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or any(k in header for k in ('jku', 'jwk', 'x5u', 'x5c', 'crit')):
                raise invalid
            claims = jwt.decode(token, self._key, algorithms=['RS256'], issuer=self.issuer,
                                audience=self.audience, leeway=5,
                                options={'strict_aud': True, 'require': [
                                    'iss', 'aud', 'sub', 'tenant_id', 'scope', 'iat', 'nbf', 'exp', 'jti']})
            if claims['iss'] != self.issuer or claims['aud'] != self.audience:
                raise invalid
            for name in ('iat', 'nbf', 'exp'):
                value = claims[name]
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise invalid
            issued, before, expires = claims['iat'], claims['nbf'], claims['exp']
            now = time.time()
            if not (0 < expires - issued <= 120 and issued - 5 <= before < expires and
                    now - 125 <= issued <= now + 5):
                raise invalid
            jti = claims['jti']
            if not isinstance(jti, str) or not 0 < len(jti) <= 128 or any(ord(c) <= 32 for c in jti):
                raise invalid
            if (claims['tenant_id'] != 'owner' or claims['sub'] not in self.subjects or
                    claims['scope'] != self.required_scope):
                raise AuthError(403, 'forbidden', 'Owner and service scope authorization is required')
            return Identity(claims['sub'], 'owner', self.required_scope)
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise invalid from None


def check_read_auth(policy: AuthPolicy, headers: Mapping[str, str], *, environment: str, token: str):
    """Static tokens are retained solely for explicitly local development."""
    if environment != 'development' or policy.configured:
        return policy.authenticate(headers)
    if token:
        scheme, _, value = headers.get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not secrets.compare_digest(value, token):
            raise AuthError(401, 'unauthorized', 'Valid bearer authorization is required')
    return None


def is_loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def readiness(policy: AuthPolicy, environment: str, store) -> tuple[int, dict]:
    # Readiness never publishes catalog rows, paths, or exception text.
    try:
        store.snapshot()
        source_ready = True
    except Exception:
        source_ready = False
    auth_ready = policy.ready
    ready = source_ready and auth_ready
    return (200 if ready else 503), {
        'status': 'ready' if ready else 'not_ready',
        'source': 'available' if source_ready else 'unavailable',
        'auth': 'ready' if auth_ready else ('development' if environment == 'development' and not policy.configured else 'disabled'),
    }


__all__ = ['AuthError', 'AuthPolicy', 'Identity', 'check_read_auth', 'is_loopback', 'readiness']
