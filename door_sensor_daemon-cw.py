#!/usr/bin/env python3
"""
Door Sensor Daemon - Simplified Python version with CloudWatch integration
A secure, robust door monitoring system for Raspberry Pi that triggers remote actions via SSH
and sends metrics/logs to AWS CloudWatch.
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

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from tenacity import retry, stop_after_attempt, wait_exponential


class ConfigurationError(Exception):
    """Configuration-related errors."""
    pass


class CloudWatchManager:
    """Handles CloudWatch metrics and logs integration."""
    
    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.enabled = config.get('enabled', False) and BOTO3_AVAILABLE
        
        if not BOTO3_AVAILABLE and config.get('enabled', False):
            self.logger.warning("CloudWatch enabled in config but boto3 not available")
            self.enabled = False
        
        if self.enabled:
            try:
                # Initialize AWS clients
                session = boto3.Session(
                    aws_access_key_id=config.get('aws_access_key_id'),
                    aws_secret_access_key=config.get('aws_secret_access_key'),
                    region_name=config.get('aws_region', 'us-east-1')
                )
                
                self.cloudwatch = session.client('cloudwatch')
                self.logs_client = session.client('logs')
                
                # CloudWatch configuration
                self.namespace = config.get('namespace', 'DoorSensor')
                self.log_group = config.get('log_group', '/aws/iot/door-sensor')
                self.log_stream = config.get('log_stream', f"door-sensor-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                self.device_id = config.get('device_id', 'door-sensor-001')
                
                # Create log group and stream
                self._setup_cloudwatch_logs()
                self.logger.info("CloudWatch integration initialized successfully")
                
            except (NoCredentialsError, ClientError) as e:
                self.logger.error(f"CloudWatch initialization failed: {e}")
                self.enabled = False
            except Exception as e:
                self.logger.error(f"Unexpected CloudWatch error: {e}")
                self.enabled = False

    def _setup_cloudwatch_logs(self) -> None:
        """Create CloudWatch log group and stream if they don't exist."""
        try:
            # Create log group
            try:
                self.logs_client.create_log_group(logGroupName=self.log_group)
                self.logger.info(f"Created CloudWatch log group: {self.log_group}")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                    raise

            # Create log stream
            try:
                self.logs_client.create_log_stream(
                    logGroupName=self.log_group,
                    logStreamName=self.log_stream
                )
                self.logger.info(f"Created CloudWatch log stream: {self.log_stream}")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                    raise

        except Exception as e:
            self.logger.error(f"Failed to setup CloudWatch logs: {e}")
            raise

    def send_metric(self, metric_name: str, value: float, unit: str = 'Count', 
                   dimensions: Optional[Dict[str, str]] = None) -> bool:
        """Send custom metric to CloudWatch."""
        if not self.enabled:
            return False

        try:
            metric_data = {
                'MetricName': metric_name,
                'Value': value,
                'Unit': unit,
                'Timestamp': datetime.utcnow()
            }

            # Add dimensions
            if dimensions:
                metric_data['Dimensions'] = [
                    {'Name': k, 'Value': v} for k, v in dimensions.items()
                ]
            else:
                metric_data['Dimensions'] = [
                    {'Name': 'DeviceId', 'Value': self.device_id}
                ]

            self.cloudwatch.put_metric_data(
                Namespace=self.namespace,
                MetricData=[metric_data]
            )
            
            self.logger.debug(f"Sent CloudWatch metric: {metric_name} = {value}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to send CloudWatch metric: {e}")
            return False

    def send_log_event(self, message: str, level: str = 'INFO') -> bool:
        """Send log event to CloudWatch Logs."""
        if not self.enabled:
            return False

        try:
            log_event = {
                'timestamp': int(datetime.utcnow().timestamp() * 1000),
                'message': json.dumps({
                    'timestamp': datetime.utcnow().isoformat(),
                    'level': level,
                    'device_id': self.device_id,
                    'message': message
                })
            }

            self.logs_client.put_log_events(
                logGroupName=self.log_group,
                logStreamName=self.log_stream,
                logEvents=[log_event]
            )
            
            self.logger.debug(f"Sent CloudWatch log: {message}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to send CloudWatch log: {e}")
            return False

    def send_door_event(self, door_open: bool, state_changed: bool = True) -> None:
        """Send door-specific events and metrics."""
        if not self.enabled:
            return

        # Send metric
        metric_value = 1.0 if door_open else 0.0
        self.send_metric(
            'DoorState',
            metric_value,
            'None',
            {'DeviceId': self.device_id, 'DoorPosition': 'Open' if door_open else 'Closed'}
        )

        # Send state change count
        if state_changed:
            self.send_metric('DoorStateChanges', 1.0, 'Count')
            
            # Send log event
            event_message = f"Door {'opened' if door_open else 'closed'}"
            log_level = 'WARN' if door_open else 'INFO'
            self.send_log_event(event_message, log_level)

    def send_system_health(self) -> None:
        """Send system health metrics."""
        if not self.enabled:
            return

        self.send_metric('SystemHealth', 1.0, 'Count')
        self.send_log_event('System health check passed', 'INFO')

    def send_error_event(self, error_message: str, error_type: str = 'GeneralError') -> None:
        """Send error events to CloudWatch."""
        if not self.enabled:
            return

        self.send_metric(
            'Errors',
            1.0,
            'Count',
            {'DeviceId': self.device_id, 'ErrorType': error_type}
        )
        
        self.send_log_event(f"Error: {error_message}", 'ERROR')


