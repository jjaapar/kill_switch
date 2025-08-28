#!/usr/bin/env python3
"""
Door Sensor Daemon - Raspberry Pi 5 Optimized Version
A secure, robust, and efficient door monitoring system with enhanced Pi 5 features.
"""

import RPi.GPIO as GPIO
import time
import logging
import subprocess
import sys
import os
import json
import signal
import asyncio
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timedelta
from configparser import ConfigParser
from tenacity import retry, stop_after_attempt, wait_exponential

# Pi 5 Specific Imports
from gpiozero import Button  # More efficient GPIO handling
import psutil  # For advanced system monitoring

# Updated Configuration Paths for Pi 5
CONFIG_FILE = "/etc/door_sensor/config.ini"
STATE_FILE = "/var/lib/door_sensor/state.json"
LOG_FILE = "/var/log/door_sensor.log"
PID_FILE = "/var/run/door_sensor.pid"

# Pi 5 Specific Constants
PI5_TEMP_FILE = "/sys/class/thermal/thermal_zone0/temp"
DEFAULT_GPIO_CHIP = "/dev/gpiochip0"

class SystemMonitor:
    """Pi 5 specific system monitoring"""
    @staticmethod
    def get_cpu_temp():
        try:
            with open(PI5_TEMP_FILE, 'r') as f:
                return float(f.read()) / 1000.0
        except Exception as e:
            logging.error(f"Failed to read CPU temperature: {e}")
            return None

    @staticmethod
    def get_system_stats():
        return {
            'cpu_usage': psutil.cpu_percent(),
            'memory_usage': psutil.virtual_memory().percent,
            'cpu_temp': SystemMonitor.get_cpu_temp()
        }

# [Previous DoorState class remains unchanged]

class DoorSensorDaemon:
    def __init__(self, config_file=CONFIG_FILE):
        """Initialize the door sensor daemon with Pi 5 optimizations."""
        self.config = self.load_config(config_file)
        self.setup_logging()
        self.setup_gpio()
        self.state = DoorState(STATE_FILE)
        self.setup_monitoring()
        self.system_monitor = SystemMonitor()
        self.setup_async_loop()

    def setup_gpio(self):
        """Initialize GPIO settings with Pi 5 optimizations."""
        self.sensor_pin = self.config['gpio']['sensor_pin']
        self.pull_up = self.config['gpio']['pull_up']

        # Use gpiozero for more efficient GPIO handling
        self.button = Button(
            pin=self.sensor_pin,
            pull_up=self.pull_up,
            bounce_time=self.config['monitoring']['debounce_delay']
        )

        self.closed_state = GPIO.LOW if self.pull_up else GPIO.HIGH
        self.logger.info(f"GPIO initialized: pin={self.sensor_pin}, pull_up={self.pull_up}")

    def setup_async_loop(self):
        """Setup async event loop for concurrent operations"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    async def monitor_system_health(self):
        """Async system health monitoring"""
        while self.running:
            stats = SystemMonitor.get_system_stats()
            self.logger.info(f"System Stats - CPU: {stats['cpu_usage']}%, "
                           f"Memory: {stats['memory_usage']}%, "
                           f"Temperature: {stats['cpu_temp']}°C")
            await asyncio.sleep(self.config['monitoring']['health_check_interval'])

    async def async_ssh_command(self, command):
        """Execute SSH command asynchronously"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-i", self.config['ssh']['key_path'],
                "-p", str(self.config['ssh']['port']),
                "-o", f"ConnectTimeout={self.config['ssh']['timeout']}",
                "-o", "BatchMode=yes",
                self.config['ssh']['host'],
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                self.logger.info("SSH command executed successfully")
                return True
            else:
                self.logger.error(f"SSH command failed: {stderr.decode()}")
                return False

        except Exception as e:
            self.logger.error(f"Error executing SSH command: {e}")
            return False

    async def async_monitor(self):
        """Async main monitoring loop"""
        health_monitor = asyncio.create_task(self.monitor_system_health())
        
        try:
            while self.running:
                current_open = not self.button.is_pressed
                
                if self.should_send_alert(current_open):
                    command = self.config['commands']['door_open' if current_open else 'door_closed']
                    success = await self.async_ssh_command(command)
                    self.state.update_state(current_open, alert_sent=success)

                await asyncio.sleep(self.config['monitoring']['alert_delay'])

        except Exception as e:
            self.logger.error(f"Error in monitoring loop: {e}")
        finally:
            health_monitor.cancel()

    def monitor(self):
        """Main monitoring entry point"""
        if not self.check_single_instance():
            sys.exit(1)

        self.create_pid_file()
        self.running = True
        
        try:
            self.loop.run_until_complete(self.async_monitor())
        finally:
            self.cleanup()

    def cleanup(self):
        """Enhanced cleanup with Pi 5 specific resources"""
        self.button.close()  # Clean up gpiozero resources
        self.loop.close()    # Clean up async loop
        self.remove_pid_file()
        self.state.save_state()
        self.logger.info("Cleanup completed")

# [Main function remains unchanged]
