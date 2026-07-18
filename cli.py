import sys
from agent import create_agent_graph

def main():
    print("=" * 60)
    print("           Olist Brazilian E-Commerce SQL Agent")
    print("=" * 60)
    print("Initializing LangGraph Pipeline...")
    
    try:
        graph = create_agent_graph()
        print("SQL Agent initialized! Ask questions in natural language.")
        print("Type 'exit', 'quit', or 'q' to end the session.")
        print("-" * 60)
    except Exception as e:
        print(f"\nError initializing agent: {e}")
        print("Please make sure you have configured valid API keys in your .env file.")
        sys.exit(1)

    # Static CLI session thread_id
    config = {"configurable": {"thread_id": "cli_session_thread"}}

    while True:
        try:
            question = input("\nQuestion: ").strip()
            if not question:
                continue
            if question.lower() in ['exit', 'quit', 'q']:
                print("\nGoodbye!")
                break
                
            print("\nProcessing your query...")
            
            initial_state = {
                "query": question,
                "plan": [],
                "current_task_idx": 0,
                "task_results": {},
                "final_answer": "",
                "eval_feedback": "",
                "eval_retries": 0,
                "chat_history": []
            }
            
            # Start execution (will interrupt before query_runner if SQL is generated)
            result = graph.invoke(initial_state, config)
            
            # Loop while there is an active SQL query awaiting human approval (handles multi-task plans & retries)
            while True:
                state_info = graph.get_state(config)
                if not (state_info.next and "query_runner" in state_info.next):
                    break
                    
                plan = state_info.values.get("plan", [])
                idx = state_info.values.get("current_task_idx", 0)
                active_task = plan[idx] if idx < len(plan) else (plan[-1] if plan else {})
                agent_sql = active_task.get("sql_query", "")
                
                print("-" * 60)
                print("Generated SQL Query awaiting approval:")
                print(f"  {agent_sql}")
                print("-" * 60)
                
                confirm = input("Execute this query? (y/n): ").strip().lower()
                if confirm in ['y', 'yes']:
                    print("Executing SQL query...")
                    result = graph.invoke(None, config)
                else:
                    print("Canceling SQL execution...")
                    current_plan = state_info.values.get("plan", [])
                    if idx < len(current_plan):
                        current_plan[idx]["status"] = "failed"
                        current_plan[idx]["error_message"] = "Eksekusi SQL dibatalkan oleh pengguna."
                    graph.update_state(config, {
                        "plan": current_plan,
                        "final_answer": "Eksekusi SQL dibatalkan oleh pengguna."
                    })
                    # Resume graph execution to process the cancellation path and clear the interrupt
                    result = graph.invoke(None, config)
                    break
            
            print("-" * 60)
            print("Answer:")
            print(result.get("final_answer", ""))
            print("=" * 60)
            
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except Exception as e:
            print(f"\nAn error occurred while running the agent: {e}")
            print("=" * 60)

if __name__ == "__main__":
    main()
