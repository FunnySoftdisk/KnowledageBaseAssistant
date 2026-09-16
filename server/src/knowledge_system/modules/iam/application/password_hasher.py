"""CORE：Argon2id口令散列；参数写入编码串并支持渐进rehash。"""

from __future__ import annotations

from typing import Protocol

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password: str, encoded: str) -> bool: ...

    def needs_rehash(self, encoded: str) -> bool: ...


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self._hasher = _Argon2PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, encoded: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except VerificationError:
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            return True
