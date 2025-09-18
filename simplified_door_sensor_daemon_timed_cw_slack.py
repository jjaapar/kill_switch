#!/usr/bin/env python3
"""
Door Monitor - Watches a door and sends alerts when it opens/closes
Supports SSH commands, CloudWatch logging, and Slack notifications
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from configparser import ConfigParser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Optional imports
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None

from tenacity import retry, stop_after_attempt, wait_exponential


class DoorMonitor:
    def __init__(self, config_path="/etc/door_sensor/config.ini"):
        self.settings = self.load_config(config_path)
        self.logger = self.setup_logging()
        self.cloudwatch = self.setup_cloudwatch()
        self.door_closed_value = self.setup_gpio()
        self.memory = self.load_state()
        self.keep_running = True
        
    def load_config(self, config_path):
        """Load configuration with defaults"""
        if not Path(config_path).exists():
            print(f"Error: Config file not found at {config_path}")
            sys.exit(1)
            
        config = ConfigParser()
        config.read(config_path)
        
        return {
            # GPIO
            'door_pin': config.getint('gpio', 'sensor_pin', fallback=17),
            'use_pullup': config.getboolean('gpio', 'pull_up', fallback=True),
            
            # SSH
            'server': config.get('ssh', 'host'),
            'ssh_port': config.getint('ssh', 'port', fallback=22),
            'key_file': os.path.expanduser(config.get('ssh', 'key_path')),
            'timeout': config.getint('ssh', 'timeout', fallback=10),
            
            # Commands
            'door_open_cmd': config.get('commands', 'door_open'),
            'door_closed_cmd': config.get('commands', 'door_closed'),
            'maintenance_open_cmd': config.get('commands', 'door_open_maint', fallback=None),
            
            # Timing
            'check_interval': config.getfloat('monitoring', 'alert_delay', fallback=0.5),
            'debounce_time': config.getfloat('monitoring', 'debounce_delay', fallback=1.0),
            
            # CloudWatch
            'use_cloudwatch': config.getboolean('cloudwatch', 'enabled', fallback=False),
            'log_group': config.get('cloudwatch', 'log_group', fallback='door-sensor'),
            'log_stream': config.get('cloudwatch', 'log_stream', fallback='door-events'),
            'aws_region': config.get('cloudwatch', 'region', fallback='us-east-1'),
            
            # Slack
            'use_slack': config.getboolean('slack', 'enabled', fallback=False),
            'slack_webhook_url': config.get('slack', 'webhook_url', fallback=None),
            'slack_channel': config.get('slack', 'channel', fallback='#alerts'),
            'slack_username': config.get('slack', 'username', fallback='Door Monitor'),
            'slack_icon': config.get('slack', 'icon_emoji', fallback=':door:'),
            'slack_notify_open': config.getboolean('slack', 'notify_door_open', fallback=True),
            'slack_notify_closed': config.getboolean('slack', 'notify_door_closed', fallback=False),
            'slack_notify_startup': config.getboolean('slack', 'notify_startup', fallback=True),
            'slack_notify_shutdown': config.getboolean('slack', 'notify_shutdown', fallback=False),
            'slack_notify_maintenance': config.getboolean('slack', 'notify_maintenance_mode', fallback=True),
            'slack_timeout': config.getint('slack', 'timeout', fallback=10)
        }
    
    def setup_logging(self):
        """Setup file and console logging"""
        Path("/var/log").mkdir(exist_ok=True)
        logger = logging.getLogger('DoorMonitor')
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        
        # File logging with rotation
        file_handler = RotatingFileHandler("/var/log/door_sensor.log", maxBytes=1024*1024, backupCount=5)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        
        # Console logging
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        logger.addHandler(console_handler)
        
        return logger
    
    def setup_cloudwatch(self):
        """Setup CloudWatch logging if configured"""
        if not self.settings['use_cloudwatch'] or not boto3:
            return None
            
        try:
            client = boto3.client('logs', region_name=self.settings['aws_region'])
            
            # Create log group and stream if they don't exist
            for method, name in [('create_log_group', self.settings['log_group']), 
                               ('create_log_stream', self.settings['log_stream'])]:
                try:
                    if method == 'create_log_group':
                        client.create_log_group(logGroupName=name)
                    else:
                        client.create_log_stream(logGroupName=self.settings['log_group'], logStreamName=name)
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                        raise
            
            self.logger.info("CloudWatch logging enabled")
            return client
        except Exception as e:
            self.logger.warning(f"CloudWatch setup failed: {e}")
            return None
    
    def setup_gpio(self):
        """Setup GPIO for door sensor"""
        if not GPIO:
            self.logger.warning("GPIO not available - test mode")
            return None
            
        GPIO.setmode(GPIO.BCM)
        pull_mode = GPIO.PUD_UP if self.settings['use_pullup'] else GPIO.PUD_DOWN
        GPIO.setup(self.settings['door_pin'], GPIO.IN, pull_up_down=pull_mode)
        
        door_closed_value = GPIO.LOW if self.settings['use_pullup'] else GPIO.HIGH
        self.logger.info(f"GPIO setup on pin {self.settings['door_pin']}")
        return door_closed_value
    
    def load_state(self):
        """Load saved door state"""
        default_state = {
            'door_is_open': False,
            'last_changed': datetime.now().isoformat(),
            'last_alert_sent': None,
            'initialized': False
        }
        
        try:
            # Ensure state directory exists
            state_dir = Path("/var/lib/door_sensor")
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            
            state_file = state_dir / "state.json"
            if state_file.exists():
                saved_state = json.loads(state_file.read_text())
                default_state.update(saved_state)
                default_state['initialized'] = True
        except Exception as e:
            self.logger.warning(f"Couldn't load state: {e}")
        
        return default_state
    
    def save_state(self):
        """Save current door state"""
        try:
            # Ensure state directory exists
            state_dir = Path("/var/lib/door_sensor")
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            
            state_file = state_dir / "state.json"
            state_file.write_text(json.dumps(self.memory, indent=2))
        except Exception as e:
            self.logger.error(f"Couldn't save state: {e}")
    
    def is_maintenance_mode(self):
        """Check if in maintenance mode"""
        try:
            mode_file = Path("/tmp/state")
            return mode_file.exists() and mode_file.read_text().strip().upper() == "MAINT"
        except:
            return False
    
    def read_door_sensor(self):
        """Read current door state"""
        if not GPIO:
            return False  # Test mode
        return GPIO.input(self.settings['door_pin']) != self.door_closed_value
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    def send_slack(self, message, color="good"):
        """Send Slack notification"""
        if not self.settings['use_slack'] or not self.settings['slack_webhook_url']:
            return
            
        payload = {
            "channel": self.settings['slack_channel'],
            "username": self.settings['slack_username'], 
            "icon_emoji": self.settings['slack_icon'],
            "attachments": [{"color": color, "text": message, "ts": int(datetime.now().timestamp())}]
        }
        
        request = urllib.request.Request(
            self.settings['slack_webhook_url'],
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(request, timeout=self.settings['slack_timeout']) as response:
            if response.getcode() != 200:
                raise Exception(f"Slack returned {response.getcode()}")
    
    def log_to_cloudwatch(self, message, level, door_open, maintenance):
        """Send message to CloudWatch"""
        if not self.cloudwatch:
            return
            
        try:
            log_entry = {
                'timestamp': int(datetime.now().timestamp() * 1000),
                'message': json.dumps({
                    'when': datetime.now().isoformat(),
                    'level': level,
                    'message': message,
                    'door_state': 'OPEN' if door_open else 'CLOSED',
                    'maintenance_mode': maintenance
                })
            }
            
            self.cloudwatch.put_log_events(
                logGroupName=self.settings['log_group'],
                logStreamName=self.settings['log_stream'],
                logEvents=[log_entry]
            )
        except Exception as e:
            self.logger.warning(f"CloudWatch logging failed: {e}")
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def run_ssh_command(self, command):
        """Execute SSH command"""
        if not command:
            return True
            
        ssh_cmd = [
            "ssh", "-i", self.settings['key_file'], "-p", str(self.settings['ssh_port']),
            "-o", f"ConnectTimeout={self.settings['timeout']}", "-o", "BatchMode=yes",
            self.settings['server'], command
        ]
        
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, 
                              timeout=self.settings['timeout'] + 5)
        
        if result.returncode == 0:
            self.logger.info("SSH command executed successfully")
            return True
        else:
            raise Exception(f"SSH command failed: {result.stderr}")
    
    def handle_door_change(self, door_open, maintenance_mode):
        """Handle door state change"""
        timestamp = datetime.now()
        time_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
        state = "OPENED" if door_open else "CLOSED"
        
        # Update memory
        self.memory.update({
            'door_is_open': door_open,
            'last_changed': timestamp.isoformat(),
            'initialized': True
        })
        
        # Log the event
        log_method = self.logger.warning if door_open else self.logger.info
        maintenance_note = " (MAINTENANCE MODE)" if maintenance_mode else ""
        message = f"Door {state}{maintenance_note}"
        log_method(message)
        
        # Determine command and alert type
        command = None
        alert_type = "ALERT"
        
        if door_open:
            if maintenance_mode and self.settings['maintenance_open_cmd']:
                command = self.settings['maintenance_open_cmd']
                alert_type = "MAINTENANCE ALERT"
            else:
                command = self.settings['door_open_cmd']
        elif not maintenance_mode:
            command = self.settings['door_closed_cmd']
        
        # Console output
        console_msg = f"[{time_str}] {alert_type}: Door {state}{maintenance_note}"
        print(console_msg)
        
        # Slack notification
        self.send_door_slack_notification(door_open, maintenance_mode, state)
        
        # CloudWatch logging
        log_level = 'WARNING' if door_open else 'INFO'
        self.log_to_cloudwatch(console_msg, log_level, door_open, maintenance_mode)
        
        # Execute SSH command
        if command:
            try:
                if self.run_ssh_command(command):
                    self.memory['last_alert_sent'] = timestamp.isoformat()
                    success_msg = f"Command executed for door {state.lower()}"
                    self.log_to_cloudwatch(success_msg, 'INFO', door_open, maintenance_mode)
            except Exception as e:
                error_msg = f"SSH command failed: {e}"
                self.logger.error(error_msg)
                self.log_to_cloudwatch(error_msg, 'ERROR', door_open, maintenance_mode)
                
                # Slack error notification
                try:
                    slack_msg = f"❌ Failed to execute command for door {state.lower()}: {e}"
                    self.send_slack(slack_msg, "danger")
                except:
                    pass
        
        self.save_state()
        return timestamp
    
    def send_door_slack_notification(self, door_open, maintenance_mode, state):
        """Send appropriate Slack notification for door state change"""
        should_notify = self.settings['slack_notify_open'] if door_open else self.settings['slack_notify_closed']
        
        if not should_notify:
            return
            
        if door_open:
            color = "warning" if not maintenance_mode else "danger"
            if maintenance_mode and self.settings['slack_notify_maintenance']:
                message = f"🔧 Maintenance Alert: Door {state.lower()}"
            else:
                message = f"🚪 Door {state.lower()}"
        else:
            color = "good"
            message = f"🚪 Door {state.lower()}"
        
        try:
            self.send_slack(message, color)
        except Exception as e:
            self.logger.warning(f"Slack notification failed: {e}")
    
    def ensure_single_instance(self):
        """Ensure only one instance is running"""
        # Ensure run directory exists
        run_dir = Path("/var/run/door_sensor")
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        
        lock_file = run_dir / "door_sensor.pid"
        
        if lock_file.exists():
            try:
                pid = int(lock_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                self.logger.error(f"Another instance running (PID: {pid})")
                return False
            except (ValueError, OSError):
                lock_file.unlink(missing_ok=True)
        
        lock_file.write_text(str(os.getpid()))
        return True
    
    def setup_signal_handlers(self):
        """Setup graceful shutdown handlers"""
        def shutdown_handler(sig, frame):
            print("\nShutting down...")
            self.keep_running = False
        
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)
    
    def run(self):
        """Main monitoring loop"""
        if not self.ensure_single_instance():
            sys.exit(1)
        
        self.setup_signal_handlers()
        
        try:
            # Startup
            start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.logger.info("Door monitor starting")
            self.log_to_cloudwatch(f"Door monitor started at {start_time}", 'INFO', 
                                  self.memory['door_is_open'], False)
            
            if self.settings['slack_notify_startup']:
                try:
                    self.send_slack(f"🟢 Door Monitor started at {start_time}", "good")
                except Exception as e:
                    self.logger.warning(f"Startup Slack notification failed: {e}")
            
            # Initialize state
            maintenance_mode = self.is_maintenance_mode()
            door_open = self.read_door_sensor()
            last_change_time = datetime.now()
            
            if not self.memory['initialized']:
                state_msg = f"Starting up - door is {'OPEN' if door_open else 'CLOSED'}"
                self.logger.info(state_msg)
                self.log_to_cloudwatch(state_msg, 'INFO', door_open, maintenance_mode)
                
                if door_open:
                    last_change_time = self.handle_door_change(door_open, maintenance_mode)
                else:
                    self.memory.update({'door_is_open': door_open, 'initialized': True})
                    self.save_state()
            
            last_maintenance_check = datetime.now()
            print(f"Monitoring door on GPIO pin {self.settings['door_pin']}")
            print("Press Ctrl+C to stop")
            
            # Main monitoring loop
            while self.keep_running:
                # Check maintenance mode every 30 seconds
                if datetime.now() - last_maintenance_check > timedelta(seconds=30):
                    maintenance_mode = self.is_maintenance_mode()
                    last_maintenance_check = datetime.now()
                
                # Check door state
                door_open = self.read_door_sensor()
                state_changed = self.memory['door_is_open'] != door_open
                debounce_passed = datetime.now() - last_change_time > timedelta(seconds=self.settings['debounce_time'])
                
                if state_changed and debounce_passed:
                    last_change_time = self.handle_door_change(door_open, maintenance_mode)
                
                time.sleep(self.settings['check_interval'])
        
        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            raise
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Cleanup resources and send shutdown notifications"""
        if GPIO:
            GPIO.cleanup()
        
        self.save_state()
        
        # Clean up PID file
        run_dir = Path("/var/run/door_sensor")
        pid_file = run_dir / "door_sensor.pid"
        pid_file.unlink(missing_ok=True)
        
        shutdown_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.logger.info("Door monitor shut down")
        self.log_to_cloudwatch(f"Door monitor shut down at {shutdown_time}", 'INFO',
                              self.memory.get('door_is_open', False), self.is_maintenance_mode())
        
        if self.settings['slack_notify_shutdown']:
            try:
                self.send_slack(f"🔴 Door Monitor shut down at {shutdown_time}", "warning")
            except Exception as e:
                self.logger.warning(f"Shutdown Slack notification failed: {e}")


def main():
    """Entry point"""
    try:
        monitor = DoorMonitor()
        monitor.run()
    except Exception as e:
        print(f"Fatal error: {e}")
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
