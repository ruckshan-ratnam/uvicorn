import asyncio
import typing
from copy import deepcopy

import httpx
import pytest

from tests.protocols.test_http import HTTP_PROTOCOLS
from tests.utils import run_server
from uvicorn.config import Config
from uvicorn.protocols.websockets.wsproto_impl import WSProtocol

try:
    import websockets
    import websockets.client
    import websockets.exceptions
    from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory
    from websockets.typing import Subprotocol

    from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol

    ONLY_WEBSOCKETS_PROTOCOL = [WebSocketProtocol]
    WS_PROTOCOLS = [WSProtocol, WebSocketProtocol]
except ImportError:  # pragma: nocover
    ONLY_WEBSOCKETS_PROTOCOL = []
    WS_PROTOCOLS = [WSProtocol]

    pytestmark = pytest.mark.skip(reason="This test needs the websockets module")

ONLY_WS_PROTOCOL = [WSProtocol]


class WebSocketResponse:
    def __init__(self, scope, receive, send):
        self.scope = scope
        self.receive = receive
        self.send = send

    def __await__(self):
        return self.asgi().__await__()

    async def asgi(self):
        while True:
            message = await self.receive()
            message_type = message["type"].replace(".", "_")
            handler = getattr(self, message_type, None)
            if handler is not None:
                await handler(message)
            if message_type == "websocket_disconnect":
                break


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_invalid_upgrade(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    def app(scope):
        return None

    config = Config(
        app=app, ws=ws_protocol_cls, http=http_protocol_cls, port=unused_tcp_port
    )
    async with run_server(config):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{unused_tcp_port}",
                headers={
                    "upgrade": "websocket",
                    "connection": "upgrade",
                    "sec-webSocket-version": "11",
                },
            )
        if response.status_code == 426:
            # response.text == ""
            pass  # ok, wsproto 0.13
        else:
            assert response.status_code == 400
            assert response.text.lower().strip().rstrip(".") in [
                "missing sec-websocket-key header",
                "missing sec-websocket-version header",  # websockets
                "missing or empty sec-websocket-key header",  # wsproto
                "failed to open a websocket connection: missing "
                "sec-websocket-key header",
                "failed to open a websocket connection: missing or empty "
                "sec-websocket-key header",
            ]


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_accept_connection(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.open

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        is_open = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert is_open


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_supports_permessage_deflate_extension(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        extension_factories = [ClientPerMessageDeflateFactory()]
        async with websockets.client.connect(
            url, extensions=extension_factories
        ) as websocket:
            return [extension.name for extension in websocket.extensions]

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        extension_names = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert "permessage-deflate" in extension_names


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_can_disable_permessage_deflate_extension(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        # enable per-message deflate on the client, so that we can check the server
        # won't support it when it's disabled.
        extension_factories = [ClientPerMessageDeflateFactory()]
        async with websockets.client.connect(
            url, extensions=extension_factories
        ) as websocket:
            return [extension.name for extension in websocket.extensions]

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        ws_per_message_deflate=False,
        port=unused_tcp_port,
    )
    async with run_server(config):
        extension_names = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert "permessage-deflate" not in extension_names


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_close_connection(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.close"})

    async def open_connection(url):
        try:
            await websockets.client.connect(url)
        except websockets.exceptions.InvalidHandshake:
            return False
        return True  # pragma: no cover

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        is_open = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert not is_open


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_headers(ws_protocol_cls, http_protocol_cls, unused_tcp_port: int):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            headers = self.scope.get("headers")
            headers = dict(headers)
            assert headers[b"host"].startswith(b"127.0.0.1")
            assert headers[b"username"] == bytes("abraão", "utf-8")
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(
            url, extra_headers=[("username", "abraão")]
        ) as websocket:
            return websocket.open

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        is_open = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert is_open


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_extra_headers(ws_protocol_cls, http_protocol_cls, unused_tcp_port: int):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send(
                {"type": "websocket.accept", "headers": [(b"extra", b"header")]}
            )

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.response_headers

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        extra_headers = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert extra_headers.get("extra") == "header"


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_path_and_raw_path(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            path = self.scope.get("path")
            raw_path = self.scope.get("raw_path")
            assert path == "/one/two"
            assert raw_path == b"/one%2Ftwo"
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.open

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        is_open = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}/one%2Ftwo")
        assert is_open


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_text_data_to_client(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})
            await self.send({"type": "websocket.send", "text": "123"})

    async def get_data(url):
        async with websockets.client.connect(url) as websocket:
            return await websocket.recv()

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        data = await get_data(f"ws://127.0.0.1:{unused_tcp_port}")
        assert data == "123"


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_binary_data_to_client(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})
            await self.send({"type": "websocket.send", "bytes": b"123"})

    async def get_data(url):
        async with websockets.client.connect(url) as websocket:
            return await websocket.recv()

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        data = await get_data(f"ws://127.0.0.1:{unused_tcp_port}")
        assert data == b"123"


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_and_close_connection(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})
            await self.send({"type": "websocket.send", "text": "123"})
            await self.send({"type": "websocket.close"})

    async def get_data(url):
        async with websockets.client.connect(url) as websocket:
            data = await websocket.recv()
            is_open = True
            try:
                await websocket.recv()
            except Exception:
                is_open = False
            return (data, is_open)

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        (data, is_open) = await get_data(f"ws://127.0.0.1:{unused_tcp_port}")
        assert data == "123"
        assert not is_open


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_text_data_to_server(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

        async def websocket_receive(self, message):
            _text = message.get("text")
            await self.send({"type": "websocket.send", "text": _text})

    async def send_text(url):
        async with websockets.client.connect(url) as websocket:
            await websocket.send("abc")
            return await websocket.recv()

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        data = await send_text(f"ws://127.0.0.1:{unused_tcp_port}")
        assert data == "abc"


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_binary_data_to_server(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

        async def websocket_receive(self, message):
            _bytes = message.get("bytes")
            await self.send({"type": "websocket.send", "bytes": _bytes})

    async def send_text(url):
        async with websockets.client.connect(url) as websocket:
            await websocket.send(b"abc")
            return await websocket.recv()

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        data = await send_text(f"ws://127.0.0.1:{unused_tcp_port}")
        assert data == b"abc"


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_after_protocol_close(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})
            await self.send({"type": "websocket.send", "text": "123"})
            await self.send({"type": "websocket.close"})
            with pytest.raises(Exception):
                await self.send({"type": "websocket.send", "text": "123"})

    async def get_data(url):
        async with websockets.client.connect(url) as websocket:
            data = await websocket.recv()
            is_open = True
            try:
                await websocket.recv()
            except Exception:
                is_open = False
            return (data, is_open)

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        (data, is_open) = await get_data(f"ws://127.0.0.1:{unused_tcp_port}")
        assert data == "123"
        assert not is_open


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_missing_handshake(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    async def app(app, receive, send):
        pass

    async def connect(url):
        await websockets.client.connect(url)

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        with pytest.raises(websockets.exceptions.InvalidStatusCode) as exc_info:
            await connect(f"ws://127.0.0.1:{unused_tcp_port}")
        assert exc_info.value.status_code == 500


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_before_handshake(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    async def app(scope, receive, send):
        await send({"type": "websocket.send", "text": "123"})

    async def connect(url):
        await websockets.client.connect(url)

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        with pytest.raises(websockets.exceptions.InvalidStatusCode) as exc_info:
            await connect(f"ws://127.0.0.1:{unused_tcp_port}")
        assert exc_info.value.status_code == 500


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_duplicate_handshake(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    async def app(scope, receive, send):
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.accept"})

    async def connect(url):
        async with websockets.client.connect(url) as websocket:
            _ = await websocket.recv()

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await connect(f"ws://127.0.0.1:{unused_tcp_port}")
        assert exc_info.value.code == 1006


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_asgi_return_value(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    """
    The ASGI callable should return 'None'. If it doesn't make sure that
    the connection is closed with an error condition.
    """

    async def app(scope, receive, send):
        await send({"type": "websocket.accept"})
        return 123

    async def connect(url):
        async with websockets.client.connect(url) as websocket:
            _ = await websocket.recv()

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await connect(f"ws://127.0.0.1:{unused_tcp_port}")
        assert exc_info.value.code == 1006


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
@pytest.mark.parametrize("code", [None, 1000, 1001])
@pytest.mark.parametrize(
    "reason",
    [None, "test", False],
    ids=["none_as_reason", "normal_reason", "without_reason"],
)
async def test_app_close(
    ws_protocol_cls, http_protocol_cls, code, reason, unused_tcp_port: int
):
    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
            elif message["type"] == "websocket.receive":
                reply = {"type": "websocket.close"}

                if code is not None:
                    reply["code"] = code

                if reason is not False:
                    reply["reason"] = reason

                await send(reply)
            elif message["type"] == "websocket.disconnect":
                break

    async def websocket_session(url):
        async with websockets.client.connect(url) as websocket:
            await websocket.ping()
            await websocket.send("abc")
            await websocket.recv()

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await websocket_session(f"ws://127.0.0.1:{unused_tcp_port}")
        assert exc_info.value.code == (code or 1000)
        assert exc_info.value.reason == (reason or "")


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_client_close(ws_protocol_cls, http_protocol_cls, unused_tcp_port: int):
    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
            elif message["type"] == "websocket.receive":
                pass
            elif message["type"] == "websocket.disconnect":
                break

    async def websocket_session(url):
        async with websockets.client.connect(url) as websocket:
            await websocket.ping()
            await websocket.send("abc")

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        await websocket_session(f"ws://127.0.0.1:{unused_tcp_port}")


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_client_connection_lost(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    got_disconnect_event = False

    async def app(scope, receive, send):
        nonlocal got_disconnect_event
        while True:
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
            elif message["type"] == "websocket.disconnect":
                break

        got_disconnect_event = True

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        ws_ping_interval=0.0,
        port=unused_tcp_port,
    )
    async with run_server(config):
        async with websockets.client.connect(
            f"ws://127.0.0.1:{unused_tcp_port}"
        ) as websocket:
            websocket.transport.close()
            await asyncio.sleep(0.1)
            got_disconnect_event_before_shutdown = got_disconnect_event

    assert got_disconnect_event_before_shutdown is True


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_connection_lost_before_handshake_complete(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    send_accept_task = asyncio.Event()
    disconnect_message = {}

    async def app(scope, receive, send):
        nonlocal disconnect_message
        message = await receive()
        if message["type"] == "websocket.connect":
            await send_accept_task.wait()
        disconnect_message = await receive()

    response: typing.Optional[httpx.Response] = None

    async def websocket_session(uri):
        nonlocal response
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{unused_tcp_port}",
                headers={
                    "upgrade": "websocket",
                    "connection": "upgrade",
                    "sec-websocket-version": "13",
                    "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                },
            )

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        task = asyncio.create_task(
            websocket_session(f"ws://127.0.0.1:{unused_tcp_port}")
        )
        await asyncio.sleep(0.1)
        send_accept_task.set()
        await asyncio.sleep(0.1)

    assert response is not None
    assert response.status_code == 500, response.text
    assert response.text == "Internal Server Error"
    assert disconnect_message == {"type": "websocket.disconnect", "code": 1006}
    await task


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_send_close_on_server_shutdown(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    disconnect_message = {}
    server_shutdown_event = asyncio.Event()

    async def app(scope, receive, send):
        nonlocal disconnect_message
        while True:
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
            elif message["type"] == "websocket.disconnect":
                disconnect_message = message
                break

    websocket: typing.Optional[websockets.client.WebSocketClientProtocol] = None

    async def websocket_session(uri):
        nonlocal websocket
        async with websockets.client.connect(uri) as ws_connection:
            websocket = ws_connection
            await server_shutdown_event.wait()

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        task = asyncio.create_task(
            websocket_session(f"ws://127.0.0.1:{unused_tcp_port}")
        )
        await asyncio.sleep(0.1)
        disconnect_message_before_shutdown = disconnect_message
    server_shutdown_event.set()

    assert websocket is not None
    assert websocket.close_code == 1012
    assert disconnect_message_before_shutdown == {}
    assert disconnect_message == {"type": "websocket.disconnect", "code": 1012}
    task.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
@pytest.mark.parametrize("subprotocol", ["proto1", "proto2"])
async def test_subprotocols(
    ws_protocol_cls, http_protocol_cls, subprotocol, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept", "subprotocol": subprotocol})

    async def get_subprotocol(url):
        async with websockets.client.connect(
            url, subprotocols=[Subprotocol("proto1"), Subprotocol("proto2")]
        ) as websocket:
            return websocket.subprotocol

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        accepted_subprotocol = await get_subprotocol(
            f"ws://127.0.0.1:{unused_tcp_port}"
        )
        assert accepted_subprotocol == subprotocol


MAX_WS_BYTES = 1024 * 1024 * 16
MAX_WS_BYTES_PLUS1 = MAX_WS_BYTES + 1


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", ONLY_WEBSOCKETS_PROTOCOL)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
@pytest.mark.parametrize(
    "client_size_sent, server_size_max, expected_result",
    [
        (MAX_WS_BYTES, MAX_WS_BYTES, 0),
        (MAX_WS_BYTES_PLUS1, MAX_WS_BYTES, 1009),
        (10, 10, 0),
        (11, 10, 1009),
    ],
    ids=[
        "max=defaults sent=defaults",
        "max=defaults sent=defaults+1",
        "max=10 sent=10",
        "max=10 sent=11",
    ],
)
async def test_send_binary_data_to_server_bigger_than_default(
    ws_protocol_cls,
    http_protocol_cls,
    client_size_sent,
    server_size_max,
    expected_result,
    unused_tcp_port: int,
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

        async def websocket_receive(self, message):
            _bytes = message.get("bytes")
            await self.send({"type": "websocket.send", "bytes": _bytes})

    async def send_text(url):
        async with websockets.client.connect(
            url, max_size=client_size_sent
        ) as websocket:
            await websocket.send(b"\x01" * client_size_sent)
            return await websocket.recv()

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        ws_max_size=server_size_max,
        port=unused_tcp_port,
    )
    async with run_server(config):
        if expected_result == 0:
            data = await send_text(f"ws://127.0.0.1:{unused_tcp_port}")
            assert data == b"\x01" * client_size_sent
        else:
            with pytest.raises(websockets.exceptions.ConnectionClosedError) as e:
                data = await send_text(f"ws://127.0.0.1:{unused_tcp_port}")
            assert e.value.code == expected_result


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_server_reject_connection(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    async def app(scope, receive, send):
        assert scope["type"] == "websocket"

        # Pull up first recv message.
        message = await receive()
        assert message["type"] == "websocket.connect"

        # Reject the connection.
        await send({"type": "websocket.close"})
        # -- At this point websockets' recv() is unusable. --

        # This doesn't raise `TypeError`:
        # See https://github.com/encode/uvicorn/issues/244
        message = await receive()
        assert message["type"] == "websocket.disconnect"

    async def websocket_session(url):
        try:
            async with websockets.client.connect(url):
                pass  # pragma: no cover
        except Exception:
            pass

    config = Config(
        app=app,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        await websocket_session(f"ws://127.0.0.1:{unused_tcp_port}")


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_server_can_read_messages_in_buffer_after_close(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    frames = []
    disconnect_message = {}

    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})
            # Ensure server doesn't start reading frames from read buffer until
            # after client has sent close frame, but server is still able to
            # read these frames
            await asyncio.sleep(0.2)

        async def websocket_disconnect(self, message):
            nonlocal disconnect_message
            disconnect_message = message

        async def websocket_receive(self, message):
            frames.append(message.get("bytes"))

    async def send_text(url):
        async with websockets.client.connect(url) as websocket:
            await websocket.send(b"abc")
            await websocket.send(b"abc")
            await websocket.send(b"abc")

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        await send_text(f"ws://127.0.0.1:{unused_tcp_port}")

    assert frames == [b"abc", b"abc", b"abc"]
    assert disconnect_message == {"type": "websocket.disconnect", "code": 1000}


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_default_server_headers(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.response_headers

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        headers = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert headers.get("server") == "uvicorn" and "date" in headers


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_no_server_headers(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.response_headers

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        server_header=False,
        port=unused_tcp_port,
    )
    async with run_server(config):
        headers = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert "server" not in headers


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", ONLY_WS_PROTOCOL)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_no_date_header(ws_protocol_cls, http_protocol_cls, unused_tcp_port: int):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.response_headers

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        date_header=False,
        port=unused_tcp_port,
    )
    async with run_server(config):
        headers = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert "date" not in headers


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_multiple_server_header(
    ws_protocol_cls, http_protocol_cls, unused_tcp_port: int
):
    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            await self.send(
                {
                    "type": "websocket.accept",
                    "headers": [
                        (b"Server", b"over-ridden"),
                        (b"Server", b"another-value"),
                    ],
                }
            )

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.response_headers

    config = Config(
        app=App,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="off",
        port=unused_tcp_port,
    )
    async with run_server(config):
        headers = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert headers.get_all("Server") == ["uvicorn", "over-ridden", "another-value"]


@pytest.mark.anyio
@pytest.mark.parametrize("ws_protocol_cls", WS_PROTOCOLS)
@pytest.mark.parametrize("http_protocol_cls", HTTP_PROTOCOLS)
async def test_lifespan_state(ws_protocol_cls, http_protocol_cls, unused_tcp_port: int):
    expected_states = [
        {"a": 123, "b": [1]},
        {"a": 123, "b": [1, 2]},
    ]

    actual_states = []

    async def lifespan_app(scope, receive, send):
        message = await receive()
        assert message["type"] == "lifespan.startup"
        scope["state"]["a"] = 123
        scope["state"]["b"] = [1]
        await send({"type": "lifespan.startup.complete"})
        message = await receive()
        assert message["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    class App(WebSocketResponse):
        async def websocket_connect(self, message):
            actual_states.append(deepcopy(self.scope["state"]))
            self.scope["state"]["a"] = 456
            self.scope["state"]["b"].append(2)
            await self.send({"type": "websocket.accept"})

    async def open_connection(url):
        async with websockets.client.connect(url) as websocket:
            return websocket.open

    async def app_wrapper(scope, receive, send):
        if scope["type"] == "lifespan":
            return await lifespan_app(scope, receive, send)
        else:
            return await App(scope, receive, send)

    config = Config(
        app=app_wrapper,
        ws=ws_protocol_cls,
        http=http_protocol_cls,
        lifespan="on",
        port=unused_tcp_port,
    )
    async with run_server(config):
        is_open = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert is_open
        is_open = await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")
        assert is_open

    assert expected_states == actual_states
