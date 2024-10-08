import os
import logging
import psycopg2
from datetime import datetime, timezone
import sqlite3
from logger import configure_logger

# Configure logging
logger = configure_logger()

# Import the upload_image_to_s3 function from s3_utils.py
from s3_utils import upload_image_to_s3

# PostgreSQL database configuration
postgres_url_var = 'POSTGRES_URL'
postgres_prisma_url_var = 'POSTGRES_PRISMA_URL'
postgres_url_non_pooling_var = 'POSTGRES_URL_NON_POOLING'
postgres_user_var = 'POSTGRES_USER'
postgres_host_var = 'POSTGRES_HOST'
postgres_password_var = 'POSTGRES_PASSWORD'
postgres_database_var = 'POSTGRES_DATABASE'

postgres_url = os.environ.get(postgres_url_var)
postgres_prisma_url = os.environ.get(postgres_prisma_url_var)
logger.info(f'postgres_prisma_url: {postgres_prisma_url}')

postgres_url_non_pooling = os.environ.get(postgres_url_non_pooling_var)
logger.info(f'postgres_url_non_pooling: {postgres_url_non_pooling}')

postgres_user = os.environ.get(postgres_user_var)
logger.info(f'postgres_user: {postgres_user}')

postgres_host = os.environ.get(postgres_host_var)
logger.info(f'postgres_host: {postgres_host}')

postgres_endpoint = postgres_host.split('.')[0]
logger.info(f'postgres_endpoint: {postgres_endpoint}')

postgres_password = os.environ.get(postgres_password_var)
postgres_database = os.environ.get(postgres_database_var)
logger.info(f'postgres_database: {postgres_database}')

postgres_port = 5432
logger.info(f'postgres_port: {postgres_port}')

postgres_sslmode = 'require'
logger.info(f'postgres_sslmode: {postgres_sslmode}')

# Specify the full path to the SQLite database directory and filename
db_directory = '/opt/gate-controller/data/'
db_filename = 'gate-controller-database.db'
db_file_path = os.path.join(db_directory, db_filename)

# Function to log an entry in the database(s)
def log_entry(reason,
              image_path,
              plate_recognized,
              score,
              fuzzy_match,
              gate_opened,
              plate_number,
              vehicle_registered_to_name,
              vehicle_make,
              vehicle_model,
              vehicle_colour):
    # SQLite Entry
    logger.info(f'Logging an entry in the SQLite database log table.')
    log_entry_sqlite(reason, image_path, plate_recognized, score, fuzzy_match, gate_opened, 
                     plate_number, vehicle_registered_to_name, vehicle_make, vehicle_model, vehicle_colour)

    # PostgreSQL Entry
    logger.info(f'Logging an entry in the PostgreSQL database log table.')
    image_url = upload_image_to_s3(image_path)
    image_path = image_url  # Replace the local image path with the S3 object URL

    log_entry_postgres(reason, image_path, plate_recognized, score, fuzzy_match, gate_opened, 
                       plate_number, vehicle_registered_to_name, vehicle_make, vehicle_model, vehicle_colour)

# Function to create the SQLite database table if it doesn't exist
def create_table_sqlite():
    logger.info(f'SQLite db_file_path: {db_file_path}')

    # Ensure the database directory exists
    if not os.path.exists(db_directory):
        os.makedirs(db_directory)

    try:
        conn = sqlite3.connect(db_file_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS log (
                id INTEGER PRIMARY KEY,
                timestamp DATETIME,
                reason TEXT,
                image_path TEXT,
                plate_recognized TEXT,
                score REAL,
                fuzzy_match TEXT,
                gate_opened TEXT,
                plate_number TEXT,
                vehicle_registered_to_name TEXT,
                vehicle_make TEXT,
                vehicle_model TEXT,
                vehicle_colour TEXT
            )
        ''')
        conn.commit()
        conn.close()
        logger.info(f'Database table created successfully.')
    except sqlite3.Error as sql_error:
        logger.error(f'SQLite error while creating the table: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error creating the table: {str(e)}')

# Function to create the PostgreSQL database table
def create_table_postgres(conn):
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP,
                reason TEXT,
                image_path TEXT,
                plate_recognized TEXT,
                score REAL,
                fuzzy_match BOOLEAN,
                gate_opened BOOLEAN,
                plate_number TEXT,
                vehicle_registered_to_name TEXT,
                vehicle_make TEXT,
                vehicle_model TEXT,
                vehicle_colour TEXT
            )
        ''')
        conn.commit()
        logger.info(f'PostgreSQL table checked/created successfully.')
    except psycopg2.Error as sql_error:
        logger.error(f'PostgreSQL error while creating/checking the table: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error creating/checking the table in PostgreSQL: {str(e)}')

# Function to log an entry in the local SQLite database
def log_entry_sqlite(reason,
                     image_path,
                     plate_recognized,
                     score,
                     fuzzy_match,
                     gate_opened,
                     plate_number,
                     vehicle_registered_to_name,
                     vehicle_make,
                     vehicle_model,
                     vehicle_colour):
    # Use UTC time for timestamp
    current_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn = sqlite3.connect(db_file_path)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO log (timestamp, reason, image_path, plate_recognized, score, fuzzy_match, gate_opened, plate_number, vehicle_registered_to_name, vehicle_make, vehicle_model, vehicle_colour) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (current_time, reason, image_path, plate_recognized, score, 'True' if fuzzy_match else 'False', 'True' if gate_opened else 'False', plate_number, vehicle_registered_to_name, vehicle_make, vehicle_model, vehicle_colour))
        conn.commit()
        logger.info(f'Logged an entry in the SQLite database for plate: {plate_number}')
    except sqlite3.Error as sql_error:
        logger.error(f'SQLite error while logging an entry: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error while logging an entry in SQLite: {str(e)}')
    finally:
        conn.close()

# Function to log an entry in the remote PostgreSQL database
def log_entry_postgres(reason,
                       image_path,
                       plate_recognized,
                       score,
                       fuzzy_match,
                       gate_opened,
                       plate_number,
                       vehicle_registered_to_name,
                       vehicle_make,
                       vehicle_model,
                       vehicle_colour):
    # Use UTC time for timestamp
    current_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn_str = f"dbname={postgres_database} user={postgres_user} password={postgres_password} host={postgres_host} port={postgres_port} sslmode={postgres_sslmode} options=endpoint={postgres_endpoint}"
        conn = psycopg2.connect(conn_str)

        # Ensure the log table exists
        create_table_postgres(conn)

        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO log (
            timestamp, reason, image_path, plate_recognized, score,
            fuzzy_match, gate_opened, plate_number,
            vehicle_registered_to_name, vehicle_make, vehicle_model, vehicle_colour)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (current_time, reason, image_path, plate_recognized, score,
          fuzzy_match, gate_opened, plate_number,
          vehicle_registered_to_name, vehicle_make, vehicle_model, vehicle_colour))

        conn.commit()
        logger.info(f'Logged an entry in the PostgreSQL database for plate: {plate_number}')
    except psycopg2.Error as sql_error:
        logger.error(f'PostgreSQL error while logging an entry: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error while logging an entry in PostgreSQL: {str(e)}')
    finally:
        conn.close()