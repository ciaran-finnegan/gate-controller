To run your Flask script as a service on a Raspberry Pi, you can use systemd, which is the standard init system for most Linux distributions, including Raspbian (now known as Raspberry Pi OS). Here's how you can configure your script as a systemd service:

1) Create a systemd service file:
Open a terminal on your Raspberry Pi and create a systemd service file for your Flask app using a text editor. You can use the nano text editor, for example:

```
sudo nano /etc/systemd/system/gate-controller.service
```
In the file you've opened, add the following content:

```
[Unit]
Description=Gate Controller Service
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/your/script.py
WorkingDirectory=/path/to/your/script/directory
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```
2) Replace /path/to/your/script.py with the full path to your Python script, and /path/to/your/script/directory with the directory where your script is located.

3) Save the file by pressing Ctrl + O, then press Enter, and exit the text editor by pressing Ctrl + X.

40 Reload systemd to read the new service file:

```
sudo systemctl daemon-reload
```

Start and enable the service to run on boot:

```
sudo systemctl start gate-controller
sudo systemctl enable gate-controller
```

Check the status of your service to make sure it's running:

```
sudo systemctl status gate-controller
```

If everything is set up correctly, you should see the service as "active (running)" in the output.

You can now access your Flask app at the specified host and port (0.0.0.0:5000) in your browser or send requests to it.

Remember to replace /path/to/your/script.py and /path/to/your/script/directory with the actual paths to your script and its directory, and also ensure that the Python executable path (/usr/bin/python3 in the service file) is correct for your Raspberry Pi.

This configuration will start your Flask app as a systemd service, ensuring it runs in the background and starts automatically on boot. You can manage the service using systemctl commands like start, stop, restart, and status.