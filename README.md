# License-Plate-Recognition-Gate-Opening-Relay--RaspberryPi
Licence Plate Recognition and Gate Opening Relay Control for Raspberry Pi
Install vsftpd (FTP Server) on Raspberry Pi:

First, you need to install the vsftpd package, which is a popular FTP server for Linux:

```
bash
Copy code
sudo apt-get update
sudo apt-get install vsftpd
Configure vsftpd:
```
Edit the vsftpd configuration file to enable certain settings and allow passive mode for FTP. Open the configuration file in a text editor:

```
bash
Copy code
sudo nano /etc/vsftpd.conf
```

Update the following settings in the vsftpd.conf file:

```
conf
Copy code
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

You should create a dedicated user for FTP access. Replace your-ftp-username with your desired username:

```
bash
Copy code
sudo adduser your-ftp-username
```

Set a password for this user when prompted.

Set Up Python Script:

Create a Python script that will be triggered when a new .jpeg file is uploaded. For example, you can create a script called image_upload_handler.py:

python
Copy code
```
# image_upload_handler.py
import os

def process_uploaded_image(file_path):
    # Your custom logic to process the uploaded image
    print(f"New image uploaded: {file_path}")

if __name__ == "__main__":
    # Replace this with the path to your FTP directory
    ftp_directory = "/path/to/ftp/directory"

    while True:
        # List all files in the FTP directory
        files = os.listdir(ftp_directory)

        for file in files:
            if file.endswith(".jpeg"):
                file_path = os.path.join(ftp_directory, file)
                process_uploaded_image(file_path)

        # Sleep for some time (e.g., 60 seconds) before checking again
        time.sleep(60)
```

Make the Python Script Executable:

```
bash
Copy code
chmod +x image_upload_handler.py
Configure the FTP Directory:
```

Ensure that the FTP directory exists and is owned by the FTP user:

```
bash
Copy code
sudo mkdir -p /path/to/ftp/directory
sudo chown -R your-ftp-username:your-ftp-username /path/to/ftp/directory
```

Start vsftpd Service:

```
bash
Copy code
sudo service vsftpd start
```

Auto-Start Python Script:

To make the Python script run on system startup, you can create a systemd service. Create a file named /etc/systemd/system/image_upload_handler.service with the following content:

ini
Copy code
```
[Unit]
Description=Image Upload Handler Service

[Service]
ExecStart=/path/to/your/script/image_upload_handler.py
Restart=always
User=your-ftp-username

[Install]
WantedBy=multi-user.target
```

Replace /path/to/your/script with the actual path to your Python script.

Enable and Start the Service:

Enable and start the service:

bash
Copy code
```
sudo systemctl enable image_upload_handler.service
sudo systemctl start image_upload_handler.service
```

Now, your Raspberry Pi should be running an FTP server, and the Python script will be triggered whenever a new .jpeg image is uploaded to the FTP directory. The solution should also survive a restart of the operating system.

Please make sure to replace placeholders with your actual directory paths, usernames, and other specific details as needed.

# Configuring senstive credentials as environment variables

Open a terminal on your Raspberry Pi.

Use a text editor like nano or vim to edit the user's .bashrc file. Replace <username> with the actual username of the local user:

```
nano /home/<username>/.bashrc
``````
Scroll to the end of the file and add your environment variable. For example, to create an environment variable named MY_VARIABLE with a value of my_value, add the following line:

```
export MY_VARIABLE="my_value"
```
Save the file and exit the text editor (in nano, you can press Ctrl+O to save and Ctrl+X to exit).


To ensure that the changes take effect, either log out and log back in or restart your Raspberry Pi:

```
sudo reboot
```
