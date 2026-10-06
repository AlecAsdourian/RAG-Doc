"""Pick three free loopback ports for the live proof's processes."""
import pathlib
import socket

LIVE = pathlib.Path(r"C:\Users\Alec\AppData\Local\Temp\rag2205-ScfByQaa\live")
ports = []
sockets = []
for _ in range(3):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    sockets.append(s)
    ports.append(s.getsockname()[1])
for s in sockets:
    s.close()
assert 5434 not in ports and 8080 not in ports
names = ("INTERNAL_PORT", "PUBLIC_PORT", "RAG_PORT")
(LIVE / "ports.env").write_text("".join(f"{n}={p}\n" for n, p in zip(names, ports)), encoding="utf-8")
print(dict(zip(names, ports)))
