GITHUB_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
API_BASE = "https://api.github.com"

def get_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}"}
