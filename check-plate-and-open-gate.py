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

# Import the configure_logger function
from logger import configure_logger

# Import the log_entry function from db_utils.py
from db_utils import log_entry, create_table_sqlite

# Configure logging
logger = configure_logger()

# Specify the full path to the SQLite database file
db_file_path = '/opt/gate-controller/data/gate-controller-database.db'

# Initialize PiRelay
r1 = PiRelay.Relay("RELAY1")

# Retrieve configuration parameters, API, and Email credentials
plate_recognizer_token_var = 'PLATE_RECOGNIZER_API_TOKEN'
fuzzy_match_threshold_var = 'FUZZY_MATCH_THRESHOLD'
smtp_server_var = 'SMTP_SERVER'
smtp_port_var = 'SMTP_PORT'
smtp_username_var = 'SMTP_USERNAME'
smtp_password_var = 'SMTP_PASSWORD'
email_to_var = 'EMAIL_TO'

plate_recognizer_token = os.environ.get(plate_recognizer_token_var)
fuzzy_match_threshold = int(os.environ.get(fuzzy_match_threshold_var, 70))  # Default to 70 if not set
smtp_server = os.environ.get(smtp_server_var)
smtp_port = int(os.environ.get(smtp_port_var, 587))
smtp_username = os.environ.get(smtp_username_var)
smtp_password = os.environ.get(smtp_password_var)
email_to = os.environ.get(email_to_var)

# Paths to the CSV files
plates_csv_file_path = '/opt/gate-controller/authorised_licence_plates.csv'
schedule_csv_file_path = '/opt/gate-controller/access_schedule.csv'

# Function to send email notification
def send_email_notification(recipient, subject, message_body, script_start_time, fuzzy_match=False, gate_opened=False):
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)

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

        msg = MIMEMultipart()
        msg['From'] = smtp_username
        msg['To'] = recipient
        msg['Subject'] = subject

        msg.attach(MIMEText(message_body_with_time, 'plain'))

        with open(image_file_path, 'rb') as attachment_file:
            img = Image.open(attachment_file)
            img.thumbnail((600, 600))
            img_byte_array = io.BytesIO()
            img.save(img_byte_array, format='JPEG')
            img_data = img_byte_array.getvalue()

        attachment = MIMEBase('application', 'octet-stream')
        attachment.set_payload(img_data)
        encoders.encode_base64(attachment)
        attachment.add_header('Content-Disposition', f'attachment; filename=attachment.jpg')
        msg.attach(attachment)

        server.sendmail(smtp_username, recipient, msg.as_string())
        server.quit()

        if gate_opened:
            logger.info(f'Email sent with execution time and attachment for {"Fuzzy Match" if fuzzy_match else "Exact Match"}')
        else:
            logger.info(f'Email sent for skipped gate opening event')

    except Exception as e:
        logger.error(f'Error sending email: {str(e)}')

# Function to load access schedule from CSV file
def load_access_schedule():
    schedule = []
    try:
        with open(schedule_csv_file_path, mode='r', encoding='utf-8') as csv_file:
            csv_reader = csv.DictReader(csv_file)
            for row in csv_reader:
                schedule.append({
                    'day_of_week': row['day_of_week'].lower(),
                    'start_time': datetime.datetime.strptime(row['start_time'], '%H:%M:%S').time(),
                    'end_time': datetime.datetime.strptime(row['end_time'], '%H:%M:%S').time()
                })
        logger.info('Access schedule loaded successfully')
    except Exception as e:
        logger.error(f'Error loading access schedule: {str(e)}')
    return schedule

