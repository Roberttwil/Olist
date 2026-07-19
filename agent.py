import os
import re
import json
from typing import TypedDict, Union, List, Dict, Any
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from dotenv import load_dotenv

from database import get_database_schema, execute_query

# Load env variables
import builtins
def print(*args, **kwargs):
    text = " ".join(str(arg) for arg in args)
    try:
        builtins.print(text, **kwargs)
    except UnicodeEncodeError:
        safe_text = text.encode('ascii', errors='replace').decode('ascii')
        builtins.print(safe_text, **kwargs)

load_dotenv()

# Define the structured info for individual sub-tasks
class TaskInfo(TypedDict):
    task_id: int
    description: str
    status: str                       # "pending", "completed", "failed", "clarification_needed"
    sql_query: str
    sql_result: Any
    error_message: str
    retry_count: int

# Define the State of the Planner-Executor-Critic pipeline
class AgentState(TypedDict, total=False):
    query: str                        # Original user question
    chat_history: List[Dict[str, str]] # List of past exchanges: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    intent: str                       # intent_classifier output
    schema: str                       # Database schema definition
    plan: List[TaskInfo]              # Decomposed list of sub-tasks
    current_task_idx: int             # Index of the task currently being executed
    task_results: Dict[str, Any]     # Combined results of all executed tasks
    final_answer: str                 # Final synthesized response
    eval_feedback: str                # Feedback from global critic
    eval_retries: int                 # Count of global re-planning loops

# Model Fallbacks
LARGE_MODELS_FALLBACK = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "qwen/qwen3-32b",
    "qwen/qwen3.6-27b"
]

SMALL_MODELS_FALLBACK = [
    "llama-3.1-8b-instant",
    "qwen/qwen3.6-27b",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b"
]

# Track active key and model indexes
current_key_idx = 0
current_large_model_idx = 0
current_small_model_idx = 0

def get_groq_api_keys() -> List[str]:
    keys = []
    key1 = os.getenv("GROQ_API_KEY_1")
    key2 = os.getenv("GROQ_API_KEY_2")
    if key1 and "gsk_" in key1:
        keys.append(key1)
    if key2 and "gsk_" in key2:
        keys.append(key2)
    # Fallback to single GROQ_API_KEY if present
    if not keys:
        single_key = os.getenv("GROQ_API_KEY")
        if single_key and "gsk_" in single_key:
            keys.append(single_key)
    return keys

def get_large_llm(api_key: str):
    model_name = LARGE_MODELS_FALLBACK[current_large_model_idx % len(LARGE_MODELS_FALLBACK)]
    print(f"Initializing Large Model: {model_name} (using key ending in ...{api_key[-6:]})")
    return ChatGroq(model=model_name, temperature=0.0, api_key=api_key, timeout=15.0)

def get_small_llm(api_key: str):
    model_name = SMALL_MODELS_FALLBACK[current_small_model_idx % len(SMALL_MODELS_FALLBACK)]
    print(f"Initializing Small Model: {model_name} (using key ending in ...{api_key[-6:]})")
    return ChatGroq(model=model_name, temperature=0.0, api_key=api_key, timeout=15.0)

def invoke_llm_with_retry(llm_creator_fn, messages, is_large=True, max_attempts=8):
    global current_key_idx, current_large_model_idx, current_small_model_idx
    keys = get_groq_api_keys()
    if not keys:
        raise ValueError("No valid GROQ API keys found in .env")
        
    last_exception = None
    for attempt in range(max_attempts):
        active_key = keys[current_key_idx % len(keys)]
        try:
            llm = llm_creator_fn(active_key)
            return llm.invoke(messages)
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "rate_limit" in err_msg.lower() or "limit reached" in err_msg.lower() or "413" in err_msg or "too large" in err_msg.lower():
                print(f"[Warning] API quota or size limit hit. Error: {err_msg}")
                # Switch API keys on even attempts, and models on odd attempts
                if attempt % 2 == 0:
                    current_key_idx = (current_key_idx + 1) % len(keys)
                    print(f"[Rotation] Switched API Key to #{current_key_idx + 1} (Attempt {attempt+1}/{max_attempts})...")
                else:
                    if is_large:
                        current_large_model_idx = (current_large_model_idx + 1) % len(LARGE_MODELS_FALLBACK)
                        new_model = LARGE_MODELS_FALLBACK[current_large_model_idx]
                        print(f"[Rotation] Switched Large Model to {new_model} (Attempt {attempt+1}/{max_attempts})...")
                    else:
                        current_small_model_idx = (current_small_model_idx + 1) % len(SMALL_MODELS_FALLBACK)
                        new_model = SMALL_MODELS_FALLBACK[current_small_model_idx]
                        print(f"[Rotation] Switched Small Model to {new_model} (Attempt {attempt+1}/{max_attempts})...")
                last_exception = e
                continue
            raise e
    raise last_exception or Exception("Failed to invoke LLM after model/key rotation attempts.")

# Helper function to extract a JSON list of tasks from model outputs
def parse_plan_json(text: str) -> List[Dict[str, Any]]:
    # Find anything between the first [ and last ]
    match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if match:
        json_str = match.group(0)
    else:
        # Fallback to try and find any square brackets
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            json_str = text[start:end+1]
        else:
            json_str = text
            
    try:
        data = json.loads(json_str)
        # Ensure it is a list of dicts
        if isinstance(data, list):
            return data
    except Exception as e:
        print(f"Error parsing plan JSON: {e}. Raw text was:\n{text}")
        
    # Recovery fallback plan (default single-task plan)
    return [{
        "task_id": 1,
        "description": "Execute a single SQL query to answer the question directly.",
        "status": "pending",
        "sql_query": "",
        "sql_result": None,
        "error_message": "",
        "retry_count": 0
    }]

def make_clarification_plan(message: str) -> List[Dict[str, Any]]:
    return [{
        "task_id": 1,
        "description": message,
        "status": "clarification_needed",
        "sql_query": "",
        "sql_result": None,
        "error_message": "",
        "retry_count": 0
    }]

