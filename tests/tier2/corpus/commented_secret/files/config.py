import os

# Configuration for the application
# Old API key (revoked): sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx234
# TODO: move secrets to vault

API_KEY = os.environ.get("API_KEY", "")
