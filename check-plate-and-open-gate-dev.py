import os
import json
import logging
import csv
import io
import requests
from fuzzywuzzy import fuzz
import datetime
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from PIL import Image
import PiRelay
import sys
import sqlite3

# Configure logging
logging.basicConfig(filename='/opt/gate-controller/logs/check-plate-and-open-gate.log',
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# Initialize PiRelay
r1 = PiRelay.Relay("RELAY1")

# Retrieve configuration parameters, API, and Email credentials

# PlateRecognizer API configuration
plate_recognizer_token_var = 'PLATE_RECOGNIZER_API_TOKEN'

# Fuzzy matching configuration
fuzzy_match_threshold_var = 'FUZZY_MATCH_THRESHOLD'

# Email configuration
smtp_server_var = 'SMTP_SERVER'
smtp_port_var = 'SMTP_PORT'
smtp_username_var = 'SMTP_USERNAME'
smtp_password_var = 'SMTP_PASSWORD'
email_to_var = 'EMAIL_TO'

plate_recognizer_token = os.environ.get(plate_recognizer_token_var)
fuzzy_match_threshold = int(os.environ.get(fuzzy_match_threshold_var, 70))
smtp_server = os.environ.get(smtp_server_var)
smtp_port = int(os.environ.get(smtp_port_var, 587))
smtp_username = os.environ.get(smtp_username_var)
smtp_password = os.environ.get(smtp_password_var)
email_to = os.environ.get(email_to_var)

# Function to send email notification
def send_email_notification(recipient, subject, message_body, script_start_time, fuzzy_match=False):
    try:
        # Create a connection to the SMTP server
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)

        # Include explanatory text, script start time, and elapsed time in the email body
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elapsed_time = time.time() - script_start_time
        elapsed_time_formatted = f'{elapsed_time:.1f}'
        formatted_script_start_time = datetime.datetime.fromtimestamp(script_start_time).strftime("%Y-%m-%d %H:%M:%S")
        message_body_with_time = (
            f'### Script Start Time: {formatted_script_start_time} ###\n\n'
            f'{message_body}\n\n'
            f'Current Time: {current_time}\n'
            f'Elapsed Time: {elapsed_time_formatted} seconds'
        )

        # Create a MIME email message with attachments
        msg = MIMEMultipart()
        msg['From'] = smtp_username
        msg['To'] = recipient
        msg['Subject'] = subject

        # Attach the message body
        msg.attach(MIMEText(message_body_with_time, 'plain'))

        # Open and resize the image
        with open(image_file_path, 'rb') as attachment_file:
            img = Image.open(attachment_file)
            img.thumbnail((600, 600))
            img_byte_array = io.BytesIO()
            img.save(img_byte_array, format='JPEG')
            img_data = img_byte_array.getvalue()

        # Attach the resized image
        attachment = MIMEBase('application', 'octet-stream')
        attachment.set_payload(img_data)
        encoders.encode_base64(attachment)
        attachment.add_header('Content-Disposition', f'attachment; filename=attachment.jpg')
        msg.attach(attachment)

        # Send the email
        server.sendmail(smtp_username, recipient, msg.as_string())
        server.quit()

        logger.info(f'Email sent with execution time and attachment for {"Fuzzy Match" if fuzzy_match else "Exact Match"}')
    except Exception as e:
        logger.error(f'Error sending email: {str(e)}')

# Make a PiRelay call to open the gate
def make_pirelay_call():
    try:
        r1.on()
        time.sleep(2)
        r1.off()
        logger.info(f'PiRelay call made to open the gate')
    except Exception as e:
        logger.error(f'Error making PiRelay call: {str(e)}')

# Function to check if another gate opening event occurred in the last 20 seconds
def is_recent_gate_opening_event():
    conn = sqlite3.connect('mydatabase.db')
    cursor = conn.cursor()

    # Query the database to check if another event occurred in the last 20 seconds
    current_time = time.time()
    twenty_seconds_ago = current_time - 20
    cursor.execute('SELECT COUNT(*) FROM log WHERE opened_gate="Yes" AND timestamp > ?', (twenty_seconds_ago,))
    count = cursor.fetchone()[0]

    conn.close()

    return count > 0

