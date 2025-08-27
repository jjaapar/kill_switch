#!/usr/bin/env python3
"""
Door Sensor Daemon
A secure, robust, and efficient door monitoring system for Raspberry Pi that triggers remote actions via SSH.
"""

import RPi.GPIO as GPIO
import time
import logging
import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from configparser import ConfigParser
from tenacity import retry, stop_after_attempt, wait_exponential
import signal
import json

# Default Configuration Paths
CONFIG_FILE = "/etc/door_sensor/config.ini"
STATE_FILE = "/var/lib/door_sensor/state.json"
LOG_FILE = "/var/log/door_sensor.log"
PID_FILE = "/var/run/door_sensor.pid"

class ConfigurationError(Exception):
    """Raised when there are configuration-related errors."""
    pass

class DoorState:
    """Class to represent and manage door states"""
    def __init__(self, state_file):
        self.state_file = state_file
        self.current_state = {
            'door_open': False,
            'last_change': datetime.now().isoformat(),
            'last_alert': datetime.now().isoformat(),
            'alert_sent': False
        }
        self.load_state()

    def load_state(self):
        """Load state from file"""
        try:
            with open(self.state_file, 'r') as f:
                saved_state = json.load(f)
                self.current_state.update(saved_state)
                # Convert ISO format strings back to datetime
                self.current_state['last_change'] = datetime.fromisoformat(self.current_state['last_change'])
                self.current_state['last_alert'] = datetime.fromisoformat(self.current_state['last_alert'])
        except FileNotFoundError:
            self.save_state()

    def save_state(self):
        """Save current state to file"""
        state_to_save = self.current_state.copy()
        # Convert datetime objects to ISO format strings for JSON serialization
        state_to_save['last_change'] = state_to_save['last_change'].isoformat()
        state_to_save['last_alert'] = state_to_save['last_alert'].isoformat()
        
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w') as f:
            json.dump(state_to_save, f, indent=2)

    def update_state(self, door_open, alert_sent=None):
        """Update state with new values"""
        if self.current_state['door_open'] != door_open:
            self.current_state['door_open'] = door_open
            self.current_state['last_change'] = datetime.now()
        
        if alert_sent is not None:
            self.current_state['alert_sent'] = alert_sent
            self.current_state['last_alert'] = datetime.now()
        
        self.save_state()

