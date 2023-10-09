import os
import logging
import psycopg2
import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database credentials & configuration as environment variables
db_conf_vars = ['POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_HOST', 'POSTGRES_DATABASE']

# Function to build PostgreSQL connection string
def build_conn_string(db_name, user, password, host, port=5432, 
                      sslmode='require', 
                      options='endpoint=ep-falling-mountain-55618104-pooler'):
    """
    Construct a connection string for PostgreSQL.

    Returns a string that can be used to connect to PostgreSQL using psycopg2.connect().
    """
    return (f"dbname={db_name} user={user} password={password} "
            f"host={host} port={port} sslmode={sslmode} options={options}")

# Function to log entry into PostgreSQL
def log_entry_postgres(image_path, plate_recognized, score, fuzzy_match, gate_opened, conn_str):
    """
    Log an entry into the PostgreSQL database.

    This function inserts logging data into a table named `log` in the PostgreSQL database.
    """
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        # Connect to PostgreSQL
        with psycopg2.connect(conn_str) as conn:
            cursor = conn.cursor()
            
            # Insert log entry into the database
            cursor.execute('''
                INSERT INTO log (timestamp, image_path, plate_recognized, score, fuzzy_match, gate_opened)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (current_time, image_path, plate_recognized, score, fuzzy_match, gate_opened))
            
            # Commit the transaction
            conn.commit()
            
            logger.info('Entry logged successfully in PostgreSQL.')
    except psycopg2.Error as e:
        logger.error(f"Database error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")

# Retrieving credentials from environment variables and logging them safely
db_conf = {var: os.environ.get(var) for var in db_conf_vars}

for var, value in db_conf.items():
    logger.info(f"{var}: {'Present' if value else 'Not present'}")

# Constructing the connection string
conn_str = build_conn_string(db_conf['POSTGRES_DATABASE'], 
                             db_conf['POSTGRES_USER'], 
                             db_conf['POSTGRES_PASSWORD'], 
                             db_conf['POSTGRES_HOST'])

# Example: Logging an entry to PostgreSQL
log_entry_postgres('/path/to/image.jpg', 'ABC123', 0.95, True, True, conn_str)
