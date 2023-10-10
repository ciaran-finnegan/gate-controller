import boto3
import logging
import os
from botocore.exceptions import NoCredentialsError, PartialCredentialsError

from logger import configure_logger

# Import the log_entry function from db_utils.py

from db_utils import log_entry, create_table_sqlite

# Configure logging

logger = configure_logger()


def upload_image_to_s3(image_file_path):
    """
    Uploads an image to an AWS S3 bucket.

    Parameters:
    - image_file_path (str): The path to the image file to upload.

    Returns:
    - str: The URL of the uploaded image file or None if the upload fails.
    """

    # AWS Configuration
    AWS_ACCESS_KEY = os.environ.get('AWS_ACCESS_KEY')
    AWS_SECRET_KEY = os.environ.get('AWS_SECRET_KEY')
    AWS_S3_BUCKET = os.environ.get('AWS_S3_BUCKET')

    # Validate image file path
    if not os.path.isfile(image_file_path):
        logger.error(f"The provided file path {image_file_path} does not exist or is not a file.")
        return None
    
    try:
        # Initialize boto3 S3 client
        s3 = boto3.client('s3', aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)

        # Extract file name from path
        filename = os.path.basename(image_file_path)

        # Upload the file
        s3.upload_file(image_file_path, AWS_S3_BUCKET, filename)
        logger.info(f"{filename} uploaded successfully to {AWS_S3_BUCKET}")

        # Generate the file URL
        file_url = f"https://{AWS_S3_BUCKET}.s3.amazonaws.com/{filename}"
        logger.info(f"File URL: {file_url}")

        return file_url

    except FileNotFoundError:
        logger.error(f"{image_file_path} not found.")
    except NoCredentialsError:
        logger.error("Credentials not available")
    except PartialCredentialsError:
        logger.error("Incomplete credentials provided")
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
    
    return None
