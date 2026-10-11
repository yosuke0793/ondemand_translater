"""API credentials stored only in the operating system's secure credential store."""

from __future__ import annotations

import os
import re
import sys
from typing import Protocol


SERVICE_NAME = "TranslateService"
API_VARIABLES = {
    "gemini": "GEMINI_API_KEY", "azure": "AZURE_SPEECH_KEY",
}


def validate_speech_region(value: str) -> str:
    region = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9]{1,49}", region):
        raise ValueError("Azure Speechのリージョン識別子を入力してください（例: japaneast）。URLは入力しません。")
    return region


class CredentialError(RuntimeError):
    pass


class SecureBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def secure_backend() -> SecureBackend:
    try:
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            return WinVaultKeyring()
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring

            return Keyring()
    except ImportError as exc:
        raise CredentialError("APIキー保存機能がありません。アプリを再インストールしてください。") from exc
    raise CredentialError("このOSの安全なAPIキー保存には対応していません。環境変数を設定してください。")


class CredentialStore:
    def __init__(self, backend: SecureBackend | None = None) -> None:
        self._backend = backend
        self._errors: tuple[type[Exception], ...] = (OSError, RuntimeError, ValueError)

    def _native_backend(self) -> SecureBackend:
        if self._backend is None:
            self._backend = secure_backend()
            from keyring.errors import KeyringError

            self._errors += (KeyringError,)
            if sys.platform == "win32":
                import pywintypes

                self._errors += (pywintypes.error,)
        return self._backend

    @staticmethod
    def _variable(provider: str) -> str:
        if provider not in API_VARIABLES:
            raise ValueError("対応していないAPIサービスです。")
        return API_VARIABLES[provider]

    def saved_key(self, provider: str) -> str | None:
        username = self._variable(provider)
        try:
            return self._native_backend().get_password(SERVICE_NAME, username)
        except CredentialError:
            raise
        except self._errors as exc:
            # Backend exception text may contain credentials; never copy it into the UI or a log.
            raise CredentialError("OSのAPIキー保存先を読み取れません。アクセス許可を確認してください。") from exc

    def effective_key(self, provider: str) -> str | None:
        variable = self._variable(provider)
        environment_key = os.environ.get(variable, "").strip()
        return environment_key or self.saved_key(provider)

    def save_key(self, provider: str, value: str) -> None:
        username = self._variable(provider)
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("APIキーは改行を含まない文字列を入力してください。")
        try:
            self._native_backend().set_password(SERVICE_NAME, username, value)
        except CredentialError:
            raise
        except self._errors as exc:
            raise CredentialError("OSのAPIキー保存先に保存できません。アクセス許可を確認してください。") from exc

    def delete_key(self, provider: str) -> None:
        username = self._variable(provider)
        try:
            backend = self._native_backend()
            if backend.get_password(SERVICE_NAME, username) is not None:
                backend.delete_password(SERVICE_NAME, username)
        except CredentialError:
            raise
        except self._errors as exc:
            raise CredentialError("OSに保存したAPIキーを削除できません。アクセス許可を確認してください。") from exc

    def worker_credentials(self, provider: str, needs_translation: bool = True) -> dict[str, str]:
        if provider == "argos" or not needs_translation:
            return {}
        if provider != "gemini":
            raise ValueError("対応していない翻訳サービスです。")
        key = self.effective_key(provider)
        if not key:
            raise CredentialError("翻訳用APIキーが未設定です。「APIキー設定」で登録してから再度ジョブを追加してください。")
        return {self._variable(provider): key}

    def speech_credentials(self, backend: str, region: str = "") -> dict[str, str]:
        if backend == "piper":
            return {}
        if backend != "azure":
            raise ValueError("音声合成はpiperまたはazureを選択してください。旧Edge TTS／XTTSは利用できません。")
        key = self.effective_key("azure")
        if not key:
            raise CredentialError("Azure SpeechのAPIキーが未設定です。「APIキー設定」で登録してください。")
        try:
            effective_region = validate_speech_region(os.environ.get("AZURE_SPEECH_REGION") or region)
        except ValueError as exc:
            raise CredentialError(str(exc)) from exc
        return {"AZURE_SPEECH_KEY": key, "AZURE_SPEECH_REGION": effective_region}
