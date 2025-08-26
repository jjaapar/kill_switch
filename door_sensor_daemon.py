#!/usr/bin/env python3
# door_sensor_daemon.py - Door sensor daemon that triggers SSH commands

import RPi.GPIO as GPIO
import time
import logging
import subprocess
import sys
import os
from pathlib import Path

# Configuration
DOOR_SENSOR_PIN = 17  # GPIO pin connected to the door sensor
PULL_UP_RESISTOR = True  # Set to True if using internal pull-up, False for pull-down
ALERT_DELAY = 0.5  # Delay between checks in seconds
LOG_FILE = "/var/log/door_sensor.log"

# SSH Configuration
SSH_HOST = "user@remote-server.com"  # Format: user@hostname
SSH_PORT = 22
SSH_KEY_PATH = "/home/pi/.ssh/id_rsa"  # Path to SSH private key
SSH_TIMEOUT = 10  # SSH command timeout in seconds

# Commands to run on remote server
SSH_COMMAND_DOOR_OPEN = "echo 'Door opened at $(date)' >> /tmp/door_events.log && systemctl start door-alert.service"
SSH_COMMAND_DOOR_CLOSED = "echo 'Door closed at $(date)' >> /tmp/door_events.log && systemctl stop door-alert.service"

# Daemon configuration
PID_FILE = "/var/run/door_sensor.pid"

class DoorSensorDaemon:
    def __init__(self, sensor_pin, pull_up=True):
        self.sensor_pin = sensor_pin
        self.pull_up = pull_up
        self.door_open = False
        self.running = False
        
        # Set up logging
        self.setup_logging()
        
        # Set up GPIO
        GPIO.setmode(GPIO.BCM)
        if pull_up:
            GPIO.setup(sensor_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.closed_state = GPIO.LOW  # Sensor closed (door closed) = LOW
            logging.info(f"Door sensor set up on GPIO {sensor_pin} with pull-up resistor")
        else:
            GPIO.setup(sensor_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            self.closed_state = GPIO.HIGH  # Sensor closed (door closed) = HIGH
            logging.info(f"Door sensor set up on GPIO {sensor_pin} with pull-down resistor")
    
    def setup_logging(self):
        """Set up logging to file and console."""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(LOG_FILE),
                logging.StreamHandler(sys.stdout)
            ]
        )
    
    def read_sensor(self):
        """Read the current state of the door sensor."""
        return GPIO.input(self.sensor_pin)
    
    def is_door_open(self):
        """Return True if door is open, False if closed."""
        current_state = self.read_sensor()
        return current_state != self.closed_state
    
    def run_ssh_command(self, command):
        """
        Execute SSH command on remote server.
        Returns True if successful, False otherwise.
        """
        try:
            # Build SSH command
            ssh_cmd = [
                "ssh",
                "-i", SSH_KEY_PATH,
                "-p", str(SSH_PORT),
                "-o", "ConnectTimeout=" + str(SSH_TIMEOUT),
                "-o", "StrictHostKeyChecking=no",
                SSH_HOST,
                command
            ]
            
            logging.info(f"Executing SSH command: {command}")
            
            # Execute command
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=SSH_TIMEOUT + 5
            )
            
            if result.returncode == 0:
                logging.info(f"SSH command successful: {result.stdout.strip()}")
                return True
            else:
                logging.error(f"SSH command failed: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logging.error(f"SSH command timed out after {SSH_TIMEOUT} seconds")
            return False
        except Exception as e:
            logging.error(f"Error executing SSH command: {e}")
            return False
    
    def create_pid_file(self):
        """Create PID file to track daemon process."""
        try:
            with open(PID_FILE, 'w') as f:
                f.write(str(os.getpid()))
            logging.info(f"PID file created: {PID_FILE}")
        except Exception as e:
            logging.error(f"Failed to create PID file: {e}")
    
    def remove_pid_file(self):
        """Remove PID file."""
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
                logging.info(f"PID file removed: {PID_FILE}")
        except Exception as e:
            logging.error(f"Failed to remove PID file: {e}")
    
    def check_single_instance(self):
        """Check if another instance is already running."""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    pid = int(f.read().strip())
                
                # Check if process is still running
                os.kill(pid, 0)  # This will raise OSError if process doesn't exist
                logging.error(f"Another instance is already running (PID: {pid})")
                return False
            except (ValueError, OSError):
                # PID file exists but process is not running
                logging.warning("Stale PID file found, removing it")
                self.remove_pid_file()
        
        return True
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logging.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def monitor(self):
        """Monitor the door sensor continuously."""
        if not self.check_single_instance():
            sys.exit(1)
        
        self.create_pid_file()
        self.running = True
        
        logging.info("Starting door sensor daemon...")
        
        # Read initial state
        previous_open = self.is_door_open()
        if previous_open:
            logging.warning("Door is OPEN at startup!")
            self.run_ssh_command(SSH_COMMAND_DOOR_OPEN)
        else:
            logging.info("Door is CLOSED at startup")
            self.run_ssh_command(SSH_COMMAND_DOOR_CLOSED)
        
        try:
            while self.running:
                current_open = self.is_door_open()
                
                # Check if state has changed
                if current_open != previous_open:
                    if current_open:
                        logging.warning("DOOR OPENED!")
                        self.run_ssh_command(SSH_COMMAND_DOOR_OPEN)
                    else:
                        logging.info("Door CLOSED")
                        self.run_ssh_command(SSH_COMMAND_DOOR_CLOSED)
                    
                    previous_open = current_open
                
                time.sleep(ALERT_DELAY)
                
        except Exception as e:
            logging.error(f"Error in monitoring: {e}")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Clean up resources."""
        GPIO.cleanup()
        self.remove_pid_file()
        logging.info("Daemon stopped and resources cleaned up")

def main():
    # Import signal module here to avoid issues in some environments
    import signal
    
    # Create door sensor daemon instance
    door_sensor = DoorSensorDaemon(DOOR_SENSOR_PIN, PULL_UP_RESISTOR)
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, door_sensor.signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, door_sensor.signal_handler)  # Termination signal
    
    # Start monitoring
    door_sensor.monitor()

if __name__ == "__main__":
    main()
