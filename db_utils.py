import os
import logging
import sqlite3
import psycopg2
import datetime

# Retrieve the root logger
logger = logging.getLogger()

# Specify the full path to the SQLite database directory and filename
db_directory = '/opt/gate-controller/data/'
db_filename = 'gate-controller-database.db'
db_file_path = os.path.join(db_directory, db_filename)

# PostgreSQL database configuration [Replace with your actual credentials or retrieve them securely]
postgres_database = 'your_database_name'
postgres_user = 'your_user'
postgres_password = 'your_password'
postgres_host = 'your_host'


def create_database_table() -> None:
    """
    Create a database table if it doesn't exist.
    """
    logger.info(f'db_file_path: {db_file_path}')

    if not os.path.exists(db_directory):
        os.makedirs(db_directory)

    try:
        with sqlite3.connect(db_file_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS log (
                    id INTEGER PRIMARY KEY,
                    timestamp DATETIME,
                    image_path TEXT,
                    plate_recognized TEXT,
                    score REAL,
                    fuzzy_match TEXT,
                    gate_opened TEXT
                )
            ''')
            logger.info('Database table created successfully.')
    except sqlite3.Error as sql_error:
        logger.error(f'SQLite error while creating the table: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error creating the table: {str(e)}')


def log_entry_sqlite(image_path: str, plate_recognized: str, score: float,
                     fuzzy_match: bool = False, gate_opened: bool = False) -> None:
    """
    Log an entry into the SQLite database.
    """
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with sqlite3.connect(db_file_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO log (timestamp, image_path, plate_recognized, score, fuzzy_match, gate_opened) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (current_time, image_path, plate_recognized, score, 'Yes' if fuzzy_match else 'No', 'Yes' if gate_opened else 'No'))
            logger.info(f'Logged an entry in the SQLite database for plate: {plate_recognized}')
    except sqlite3.Error as sql_error:
        logger.error(f'SQLite error while logging an entry: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error while logging an entry in SQLite: {str(e)}')


def log_entry_postgres(image_path: str, plate_recognized: str, score: float,
                       fuzzy_match: bool = False, gate_opened: bool = False) -> None:
    """
    Log an entry into the PostgreSQL database.
    """
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with psycopg2.connect(dbname=postgres_database, user=postgres_user,
                              password=postgres_password, host=postgres_host) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO log (timestamp, image_path, plate_recognized, score, fuzzy_match, gate_opened)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (current_time, image_path, plate_recognized, score, fuzzy_match, gate_opened))
            logger.info(f'Logged an entry in the PostgreSQL database for plate: {plate_recognized}')
    except psycopg2.Error as sql_error:
        logger.error(f'PostgreSQL error while logging an entry: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error while logging an entry in PostgreSQL: {str(e)}')


def log_entry(image_path: str, plate_recognized: str, score: float, 
              fuzzy_match: bool = False, gate_opened: bool = False) -> None:
    """
    Log an entry in both the SQLite and PostgreSQL databases.
    """
    logging.info(f"log_entry called with image_path: {image_path}, plate_recognized: {plate_recognized}, "
                 f"score: {score}, fuzzy_match: {fuzzy_match}, gate_opened: {gate_opened}")
    
    log_entry_sqlite(image_path, plate_recognized, score, fuzzy_match, gate_opened)
    logging.info(f"log_entry_sqlite called with image_path: {image_path}, plate_recognized: {plate_recognized}, "
                 f"score: {score}, fuzzy_match: {fuzzy_match}, gate_opened: {gate_opened}")
    
    log_entry_postgres(image_path, plate_recognized, score, fuzzy_match, gate_opened)
    logging.info(f"log_entry_postgres called with image_path: {image_path}, plate_recognized: {plate_recognized}, "
                 f"score: {score}, fuzzy_match: {fuzzy_match}, gate_opened: {gate_opened}")
