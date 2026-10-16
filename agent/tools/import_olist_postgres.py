"""事务发布不可变 Olist 快照；仅授予独立查询账号视图 SELECT。"""
import argparse
import csv
import getpass
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql

from .analysis_revision import revision
from .import_olist import TABLES, chunks

DDL = {
    "orders": "order_id text PRIMARY KEY, customer_id text NOT NULL, order_status text NOT NULL, order_purchase_timestamp timestamp NOT NULL, order_delivered_customer_date timestamp",
    "customers": "customer_id text PRIMARY KEY, customer_state text NOT NULL",
    "items": "order_id text NOT NULL, order_item_id integer NOT NULL, price numeric(18,2) NOT NULL, PRIMARY KEY(order_id,order_item_id)",
    "reviews": "review_id text NOT NULL, order_id text NOT NULL, review_score integer NOT NULL, review_creation_date timestamp NOT NULL, review_answer_timestamp timestamp",
}


def provision(conn, folder: Path, schema: str, readonly_user: str, readonly_password: str, states=(), *, create_role=True):
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", name) for name in (schema, readonly_user)):
        raise ValueError("schema 或账号格式无效")
    if any(not re.fullmatch(r"[A-Z]{2}", state) for state in states):
        raise ValueError("州代码格式无效")
    digest = revision(folder)
    with conn.transaction(), conn.cursor() as cur:
        # 已有 schema 不覆盖；失败时表、数据、账号授权一起回滚。
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET LOCAL search_path={},pg_catalog").format(sql.Identifier(schema)))
        for table, (filename, columns, _) in TABLES.items():
            cur.execute(sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(table), sql.SQL(DDL[table])))
            with (folder / filename).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not set(columns) <= set(reader.fieldnames or []):
                    raise ValueError(f"{filename} 缺少字段")
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)), sql.SQL(",").join(sql.Placeholder() for _ in columns))
                for batch in chunks(([row[c] or None for c in columns] for row in reader)):
                    cur.executemany(statement, batch)
        cur.execute("""CREATE TABLE order_facts AS
            SELECT o.order_id,o.order_status,o.order_purchase_timestamp purchased_at,c.customer_state,i.item_amount,
                CASE WHEN o.order_status='delivered' AND o.order_delivered_customer_date>=o.order_purchase_timestamp
                  THEN EXTRACT(EPOCH FROM (o.order_delivered_customer_date-o.order_purchase_timestamp))/86400.0 END delivery_days,
                r.review_score
            FROM orders o JOIN customers c USING(customer_id)
            LEFT JOIN (SELECT order_id,SUM(price) item_amount FROM items GROUP BY order_id) i USING(order_id)
            LEFT JOIN (SELECT order_id,review_score FROM (
                SELECT order_id,review_score,ROW_NUMBER() OVER(PARTITION BY order_id ORDER BY review_creation_date DESC,
                  review_answer_timestamp DESC NULLS LAST,review_id DESC,review_score DESC) rn
                FROM reviews WHERE review_score BETWEEN 1 AND 5) ranked WHERE rn=1) r USING(order_id)""")
        cur.execute("ALTER TABLE order_facts ADD PRIMARY KEY(order_id)")
        cur.execute("CREATE INDEX ON order_facts(purchased_at,customer_state)")
        cur.execute("SELECT (SELECT count(*) FROM orders)=(SELECT count(*) FROM order_facts)")
        if not cur.fetchone()[0]:
            raise ValueError("聚合丢失订单，导入已回滚")
        restriction = sql.SQL(" WHERE customer_state IN ({})").format(sql.SQL(",").join(map(sql.Literal, states))) if states else sql.SQL("")
        cur.execute(sql.SQL("CREATE VIEW metric_orders WITH (security_barrier=true) AS SELECT order_id,order_status,purchased_at,customer_state,item_amount,delivery_days,review_score FROM order_facts{}").format(restriction))
        cur.execute("CREATE TABLE dataset_release(revision text PRIMARY KEY, published_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("INSERT INTO dataset_release(revision) VALUES(%s)", (digest,))
        if create_role:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT").format(sql.Identifier(readonly_user), sql.Literal(readonly_password)))
        # schema USAGE 不授予原始表 SELECT；PG 视图默认按拥有者权限读取底表。
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(readonly_user)))
        cur.execute(sql.SQL("GRANT SELECT ON metric_orders TO {}").format(sql.Identifier(readonly_user)))
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(sql.Identifier(readonly_user)))
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--readonly-user", required=True)
    parser.add_argument("--states", nargs="*", default=[])
    args = parser.parse_args()
    password = os.getenv("OLIST_READONLY_PASSWORD") or getpass.getpass("只读账号密码: ")
    with psycopg.connect(os.environ["IMPORT_DATABASE_URL"], autocommit=True) as connection:
        print(provision(connection, args.csv_dir, args.schema, args.readonly_user, password, args.states))
