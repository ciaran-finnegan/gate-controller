import unittest
from unittest.mock import patch, Mock
import os
from s3_utils import upload_image_to_s3  # Adjusted import statement

class TestUploadImageToS3(unittest.TestCase):

    @patch("boto3.client")
    @patch("os.path.isfile")
    def test_upload_image_to_s3_success(self, mock_isfile, mock_boto_client):
        mock_isfile.return_value = True
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        os.environ["AWS_ACCESS_KEY"] = "dummy_access_key"
        os.environ["AWS_SECRET_KEY"] = "dummy_secret_key"
        os.environ["AWS_S3_BUCKET"] = "dummy_bucket"
        image_file_path = "~/Downloads/TEST_IMAGE_WITH_VALID_LICENCSE_PLATE"
        result = upload_image_to_s3(image_file_path)
        mock_boto_client.assert_called_once_with(
            "s3",
            aws_access_key_id="dummy_access_key",
            aws_secret_access_key="dummy_secret_key",
        )
        mock_s3.upload_file.assert_called_once_with(image_file_path, "dummy_bucket", "image.jpg")
        self.assertEqual(result, "https://dummy_bucket.s3.amazonaws.com/image.jpg")

    @patch("os.path.isfile")
    def test_upload_image_to_s3_file_not_found(self, mock_isfile):
        mock_isfile.return_value = False
        image_file_path = "path/to/nonexistent_image.jpg"
        result = upload_image_to_s3(image_file_path)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
