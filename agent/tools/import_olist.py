"""把官方下载的 Olist CSV 导入指定租户，先按订单聚合再生成受限视图。"""
import argparse
import csv
import getpass
import os
import re
from pathlib import Path

import pymysql


TABLES = {
    "orders": ("olist_orders_dataset.csv", ["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_delivered_customer_date"], "order_id CHAR(32) PRIMARY KEY, customer_id CHAR(32) NOT NULL, order_status VARCHAR(24) NOT NULL, order_purchase_timestamp DATETIME NOT NULL, order_delivered_customer_date DATETIME NULL, INDEX(order_purchase_timestamp)"),
    "customers": ("olist_customers_dataset.csv", ["customer_id", "customer_state"], "customer_id CHAR(32) PRIMARY KEY, customer_state CHAR(2) NOT NULL"),
    "items": ("olist_order_items_dataset.csv", ["order_id", "order_item_id", "price"], "order_id CHAR(32) NOT NULL, order_item_id INT NOT NULL, price DECIMAL(18,2) NOT NULL, PRIMARY KEY(order_id, order_item_id)"),
    "reviews": ("olist_order_reviews_dataset.csv", ["review_id", "order_id", "review_score", "review_creation_date", "review_answer_timestamp"], "review_id CHAR(32) NOT NULL, order_id CHAR(32) NOT NULL, review_score INT NOT NULL, review_creation_date DATETIME NOT NULL, review_answer_timestamp DATETIME NULL, INDEX(order_id)"),
}


def chunks(iterator, size=2000):
    batch = []
    for row in iterator:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def provision(connection, folder: Path, schema: str, readonly_user: str, readonly_password: str, states: list[str], *, use_existing_database=False, skip_account_provisioning=False):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", schema) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", readonly_user):
        raise ValueError("schema 和账号必须是受限标识符")
    if any(not re.fullmatch("[A-Z]{2}", state) for state in states):
        raise ValueError("州代码格式无效")
    # 已有 schema 必须显式由管理员另行管理，避免导入意外覆盖线上数据。
    with connection.cursor() as cursor:
        if use_existing_database:
            cursor.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s", (schema,))
            if cursor.fetchone()[0] != 1:
                raise ValueError("指定数据库不存在")
            cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s", (schema,))
            if cursor.fetchone()[0] != 0:
                raise ValueError("指定数据库非空，导入已拒绝")
        else:
            cursor.execute("CREATE DATABASE " + schema + " CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
        cursor.execute("USE " + schema)
        for table, (filename, columns, ddl) in TABLES.items():
            path = folder / filename
            cursor.execute(f"CREATE TABLE {table} ({ddl})")
            with path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                if not set(columns).issubset(reader.fieldnames or []):
                    raise ValueError(f"{filename} 缺少必需字段")
                insert = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})"
                for batch in chunks(([row[column] or None for column in columns] for row in reader)):
                    cursor.executemany(insert, batch)
            print("已导入", table)
        # 先去重评价并聚合商品，避免联表把金额或评分重复累计。
        cursor.execute("""CREATE TABLE order_facts AS
            SELECT o.order_id, o.order_status, o.order_purchase_timestamp AS purchased_at,
                   c.customer_state, i.item_amount,
                   CASE WHEN o.order_status='delivered' AND o.order_delivered_customer_date>=o.order_purchase_timestamp
                        THEN TIMESTAMPDIFF(SECOND,o.order_purchase_timestamp,o.order_delivered_customer_date)/86400.0 END AS delivery_days,
                   r.review_score
            FROM orders o JOIN customers c ON c.customer_id=o.customer_id
            LEFT JOIN (SELECT order_id,SUM(price) AS item_amount FROM items GROUP BY order_id) i ON i.order_id=o.order_id
            LEFT JOIN (SELECT order_id,review_score FROM (
                SELECT order_id,review_score,ROW_NUMBER() OVER(PARTITION BY order_id ORDER BY review_creation_date DESC,review_answer_timestamp DESC,review_id DESC,review_score DESC) AS row_number_value
                FROM reviews WHERE review_score BETWEEN 1 AND 5) ranked WHERE row_number_value=1) r ON r.order_id=o.order_id""")
        cursor.execute("ALTER TABLE order_facts ADD PRIMARY KEY(order_id), ADD INDEX(purchased_at,customer_state)")
        cursor.execute("SELECT (SELECT COUNT(*) FROM orders), (SELECT COUNT(*) FROM order_facts)")
        imported, aggregated = cursor.fetchone()
        if imported != aggregated:
            raise ValueError("订单聚合发生丢失，请检查客户关联；不会创建查询账号")
        filter_sql = " WHERE customer_state IN (" + ",".join("'" + state + "'" for state in states) + ")" if states else ""
        cursor.execute("CREATE SQL SECURITY DEFINER VIEW metric_orders AS SELECT order_id,order_status,purchased_at,customer_state,item_amount,delivery_days,review_score FROM order_facts" + filter_sql)
        if not skip_account_provisioning:
            cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (readonly_user, readonly_password))
            cursor.execute(f"GRANT SELECT ON {schema}.metric_orders TO %s@'%%'", (readonly_user,))
        cursor.execute("SELECT COUNT(*) AS order_count, MAX(purchased_at) AS latest_date FROM order_facts")
        print("订单校验：", cursor.fetchone(), "；请据此设置 analysis_date")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--readonly-user", required=True)
    parser.add_argument("--states", default="", help="可选行级权限，例如 SP,RJ")
    parser.add_argument("--use-existing-database", action="store_true", help="仅导入已存在的空数据库")
    parser.add_argument("--skip-account-provisioning", action="store_true", help="只导入，不创建账号或修改授权")
    args = parser.parse_args()
    missing = [filename for filename, _, _ in TABLES.values() if not (args.csv_dir / filename).is_file()]
    if missing:
        parser.error("缺少官方下载 CSV：" + ", ".join(missing))
    admin_password = os.getenv("MYSQL_ADMIN_PASSWORD") or getpass.getpass("MySQL 管理密码：")
    readonly_password = "" if args.skip_account_provisioning else (os.getenv("MYSQL_READONLY_PASSWORD") or getpass.getpass("新只读账号密码："))
    if not args.skip_account_provisioning and len(readonly_password) < 12:
        parser.error("只读账号密码至少 12 字符")
    ca = os.getenv("MYSQL_SSL_CA")
    connection = pymysql.connect(host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")), user=os.getenv("MYSQL_ADMIN_USER", "root"), password=admin_password, autocommit=True, ssl_ca=ca, ssl_verify_cert=bool(ca), ssl_verify_identity=bool(ca))
    try:
        provision(connection, args.csv_dir, args.schema, args.readonly_user, readonly_password, [s for s in args.states.split(",") if s], use_existing_database=args.use_existing_database, skip_account_provisioning=args.skip_account_provisioning)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
