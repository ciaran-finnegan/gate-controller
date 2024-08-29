# Vehicle Gate Controller

## Status
This is a Working prototype. Documentation is limited and untested; a working knowledge of Linux is recommended if you choose to implement.

## Overview

This project runs on a Raspberry Pi and serves as a vehicle gate controller for an electric gate. The system uses images captured by a CCTV camera and uploaded via FTP to recognise vehicle licence plates. It checks these plates against a list of authorised vehicles and triggers a relay to open the gate if a match is found. The system also sends email notifications to alert users when a match is detected or when no match is found.

An alternative AWS hosted version can be found [here](https://github.com/ciaran-finnegan/License-Plate-Recognition-Notifier). This version may introduce additional latency and requires a GSM Gate Opening Relay device to open the gate.

A separate web application, which can be found [here](https://github.com/ciaran-finnegan/gate-controller-refine-front-end), allows users to manage authorised vehicles, view access logs, and configure system settings. The web application connects to Supabase, which hosts the PostgreSQL database and handles authentication, and AWS S3, which stores captured images. The web application is hosted on Netlify, which automatically builds and deploys the site from the GitHub repository. However, these services are not strictly required for basic functionality.

## How It Works

### System Overview

1. **Image Capture and Upload**: A CCTV camera monitors the gate and captures images of approaching vehicles. These images are uploaded to the Raspberry Pi via FTP.

2. **File Monitoring**: A file monitoring script (`file_monitor.sh`) continuously watches a specified directory for new images. When a new image (in `.jpg` format) is detected, the script triggers the `check-plate-and-open-gate.py` script to process the image.

3. **Image Processing and Plate Recognition**:
   - The image is sent to the Plate Recognizer API, which identifies the vehicle's licence plate number.
   - The script logs the API response and extracts the recognised plate number and confidence score.

4. **Fuzzy Matching**:
   - The recognised plate number is compared against a list of authorised plates stored in a CSV file (`authorised_licence_plates.csv`).
   - The fuzzy matching algorithm, using the `fuzzywuzzy` library, calculates a similarity score between the recognised plate and each authorised plate in the list.
   - If the similarity score is above a configurable threshold (default is 70 but can be adjusted via the `FUZZY_MATCH_THRESHOLD` environment variable), the system considers it a match. This allows for slight variations in plate recognition due to factors like image quality or plate damage.

5. **Access Decision**:
   - If a match is found and the vehicle is authorised, the system sends a command to the relay (`PiRelay`) to open the gate for a few seconds and then close it.
   - The event is logged, and an email notification is sent to the specified recipient detailing the gate opening event, including the image and the matched plate information.
   - If no match is found or if the score is below the threshold, the gate remains closed, and a notification is sent stating that access was denied.

6. **Logging and Notifications**:
   - All events are logged in both a local SQLite database and optionally in a remote PostgreSQL database hosted on Supabase.
   - Images can be uploaded to an AWS S3 bucket if configured, allowing remote access through the web application.
   - Notifications include details of the event, such as whether access was granted based on a perfect match or a fuzzy match.

### Fuzzy Matching Explained

Fuzzy matching is used to allow for minor discrepancies in the plate recognition process, such as partial visibility of a plate or varying lighting conditions, headlight glare and rain. The fuzzy matching process uses the `fuzzywuzzy` library to compare the recognised plate against the list of authorised plates with a similarity score ranging from 0 to 100:

- A **perfect match** score of 100 indicates the recognised plate exactly matches an authorised plate.
- A **fuzzy match** score (e.g., 70-99) allows for slight differences but still recognises the plate as authorised, depending on the threshold set.
- If the similarity score is below the threshold, the system treats the plate as unauthorised, and access is denied.

This approach provides flexibility and reduces the chance of rejecting authorised vehicles due to minor recognition errors.

## Web Application UI

The web application provides an intuitive interface for managing gate access. Below is an example of the web application's UI showing the vehicle access log:

![Gate Access Manager](https://github.com/ciaran-finnegan/gate-controller/blob/master/redacted_gate_access_manager.jpg)

- **Vehicle Access Log:** Shows a history of vehicles that were granted access, along with the corresponding images and timestamps.
- **Manage Vehicles:** Allows users to add or remove authorised vehicles.
- **Manage Schedules:** Configure access schedules for specific times or days.
- **Analytics:** Provides insights into gate usage patterns.

## Prerequisites

Before setting up the project, you need the following:

- Raspberry Pi with internet connectivity
- A compatible relay device, e.g., PiRelay v2 Relay Shield
- PlateRecognizer API token (sign up for a free account at PlateRecognizer)
- A Gmail account (or any SMTP server) for email notifications
- Python 3.x installed on your Raspberry Pi

Optional:
- Supabase account to host the PostgreSQL database and handle user authentication for the web application
- AWS S3 bucket for storing captured images accessed by the web application

## Installation

1. Clone the Repository:
   git clone https://github.com/ciaran-finnegan/gate-controller
   cd /opt/gate-controller

2. Create Log Directory:
   mkdir ./logs

3. Install Required Python Packages:
   pip install -r requirements.txt

4. Install Relay Library:
   Download the appropriate library for your relay device. For PiRelay, see the Pi Relay v2 Library.

5. Test Relay Device:
   Ensure your relay device is installed and working by testing the relay activation LEDs. Use the provided script, which may require root privileges:
   sudo python test_operate_relay

6. Configure Environment Variables:
   Set environment variables in the systemd service unit file /etc/systemd/system/file-monitor.service. Below is an example configuration:
   Environment=FUZZY_MATCH_THRESHOLD=80
   Environment=PLATE_RECOGNIZER_API_TOKEN=REPLACE_WITH_YOUR_API_TOKEN
   Environment=SMTP_SERVER=smtp.gmail.com
   Environment=SMTP_PORT=587
   Environment=SMTP_USERNAME=something@gmail.com
   Environment=SMTP_PASSWORD=your_password
   Environment=EMAIL_TO=recipient@gmail.com

   If using AWS S3 for image storage or Supabase for database management, include these additional variables:
   Environment=AWS_ACCESS_KEY=your_aws_access_key
   Environment=AWS_SECRET_KEY=your_aws_secret_key
   Environment=AWS_S3_BUCKET=your_s3_bucket
   Environment=POSTGRES_URL=your_supabase_postgres_url

7. Install and Configure FTP Server:
   Install and configure vsftpd on the Raspberry Pi to receive images via FTP.
   sudo apt-get update
   sudo apt-get install vsftpd
   sudo nano /etc/vsftpd.conf

   Update or add these settings:
   anonymous_enable=NO
   local_enable=YES
   write_enable=YES
   local_umask=022
   pasv_min_port=40000
   pasv_max_port=40100

8. Create FTP User:
   Create a dedicated user for FTP access:
   sudo adduser ftp-user

9. Set Up File Monitoring Service:
   Edit /opt/gate-controller/file_monitor.sh to specify the directory to monitor (default: /home/ftp-user).

10. Start the Monitoring Service:
    Enable and start the file monitoring service:
    sudo systemctl enable file-monitor.service
    sudo systemctl start file-monitor.service

11. Update Authorised Plates (Optional):
    If using Supabase for database management, schedule a cron job to update authorised vehicle plates from the server-side database periodically (e.g., hourly).
    crontab -e
    Add the following line:
    0 * * * * /usr/bin/python3 /opt/gate-controller/update_authorized_plates.py

## Testing

Test the setup by uploading an image with a vehicle licence plate via FTP. Check the logs to verify that the plate was recognised and the gate was triggered accordingly.

## Troubleshooting

1. Check Relay Functionality:
   sudo python /opt/gate-controller/test_relay.py

2. Check Service Status:
   sudo systemctl status file-monitor.service
   sudo tail -f /opt/gate-controller/logs/file-monitor.log

3. Check Script Logs:
   sudo tail -f check-plate-and-open-gate.log

4. Check File and Directory Permissions:
   sudo chown -R filemonitor:root /opt/gate-controller
   sudo chmod -R 775 /opt/gate-controller

5. Test the Plates and Schedule Update:
   sudo bash -c "source /home/filemonitor/.bashrc && python /opt/gate-controller/update_authorized_plates.py"

6. Run the Script Manually:
   sudo -E python check-plate-and-open-gate.py /home/ftp-user/test.jpg

## Maintenance

- Regularly update your Raspberry Pi and installed packages to maintain security.
- Keep the authorised plates list updated (authorised_licence_plates.csv).
- Monitor logs and email notifications to ensure everything is functioning correctly.

## Useful Resources

- [IPCamTalk Forum](https://ipcamtalk.com) - Information and support for CCTV cameras.
- [TOpens](https://topens.com) - Electric gate systems and accessories.
- [Faac](https://www.faac.co.uk) - High-quality gate systems and accessories.
- [Pi Relay v2 Library](https://github.com/sbcshop/PiRelay-V2/blob/main/PiRelay.py) - Relay board for Raspberry Pi.

## Contributors and thanks
Thanks to [Kaja](https://github.com/kaja-osojnik) for web application styling.
Thanks to the [IPCamTalk Forum](https://ipcamtalk.com) community for guidance on configuring Hikvision smart event detection policies.
