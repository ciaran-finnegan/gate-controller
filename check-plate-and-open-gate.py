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

# Import the log_entry function from db_utils.py

from db_utils import log_entry, create_table_sqlite

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
            
            # Dictionary to hold CSV data
            csv_data = {}
            
            # Open and read the CSV file
            with open('authorised_licence_plates.csv', 'r', encoding='utf-8') as csv_file:
                csv_reader = csv.reader(csv_file)
                next(csv_reader)  # Skip header row
                for row in csv_reader:
                    # Ensure all expected columns are present
                        if len(row) == 5:
                            plate, name, colour, make, model = [item.strip().lower() for item in row]
                            csv_data[plate] = {'name': name, 'colour': colour, 'make': make, 'model': model}
            
            # Log the results
            logger.info('CSV data for authorised licence plate numbers loaded:')
            logger.debug(csv_data[plate])

            # Compare the recognized plate to the values in the CSV (including fuzzy matching)
            best_match = None
            match_score = 0  # Initialize match_score
            for csv_key in csv_data.keys():
                match_score = fuzz.partial_ratio(plate_recognized, csv_key)
                if match_score >= fuzzy_match_threshold:
                    best_match = csv_key
                    if match_score == 100:
                        fuzzy_match = False # Exact match
                    else:
                        fuzzy_match = True  # Fuzzy match (greater than or equal to fuzzy_match_threshold)
                        break
                    break

            if best_match is not None:
                matched_value = csv_data.get(best_match, '')
                logger.info(f'Match found for vehicle license plate number: {plate_recognized}, Registered to: {matched_value}')
                
                # Retrieve vehicle data based on matched plate number

                matched_value = csv_data.get(best_match, {})
                
                plate_number = best_match
                vehicle_registered_to_name = matched_value.get('name', '')
                vehicle_make = matched_value.get('make', '')
                vehicle_model = matched_value.get('model', '')
                vehicle_colour = matched_value.get('colour', '')


                # Check if another gate opening event occurred in the last 20 seconds
                if is_recent_gate_opening_event():
                    logger.info(f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for {plate_recognized}, Registered to: {matched_value}.')
                    send_email_notification(email_to, f'Gate Opening Alert - Skipped - Another Event in Progress',
                                            f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for plate: {plate_recognized}, Registered to: {matched_value}', script_start_time, fuzzy_match=score < 1.0,gate_opened=False)
                    reason='Gate opening already in progress' 
                    log_entry(reason,
                            image_file_path,
                            plate_recognized,
                            match_score,
                            plate_number,
                            vehicle_registered_to_name,
                            vehicle_make,
                            vehicle_model,
                            vehicle_colour,
                            fuzzy_match,
                            gate_opened=False)
                else:
                    # Perform gate opening logic
                    make_pirelay_call()
                    reason='Licence plate number accepted' 
                    log_entry(reason,
                            image_file_path,
                            plate_recognized,
                            match_score,
                            plate_number,
                            vehicle_registered_to_name,
                            vehicle_make,
                            vehicle_model,
                            vehicle_colour,
                            fuzzy_match,
                            gate_opened=True)
                    send_email_notification(email_to, f'Gate Opening Alert - Opened Gate for {plate_recognized}, Registered to: {matched_value}',
                                            f'Match found for licence plate number: {plate_recognized} which is registered to {matched_value}', script_start_time, fuzzy_match=score < 1.0,gate_opened=True)
                   

            else:
                logger.info(f'No match found for vehicle license plate number: {plate_recognized}')

                # Send an email notification when no match is found
                send_email_notification(email_to, f'Gate Opening Alert - No Match Found for Plate: {plate_recognized}, did not Open Gate',
                                        f'No match found or vehicle not registered for licence plate number: {plate_recognized}', script_start_time,gate_opened=False)
                reason='Licence plate number could not be recognised or is not authorised'

                # Log the event
                # Set values for vehicle data to empty strings
                # Set fuzzy_match to False
                # Set gate_opened to False 

                plate_number = ''
                vehicle_registered_to_name = ''
                vehicle_make = ''
                vehicle_model = ''
                vehicle_colour = ''

                log_entry(reason,
                            image_file_path,
                            plate_recognized,
                            match_score,
                            plate_number,
                            vehicle_registered_to_name,
                            vehicle_make,
                            vehicle_model,
                            vehicle_colour,
                            fuzzy_match=False,
                            gate_opened=False)

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
        # Create the SQLlite database and initialise the log table if it doesn't exist
        create_table_sqlite()
        # Process the image file
        process_image_file(image_file_path)
    except Exception as e:
        # Log any unhandled exceptions
        logger.error(f'Unhandled exception in main: {str(e)}')