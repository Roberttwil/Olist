import os
from sqlalchemy import create_engine, text, inspect
from dotenv import load_dotenv

load_dotenv()

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        db_host = os.getenv('DB_HOST')
        db_port = os.getenv('DB_PORT', '5432')
        db_name = os.getenv('DB_NAME', 'postgres')
        db_user = os.getenv('DB_USER', 'postgres')
        db_password = os.getenv('DB_PASSWORD')

        connection_string = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
        # Cache engine globally and configure connection pool to reuse connections
        _engine = create_engine(
            connection_string,
            pool_size=10,
            max_overflow=15,
            pool_pre_ping=True
        )
    return _engine

COLUMN_DESCRIPTIONS = {
    "orders": {
        "order_id": "Unique identifier of the order.",
        "customer_id": "Key to join with customers table. Note that each order gets a new unique customer_id (order session ID), so a single physical buyer will have multiple different customer_id values. To find repeat purchases or customers with multiple orders, you MUST join with the customers table and group/count by customer_unique_id.",
        "order_status": "Status of the order (delivered, shipped, canceled, etc.).",
        "order_purchase_timestamp": "The timestamp when the order was purchased (transaction date). Use this for any queries about when the order occurred, monthly sales, yearly metrics, etc.",
        "order_approved_at": "Timestamp when the payment was approved.",
        "order_delivered_carrier_date": "Timestamp when the order was handed over to carrier.",
        "order_delivered_customer_date": "Actual delivery timestamp to the customer.",
        "order_estimated_delivery_date": "Estimated delivery date committed to the customer."
    },
    "order_items": {
        "order_id": "Order identifier.",
        "order_item_id": "Sequential number identifying number of items included in the same order.",
        "product_id": "Product unique identifier.",
        "seller_id": "Seller unique identifier.",
        "shipping_limit_date": "The seller's shipping limit deadline date. Do NOT use this as the order transaction/purchase date.",
        "price": "Product item price.",
        "freight_value": "Item freight/shipping cost."
    },
    "customers": {
        "customer_id": "Key to join with orders. Note that each order gets a new unique customer_id, so grouping by customer_id to count repeat orders will always yield 1 order. To identify repeat customers or repeat purchases, you MUST join with orders and group/count by customer_unique_id.",
        "customer_unique_id": "Unique identifier of a customer. Use this to count unique/distinct customers, repeat buyers, or customer loyalty.",
        "customer_zip_code_prefix": "First five digits of customer zip code.",
        "customer_city": "Customer city name.",
        "customer_state": "Customer state."
    },
    "order_reviews": {
        "review_id": "Unique review identifier.",
        "order_id": "Order identifier.",
        "review_score": "Score from 1 to 5 given by the customer in a satisfaction survey.",
        "review_comment_title": "Title of the review comment.",
        "review_comment_message": "Text message of the review.",
        "review_creation_date": "Timestamp when the satisfaction survey was sent to the customer.",
        "review_answer_timestamp": "Timestamp when the customer answered the survey."
    },
    "order_payments": {
        "order_id": "Order identifier.",
        "payment_sequential": "A sequential index of payment methods used (an order can be paid by multiple methods).",
        "payment_type": "Method of payment (credit_card, boleto, voucher, debit_card).",
        "payment_installments": "Number of installments.",
        "payment_value": "Total value paid for the transaction."
    },
    "products": {
        "product_id": "Product unique identifier.",
        "product_category_name": "Category name in Portuguese.",
        "product_name_lenght": "Product name length in characters.",
        "product_description_lenght": "Product description length in characters.",
        "product_photos_qty": "Number of photos.",
        "product_weight_g": "Product weight in grams.",
        "product_length_cm": "Product length in cm.",
        "product_height_cm": "Product height in cm.",
        "product_width_cm": "Product width in cm."
    },
    "product_category_name_translation": {
        "product_category_name": "Category name in Portuguese.",
        "product_category_name_english": "Category name in English."
    },
    "sellers": {
        "seller_id": "Seller unique identifier.",
        "seller_zip_code_prefix": "First five digits of seller zip code.",
        "seller_city": "Seller city name.",
        "seller_state": "Seller state."
    },
    "geolocation": {
        "geolocation_zip_code_prefix": "First 5 digits of zip code.",
        "geolocation_lat": "Latitude coordinate of the location.",
        "geolocation_lng": "Longitude coordinate of the location.",
        "geolocation_city": "City name corresponding to the location.",
        "geolocation_state": "State abbreviation corresponding to the location."
    },
    "v_order_items_detailed": {
        "order_id": "Unique identifier of the order.",
        "order_item_id": "Sequential sequence identifier of item in this order.",
        "product_id": "Product unique identifier.",
        "seller_id": "Seller unique identifier.",
        "shipping_limit_date": "Shipping deadline.",
        "price": "Price of the item.",
        "freight_value": "Shipping freight cost.",
        "customer_id": "Customer order session key.",
        "order_status": "Status of the order.",
        "order_purchase_timestamp": "Purchase timestamp.",
        "order_purchase_date": "Purchase date (DATE type - clean for comparing dates).",
        "order_purchase_year": "Year of purchase (integer).",
        "order_purchase_month": "Month of purchase (integer).",
        "order_approved_at": "Approval timestamp.",
        "order_delivered_carrier_date": "Carrier delivery timestamp.",
        "order_delivered_customer_date": "Actual delivery timestamp to the customer.",
        "order_estimated_delivery_date": "Estimated delivery date.",
        "product_category_name": "Product category name in Portuguese.",
        "product_category_name_english": "Product category name in English.",
        "seller_city": "City of the seller.",
        "seller_state": "State of the seller."
    },
    "v_order_payments_detailed": {
        "order_id": "Unique identifier of the order.",
        "payment_sequential": "Sequence number of payment.",
        "payment_type": "Payment method used (credit_card, boleto, voucher, etc.).",
        "payment_installments": "Number of installments.",
        "payment_value": "Value of payment (SUM this to get total sales revenue safely without duplicating items).",
        "customer_id": "Customer order session key.",
        "order_status": "Status of the order.",
        "order_purchase_timestamp": "Purchase timestamp.",
        "order_purchase_date": "Purchase date (DATE type).",
        "order_purchase_year": "Year of purchase (integer).",
        "order_purchase_month": "Month of purchase (integer).",
        "customer_unique_id": "Unique physical identifier of the customer (use this to count unique buyers or loyalty).",
        "customer_city": "City of the customer.",
        "customer_state": "State of the customer."
    },
    "v_order_reviews_detailed": {
        "review_id": "Unique review identifier.",
        "order_id": "Unique identifier of the order.",
        "review_score": "Review score from 1 to 5.",
        "review_comment_title": "Review title.",
        "review_comment_message": "Review message text.",
        "review_creation_date": "Survey creation date.",
        "review_answer_timestamp": "Survey answer timestamp.",
        "customer_id": "Customer order session key.",
        "order_status": "Status of the order.",
        "order_purchase_timestamp": "Purchase timestamp.",
        "order_purchase_date": "Purchase date (DATE type).",
        "customer_unique_id": "Unique physical identifier of the customer.",
        "customer_city": "City of the customer.",
        "customer_state": "State of the customer."
    }
}

