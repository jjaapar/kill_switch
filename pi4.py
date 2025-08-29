#!/usr/bin/env python3
"""
Simple Door Sensor Test Script
For Raspberry Pi 4 using GPIO pin 18
"""

import RPi.GPIO as GPIO
import time

# Constants
SENSOR_PIN = 18
DELAY = 0.1  # 100ms delay between readings

def setup():
    """Initialize GPIO settings"""
    # Use BCM GPIO numbering
    GPIO.setmode(GPIO.BCM)
    
    # Set up the sensor pin with pull-up resistor
    # When door is closed, circuit is completed, reading LOW
    # When door is open, circuit is broken, reading HIGH
    GPIO.setup(SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    print("Door Sensor Test Script Started")
    print("Press CTRL+C to exit")
    print("----------------------------")

def read_sensor():
    """Read the door state"""
    # With pull-up resistor:
    # GPIO.LOW (0) = Door Closed (Circuit completed)
    # GPIO.HIGH (1) = Door Open (Circuit broken)
    return GPIO.input(SENSOR_PIN)

def main():
    try:
        setup()
        previous_state = None
        
        while True:
            current_state = read_sensor()
            
            # Only print if state has changed
            if current_state != previous_state:
                if current_state == GPIO.HIGH:
                    print("Door is OPEN")
                else:
                    print("Door is CLOSED")
                previous_state = current_state
            
            time.sleep(DELAY)
            
    except KeyboardInterrupt:
        print("\nProgram stopped by user")
    except Exception as e:
        print(f"Error occurred: {e}")
    finally:
        GPIO.cleanup()
        print("GPIO cleanup completed")

if __name__ == "__main__":
    main()
