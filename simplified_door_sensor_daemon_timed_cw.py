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
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None

from tenacity import retry, stop_after_attempt, wait_exponential


def load_config(config_file="/etc/door_sensor/config.ini"):
    """Load configuration from file"""
    if not Path(config_file).exists():
        raise Exception(f"Config file not found: {config_file}")
    
    config = ConfigParser()
    config.read(config_file)
    
    # Set defaults and read all at once
    defaults = {
        'gpio_pin': 17, 'gpio_pull_up': True, 'ssh_port': 22, 'ssh_timeout': 10,
        'alert_delay': 0.5, 'debounce_delay': 1.0, 'cloudwatch_enabled': False,
        'cloudwatch_log_group': 'door-sensor', 'cloudwatch_log_stream': 'door-events',
        'cloudwatch_region': 'us-east-1'
    }
    
    result = {}
    for key, default in defaults.items():
        section, option = key.split('_', 1) if '_' in key else ('DEFAULT', key)
        if section == 'cloudwatch':
            section = 'cloudwatch'
        elif section in ['gpio', 'ssh', 'alert', 'debounce']:
            section = section
        else:
            section = 'monitoring'
        
        if isinstance(default, bool):
            result[key] = config.getboolean(section, option, fallback=default)
        elif isinstance(default, int):
            result[key] = config.getint(section, option, fallback=default)
        elif isinstance(default, float):
            result[key] = config.getfloat(section, option, fallback=default)
        else:
            result[key] = config.get(section, option, fallback=default)
    
    # Required string configs
    for key in ['ssh_host', 'ssh_key', 'cmd_open', 'cmd_closed']:
        section = 'ssh' if key.startswith('ssh_') else 'commands'
        result[key] = config.get(section, key.replace('ssh_', '').replace('cmd_', ''))
    
    result['ssh_key'] = os.path.expanduser(result['ssh_key'])
    result['cmd_open_maint'] = config.get('commands', 'door_open_maint', fallback=None)
    
    return result


def setup_logging():
    """Setup logging configuration"""
    Path("/var/log").mkdir(exist_ok=True)
    logger = logging.getLogger('DoorSensor')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    handler = RotatingFileHandler("/var/log/door_sensor.log", maxBytes=1024*1024, backupCount=5)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    return logger


def setup_cloudwatch(config, logger):
    """Initialize CloudWatch client"""
    if not config['cloudwatch_enabled'] or not boto3:
        return None
    
    try:
        client = boto3.client('logs', region_name=config['cloudwatch_region'])
        
        # Ensure resources exist
        for resource_type, name in [('log_group', config['cloudwatch_log_group']), 
                                   ('log_stream', config['cloudwatch_log_stream'])]:
            try:
                if resource_type == 'log_group':
                    client.create_log_group(logGroupName=name)
                else:
                    client.create_log_stream(logGroupName=config['cloudwatch_log_group'], 
                                           logStreamName=name)
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                    raise
        
        logger.info("CloudWatch logging initialized")
        return client
    except Exception as e:
        logger.warning(f"CloudWatch initialization failed: {e}")
        return None


def log_to_cloudwatch(cloudwatch, config, message, level, door_open, maintenance_mode, logger):
    """Send message to CloudWatch"""
    if not cloudwatch:
        return
    
    try:
        now = datetime.now()
        log_event = {
            'timestamp': int(now.timestamp() * 1000),
            'message': json.dumps({
                'timestamp': now.isoformat(),
                'formatted_timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
                'device_id': config.get('device_id', 'unknown'),
                'level': level,
                'message': message,
                'door_state': 'OPEN' if door_open else 'CLOSED',
                'maintenance_mode': maintenance_mode
            })
        }
        
        # Get sequence token if available
        sequence_token = None
        try:
            response = cloudwatch.describe_log_streams(
                logGroupName=config['cloudwatch_log_group'],
                logStreamNamePrefix=config['cloudwatch_log_stream']
            )
            for stream in response.get('logStreams', []):
                if stream['logStreamName'] == config['cloudwatch_log_stream']:
                    sequence_token = stream.get('uploadSequenceToken')
                    break
        except ClientError:
            pass
        
        # Send log event
        kwargs = {
            'logGroupName': config['cloudwatch_log_group'],
            'logStreamName': config['cloudwatch_log_stream'],
            'logEvents': [log_event]
        }
        if sequence_token:
            kwargs['sequenceToken'] = sequence_token
            
        cloudwatch.put_log_events(**kwargs)
        
    except Exception as e:
        logger.warning(f"CloudWatch logging failed: {e}")


