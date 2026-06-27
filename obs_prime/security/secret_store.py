from __future__ import annotations

import base64
import binascii
import ctypes
import os
from ctypes import wintypes

MAX_DPAPI_PAYLOAD_CHARS = 8192


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.c_void_p),
    ]


def _configure_winapi() -> None:
    ctypes.windll.crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    ctypes.windll.crypt32.CryptProtectData.restype = wintypes.BOOL
    ctypes.windll.crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    ctypes.windll.crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) > 1024:
        raise RuntimeError("비밀번호가 너무 김")
    if os.name != "nt":
        raise RuntimeError("비밀번호 저장은 Windows DPAPI 환경에서만 지원됨")
    _configure_winapi()
    data = secret.encode("utf-8")
    blob_in, buffer_in = _blob_from_bytes(data)
    blob_out = _DataBlob()
    try:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        _ = buffer_in
        if blob_out.pbData:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def unprotect_secret(payload: str) -> str:
    if not payload:
        return ""
    if not payload.startswith("dpapi:"):
        raise RuntimeError("지원하지 않는 비밀번호 저장 형식")
    if len(payload) > MAX_DPAPI_PAYLOAD_CHARS:
        raise RuntimeError("저장된 비밀번호 payload가 너무 김")
    if os.name != "nt":
        raise RuntimeError("비밀번호 복호화는 Windows DPAPI 환경에서만 지원됨")
    _configure_winapi()
    try:
        encrypted = base64.b64decode(payload.removeprefix("dpapi:"), validate=True)
    except binascii.Error as exc:
        raise RuntimeError("저장된 비밀번호 payload 형식이 올바르지 않음") from exc
    blob_in, buffer_in = _blob_from_bytes(encrypted)
    blob_out = _DataBlob()
    try:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return decrypted.decode("utf-8")
    finally:
        _ = buffer_in
        if blob_out.pbData:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p)), buffer