DIRECT_INTENT_MESSAGES = {
    "greeting": (
        "Hello! I am your Olist e-commerce data analyst assistant. "
        "I can help you explore sales data, product performance, customer reviews, and other operational metrics. "
        "What would you like to ask?"
    ),
    "help_request": (
        "I can help you analyze Olist data, such as calculating total sales, finding top-selling product categories, "
        "comparing payment methods, checking customer reviews, or analyzing delivery performance. "
        "Please ask about any metric or historical period you'd like to explore."
    ),
    "out_of_scope": (
        "Sorry, that question is not relevant to the Olist e-commerce dataset. "
        "I can only assist with data analysis related to sales, orders, products, customers, payments, reviews, sellers, and shipping on Olist."
    ),
    "ambiguous_time": (
        "The Olist dataset contains historical data from 2016 to 2018, with the last available transaction period around August 2018. "
        "Which specific period would you like to check?"
    ),
    "ambiguous_analytics": (
        "Your question is still too general. For sales performance analysis, which metric would you like to see: "
        "total revenue, order count, number of items sold, monthly trends, best-selling categories, or seller performance?"
    ),
}

def normalize_user_query(query: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", query.lower())).strip()

def is_simple_greeting(query: str) -> bool:
    normalized = normalize_user_query(query)
    if not normalized:
        return True

    greeting_phrases = {
        "hi",
        "hello",
        "helo",
        "hey",
        "hai",
        "halo",
        "hallo",
        "pagi",
        "siang",
        "sore",
        "malam",
        "selamat pagi",
        "selamat siang",
        "selamat sore",
        "selamat malam",
        "assalamualaikum",
        "assalamu alaikum",
        "test",
        "tes",
    }
    return normalized in greeting_phrases

def is_help_request(query: str) -> bool:
    normalized = normalize_user_query(query)
    help_phrases = {
        "help",
        "bantuan",
        "cara pakai",
        "cara menggunakan",
        "gimana cara pakai",
        "bagaimana cara pakai",
        "bisa apa",
        "kamu bisa apa",
        "apa yang bisa kamu lakukan",
        "contoh pertanyaan",
        "pertanyaan apa saja",
        "ini apa",
        "kamu siapa",
    }
    return normalized in help_phrases

def has_relative_time_without_year(query: str) -> bool:
    normalized = normalize_user_query(query)
    relative_time_terms = {
        "hari ini",
        "kemarin",
        "besok",
        "minggu ini",
        "minggu lalu",
        "bulan ini",
        "bulan lalu",
        "tahun ini",
        "tahun lalu",
    }
    has_relative_term = any(term in normalized for term in relative_time_terms)
    has_year = bool(re.search(r"\b20\d{2}\b|\b19\d{2}\b", normalized))
    return has_relative_term and not has_year

def is_obvious_out_of_scope(query: str) -> bool:
    normalized = normalize_user_query(query)
    if not normalized:
        return False

    data_terms = {
        "olist", "order", "orders", "pesanan", "produk", "product", "products",
        "customer", "customers", "pelanggan", "seller", "sellers", "penjual",
        "payment", "payments", "pembayaran", "review", "reviews", "ulasan",
        "sales", "sale", "penjualan", "revenue", "omzet", "kategori", "category",
        "harga", "price", "freight", "ongkir", "pengiriman", "delivery",
        "delivered", "kota", "city", "state", "sp", "sao paulo", "rata rata",
        "average", "avg", "total", "jumlah", "berapa", "top", "terbanyak",
        "terlaris", "tertinggi", "terendah", "performa", "metric", "metrik",
    }
    if any(term in normalized for term in data_terms):
        return False

    out_of_scope_terms = {
        "presiden", "menteri", "cuaca", "berita", "news", "unpad", "universitas",
        "kampus", "resep", "masak", "film", "musik", "lagu", "translate",
        "terjemahkan", "python", "javascript", "kode", "coding", "programming",
        "joke", "lelucon", "cerita", "sejarah indonesia", "piala dunia",
    }
    return any(term in normalized for term in out_of_scope_terms)

def is_ambiguous_data_request(query: str) -> bool:
    normalized = normalize_user_query(query)
    if not normalized:
        return False

    ambiguous_patterns = {
        "bagaimana performa",
        "gimana performa",
        "performa penjualan",
        "performa produk",
        "performa seller",
        "performa pelanggan",
        "data penjualan",
        "penjualannya gimana",
        "gimana penjualan",
        "bagaimana penjualan",
        "analisis penjualan",
        "lihat penjualan",
        "cek penjualan",
    }
    if not any(pattern in normalized for pattern in ambiguous_patterns):
        return False

    specific_terms = {
        "total",
        "jumlah",
        "rata rata",
        "average",
        "avg",
        "top",
        "terbanyak",
        "terlaris",
        "tertinggi",
        "terendah",
        "persen",
        "persentase",
        "berapa",
        "bulanan",
        "monthly",
        "harian",
        "daily",
        "tahunan",
        "yearly",
        "kategori",
        "category",
        "state",
        "kota",
        "city",
        "seller",
        "customer",
        "review",
        "payment",
        "order",
        "pesanan",
        "2016",
        "2017",
        "2018",
    }
    return not any(term in normalized for term in specific_terms)

def classify_intent_deterministic(query: str) -> Union[str, None]:
    if is_simple_greeting(query):
        return "greeting"

    if is_help_request(query):
        return "help_request"

    return None

def classify_intent_fallback_heuristic(query: str) -> str:
    """
    Conservative fallback used only when the LLM intent router returns invalid JSON
    or is unavailable. The main routing decision should come from the classifier node.
    """
    if is_simple_greeting(query):
        return "greeting"

    if is_help_request(query):
        return "help_request"

    if is_obvious_out_of_scope(query):
        return "out_of_scope"

    if is_ambiguous_data_request(query):
        return "ambiguous_analytics"

    if has_relative_time_without_year(query):
        return "ambiguous_time"

    return "analytical_query"

def classify_intent_with_llm(state: AgentState) -> str:
    """
    Classifies whether a user message needs SQL planning before any schema/database work.
    This keeps intent routing separate from SQL generation.
    """
    valid_intents = {
        "analytical_query",
        "greeting",
        "help_request",
        "out_of_scope",
        "ambiguous_time",
        "ambiguous_analytics",
    }

    history_context = ""
    for msg in state.get("chat_history", [])[-6:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        history_context += f"{role}: {msg.get('content', '')}\n"

    system_prompt = (
        "You are an intent router for an Olist Brazilian e-commerce database assistant. "
        "Classify the user's message BEFORE any database schema inspection or SQL generation.\n\n"
        "Return ONLY compact JSON in this exact shape: {\"intent\":\"...\"}\n\n"
        "Valid intents:\n"
        "- analytical_query: The user asks for a concrete metric, count, sum, average, ranking, comparison, trend, filter, or analysis that can be answered from Olist data.\n"
        "- greeting: Simple greeting or small talk with no data request.\n"
        "- help_request: The user asks what the assistant can do or how to use it.\n"
        "- out_of_scope: The request is not about Olist e-commerce data analysis, even if it mentions time words.\n"
        "- ambiguous_time: The user asks an Olist data question using relative time like today, yesterday, this month, last month, or last year without a specific historical year/month.\n"
        "- ambiguous_analytics: The user asks about Olist data, but the metric or analysis target is too vague to choose a safe SQL query.\n\n"
        "Important routing rules:\n"
        "1. Questions with ranking or superlative words (best, worst, top, bottom, highest, lowest, most, least, paling, terbaik, terburuk, terjelek, tertinggi, terendah, terbanyak, terlaris) ARE concrete analytical queries — classify them as analytical_query.\n"
        "2. Do not classify broad questions like 'how is sales performance?' as analytical_query unless they specify a concrete metric, grouping, period, ranking, or comparison.\n"
        "3. Classify weather, politics, general knowledge, coding help, translation, recipes, entertainment, and unrelated campus/company questions as out_of_scope.\n"
        "4. If the message is a short follow-up (e.g. 'what is the product name?', 'show me the details', 'which city?') and the conversation history contains a prior Olist data question, classify it as analytical_query — not help_request or ambiguous.\n"
        "5. When in doubt between analytical_query and ambiguous_analytics, prefer analytical_query. Let the planner handle specificity.\n"
    )
    human_content = (
        f"Conversation History:\n{history_context}\n"
        f"Current User Message: {state.get('query', '')}"
    )

    response = invoke_llm_with_retry(
        get_small_llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=human_content)],
        is_large=False,
    )
    try:
        payload = json.loads(response.content.strip())
        intent = str(payload.get("intent", "")).strip().lower()
        if intent in valid_intents:
            return intent
    except Exception as e:
        print(f"Intent classifier JSON parse failed: {e}. Raw text was:\n{response.content}")

    return classify_intent_fallback_heuristic(state.get("query", ""))

