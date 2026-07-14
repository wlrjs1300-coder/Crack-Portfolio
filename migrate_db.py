import pymysql
import certifi
import os
from dotenv import load_dotenv

# Load environment variables
base_dir = os.path.dirname(os.path.abspath(__file__))
# Note: This script is intended to be run from the project root or with proper path to .env
env_path = os.path.join(base_dir, 'secrets', '.env')
load_dotenv(env_path)

db_host = os.getenv('DB_HOST')
db_port = int(os.getenv('DB_PORT', '4000'))
db_user = os.getenv('DB_USER')
db_password = os.getenv('DB_PASSWORD')
src_db = 'test'
dest_db = 'last'

print(f"Connecting to {db_host}...")

try:
    # Connect without database specified to create the new one
    conn = pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        ssl={'ca': certifi.where()},
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

    with conn.cursor() as cursor:
        # 1. Disable foreign key checks
        print("Disabling foreign key checks...")
        cursor.execute("SET FOREIGN_KEY_CHECKS=0;")
        
        # 2. Create the new database
        print(f"Creating database {dest_db} if not exists...")
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {dest_db} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        
        # 3. Get list of tables from source database
        print(f"Fetching tables from {src_db}...")
        cursor.execute(f"SHOW TABLES FROM {src_db}")
        tables = [list(row.values())[0] for row in cursor.fetchall()]
        
        print(f"Found tables: {tables}")
        
        for table in tables:
            print(f"Processing table: {table}")
            
            # 4. Get CREATE TABLE statement
            cursor.execute(f"SHOW CREATE TABLE {src_db}.{table}")
            create_stmt = cursor.fetchone()['Create Table']
            
            # The create_stmt will have the table name without database prefix.
            # We should ensure it's created in the destination database.
            cursor.execute(f"USE {dest_db}")
            
            # Drop table if exists to be safe (or just skip if it exists)
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
            cursor.execute(create_stmt)
            
            # 5. Copy data
            print(f"Copying data for {table}...")
            cursor.execute(f"INSERT INTO {dest_db}.{table} SELECT * FROM {src_db}.{table}")
            
        # 6. Re-enable foreign key checks
        print("Re-enabling foreign key checks...")
        cursor.execute("SET FOREIGN_KEY_CHECKS=1;")
        conn.commit()
    
    print("Migration completed successfully.")

except Exception as e:
    print(f"Error during migration: {e}")
finally:
    if 'conn' in locals():
        conn.close()
