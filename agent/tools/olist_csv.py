"""Olist 原始 CSV 的公共列契约与流式分批读取。"""
TABLES = {'orders': ('olist_orders_dataset.csv', ['order_id', 'customer_id', 'order_status', 'order_purchase_timestamp', 'order_delivered_customer_date']), 'customers': ('olist_customers_dataset.csv', ['customer_id', 'customer_state']), 'items': ('olist_order_items_dataset.csv', ['order_id', 'order_item_id', 'price']), 'reviews': ('olist_order_reviews_dataset.csv', ['review_id', 'order_id', 'review_score', 'review_creation_date', 'review_answer_timestamp'])}


def chunks(iterator, size=2000):
    batch = []
    for row in iterator:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
