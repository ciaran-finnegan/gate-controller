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

# Configure logging
logging.basicConfig(filename='/opt/gate-controller/logs/check-plate-and-open-gate.log',
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# Initialize PiRelay
r1 = PiRelay.Relay("RELAY1")

# Retrieve configuration parameters, API and Email credentials

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
def send_email_notification(recipient, subject, message_body, script_start_time):
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

        logger.info(f'Email sent with execution time and attachment')
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

                # Send an email notification when a match is found
                if score == 1.0:
                    make_pirelay_call()
                    send_email_notification(email_to, f'Gate Opening Alert - Exact Match Found for Plate: {plate_recognized}',
                                            f'Exact match, licence plate number {plate_recognized} is registered to: {matched_value}', script_start_time)
                else:
                    make_pirelay_call()
                    send_email_notification(email_to, f'Gate Opening Alert - Fuzzy Match Found for Plate: {plate_recognized}',
                                            f'Fuzzy match, licence plate number {plate_recognized} is registered to: {matched_value}', script_start_time)
            else:
                logger.info(f'No match found for vehicle license plate number: {plate_recognized}')

                # Send an email notification when no match is found
                send_email_notification(email_to, f'Gate Opening Alert - No Match Found for Plate: {plate_recognized}, did not Open Gate',
                                        f'No match found or vehicle not registered for licence plate number: {plate_recognized}', script_start_time)

    except Exception as e:
        # Log the error message
        logger.error(f'Error: {str(e)}')

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