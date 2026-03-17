class UserService:
    def __init__(self, db):
        self.db = db

    def create_user(self, name, email):
        return self.db.insert("users", {"name": name, "email": email})

    def get_user(self, user_id):
        return self.db.find("users", user_id)
