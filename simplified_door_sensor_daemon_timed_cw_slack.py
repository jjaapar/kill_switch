#!/usr/bin/env python3
"""
Door Monitor - Watches a door and sends alerts when it opens/closes

This script monitors a door sensor and runs commands when the door state changes.
It supports both normal operation and maintenance mode with different commands.
Now includes Slack notification support.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from configparser import ConfigParser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Import optional dependencies
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None
    print("Warning: RPi.GPIO not available - running in test mode")

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None

from tenacity import retry, stop_after_attempt, wait_exponential


def read_config_file(config_path="/etc/door_sensor/config.ini"):
    """Read the configuration file and return settings"""
    if not Path(config_path).exists():
        print(f"Error: Can't find config file at {config_path}")
        sys.exit(1)
    
    config = ConfigParser()
    config.read(config_path)
    
    # Get the settings we need with sensible defaults
    settings = {
        # GPIO settings
        'door_pin': config.getint('gpio', 'sensor_pin', fallback=17),
        'use_pullup': config.getboolean('gpio', 'pull_up', fallback=True),
        
        # SSH connection
        'server': config.get('ssh', 'host'),
        'ssh_port': config.getint('ssh', 'port', fallback=22),
        'key_file': os.path.expanduser(config.get('ssh', 'key_path')),
        'timeout': config.getint('ssh', 'timeout', fallback=10),
        
        # Commands to run
        'door_open_cmd': config.get('commands', 'door_open'),
        'door_closed_cmd': config.get('commands', 'door_closed'),
        'maintenance_open_cmd': config.get('commands', 'door_open_maint', fallback=None),
        
        # Timing
        'check_every': config.getfloat('monitoring', 'alert_delay', fallback=0.5),
        'ignore_bounces_for': config.getfloat('monitoring', 'debounce_delay', fallback=1.0),
        
        # CloudWatch (optional)
        'use_cloudwatch': config.getboolean('cloudwatch', 'enabled', fallback=False),
        'log_group': config.get('cloudwatch', 'log_group', fallback='door-sensor'),
        'log_stream': config.get('cloudwatch', 'log_stream', fallback='door-events'),
        'aws_region': config.get('cloudwatch', 'region', fallback='us-east-1'),
        
        # Slack notifications (optional)
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
    
    return settings


def setup_file_logging():
    """Create a logger that writes to both file and console"""
    Path("/var/log").mkdir(exist_ok=True)
    
    logger = logging.getLogger('DoorMonitor')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    # Log to file with rotation
    file_handler = RotatingFileHandler("/var/log/door_sensor.log", 
                                      maxBytes=1024*1024, backupCount=5)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)
    
    # Also log to console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    logger.addHandler(console_handler)
    
    return logger


def connect_to_cloudwatch(settings, logger):
    """Try to connect to AWS CloudWatch for logging"""
    if not settings['use_cloudwatch'] or not boto3:
        return None
    
    try:
        # Connect to AWS
        client = boto3.client('logs', region_name=settings['aws_region'])
        
        # Make sure our log group exists
        try:
            client.create_log_group(logGroupName=settings['log_group'])
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                raise
        
        # Make sure our log stream exists  
        try:
            client.create_log_stream(logGroupName=settings['log_group'],
                                   logStreamName=settings['log_stream'])
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                raise
        
        logger.info("Connected to CloudWatch for logging")
        return client
    
    except Exception as e:
        logger.warning(f"Couldn't connect to CloudWatch: {e}")
        return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
def send_slack_notification(settings, message, color="good", logger=None):
    """Send a notification to Slack using webhook"""
    if not settings['use_slack'] or not settings['slack_webhook_url']:
        return True
    
    try:
        # Prepare the Slack message payload
        slack_data = {
            "channel": settings['slack_channel'],
            "username": settings['slack_username'],
            "icon_emoji": settings['slack_icon'],
            "attachments": [{
                "color": color,
                "text": message,
                "ts": int(datetime.now().timestamp())
            }]
        }
        
        # Convert to JSON and encode
        json_data = json.dumps(slack_data).encode('utf-8')
        
        # Create the request
        request = urllib.request.Request(
            settings['slack_webhook_url'],
            data=json_data,
            headers={'Content-Type': 'application/json'}
        )
        
        # Send the request
        with urllib.request.urlopen(request, timeout=settings['slack_timeout']) as response:
            if response.getcode() == 200:
                if logger:
                    logger.debug("Slack notification sent successfully")
                return True
            else:
                error_msg = f"Slack webhook returned status {response.getcode()}"
                if logger:
                    logger.warning(error_msg)
                raise Exception(error_msg)
    
    except Exception as e:
        error_msg = f"Failed to send Slack notification: {e}"
        if logger:
            logger.warning(error_msg)
        raise Exception(error_msg)


def send_to_cloudwatch(cloudwatch, settings, message, level, door_is_open, in_maintenance, logger):
    """Send a message to CloudWatch if it's available"""
    if not cloudwatch:
        return
    
    try:
        now = datetime.now()
        
        # Create the log entry
        log_entry = {
            'timestamp': int(now.timestamp() * 1000),
            'message': json.dumps({
                'when': now.isoformat(),
                'readable_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                'level': level,
                'message': message,
                'door_state': 'OPEN' if door_is_open else 'CLOSED',
                'maintenance_mode': in_maintenance
            })
        }
        
        # Send it to CloudWatch
        cloudwatch.put_log_events(
            logGroupName=settings['log_group'],
            logStreamName=settings['log_stream'],
            logEvents=[log_entry]
        )
        
    except Exception as e:
        logger.warning(f"Failed to send to CloudWatch: {e}")