def get_direct_response_plan(query: str) -> Union[List[Dict[str, Any]], None]:
    intent = classify_intent_deterministic(query)
    if intent in DIRECT_INTENT_MESSAGES:
        return make_clarification_plan(DIRECT_INTENT_MESSAGES[intent])
    return None

# ----------------- NODES -----------------

def intent_classifier_node(state: AgentState) -> Dict[str, Any]:
    print("[Node: intent_classifier] Classifying user intent before schema/SQL planning...")
    query = state.get("query", "")
    intent = classify_intent_deterministic(query)
    if intent is None:
        intent = classify_intent_with_llm(state)

    print(f"  Intent: {intent}")
    if intent in DIRECT_INTENT_MESSAGES:
        return {
            "intent": intent,
            "plan": make_clarification_plan(DIRECT_INTENT_MESSAGES[intent]),
            "current_task_idx": 0,
            "task_results": {},
            "eval_feedback": "",
            "schema": "",
        }

    return {
        "intent": "analytical_query",
        "plan": [],
        "current_task_idx": 0,
        "task_results": {},
        "eval_feedback": "",
    }

def schema_inspector_node(state: AgentState) -> Dict[str, Any]:
    print("[Node: schema_inspector] Inspecting database DDL and sample data...")
    schema = get_database_schema()
    return {"schema": schema}

