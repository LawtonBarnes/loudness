#!/usr/bin/env python3
"""LOUDNESS shared mic server -- runs on MP (micserver.service).

One USB mic for the whole fleet: this owns the mic (ALSA lets only one
program open it), captures mono S16_LE at 44.1 kHz via `arecord`, and
streams it by unicast UDP to every LOUDNESS that has asked for it
(settings.ini `device = net:mp`, see loudness.py's NetCapture).

Protocol (UDP port 8440):
  client -> server  b"LOUD" every ~1 s while it wants audio (heartbeat).
                    Subscribers not heard from for SUBSCRIBER_TTL are dropped.
  server -> client  b"LDN1" + uint32 LE sequence number + PACKET_SAMPLES
                    S16_LE samples (1032 bytes, well under the MTU).

Unicast, not multicast, for reliability on the Deco mesh WiFi. About
0.7 Mbps per listener. A lost packet just skips a little audio.

Usage: micserver.py [ALSA_DEVICE]   (default plughw:CMTECK,0, the gooseneck,
addressed by card name so it can't swap with the remote receiver's card)
"""
import select
import socket
import struct
import subprocess
import sys
import time

PORT = 8440
SAMPLE_RATE = 44100
PACKET_SAMPLES = 512  # ~11.6 ms per packet
MAGIC = b"LDN1"
HELLO = b"LOUD"
SUBSCRIBER_TTL = 5.0


def main():
    device = sys.argv[1] if len(sys.argv) > 1 else "plughw:CMTECK,0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", PORT))
    sock.setblocking(False)

    proc = subprocess.Popen(
        ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
         "-t", "raw", "--period-size", str(PACKET_SAMPLES), "--buffer-size", str(PACKET_SAMPLES * 4)],
        stdout=subprocess.PIPE,
    )
    packet_bytes = PACKET_SAMPLES * 2
    subscribers = {}  # (ip, port) -> last heartbeat time
    seq = 0
    print(f"micserver: {device} -> UDP {PORT}", flush=True)
    try:
        while True:
            data = proc.stdout.read(packet_bytes)
            if len(data) < packet_bytes:
                sys.exit("arecord exited -- is the mic plugged in?")  # systemd restarts us
            now = time.monotonic()
            while select.select([sock], [], [], 0)[0]:
                try:
                    msg, addr = sock.recvfrom(64)
                except OSError:
                    break
                if msg.startswith(HELLO):
                    if addr not in subscribers:
                        print(f"micserver: + {addr[0]}:{addr[1]}", flush=True)
                    subscribers[addr] = now
            for addr in [a for a, t in subscribers.items() if now - t > SUBSCRIBER_TTL]:
                print(f"micserver: - {addr[0]}:{addr[1]}", flush=True)
                del subscribers[addr]
            if subscribers:
                packet = MAGIC + struct.pack("<I", seq) + data
                for addr in subscribers:
                    try:
                        sock.sendto(packet, addr)
                    except OSError:
                        pass  # a host dropping off WiFi mustn't stall the others
            seq = (seq + 1) & 0xFFFFFFFF
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
