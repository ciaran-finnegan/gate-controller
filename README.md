![Gate Access Manager](redacted_gate_access_manager.png)

- **Vehicle Access Log:** Shows a history of vehicles that were granted access, along with the corresponding images and timestamps.
- **Manage Vehicles:** Allows users to add or remove authorised vehicles.
- **Manage Schedules:** Configure access schedules for specific times or days.
- **Analytics:** Provides insights into gate usage patterns.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Web Application](#web-application)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Maintenance](#maintenance)
- [Useful Resources](#useful-resources)

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

## Web Application

The accompanying web application, Gate Controller Web Application, provides a user-friendly interface for managing authorised vehicle plates, viewing access logs, and configuring system settings.

- **Database and Authentication:** The web application uses Supabase for managing the PostgreSQL database and handling user authentication.
- **Image Storage:** Images captured by the system can optionally be stored in an AWS S3 bucket, allowing users to view them through the web application.
- **Hosting and Deployment:** The front end of the web application is hosted on Netlify, which builds and deploys the site automatically from the GitHub repository.

### Accessing the Web Application:
1. Clone the web application repository:
   git clone https://github.com/ciaran-finnegan/gate-controller-refine-front-end
2. Follow the installation instructions in the repository’s README to set up the application.

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

- IPCamTalk Forum - Information and support for CCTV cameras.
- TOpens - Electric gate systems and accessories.
- Faac - High-quality gate systems and accessories.
- Pi Relay v2 Library - Relay board for Raspberry Pi.