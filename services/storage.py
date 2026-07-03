import requests
import os

# Your Supabase details


# File to upload (local path on your phone)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SERVICE_ROLE_KEY = os.environ.get("SERVICE_ROLE_KEY")
BUCKET = "Study Hub"
headers = {
    "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
    "Content-Type": "application/octet-stream"
}

import secrets
import datetime
from werkzeug.utils import secure_filename


class FilenameService:
    """Generate secure, unique filenames for uploads"""
    
    @staticmethod
    def _get_extension(original_filename):
        """Extract and validate file extension"""
        if '.' not in original_filename:
            raise ValueError("File has no extension")
        
        ext = original_filename.rsplit('.', 1)[1].lower()
        return ext
    
    @staticmethod
    def _generate_token(length=8):
        """Generate random hex token"""
        return secrets.token_hex(length)
    
    @staticmethod
    def _get_timestamp():
        """Get current timestamp in YYYYMMDD format"""
        return datetime.datetime.utcnow().strftime('%Y%m%d')
    
    @staticmethod
    def _get_date_path():
        """Get date-based folder path (YYYY/MM)"""
        now = datetime.datetime.utcnow()
        return f"{now.year}/{now.month:02d}"
    
        
    # ========================================
    # AVATAR FILENAMES
    # ========================================
    
    @staticmethod
    def generate_avatar_filename(user_id, original_filename):
        """
        Generate avatar filename
        Format: user_{id}_{timestamp}_{token}.{ext}
        Example: user_123_20240101_a3f5c9.jpg
        """
        ext = FilenameService._get_extension(original_filename)
        timestamp = FilenameService._get_timestamp()
        token = FilenameService._generate_token(6)
        
        return f"user_{user_id}_{timestamp}_{token}.{ext}"
    
    @staticmethod
    def get_avatar_path(user_id, original_filename):
        """
        Get full Cloudinary path for avatar
        Returns: (folder, filename)
        Example: ("studyhub/avatars", "user_123_20240101_a3f5c9.jpg")
        """
        filename = FilenameService.generate_avatar_filename(user_id, original_filename)
        return "avatars", filename
    
    # ========================================
    # POST FILENAMES
    # ========================================
    
    @staticmethod
    def generate_post_filename(post_id, original_filename, file_type="image"):
        """
        Generate post file filename
        Format: post_{id}_{timestamp}_{token}.{ext}
        Example: post_456_20240101_f3a5c9b2.jpg
        """
        ext = FilenameService._get_extension(original_filename)
        timestamp = FilenameService._get_timestamp()
        token = FilenameService._generate_token(8)
        
        return f"post_{post_id}_{timestamp}_{token}.{ext}"
    
    @staticmethod
    def get_post_file_path(post_id, original_filename, file_type="image"):
        """
        Get full path for post file with date-based folders
        Returns: (folder, filename)
        Example: ("studyhub/posts/images/2024/01", "post_456_20240101_f3a5c9b2.jpg")
        """
        date_path = FilenameService._get_date_path()
        filename = FilenameService.generate_post_filename(post_id, original_filename, file_type)
        folder = f"posts/{file_type}s/{date_path}"
        
        return folder, filename
    
    # ========================================
    # MESSAGE FILENAMES
    # ========================================
    
    @staticmethod
    def generate_message_filename(message_id, original_filename):
        """
        Generate message attachment filename
        Format: msg_{id}_{token}.{ext}
        Example: msg_789_a3f5c9b2.jpg
        """
        ext = FilenameService._get_extension(original_filename)
        token = FilenameService._generate_token(8)
        
        return f"msg_{message_id}_{token}.{ext}"
    
    @staticmethod
    def get_message_file_path(message_id, original_filename, file_type="image"):
        """
        Get full path for message attachment
        Returns: (folder, filename)
        Example: ("studyhub/messages/images", "msg_789_a3f5c9b2.jpg")
        """
        filename = FilenameService.generate_message_filename(message_id, original_filename)
        folder = f"messages/{file_type}s"
        
        return folder, filename
    
    # ========================================
    # RESOURCE LIBRARY FILENAMES
    # ========================================
    
    @staticmethod
    def generate_resource_filename(resource_id, original_filename):
        """
        Generate resource library filename
        Format: resource_{id}_{timestamp}_{token}.{ext}
        Example: resource_12_20240101_9b1c4d.pdf
        """
        ext = FilenameService._get_extension(original_filename)
        timestamp = FilenameService._get_timestamp()
        token = FilenameService._generate_token(8)
        
        return f"resource_{resource_id}_{timestamp}_{token}.{ext}"
    
    @staticmethod
    def get_resource_file_path(resource_id, original_filename, file_type="document"):
        """
        Get full path for resource file
        Returns: (folder, filename)
        Example: ("studyhub/resources/documents/2024/01", "resource_12_20240101_9b1c4d.pdf")
        """
        date_path = FilenameService._get_date_path()
        filename = FilenameService.generate_resource_filename(resource_id, original_filename)
        folder = f"resources/{file_type}s/{date_path}"
        
        return folder, filename
    
    # ========================================
    # AI ASSISTANT TEMP FILES
    # ========================================
    
    @staticmethod
    def generate_ai_temp_filename(user_id, original_filename):
        """
        Generate temporary filename for AI uploads
        Format: ai_temp_{user_id}_{token}.{ext}
        """
        ext = FilenameService._get_extension(original_filename)
        token = FilenameService._generate_token(8)
        return f"ai_temp_{user_id}_{token}.{ext}"
    
    @staticmethod
    def get_ai_temp_path(user_id, original_filename):
        """
        Get path for temporary AI uploads
        Returns: (bucket, path, filename)
        """
        filename = FilenameService.generate_ai_temp_filename(user_id, original_filename)
        path = f"ai-uploads/user_{user_id}/{filename}"
        return BUCKET, path, filename

