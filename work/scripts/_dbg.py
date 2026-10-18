import sys, os, threading, time, traceback
os.chdir(r'F:/haowancheng/OpenOrange'); sys.path.insert(0, os.getcwd())
import fake_server, config
errs={}
def runner():
    try: fake_server.serve(config.SERVER_PORT)
    except Exception as e: errs['e']=traceback.format_exc()
t=threading.Thread(target=runner,daemon=True); t.start()
time.sleep(3)
print('ERRS:', errs.get('e','(none)'))
import socket
s=socket.socket(); s.settimeout(3)
try: s.connect(('127.0.0.1',8443)); print('connect OK')
except Exception as e: print('connect FAIL', repr(e))
