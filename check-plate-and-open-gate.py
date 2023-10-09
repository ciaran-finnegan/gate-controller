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
import psycopg2

# Import the create_database_table and log_entry functions from db_utils.py

from db_utils import create_database_table, log_entry

# Configure logging

logging.basicConfig(filename='/opt/gate-controller/logs/check-plate-and-open-gate.log',
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Get the root logger (the logger you've configured with basicConfig)

logger = logging.getLogger()

# Create a handler for console output

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Set the desired log level for the console
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)

# Add the console handler to the root logger
logger.addHandler(console_handler)

# Specify the full path to the SQLite database file
db_file_path = '/opt/gate-controller/data/gate-controller-database.db'

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

# PostgreSQL database configuration
postgres_url_var = 'POSTGRES_URL'
postgres_prisma_url_var = 'POSTGRES_PRISMA_URL'
postgres_url_non_pooling_var = 'POSTGRES_URL_NON_POOLING'
postgres_user_var = 'POSTGRES_USER'
postgres_host_var = 'POSTGRES_HOST'
postgres_password_var = 'POSTGRES_PASSWORD'
postgres_database_var = 'POSTGRES_DATABASE'


plate_recognizer_token = os.environ.get(plate_recognizer_token_var)
#logger.info(f'plate_recognizer_token: {plate_recognizer_token}')
fuzzy_match_threshold = int(os.environ.get(fuzzy_match_threshold_var, 70))
logger.info(f'fuzzy_match_threshold: {fuzzy_match_threshold}')
smtp_server = os.environ.get(smtp_server_var)
logger.info(f'smtp_server: {smtp_server}')
smtp_port = int(os.environ.get(smtp_port_var, 587))
logger.info(f'smtp_port: {smtp_port}')
smtp_username = os.environ.get(smtp_username_var)
logger.info(f'smtp_username: {smtp_username}')
smtp_password = os.environ.get(smtp_password_var)
#logger.info(f'smtp_password: {smtp_password}')
email_to = os.environ.get(email_to_var)
logger.info(f'email_to: {email_to}')
postgres_url = os.environ.get(postgres_url_var)
logger.info(f'postgres_url: {postgres_url}')
#postgres_url = os.environ.get(postgres_url_var)
#logger.info(f'postgres_url: {postgres_url}')
postgres_prisma_url = os.environ.get(postgres_prisma_url_var)
logger.info(f'postgres_prisma_url: {postgres_prisma_url}')
postgres_url_non_pooling = os.environ.get(postgres_url_non_pooling_var)
logger.info(f'postgres_url_non_pooling: {postgres_url_non_pooling}')
postgres_user = os.environ.get(postgres_user_var)
logger.info(f'postgres_user: {postgres_user}')
postgres_host = os.environ.get(postgres_host_var)
logger.info(f'postgres_host: {postgres_host}')
postgres_password = os.environ.get(postgres_password_var)
#logger.info(f'postgres_password: {postgres_password}')
postgres_database = os.environ.get(postgres_database_var)
logger.info(f'postgres_database: {postgres_database}')


# Function to send email notification
def send_email_notification(recipient, subject, message_body, script_start_time, fuzzy_match=False, gate_opened=False):
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

        if gate_opened:
            logger.info(f'Email sent with execution time and attachment for {"Fuzzy Match" if fuzzy_match else "Exact Match"}')
        else:
            logger.info(f'Email sent for skipped gate opening event')

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
                next(csv_reader)  # Skip the header if it exists
                for row in csv_reader:
                    if len(row) >= 5:
                        plate, name, colour, make, model = [item.strip().lower() for item in row]
                        csv_data[plate] = {'name': name, 'colour': colour, 'make': make, 'model': model}


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
                matched_vehicle_data = csv_data.get(best_match, {})
                matched_name = matched_vehicle_data.get('name', '')
                logger.info(f'Match found for vehicle license plate number: {plate_recognized}, Registered to: {matched_name}')

                # Check if another gate opening event occurred in the last 20 seconds
                if is_recent_gate_opening_event():
                    logger.info(f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for {"Fuzzy Match" if score < 1.0 else "Exact Match"}.')
                    send_email_notification(email_to, f'Gate Opening Alert - Skipped - Another Event in Progress',
                                            f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for plate: {plate_recognized}', script_start_time, fuzzy_match=score < 1.0,gate_opened=False)
                    log_entry(image_file_path, plate_recognized, score, fuzzy_match=score < 1.0, gate_opened=True)

                else:
                    # Perform gate opening logic
                    make_pirelay_call()
                    log_entry(image_file_path, plate_recognized, score, fuzzy_match=score < 1.0, gate_opened=True)

                    send_email_notification(email_to, f'Gate Opening Alert - Opened Gate for {matched_name}',
                                            f'Match found for licence plate number: {plate_recognized} which is registered to {matched_name}', script_start_time, fuzzy_match=score < 1.0,gate_opened=True)
                   

            else:
                logger.info(f'No match found for vehicle license plate number: {plate_recognized}')

                # Send an email notification when no match is found
                send_email_notification(email_to, f'Gate Opening Alert - No Match Found for Plate: {plate_recognized}, did not Open Gate',
                                        f'No match found or vehicle not registered for licence plate number: {plate_recognized}', script_start_time,gate_opened=False)
                log_entry(image_file_path, plate_recognized, score, fuzzy_match=score < 1.0, gate_opened=False)

    except Exception as e:
        # Log the error message
        logger.error(f'Error processing image file: {str(e)}')


# Function to check if another gate opening event occurred in the last 20 seconds
def is_recent_gate_opening_event():
    conn = sqlite3.connect(db_file_path)
    cursor = conn.cursor()

    logger.info(f'Checking for recent gate opening event')
    # Query the database to check if another event occurred in the last 20 seconds
    current_time = time.time()
    logger.info(f'Current time: {current_time}')
    # Calculate the timestamp 20 seconds ago in the format 'YYYY-MM-DD HH:MM:SS'
    # Update based on your requirements, 20 seconds chosen as that is the delay before this gate automatically closes
    twenty_seconds_ago = (datetime.datetime.now() - datetime.timedelta(seconds=20)).strftime('%Y-%m-%d %H:%M:%S')
    logger.info(f'Time 20 seconds ago: {twenty_seconds_ago}')
    
    # Log the SQL query
    logger.info(f'SQL Query: SELECT COUNT(*) FROM log WHERE gate_opened="Yes" AND timestamp > {twenty_seconds_ago}')
    cursor.execute('SELECT COUNT(*) FROM log WHERE gate_opened="Yes" AND timestamp > ?', (twenty_seconds_ago,))
    count = cursor.fetchone()[0]
    logger.info(f'Count of matching values in database log table: {count}')

    # For debugging only
    # Fetch and log each row of data
    for row in cursor.fetchall():
        logger.info(f'Database table query returned the following: {row}')
    conn.close()

    return count > 0


# Entry point for running the script
if __name__ == "__main__":
    try:
        # Get the path to the image file from command line arguments
        if len(sys.argv) != 2:
            print("Usage: python script.py /path/to/image.jpg")
            sys.exit(1)

        image_file_path = sys.argv[1]
        # Create the database and initialise the log table if it doesn't exist
        create_database_table()
        # Process the image file
        process_image_file(image_file_path)
    except Exception as e:
        # Log any unhandled exceptions
        logger.error(f'Unhandled exception in main: {str(e)}')