def planner_node(state: AgentState) -> Dict[str, Any]:
    """
    Planner (Large Model): Decomposes the question into a logical sequence of sub-tasks.
    Integrates conversational history to resolve follow-up questions.
    """
    print("[Node: planner] Generating execution plan...")
    
    system_prompt = (
        "You are an expert database planner and system architect. Your goal is to decompose "
        "a complex user question about an e-commerce database into a sequence of simple "
        "SQL querying tasks. Each task should retrieve a specific subset of data required "
        "to solve the overall question.\n\n"
        "Here is the database schema:\n"
        f"{state['schema']}\n\n"
        "You also have access to the conversation history to help resolve follow-up questions.\n\n"
        "Return the output ONLY as a valid JSON array of objects. Do not write markdown text, "
        "explanations, or wrap it in anything other than raw JSON. Use this structure for each task object:\n"
        "{\n"
        "  \"task_id\": 1,\n"
        "  \"description\": \"Retrieve top 3 categories by total sales\",\n"
        "  \"status\": \"pending\",\n"
        "  \"requires_approval\": false,\n"
        "  \"sql_query\": \"\",\n"
        "  \"sql_result\": null,\n"
        "  \"error_message\": \"\",\n"
        "  \"retry_count\": 0\n"
        "}\n\n"
        "CRITICAL RULES:\n"
        "1. Prefer a SINGLE-task plan (1 step) using SQL JOINs or CTEs (WITH clauses) if the question can be resolved in a single PostgreSQL query. This is much more efficient than multiple roundtrips.\n"
        "2. ONLY split into multiple tasks if it is logically impossible or highly complex to write as a single query.\n"
        "3. RANKING & TOP-N QUERIES: If the question asks for the best, worst, top, bottom, highest, lowest, most, least — you MUST instruct the executor to:\n"
        "   - Aggregate using AVG() for scores/ratings, SUM() for revenue, COUNT() for volume.\n"
        "   - Always include ORDER BY <aggregated_column> ASC (for worst/lowest) or DESC (for best/highest).\n"
        "   - Always include HAVING COUNT(*) >= 5 (or similar minimum threshold) to exclude products/sellers with very few data points.\n"
        "   - Always include LIMIT 10 (or as appropriate).\n"
        "   - NEVER use MIN() or MAX() alone as the aggregate for ranking — they return edge outliers, not representative rankings.\n"
        "4. CLARIFICATION & OUT-OF-SCOPE HANDLING: \n"
        "   - If the user query is completely out-of-scope (e.g. general knowledge questions like 'unpad apaan', 'siapa presiden RI', etc.), do NOT generate SQL tasks. Generate a single task object with description explaining politely in English that the question is not relevant to the Olist e-commerce dataset and you cannot answer it. Example: {\"task_id\": 1, \"description\": \"Sorry, your question regarding Universitas Padjadjaran (UNPAD) is not relevant to the Olist e-commerce database. I can only assist you with sales, products, customers, reviews, and delivery operational metrics of Olist.\", \"status\": \"clarification_needed\", \"requires_approval\": false, \"sql_query\": \"\", \"sql_result\": null, \"error_message\": \"\", \"retry_count\": 0}\n"
        "   - If the user query is about the database but is extremely ambiguous, generate a single task object with description set to a polite clarifying question in English. Example: {\"task_id\": 1, \"description\": \"Sorry, does the sales data you requested refer to total revenue (payment_value) or the number of items sold?\", \"status\": \"clarification_needed\", \"requires_approval\": false, \"sql_query\": \"\", \"sql_result\": null, \"error_message\": \"\", \"retry_count\": 0}\n"
        "5. HISTORICAL DATASET & RELATIVE TIME: The Olist database contains static historical data from 2016 to 2018. If the user asks questions containing relative time expressions like 'bulan lalu', 'minggu ini', 'tahun lalu', or 'kemarin' without specifying a year, you MUST NOT generate SQL tasks. Instead, generate a single task object with description set to a polite clarifying question in English asking which specific historical month/year they want to check (reminding them that the last complete month is August 2018), and status set to \"clarification_needed\". Example: {\"task_id\": 1, \"description\": \"The Olist database contains static historical transaction data from 2016 to 2018. The latest complete transaction month available is August 2018. Did you mean August 2018, or another specific historical month and year?\", \"status\": \"clarification_needed\", \"requires_approval\": false, \"sql_query\": \"\", \"sql_result\": null, \"error_message\": \"\", \"retry_count\": 0}\n"
        "6. GREETINGS & SMALL TALK: If the user query is a simple greeting (like 'hi', 'hello', 'halo', 'siang') or small talk, do NOT generate SQL tasks. Instead, generate a single task object with description set to a warm, friendly greeting in English and a brief explanation of what you can help with, and status set to \"clarification_needed\". Example: {\"task_id\": 1, \"description\": \"Hello! I am your Olist e-commerce data analyst assistant. I can help you analyze sales data, product performance, customer reviews, and shipping operational metrics. What would you like to ask?\", \"status\": \"clarification_needed\", \"requires_approval\": false, \"sql_query\": \"\", \"sql_result\": null, \"error_message\": \"\", \"retry_count\": 0}\n"
        "7. APPROVAL REQUIREMENT RULE:\n"
        "   - Set \"requires_approval\" to false (default) for clear, standard, and direct database questions where you are highly confident about the table schemas and filter criteria (e.g. simple counting, summing payments, top selling items, order counts, etc.). This executes the query automatically for a faster user experience.\n"
        "   - Set \"requires_approval\" to true ONLY if you are making substantial assumptions about vague business terms, if the request is highly ambiguous/risky, or if you explicitly want the user to double-check and confirm the SQL before execution."
    )
    
    messages = [SystemMessage(content=system_prompt)]
    
    # Append chat history safely
    for msg in state.get("chat_history", []):
        content = msg.get("content") or ""
        if msg["role"] == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
            
    if state.get("eval_feedback"):
        print(f"-> Re-planning triggered. Feedback: {state['eval_feedback']}")
        replan_prompt = (
            f"Your previous plan failed to answer the user query correctly.\n"
            f"Evaluation Feedback: {state['eval_feedback']}\n\n"
            f"Original Query: {state['query']}\n"
            "Please refine the plan to gather the correct tables/metrics."
        )
        messages.append(HumanMessage(content=replan_prompt))
    else:
        messages.append(HumanMessage(content=state["query"]))
        
    response = invoke_llm_with_retry(get_large_llm, messages, is_large=True)
    plan_data = parse_plan_json(response.content.strip())
    print(f"  Generated plan: {plan_data}")
    
    return {
        "plan": plan_data,
        "current_task_idx": 0,
        "task_results": {},
        "eval_feedback": "" # Clear feedback once replanned
    }

