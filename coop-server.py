#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VOXEL ZOMBIE —— 局域网合作服务器（零依赖，Python 3.8+）

用法:
    python coop-server.py [端口]        # 默认端口 8770

所有玩家用浏览器打开  http://<主机IP>:端口/
  · 第一点「局域网 · 创建队伍」的玩家成为主机（权威模拟端）
  · 其他玩家点「局域网 · 加入队伍」进入同一战局

服务器本身不做游戏逻辑，只做:
  1. 静态托管 index.html（同源，避免跨域问题）
  2. WebSocket 消息转发（主机 <-> 所有队友）
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'text/javascript; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.svg':  'image/svg+xml',
    '.ico':  'image/x-icon',
    '.txt':  'text/plain; charset=utf-8',
    '.md':   'text/plain; charset=utf-8',
}

# ---------------- 房间状态 ----------------
room_lock = threading.Lock()
room = {'host': None, 'clients': {}}      # clients: cid -> Conn
next_cid = 1


def get_lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        try:
            s.connect(('8.8.8.8', 80))
            ips.append(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    except OSError:
        pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except OSError:
        pass
    return ips or ['127.0.0.1']


# ---------------- WebSocket 帧编解码 ----------------
class WsError(Exception):
    pass


def ws_read_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WsError('closed')
        buf += chunk
    return buf


def ws_recv_message(sock):
    """返回 (opcode, payload bytes)。自动处理分片与 ping。"""
    opcode_acc = None
    payload_acc = b''
    while True:
        b1, b2 = ws_read_exact(sock, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack('>H', ws_read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack('>Q', ws_read_exact(sock, 8))[0]
        mask = ws_read_exact(sock, 4) if masked else None
        data = ws_read_exact(sock, length) if length else b''
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        if opcode == 0x9:            # ping -> pong
            ws_send_frame(sock, 0xA, data)
            continue
        if opcode == 0xA:            # pong
            continue
        if opcode == 0x8:            # close
            try:
                ws_send_frame(sock, 0x8, data[:2])
            except OSError:
                pass
            raise WsError('close')

        if opcode in (0x1, 0x2):
            opcode_acc = opcode
            payload_acc = data
        elif opcode == 0x0:          # continuation
            payload_acc += data
        else:
            continue                 # 未知 opcode 忽略

        if fin:
            return opcode_acc, payload_acc


def ws_send_frame(sock, opcode, payload=b''):
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack('>H', n)
    else:
        header += bytes([127]) + struct.pack('>Q', n)
    sock.sendall(header + payload)


def ws_send_text(sock, text):
    ws_send_frame(sock, 0x1, text.encode('utf-8'))


# ---------------- 房间路由 ----------------
def route_text(conn, text):
    try:
        m = json.loads(text)
    except ValueError:
        return
    if not isinstance(m, dict):
        return
    t = m.get('t')

    with room_lock:
        if t == 'host':
            if room['host'] is not None and room['host'] is not conn:
                ws_send_text(conn.sock, json.dumps({'t': 's', 'k': 'full'}))
                return
            room['host'] = conn
            conn.role = 'host'
            ws_send_text(conn.sock, json.dumps({'t': 'role', 'r': 'host'}))
            print('[房间] 主机已就位')
            return

        if t == 'join':
            global next_cid
            if room['host'] is None:
                print(f'[房间] join 被拒：当前无主机（conn.role={conn.role}）')
                ws_send_text(conn.sock, json.dumps({'t': 's', 'k': 'nohost'}))
                return
            conn.role = 'client'
            conn.cid = str(next_cid)
            next_cid += 1
            room['clients'][conn.cid] = conn
            ws_send_text(conn.sock, json.dumps({'t': 'role', 'r': 'client', 'id': conn.cid}))
            # 通知主机有新队友
            try:
                ws_send_text(room['host'].sock, json.dumps(
                    {'t': 'c', 'id': conn.cid, 'm': {'t': 'join', 'name': m.get('name', '')}}))
            except OSError:
                pass
            print(f'[房间] 队友 {conn.cid} 加入（当前 {len(room["clients"])} 人）')
            return

        if conn.role == 'client' and room['host'] is not None:
            # 客户端 -> 主机（附带来源 id）
            try:
                ws_send_text(room['host'].sock, json.dumps({'t': 'c', 'id': conn.cid, 'm': m}))
            except OSError:
                pass
            return

        if conn.role == 'client':
            print(f'[警告] client {conn.cid} 的消息被丢弃（无主机）：t={t}')

        if conn.role == 'host':
            to = m.pop('to', None)
            if to:
                inner = m.pop('m', m)          # 定向转发：剥掉 {t:'c'} 包装发内层
                if isinstance(inner, dict) and inner:
                    c = room['clients'].get(str(to))
                    if c is not None:
                        try:
                            ws_send_text(c.sock, json.dumps(inner))
                        except OSError:
                            pass
            else:
                payload = json.dumps(m)
                dead = []
                for cid, c in room['clients'].items():
                    try:
                        ws_send_text(c.sock, payload)
                    except OSError:
                        dead.append(cid)
                for cid in dead:
                    room['clients'].pop(cid, None)


def detach(conn):
    with room_lock:
        if conn.role == 'host' and room['host'] is conn:
            room['host'] = None
            print('[房间] 主机离开，解散战局')
            dead = []
            for cid, c in room['clients'].items():
                try:
                    ws_send_text(c.sock, json.dumps({'t': 's', 'k': 'hostGone'}))
                except OSError:
                    dead.append(cid)
            for cid in dead:
                room['clients'].pop(cid, None)
            room['clients'].clear()
        elif conn.role == 'client':
            room['clients'].pop(conn.cid, None)
            host = room['host']
            if host is not None:
                try:
                    ws_send_text(host.sock, json.dumps(
                        {'t': 'c', 'id': conn.cid, 'm': {'t': 'bye'}}))
                except OSError:
                    pass
            print(f'[房间] 队友 {conn.cid} 离开（剩余 {len(room["clients"])} 人）')
        conn.role = None


# ---------------- 连接处理 ----------------
def handle_conn(sock, addr):
    sock.settimeout(30)
    conn = type('Conn', (), {'sock': sock, 'role': None, 'cid': None})()

    # ---- 读 HTTP 请求头 ----
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            return
        head += chunk
        if len(head) > 65536:
            sock.close()
            return
    head_part, _, rest = head.partition(b'\r\n\r\n')
    lines = head_part.decode('latin-1', 'replace').split('\r\n')
    try:
        method, path, _ver = lines[0].split(' ', 2)
    except ValueError:
        sock.close()
        return
    headers = {}
    for ln in lines[1:]:
        if ':' in ln:
            k, v = ln.split(':', 1)
            headers[k.strip().lower()] = v.strip()

    # ---- WebSocket 升级 ----
    if headers.get('upgrade', '').lower() == 'websocket':
        key = headers.get('sec-websocket-key', '')
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        sock.sendall(
            ('HTTP/1.1 101 Switching Protocols\r\n'
             'Upgrade: websocket\r\n'
             'Connection: Upgrade\r\n'
             f'Sec-WebSocket-Accept: {accept}\r\n\r\n').encode())
        sock.settimeout(75)          # 75s 无任何消息（含心跳）视为死连接，自动清理
        print(f'[连接] {addr[0]} WebSocket 建立')
        try:
            while True:
                op, data = ws_recv_message(sock)
                if op == 0x1:
                    route_text(conn, data.decode('utf-8', 'replace'))
                # 二进制帧忽略
        except (WsError, OSError, socket.timeout) as e:
            print(f'[断开] {addr[0]} reason={type(e).__name__}: {e}')
        finally:
            detach(conn)
            try:
                sock.close()
            except OSError:
                pass
        return

    # ---- 普通 HTTP（静态文件 / info）----
    if method not in ('GET', 'HEAD'):
        sock.sendall(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
        sock.close()
        return

    clean = path.split('?', 1)[0]
    if clean == '/info':
        body = json.dumps({'ips': get_lan_ips(), 'port': PORT}).encode()
        sock.sendall(
            ('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
             f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
        sock.close()
        return

    if clean in ('/', '/index.html'):
        fs_path = os.path.join(BASE_DIR, 'index.html')
    else:
        fs_path = os.path.normpath(os.path.join(BASE_DIR, clean.lstrip('/')))
        if not fs_path.startswith(BASE_DIR):
            sock.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            sock.close()
            return

    if os.path.isfile(fs_path):
        with open(fs_path, 'rb') as f:
            body = f.read()
        ext = os.path.splitext(fs_path)[1].lower()
        ctype = MIME.get(ext, 'application/octet-stream')
        sock.sendall(
            ('HTTP/1.1 200 OK\r\n'
             f'Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n'
             'Cache-Control: no-cache\r\n\r\n').encode()
            + (body if method == 'GET' else b''))
    else:
        sock.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')
    sock.close()


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', PORT))
    server.listen(16)
    ips = get_lan_ips()
    print('=' * 56)
    print('  VOXEL ZOMBIE 局域网合作服务器已启动')
    print(f'  端口: {PORT}')
    print('  本机地址（把下面任意一个发给同一 Wi-Fi 的队友）:')
    for ip in ips:
        print(f'    http://{ip}:{PORT}/')
    print('  主机点「创建队伍」，队友点「加入队伍」')
    print('  Ctrl+C 停止')
    print('=' * 56)
    try:
        while True:
            sock, addr = server.accept()
            threading.Thread(target=handle_conn, args=(sock, addr), daemon=True).start()
    except KeyboardInterrupt:
        print('\n服务器已停止')
    finally:
        server.close()


if __name__ == '__main__':
    main()
