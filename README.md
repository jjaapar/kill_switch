# kill_switch
Setup Instructions

Install required packages:
sudo apt-get update
sudo apt-get install python3-rpi.gpio


Set up SSH key authentication:
# Generate SSH key (if you haven't already)
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""

# Copy public key to remote server
ssh-copy-id -i ~/.ssh/id_rsa.pub user@remote-server.com

Make the script executable:
chmod +x /home/pi/door_sensor_daemon.py

Create log directory and set permissions:
sudo mkdir -p /var/log
sudo touch /var/log/door_sensor.log
sudo chown pi:pi /var/log/door_sensor.log
sudo mkdir -p /var/run

Install the systemd service:
sudo cp /home/pi/door_sensor_daemon.service /etc/systemd/system/
sudo systemctl daemon-reload

Enable and start the service:
sudo systemctl enable door-sensor.service
sudo systemctl start door-sensor.service

Check service status:
sudo systemctl status door-sensor.service
journalctl -u door-sensor.service -f

Usage Commands
# Start the service
sudo systemctl start door-sensor.service

# Stop the service
sudo systemctl stop door-sensor.service

# Restart the service
sudo systemctl restart door-sensor.service

# Check status
sudo systemctl status door-sensor.service

# View logs
journalctl -u door-sensor.service -f