def setup_gpio(config, logger):
    """Setup GPIO pins"""
    if not GPIO:
        logger.warning("RPi.GPIO not available - test mode")
        return None
    
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(config['gpio_pin'], GPIO.IN, 
              pull_up_down=GPIO.PUD_UP if config['gpio_pull_up'] else GPIO.PUD_DOWN)
    
    closed_state = GPIO.LOW if config['gpio_pull_up'] else GPIO.HIGH
    return closed_state


def load_state():
    """Load persistent state from file"""
    state = {'door_open': False, 'last_change': datetime.now().isoformat(), 
             'last_alert': None, 'initialized': False}
    
    try:
        state_file = Path("/var/lib/door_sensor/state.json")
        if state_file.exists():
            state.update(json.loads(state_file.read_text()))
            state['initialized'] = True
    except Exception as e:
        logging.warning(f"Could not load state: {e}")
    
    return state


def save_state(state):
    """Save persistent state to file"""
    try:
        state_file = Path("/var/lib/door_sensor/state.json")
        state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logging.error(f"Could not save state: {e}")


def check_maintenance_mode():
    """Check if maintenance mode is enabled"""
    try:
        return Path("/tmp/state").read_text().strip().upper() == "MAINT"
    except:
        return False


def is_door_open(config, closed_state):
    """Check if door is currently open"""
    return GPIO and GPIO.input(config['gpio_pin']) != closed_state


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def run_ssh_command(config, command, logger):
    """Execute SSH command with retry logic"""
    if not command:
        return True
        
    cmd = ["ssh", "-i", config['ssh_key'], "-p", str(config['ssh_port']),
           "-o", f"ConnectTimeout={config['ssh_timeout']}", "-o", "BatchMode=yes",
           config['ssh_host'], command]
    
    result = subprocess.run(cmd, capture_output=True, text=True, 
                          timeout=config['ssh_timeout'] + 5)
    
    if result.returncode == 0:
        logger.info("SSH command executed successfully")
        return True
    else:
        logger.error(f"SSH command failed: {result.stderr}")
        raise Exception(f"SSH command failed: {result.stderr}")


def handle_door_change(config, state, current_open, maintenance_mode, cloudwatch, logger):
    """Handle door state change"""
    now = datetime.now()
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    # Update state
    state.update({
        'door_open': current_open,
        'last_change': now.isoformat(),
        'initialized': True
    })
    
    state_text = "OPEN" if current_open else "CLOSED"
    log_level = logging.WARNING if current_open else logging.INFO
    logger.log(log_level, f"Door {state_text}")
    
    # Determine command and alert type
    command = None
    alert_type = "ALERT"
    
    if current_open:  # Door open
        if maintenance_mode and config['cmd_open_maint']:
            command = config['cmd_open_maint']
            alert_type = "MAINT ALERT"
        else:
            command = config['cmd_open']
    elif not maintenance_mode:  # Door closed, not in maintenance
        command = config['cmd_closed']
    
    # Print alert
    mode_suffix = " (MAINTENANCE MODE)" if maintenance_mode else ""
    print(f"[{timestamp_str}] {alert_type}: Door {state_text}{mode_suffix}")
    
    # Log to CloudWatch
    cloudwatch_level = 'WARNING' if current_open else 'INFO'
    log_to_cloudwatch(cloudwatch, config, f"[{timestamp_str}] Door {state_text}{mode_suffix}", 
                     cloudwatch_level, current_open, maintenance_mode, logger)
    
    # Execute SSH command
    if command:
        try:
            if run_ssh_command(config, command, logger):
                state['last_alert'] = now.isoformat()
                cmd_type = "maintenance" if (current_open and maintenance_mode) else "normal"
                log_to_cloudwatch(cloudwatch, config, 
                                f"[{timestamp_str}] {alert_type} sent for door {state_text} ({cmd_type} command)", 
                                'INFO', current_open, maintenance_mode, logger)
        except Exception as e:
            error_msg = f"SSH command failed: {e}"
            logger.error(error_msg)
            log_to_cloudwatch(cloudwatch, config, f"[{timestamp_str}] {error_msg}", 
                            'ERROR', current_open, maintenance_mode, logger)
    
    save_state(state)
    return now