def task_executor_node(state: AgentState) -> Dict[str, Any]:
    """
    Task Executor (Small Model): Generates a single SQL query for the active sub-task.
    We do NOT run the query here. We just write the SQL.
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    active_task = plan[idx]
    
    print(f"[Node: task_executor] Writing SQL query for Task {active_task['task_id']}: '{active_task['description']}'")
    
    system_prompt = (
        "You are an expert PostgreSQL query generator. Your task is to write a single "
        "read-only SELECT statement to complete the current sub-task.\n\n"
        "Here is the database schema:\n"
        f"{state['schema']}\n\n"
        "Here are the results of previously executed tasks (if any) to help you:\n"
        f"{state['task_results']}\n\n"
        "Guidelines:\n"
        "1. Write ONLY the raw SQL code. No markdown wraps (e.g. ```sql ... ```), no explanations.\n"
        "2. Do not write calculations in Python; calculate aggregates (SUM, AVG, COUNT, divisions) directly in the SQL statement.\n"
        "3. Use case-sensitive lowercase table and column names matching the schema.\n"
        "4. PREVENT SUM DUPLICATION (FAN EFFECT): When calculating the SUM of values from order_payments (like payment_value), do NOT directly JOIN with order_items or products. Because one order can contain multiple items, a direct join multiplies the payment rows, making SUM(payment_value) artificially high and incorrect. Instead, use a subquery with IN or EXISTS to filter orders, e.g.:\n"
        "   SELECT SUM(payment_value) FROM order_payments WHERE order_id IN (SELECT DISTINCT oi.order_id FROM order_items oi JOIN products p ON oi.product_id = p.product_id WHERE p.product_category_name = '...');\n"
        "5. DATE AND TIME COMPARISONS: When comparing dates in columns that contain full timestamps (like order_approved_at and order_purchase_timestamp) to check if they occur on the same day, do NOT use direct equality (=) on the raw timestamp columns. Instead, cast them to DATE using DATE(column) to compare only the calendar date portions, e.g. DATE(order_approved_at) = DATE(order_purchase_timestamp).\n"
        "6. SEMANTIC VIEWS: For maximum query simplicity and logic safety, you should prioritize querying the analytical views v_order_items_detailed, v_order_payments_detailed, and v_order_reviews_detailed when analyzing orders, items, categories, payments, or reviews, rather than doing complex manual joins on orders, order_items, customers, and order_payments.\n"
        "7. COMPARING TOP/BEST ITEMS (CROSS JOIN): When asked to retrieve and compare two distinct top items (e.g. the single best-selling product and the single highest-rated product), do NOT use an INNER JOIN on product_id. Since they are likely different physical products, an INNER JOIN on product_id will return 0 rows. Instead, use a CROSS JOIN to combine the single-row subqueries, or combine them using UNION ALL. E.g.:\n"
        "   WITH top_sold AS (SELECT product_id, COUNT(*) as units FROM v_order_items_detailed GROUP BY product_id ORDER BY units DESC LIMIT 1),\n"
        "   top_rated AS (SELECT product_id, AVG(review_score) as rating FROM v_order_reviews_detailed JOIN v_order_items_detailed ON ... GROUP BY product_id ORDER BY rating DESC LIMIT 1)\n"
        "   SELECT * FROM top_sold CROSS JOIN top_rated;\n\n"
        "8. RANKING QUERIES — MANDATORY PATTERN: For any question asking for best/worst/top/bottom ranking of a product, category, or seller by review score, revenue, or volume:\n"
        "   - Use AVG() for scores/ratings, SUM() for revenue/price, COUNT() for order volume.\n"
        "   - Always add HAVING COUNT(*) >= 5 to exclude items with too few data points.\n"
        "   - Always add ORDER BY <metric> ASC (for worst) or DESC (for best).\n"
        "   - Always add LIMIT 10.\n"
        "   - NEVER use MIN() or MAX() as the ranking aggregate — these return one-off outliers.\n\n"
        "Here are examples of how to correctly query the whitelisted Views and tables in the Olist database:\n\n"
        "Example 1: Products with the WORST average review score\n"
        "SELECT oi.product_id, oi.product_category_name_english,\n"
        "       ROUND(AVG(r.review_score)::numeric, 2) AS avg_review_score,\n"
        "       COUNT(r.review_id) AS total_reviews\n"
        "FROM v_order_items_detailed oi\n"
        "JOIN v_order_reviews_detailed r ON oi.order_id = r.order_id\n"
        "GROUP BY oi.product_id, oi.product_category_name_english\n"
        "HAVING COUNT(r.review_id) >= 5\n"
        "ORDER BY avg_review_score ASC\n"
        "LIMIT 10;\n\n"
        "Example 2: Product CATEGORIES with the BEST average review score\n"
        "SELECT oi.product_category_name_english,\n"
        "       ROUND(AVG(r.review_score)::numeric, 2) AS avg_review_score,\n"
        "       COUNT(r.review_id) AS total_reviews\n"
        "FROM v_order_reviews_detailed r\n"
        "JOIN v_order_items_detailed oi ON r.order_id = oi.order_id\n"
        "GROUP BY oi.product_category_name_english\n"
        "HAVING COUNT(r.review_id) >= 10\n"
        "ORDER BY avg_review_score DESC\n"
        "LIMIT 10;\n\n"
        "Example 3: Find total sales revenue per seller in a specific state\n"
        "Correct Path: Query v_order_items_detailed directly (contains price and seller_state)\n"
        "SELECT seller_id, SUM(price) as revenue\n"
        "FROM v_order_items_detailed\n"
        "WHERE seller_state = 'SP'\n"
        "GROUP BY seller_id\n"
        "ORDER BY revenue DESC\n"
        "LIMIT 10;\n\n"
        "Example 4: Find the number of unique customers who placed more than one order (repeat buyers)\n"
        "Correct Path: Query v_order_payments_detailed directly using customer_unique_id\n"
        "SELECT COUNT(*)\n"
        "FROM (\n"
        "    SELECT customer_unique_id\n"
        "    FROM v_order_payments_detailed\n"
        "    GROUP BY customer_unique_id\n"
        "    HAVING COUNT(DISTINCT order_id) > 1\n"
        ") as repeat_customers;\n"
    )
    
    messages = [SystemMessage(content=system_prompt)]
    
    # If task criticism indicates a database error occurred
    if active_task.get("error_message"):
        print(f"  -> Task Self-Healing: Error: {active_task['error_message']}")
        retry_prompt = (
            f"Your previous query was:\n{active_task['sql_query']}\n\n"
            f"It failed with this error:\n{active_task['error_message']}\n\n"
            "Please fix the syntax, joins, or column names and generate a corrected SQL query."
        )
        messages.append(HumanMessage(content=retry_prompt))
    else:
        messages.append(HumanMessage(content=f"Sub-task to complete: {active_task['description']}"))
        
    response = invoke_llm_with_retry(get_large_llm, messages, is_large=True)
    sql_text = response.content.strip()
    
    # Strip markdown wrappers if any
    sql_text = re.sub(r"^```sql\s*", "", sql_text, flags=re.IGNORECASE)
    sql_text = re.sub(r"^```\s*", "", sql_text)
    sql_text = re.sub(r"```$", "", sql_text).strip()
    
    print(f"  Generated SQL (pending confirmation): {sql_text}")
    
    # Safety fallback: If small model output is completely blank, generate a basic functional fallback query
    if not sql_text:
        print("  -> [Warning] Generated SQL is blank. Constructing basic fallback query.")
        if "rating" in active_task["description"].lower() or "score" in active_task["description"].lower() or "ulasan" in active_task["description"].lower():
            sql_text = "SELECT AVG(review_score) FROM v_order_reviews_detailed LIMIT 5;"
        elif "payment" in active_task["description"].lower() or "uang" in active_task["description"].lower() or "penjualan" in active_task["description"].lower():
            sql_text = "SELECT SUM(price) FROM v_order_items_detailed LIMIT 5;"
        else:
            sql_text = "SELECT COUNT(*) FROM customers LIMIT 5;"
    
    updated_plan = list(plan)
    updated_plan[idx] = {
        **active_task,
        "sql_query": sql_text,
        "status": "pending"
    }
        
    return {"plan": updated_plan}

def query_runner_node(state: AgentState) -> Dict[str, Any]:
    """
    Query Runner (Human-in-the-loop Node): Executes the approved SQL query on AWS RDS.
    This node runs AFTER human confirmation has resumed the graph execution.
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    active_task = plan[idx]
    # Bypass execution if task was already marked failed due to user cancellation
    if active_task.get("status") == "failed" and "dibatalkan" in active_task.get("error_message", ""):
        print("[Node: query_runner] SQL execution was canceled by user. Skipping query run.")
        return {"plan": plan}
        
    sql_text = active_task["sql_query"]
    
    print(f"[Node: query_runner] Executing confirmed SQL query on RDS: {sql_text}")
    
    updated_plan = list(plan)
    try:
        results = execute_query(sql_text)
        print(f"  Query succeeded. Returned {len(results)} rows.")
        updated_plan[idx] = {
            **active_task,
            "sql_result": results,
            "error_message": "",
            "status": "completed"
        }
    except Exception as e:
        error_msg = str(e)
        print(f"  Query execution failed: {error_msg}")
        updated_plan[idx] = {
            **active_task,
            "error_message": error_msg,
            "retry_count": active_task.get("retry_count", 0) + 1,
            "status": "failed"
        }
        
    return {"plan": updated_plan}

