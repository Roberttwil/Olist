import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import inspect, text
from database import get_engine, execute_query
from agent import create_agent_graph

app = FastAPI(title="Olist SQL Agent Web Service")

from fastapi import Request
from fastapi.responses import JSONResponse

@app.middleware("http")
async def check_demo_passcode_middleware(request: Request, call_next):
    demo_passcode = os.getenv("DEMO_PASSCODE")
    if demo_passcode and request.url.path.startswith("/api"):
        if request.method == "OPTIONS":
            return await call_next(request)
        passcode = request.headers.get("X-Demo-Passcode")
        if passcode != demo_passcode:
            return JSONResponse(
                status_code=401,
                content={"detail": "Akses Ditolak: Passcode demo salah atau tidak disertakan."}
            )
    return await call_next(request)

# Mount static files folder (will contain HTML, CSS, JS)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

# Initialize compiled LangGraph SQL Agent
try:
    agent_graph = create_agent_graph()
except Exception as e:
    print(f"Error compiling LangGraph in Web App: {e}")
    agent_graph = None

from typing import Optional

class ChatRequest(BaseModel):
    question: str
    thread_id: Optional[str] = None

class ConfirmRequest(BaseModel):
    thread_id: str
    confirm: bool

def get_active_task(values: dict):
    plan = values.get("plan", [])
    idx = values.get("current_task_idx", 0)
    return plan[idx] if idx < len(plan) else (plan[-1] if plan else {})

def is_query_explanation_request(question: str) -> bool:
    normalized = " ".join(question.lower().replace("?", " ").replace("!", " ").split())
    explanation_terms = [
        "buat apa",
        "maksud",
        "jelas",
        "arti",
        "kenapa",
        "sql apa",
        "query apa",
        "why",
        "what",
        "explain",
    ]
    return any(term in normalized for term in explanation_terms)

def classify_user_response_llm(question: str) -> str:
    """
    Classifies the user's response to a pending SQL query into one of:
    - CONFIRM: User wants to execute/run the SQL query.
    - CANCEL: User wants to cancel/abort the SQL query.
    - DISCUSSION: User is asking questions or verifying the query.
    - NEW_QUERY: User is shifting topic/asking a new database query.
    """
    from langchain_core.messages import SystemMessage, HumanMessage
    from agent import get_small_llm, invoke_llm_with_retry
    
    system_prompt = (
        "You are an AI conversational classifier. The user has a pending SQL query displayed on their screen "
        "and is deciding whether to execute or cancel it.\n"
        "Classify their new message into exactly one of these classes:\n"
        "- CONFIRM: If they are agreeing, confirming, or telling you to run/execute the query (e.g. 'ya', 'yes', 'run', 'jalankan', 'ok', 'boleh', 'lanjut', 'exec').\n"
        "- CANCEL: If they are rejecting, canceling, or telling you NOT to run the query (e.g. 'tidak', 'jangan', 'no', 'cancel', 'batalkan').\n"
        "- DISCUSSION: If they are asking questions, explaining, verifying, or commenting about the query (e.g. 'is this correct?', 'what does it do?', 'udh bener?', 'buat apa?', 'kenapa pakai join?').\n"
        "- NEW_QUERY: If they are ignoring the prompt and asking a completely new database analysis question or starting a new topic (e.g. 'berapa pembeli di bekasi?', 'siapa top seller?').\n\n"
        "Respond ONLY with the class name (CONFIRM, CANCEL, DISCUSSION, or NEW_QUERY)."
    )
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"User Message: '{question}'")
    ]
    
    try:
        response = invoke_llm_with_retry(get_small_llm, messages, is_large=False)
        decision = response.content.strip().upper()
        print(f"  -> User Response Classifier Decision: {decision}")
        if decision in ["CONFIRM", "CANCEL", "DISCUSSION", "NEW_QUERY"]:
            return decision
        return "NEW_QUERY" # Default fallback
    except Exception as e:
        print(f"  -> Error invoking response classifier: {e}")
        # Simple fallback based on string search
        q_lower = question.lower().strip()
        if q_lower in ["ya", "yes", "ok", "jalankan", "run", "lanjut", "boleh", "confirm"]:
            return "CONFIRM"
        if q_lower in ["tidak", "no", "jangan", "batalkan", "cancel"]:
            return "CANCEL"
        if any(term in q_lower for term in ["maksud", "buat apa", "jelas", "arti", "kenapa"]):
            return "DISCUSSION"
        return "NEW_QUERY"