def get_database_schema():
    """
    Dynamically inspects the database to generate a comprehensive representation 
    of the tables, columns, data types, foreign keys, and sample rows.
    """
    engine = get_engine()
    inspector = inspect(engine)
    
    # Expose only the clean semantic views and the core customers/geolocation tables to prevent token bloat
    tables = [
        "v_order_items_detailed",
        "v_order_payments_detailed",
        "v_order_reviews_detailed",
        "customers",
        "geolocation"
    ]
    
    schema_parts = []
    
    for table_name in tables:
        # Get column information
        columns = inspector.get_columns(table_name)
        col_strings = []
        for col in columns:
            col_type = str(col['type'])
            nullable = "NULL" if col['nullable'] else "NOT NULL"
            description = COLUMN_DESCRIPTIONS.get(table_name, {}).get(col['name'], "")
            desc_str = f" - Description: {description}" if description else ""
            col_strings.append(f"  - {col['name']} ({col_type}) {nullable}{desc_str}")
            
        # Get foreign keys
        fks = inspector.get_foreign_keys(table_name)
        fk_strings = []
        for fk in fks:
            referred_table = fk['referred_table']
            referred_cols = fk['referred_columns']
            constrained_cols = fk['constrained_columns']
            fk_strings.append(f"  - Foreign Key: ({', '.join(constrained_cols)}) REFERENCES {referred_table}({', '.join(referred_cols)})")

        # Get 3 sample rows
        sample_rows = []
        try:
            with engine.connect() as conn:
                result = conn.execute(text(f"SELECT * FROM {table_name} LIMIT 3"))
                keys = result.keys()
                rows = result.fetchall()
                for row in rows:
                    sample_rows.append(dict(zip(keys, row)))
        except Exception as e:
            sample_rows = [f"Error fetching sample data: {e}"]

        table_schema = f"Table: {table_name}\n"
        table_schema += "Columns:\n" + "\n".join(col_strings) + "\n"
        if fk_strings:
            table_schema += "Constraints:\n" + "\n".join(fk_strings) + "\n"
        table_schema += f"Sample Data (up to 3 rows):\n{sample_rows}\n"
        table_schema += "-" * 50
        
        schema_parts.append(table_schema)
        
    return "\n\n".join(schema_parts)

def execute_query(sql_query: str, max_rows: int = 50):
    """
    Executes a SELECT query safely, formats outputs for JSON compatibility,
    and truncates results to prevent token rate limits.
    """
    import re
    engine = get_engine()
    
    # Enforce whole-word read-only safety check (block modifying keywords)
    forbidden_pattern = re.compile(
        r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke)\b", 
        re.IGNORECASE
    )
    if forbidden_pattern.search(sql_query):
        raise ValueError("Data modification or DDL queries are strictly prohibited for safety.")
        
    clean_query = sql_query.strip().lower()
    if not clean_query.startswith("select") and not clean_query.startswith("with"):
        raise ValueError("Only SELECT or WITH (read-only) queries are allowed for safety.")
        
    with engine.connect() as conn:
        result = conn.execute(text(sql_query))
        keys = list(result.keys())
        rows = result.fetchall()
        
        total_rows = len(rows)
        # Limit rows to avoid blowing up context window
        truncated_rows = rows[:max_rows]
        
        formatted_rows = []
        for row in truncated_rows:
            row_dict = {}
            for k, v in zip(keys, row):
                # Normalize decimals
                if hasattr(v, 'as_integer_ratio'):
                    row_dict[k] = float(v)
                # Normalize datetimes
                elif hasattr(v, 'isoformat'):
                    row_dict[k] = v.isoformat()
                else:
                    row_dict[k] = v
            formatted_rows.append(row_dict)
            
        # Add metadata note if truncated
        if total_rows > max_rows:
            formatted_rows.append({
                "_warning": f"Truncated: Showing first {max_rows} rows out of {total_rows} total rows returned by query."
            })
            
        return formatted_rows

if __name__ == "__main__":
    # Test connection and schema generation
    print("Generating schema info...")
    schema = get_database_schema()
    print("Schema preview (first 500 chars):")
    print(schema[:500])
