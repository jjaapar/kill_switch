#!/usr/bin/env python3

import json
import logging
import os
import signal
import subprocess
import sys
import time
from configparser import ConfigParser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

from tenacity import retry, stop_after_attempt, wait_exponential


class DoorMonitor:
    def __init__(self):
        # Load settings
        self.config = self.read_config()
        self.setup_logs()
        
        # Door state tracking
        self.door_is_open = False
        self.door_opened_at = None
        self.already_alerted = False
        self.is_initialized = False
        self.keep_running = True
        self.maintenance_mode = False
        
        # Load saved state
        self.load_saved_state()
        self.init_gpio()
        
        # Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self.stop_daemon)
        signal.signal(signal.SIGTERM, self.stop_daemon)

    def read_config(self):
        config_file = "/etc/door_sensor/config.ini"
        if not os.path.exists(config_file):
            print(f"Error: Can't find config file {config_file}")
            sys.exit(1)
        
        cfg = ConfigParser()
        cfg.read(config_file)
        
        return {
            'pin': cfg.getint('gpio', 'sensor_pin', fallback=17),
            'pull_up': cfg.getboolean('gpio', 'pull_up', fallback=True),
            'host': cfg.get('ssh', 'host'),
            'port': cfg.getint('ssh', 'port', fallback=22),
            'key_file': os.path.expanduser(cfg.get('ssh', 'key_path')),
            'timeout': cfg.getint('ssh', 'timeout', fallback=10),
            'open_cmd': cfg.get('commands', 'door_open'),
            'close_cmd': cfg.get('commands', 'door_closed'),
            'check_delay': cfg.getfloat('monitoring', 'alert_delay', fallback=0.5),
            'debounce': cfg.getfloat('monitoring', 'debounce_delay', fallback=1.0)
        }

    def setup_logs(self):
        os.makedirs("/var/log", exist_ok=True)
        
        self.log = logging.getLogger('DoorMonitor')
        self.log.setLevel(logging.INFO)
        
        # Clear old handlers
        self.log.handlers = []
        
        # Log to file with rotation
        file_log = RotatingFileHandler("/var/log/door_sensor.log", maxBytes=1024*1024, backupCount=3)
        file_log.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        self.log.addHandler(file_log)
        
        # Also show on screen
        screen_log = logging.StreamHandler()
        screen_log.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        self.log.addHandler(screen_log)

    def init_gpio(self):
        if not GPIO:
            self.log.warning("No GPIO library - running in test mode")
            return
        
        GPIO.setmode(GPIO.BCM)
        
        if self.config['pull_up']:
            GPIO.setup(self.config['pin'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.door_closed_value = GPIO.LOW
        else:
            GPIO.setup(self.config['pin'], GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            self.door_closed_value = GPIO.HIGH

    def load_saved_state(self):
        state_file = "/var/lib/door_sensor/state.json"
        try:
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    data = json.load(f)
                    self.door_is_open = data.get('door_is_open', False)
                    self.is_initialized = data.get('is_initialized', False)
                    self.already_alerted = data.get('already_alerted', False)
                    
                    # Restore timer if door was open
                    if data.get('door_opened_at'):
                        self.door_opened_at = datetime.fromisoformat(data['door_opened_at'])
        except Exception as e:
            self.log.warning(f"Couldn't load saved state: {e}")

    def save_state(self):
        state_file = "/var/lib/door_sensor/state.json"
        data = {
            'door_is_open': self.door_is_open,
            'is_initialized': self.is_initialized,
            'already_alerted': self.already_alerted,
            'door_opened_at': self.door_opened_at.isoformat() if self.door_opened_at else None,
            'last_saved': datetime.now().isoformat()
        }
        
        try:
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with open(state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log.error(f"Couldn't save state: {e}")

    def check_if_maintenance_mode(self):
        # Check /tmp/state file for ACTIVE or MAINT
        state_file = "/tmp/state"
        old_mode = self.maintenance_mode
        
        try:
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    mode = f.read().strip().upper()
                    self.maintenance_mode = (mode == "MAINT")
            else:
                self.maintenance_mode = False
        except:
            self.maintenance_mode = False
        
        if old_mode != self.maintenance_mode:
            status = "MAINTENANCE" if self.maintenance_mode else "ACTIVE"
            self.log.info(f"Mode changed to: {status}")

    def read_door_state(self):
        if not GPIO:
            return False  # Test mode
        
        pin_value = GPIO.input(self.config['pin'])
        return pin_value != self.door_closed_value

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def run_ssh_command(self, cmd):
        ssh_command = [
            "ssh", "-i", self.config['key_file'],
            "-p", str(self.config['port']),
            "-o", f"ConnectTimeout={self.config['timeout']}",
            "-o", "BatchMode=yes",
            self.config['host'], cmd
        ]
        
        try:
            result = subprocess.run(ssh_command, capture_output=True, text=True, 
                                  timeout=self.config['timeout'] + 5)
            
            if result.returncode == 0:
                self.log.info(f"SSH command worked: {cmd}")
                return True
            else:
                self.log.error(f"SSH failed: {result.stderr.strip()}")
                return False
        except Exception as e:
            self.log.error(f"SSH error: {e}")
            raise

    def door_state_changed(self, door_open_now):
        self.door_is_open = door_open_now
        self.is_initialized = True
        
        if door_open_now:
            # Door just opened
            self.door_opened_at = datetime.now()
            self.already_alerted = False
            self.log.info("Door opened - starting 10 second timer")
            print("Door OPEN - waiting 10 seconds before sending alert...")
        else:
            # Door closed
            self.door_opened_at = None
            self.already_alerted = False
            self.log.info("Door closed")
            
            # Send close alert immediately (no waiting)
            if self.maintenance_mode:
                print("Door CLOSED (maintenance mode)")
            else:
                print("ALERT: Door CLOSED")
                try:
                    self.run_ssh_command(self.config['close_cmd'])
                except Exception as e:
                    self.log.error(f"Close command failed: {e}")
        
        self.save_state()

    def check_open_door_timer(self):
        # Only check if door is open and we haven't sent alert yet
        if not self.door_is_open or not self.door_opened_at or self.already_alerted:
            return
        
        # How long has door been open?
        time_open = datetime.now() - self.door_opened_at
        
        if time_open.total_seconds() >= 10:
            self.log.warning("Door open for 10+ seconds")
            
            if self.maintenance_mode:
                print("Door has been open 10+ seconds (maintenance mode)")
            else:
                print("ALERT: Door has been open for 10+ seconds!")
                try:
                    self.run_ssh_command(self.config['open_cmd'])
                except Exception as e:
                    self.log.error(f"Open command failed: {e}")
            
            self.already_alerted = True
            self.save_state()

    def ensure_single_instance(self):
        pid_file = "/var/run/door_sensor.pid"
        
        # Check if already running
        if os.path.exists(pid_file):
            try:
                with open(pid_file, 'r') as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)  # Check if process exists
                print(f"Already running with PID {old_pid}")
                return False
            except:
                # Old process died, remove stale file
                os.remove(pid_file)
        
        # Write our PID
        with open(pid_file, 'w') as f:
            f.write(str(os.getpid()))
        
        return True

    def cleanup_and_exit(self):
        if GPIO:
            GPIO.cleanup()
        
        self.save_state()
        
        # Remove PID file
        try:
            os.remove("/var/run/door_sensor.pid")
        except:
            pass
        
        self.log.info("Exiting cleanly")

    def stop_daemon(self, signal_num, frame):
        self.log.info(f"Got signal {signal_num}, stopping...")
        self.keep_running = False

    def start_monitoring(self):
        # Make sure only one copy is running
        if not self.ensure_single_instance():
            sys.exit(1)
        
        self.log.info("Door monitor starting up...")
        
        # Track when we last checked maintenance mode
        last_maintenance_check = datetime.now()
        last_state_change = datetime.now()
        
        try:
            # Check initial mode
            self.check_if_maintenance_mode()
            
            # Get current door state
            door_open_now = self.read_door_state()
            
            # Handle startup state
            if not self.is_initialized:
                status = "OPEN" if door_open_now else "CLOSED"
                self.log.info(f"Starting up - door is {status}")
                
                if door_open_now:
                    # Door open at startup
                    self.door_state_changed(door_open_now)
                else:
                    # Door closed at startup
                    self.door_is_open = door_open_now
                    self.is_initialized = True
                    self.save_state()
            
            # Main monitoring loop
            while self.keep_running:
                now = datetime.now()
                
                # Check maintenance mode at configured interval
                if (now - last_maintenance_check).total_seconds() > self.config['maintenance_check_interval']:
                    self.check_if_maintenance_mode()
                    last_maintenance_check = now
                
                # If door is open, check if we need to send alert
                if self.door_is_open:
                    self.check_open_door_timer()
                
                # Check for door state changes
                current_door_state = self.read_door_state()
                
                if current_door_state != self.door_is_open:
                    # State changed - but wait for debounce
                    time_since_change = (now - last_state_change).total_seconds()
                    if time_since_change > self.config['debounce']:
                        self.door_state_changed(current_door_state)
                        last_state_change = now
                
                time.sleep(self.config['check_delay'])
                
        except KeyboardInterrupt:
            self.log.info("Stopped by user")
        except Exception as e:
            self.log.error(f"Unexpected error: {e}")
            raise
        finally:
            self.cleanup_and_exit()


def main():
    try:
        monitor = DoorMonitor()
        monitor.start_monitoring()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
