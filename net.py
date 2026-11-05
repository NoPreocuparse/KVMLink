"""Canal TCP cifrado (ChaCha20-Poly1305 con clave derivada de contraseña) y descubrimiento LAN."""
import hashlib
import hmac
import json
import os
import socket
import struct
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MAGIC = b"KVMLINK1"
DEFAULT_PORT = 24800
DISCOVERY_PORT = 24801
MAX_FRAME = 4 * 1024 * 1024


class AuthError(Exception):
    pass


def derive_master_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"kvmlink-v1", 200_000, 32)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexión cerrada")
        buf += chunk
    return bytes(buf)


class SecureChannel:
    """Mensajes JSON enmarcados, cifrados y autenticados. Envío thread-safe."""

    def __init__(self, sock: socket.socket, master_key: bytes, is_server: bool):
        self.sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(10)
        my_nonce = os.urandom(16)
        sock.sendall(MAGIC + my_nonce)
        peer = _recv_exact(sock, len(MAGIC) + 16)
        if peer[: len(MAGIC)] != MAGIC:
            raise AuthError("el otro extremo no es kvmlink (o versión distinta)")
        peer_nonce = peer[len(MAGIC):]
        cn, sn = (peer_nonce, my_nonce) if is_server else (my_nonce, peer_nonce)
        k_c2s = hmac.new(master_key, b"c2s" + cn + sn, hashlib.sha256).digest()
        k_s2c = hmac.new(master_key, b"s2c" + cn + sn, hashlib.sha256).digest()
        send_key, recv_key = (k_s2c, k_c2s) if is_server else (k_c2s, k_s2c)
        self._tx = ChaCha20Poly1305(send_key)
        self._rx = ChaCha20Poly1305(recv_key)
        self._tx_ctr = 0
        self._rx_ctr = 0
        self._send_lock = threading.Lock()
        self.closed = False

    def send(self, msg: dict):
        self.send_many((msg,))

    def send_many(self, msgs):
        """Cifra cada mensaje en su propia trama (el protocolo no cambia) pero las
        manda todas con una sola llamada al socket."""
        frames = []
        with self._send_lock:
            for msg in msgs:
                data = json.dumps(msg, separators=(",", ":")).encode()
                nonce = b"\0\0\0\0" + struct.pack(">Q", self._tx_ctr)
                self._tx_ctr += 1
                ct = self._tx.encrypt(nonce, data, None)
                frames.append(struct.pack(">I", len(ct)))
                frames.append(ct)
            if frames:
                self.sock.sendall(b"".join(frames))

    def recv(self) -> dict:
        (n,) = struct.unpack(">I", _recv_exact(self.sock, 4))
        if n > MAX_FRAME:
            raise ConnectionError("trama demasiado grande")
        ct = _recv_exact(self.sock, n)
        nonce = b"\0\0\0\0" + struct.pack(">Q", self._rx_ctr)
        self._rx_ctr += 1
        try:
            data = self._rx.decrypt(nonce, ct, None)
        except InvalidTag:
            raise AuthError("contraseña incorrecta o datos manipulados")
        return json.loads(data)

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def beacon_forever(tcp_port: int, stop: threading.Event):
    """El lado que escucha anuncia su presencia por broadcast UDP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = MAGIC + struct.pack(">H", tcp_port) + socket.gethostname().encode()[:64]
    while not stop.is_set():
        try:
            s.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass
        stop.wait(1.0)


def discover(timeout: float = 30.0):
    """Espera un beacon y devuelve (ip, puerto, nombre_host)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", DISCOVERY_PORT))
    s.settimeout(1.0)
    end = time.time() + timeout
    try:
        while time.time() < end:
            try:
                data, (ip, _) = s.recvfrom(256)
            except socket.timeout:
                continue
            if data.startswith(MAGIC) and len(data) >= len(MAGIC) + 2:
                (port,) = struct.unpack(">H", data[len(MAGIC): len(MAGIC) + 2])
                return ip, port, data[len(MAGIC) + 2:].decode(errors="replace")
    finally:
        s.close()
    return None
