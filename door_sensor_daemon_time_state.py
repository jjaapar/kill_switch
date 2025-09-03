#!/usr/bin/env python3
import json, logging, os, signal, subprocess, sys, time
from pathlib import Path
from configparser import ConfigParser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None


CONFIG_FILE = Path("/etc/door_sensor/config.ini")
STATE_FILE = Path("/var/lib/door_sensor/state.json")
PID_FILE   = Path("/var/run/door_sensor.pid")
LOG_FILE   = Path("/var/log/door_sensor.log")
MAINT_FILE = Path("/tmp/state")


class DoorMonitor:
    def __init__(self):
        self.config = self.read_config()
        self.log = self.setup_logs()

        self.door_is_open = False
        self.door_opened_at = None
        self.already_alerted = False
        self.is_initialized = False
        self.keep_running = True
        self.maintenance_mode = False

        self.load_state()
        self.init_gpio()

        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def read_config(self):
        if not CONFIG_FILE.exists():
            sys.exit(f"Error: Missing {CONFIG_FILE}")
        cfg = ConfigParser()
        cfg.read(CONFIG_FILE)

        return {
            "pin": cfg.getint("gpio", "sensor_pin", fallback=17),
            "pull_up": cfg.getboolean("gpio", "pull_up", fallback=True),
            "host": cfg.get("ssh", "host"),
            "port": cfg.getint("ssh", "port", fallback=22),
            "key_file": os.path.expanduser(cfg.get("ssh", "key_path")),
            "timeout": cfg.getint("ssh", "timeout", fallback=10),
            "open_cmd": cfg.get("commands", "door_open"),
            "close_cmd": cfg.get("commands", "door_closed"),
            "check_delay": cfg.getfloat("monitoring", "alert_delay", fallback=0.5),
            "debounce": cfg.getfloat("monitoring", "debounce_delay", fallback=1.0),
            "maintenance_check_interval": cfg.getint("monitoring", "maintenance_check_interval", fallback=5),
        }

    def setup_logs(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(message)s",
            handlers=[
                RotatingFileHandler(LOG_FILE, maxBytes=1_048_576, backupCount=3),
                logging.StreamHandler(sys.stdout)
            ],
        )
        return logging.getLogger("DoorMonitor")

    def init_gpio(self):
        if not GPIO:
            self.log.warning("No GPIO library, test mode only")
            return
        GPIO.setmode(GPIO.BCM)
        pull = GPIO.PUD_UP if self.config["pull_up"] else GPIO.PUD_DOWN
        GPIO.setup(self.config["pin"], GPIO.IN, pull_up_down=pull)
        self.door_closed_value = GPIO.LOW if self.config["pull_up"] else GPIO.HIGH

    def load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.door_is_open = data.get("door_is_open", False)
                self.is_initialized = data.get("is_initialized", False)
                self.already_alerted = data.get("already_alerted", False)
                if data.get("door_opened_at"):
                    self.door_opened_at = datetime.fromisoformat(data["door_opened_at"])
            except Exception as e:
                self.log.warning(f"Couldn't load state: {e}")

    def save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "door_is_open": self.door_is_open,
            "is_initialized": self.is_initialized,
            "already_alerted": self.already_alerted,
            "door_opened_at": self.door_opened_at.isoformat() if self.door_opened_at else None,
            "last_saved": datetime.now().isoformat(),
        }
        try:
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self.log.error(f"Couldn't save state: {e}")

    def check_maintenance(self):
        prev = self.maintenance_mode
        self.maintenance_mode = MAINT_FILE.exists() and MAINT_FILE.read_text().strip().upper() == "MAINT"
        if prev != self.maintenance_mode:
            self.log.info("Mode changed to: %s", "MAINTENANCE" if self.maintenance_mode else "ACTIVE")

    def read_door_state(self):
        return GPIO and GPIO.input(self.config["pin"]) != self.door_closed_value

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
    def run_ssh(self, cmd):
        ssh_cmd = [
            "ssh", "-i", self.config["key_file"],
            "-p", str(self.config["port"]),
            "-o", f"ConnectTimeout={self.config['timeout']}",
            "-o", "BatchMode=yes",
            self.config["host"], cmd
        ]
        try:
            r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=self.config["timeout"]+5)
            if r.returncode == 0:
                self.log.info("SSH ok: %s", cmd)
                return True
            self.log.error("SSH failed: %s", r.stderr.strip())
            return False
        except Exception as e:
            self.log.error(f"SSH error: {e}")
            raise

    def door_state_changed(self, open_now):
        self.door_is_open, self.is_initialized = open_now, True
        self.already_alerted = False
        self.door_opened_at = datetime.now() if open_now else None
        self.log.info("Door %s", "opened" if open_now else "closed")

        if not open_now:  # send close immediately
            try:
                if not self.maintenance_mode:
                    self.run_ssh(self.config["close_cmd"])
            except Exception as e:
                self.log.error(f"Close failed: {e}")
        self.save_state()

    def check_open_timer(self):
        if self.door_is_open and self.door_opened_at and not self.already_alerted:
            if (datetime.now() - self.door_opened_at).total_seconds() >= 10:
                self.log.warning("Door open 10+ seconds")
                try:
                    if not self.maintenance_mode:
                        self.run_ssh(self.config["open_cmd"])
                except Exception as e:
                    self.log.error(f"Open failed: {e}")
                self.already_alerted = True
                self.save_state()

    def ensure_single_instance(self):
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text())
                os.kill(pid, 0)
                print(f"Already running with PID {pid}")
                return False
            except:
                PID_FILE.unlink(missing_ok=True)
        PID_FILE.write_text(str(os.getpid()))
        return True

    def cleanup(self):
        GPIO and GPIO.cleanup()
        self.save_state()
        PID_FILE.unlink(missing_ok=True)
        self.log.info("Exiting cleanly")

    def stop(self, *_):
        self.keep_running = False

    def start(self):
        if not self.ensure_single_instance():
            sys.exit(1)

        self.log.info("Door monitor starting up")
        self.check_maintenance()
        if not self.is_initialized:
            self.door_state_changed(self.read_door_state())

        last_maint_check = datetime.now()
        last_state_change = datetime.now()

        try:
            while self.keep_running:
                now = datetime.now()
                if (now - last_maint_check).total_seconds() > self.config["maintenance_check_interval"]:
                    self.check_maintenance()
                    last_maint_check = now

                self.check_open_timer()

                current = self.read_door_state()
                if current != self.door_is_open and (now - last_state_change).total_seconds() > self.config["debounce"]:
                    self.door_state_changed(current)
                    last_state_change = now

                time.sleep(self.config["check_delay"])
        finally:
            self.cleanup()


def main():
    try:
        DoorMonitor().start()
    except Exception as e:
        sys.exit(f"Fatal error: {e}")


if __name__ == "__main__":
    main()
