#!/usr/bin/env python3
"""
Door Sensor Script for Raspberry Pi 5
Uses gpiod library for GPIO access on Pi 5
"""

import gpiod
import time
import sys

# Configuration
CHIP_NAME = "gpiochip4"  # Pi 5 uses gpiochip4
GPIO_PIN = 17  # The GPIO pin number you're using
PULL_UP = True  # Set to False if using pull-down
CHECK_INTERVAL = 0.5  # seconds

class DoorSensor:
    def __init__(self):
        try:
            # Open GPIO chip
            self.chip = gpiod.Chip(CHIP_NAME)
            
            # Request GPIO line with proper configuration
            config = {
                "consumer": "door_sensor",
                "direction": gpiod.Line.Direction.INPUT,
                "bias": gpiod.Line.Bias.PULL_UP if PULL_UP else gpiod.Line.Bias.PULL_DOWN
            }
            
            self.line = self.chip.get_line(GPIO_PIN)
            self.line.request(**config)
            
        except Exception as e:
            print(f"Error initializing GPIO: {e}")
            sys.exit(1)

    def is_door_open(self):
        """Check if the door is open based on sensor reading"""
        try:
            value = self.line.get_value()
            if PULL_UP:
                return value == 1  # For pull-up, 1 means open
            else:
                return value == 0  # For pull-down, 0 means open
        except Exception as e:
            print(f"Error reading sensor: {e}")
            return None

    def cleanup(self):
        """Release GPIO resources"""
        try:
            self.line.release()
            self.chip.close()
        except Exception as e:
            print(f"Error during cleanup: {e}")

def main():
    """Main function to monitor door state"""
    print("Door Sensor Monitoring Started (Press CTRL+C to exit)")
    print(f"Using GPIO pin {GPIO_PIN} on {CHIP_NAME}")
    print("Configuration: " + ("PULL UP" if PULL_UP else "PULL DOWN"))
    
    door_sensor = DoorSensor()
    last_state = None

    try:
        while True:
            current_state = door_sensor.is_door_open()
            
            if current_state is not None and current_state != last_state:
                state_str = "OPEN" if current_state else "CLOSED"
                print(f"Door is {state_str}")
                last_state = current_state
            
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user")
    finally:
        door_sensor.cleanup()

if __name__ == "__main__":
    main()