def explain_pending_query_llm(question: str, active_task: dict) -> str:
    """
    Uses the Large Model to explain the pending SQL query to the user, 
    answering their specific question or concern.
    """
    from langchain_core.messages import SystemMessage, HumanMessage
    from agent import get_large_llm, invoke_llm_with_retry
    
    system_prompt = (
        "You are a friendly SQL Database Analyst. There is a pending SQL query awaiting execution.\n"
        "Explain the query to the user in Indonesian, addressing their specific question/concern.\n"
        "Keep your explanation clear, concise, and focused on helping them decide whether to run it.\n\n"
        f"Pending SQL Query:\n{active_task.get('sql_query', '')}\n"
        f"Task Description:\n{active_task.get('description', '')}"
    )
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"User Question: '{question}'")
    ]
    
    try:
        response = invoke_llm_with_retry(get_large_llm, messages, is_large=True)
        explanation = response.content.strip()
        # Append interactive helper guidance at the end
        return (
            f"{explanation}\n\n"
            "Belum saya eksekusi. Kalau Anda klik Jalankan Query, query tersebut akan dijalankan ke database AWS RDS "
            "untuk mengambil hasilnya. Kalau query-nya belum sesuai, klik Batalkan lalu kirim pertanyaan yang lebih spesifik."
        )
    except Exception as e:
        print(f"  -> Error generating query explanation: {e}")
        # Fallback description
        desc = active_task.get("description") or "menjawab pertanyaan analitik sebelumnya"
        return (
            f"Query itu dibuat untuk: {desc}\n\n"
            "Belum saya eksekusi. Kalau Anda klik Jalankan Query, query tersebut akan dijalankan ke database AWS RDS "
            "untuk mengambil hasilnya. Kalau query-nya belum sesuai, klik Batalkan lalu kirim pertanyaan yang lebih spesifik."
        )