class DoorSensorDaemon:
    """Simplified door sensor daemon with CloudWatch integration."""
    
    # Default paths
    CONFIG_FILE = "/etc/door_sensor/config.ini"
    STATE_FILE = "/var/lib/door_sensor/state.json"
    LOG_FILE = "/var/log/door_sensor.log"
    PID_FILE = "/var/run/door_sensor.pid"

    def __init__(self, config_file: str = None):
        """Initialize daemon with configuration."""
        self.config_file = config_file or self.CONFIG_FILE
        self.config = self._load_config()
        self.logger = self._setup_logging()
        
        # Initialize CloudWatch
        self.cloudwatch = CloudWatchManager(self.config.get('cloudwatch', {}), self.logger)
        
        # State management
        self.state = {
            'door_open': False,
            'last_change': datetime.now().isoformat(),
            'last_alert': None,
            'alert_sent': False,
            'initialized': False
        }
        self._load_state()
        
        # Runtime variables
        self.running = False
        self.last_change_time = datetime.now()
        self.last_health_check = datetime.now()
        self.last_cloudwatch_health = datetime.now()
        
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

        # Standard configuration
        result = {
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
            'health_interval': config.getint('monitoring', 'health_check_interval', fallback=300)
        }

        # CloudWatch configuration
        if config.has_section('cloudwatch'):
            result['cloudwatch'] = {
                'enabled': config.getboolean('cloudwatch', 'enabled', fallback=False),
                'aws_access_key_id': config.get('cloudwatch', 'aws_access_key_id', fallback=None),
                'aws_secret_access_key': config.get('cloudwatch', 'aws_secret_access_key', fallback=None),
                'aws_region': config.get('cloudwatch', 'aws_region', fallback='us-east-1'),
                'namespace': config.get('cloudwatch', 'namespace', fallback='DoorSensor'),
                'log_group': config.get('cloudwatch', 'log_group', fallback='/aws/iot/door-sensor'),
                'device_id': config.get('cloudwatch', 'device_id', fallback='door-sensor-001'),
                'health_interval': config.getint('cloudwatch', 'health_interval', fallback=900)  # 15 minutes
            }
        else:
            result['cloudwatch'] = {'enabled': False}

        return result

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
            self.cloudwatch.send_error_event(f"State file load error: {e}", "StateFileError")
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

    def _has_state_changed(self, current_open: bool) -> bool:
        """Check if door state has changed."""
        return self.state['door_open'] != current_open

    def _is_first_run(self) -> bool:
        """Check if this is the first run."""
        return not self.state['initialized']

    def _should_send_alert(self, current_open: bool) -> bool:
        """Determine if alert should be sent."""
        return (self._has_state_changed(current_open) or 
                (self._is_first_run() and current_open))

    def _is_debounce_over(self) -> bool:
        """Check if debounce period has elapsed."""
        return datetime.now() - self.last_change_time > timedelta(seconds=self.config['debounce_delay'])

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
                self.cloudwatch.send_metric('SSHCommandSuccess', 1.0, 'Count')
                return True
            else:
                self.logger.error(f"SSH command failed: {result.stderr}")
                self.cloudwatch.send_error_event(f"SSH command failed: {result.stderr}", "SSHError")
                return False

        except subprocess.TimeoutExpired:
            self.logger.error(f"SSH command timed out")
            self.cloudwatch.send_error_event("SSH command timeout", "SSHTimeout")
            raise
        except Exception as e:
            self.logger.error(f"SSH command error: {e}")
            self.cloudwatch.send_error_event(f"SSH command error: {e}", "SSHError")
            raise

    def _update_state(self, door_open: bool, alert_sent: Optional[bool] = None) -> bool:
        """Update state and return if it changed."""
        state_changed = self._has_state_changed(door_open)
        
        if state_changed:
            self.state['door_open'] = door_open
            self.state['last_change'] = datetime.now().isoformat()
        
        if alert_sent is not None:
            self.state['alert_sent'] = alert_sent
            if alert_sent:
                self.state['last_alert'] = datetime.now().isoformat()
        
        self.state['initialized'] = True
        self._save_state()
        return state_changed

    def _log_door_state(self, door_open: bool, state_changed: bool = False) -> None:
        """Log door state appropriately."""
        state_text = "OPEN" if door_open else "CLOSED"
        
        if state_changed:
            if door_open:
                self.logger.warning(f"Door state changed: {state_text}")
            else:
                self.logger.info(f"Door state changed: {state_text}")
            print(f"ALERT: Door {state_text}")
            
            # Send to CloudWatch
            self.cloudwatch.send_door_event(door_open, state_changed=True)
        else:
            self.logger.debug(f"Door state: {state_text}")
            # Send current state to CloudWatch (without state change event)
            self.cloudwatch.send_door_event(door_open, state_changed=False)

    def _check_health(self) -> None:
        """Perform periodic health check."""
        now = datetime.now()
        
        # Local health check
        if now - self.last_health_check > timedelta(seconds=self.config['health_interval']):
            self.last_health_check = now
            self.logger.info("Health check: System operational")

        # CloudWatch health check (less frequent)
        cloudwatch_interval = self.config['cloudwatch'].get('health_interval', 900)  # 15 minutes
        if now - self.last_cloudwatch_health > timedelta(seconds=cloudwatch_interval):
            self.last_cloudwatch_health = now
            self.cloudwatch.send_system_health()

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.cloudwatch.send_log_event(f"System shutdown - signal {signum}", "INFO")
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
            self.cloudwatch.send_error_event(f"PID file creation failed: {e}", "SystemError")
            raise
        finally:
            pid_path.unlink(missing_ok=True)
            self.logger.info("PID file removed")

    def _handle_state_change(self, current_open: bool) -> None:
        """Handle door state change."""
        self.last_change_time = datetime.now()
        self._log_door_state(current_open, state_changed=True)

        # Execute appropriate command
        command = self.config['cmd_open'] if current_open else self.config['cmd_closed']
        
        try:
            success = self._run_ssh_command(command)
            self._update_state(current_open, alert_sent=success)
        except Exception as e:
            self.logger.error(f"Failed to execute SSH command: {e}")
            self.cloudwatch.send_error_event(f"SSH command execution failed: {e}", "SSHError")
            self._update_state(current_open, alert_sent=False)

    def monitor(self) -> None:
        """Main monitoring loop."""
        if not self._check_single_instance():
            sys.exit(1)

        with self._pid_file_context():
            self.running = True
            self.logger.info("Starting door sensor monitoring...")
            self.cloudwatch.send_log_event("Door sensor daemon started", "INFO")
            
            try:
                # Initial state check
                current_open = self._is_door_open()
                
                if self._is_first_run():
                    self.logger.info(f"Initial door state: {'OPEN' if current_open else 'CLOSED'}")
                    self.cloudwatch.send_log_event(f"Initial door state: {'OPEN' if current_open else 'CLOSED'}", "INFO")
                    if current_open:
                        print("Initial state: Door OPEN")
                
                # Handle initial alert if needed
                if self._should_send_alert(current_open):
                    command = self.config['cmd_open'] if current_open else self.config['cmd_closed']
                    try:
                        success = self._run_ssh_command(command)
                        self._update_state(current_open, alert_sent=success)
                    except Exception as e:
                        self.logger.error(f"Initial SSH command failed: {e}")
                        self.cloudwatch.send_error_event(f"Initial SSH command failed: {e}", "SSHError")
                        self._update_state(current_open, alert_sent=False)
                else:
                    self._update_state(current_open, alert_sent=False)

                # Main monitoring loop
                while self.running:
                    current_open = self._is_door_open()
                    
                    if self._is_debounce_over():
                        if self._should_send_alert(current_open):
                            self._handle_state_change(current_open)
                        else:
                            self._log_door_state(current_open, state_changed=False)

                    self._check_health()
                    time.sleep(self.config['alert_delay'])

            except KeyboardInterrupt:
                self.logger.info("Received keyboard interrupt")
                self.cloudwatch.send_log_event("Keyboard interrupt received", "INFO")
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                self.cloudwatch.send_error_event(f"Monitoring loop error: {e}", "SystemError")
                raise
            finally:
                self._cleanup()

    def _cleanup(self) -> None:
        """Clean up resources."""
        if GPIO is not None:
            GPIO.cleanup()
        self._save_state()
        self.cloudwatch.send_log_event("Door sensor daemon stopped - cleanup completed", "INFO")
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
