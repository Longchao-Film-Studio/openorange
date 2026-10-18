import sys, os, threading, time, ssl, socket
os.chdir(r'F:/haowancheng/OpenOrange'); sys.path.insert(0, os.getcwd())
import fake_server, config
threading.Thread(target=fake_server.serve,daemon=True).start()
def tryconn(n=5):
    for i in range(n):
        try:
            return ssl._create_unverified_context().wrap_socket(socket.create_connection(('127.0.0.1',config.SERVER_PORT),timeout=2),server_hostname='localhost')
        except Exception: time.sleep(1)
    return None
s=tryconn()
if s is None: print('NO CONNECT'); sys.exit(1)
print('TLS OK')
s.sendall(b'POST /api/send_code HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Type: application/json\r\n\r\nhello')
time.sleep(1)
data=b''
s.settimeout(3)
try:
    while True:
        b=s.recv(400)
        if not b: break
        data+=b
except Exception: pass
print('RESP:', data[:200])