def auto_query_runner_node(state: AgentState) -> Dict[str, Any]:
    """
    Auto Query Runner (Automatic Execution Node): Executes the SQL query directly on AWS RDS.
    This node runs automatically without waiting for human confirmation.
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    active_task = plan[idx]
    
    sql_text = active_task["sql_query"]
    
    print(f"[Node: auto_query_runner] Executing SQL query automatically on RDS: {sql_text}")
    
    updated_plan = list(plan)
    try:
        results = execute_query(sql_text)
        print(f"  Query succeeded. Returned {len(results)} rows.")
        updated_plan[idx] = {
            **active_task,
            "sql_result": results,
            "error_message": "",
            "status": "completed"
        }
    except Exception as e:
        error_msg = str(e)
        print(f"  Query execution failed: {error_msg}")
        updated_plan[idx] = {
            **active_task,
            "error_message": error_msg,
            "retry_count": active_task.get("retry_count", 0) + 1,
            "status": "failed"
        }
        
    return {"plan": updated_plan}

def task_critic_node(state: AgentState) -> Dict[str, Any]:
    """
    Task Critic (Local Python check & AI Empty Result validation): Examines sub-task execution.
    If it fails, keeps index same to retry. If succeeds, saves result and advances index.
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    active_task = plan[idx]
    
    print(f"[Node: task_critic] Inspecting outcome of Task {active_task['task_id']}")
    
    updated_plan = list(plan)
    
    # 1. If query execution returned success but returned 0 rows, check if it's a join/logic error
    if active_task["status"] == "completed" and active_task["sql_result"] == []:
        print("  -> Query returned 0 rows. Invoking AI Auditor to verify if empty result is expected or a logic error...")
        system_prompt = (
            "You are a Senior SQL Auditor. A PostgreSQL query was executed on the database and returned 0 rows (empty result).\n"
            "You must determine if this empty result is EXPECTED (e.g., the data naturally does not exist in the database based on the query filters) "
            "or if it is a LOGICAL ERROR (e.g., the query joined tables on the wrong columns, causing a mismatch like orders.order_id = products.product_id).\n\n"
            "Respond ONLY with one of the following words:\n"
            "- EXPECTED (if 0 rows is the correct logical outcome of the query)\n"
            "- FAILED (if 0 rows is due to a bad join, wrong column comparison, or syntax logic error)"
        )
        human_content = (
            f"User Question: {state['query']}\n"
            f"Current Sub-task: {active_task['description']}\n"
            f"Generated SQL: {active_task['sql_query']}\n"
            "Result: 0 rows returned."
        )
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_content)
        ]
        
        try:
            # We call large model to verify empty result
            response = invoke_llm_with_retry(get_large_llm, messages, is_large=True)
            decision = response.content.strip().upper()
            print(f"  -> AI Auditor Decision: {decision}")
            if "FAILED" in decision:
                # Mark as failed to trigger self-healing
                active_task["status"] = "failed"
                active_task["error_message"] = (
                    "Query returned 0 rows. The SQL Auditor detected a logical join/comparison error. "
                    "Please verify your JOIN paths. Do NOT join orders.order_id to products.product_id directly; "
                    "you must join through order_items (orders -> order_items -> products)."
                )
                active_task["retry_count"] = active_task.get("retry_count", 0) + 1
                updated_plan[idx] = active_task
        except Exception as audit_err:
            print(f"  -> Error invoking SQL Auditor: {audit_err}. Proceeding with completed status.")
            
    if active_task["status"] == "failed" and "dibatalkan" in active_task.get("error_message", ""):
        print("  -> Result: CANCELED BY USER. Aborting remaining tasks.")
        return {
            "plan": updated_plan,
            "current_task_idx": len(plan),
            "final_answer": "Eksekusi SQL dibatalkan oleh pengguna."
        }

    if active_task["status"] == "failed" and active_task["retry_count"] < 3:
        print(f"  -> Result: FAILED. Retrying task (Attempt {active_task['retry_count'] + 1})")
        return {"plan": updated_plan}
    else:
        if active_task["status"] == "failed":
            print("  -> Result: FAILED (Max retries reached). Moving forward with error.")
        else:
            print("  -> Result: SUCCESS. Saving results and advancing plan.")
            
        # Save results in combined task_results dict
        results_key = f"task_{active_task['task_id']}_result"
        updated_results = dict(state.get("task_results", {}))
        updated_results[results_key] = active_task["sql_result"]
        
        return {
            "plan": updated_plan,
            "task_results": updated_results,
            "current_task_idx": idx + 1
        }