# Function to check if the current time falls within the access schedule
def is_within_schedule(schedule):
    current_time = datetime.datetime.now()
    current_day = current_time.strftime('%A').lower()
    current_time_only = current_time.time()

    for entry in schedule:
        if entry['day_of_week'] == current_day:
            if entry['start_time'] <= current_time_only <= entry['end_time']:
                return True
    return False

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
        # Load access schedule
        access_schedule = load_access_schedule()

        # Check if the current time is within the access schedule
        if is_within_schedule(access_schedule):
            logger.info('Current time is within the access schedule, allowing all vehicles to access.')
            make_pirelay_call()
            
            # Log the event with the reason for access
            log_entry('Access permitted due to access policy schedule',
                      image_file_path, '', 0, False, True, '', '', '', '', '')

            send_email_notification(email_to, 'Gate Opening Alert - Access Permitted by Schedule',
                                    'Gate was opened based on schedule access.', script_start_time, gate_opened=True)
            return  # Exit the function after opening the gate based on the schedule

        # Upload the image to the Plate Recognizer API using requests
        regions = ["ie"]  # Change to your country
        with open(image_file_path, 'rb') as fp:
            response = requests.post(
                'https://api.platerecognizer.com/v1/plate-reader/',
                data=dict(regions=regions),  # Optional
                files=dict(upload=fp),
                headers={'Authorization': f'Token {plate_recognizer_token}'})

            response_text = response.text
            logger.info(f'Plate Recognizer API Response Status Code: {response.status_code}')
            logger.info(f'Plate Recognizer API Response: {response_text}')

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

            csv_data = {}
            with open('authorised_licence_plates.csv', 'r', encoding='utf-8') as csv_file:
                csv_reader = csv.reader(csv_file)
                next(csv_reader)
                for row in csv_reader:
                    if len(row) == 5:
                        plate, name, colour, make, model = [item.strip().lower() for item in row]
                        csv_data[plate] = {'name': name, 'colour': colour, 'make': make, 'model': model}

            best_match = None
            highest_match_score = 0
            fuzzy_match = False

            for csv_key in csv_data.keys():
                match_score = fuzz.partial_ratio(plate_recognized, csv_key)
                if match_score > highest_match_score:
                    highest_match_score = match_score
                    best_match = csv_key
                    # Check if the match is above the threshold but not an exact match
                    fuzzy_match = match_score >= fuzzy_match_threshold and match_score < 100

            # Decision making based on match score and threshold
            if best_match is not None and highest_match_score >= fuzzy_match_threshold:
                matched_value = csv_data.get(best_match, {})
                plate_number = best_match
                vehicle_registered_to_name = matched_value.get('name', '')
                vehicle_make = matched_value.get('make', '')
                vehicle_model = matched_value.get('model', '')
                vehicle_colour = matched_value.get('colour', '')

                logger.info(f'Match found for vehicle license plate number: {plate_number}, Registered to: {vehicle_registered_to_name}')

                if is_recent_gate_opening_event():
                    logger.info(f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for {plate_number}, Registered to: {vehicle_registered_to_name}.')
                    send_email_notification(email_to, f'Gate Opening Alert - Skipped - Another Event in Progress',
                                            f'Another gate opening event occurred in the last 20 seconds. Skipping gate opening for plate: {plate_number}, Registered to: {vehicle_registered_to_name}', 
                                            script_start_time, fuzzy_match=fuzzy_match, gate_opened=False)
                    
                    # Log the event for skipped gate opening
                    log_entry('Gate opening skipped due to another recent event',
                              image_file_path,
                              plate_recognized,
                              highest_match_score,
                              fuzzy_match,
                              gate_opened=False,
                              plate_number=plate_number,
                              vehicle_registered_to_name=vehicle_registered_to_name,
                              vehicle_make=vehicle_make,
                              vehicle_model=vehicle_model,
                              vehicle_colour=vehicle_colour)
                else:
                    # Perform gate opening logic
                    make_pirelay_call()

                    # Log the event
                    log_entry('Licence plate number accepted',
                              image_file_path,
                              plate_recognized,
                              highest_match_score,
                              fuzzy_match,
                              gate_opened=True,
                              plate_number=plate_number,
                              vehicle_registered_to_name=vehicle_registered_to_name,
                              vehicle_make=vehicle_make,
                              vehicle_model=vehicle_model,
                              vehicle_colour=vehicle_colour)

                    send_email_notification(email_to, f'Gate Opening Alert - Opened Gate for {plate_number}, Registered to: {vehicle_registered_to_name}',
                                            f'Match found for licence plate number: {plate_number} which is registered to {vehicle_registered_to_name}', 
                                            script_start_time, fuzzy_match=fuzzy_match, gate_opened=True)
            else:
                logger.info(f'No match found for vehicle license plate number: {plate_recognized}')

                # Send an email notification when no match is found
                send_email_notification(email_to, f'Gate Opening Alert - No Match Found for Plate: {plate_recognized}, did not Open Gate',
                                        f'No match found or vehicle not registered for licence plate number: {plate_recognized}', 
                                        script_start_time, gate_opened=False)

                # Log the event
                log_entry('Licence plate number not recognised or not authorised',
                          image_file_path,
                          plate_recognized,
                          0,
                          fuzzy_match=False,
                          gate_opened=False,
                          plate_number='',
                          vehicle_registered_to_name='',
                          vehicle_make='',
                          vehicle_model='',
                          vehicle_colour='')
    except Exception as e:
        logger.error(f'Error processing image file: {str(e)}')

# Function to check if another gate opening event occurred in the last 20 seconds
def is_recent_gate_opening_event():
    conn = sqlite3.connect(db_file_path)
    cursor = conn.cursor()

    logger.info(f'Checking for recent gate opening event')
    current_time = time.time()
    twenty_seconds_ago = (datetime.datetime.now() - datetime.timedelta(seconds=20)).strftime('%Y-%m-%d %H:%M:%S')
    logger.info(f'Time 20 seconds ago: {twenty_seconds_ago}')

    cursor.execute('SELECT COUNT(*) FROM log WHERE gate_opened="Yes" AND timestamp > ?', (twenty_seconds_ago,))
    count = cursor.fetchone()[0]
    logger.info(f'Count of matching values in database log table: {count}')

    conn.close()
    return count > 0

# Entry point for running the script
if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            print("Usage: python script.py /path/to/image.jpg")
            sys.exit(1)

        image_file_path = sys.argv[1]
        # Create the SQLite database and initialise the log table if it doesn't exist
        create_table_sqlite()
        # Process the image file
        process_image_file(image_file_path)
    except Exception as e:
        logger.error(f'Unhandled exception in main: {str(e)}')