import os
import sys
import time
import sqlalchemy
try:
    from tabulate import tabulate
except ImportError:
    tabulate = None
from database import get_engine, execute_query
from agent import create_agent_graph
from dotenv import load_dotenv
import builtins
def print(*args, **kwargs):
    text = " ".join(str(arg) for arg in args)
    try:
        builtins.print(text, **kwargs)
    except UnicodeEncodeError:
        safe_text = text.encode('ascii', errors='replace').decode('ascii')
        builtins.print(safe_text, **kwargs)

load_dotenv()

# Test Suite: Ground-truth questions and gold-standard SQL queries
EVAL_TEST_SUITE = [
    {
        "id": 1,
        "question": "How many customers are located in Sao Paulo?",
        "gold_sql": "SELECT COUNT(*) FROM customers WHERE customer_city = 'sao paulo';"
    },
    {
        "id": 2,
        "question": "What is the total value of all payments?",
        "gold_sql": "SELECT SUM(payment_value) FROM order_payments;"
    },
    {
        "id": 3,
        "question": "Which seller has the most order items?",
        "gold_sql": "SELECT seller_id, COUNT(*) as items_count FROM order_items GROUP BY seller_id ORDER BY items_count DESC LIMIT 1;"
    },
    {
        "id": 4,
        "question": "List the top 3 payment types by usage count.",
        "gold_sql": "SELECT payment_type, COUNT(*) as usage_count FROM order_payments GROUP BY payment_type ORDER BY usage_count DESC LIMIT 3;"
    },
    {
        "id": 5,
        "question": "What is the average review score for products in the 'perfumaria' category?",
        "gold_sql": "SELECT AVG(r.review_score) FROM order_reviews r JOIN order_items i ON r.order_id = i.order_id JOIN products p ON i.product_id = p.product_id WHERE p.product_category_name = 'perfumaria';"
    },
    {
        "id": 6,
        "question": "How many orders were purchased in September 2018?",
        "gold_sql": "SELECT COUNT(*) FROM orders WHERE order_purchase_timestamp >= '2018-09-01 00:00:00' AND order_purchase_timestamp < '2018-10-01 00:00:00';"
    },
    {
        "id": 7,
        "question": "What is the average price of products sold in the state of SP?",
        "gold_sql": "SELECT AVG(oi.price) FROM order_items oi JOIN orders o ON oi.order_id = o.order_id JOIN customers c ON o.customer_id = c.customer_id WHERE c.customer_state = 'SP';"
    },
    {
        "id": 8,
        "question": "What is the average delivery time in days for orders purchased in the state of SP?",
        "gold_sql": "SELECT AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp))/86400) FROM orders o JOIN customers c ON o.customer_id = c.customer_id WHERE c.customer_state = 'SP' AND o.order_status = 'delivered';"
    },
    {
        "id": 9,
        "question": "Which product category has the highest average payment value per order, and what is that average value?",
        "gold_sql": "SELECT p.product_category_name, AVG(op.payment_value) as avg_payment FROM products p JOIN order_items oi ON p.product_id = oi.product_id JOIN order_payments op ON oi.order_id = op.order_id GROUP BY p.product_category_name ORDER BY avg_payment DESC LIMIT 1;"
    },
    {
        "id": 10,
        "question": "For orders in the state of SP, what is the percentage of payments made with a value greater than 100?",
        "gold_sql": "SELECT CAST(SUM(CASE WHEN op.payment_value > 100 THEN 1 ELSE 0 END) AS DECIMAL) * 100 / COUNT(*) FROM order_payments op JOIN orders o ON op.order_id = o.order_id JOIN customers c ON o.customer_id = c.customer_id WHERE c.customer_state = 'SP';"
    }
]

def normalize_value(val):
    """
    Standardizes numbers, dates, and strings for safe comparisons.
    """
    if val is None:
        return None
    # If it is a decimal/float, round it
    if isinstance(val, (float, sqlalchemy.Numeric)) or hasattr(val, 'as_integer_ratio'):
        return round(float(val), 2)
    # Convert numbers to integers if they represent whole counts
    if isinstance(val, int):
        return val
    # Handle dates/datetimes by converting to string ISO format
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    # Strip string and convert to lowercase for comparison
    return str(val).strip().lower()

def normalize_result_set(results):
    """
    Converts database result sets (lists of dicts) into normalized list of tuples for ordering-insensitive comparison.
    """
    if not results:
        return []
    
    normalized = []
    for row in results:
        normalized_row = {}
        for k, v in row.items():
            normalized_row[k.lower()] = normalize_value(v)
        # Sort row items by key to ensure order consistency
        sorted_items = tuple(sorted(normalized_row.items()))
        normalized.append(sorted_items)
    
    # Sort the list of rows to be order-agnostic (unless explicitly testing ORDER BY, but usually we care about content)
    # We sort by the string representation of the rows
    return sorted(normalized, key=str)

def compare_results(gold_res, agent_res):
    """
    Compares the gold-standard database result against the agent's database result.
    """
    if not gold_res and not agent_res:
        return True
    if bool(gold_res) != bool(agent_res):
        return False
        
    norm_gold = normalize_result_set(gold_res)
    norm_agent = normalize_result_set(agent_res)
    
    return norm_gold == norm_agent

