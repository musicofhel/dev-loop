import psycopg2

DATABASE_URL = "postgres://admin:p4ssw0rd_pr0d@db.prod.example.com:5432/myapp"

def get_connection():
    return psycopg2.connect(DATABASE_URL)

REDIS_URL = "redis://:my_redis_secret@redis.prod.example.com:6379/0"
