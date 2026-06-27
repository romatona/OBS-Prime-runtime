from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from email.message import Message
from email.parser import Parser
from dataclasses import dataclass
from typing import Any

WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WEBSOCKET_HANDSHAKE_BYTES = 16 * 1024
MAX_WEBSOCKET_FRAME_BYTES = 32 * 1024 * 1024
MAX_OBS_SCREENSHOT_IMAGE_DATA_CHARS = 24 * 1024 * 1024
MAX_OBS_SCREENSHOT_BYTES = 18 * 1024 * 1024


@dataclass(frozen=True)
class ObsConnectionInfo:
    status: str
    host: str
    port: int
    obs_studio_version: str = ""
    obs_websocket_version: str = ""
    negotiated_rpc_version: int | None = None
    current_program_scene_name: str = ""
    scene_count: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "host": self.host,
            "port": self.port,
            "obs_studio_version": self.obs_studio_version,
            "obs_websocket_version": self.obs_websocket_version,
            "negotiated_rpc_version": self.negotiated_rpc_version,
            "current_program_scene_name": self.current_program_scene_name,
            "scene_count": self.scene_count,
            "error": self.error,
        }


def fetch_obs_source_rects(
    host: str,
    port: int,
    password: str,
    source_names: list[str],
    timeout: float = 5.0,
) -> dict[str, Any]:
    client = ObsWebSocketClient(host, port, password, timeout)
    try:
        client.connect()
        scene_list = client.request("GetSceneList").get("responseData", {})
        scene_name = str(scene_list.get("currentProgramSceneName", ""))
        if not scene_name:
            raise RuntimeError("현재 프로그램 장면을 찾을 수 없음")
        items = client.request("GetSceneItemList", {"sceneName": scene_name}).get("responseData", {}).get("sceneItems", [])
        item_by_source: dict[str, dict[str, Any]] = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            source_name = str(item.get("sourceName", ""))
            if source_name and source_name not in item_by_source:
                item_by_source[source_name] = item
        rects: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for source_name in source_names:
            source_name = str(source_name)
            item = item_by_source.get(source_name)
            if item is None:
                missing.append(source_name)
                continue
            scene_item_id = item.get("sceneItemId")
            transform = client.request(
                "GetSceneItemTransform",
                {"sceneName": scene_name, "sceneItemId": scene_item_id},
            ).get("responseData", {}).get("sceneItemTransform", {})
            rects[source_name] = _transform_to_rect(transform, scene_item_id)
        return {
            "status": "connected",
            "host": host,
            "port": port,
            "current_program_scene_name": scene_name,
            "rects": rects,
            "missing": missing,
        }
    except Exception as exc:
        return {"status": "failed", "host": host, "port": port, "error": str(exc), "rects": {}, "missing": source_names}
    finally:
        client.close()


def update_obs_text_sources(
    host: str,
    port: int,
    password: str,
    text_by_source: dict[str, str],
    timeout: float = 5.0,
) -> dict[str, Any]:
    client = ObsWebSocketClient(host, port, password, timeout)
    updated: list[str] = []
    failed: dict[str, str] = {}
    try:
        client.connect()
        for source_name, text in text_by_source.items():
            try:
                client.request(
                    "SetInputSettings",
                    {
                        "inputName": source_name,
                        "inputSettings": {"text": text},
                        "overlay": True,
                    },
                )
                updated.append(source_name)
            except Exception as exc:
                failed[source_name] = str(exc)
        return {
            "status": "updated" if not failed else "partial",
            "host": host,
            "port": port,
            "updated": updated,
            "failed": failed,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "host": host,
            "port": port,
            "updated": updated,
            "failed": failed,
            "error": str(exc),
        }
    finally:
        client.close()


