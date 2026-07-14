import pymysql
import certifi
import os
from dotenv import load_dotenv

load_dotenv('secrets/.env')

src_db = 'last'
target_db = 'test'

conn = pymysql.connect(
    host=os.getenv('DB_HOST'),
    port=int(os.getenv('DB_PORT', '4000')),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    ssl={'ca': certifi.where()},
    autocommit=True
)
cur = conn.cursor()

# 1. 대상 데이터베이스 생성
cur.execute(f"CREATE DATABASE IF NOT EXISTS {target_db}")
print(f"[{target_db}] 데이터베이스 생성 완료.")

# 2. 모든 테이블을 읽어서 복사
cur.execute(f"USE {src_db}")
cur.execute("SHOW TABLES")
tables = [row[0] for row in cur.fetchall()]

# 외래 키 무시 설정 (복원 시 충돌 방지)
cur.execute("SET FOREIGN_KEY_CHECKS=0;")

for table in tables:
    print(f"[{table}] 복구 작업 시작...")
    
    cur.execute(f"SHOW CREATE TABLE {src_db}.{table}")
    create_stmt = cur.fetchone()[1]
    
    cur.execute(f"USE {target_db}")
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(create_stmt)
    
    # 데이터 복사
    cur.execute(f"INSERT INTO {target_db}.{table} SELECT * FROM {src_db}.{table}")
    print(f"[{table}] 복구 완료.")

cur.execute("SET FOREIGN_KEY_CHECKS=1;")
conn.close()
print("모든 데이터페이스 복구 작업이 완료되었습니다!")
