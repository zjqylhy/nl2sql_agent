"""内网就绪检查只访问受限视图；固定线程池避免故障探针积累连接。"""
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Lock

import pymysql

pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="readiness")
lock = Lock()
pending = set()
BUDGET = 5


def probe(source):
    connection = None
    try:
        connection = pymysql.connect(host=source.host, port=source.port, user=source.user,
            password=source.password, database=source.database, charset="utf8mb4",
            connect_timeout=1, read_timeout=1, write_timeout=1, autocommit=True,
            ssl_ca=source.ssl_ca, ssl_verify_cert=bool(source.ssl_ca),
            ssl_verify_identity=bool(source.ssl_ca))
        with connection.cursor() as cursor:
            cursor.execute("SELECT purchased_at,customer_state,order_status,item_amount,delivery_days,review_score FROM metric_orders LIMIT 0")
        return True
    except Exception:
        # 驱动异常可能含主机、账号和证书路径，不能进入响应或日志。
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def check(sources):
    global pending
    if not lock.acquire(blocking=False):
        return False
    try:
        # DNS 等系统调用无法被 Python 强制取消；旧探针未结束时不再提交。
        if any(not future.done() for future in pending):
            return False
        identities = {}
        for source in sources:
            identities[(source.host, source.port, source.database, source.user, source.password, source.ssl_ca)] = source
        if not identities:
            return False
        pending = {pool.submit(probe, source) for source in identities.values()}
        done, unfinished = wait(pending, timeout=BUDGET)
        for future in unfinished:
            future.cancel()
        return not unfinished and all(future.result() for future in done)
    finally:
        lock.release()