def ensure_single_instance(logger):
    """Ensure only one instance runs"""
    pid_file = Path("/var/run/door_sensor.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            logger.error(f"Already running (PID: {pid})")
            return False
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
    
    pid_file.write_text(str(os.getpid()))
    return True


def monitor():
    """Main monitoring function"""
    # Initialize components
    config = load_config()
    logger = setup_logging()
    cloudwatch = setup_cloudwatch(config, logger)
    closed_state = setup_gpio(config, logger)
    state = load_state()
    
    if not ensure_single_instance(logger):
        sys.exit(1)

    # Setup signal handlers
    running = [True]  # Use list to allow modification in nested function
    def signal_handler(signum, frame):
        running[0] = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        start_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        logger.info("Starting door sensor monitoring")
        log_to_cloudwatch(cloudwatch, config, f"[{start_timestamp}] Starting door sensor monitoring", 
                         'INFO', state['door_open'], False, logger)
        
        # Initialize
        maintenance_mode = check_maintenance_mode()
        current_open = is_door_open(config, closed_state)
        last_change_time = datetime.now()
        
        if not state['initialized']:
            initial_state = 'OPEN' if current_open else 'CLOSED'
            init_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"Initial door state: {initial_state}")
            log_to_cloudwatch(cloudwatch, config, f"[{init_timestamp}] Initial door state: {initial_state}", 
                            'INFO', current_open, maintenance_mode, logger)
            
            if current_open:
                last_change_time = handle_door_change(config, state, current_open, 
                                                    maintenance_mode, cloudwatch, logger)
            else:
                state.update({'door_open': current_open, 'initialized': True})
                save_state(state)

        last_maintenance_check = datetime.now()
        
        # Main monitoring loop
        while running[0]:
            # Check maintenance mode every 30 seconds
            if datetime.now() - last_maintenance_check > timedelta(seconds=30):
                maintenance_mode = check_maintenance_mode()
                last_maintenance_check = datetime.now()
            
            current_open = is_door_open(config, closed_state)
            
            # Handle state change after debounce
            if (state['door_open'] != current_open and 
                datetime.now() - last_change_time > timedelta(seconds=config['debounce_delay'])):
                last_change_time = handle_door_change(config, state, current_open, 
                                                    maintenance_mode, cloudwatch, logger)
            
            time.sleep(config['alert_delay'])

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise
    finally:
        if GPIO:
            GPIO.cleanup()
        save_state(state)
        Path("/var/run/door_sensor.pid").unlink(missing_ok=True)
        
        shutdown_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        logger.info("Shutdown complete")
        log_to_cloudwatch(cloudwatch, config, f"[{shutdown_timestamp}] Shutdown complete", 
                         'INFO', state.get('door_open', False), check_maintenance_mode(), logger)


def main():
    """Main entry point"""
    try:
        monitor()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