class DoorSensorDaemon:
    def __init__(self, config_file=CONFIG_FILE):
        """Initialize the door sensor daemon with configuration."""
        self.config = self.load_config(config_file)
        self.setup_logging()
        self.setup_gpio()
        self.state = DoorState(STATE_FILE)
        self.setup_monitoring()

    def load_config(self, config_file):
        """Load configuration from INI file."""
        if not os.path.exists(config_file):
            raise ConfigurationError(f"Configuration file not found: {config_file}")

        config = ConfigParser()
        config.read(config_file)

        return {
            'gpio': {
                'sensor_pin': config.getint('gpio', 'sensor_pin', fallback=17),
                'pull_up': config.getboolean('gpio', 'pull_up', fallback=True)
            },
            'ssh': {
                'host': config.get('ssh', 'host'),
                'port': config.getint('ssh', 'port', fallback=22),
                'key_path': os.path.expanduser(config.get('ssh', 'key_path')),
                'timeout': config.getint('ssh', 'timeout', fallback=10)
            },
            'commands': {
                'door_open': config.get('commands', 'door_open'),
                'door_closed': config.get('commands', 'door_closed')
            },
            'monitoring': {
                'alert_delay': config.getfloat('monitoring', 'alert_delay', fallback=0.5),
                'debounce_delay': config.getfloat('monitoring', 'debounce_delay', fallback=1.0),
                'health_check_interval': config.getint('monitoring', 'health_check_interval', fallback=300)
            }
        }

    def setup_logging(self):
        """Configure logging with rotation and proper formatting."""
        log_dir = os.path.dirname(LOG_FILE)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, mode=0o755, exist_ok=True)

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
            handlers=[
                logging.RotatingFileHandler(
                    LOG_FILE,
                    maxBytes=1024*1024,  # 1MB
                    backupCount=5
                ),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger('DoorSensor')

    def setup_gpio(self):
        """Initialize GPIO settings."""
        self.sensor_pin = self.config['gpio']['sensor_pin']
        self.pull_up = self.config['gpio']['pull_up']

        GPIO.setmode(GPIO.BCM)
        pull_up_down = GPIO.PUD_UP if self.pull_up else GPIO.PUD_DOWN
        GPIO.setup(self.sensor_pin, GPIO.IN, pull_up_down=pull_up_down)
        
        self.closed_state = GPIO.LOW if self.pull_up else GPIO.HIGH
        self.logger.info(f"GPIO initialized: pin={self.sensor_pin}, pull_up={self.pull_up}")

    def setup_monitoring(self):
        """Initialize monitoring parameters."""
        self.running = False
        self.last_change_time = datetime.now()
        self.last_health_check = datetime.now()
        self.debounce_delay = timedelta(seconds=self.config['monitoring']['debounce_delay'])
        self.health_check_interval = timedelta(seconds=self.config['monitoring']['health_check_interval'])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def run_ssh_command(self, command):
        """Execute SSH command with retries."""
        try:
            ssh_cmd = [
                "ssh",
                "-i", self.config['ssh']['key_path'],
                "-p", str(self.config['ssh']['port']),
                "-o", f"ConnectTimeout={self.config['ssh']['timeout']}",
                "-o", "BatchMode=yes",
                self.config['ssh']['host'],
                command
            ]

            self.logger.debug(f"Executing SSH command: {command}")
            
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=self.config['ssh']['timeout'] + 5
            )

            if result.returncode == 0:
                self.logger.info("SSH command executed successfully")
                return True
            else:
                self.logger.error(f"SSH command failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            self.logger.error(f"SSH command timed out after {self.config['ssh']['timeout']} seconds")
            raise
        except Exception as e:
            self.logger.error(f"Error executing SSH command: {e}")
            raise

    def read_sensor(self):
        """Read the current state of the door sensor."""
        return GPIO.input(self.sensor_pin)

    def is_door_open(self):
        """Determine if the door is open based on sensor reading."""
        return self.read_sensor() != self.closed_state

    def is_debounce_period_over(self):
        """Check if the debounce period has elapsed."""
        return datetime.now() - self.last_change_time > self.debounce_delay

    def check_health(self):
        """Perform system health check."""
        if datetime.now() - self.last_health_check > self.health_check_interval:
            self.last_health_check = datetime.now()
            self.logger.info("Health check: System operational")
            return True
        return False

    def create_pid_file(self):
        """Create PID file for process management."""
        try:
            with open(PID_FILE, 'w') as f:
                f.write(str(os.getpid()))
            self.logger.info(f"PID file created: {PID_FILE}")
        except Exception as e:
            self.logger.error(f"Failed to create PID file: {e}")
            raise

    def remove_pid_file(self):
        """Remove PID file during cleanup."""
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
                self.logger.info(f"PID file removed: {PID_FILE}")
        except Exception as e:
            self.logger.error(f"Failed to remove PID file: {e}")

    def check_single_instance(self):
        """Ensure only one instance is running."""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                self.logger.error(f"Another instance is already running (PID: {pid})")
                return False
            except (ValueError, OSError):
                self.logger.warning("Stale PID file found, removing it")
                self.remove_pid_file()
        return True

    def signal_handler(self, signum, frame):
        """Handle system signals for graceful shutdown."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def should_send_alert(self, current_open):
        """
        Determine if an alert should be sent based on current and previous states
        """
        if current_open != self.state.current_state['door_open']:
            # State has changed
            return True
        
        if current_open and not self.state.current_state['alert_sent']:
            # Door is open and no alert has been sent
            return True
            
        return False

    def monitor(self):
        """Main monitoring loop with state checking."""
        if not self.check_single_instance():
            sys.exit(1)

        self.create_pid_file()
        self.running = True
        
        self.logger.info("Starting door sensor monitoring...")

        # Initial state check
        current_open = self.is_door_open()
        
        # Only send initial alert if different from saved state
        if self.should_send_alert(current_open):
            self.logger.info(f"Initial door state: {'OPEN' if current_open else 'CLOSED'}")
            command = self.config['commands']['door_open' if current_open else 'door_closed']
            if self.run_ssh_command(command):
                self.state.update_state(current_open, alert_sent=True)

        try:
            while self.running:
                current_open = self.is_door_open()
                
                if self.is_debounce_period_over() and self.should_send_alert(current_open):
                    self.last_change_time = datetime.now()

                    if current_open:
                        self.logger.warning("Door OPENED")
                        command = self.config['commands']['door_open']
                    else:
                        self.logger.info("Door CLOSED")
                        command = self.config['commands']['door_closed']

                    if self.run_ssh_command(command):
                        self.state.update_state(current_open, alert_sent=True)
                    else:
                        # If command failed, mark alert as not sent to retry
                        self.state.update_state(current_open, alert_sent=False)

                self.check_health()
                time.sleep(self.config['monitoring']['alert_delay'])

        except Exception as e:
            self.logger.error(f"Error in monitoring loop: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources during shutdown."""
        GPIO.cleanup()
        self.remove_pid_file()
        self.state.save_state()
        self.logger.info("Cleanup completed")

def main():
    try:
        door_sensor = DoorSensorDaemon()
        signal.signal(signal.SIGINT, door_sensor.signal_handler)
        signal.signal(signal.SIGTERM, door_sensor.signal_handler)
        door_sensor.monitor()
    except ConfigurationError as e:
        logging.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