class CloudinaryStorage:
    def __init__(self):
        self.api_name = API_NAME
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        if not all([self.api_name, self.api_key, self.api_secret]):
            raise ValueError("Cloudknary setup detsi is incomplete")
    def upload_file(folder, filename, type):
        result = cloudinary.uploader.upload(file, folder, type=type)
        if result:
            url = result["secure_url"]
            return url
        

class SupabaseStorage:
    """Handle Supabase storage operations without Python SDK"""
    
    def __init__(self):
        self.supabase_url = SUPABASE_URL
        self.service_key = SERVICE_ROLE_KEY
        
        if not self.supabase_url or not self.service_key:
            raise ValueError("SUPABASE_URL and SERVICE_ROLE_KEY must be set")
    
    def upload_file(self, file_data, bucket, path, content_type=None):
        """
        Upload file to Supabase storage
        
        Args:
            file_data: Binary file data (bytes)
            bucket: Bucket name (studyhub or studyhub-private)
            path: Full path in bucket (e.g., "ai-uploads/user_123/file.jpg")
            content_type: MIME type (auto-detected if None)
        
        Returns:
            dict: {"success": bool, "url": str, "path": str, "error": str}
        """
        upload_url = f"{self.supabase_url}/storage/v1/object/{bucket}/{path}"
        
        headers = {
            "Authorization": f"Bearer {self.service_key}",
        }
        
        if content_type:
            headers["Content-Type"] = content_type
        
        try:
            response = requests.post(
                upload_url,
                headers=headers,
                data=file_data,
                timeout=30
            )
            
            if response.status_code in [200, 201]:
                # Generate public URL
                public_url = f"{self.supabase_url}/storage/v1/object/public/{bucket}/{path}"
                
                return {
                    "success": True,
                    "url": public_url,
                    "path": path,
                    "bucket": bucket,
                    "error": None
                }
            else:
                return {
                    "success": False,
                    "url": None,
                    "path": path,
                    "bucket": bucket,
                    "error": f"Upload failed: {response.status_code} - {response.text}"
                }
        
        except Exception as e:
            return {
                "success": False,
                "url": None,
                "path": path,
                "bucket": bucket,
                "error": str(e)
            }
    
    def delete_file(self, bucket, path):
        """
        Delete file from Supabase storage
        
        Returns:
            dict: {"success": bool, "error": str}
        """
        delete_url = f"{self.supabase_url}/storage/v1/object/{bucket}/{path}"
        
        headers = {
            "Authorization": f"Bearer {self.service_key}",
        }
        
        try:
            response = requests.delete(delete_url, headers=headers, timeout=10)
            
            if response.status_code in [200, 204]:
                return {"success": True, "error": None}
            else:
                return {
                    "success": False,
                    "error": f"Delete failed: {response.status_code}"
                }
        
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def upload_ai_file(self, file, user_id):
        """
        Upload file for AI conversation (images and documents)
        
        Args:
            file: Flask FileStorage object
            user_id: User ID
        
        Returns:
            dict: File metadata for storing in message
        """
        try:
            # Get file info
            filename = secure_filename(file.filename)
            
            # Generate path using FilenameService
            bucket, path, generated_filename = FilenameService.get_ai_temp_path(
                user_id, filename
            )
            
            # Read file data
            file.seek(0)
            file_data = file.read()
            file_size = len(file_data)
            
            # Get MIME type
            import mimetypes
            mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            
            # Upload to Supabase
            result = self.upload_file(file_data, bucket, path, mime_type)
            
            if result["success"]:
                return {
                    "success": True,
                    "metadata": {
                        "filename": filename,
                        "path": path,
                        "url": result["url"],
                        "size": file_size,
                        "mime_type": mime_type,
                        "bucket": bucket
                    },
                    "error": None
                }
            else:
                return {
                    "success": False,
                    "metadata": None,
                    "error": result["error"]
                }
        
        except Exception as e:
            return {
                "success": False,
                "metadata": None,
                "error": str(e)
            }
    
    def cleanup_temp_files(self, user_id, older_than_hours=24):
        """
        Clean up temporary AI upload files older than specified hours
        (Optional - implement if needed)
        """
        # Implementation for cleaning old temp files
        # You can call this periodically via a cron job
        pass

supabase_storage = SupabaseStorage()
    



