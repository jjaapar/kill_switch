# /etc/door_sensor/config.ini
# Door Sensor Configuration with CloudWatch Integration

[gpio]
sensor_pin = 24
pull_up = true

[ssh]
host = 192.168.1.100
port = 22
key_path = ~/.ssh/door_sensor_key
timeout = 10

[commands]
door_open = echo "Door opened at $(date)" >> /tmp/door_alerts.log && curl -X POST https://api.example.com/door/open
door_closed = echo "Door closed at $(date)" >> /tmp/door_alerts.log && curl -X POST https://api.example.com/door/closed

[monitoring]
; seconds to wait before alerting for open door
open_alert_delay = 10.0 
alert_delay = 0.5
debounce_delay = 1.0
health_check_interval = 300

[cloudwatch]
# Enable CloudWatch integration
enabled = true

# AWS Credentials (alternatively use IAM roles, AWS CLI config, or environment variables)
aws_access_key_id = AKIA1234567890EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
aws_region = us-west-2

# CloudWatch configuration
namespace = DoorSensor/SmartHome
log_group = /aws/iot/door-sensor
device_id = front-door-001

# Health check interval for CloudWatch (seconds) - less frequent than local health checks
health_interval = 900