# Function to process the image file
def process_image_file(image_file_path):
    script_start_time = time.time()
    try:
        # Upload the image to the Plate Recognizer API using requests
        regions = ["ie"]  # Change to your country
        with open(image_file_path, 'rb') as fp:
            response = requests.post(
                'https://api.platerecognizer.com/v1/plate-reader/',
                data=dict(regions=regions),  # Optional
                files=dict(upload=fp),
                headers={'Authorization': f'Token {plate_recognizer_token}'})

            # Log the response from Plate Recognizer API
            response_text = response.text
            logger.info(f'Plate Recognizer API Response Status Code: {response.status_code}')
            logger.info(f'Plate Recognizer API Response: {response_text}')

            # Extract the recognized plate value from the API response
            plate_recognized = None
            try:
                response_data = json.loads(response_text)
                if 'results' in response_data:
                    first_result = response_data['results'][0]
                    plate_recognized = first_result.get('plate', '').lower()
                if 'score' in first_result:
                    score = float(first_result.get('score'))
                else:
                    score = 0.0
            except Exception as e:
                logger.error(f'Error extracting recognized plate: {str(e)}')

            # Now, retrieve the CSV file and store its contents in a dictionary
            csv_data = {}
            with open('authorised_licence_plates.csv', 'r') as csv_file:
                csv_reader = csv.reader(csv_file)
                for row in csv_reader:
                    if len(row) >= 2:
                        key, value = row[0].strip(), row[1].strip()
                        csv_data[key.lower()] = value.lower()

            # Log the results
            logger.info('CSV data for authorised licence plate numbers:')
            logger.info(csv_data)

            # Compare the recognized plate to the values in the CSV (including fuzzy matching)
            best_match = None
            for csv_key in csv_data.keys():
                match_score = fuzz.partial_ratio(plate_recognized, csv_key)
                if match_score >= fuzzy_match_threshold:
                    best_match = csv_key
                    break

            if best_match is not None:
                matched_value = csv_data.get(best_match, '')
                logger.info(f'Match found for vehicle license plate number: {plate_recognized}, Registered to: {matched_value}')

                # Check if another gate opening event occurred in the last 20 seconds
                if is_recent_gate_opening_event():
                    logger.info(f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for {"Fuzzy Match" if score < 1.0 else "Exact Match"}.')
                    send_email_notification(email_to, f'Gate Opening Skipped - Another Event in Progress',
                                            f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for plate: {plate_recognized}', script_start_time, fuzzy_match=score < 1.0)
                else:
                    # Perform gate opening logic
                    make_pirelay_call()
                    log_entry(image_file_path, plate_recognized, score, script_start_time)
                    logger.info(f'logged gate opening event to database')
                    send_email_notification(email_to, f'Gate Opening Alert - Opened Gate', f'Vehicle with licence plate number: {plate_recognized} is registered to {matched_value}', script_start_time, fuzzy_match={score < 1.0}')

            else:
                logger.info(f'No match found for vehicle license plate number: {plate_recognized}')

                # Send an email notification when no match is found
                send_email_notification(email_to, f'Gate Opening Alert - No Match Found for Plate: {plate_recognized}, did not Open Gate',
                                        f'No match found or vehicle not registered for licence plate number: {plate_recognized}', script_start_time)

    except Exception as e:
        # Log the error message
        logger.error(f'Error: {str(e)}')

# Function to log an entry in the database
def log_entry(image_path, plate_recognized, score, script_start_time):
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect('mydatabase.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS log (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME,
            image_path TEXT,
            plate_recognized TEXT,
            score REAL,
            opened_gate TEXT
        )
    ''')

    cursor.execute('INSERT INTO log (timestamp, image_path, plate_recognized, score, opened_gate) VALUES (?, ?, ?, ?, ?)',
                   (current_time, image_path, plate_recognized, score, 'Yes' if score == 1.0 else 'No'))

    conn.commit()
    conn.close()

# Entry point for running the script
if __name__ == "__main__":
    try:
        # Get the path to the image file from command line arguments
        if len(sys.argv) != 2:
            print("Usage: python script.py /path/to/image.jpg")
            sys.exit(1)

        image_file_path = sys.argv[1]

        # Process the image file
        process_image_file(image_file_path)
    except Exception as e:
        # Log any unhandled exceptions
        logger.error(f'Unhandled exception: {str(e)}')
