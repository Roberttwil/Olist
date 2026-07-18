import os
import sys
import time
import csv
import io
import argparse
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Dictionary mapping CSV file names to table names
CSV_TO_TABLE = {
    'olist_customers_dataset.csv': 'customers',
    'olist_geolocation_dataset.csv': 'geolocation',
    'olist_order_items_dataset.csv': 'order_items',
    'olist_order_payments_dataset.csv': 'order_payments',
    'olist_order_reviews_dataset.csv': 'order_reviews',
    'olist_orders_dataset.csv': 'orders',
    'olist_products_dataset.csv': 'products',
    'olist_sellers_dataset.csv': 'sellers',
    'product_category_name_translation.csv': 'product_category_name_translation'
}

# Timestamp columns to be parsed for each CSV
TIMESTAMP_COLUMNS = {
    'orders': [
        'order_purchase_timestamp', 
        'order_approved_at', 
        'order_delivered_carrier_date', 
        'order_delivered_customer_date', 
        'order_estimated_delivery_date'
    ],
    'order_items': ['shipping_limit_date'],
    'order_reviews': ['review_creation_date', 'review_answer_timestamp']
}

def psql_insert_copy(table, conn, keys, data_iter):
    """
    Ultra-fast insertion method using PostgreSQL COPY command.
    """
    # Get the underlying DBAPI connection
    dbapi_conn = conn.connection
    with dbapi_conn.cursor() as cur:
        s_buf = io.StringIO()
        writer = csv.writer(s_buf)
        writer.writerows(data_iter)
        s_buf.seek(0)

        columns = ', '.join([f'"{k}"' for k in keys])
        table_name = f'"{table.name}"'
        if table.schema:
            table_name = f'"{table.schema}".{table_name}'

        sql = f'COPY {table_name} ({columns}) FROM STDIN WITH CSV NULL AS \'\''
        cur.copy_expert(sql=sql, file=s_buf)

def get_db_engine():
    db_host = os.getenv('DB_HOST')
    db_port = os.getenv('DB_PORT', '5432')
    db_name = os.getenv('DB_NAME', 'postgres')
    db_user = os.getenv('DB_USER', 'postgres')
    db_password = os.getenv('DB_PASSWORD')

    if not db_password or db_password == 'your_aws_rds_password':
        print("Error: DB_PASSWORD is not set or has placeholder value in .env file.")
        print("Please edit the .env file and set your actual RDS password.")
        sys.exit(1)

    connection_string = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    print(f"Connecting to database at: {db_host}:{db_port}/{db_name}")
    return create_engine(connection_string)

def run_ddl_constraints(engine):
    """
    Apply primary keys and foreign keys to structure the database schema.
    """
    print("\n--- Applying DDL Constraints (Primary & Foreign Keys) ---")
    
    constraints = [
        # Primary Keys
        ("ALTER TABLE customers ADD PRIMARY KEY (customer_id);", "PK on customers"),
        ("ALTER TABLE sellers ADD PRIMARY KEY (seller_id);", "PK on sellers"),
        ("ALTER TABLE products ADD PRIMARY KEY (product_id);", "PK on products"),
        ("ALTER TABLE orders ADD PRIMARY KEY (order_id);", "PK on orders"),
        
        # Foreign Keys
        ("ALTER TABLE orders ADD CONSTRAINT fk_orders_customers FOREIGN KEY (customer_id) REFERENCES customers(customer_id);", "FK orders -> customers"),
        ("ALTER TABLE order_items ADD CONSTRAINT fk_items_orders FOREIGN KEY (order_id) REFERENCES orders(order_id);", "FK order_items -> orders"),
        ("ALTER TABLE order_items ADD CONSTRAINT fk_items_products FOREIGN KEY (product_id) REFERENCES products(product_id);", "FK order_items -> products"),
        ("ALTER TABLE order_items ADD CONSTRAINT fk_items_sellers FOREIGN KEY (seller_id) REFERENCES sellers(seller_id);", "FK order_items -> sellers"),
        ("ALTER TABLE order_payments ADD CONSTRAINT fk_payments_orders FOREIGN KEY (order_id) REFERENCES orders(order_id);", "FK order_payments -> orders"),
        ("ALTER TABLE order_reviews ADD CONSTRAINT fk_reviews_orders FOREIGN KEY (order_id) REFERENCES orders(order_id);", "FK order_reviews -> orders"),
    ]

    for sql, desc in constraints:
        try:
            with engine.connect() as conn:
                with conn.begin():
                    conn.execute(text(sql))
            print(f"Successfully applied: {desc}")
        except Exception as e:
            # Catch failures (e.g. constraints already exist or duplicate records)
            print(f"Skipped/Failed to apply {desc}: {e}")

