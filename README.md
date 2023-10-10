# Vehicle Gate Controller

## Status
Working prototype. Documentation is limited and untested, a working knowledge of linux is recommended, no guidance is provided on setting file and directory permissions etc.

## Overview

This project is designed to run on a Raspberry Pi and serves as a vehicle gate controller for an electric gate. It receives still images of vehicles approaching an electric gate from a CCTV camera which uploads the images via FTP. It uses the PlateRecognizer API to attempt to detect vehicle license plates, matches them against a list of authorised vehicle license plates, and triggers a relay device to open the gate if a match is found. Additionally, it sends email notifications to alert users when a match is detected or when no match is found.

## Table of Contents

- [Vehicle Gate Controller](#vehicle-gate-controller)
  - [Status](#status)
  - [Overview](#overview)
  - [Table of Contents](#table-of-contents)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
- [Testing](#testing)
- [Troubeshooting](#troubeshooting)
  - [Maintenance](#maintenance)
  - [Useful resources](#useful-resources)

## Prerequisites

Before setting up and running this project, you should have the following:

- Raspberry Pi (with internet connectivity)
- A compatible relay device connected to your Raspberry Pi for controlling the gate e.g. [PiRelay v2 Relay Shield for Raspberry Pi](https://shop.sb-components.co.uk/products/pirelay-relay-board-shield-for-raspberry-pi)
- A PlateRecognizer API token (you can obtain a limited use free account from [PlateRecognizer](https://platerecognizer.com/))
- A Gmail account (or any other SMTP server) to send email notifications.
- Python 3.x installed on your Raspberry Pi.

## Installation

1. Clone the project repository to your Raspberry Pi:

```git clone https://github.com/ciaran-finnegan/gate-controller```

2. Navigate to the project directory, for example:

```cd /opt/gate-controller```

3. Create the log file directory

```mkdir ./logs```

4. Install the required Python packages:

```pip install -r requirements.txt```

5. Install library for your Relay device (if required), for example if you are using the PiRelay v2 Relay Shield for Raspberry Pi you can download the library from this GitHub repository, [Pi Relay v2 Library](https://github.com/sbcshop/PiRelay-V2/blob/main/PiRelay.py)
   
6. Install the physical relay device and test it's operating. For example if you are using the PiRelay v3 Relay
 Shield for Raspberry Pi you can use the python script provided and check the relay activation LEDs on the relay device. Note this will require root privileges.

 ```sudo python test_operate_relay```

7. Before running the project, you need to configure environment variables that contain sensitive information and configuration settings. You can set these environment variables in the systemd service unit file.

Edit the file /etc/systemd/system/file-monitor.service and set the following environment variables:

```
Environment=PLATE_RECOGNIZER_API_TOKEN=REPLACE_WITH_YOUR_API_TOKEN e.g. xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Environment=SMTP_SERVER=REPLACE_WITH_YOUR_SMTP_GATEWAY e.g. smtp.gmail.com
Environment=SMTP_PORT=REPLACE_WITH_YOUR_SMTP_PORT e.g. 587
Environment=SMTP_USERNAME=REPLACE_WITH_YOUR_SMTP_USERNAME e.g. something@gmail.com
Environment=SMTP_PASSWORD=REPLACE_WITH_YOUR_SMTP_PASSWORD
Environment=EMAIL_TO=REPLACE_WITH_YOUR_NOTIFICATIONS_RECIPIENTS_EMAIL_ADDRESS e.g. e.g. something@gmail.com
```

8. Install vsftpd (FTP Server) on Raspberry Pi:

First, you need to install the vsftpd package, which is a popular FTP server for Linux:

```
sudo apt-get update
sudo apt-get install vsftpd
```

Configure vsftpd:

Edit the vsftpd configuration file to enable certain settings and allow passive mode for FTP. Open the configuration file in a text editor:

```
sudo nano /etc/vsftpd.conf
```

Update the following settings in the vsftpd.conf file (if the configuration item isn't present, add it):

```
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=022
dirmessage_enable=YES
xferlog_enable=YES
connect_from_port_20=YES
xferlog_std_format=YES
chroot_local_user=YES
allow_writeable_chroot=YES
pasv_min_port=40000
pasv_max_port=40100
Save and exit the text editor.
```

Create an FTP User:

You should create a dedicated user for FTP access. Replace ftp-user with your desired username:

```
sudo adduser ftp-user
```

Set a password for this user when prompted. 

9. Set Up File Monitoring Service
Edit the file /opt/gate-controller/file_monitor.sh to specify the directory you want to monitor. By default, it is set to /home/ftp-user. If you want to monitor a different directory, update the directory_to_watch variable.

10. Start the File Monitoring Service
Start the file monitoring service by enabling and starting it with the following commands:

```
sudo systemctl enable file-monitor.service
sudo systemctl start file-monitor.service
```
This will monitor the specified directory for new .jpg files and trigger the Python script to process them.

# Testing

That's it! Your vehicle gate controller is now set up and monitoring the specified directory for new images. When new images are detected, the script will attempt to recognize license plates and open the gate as needed. Notifications will be sent to the specified email address.

You can test this by uploading an image with a vehicle licence plate via FTP

You should see the following log entries

```
filemonitor@raspberrypi:/opt/gate-controller$ sudo tail -f /opt/gate-controller/logs/file_monitor.log
Setting up watches.
Watches established.
INFO:db_utils:postgres_host: redacted
INFO:db_utils:postgres_endpoint: redacted
INFO:db_utils:postgres_database: redacted
INFO:db_utils:postgres_port: 5432
INFO:db_utils:postgres_sslmode: require
Traceback (most recent call last):
  File "/opt/gate-controller/check-plate-and-open-gate.py", line 23, in <module>
    from db_utils import log_entry, create_table_sqlite
ImportError: cannot import name 'create_table_sqlite' from 'db_utils' (/opt/gate-controller/db_utils.py)
[2023-10-10 14:12:04] Processing of TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg completed.
[2023-10-10 14:22:38] New .jpg file detected: TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg
INFO:db_utils:postgres_prisma_url: redacted
INFO:db_utils:postgres_url_non_pooling: redacted
INFO:db_utils:postgres_user: default
INFO:db_utils:postgres_host: redacted
INFO:db_utils:postgres_endpoint: redacted
INFO:db_utils:postgres_database: redacted
INFO:db_utils:postgres_port: 5432
INFO:db_utils:postgres_sslmode: require
INFO:root:fuzzy_match_threshold: 70
2023-10-10 14:22:40,402 - INFO - fuzzy_match_threshold: 70
INFO:root:smtp_server: smtp.gmail.com
2023-10-10 14:22:40,403 - INFO - smtp_server: smtp.gmail.com
INFO:root:smtp_port: 587
2023-10-10 14:22:40,404 - INFO - smtp_port: 587
INFO:root:smtp_username: redacted
2023-10-10 14:22:40,405 - INFO - smtp_username: redacted
INFO:root:email_to: redacted
2023-10-10 14:22:40,406 - INFO - email_to: redacted
INFO:db_utils:SQLite db_file_path: /opt/gate-controller/data/gate-controller-database.db
2023-10-10 14:22:40,407 - INFO - SQLite db_file_path: /opt/gate-controller/data/gate-controller-database.db
INFO:db_utils:Database table created successfully.
2023-10-10 14:22:40,409 - INFO - Database table created successfully.
INFO:root:Plate Recognizer API Response Status Code: 201
2023-10-10 14:22:41,320 - INFO - Plate Recognizer API Response Status Code: 201
INFO:root:Plate Recognizer API Response: {"processing_time":72.001,"results":[{"box":{"xmin":615,"ymin":421,"xmax":704,"ymax":454},"plate":"redacted","region":{"code":"be","score":0.451},"score":0.873,"candidates":[{"score":0.873,"plate":"11wh2571"}],"dscore":0.717,"vehicle":{"score":0.666,"type":"Sedan","box":{"xmin":548,"ymin":228,"xmax":893,"ymax":491}}}],"filename":"1322_e90UI_TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg","version":1,"camera_id":null,"timestamp":"2023-10-10T13:22:41.178728Z"}
2023-10-10 14:22:41,323 - INFO - Plate Recognizer API Response: {"processing_time":72.001,"results":[{"box":{"xmin":615,"ymin":421,"xmax":704,"ymax":454},"plate":"11wh2571","region":{"code":"be","score":0.451},"score":0.873,"candidates":[{"score":0.873,"plate":"11wh2571"}],"dscore":0.717,"vehicle":{"score":0.666,"type":"Sedan","box":{"xmin":548,"ymin":228,"xmax":893,"ymax":491}}}],"filename":"1322_e90UI_TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg","version":1,"camera_id":null,"timestamp":"2023-10-10T13:22:41.178728Z"}
INFO:root:CSV data for authorised licence plate numbers:
2023-10-10 14:22:41,325 - INFO - CSV data for authorised licence plate numbers:
INFO:root:{'name': '"redacted"', 'colour': '"black"', 'make': '"renault"', 'model': '"unknown"'}
2023-10-10 14:22:41,326 - INFO - {'name': '"redacted"', 'colour': '"black"', 'make': '"renault"', 'model': '"unknown"'}
INFO:root:Match found for vehicle license plate number: redacted, Registered to: {'name': '"redacted"', 'colour': '"silver"', 'make': '"skoda"', 'model': '"octavia"'}
2023-10-10 14:22:41,329 - INFO - Match found for vehicle license plate number: redacted1, Registered to: {'name': '"redacted"', 'colour': '"silver"', 'make': '"skoda"', 'model': '"octavia"'}
INFO:root:Checking for recent gate opening event
2023-10-10 14:22:41,331 - INFO - Checking for recent gate opening event
INFO:root:Current time: 1696944161.3319693
2023-10-10 14:22:41,332 - INFO - Current time: 1696944161.3319693
INFO:root:Time 20 seconds ago: 2023-10-10 14:22:21
2023-10-10 14:22:41,333 - INFO - Time 20 seconds ago: 2023-10-10 14:22:21
INFO:root:SQL Query: SELECT COUNT(*) FROM log WHERE gate_opened="Yes" AND timestamp > 2023-10-10 14:22:21
2023-10-10 14:22:41,334 - INFO - SQL Query: SELECT COUNT(*) FROM log WHERE gate_opened="Yes" AND timestamp > 2023-10-10 14:22:21
INFO:root:Count of matching values in database log table: 0
2023-10-10 14:22:41,335 - INFO - Count of matching values in database log table: 0
INFO:root:PiRelay call made to open the gate
2023-10-10 14:22:43,339 - INFO - PiRelay call made to open the gate
ERROR:root:Error processing image file: log_entry() got multiple values for argument 'fuzzy_match'
2023-10-10 14:22:43,340 - ERROR - Error processing image file: log_entry() got multiple values for argument 'fuzzy_match'
RELAY1 - ON
RELAY1 - OFF
[2023-10-10 14:22:43] Processing of TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg completed.
```

# Troubeshooting

1. Check the relay device is functioning
   
```
sudo python /opt/gate-controller/test_relay.py
```

2. Check the systemd file-monitor service status and logs
   
```
sudo systemctl daemon-reload
sudo systemctl restart file-monitor.service
sudo systemctl status file-monitor.service
sudo tail -f /opt/gate-controller/logs/file-monitor.log
```

3. Check the python script logs

```
sudo tail -f check-plate-and-open-gate.log
```

4. Check permissions

```
ls -l /opt/gate-controller/file_monitor.sh
chmod +x /opt/gate-controller/file_monitor.sh
ls -l /opt/gate-controller/
sudo chown -R filemonitor:root /opt/gate-controller
sudo chmod -R 775 /opt/gate-controller

```

## Maintenance

To maintain this project:

- Regularly check for updates and security patches for the Raspberry Pi and the installed packages.

- Update the authorized license plates list (`authorised_licence_plates.csv`) as needed. 

- Monitor the email notifications to ensure the service is working as expected

- Review the logs to troubleshoot any errors or unusual activity.

## Useful resources
The [IPCamTalk Forum](https://ipcamtalk.com) has lots of useful information and support for commonly used CCTV cameras

[TOpens](https://topens.com) offer low cost electric gate systems and accessories

[Faac](https://www.faac.co.uk) offer high quality electric gate systems and accessories

[Pi Relay v2 Library](https://github.com/sbcshop/PiRelay-V2/blob/main/PiRelay.py) offer a low cost Raspberry Pi Hat with 4 integrated relays