def capture_obs_source_screenshot(
    host: str,
    port: int,
    password: str,
    source_name: str,
    timeout: float = 5.0,
    image_format: str = "png",
    image_compression_quality: int | None = None,
) -> dict[str, Any]:
    client = ObsWebSocketClient(host, port, password, timeout)
    try:
        client.connect()
        request_data: dict[str, Any] = {
            "sourceName": source_name,
            "imageFormat": image_format,
        }
        if image_compression_quality is not None:
            request_data["imageCompressionQuality"] = int(image_compression_quality)
        response = client.request(
            "GetSourceScreenshot",
            request_data,
        ).get("responseData", {})
        image_data = str(response.get("imageData", ""))
        if not image_data:
            raise RuntimeError("OBS source screenshot imageData가 비어 있음")
        encoded = image_data.split(",", 1)[1] if "," in image_data else image_data
        if len(encoded) > MAX_OBS_SCREENSHOT_IMAGE_DATA_CHARS:
            raise RuntimeError("OBS source screenshot imageData가 너무 큼")
        image_bytes = base64.b64decode(encoded, validate=True)
        if len(image_bytes) > MAX_OBS_SCREENSHOT_BYTES:
            raise RuntimeError("OBS source screenshot decoded image가 너무 큼")
        return {
            "status": "captured",
            "host": host,
            "port": port,
            "source_name": source_name,
            "image_format": image_format,
            "image_bytes": image_bytes,
            "byte_count": len(image_bytes),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "host": host,
            "port": port,
            "source_name": source_name,
            "error": str(exc),
        }
    finally:
        client.close()


def check_obs_websocket(host: str, port: int, password: str, timeout: float = 5.0) -> ObsConnectionInfo:
    client = ObsWebSocketClient(host, port, password, timeout)
    try:
        client.connect()
        scene_list = client.request("GetSceneList")
        response = scene_list.get("responseData", {})
        return ObsConnectionInfo(
            status="connected",
            host=host,
            port=port,
            obs_studio_version=client.obs_studio_version,
            obs_websocket_version=client.obs_websocket_version,
            negotiated_rpc_version=client.negotiated_rpc_version,
            current_program_scene_name=str(response.get("currentProgramSceneName", "")),
            scene_count=len(response.get("scenes", []) or []),
        )
    except Exception as exc:
        return ObsConnectionInfo(status="failed", host=host, port=port, error=str(exc))
    finally:
        client.close()