def main():
    parser = argparse.ArgumentParser(description="Ingest Olist CSV files into AWS RDS PostgreSQL.")
    parser.add_argument('--data-dir', type=str, required=True, help="Directory path where the Olist CSV files are located.")
    args = parser.parse_args()

    data_dir = args.data_dir
    if not os.path.exists(data_dir):
        print(f"Error: Directory '{data_dir}' does not exist.")
        sys.exit(1)

    # Resolve database engine
    try:
        engine = get_db_engine()
        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Database connection test: SUCCESS")
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

    # Iterate and upload each file
    for csv_file, table_name in CSV_TO_TABLE.items():
        file_path = os.path.join(data_dir, csv_file)
        if not os.path.exists(file_path):
            print(f"\n[Warning] File '{csv_file}' not found in '{data_dir}'. Skipping ingestion for '{table_name}'.")
            continue

        print(f"\n--- Ingesting {csv_file} -> '{table_name}' table ---")
        start_time = time.time()

        try:
            # Read CSV in chunks or directly
            # For massive files (like geolocation), we process in chunks to optimize memory
            chunk_size = 50000
            
            # Read first chunk to inspect columns
            df_preview = pd.read_csv(file_path, nrows=5)
            
            # Identify columns to parse as datetime
            parse_dates = []
            if table_name in TIMESTAMP_COLUMNS:
                for col in TIMESTAMP_COLUMNS[table_name]:
                    if col in df_preview.columns:
                        parse_dates.append(col)

            print(f"Parsing dates: {parse_dates}" if parse_dates else "No datetime columns to parse.")

            first_chunk = True
            total_rows = 0

            # Set up iterator to read CSV in chunks
            chunks = pd.read_csv(
                file_path, 
                chunksize=chunk_size, 
                parse_dates=parse_dates,
                keep_default_na=False, # Keep empty fields as empty strings for Postgres COPY
                na_values=[''] # Define NaN
            )

            for chunk in chunks:
                # Convert datetime columns to pandas datetime type and handle NaT
                for col in parse_dates:
                    chunk[col] = pd.to_datetime(chunk[col], errors='coerce')
                    # Replace NaT with None so they insert as NULL in DB
                    chunk[col] = chunk[col].where(chunk[col].notnull(), None)

                # Write to database
                if first_chunk:
                    # Drop table if exists and recreate it
                    chunk.to_sql(
                        name=table_name,
                        con=engine,
                        if_exists='replace',
                        index=False,
                        method=psql_insert_copy
                    )
                    first_chunk = False
                else:
                    # Append subsequent chunks
                    chunk.to_sql(
                        name=table_name,
                        con=engine,
                        if_exists='append',
                        index=False,
                        method=psql_insert_copy
                    )
                total_rows += len(chunk)
                print(f"Uploaded {total_rows} rows...")

            duration = time.time() - start_time
            print(f"Successfully ingested '{table_name}' ({total_rows} rows) in {duration:.2f} seconds.")

        except Exception as e:
            print(f"Error ingesting '{csv_file}': {e}")

    # Apply DDL constraints
    run_ddl_constraints(engine)
    print("\nIngestion process complete!")

if __name__ == '__main__':
    main()
