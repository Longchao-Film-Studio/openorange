import socket, ssl, time
time.sleep(1)
s=socket.socket(); s.settimeout(4)
try:
    s.connect(('127.0.0.1',8443)); print('TCP connect OK')
    ctx=ssl._create_unverified_context()
    ss=ctx.wrap_socket(s, server_hostname='localhost')
    print('TLS OK', ss.version())
    ss.close()
except Exception as e:
    print('FAIL', repr(e))
