import sys, os, time, threading, ssl, traceback
os.chdir(r'F:/haowencheng/OpenOrange')
sys.path.insert(0, os.getcwd())
import fake_server, config
res={}
def runner():
    try:
        fake_server.serve(config.SERVER_PORT)
    except Exception as e:
        res['err']=traceback.format_exc()
t=threading.Thread(target=runner,daemon=True); t.start()
time.sleep(2)
print('err:', res.get('err','none'))
import socket
s=socket.socket(); s.settimeout(2)
try:
    s.connect(('127.0.0.1',8443)); print('connect OK')
except Exception as e:
    print('connect FAIL', e)
