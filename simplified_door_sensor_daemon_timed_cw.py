#!/usr/bin/env python3
"""Door Sensor Daemon - Simplified with CloudWatch Logging"""

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

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    boto3 = None

from tenacity import retry, stop_after_attempt, wait_exponential


class DoorSensorDaemon:
    def __init__(self, config_file="/etc/door_sensor/config.ini"):
        self.config = self._load_config(config_file)
        self.logger = self._setup_logging()
        
        # State
        self.state = {'door_open': False, 'last_change': datetime.now().isoformat(), 
                     'last_alert': None, 'initialized': False}
        self._load_state()
        
        # Runtime
        self.running = False
        self.last_change_time = datetime.now()
        self.maintenance_mode = False
        
        # CloudWatch
        self._setup_cloudwatch()
        
        self._setup_gpio()
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, 'running', False))
        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, 'running', False))

    def _load_config(self, config_file):
        if not Path(config_file).exists():
            raise Exception(f"Config file not found: {config_file}")
        
        config = ConfigParser()
        config.read(config_file)
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
            # CloudWatch settings
            'cloudwatch_enabled': config.getboolean('cloudwatch', 'enabled', fallback=False),
            'cloudwatch_log_group': config.get('cloudwatch', 'log_group', fallback='door-sensor'),
            'cloudwatch_log_stream': config.get('cloudwatch', 'log_stream', fallback='door-events'),
            'cloudwatch_region': config.get('cloudwatch', 'region', fallback='us-east-1')
        }

    def _setup_logging(self):
        Path("/var/log").mkdir(exist_ok=True)
        logger = logging.getLogger('DoorSensor')
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        
        handler = RotatingFileHandler("/var/log/door_sensor.log", maxBytes=1024*1024, backupCount=5)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler())
        return logger

    def _setup_cloudwatch(self):
        """Initialize CloudWatch logging client if enabled and available"""
        self.cloudwatch_client = None
        
        if not self.config['cloudwatch_enabled']:
            self.logger.info("CloudWatch logging disabled in config")
            return
        
        if boto3 is None:
            self.logger.warning("boto3 not available - CloudWatch logging disabled")
            return
        
        try:
            self.cloudwatch_client = boto3.client('logs', region_name=self.config['cloudwatch_region'])
            self._ensure_cloudwatch_resources()
            self.logger.info("CloudWatch logging initialized")
        except (ClientError, NoCredentialsError) as e:
            self.logger.warning(f"CloudWatch initialization failed: {e}")
            self.cloudwatch_client = None

    def _ensure_cloudwatch_resources(self):
        """Create CloudWatch log group and stream if they don't exist"""
        try:
            # Create log group if it doesn't exist
            try:
                self.cloudwatch_client.create_log_group(logGroupName=self.config['cloudwatch_log_group'])
                self.logger.info(f"Created CloudWatch log group: {self.config['cloudwatch_log_group']}")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                    raise
            
            # Create log stream if it doesn't exist
            try:
                self.cloudwatch_client.create_log_stream(
                    logGroupName=self.config['cloudwatch_log_group'],
                    logStreamName=self.config['cloudwatch_log_stream']
                )
                self.logger.info(f"Created CloudWatch log stream: {self.config['cloudwatch_log_stream']}")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                    raise
                    
        except ClientError as e:
            self.logger.error(f"Failed to create CloudWatch resources: {e}")
            raise

    def _send_to_cloudwatch(self, message, level='INFO'):
        """Send a log event to CloudWatch"""
        if not self.cloudwatch_client:
            return
        
        try:
            now = datetime.now()
            timestamp = int(now.timestamp() * 1000)
            log_event = {
                'timestamp': timestamp,
                'message': json.dumps({
                    'timestamp': now.isoformat(),
                    'formatted_timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'device_id': self.config.get('device_id', 'unknown'),
                    'level': level,
                    'message': message,
                    'door_state': 'OPEN' if self.state['door_open'] else 'CLOSED',
                    'maintenance_mode': self.maintenance_mode
                })
            }
            
            # Get sequence token if needed
            sequence_token = None
            try:
                response = self.cloudwatch_client.describe_log_streams(
                    logGroupName=self.config['cloudwatch_log_group'],
                    logStreamNamePrefix=self.config['cloudwatch_log_stream']
                )
                for stream in response['logStreams']:
                    if stream['logStreamName'] == self.config['cloudwatch_log_stream']:
                        sequence_token = stream.get('uploadSequenceToken')
                        break
            except ClientError:
                pass
            
            # Send the log event
            kwargs = {
                'logGroupName': self.config['cloudwatch_log_group'],
                'logStreamName': self.config['cloudwatch_log_stream'],
                'logEvents': [log_event]
            }
            if sequence_token:
                kwargs['sequenceToken'] = sequence_token
                
            self.cloudwatch_client.put_log_events(**kwargs)
            
        except ClientError as e:
            self.logger.warning(f"Failed to send to CloudWatch: {e}")

    def _setup_gpio(self):
        if GPIO is None:
            self.logger.warning("RPi.GPIO not available - test mode")
            return
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.config['gpio_pin'], GPIO.IN, 
                  pull_up_down=GPIO.PUD_UP if self.config['gpio_pull_up'] else GPIO.PUD_DOWN)
        self.closed_state = GPIO.LOW if self.config['gpio_pull_up'] else GPIO.HIGH

    def _load_state(self):
        try:
            state_file = Path("/var/lib/door_sensor/state.json")
            if state_file.exists():
                self.state.update(json.loads(state_file.read_text()))
                self.state['initialized'] = True
        except Exception as e:
            self.logger.warning(f"Could not load state: {e}")

    def _save_state(self):
        try:
            state_file = Path("/var/lib/door_sensor/state.json")
            state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            state_file.write_text(json.dumps(self.state, indent=2))
        except Exception as e:
            self.logger.error(f"Could not save state: {e}")

    def _check_maintenance_mode(self):
        try:
            state_file = Path("/tmp/state")
            if state_file.exists():
                content = state_file.read_text().strip().upper()
                new_mode = content == "MAINT"
                if new_mode != self.maintenance_mode:
                    self.maintenance_mode = new_mode
                    mode = "MAINTENANCE" if new_mode else "ACTIVE"
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.logger.info(f"Mode changed to: {mode}")
                    self._send_to_cloudwatch(f"[{timestamp}] Mode changed to: {mode}", 'INFO')
            else:
                self.maintenance_mode = False
        except Exception as e:
            self.logger.warning(f"Could not read maintenance state: {e}")
            self.maintenance_mode = False

    def _is_door_open(self):
        return GPIO and GPIO.input(self.config['gpio_pin']) != self.closed_state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def _run_ssh_command(self, command):
        cmd = ["ssh", "-i", self.config['ssh_key'], "-p", str(self.config['ssh_port']),
               "-o", f"ConnectTimeout={self.config['ssh_timeout']}", "-o", "BatchMode=yes",
               self.config['ssh_host'], command]
        
        result = subprocess.run(cmd, capture_output=True, text=True, 
                              timeout=self.config['ssh_timeout'] + 5)
        if result.returncode == 0:
            self.logger.info("SSH command executed successfully")
            return True
        else:
            self.logger.error(f"SSH command failed: {result.stderr}")
            raise Exception("SSH command failed")

    def _handle_door_change(self, current_open):
        now = datetime.now()
        timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
        
        self.state.update({
            'door_open': current_open,
            'last_change': now.isoformat(),
            'initialized': True
        })
        self.last_change_time = now
        
        state_text = "OPEN" if current_open else "CLOSED"
        log_level = logging.WARNING if current_open else logging.INFO
        self.logger.log(log_level, f"Door {state_text}")
        
        # Send state change to CloudWatch with timestamp
        cloudwatch_level = 'WARNING' if current_open else 'INFO'
        self._send_to_cloudwatch(f"[{timestamp_str}] Door {state_text}", cloudwatch_level)
        
        if self.maintenance_mode:
            maintenance_msg = f"MAINTENANCE MODE: Skipping alerts for door {state_text}"
            self.logger.info(maintenance_msg)
            self._send_to_cloudwatch(f"[{timestamp_str}] {maintenance_msg}", 'INFO')
            print(f"[{timestamp_str}] Door {state_text} (MAINTENANCE MODE)")
        else:
            print(f"[{timestamp_str}] ALERT: Door {state_text}")
            try:
                command = self.config['cmd_open'] if current_open else self.config['cmd_closed']
                if self._run_ssh_command(command):
                    self.state['last_alert'] = now.isoformat()
                    self._send_to_cloudwatch(f"[{timestamp_str}] Alert sent for door {state_text}", 'INFO')
            except Exception as e:
                error_msg = f"SSH command failed: {e}"
                self.logger.error(error_msg)
                self._send_to_cloudwatch(f"[{timestamp_str}] {error_msg}", 'ERROR')
        
        self._save_state()

    def _check_single_instance(self):
        pid_file = Path("/var/run/door_sensor.pid")
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                self.logger.error(f"Already running (PID: {pid})")
                return False
            except (ValueError, OSError):
                pid_file.unlink(missing_ok=True)
        
        pid_file.write_text(str(os.getpid()))
        return True

    def monitor(self):
        if not self._check_single_instance():
            sys.exit(1)

        try:
            self.running = True
            start_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.logger.info("Starting door sensor monitoring")
            self._send_to_cloudwatch(f"[{start_timestamp}] Starting door sensor monitoring", 'INFO')
            
            self._check_maintenance_mode()
            current_open = self._is_door_open()
            
            # Handle initial state
            if not self.state['initialized']:
                initial_state = 'OPEN' if current_open else 'CLOSED'
                init_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.logger.info(f"Initial door state: {initial_state}")
                self._send_to_cloudwatch(f"[{init_timestamp}] Initial door state: {initial_state}", 'INFO')
                if current_open:
                    self._handle_door_change(current_open)
                else:
                    self.state.update({'door_open': current_open, 'initialized': True})
                    self._save_state()

            last_maintenance_check = datetime.now()
            
            # Main loop
            while self.running:
                # Check maintenance mode every 30 seconds
                if datetime.now() - last_maintenance_check > timedelta(seconds=30):
                    self._check_maintenance_mode()
                    last_maintenance_check = datetime.now()
                
                current_open = self._is_door_open()
                
                # Handle state change after debounce
                if (self.state['door_open'] != current_open and 
                    datetime.now() - self.last_change_time > timedelta(seconds=self.config['debounce_delay'])):
                    self._handle_door_change(current_open)
                
                time.sleep(self.config['alert_delay'])

        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Error: {e}")
            raise
        finally:
            if GPIO:
                GPIO.cleanup()
            self._save_state()
            Path("/var/run/door_sensor.pid").unlink(missing_ok=True)
            shutdown_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.logger.info("Shutdown complete")
            if self.cloudwatch_client:
                self._send_to_cloudwatch(f"[{shutdown_timestamp}] Shutdown complete", 'INFO')


def main():
    try:
        DoorSensorDaemon().monitor()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