@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest):
    if not agent_graph:
        raise HTTPException(
            status_code=500, 
            detail="LangGraph SQL Agent failed to initialize. Check your API keys in .env"
        )
        
    # Default thread_id if none provided
    thread_id = payload.thread_id or "default_thread_session"
    config = {"configurable": {"thread_id": thread_id}}
    
    try:
        # Retrieve previous state to build chat history context
        state_info = agent_graph.get_state(config)
        chat_history = []

        has_pending_query = bool(state_info.next and "query_runner" in state_info.next)
        if has_pending_query:
            active_task = get_active_task(state_info.values)
            agent_sql = active_task.get("sql_query", "")
            
            # Classify user response
            classification = classify_user_response_llm(payload.question)
            
            if classification == "CONFIRM":
                print(f"User confirmed execution via chat: '{payload.question}' on thread {thread_id}")
                # Resume graph execution (will execute query_runner)
                result = agent_graph.invoke(None, config)
                
                # Check if there's another sub-task in the plan awaiting approval
                new_state_info = agent_graph.get_state(config)
                is_awaiting_approval = len(new_state_info.next) > 0 and "query_runner" in new_state_info.next
                
                plan = result.get("plan", [])
                active_task = get_active_task(result)
                agent_sql = active_task.get("sql_query", "")
                agent_db_res = active_task.get("sql_result", "")
                agent_err = active_task.get("error_message", "")
                agent_retries = sum(t.get("retry_count", 0) for t in plan) + result.get("eval_retries", 0)
                
                if is_awaiting_approval:
                    return {
                        "status": "awaiting_approval",
                        "thread_id": thread_id,
                        "query": result["query"],
                        "generated_sql": agent_sql,
                        "final_answer": "Query sebelumnya berhasil. Saya telah merancang query SQL baru berikut. Apakah Anda ingin mengeksekusinya?"
                    }
                    
                return {
                    "status": "completed",
                    "thread_id": thread_id,
                    "query": result["query"],
                    "generated_sql": agent_sql,
                    "sql_result": agent_db_res,
                    "error_message": agent_err,
                    "retry_count": agent_retries,
                    "final_answer": result["final_answer"]
                }
                
            elif classification == "CANCEL":
                print(f"User canceled execution via chat: '{payload.question}' on thread {thread_id}")
                current_plan = state_info.values.get("plan", [])
                idx = state_info.values.get("current_task_idx", 0)
                if idx < len(current_plan):
                    current_plan[idx]["status"] = "failed"
                    current_plan[idx]["error_message"] = "Eksekusi SQL dibatalkan oleh pengguna."
                    
                agent_graph.update_state(config, {
                    "plan": current_plan,
                    "final_answer": "Eksekusi SQL dibatalkan oleh pengguna."
                })
                # Resume to clear the interrupt from checkpointer memory
                agent_graph.invoke(None, config)
                
                return {
                    "status": "canceled",
                    "thread_id": thread_id,
                    "final_answer": "Eksekusi SQL dibatalkan. Silakan kirim pertanyaan baru."
                }
                
            elif classification == "DISCUSSION":
                explanation_answer = explain_pending_query_llm(payload.question, active_task)
                return {
                    "status": "awaiting_approval",
                    "thread_id": thread_id,
                    "query": state_info.values.get("query", payload.question),
                    "generated_sql": agent_sql,
                    "final_answer": explanation_answer
                }
                
            else: # NEW_QUERY
                # User sent a new question instead of confirming, canceling, or discussing.
                # Automatically cancel the pending query, clear the interrupt, and proceed to the new question!
                print(f"Auto-canceling pending query on thread {thread_id} due to new question: '{payload.question}'")
                current_plan = state_info.values.get("plan", [])
                idx = state_info.values.get("current_task_idx", 0)
                if idx < len(current_plan):
                    current_plan[idx]["status"] = "failed"
                    current_plan[idx]["error_message"] = "Eksekusi SQL dibatalkan karena pengguna mengirimkan pertanyaan baru."
                
                agent_graph.update_state(config, {
                    "plan": current_plan,
                    "final_answer": "Eksekusi SQL dibatalkan."
                })
                # Resume to clear the interrupt from checkpointer memory
                agent_graph.invoke(None, config)
                
                # Fetch state again to build chat history correctly
                state_info = agent_graph.get_state(config)
        
        if state_info.values:
            chat_history = state_info.values.get("chat_history", [])
            last_query = state_info.values.get("query")
            last_answer = state_info.values.get("final_answer")
            if last_query and last_answer:
                # Prevent duplicate entries in chat history
                if not chat_history or chat_history[-1]["content"] != last_query:
                    chat_history.append({"role": "user", "content": last_query})
                    chat_history.append({"role": "assistant", "content": last_answer})
        
        initial_state = {
            "query": payload.question,
            "chat_history": chat_history,
            "schema": "",
            "plan": [],
            "current_task_idx": 0,
            "task_results": {},
            "final_answer": "",
            "eval_feedback": "",
            "eval_retries": 0
        }
        
        # Execute workflow (will interrupt before query_runner if SQL task is generated)
        result = agent_graph.invoke(initial_state, config)
        
        # Check if the graph is currently paused at the query_runner node
        new_state_info = agent_graph.get_state(config)
        is_awaiting_approval = len(new_state_info.next) > 0 and "query_runner" in new_state_info.next
        
        # Extract SQL and task information
        plan = result.get("plan", [])
        active_task = get_active_task(result)
        
        agent_sql = active_task.get("sql_query", "")
        agent_db_res = active_task.get("sql_result", "")
        agent_err = active_task.get("error_message", "")
        agent_retries = sum(t.get("retry_count", 0) for t in plan) + result.get("eval_retries", 0)
        
        if is_awaiting_approval:
            return {
                "status": "awaiting_approval",
                "thread_id": thread_id,
                "query": result["query"],
                "generated_sql": agent_sql,
                "final_answer": "Saya telah merancang query SQL berikut. Apakah Anda ingin mengeksekusinya di database AWS RDS?"
            }
            
        # Normal completion (e.g. clarification needed, or direct answers that didn't hit SQL runner)
        return {
            "status": "completed",
            "thread_id": thread_id,
            "query": result["query"],
            "generated_sql": agent_sql,
            "sql_result": agent_db_res,
            "error_message": agent_err,
            "retry_count": agent_retries,
            "final_answer": result["final_answer"]
        }
    except Exception as e:
        error_msg = str(e)
        print(f"ERROR: Exception in chat endpoint: {error_msg}")
        
        if "rate_limit" in error_msg.lower() or "429" in error_msg:
            sanitized_detail = (
                "Batas kuota API (Rate Limit) dari penyedia layanan AI terlampaui. "
                "Silakan tunggu beberapa menit sebelum mencoba lagi."
            )
        else:
            sanitized_detail = "Terjadi kesalahan internal pada sistem saat memproses data Anda. Silakan coba beberapa saat lagi."
            
        raise HTTPException(status_code=500, detail=sanitized_detail)