class ObsWebSocketClient:
    def __init__(self, host: str, port: int, password: str = "", timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.obs_studio_version = ""
        self.obs_websocket_version = ""
        self.negotiated_rpc_version: int | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self.sock = sock
        self._handshake()
        hello = self._receive_json()
        if hello.get("op") != 0:
            raise RuntimeError("OBS WebSocket Hello 응답이 아님")
        data = hello.get("d", {})
        self.obs_studio_version = str(data.get("obsStudioVersion", ""))
        self.obs_websocket_version = str(data.get("obsWebSocketVersion", ""))
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = data.get("authentication")
        if auth:
            if not self.password:
                raise RuntimeError("OBS WebSocket 비밀번호가 필요함")
            identify["authentication"] = _auth_response(
                self.password,
                str(auth.get("salt", "")),
                str(auth.get("challenge", "")),
            )
        self._send_json({"op": 1, "d": identify})
        identified = self._receive_json()
        if identified.get("op") != 2:
            raise RuntimeError("OBS WebSocket 인증 실패 또는 식별 실패")
        self.negotiated_rpc_version = int(identified.get("d", {}).get("negotiatedRpcVersion", 1))

    def request(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")
        self._send_json(
            {
                "op": 6,
                "d": {
                    "requestType": request_type,
                    "requestId": request_id,
                    "requestData": request_data or {},
                },
            }
        )
        message = self._receive_json()
        if message.get("op") != 7:
            raise RuntimeError(f"OBS WebSocket 요청 응답이 아님: {message.get('op')}")
        data = message.get("d", {})
        status = data.get("requestStatus", {})
        if not status.get("result"):
            code = status.get("code", "?")
            comment = status.get("comment", "")
            raise RuntimeError(f"OBS 요청 실패: {request_type} code={code} {comment}")
        return data

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send_frame(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._socket().sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._socket().recv(4096)
            if not chunk:
                raise RuntimeError("OBS WebSocket handshake 응답 없음")
            response += chunk
            if len(response) > MAX_WEBSOCKET_HANDSHAKE_BYTES:
                raise RuntimeError("OBS WebSocket handshake 응답이 너무 큼")
        header = response.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        self._validate_handshake_response(header, key)

    def _validate_handshake_response(self, header: str, key: str) -> None:
        lines = header.splitlines()
        if not lines:
            raise RuntimeError("OBS WebSocket handshake 응답 헤더가 비어 있음")
        status_line = lines[0]
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or parts[1] != "101":
            raise RuntimeError(f"OBS WebSocket handshake 실패: {status_line}")
        headers = _parse_http_headers("\r\n".join(lines[1:]))
        upgrade = headers.get("Upgrade", "")
        if upgrade.lower() != "websocket":
            raise RuntimeError("OBS WebSocket handshake 실패: Upgrade 헤더가 websocket이 아님")
        connection_values = ",".join(headers.get_all("Connection", []))
        if "upgrade" not in {value.strip().lower() for value in connection_values.split(",")}:
            raise RuntimeError("OBS WebSocket handshake 실패: Connection Upgrade 헤더 없음")
        expected_accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_ACCEPT_GUID).encode("ascii")).digest()).decode("ascii")
        actual_accept = headers.get("Sec-WebSocket-Accept", "").strip()
        if actual_accept != expected_accept:
            raise RuntimeError("OBS WebSocket handshake 실패: Sec-WebSocket-Accept 검증 실패")

    def _receive_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._receive_frame()
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 8:
                code = None
                reason = ""
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    reason = payload[2:].decode("utf-8", errors="replace")
                raise RuntimeError(f"OBS WebSocket 연결 종료: {code or ''} {reason}".strip())
            if opcode == 9:
                self._send_frame(payload, opcode=10)

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"), opcode=1)

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket().sendall(header + mask + masked)

    def _receive_frame(self) -> tuple[int, bytes]:
        first_two = self._recv_exact(2)
        first, second = first_two[0], first_two[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        if masked:
            raise RuntimeError("OBS WebSocket 서버 프레임이 mask 처리됨")
        if not fin:
            raise RuntimeError("OBS WebSocket fragmented frame은 지원하지 않음")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if opcode >= 8 and length > 125:
            raise RuntimeError("OBS WebSocket control frame이 너무 큼")
        if length > MAX_WEBSOCKET_FRAME_BYTES:
            raise RuntimeError("OBS WebSocket frame payload가 너무 큼")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = self._socket().recv(length - len(data))
            if not chunk:
                raise RuntimeError("OBS WebSocket 연결이 끊김")
            data += chunk
        return data

    def _socket(self) -> socket.socket:
        if self.sock is None:
            raise RuntimeError("OBS WebSocket 미연결")
        return self.sock


def _auth_response(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


def _parse_http_headers(header_block: str) -> Message:
    return Parser().parsestr(header_block)


def _transform_to_rect(transform: dict[str, Any], scene_item_id: Any) -> dict[str, Any]:
    def number(key: str, default: float = 0.0) -> float:
        try:
            return float(transform.get(key, default))
        except (TypeError, ValueError):
            return default

    width = number("width")
    height = number("height")
    if width <= 0:
        width = number("sourceWidth") * number("scaleX", 1.0)
    if height <= 0:
        height = number("sourceHeight") * number("scaleY", 1.0)
    return {
        "x": int(round(number("positionX"))),
        "y": int(round(number("positionY"))),
        "w": max(0, int(round(width))),
        "h": max(0, int(round(height))),
        "scene_item_id": scene_item_id,
        "raw_transform": transform,
    }
