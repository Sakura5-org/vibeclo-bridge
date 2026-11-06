"""CLO 2 Bonjour advert (_clo2._tcp): up while the bridge runs, gone within seconds after it dies — even SIGKILL —
so no ghost Mac stays in the app's Wi-Fi list. Throwaway bridge."""
import os, signal, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, check, done

b = Bridge(7804, "bonjour", {"label": "Bonjour Test Mac"}, extra_env={"CLO2_NO_BONJOUR": ""})
time.sleep(3)
up = lambda: "Bonjour Test Mac" in subprocess.run(["pgrep", "-fl", "dns-sd -R"], capture_output=True, text=True).stdout
check("advertised while running", up())
browse = subprocess.Popen(["dns-sd", "-B", "_clo2._tcp"], stdout=subprocess.PIPE, text=True)
time.sleep(2); browse.terminate()
check("visible to a Bonjour browse", "Bonjour Test Mac" in browse.communicate()[0])
os.kill(b.proc.pid, signal.SIGKILL)
time.sleep(6)
check("gone 6 s after the bridge is killed", not up())
done()
