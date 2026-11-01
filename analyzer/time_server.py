"""
Answers Latency Cam's clock sync requests with the host's QueryPerformanceCounter, the clock latency-test.exe logs
with, so the phone can map its camera timestamps onto the host's clock.

  python time_server.py [--port 48100]

Request, 16 bytes little endian: magic "MLCS", sequence number, t1 (phone ns).
Reply, 40 bytes little endian: magic "MLCR", sequence number, t1 echoed, t2 (QPC ticks when the request arrived),
t3 (QPC ticks when the reply was sent), QPC ticks per second.

Windows only, because it reads QueryPerformanceCounter. Allow Python through the firewall for UDP when asked.
"""
import argparse
import ctypes
import socket
import struct
import sys
import time

REQUEST = struct.Struct("<IIq")
REPLY = struct.Struct("<IIqqqq")
REQUEST_MAGIC = 0x53434C4D  # "MLCS"
REPLY_MAGIC = 0x52434C4D  # "MLCR"


def qpc_reader():
    """A function returning the current QueryPerformanceCounter value, and the counter's frequency."""
    kernel32 = ctypes.windll.kernel32
    value = ctypes.c_int64()
    frequency = ctypes.c_int64()
    kernel32.QueryPerformanceFrequency(ctypes.byref(frequency))
    query = kernel32.QueryPerformanceCounter
    ref = ctypes.byref(value)

    def now():
        query(ref)
        return value.value

    return now, frequency.value


def answer(data, t2, t3_reader, frequency):
    """The reply to one request, or None if it isn't one."""
    if len(data) < REQUEST.size:
        return None
    magic, sequence, t1 = REQUEST.unpack_from(data)
    if magic != REQUEST_MAGIC:
        return None
    return REPLY.pack(REPLY_MAGIC, sequence, t1, t2, t3_reader(), frequency)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=48100)
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("time_server.py needs Windows' QueryPerformanceCounter")

    now, frequency = qpc_reader()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", args.port))
    print(f"Answering clock sync requests on UDP port {args.port} (QPC at {frequency} Hz). Ctrl+C to stop.")
    clients = {}
    last_report = time.monotonic()
    try:
        while True:
            data, address = sock.recvfrom(64)
            t2 = now()
            reply = answer(data, t2, now, frequency)
            if reply is None:
                continue
            sock.sendto(reply, address)
            if address[0] not in clients:
                print(f"Phone {address[0]} connected")
            clients[address[0]] = clients.get(address[0], 0) + 1
            if time.monotonic() - last_report > 10:
                last_report = time.monotonic()
                print("  " + ", ".join(f"{ip}: {count} requests" for ip, count in clients.items()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