def global_synthesizer_node(state: AgentState) -> Dict[str, Any]:
    """
    Global Synthesizer (Large Model): Combines user query, the plan steps, and database results.
    Directly bypasses to clarification message if status is clarification_needed.
    """
    print("[Node: global_synthesizer] Synthesizing final answer...")
    
    plan = state.get("plan", [])
    if any(
        task.get("status") == "failed" and ("dibatalkan" in task.get("error_message", "") or "canceled" in task.get("error_message", "").lower())
        for task in plan
    ):
        print("  -> SQL execution was canceled by user. Returning cancellation message.")
        return {"final_answer": "SQL execution was canceled by the user."}

    if plan and plan[0]["status"] == "clarification_needed":
        # Prioritize description since we now explicitly store the clarifying question text there
        clarifying_question = plan[0].get("description") or plan[0].get("sql_result") or "Sorry, could you please clarify your question?"
        print("  -> Clarification question returned.")
        return {"final_answer": clarifying_question}
        
    task_results = dict(state.get("task_results", {}))
    # If task_results is empty but tasks are completed, copy results from plan (e.g. general dataset overview)
    if not task_results and plan:
        for task in plan:
            if task["status"] == "completed" and task["sql_result"]:
                task_results[f"task_{task['task_id']}_result"] = task["sql_result"]
        
    system_prompt = (
        "You are an expert e-commerce business analyst. Your task is to write a detailed, "
        "polishes, and professional response to the user's question using the results of "
        "the database execution tasks.\n\n"
        "Guidelines:\n"
        "1. Do not invent any data. Only report numbers returned from the database queries.\n"
        "2. Format currencies, dates, and percentages nicely.\n"
        "3. Keep the tone helpful, professional, and clear."
    )
    
    human_content = (
        f"User Question: {state['query']}\n\n"
        f"Plan Steps Executed:\n{json.dumps(state['plan'], indent=2)}\n\n"
        f"Database Results Gathered:\n{json.dumps(task_results, indent=2)}\n"
    )
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content)
    ]
    response = invoke_llm_with_retry(get_large_llm, messages, is_large=True)
    
    return {"final_answer": response.content.strip()}

def global_critic_node(state: AgentState) -> Dict[str, Any]:
    """
    Global Critic (Large Model): Checks if the final answer actually answers the user's intent.
    If flawed, triggers re-planning. Auto-approves clarification.
    """
    plan = state.get("plan", [])
    if any(
        task.get("status") == "failed" and "dibatalkan" in task.get("error_message", "")
        for task in plan
    ):
        print("  -> Global Critic: Auto-approving user-canceled execution.")
        return {"eval_feedback": ""}

    if plan and plan[0]["status"] == "clarification_needed":
        return {"eval_feedback": ""} # Auto-approve clarifying queries
        
    # Auto-approve if all tasks were completed directly by the planner without SQL (e.g. general dataset overview)
    if plan and all(t["status"] == "completed" and not t["sql_query"] for t in plan):
        print("  -> Global Critic: Auto-approving non-SQL direct planner answers.")
        return {"eval_feedback": ""}
        
    print("[Node: global_critic] Evaluating final answer quality...")
    
    system_prompt = (
        "You are a rigorous Quality Assurance Auditor. Your job is to verify if the "
        "final answer generated by the agent matches the user's original question "
        "intent and is supported by the gathered database results.\n\n"
        "If the answer is correct, complete, and aligns with the query, respond ONLY with "
        "the word: APPROVED\n"
        "If the answer is incorrect, incomplete, or relies on a wrong plan, write a short, "
        "specific explanation of what needs to be fixed. Do not write APPROVED."
    )
    
    history_context = ""
    for msg in state.get("chat_history", []):
        role = "User" if msg["role"] == "user" else "Assistant"
        history_context += f"{role}: {msg['content']}\n"
        
    human_content = (
        f"Conversation History:\n{history_context}\n"
        f"Current User Query: {state['query']}\n\n"
        f"Final Answer: {state['final_answer']}\n\n"
        f"Database Results: {json.dumps(state['task_results'], indent=2)}\n"
    )
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content)
    ]
    response = invoke_llm_with_retry(get_large_llm, messages, is_large=True)
    
    feedback = response.content.strip()
    print(f"  -> Audit Feedback: {feedback}")
    
    if feedback.upper() == "APPROVED":
        return {"eval_feedback": ""}
    else:
        return {
            "eval_feedback": feedback,
            "eval_retries": state.get("eval_retries", 0) + 1
        }

