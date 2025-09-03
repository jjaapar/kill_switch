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
from pathlib import Path

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

from tenacity import retry, stop_after_attempt, wait_exponential


class DoorSensorDaemon:
    def __init__(self, config_file="/etc/door_sensor/config.ini"):
        self.config = self.load_config(config_file)
        self.logger = self.setup_logging()
        
        self.door_open = False
        self.last_change = datetime.now()
        self.initialized = False
        self.running = False
        self.maintenance_mode = False
        self.state_data = {}
        
        self.load_state()
        self.setup_gpio()
        
        # Handle shutdown gracefully
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def load_config(self, config_file):
        if not os.path.exists(config_file):
            print(f"ERROR: Config file {config_file} not found!")
            sys.exit(1)
        
        config = ConfigParser()
        config.read(config_file)
        
        # Get all the settings we need
        settings = {}
        settings['gpio_pin'] = config.getint('gpio', 'sensor_pin', fallback=17)
        settings['gpio_pull_up'] = config.getboolean('gpio', 'pull_up', fallback=True)
        settings['ssh_host'] = config.get('ssh', 'host')
        settings['ssh_port'] = config.getint('ssh', 'port', fallback=22)
        settings['ssh_key'] = os.path.expanduser(config.get('ssh', 'key_path'))
        settings['ssh_timeout'] = config.getint('ssh', 'timeout', fallback=10)
        settings['cmd_open'] = config.get('commands', 'door_open')
        settings['cmd_closed'] = config.get('commands', 'door_closed')
        settings['alert_delay'] = config.getfloat('monitoring', 'alert_delay', fallback=0.5)
        settings['debounce_delay'] = config.getfloat('monitoring', 'debounce_delay', fallback=1.0)
        
        return settings

    def setup_logging(self):
        # Make sure log directory exists
        os.makedirs("/var/log", exist_ok=True)
        
        logger = logging.getLogger('DoorSensor')
        logger.setLevel(logging.INFO)
        
        # Clear any existing handlers
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # File logging with rotation
        file_handler = RotatingFileHandler("/var/log/door_sensor.log", 
                                         maxBytes=1024*1024, backupCount=5)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        
        # Also log to console
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(console_handler)
        
        return logger

    def setup_gpio(self):
        if GPIO is None:
            self.logger.warning("RPi.GPIO not available - running in test mode")
            return
            
        GPIO.setmode(GPIO.BCM)
        
        if self.config['gpio_pull_up']:
            GPIO.setup(self.config['gpio_pin'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.closed_state = GPIO.LOW
        else:
            GPIO.setup(self.config['gpio_pin'], GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            self.closed_state = GPIO.HIGH

    def load_state(self):
        state_file = "/var/lib/door_sensor/state.json"
        try:
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    self.state_data = json.load(f)
                    self.door_open = self.state_data.get('door_open', False)
                    self.initialized = self.state_data.get('initialized', False)
        except Exception as e:
            self.logger.warning(f"Couldn't load state file: {e}")

    def save_state(self):
        state_file = "/var/lib/door_sensor/state.json"
        self.state_data = {
            'door_open': self.door_open,
            'last_change': self.last_change.isoformat(),
            'initialized': self.initialized
        }
        
        try:
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with open(state_file, 'w') as f:
                json.dump(self.state_data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Couldn't save state: {e}")

    def check_maintenance_mode(self):
        # Check if we're in maintenance mode
        maintenance_file = "/tmp/state"
        old_mode = self.maintenance_mode
        
        try:
            if os.path.exists(maintenance_file):
                with open(maintenance_file, 'r') as f:
                    content = f.read().strip().upper()
                    if content == "MAINT":
                        self.maintenance_mode = True
                    else:
                        self.maintenance_mode = False
            else:
                self.maintenance_mode = False
        except Exception as e:
            self.logger.warning(f"Error checking maintenance mode: {e}")
            self.maintenance_mode = False
        
        # Log if mode changed
        if old_mode != self.maintenance_mode:
            mode_str = "MAINTENANCE" if self.maintenance_mode else "ACTIVE"
            self.logger.info(f"System mode: {mode_str}")

    def read_door_sensor(self):
        if GPIO is None:
            return False
        
        pin_state = GPIO.input(self.config['gpio_pin'])
        return pin_state != self.closed_state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def execute_ssh_command(self, command):
        ssh_cmd = [
            "ssh", 
            "-i", self.config['ssh_key'],
            "-p", str(self.config['ssh_port']),
            "-o", f"ConnectTimeout={self.config['ssh_timeout']}",
            "-o", "BatchMode=yes",
            self.config['ssh_host'],
            command
        ]
        
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, 
                                  timeout=self.config['ssh_timeout'] + 5)
            
            if result.returncode == 0:
                self.logger.info(f"SSH command successful: {command}")
                return True
            else:
                self.logger.error(f"SSH command failed: {result.stderr.strip()}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error("SSH command timed out")
            raise
        except Exception as e:
            self.logger.error(f"SSH error: {e}")
            raise

    def handle_door_event(self, is_open):
        self.door_open = is_open
        self.last_change = datetime.now()
        self.initialized = True
        
        status = "OPEN" if is_open else "CLOSED"
        
        if is_open:
            self.logger.warning(f"Door {status}")
        else:
            self.logger.info(f"Door {status}")
        
        # Check if we should send alerts/run commands
        if self.maintenance_mode:
            print(f"Door {status} (MAINTENANCE MODE)")
            self.logger.info("In maintenance mode - skipping SSH command")
        else:
            print(f"ALERT: Door {status}")
            
            # Run the appropriate command
            command = self.config['cmd_open'] if is_open else self.config['cmd_closed']
            try:
                self.execute_ssh_command(command)
            except Exception as e:
                self.logger.error(f"Failed to execute SSH command: {e}")
        
        self.save_state()

    def check_single_instance(self):
        pid_file = "/var/run/door_sensor.pid"
        
        if os.path.exists(pid_file):
            try:
                with open(pid_file, 'r') as f:
                    pid = int(f.read().strip())
                # Check if process is still running
                os.kill(pid, 0)
                print(f"Another instance is already running (PID {pid})")
                return False
            except (OSError, ValueError):
                # Process doesn't exist, remove stale pid file
                os.remove(pid_file)
        
        # Write our PID
        with open(pid_file, 'w') as f:
            f.write(str(os.getpid()))
        
        return True

    def cleanup(self):
        if GPIO:
            GPIO.cleanup()
        
        self.save_state()
        
        # Remove PID file
        try:
            os.remove("/var/run/door_sensor.pid")
        except OSError:
            pass
        
        self.logger.info("Cleanup completed")

    def shutdown(self, signum, frame):
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def run(self):
        # Make sure we're the only instance
        if not self.check_single_instance():
            sys.exit(1)
        
        self.running = True
        self.logger.info("Door sensor daemon starting...")
        
        last_maintenance_check = datetime.now()
        
        try:
            # Check initial maintenance mode
            self.check_maintenance_mode()
            
            # Get initial door state
            current_state = self.read_door_sensor()
            
            if not self.initialized:
                self.logger.info(f"Initial door state: {'OPEN' if current_state else 'CLOSED'}")
                if current_state:  # Door is open on startup
                    self.handle_door_event(current_state)
                else:
                    self.door_open = current_state
                    self.initialized = True
                    self.save_state()
            
            # Main loop
            while self.running:
                # Check maintenance mode every 30 seconds
                now = datetime.now()
                if now - last_maintenance_check > timedelta(seconds=30):
                    self.check_maintenance_mode()
                    last_maintenance_check = now
                
                # Read door sensor
                current_state = self.read_door_sensor()
                
                # Check for state change with debounce
                if current_state != self.door_open:
                    time_since_change = now - self.last_change
                    if time_since_change.total_seconds() > self.config['debounce_delay']:
                        self.handle_door_event(current_state)
                
                time.sleep(self.config['alert_delay'])
                
        except KeyboardInterrupt:
            self.logger.info("Interrupted by user")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            raise
        finally:
            self.cleanup()


def main():
    try:
        daemon = DoorSensorDaemon()
        daemon.run()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