@app.post("/api/chat/confirm")
async def confirm_endpoint(payload: ConfirmRequest):
    if not agent_graph:
        raise HTTPException(status_code=500, detail="SQL Agent not initialized.")
        
    config = {"configurable": {"thread_id": payload.thread_id}}
    state_info = agent_graph.get_state(config)
    
    if not state_info.next or "query_runner" not in state_info.next:
        return {
            "status": "completed",
            "final_answer": "Tidak ada eksekusi SQL yang tertunda untuk sesi ini."
        }
        
    if not payload.confirm:
        # Cancel the task
        current_plan = state_info.values.get("plan", [])
        idx = state_info.values.get("current_task_idx", 0)
        if idx < len(current_plan):
            current_plan[idx]["status"] = "failed"
            current_plan[idx]["error_message"] = "Eksekusi SQL dibatalkan oleh pengguna."
            
        agent_graph.update_state(config, {
            "plan": current_plan,
            "final_answer": "Eksekusi SQL dibatalkan oleh pengguna."
        })
        
        # Resume graph execution to process the cancellation path and clear the interrupt
        agent_graph.invoke(None, config)
        
        return {
            "status": "canceled",
            "thread_id": payload.thread_id,
            "final_answer": "Eksekusi SQL dibatalkan. Silakan kirim pertanyaan baru."
        }
        
    try:
        # Resume graph execution (will execute query_runner)
        result = agent_graph.invoke(None, config)
        
        # Check if there's another sub-task in the plan awaiting approval
        new_state_info = agent_graph.get_state(config)
        is_awaiting_approval = len(new_state_info.next) > 0 and "query_runner" in new_state_info.next
        
        plan = result.get("plan", [])
        active_task = get_active_task(result)
        agent_sql = active_task.get("sql_query", "")
        agent_db_res = active_task.get("sql_result", "")
        agent_err = active_task.get("error_message", "")
        agent_retries = sum(t.get("retry_count", 0) for t in plan) + result.get("eval_retries", 0)
        
        if is_awaiting_approval:
            return {
                "status": "awaiting_approval",
                "thread_id": payload.thread_id,
                "query": result["query"],
                "generated_sql": agent_sql,
                "final_answer": "Query sebelumnya berhasil. Saya telah merancang query SQL baru berikut. Apakah Anda ingin mengeksekusinya?"
            }
            
        return {
            "status": "completed",
            "thread_id": payload.thread_id,
            "query": result["query"],
            "generated_sql": agent_sql,
            "sql_result": agent_db_res,
            "error_message": agent_err,
            "retry_count": agent_retries,
            "final_answer": result["final_answer"]
        }
    except Exception as e:
        error_msg = str(e)
        print(f"ERROR: Exception in confirm endpoint: {error_msg}")
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan saat mengeksekusi SQL: {error_msg}")

@app.get("/api/tables")
def list_tables():
    """
    Get all Olist database tables and row counts using a single fast metadata query.
    """
    try:
        engine = get_engine()
        inspector = inspect(engine)
        tables = inspector.get_table_names() + inspector.get_view_names()
        
        table_counts = {}
        with engine.connect() as conn:
            # Query pg_class to get estimated tuples for all tables at once (saves 9 network round-trips!)
            table_list_str = ", ".join(f"'{t}'" for t in tables)
            query_str = f"""
            SELECT relname AS table_name, reltuples::bigint AS row_count
            FROM pg_class
            WHERE relname IN ({table_list_str})
            """
            res = conn.execute(text(query_str))
            rows = res.fetchall()
            for r in rows:
                table_counts[r.table_name] = r.row_count
                
            # Populate results, falling back to COUNT(*) only if empty or <= 0
            result_list = []
            for table in tables:
                count = table_counts.get(table, 0)
                if count <= 0:
                    fallback_res = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    count = fallback_res.scalar()
                result_list.append({
                    "name": table,
                    "row_count": count
                })
        return result_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tables/{table_name}/data")