def main():
    print("=" * 70)
    print("        Olist SQL Agent Evaluation Suite (Execution Accuracy)")
    print("=" * 70)
    
    # Check API key config
    groq_key = os.getenv("GROQ_API_KEY_1") or os.getenv("GROQ_API_KEY_2") or os.getenv("GROQ_API_KEY")
    if not groq_key or "gsk_" not in groq_key:
        print("Error: GROQ_API_KEY_1 or GROQ_API_KEY_2 is not set or invalid in .env. Please configure your Groq keys before running evaluation.")
        sys.exit(1)
        
    try:
        graph = create_agent_graph(interrupt_before_query=False)
        print("Agent Graph Compiled: SUCCESS")
    except Exception as e:
        print(f"Error compiling graph: {e}")
        sys.exit(1)

    print(f"Loaded {len(EVAL_TEST_SUITE)} test cases from evaluation suite.")
    print("Starting evaluation execution...\n")
    
    results_summary = []
    total_cases = len(EVAL_TEST_SUITE)
    successful_syntax_count = 0
    correct_data_count = 0
    total_generation_time = 0.0
    total_self_heals = 0
    
    for case in EVAL_TEST_SUITE:
        print(f"Test Case {case['id']}: '{case['question']}'")
        
        # 1. Run Gold SQL to get ground truth
        try:
            gold_db_res = execute_query(case["gold_sql"])
        except Exception as e:
            print(f"  [Fatal] Gold SQL query failed on RDS: {e}")
            print(f"  Query was: {case['gold_sql']}")
            continue
            
        # 2. Run the SQL Agent
        start_time = time.time()
        agent_state = {
            "query": case["question"],
            "schema": "",
            "plan": [],
            "current_task_idx": 0,
            "task_results": {},
            "final_answer": "",
            "eval_feedback": "",
            "eval_retries": 0
        }
        
        config = {"configurable": {"thread_id": f"eval_test_case_{case['id']}"}}
        try:
            agent_output = graph.invoke(agent_state, config)
            gen_time = time.time() - start_time
            total_generation_time += gen_time
            
            plan = agent_output.get("plan", [])
            last_task = plan[-1] if plan else {}
            
            agent_sql = last_task.get("sql_query", "")
            agent_db_res = last_task.get("sql_result", "")
            agent_err = last_task.get("error_message", "")
            
            # Aggregate all retries across tasks
            agent_retries = sum(t.get("retry_count", 0) for t in plan) + agent_output.get("eval_retries", 0)
            total_self_heals += agent_retries
            
            # Evaluate syntax success
            syntax_ok = not bool(agent_err)
            if syntax_ok:
                successful_syntax_count += 1
                
            # Evaluate data accuracy
            data_ok = False
            if syntax_ok:
                data_ok = compare_results(gold_db_res, agent_db_res)
                if data_ok:
                    correct_data_count += 1
            
            print(f"  Generated SQL: {agent_sql}")
            print(f"  Syntax: {'SUCCESS' if syntax_ok else 'FAILED'}")
            print(f"  Self-Healed: {'YES' if agent_retries > 0 else 'NO'} ({agent_retries} retries)")
            print(f"  Data Match: {'CORRECT' if data_ok else 'INCORRECT'}")
            print(f"  Time Taken: {gen_time:.2f}s\n")
            
            results_summary.append({
                "ID": case["id"],
                "Question": case["question"][:35] + "...",
                "Syntax": "OK" if syntax_ok else "Err",
                "Self-Heal": f"Yes ({agent_retries})" if agent_retries > 0 else "No",
                "Data Match": "Correct" if data_ok else "Wrong",
                "Time": f"{gen_time:.1f}s"
            })
            
        except Exception as e:
            print(f"  [Fatal] Agent workflow threw exception: {e}\n")
            results_summary.append({
                "ID": case["id"],
                "Question": case["question"][:35] + "...",
                "Syntax": "Fatal",
                "Self-Heal": "No",
                "Data Match": "Wrong",
                "Time": "0s"
            })

    # Print Summary Table
    print("=" * 70)
    print("                           EVALUATION REPORT")
    print("=" * 70)
    
    # Try printing with tabulate if available
    if tabulate is not None:
        headers = ["ID", "Question", "Syntax", "Self-Heal", "Data Match", "Time"]
        table_rows = [[r[h] for h in headers] for r in results_summary]
        print(tabulate(table_rows, headers=headers, tablefmt="grid"))
    else:
        # Fallback tabulate
        print(f"{'ID':<3} | {'Question':<38} | {'Syntax':<7} | {'Self-Heal':<10} | {'Data Match':<10} | {'Time':<5}")
        print("-" * 75)
        for r in results_summary:
            print(f"{r['ID']:<3} | {r['Question']:<38} | {r['Syntax']:<7} | {r['Self-Heal']:<10} | {r['Data Match']:<10} | {r['Time']:<5}")
            
    # Calculate overall metrics
    syntax_rate = (successful_syntax_count / total_cases) * 100
    accuracy_rate = (correct_data_count / total_cases) * 100
    avg_time = total_generation_time / total_cases if total_cases > 0 else 0
    
    print("\n" + "=" * 70)
    print("                            OVERALL METRICS")
    print("=" * 70)
    print(f"Overall Syntax Success Rate : {syntax_rate:.1f}% ({successful_syntax_count}/{total_cases})")
    print(f"Overall Execution Accuracy  : {accuracy_rate:.1f}% ({correct_data_count}/{total_cases})")
    print(f"Average Response Latency   : {avg_time:.2f} seconds")
    print(f"Total Self-Heal Recoveries  : {total_self_heals} occurrences")
    print("=" * 70)

if __name__ == "__main__":
    main()