# ----------------- ROUTING LOGIC -----------------

def intent_router(state: AgentState):
    """
    Routes messages before any schema inspection. Only concrete analytical requests
    are allowed to enter the SQL planning path.
    """
    intent = state.get("intent", "analytical_query")
    if intent == "analytical_query":
        print("Routing: Analytical query. Proceeding to schema inspection.")
        return "schema_inspector"

    print(f"Routing: Non-SQL intent ({intent}). Proceeding to final response.")
    return "global_synthesizer"

def task_router(state: AgentState):
    """
    Determines whether there are more tasks in the plan to execute.
    Bypasses tasks immediately if clarification is requested or if all tasks are completed.
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    
    if plan and plan[0]["status"] == "clarification_needed":
        print("Routing: Clarification needed. Skipping SQL execution.")
        return "global_synthesizer"
        
    # If all tasks are already completed by the planner directly (e.g. out-of-scope or general dataset overview)
    if plan and all(t["status"] == "completed" for t in plan):
        print("Routing: All tasks already completed by planner. Skipping SQL execution.")
        return "global_synthesizer"
        
    if idx < len(plan):
        print(f"Routing: Proceeding to task {idx + 1} of {len(plan)}")
        return "task_executor"
    else:
        print("Routing: All tasks executed. Proceeding to global synthesizer.")
        return "global_synthesizer"

def local_critic_router(state: AgentState):
    """
    Determines if the active task needs a retry or proceeds.
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    active_task = plan[idx]
    
    if active_task["status"] == "failed":
        if "dibatalkan" in active_task.get("error_message", ""):
            return "task_critic"
        if active_task.get("retry_count", 0) < 3:
            return "task_executor"
    return "task_critic"

def global_critic_router(state: AgentState):
    """
    Checks if the auditor approved the answer or requires re-planning.
    """
    feedback = state.get("eval_feedback", "")
    retries = state.get("eval_retries", 0)
    
    if feedback and retries < 2:
        print(f"Routing: Audit failed. Re-planning loop triggered (Attempt {retries} of 2)")
        return "planner"
    else:
        if feedback:
            print("Routing: Audit failed, but maximum re-planning limits reached. Ending workflow.")
        else:
            print("Routing: Audit approved! Ending workflow.")
        return END

def execution_router(state: AgentState):
    """
    Routes the execution flow:
    - If the active task requires_approval is True, routes to query_runner (which is interrupted).
    - If requires_approval is False, routes to auto_query_runner (which executes automatically).
    """
    idx = state["current_task_idx"]
    plan = state["plan"]
    if idx < len(plan):
        active_task = plan[idx]
        if active_task.get("requires_approval", True):
            print("Routing: Task requires approval. Halting before query_runner.")
            return "query_runner"
        else:
            print("Routing: Task is clear. Running SQL query automatically.")
            return "auto_query_runner"
    return "query_runner"

# ----------------- GRAPH COMPILATION -----------------

def create_agent_graph(interrupt_before_query: bool = True):
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("intent_classifier", intent_classifier_node)
    workflow.add_node("schema_inspector", schema_inspector_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("task_executor", task_executor_node)
    workflow.add_node("query_runner", query_runner_node)
    workflow.add_node("auto_query_runner", auto_query_runner_node)
    workflow.add_node("task_critic", task_critic_node)
    workflow.add_node("global_synthesizer", global_synthesizer_node)
    workflow.add_node("global_critic", global_critic_node)
    
    # Define connection edges
    workflow.add_edge(START, "intent_classifier")
    workflow.add_conditional_edges(
        "intent_classifier",
        intent_router,
        {
            "schema_inspector": "schema_inspector",
            "global_synthesizer": "global_synthesizer"
        }
    )
    workflow.add_edge("schema_inspector", "planner")
    
    # Router conditional edge from planner
    workflow.add_conditional_edges(
        "planner",
        task_router,
        {
            "task_executor": "task_executor",
            "global_synthesizer": "global_synthesizer"
        }
    )
    
    # Executor generates SQL and proceeds to either query_runner (interrupted) or auto_query_runner (non-interrupted)
    workflow.add_conditional_edges(
        "task_executor",
        execution_router,
        {
            "query_runner": "query_runner",
            "auto_query_runner": "auto_query_runner"
        }
    )
    
    # Query Runner executes SQL, then goes to Local Critic to check success
    workflow.add_conditional_edges(
        "query_runner",
        local_critic_router,
        {
            "task_executor": "task_executor",
            "task_critic": "task_critic"
        }
    )
    
    # Auto Query Runner executes SQL automatically, then goes to Local Critic
    workflow.add_conditional_edges(
        "auto_query_runner",
        local_critic_router,
        {
            "task_executor": "task_executor",
            "task_critic": "task_critic"
        }
    )
    
    # Router conditional edge from task critic back to next task check
    workflow.add_conditional_edges(
        "task_critic",
        task_router,
        {
            "task_executor": "task_executor",
            "global_synthesizer": "global_synthesizer"
        }
    )
    
    workflow.add_edge("global_synthesizer", "global_critic")
    
    # Global audit replanning router
    workflow.add_conditional_edges(
        "global_critic",
        global_critic_router,
        {
            "planner": "planner",
            END: END
        }
    )
    
    # Compile with persistence memory checkpointer
    memory = MemorySaver()
    if interrupt_before_query:
        return workflow.compile(
            checkpointer=memory,
            interrupt_before=["query_runner"]
        )
    else:
        return workflow.compile(checkpointer=memory)

if __name__ == "__main__":
    print("Compiling Planner-Executor-Critic Agent with memory & human-in-the-loop interrupts...")
    graph = create_agent_graph()
    print("Graph compiled successfully!")