def setup_door_sensor(settings, logger):
    """Setup the GPIO pin for the door sensor"""
    if not GPIO:
        logger.warning("GPIO not available - running in test mode")
        return None
    
    GPIO.setmode(GPIO.BCM)
    
    if settings['use_pullup']:
        GPIO.setup(settings['door_pin'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
        door_closed_value = GPIO.LOW
    else:
        GPIO.setup(settings['door_pin'], GPIO.IN, pull_up_down=GPIO.PUD_DOWN)  
        door_closed_value = GPIO.HIGH
    
    logger.info(f"Door sensor setup on GPIO pin {settings['door_pin']}")
    return door_closed_value


def remember_door_state():
    """Load the last known door state from disk"""
    memory = {
        'door_is_open': False,
        'last_changed': datetime.now().isoformat(),
        'last_alert_sent': None,
        'been_initialized': False
    }
    
    try:
        memory_file = Path("/var/lib/door_sensor/state.json")
        if memory_file.exists():
            saved_memory = json.loads(memory_file.read_text())
            memory.update(saved_memory)
            memory['been_initialized'] = True
    except Exception as e:
        logging.warning(f"Couldn't load saved state: {e}")
    
    return memory


def save_door_state(memory):
    """Save the current door state to disk"""
    try:
        memory_file = Path("/var/lib/door_sensor/state.json")
        memory_file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        memory_file.write_text(json.dumps(memory, indent=2))
    except Exception as e:
        logging.error(f"Couldn't save state: {e}")


def check_if_maintenance_mode():
    """See if we're in maintenance mode by checking a simple file"""
    try:
        mode_file = Path("/tmp/state")
        if mode_file.exists():
            content = mode_file.read_text().strip().upper()
            return content == "MAINT"
    except:
        pass
    return False


def read_door_sensor(settings, door_closed_value):
    """Check if the door is currently open"""
    if not GPIO:
        return False  # Test mode - assume door is closed
    
    current_value = GPIO.input(settings['door_pin'])
    return current_value != door_closed_value


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def run_remote_command(settings, command, logger):
    """Run a command on the remote server via SSH"""
    if not command:
        return True
    
    # Build the SSH command
    ssh_cmd = [
        "ssh", 
        "-i", settings['key_file'],
        "-p", str(settings['ssh_port']),
        "-o", f"ConnectTimeout={settings['timeout']}",
        "-o", "BatchMode=yes",
        settings['server'],
        command
    ]
    
    # Run it
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, 
                          timeout=settings['timeout'] + 5)
    
    if result.returncode == 0:
        logger.info(f"Successfully ran remote command")
        return True
    else:
        error_msg = f"Remote command failed: {result.stderr}"
        logger.error(error_msg)
        raise Exception(error_msg)


def handle_door_change(settings, memory, door_is_open, in_maintenance, cloudwatch, logger):
    """Deal with the door changing state (opening or closing)"""
    right_now = datetime.now()
    time_stamp = right_now.strftime('%Y-%m-%d %H:%M:%S')
    
    # Update our memory
    memory.update({
        'door_is_open': door_is_open,
        'last_changed': right_now.isoformat(),
        'been_initialized': True
    })
    
    # Figure out what happened
    what_happened = "OPENED" if door_is_open else "CLOSED"
    
    # Log it appropriately (opening is more serious than closing)
    if door_is_open:
        logger.warning(f"Door {what_happened}")
    else:
        logger.info(f"Door {what_happened}")
    
    # Figure out what command to run and what kind of alert to send
    command_to_run = None
    alert_type = "ALERT"
    
    if door_is_open:
        if in_maintenance and settings['maintenance_open_cmd']:
            command_to_run = settings['maintenance_open_cmd']
            alert_type = "MAINTENANCE ALERT"
        else:
            command_to_run = settings['door_open_cmd']
    elif not in_maintenance:
        # Door closed and we're not in maintenance mode
        command_to_run = settings['door_closed_cmd']
    
    # Show the alert
    mode_note = " (MAINTENANCE MODE)" if in_maintenance else ""
    console_message = f"[{time_stamp}] {alert_type}: Door {what_happened}{mode_note}"
    print(console_message)
    
    # Prepare Slack notification
    slack_should_notify = False
    slack_color = "good"
    slack_message = f"🚪 Door {what_happened.lower()}{mode_note.lower()}"
    
    if door_is_open:
        slack_should_notify = settings['slack_notify_open']
        slack_color = "warning" if not in_maintenance else "danger"
        if in_maintenance and settings['slack_notify_maintenance']:
            slack_message = f"🔧 Maintenance Alert: Door {what_happened.lower()}"
    else:
        slack_should_notify = settings['slack_notify_closed']
        slack_color = "good"
    
    # Send Slack notification if configured
    if slack_should_notify:
        try:
            send_slack_notification(settings, slack_message, slack_color, logger)
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")
    
    # Log to CloudWatch
    log_level = 'WARNING' if door_is_open else 'INFO'
    send_to_cloudwatch(cloudwatch, settings, console_message,
                      log_level, door_is_open, in_maintenance, logger)
    
    # Run the appropriate command if we have one
    if command_to_run:
        try:
            if run_remote_command(settings, command_to_run, logger):
                memory['last_alert_sent'] = right_now.isoformat()
                command_type = "maintenance" if (door_is_open and in_maintenance) else "normal"
                success_msg = f"[{time_stamp}] {alert_type} sent for door {what_happened} ({command_type} command)"
                send_to_cloudwatch(cloudwatch, settings, success_msg,
                                 'INFO', door_is_open, in_maintenance, logger)
        except Exception as e:
            error_msg = f"Failed to run remote command: {e}"
            logger.error(error_msg)
            send_to_cloudwatch(cloudwatch, settings, f"[{time_stamp}] {error_msg}",
                             'ERROR', door_is_open, in_maintenance, logger)
            
            # Send Slack notification about command failure
            try:
                slack_error_msg = f"❌ Failed to execute command for door {what_happened.lower()}: {e}"
                send_slack_notification(settings, slack_error_msg, "danger", logger)
            except:
                pass  # Don't let Slack failures cascade
    
    # Save our current state
    save_door_state(memory)
    return right_now


def make_sure_only_one_copy_running(logger):
    """Prevent multiple copies of this script from running at the same time"""
    lock_file = Path("/var/run/door_sensor.pid")
    
    if lock_file.exists():
        try:
            existing_pid = int(lock_file.read_text().strip())
            os.kill(existing_pid, 0)  # Check if that process still exists
            logger.error(f"Another copy is already running (PID: {existing_pid})")
            return False
        except (ValueError, OSError):
            # The old process is gone, clean up the stale lock file
            lock_file.unlink(missing_ok=True)
    
    # Create our lock file
    lock_file.write_text(str(os.getpid()))
    return True


def watch_the_door():
    """Main function - this does all the work"""
    # Get everything set up
    settings = read_config_file()
    logger = setup_file_logging()
    cloudwatch = connect_to_cloudwatch(settings, logger)
    door_closed_value = setup_door_sensor(settings, logger)
    memory = remember_door_state()
    
    # Make sure we're the only copy running
    if not make_sure_only_one_copy_running(logger):
        sys.exit(1)
    
    # Set up signal handlers so we can shut down cleanly
    keep_running = [True]  # Use a list so we can modify it from inside the function
    
    def shutdown_gracefully(signal_num, frame):
        print("\nShutting down...")
        keep_running[0] = False
    
    signal.signal(signal.SIGINT, shutdown_gracefully)   # Ctrl+C
    signal.signal(signal.SIGTERM, shutdown_gracefully)  # Kill command
    
    try:
        # Start up
        start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        startup_msg = f"[{start_time}] Door monitor starting up"
        logger.info("Door monitor starting up")
        send_to_cloudwatch(cloudwatch, settings, startup_msg,
                          'INFO', memory['door_is_open'], False, logger)
        
        # Send startup Slack notification
        if settings['slack_notify_startup']:
            try:
                slack_startup_msg = f"🟢 Door Monitor started at {start_time}"
                send_slack_notification(settings, slack_startup_msg, "good", logger)
            except Exception as e:
                logger.warning(f"Startup Slack notification failed: {e}")
        
        # Check current state
        in_maintenance = check_if_maintenance_mode()
        door_is_open = read_door_sensor(settings, door_closed_value)
        last_change_time = datetime.now()
        
        # Handle initial state if this is our first time running
        if not memory['been_initialized']:
            initial_state = 'OPEN' if door_is_open else 'CLOSED'
            init_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            init_msg = f"[{init_time}] Starting up - door is {initial_state}"
            logger.info(f"Starting up - door is {initial_state}")
            send_to_cloudwatch(cloudwatch, settings, init_msg,
                             'INFO', door_is_open, in_maintenance, logger)
            
            if door_is_open:
                # Door is open on startup - treat this as a change
                last_change_time = handle_door_change(settings, memory, door_is_open,
                                                    in_maintenance, cloudwatch, logger)
            else:
                # Door is closed on startup - just remember this state
                memory.update({'door_is_open': door_is_open, 'been_initialized': True})
                save_door_state(memory)
        
        last_maintenance_check = datetime.now()
        
        print(f"Monitoring door sensor on GPIO pin {settings['door_pin']}")
        print("Press Ctrl+C to stop")
        
        # Main loop - keep watching the door
        while keep_running[0]:
            # Check if we've switched maintenance mode (every 30 seconds)
            if datetime.now() - last_maintenance_check > timedelta(seconds=30):
                in_maintenance = check_if_maintenance_mode()
                last_maintenance_check = datetime.now()
            
            # Check the door
            door_is_open = read_door_sensor(settings, door_closed_value)
            
            # Has the door state changed?
            door_state_changed = memory['door_is_open'] != door_is_open
            enough_time_passed = datetime.now() - last_change_time > timedelta(seconds=settings['ignore_bounces_for'])
            
            if door_state_changed and enough_time_passed:
                last_change_time = handle_door_change(settings, memory, door_is_open,
                                                    in_maintenance, cloudwatch, logger)
            
            # Wait a bit before checking again
            time.sleep(settings['check_every'])
    
    except KeyboardInterrupt:
        logger.info("Got keyboard interrupt")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise
    finally:
        # Clean up
        if GPIO:
            GPIO.cleanup()
        save_door_state(memory)
        Path("/var/run/door_sensor.pid").unlink(missing_ok=True)
        
        shutdown_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        shutdown_msg = f"[{shutdown_time}] Door monitor shut down"
        logger.info("Door monitor shut down")
        send_to_cloudwatch(cloudwatch, settings, shutdown_msg,
                          'INFO', memory.get('door_is_open', False), check_if_maintenance_mode(), logger)
        
        # Send shutdown Slack notification
        if settings['slack_notify_shutdown']:
            try:
                slack_shutdown_msg = f"🔴 Door Monitor shut down at {shutdown_time}"
                send_slack_notification(settings, slack_shutdown_msg, "warning", logger)
            except Exception as e:
                logger.warning(f"Shutdown Slack notification failed: {e}")


def main():
    """Entry point when script is run directly"""
    try:
        watch_the_door()
    except Exception as e:
        print(f"Fatal error: {e}")
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
