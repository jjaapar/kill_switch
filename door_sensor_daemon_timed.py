#!/usr/bin/env python3
"""
Door Sensor Daemon
Door monitoring system that triggers remote actions via SSH.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from configparser import ConfigParser
from contextlib import contextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

from tenacity import retry, stop_after_attempt, wait_exponential

class ConfigurationError(Exception):
    """Configuration-related errors."""
    pass


class DoorSensorDaemon:
    # Default paths
    CONFIG_FILE = "/etc/door_sensor/config.ini"
    STATE_FILE = "/var/lib/door_sensor/state.json"
    LOG_FILE = "/var/log/door_sensor.log"
    PID_FILE = "/var/run/door_sensor/door_sensor.pid"

    def __init__(self, config_file: str = None):
        """Initialize daemon with configuration."""
        self.config_file = config_file or self.CONFIG_FILE
        self.config = self._load_config()
        self.logger = self._setup_logging()
        
        # State management
        self.state = {
            'door_open': False,
            'last_change': datetime.now().isoformat(),
            'last_alert': None,
            'open_alert_sent': False,
            'initialized': False
        }
        self._load_state()
        
        # Runtime variables
        self.running = False
        self.last_change_time = datetime.now()
        self.door_opened_time = None
        self.last_health_check = datetime.now()
        
        # GPIO setup
        self._setup_gpio()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from INI file."""
        if not Path(self.config_file).exists():
            raise ConfigurationError(f"Configuration file not found: {self.config_file}")

        config = ConfigParser()
        config.read(self.config_file)

        return {
            'gpio_pin': config.getint('gpio', 'sensor_pin', fallback=17),
            'gpio_pull_up': config.getboolean('gpio', 'pull_up', fallback=True),
            'ssh_host': config.get('ssh', 'host'),
            'ssh_port': config.getint('ssh', 'port', fallback=22),
            'ssh_key': os.path.expanduser(config.get('ssh', 'key_path')),
            'ssh_timeout': config.getint('ssh', 'timeout', fallback=10),
            'cmd_open': config.get('commands', 'door_open'),
            'cmd_closed': config.get('commands', 'door_closed'),
            'alert_delay': config.getfloat('monitoring', 'alert_delay', fallback=0.5),
            'debounce_delay': config.getfloat('monitoring', 'debounce_delay', fallback=1.0),
            'open_alert_delay': config.getfloat('monitoring', 'open_alert_delay', fallback=10.0),
            'health_interval': config.getint('monitoring', 'health_check_interval', fallback=300)
        }

    def _setup_logging(self) -> logging.Logger:
        """Configure logging with rotation."""
        log_path = Path(self.LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

        logger = logging.getLogger('DoorSensor')
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        # File handler with rotation
        file_handler = RotatingFileHandler(log_path, maxBytes=1024*1024, backupCount=5)
        console_handler = logging.StreamHandler(sys.stdout)
        
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(name)s] %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
        return logger

    def _setup_gpio(self) -> None:
        """Initialize GPIO settings."""
        if GPIO is None:
            self.logger.warning("RPi.GPIO not available - running in test mode")
            return
            
        GPIO.setmode(GPIO.BCM)
        pull_up_down = GPIO.PUD_UP if self.config['gpio_pull_up'] else GPIO.PUD_DOWN
        GPIO.setup(self.config['gpio_pin'], GPIO.IN, pull_up_down=pull_up_down)
        
        self.closed_state = GPIO.LOW if self.config['gpio_pull_up'] else GPIO.HIGH
        self.logger.info(f"GPIO initialized: pin={self.config['gpio_pin']}, pull_up={self.config['gpio_pull_up']}")

    def _load_state(self) -> None:
        """Load state from file."""
        state_path = Path(self.STATE_FILE)
        try:
            if state_path.exists():
                with state_path.open('r') as f:
                    saved_state = json.load(f)
                    self.state.update(saved_state)
                    self.state['initialized'] = True
        except (json.JSONDecodeError, OSError) as e:
            self.logger.warning(f"Could not load state file: {e}")
            self._save_state()

    def _save_state(self) -> None:
        """Save current state to file."""
        state_path = Path(self.STATE_FILE)
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        
        with state_path.open('w') as f:
            json.dump(self.state, f, indent=2)

    def _is_door_open(self) -> bool:
        """Read sensor and determine if door is open."""
        if GPIO is None:
            return False  # Test mode
        return GPIO.input(self.config['gpio_pin']) != self.closed_state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def _run_ssh_command(self, command: str) -> bool:
        """Execute SSH command with retries."""
        ssh_cmd = [
            "ssh", "-i", self.config['ssh_key'], "-p", str(self.config['ssh_port']),
            "-o", f"ConnectTimeout={self.config['ssh_timeout']}", "-o", "BatchMode=yes",
            self.config['ssh_host'], command
        ]

        self.logger.debug(f"Executing SSH command: {command}")
        
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, 
                                  timeout=self.config['ssh_timeout'] + 5, check=False)

            if result.returncode == 0:
                self.logger.info("SSH command executed successfully")
                return True
            else:
                self.logger.error(f"SSH command failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            self.logger.error(f"SSH command timed out")
            raise
        except Exception as e:
            self.logger.error(f"SSH command error: {e}")
            raise

    def _handle_door_change(self, current_open: bool) -> None:
        """Handle door state change and execute command."""
        # Update state and timing
        self.state['door_open'] = current_open
        self.state['last_change'] = datetime.now().isoformat()
        self.state['initialized'] = True
        self.last_change_time = datetime.now()
        
        # Track door opening time for alert delay
        if current_open:
            self.door_opened_time = datetime.now()
            self.state['open_alert_sent'] = False
        else:
            self.door_opened_time = None
            self.state['open_alert_sent'] = False
        
        # Log the change
        state_text = "OPEN" if current_open else "CLOSED"
        log_level = logging.WARNING if current_open else logging.INFO
        self.logger.log(log_level, f"Door state changed: {state_text}")
        print(f"ALERT: Door {state_text}")

        # For door closing, execute command immediately
        if not current_open:
            command = self.config['cmd_closed']
            try:
                success = self._run_ssh_command(command)
                if success:
                    self.state['last_alert'] = datetime.now().isoformat()
            except Exception as e:
                self.logger.error(f"Failed to execute SSH command: {e}")
        
        self._save_state()

    def _check_open_alert(self) -> None:
        """Check if door has been open long enough to send alert."""
        if (self.state['door_open'] and 
            not self.state['open_alert_sent'] and 
            self.door_opened_time and
            datetime.now() - self.door_opened_time > timedelta(seconds=self.config['open_alert_delay'])):
            
            self.logger.warning(f"Door has been open for {self.config['open_alert_delay']} seconds - sending alert")
            print(f"PROLONGED ALERT: Door open for {self.config['open_alert_delay']} seconds!")
            
            command = self.config['cmd_open']
            try:
                success = self._run_ssh_command(command)
                if success:
                    self.state['last_alert'] = datetime.now().isoformat()
                    self.state['open_alert_sent'] = True
                    self._save_state()
            except Exception as e:
                self.logger.error(f"Failed to execute SSH command for open alert: {e}")

    def _check_health(self) -> None:
        """Perform periodic health check."""
        if datetime.now() - self.last_health_check > timedelta(seconds=self.config['health_interval']):
            self.last_health_check = datetime.now()
            self.logger.info("Health check: System operational")

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _check_single_instance(self) -> bool:
        """Ensure only one instance is running."""
        pid_path = Path(self.PID_FILE)
        if pid_path.exists():
            try:
                with pid_path.open('r') as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)  # Check if process exists
                self.logger.error(f"Another instance already running (PID: {pid})")
                return False
            except (ValueError, OSError):
                self.logger.warning("Removing stale PID file")
                pid_path.unlink(missing_ok=True)
        return True

    @contextmanager
    def _pid_file_context(self):
        """Manage PID file lifecycle."""
        pid_path = Path(self.PID_FILE)
        try:
            with pid_path.open('w') as f:
                f.write(str(os.getpid()))
            self.logger.info(f"PID file created: {pid_path}")
            yield
        except Exception as e:
            self.logger.error(f"Failed to create PID file: {e}")
            raise
        finally:
            pid_path.unlink(missing_ok=True)
            self.logger.info("PID file removed")

    def monitor(self) -> None:
        """Main monitoring loop."""
        if not self._check_single_instance():
            sys.exit(1)

        with self._pid_file_context():
            self.running = True
            self.logger.info("Starting door sensor monitoring...")
            
            try:
                # Handle initial state
                current_open = self._is_door_open()
                
                if not self.state['initialized']:
                    self.logger.info(f"Initial door state: {'OPEN' if current_open else 'CLOSED'}")
                    if current_open:
                        print("Initial state: Door OPEN")
                        self.door_opened_time = datetime.now()
                        self.state['door_open'] = current_open
                        self.state['open_alert_sent'] = False
                        self.state['initialized'] = True
                        self._save_state()
                    else:
                        self.state['door_open'] = current_open
                        self.state['initialized'] = True
                        self._save_state()

                # Main monitoring loop
                while self.running:
                    current_open = self._is_door_open()
                    
                    # Check for state change after debounce period
                    if (self.state['door_open'] != current_open and 
                        datetime.now() - self.last_change_time > timedelta(seconds=self.config['debounce_delay'])):
                        self._handle_door_change(current_open)
                    else:
                        self.logger.debug(f"Door state: {'OPEN' if current_open else 'CLOSED'}")
                    
                    # Check if door has been open long enough for alert
                    self._check_open_alert()

                    self._check_health()
                    time.sleep(self.config['alert_delay'])

            except KeyboardInterrupt:
                self.logger.info("Received keyboard interrupt")
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                raise
            finally:
                self._cleanup()

    def _cleanup(self) -> None:
        """Clean up resources."""
        if GPIO is not None:
            GPIO.cleanup()
        self._save_state()
        self.logger.info("Cleanup completed")


def main() -> None:
    """Main entry point."""
    try:
        daemon = DoorSensorDaemon()
        daemon.monitor()
    except ConfigurationError as e:
        logging.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
