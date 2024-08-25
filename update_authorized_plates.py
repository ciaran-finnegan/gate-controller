import os
import psycopg2
import csv
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# PostgreSQL database configuration (use your existing environment variables)
postgres_database = os.environ.get('POSTGRES_DATABASE')
postgres_user = os.environ.get('POSTGRES_USER')
postgres_password = os.environ.get('POSTGRES_PASSWORD')
postgres_host = os.environ.get('POSTGRES_HOST')
postgres_port = 5432  # Default PostgreSQL port
postgres_sslmode = 'require'

# Path to the CSV file
csv_file_path = '/opt/gate-controller/authorised_licence_plates.csv'

def update_csv_from_postgres():
    try:
        conn_str = f"dbname={postgres_database} user={postgres_user} password={postgres_password} host={postgres_host} port={postgres_port} sslmode={postgres_sslmode}"
        conn = psycopg2.connect(conn_str)
        cursor = conn.cursor()

        # Fetch the authorised plates 
        cursor.execute("SELECT plate, name, colour, make, model FROM plates;")
        rows = cursor.fetchall()

        # Write the data to the CSV file
        with open(csv_file_path, mode='w', newline='', encoding='utf-8') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(['plate', 'name', 'colour', 'make', 'model'])  # Write header
            for row in rows:
                csv_writer.writerow(row)

        conn.close()
        logger.info(f'Successfully updated {csv_file_path} from PostgreSQL.')
    except psycopg2.Error as sql_error:
        logger.error(f'PostgreSQL error: {str(sql_error)}')
    except Exception as e:
        logger.error(f'Error: {str(e)}')

if __name__ == "__main__":
    update_csv_from_postgres()