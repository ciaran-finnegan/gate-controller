#!/bin/bash

# Directory to monitor
directory_to_watch="/home/ftp-user"

# Redirect stdout and stderr to the log file
log_file="/opt/gate-controller/logs/file_monitor.log"
exec >> "$log_file" 2>&1

# Function to log a message with timestamp
log_message() {
    local message="$1"
    local timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    echo "[$timestamp] $message"
}

# Use inotifywait to monitor the directory for create events
inotifywait -m -e create --format '%f' "$directory_to_watch" |
while read -r new_file
do
    if [[ "$new_file" == *.jpg ]]; then
        log_message "New .jpg file detected: $new_file"
        # Run your Python script to detect license plate and open gate
        /usr/bin/python3 /opt/gate-controller/check-plate-and-open-gate.py "$directory_to_watch/$new_file"
        log_message "Processing of $new_file completed."
    fi
done