def get_table_data(table_name: str, page: int = 1, page_size: int = 50):
    """
    Get ONLY the paginated row data for the live preview.
    """
    try:
        engine = get_engine()
        inspector = inspect(engine)
        tables = inspector.get_table_names() + inspector.get_view_names()
        
        if table_name not in tables:
            raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found.")
            
        preview_rows = []
        total_rows = 0
        with engine.connect() as conn:
            # Count total rows using pg_class estimate for instant load times
            count_res = conn.execute(text(
                f"SELECT reltuples::bigint FROM pg_class WHERE relname = '{table_name}'"
            ))
            total_rows = count_res.scalar()
            if total_rows is None or total_rows <= 0:
                count_res = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
                total_rows = count_res.scalar()
            
            # Fetch paginated rows
            offset = (page - 1) * page_size
            result = conn.execute(text(f"SELECT * FROM {table_name} LIMIT {page_size} OFFSET {offset}"))
            keys = list(result.keys())
            rows = result.fetchall()
            for row in rows:
                row_dict = {}
                for k, v in zip(keys, row):
                    if hasattr(v, 'isoformat'):
                        row_dict[k] = v.isoformat()
                    else:
                        row_dict[k] = v
                preview_rows.append(row_dict)
                
        return {
            "name": table_name,
            "preview_data": preview_rows,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_rows": total_rows
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

schema_cache = {}

def get_cached_schema(table_name: str):
    if table_name in schema_cache:
        return schema_cache[table_name]
        
    engine = get_engine()
    inspector = inspect(engine)
    
    # Get column details
    columns = inspector.get_columns(table_name)
    col_list = []
    for col in columns:
        col_list.append({
            "name": col["name"],
            "type": str(col["type"]),
            "nullable": col["nullable"]
        })
        
    # Get foreign keys
    fks = inspector.get_foreign_keys(table_name)
    fk_list = []
    for fk in fks:
        fk_list.append({
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
            "constrained_columns": fk["constrained_columns"]
        })
        
    schema_cache[table_name] = {
        "columns": col_list,
        "foreign_keys": fk_list
    }
    return schema_cache[table_name]

@app.get("/api/tables/{table_name}")
def get_table_details(table_name: str, page: int = 1, page_size: int = 50):
    """
    Get schemas, columns, constraints, and paginated rows for preview.
    """
    try:
        engine = get_engine()
        inspector = inspect(engine)
        tables = inspector.get_table_names() + inspector.get_view_names()
        
        if table_name not in tables:
            raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found.")
            
        # Get cached schema (columns and foreign keys) - instantaneous!
        schema = get_cached_schema(table_name)
        col_list = schema["columns"]
        fk_list = schema["foreign_keys"]
            
        # Get total row count and fetch paginated rows preview
        preview_rows = []
        total_rows = 0
        with engine.connect() as conn:
            # Count total rows using pg_class estimate for instant load times
            count_res = conn.execute(text(
                f"SELECT reltuples::bigint FROM pg_class WHERE relname = '{table_name}'"
            ))
            total_rows = count_res.scalar()
            if total_rows is None or total_rows <= 0:
                count_res = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
                total_rows = count_res.scalar()
            
            # Fetch paginated rows
            offset = (page - 1) * page_size
            result = conn.execute(text(f"SELECT * FROM {table_name} LIMIT {page_size} OFFSET {offset}"))
            keys = list(result.keys())
            rows = result.fetchall()
            for row in rows:
                row_dict = {}
                for k, v in zip(keys, row):
                    if hasattr(v, 'isoformat'):
                        row_dict[k] = v.isoformat()
                    else:
                        row_dict[k] = v
                preview_rows.append(row_dict)
                
        return {
            "name": table_name,
            "columns": col_list,
            "foreign_keys": fk_list,
            "preview_data": preview_rows,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_rows": total_rows
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class QueryRequest(BaseModel):
    sql: str

@app.post("/api/query")
def run_custom_query(payload: QueryRequest):
    """
    Executes a user-submitted read-only SQL query against AWS RDS.
    """
    try:
        results = execute_query(payload.sql, max_rows=100)
        return {
            "status": "success",
            "data": results
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Fallback to serve static web pages
@app.get("/")
async def get_index():
    return FileResponse(os.path.join(static_dir, "index.html"))

# Mount remaining static directory for style.css, app.js, etc.
app.mount("/", StaticFiles(directory=static_dir), name="static")
