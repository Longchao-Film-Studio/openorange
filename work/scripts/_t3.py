import sys, os, threading, time, ssl, socket
os.chdir(r'F:/haowancheng/OpenOrange'); sys.path.insert(0, os.getcwd())
import fake_server, config
t=threading.Thread(target=fake_server.serve,daemon=True); t.start()
time.sleep(2)
ctx=ssl._create_unverified_context()
try:
    s=ctx.wrap_socket(socket.create_connection(('127.0.0.1',config.SERVER_PORT),timeout=4),server_hostname='localhost')
    print('TLS OK')
    s.sendall(b'POST /api/send_code HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Type: application/json\r\n\r\nhello')
    time.sleep(1)
    print('resp', s.recv(300))
    s.close()
except Exception as e:
    print('FAIL', repr(e))
