# kill_switch

This set of scripts and config file is for use with the Closed Loop Seco-Larm SM-4601-LQ Magnetic Contact and Raspberry Pi to sense the door state (OPEN/CLOSE). These logs and alerts when the door state changes from OPEN -> CLOSE or CLOSE -> OPEN. 

In this case, it sends a command via ssh to a remote server to trigger a light tower (Patlite LA6-POE) to blink RED.

## Install required packages:

sudo apt-get update
```
sudo apt-get install python3-rpi.gpio
sudo apt-get install python3-tenacity
```


## Set up SSH key authentication:
## Generate SSH key (if you haven't already)

```
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""
```

## Copy public key to remote server
```
ssh-copy-id -i ~/.ssh/id_rsa.pub user@remote-server.com
```

## Make the script executable:
```
chmod +x /home/user_name/door_sensor_daemon.py
```

## Create log directory and set permissions:
```
sudo mkdir -p /var/log
sudo touch /var/log/door_sensor.log
sudo chown user_name:user_name /var/log/door_sensor.log
sudo mkdir -p /var/run/door_sensor
sudo chown user_name:user_name /var/run/door_sensor
```

## Install the systemd service:
```
sudo cp /home/pi/door_sensor_daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## Enable and start the service:
```
sudo systemctl enable door-sensor.service
sudo systemctl start door-sensor.service
```

## Check service status:
```
sudo systemctl status door-sensor.service
journalctl -u door-sensor.service -f
```

# Usage Commands

## Start the service
```
sudo systemctl start door-sensor.service
```

## Stop the service
```
sudo systemctl stop door-sensor.service
```

## Restart the service
```
sudo systemctl restart door-sensor.service
```

## Check status
```
sudo systemctl status door-sensor.service
```

## View logs
```
journalctl -u door-sensor.service -f
```
