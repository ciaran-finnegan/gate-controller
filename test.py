import os
from ftplib import FTP, error_perm
import unittest

# Configuration with fallback to hardcoded values if environment variables do not exist.
FTP_IP = os.getenv('FTP_IP', 'raspberrypi')
FTP_USER = os.getenv('FTP_USER', 'ftp-user')
FTP_PASSWORD = os.getenv('FTP_PASSWORD', 'some-complex-password')
FILE_PATH = '~/Downloads/TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg'
REMOTE_FILE_NAME = 'TEST_IMAGE_WITH_VALID_LICENCSE_PLATE.jpg'

class TestFTPUpload(unittest.TestCase):

    def test_ftp_upload(self):
        # Connect to FTP Server
        ftp = FTP(FTP_IP)
        
        # Login
        login_status = ftp.login(user=FTP_USER, passwd=FTP_PASSWORD)
        self.assertIn('230', login_status)  # 230 means login successful
        
        # Delete file if exists
        try:
            ftp.delete(REMOTE_FILE_NAME)
            print(f"Deleted {REMOTE_FILE_NAME} on the server.")
        except error_perm as e:
            print(f"No file named {REMOTE_FILE_NAME} to delete on the server. Continuing...")

        # Upload file
        file_path = os.path.expanduser(FILE_PATH)
        with open(file_path, 'rb') as file:
            upload_status = ftp.storbinary(f'STOR {REMOTE_FILE_NAME}', file)
            self.assertIn('226', upload_status)  # 226 means file transfer complete
        
        # Cleanup
        ftp.quit()

if __name__ == '__main__':
    unittest.main()
