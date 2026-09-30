import asyncio
import sys
from collections.abc import Collection


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def _respond(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
    await writer.drain()
    writer.close()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    allowed_hosts: Collection[str],
) -> None:
    request_line = (await reader.readline()).decode(errors="replace").split()
    while await reader.readline() not in (b"\r\n", b"\n", b""):
        pass
    if len(request_line) < 2 or request_line[0] != "CONNECT":
        await _respond(writer, "405 Method Not Allowed")
        return
    host, _, port = request_line[1].rpartition(":")
    if host not in allowed_hosts or port != "443":
        await _respond(writer, "403 Forbidden")
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(host, 443)
    except OSError:
        await _respond(writer, "502 Bad Gateway")
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(
        _pipe(reader, upstream_writer), _pipe(upstream_reader, writer)
    )


async def serve(port: int, allowed_hosts: Collection[str], host: str = "0.0.0.0") -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, allowed_hosts), host, port
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve(int(sys.argv[1]), set(sys.argv[2:])))
