# #create the agent first using the create_agent base harness in langchain and configure the harness using the first-class primitives
# from __future__ import annotations
# import asyncio
# import logging

# logger = logging.getLogger(__name__)
# from langchain.agents.middleware import TodoListMiddleware as _planning_tools, HumanInTheLoopMiddleware as _steering
# from deepagents.middleware import (
#     FilesystemMiddleware as _execution_environment,
#     SkillsMiddleware as _skills,
#     SummarizationMiddleware as _context_compression,
#     MemoryMiddleware as _memory
# )
# from langchain.agents import create_agent as _create_agent_harness
# from deepagents.middleware import SubAgentMiddleware as _subagents
# from langchain.agents.middleware import ToolRetryMiddleware, PIIMiddleware, ModelRetryMiddleware
# from langchain.agents import AgentState as _baseAgentState
# from datetime_tools import date_time_tools as iris_temporal_tools     
# from agent_memory import memory_backend, memory_store
# from subagent_config import build_subagent_config 
# from loadenv import orchestrator_model as _chat_model
# from PROMPTS import ORCHESTRATOR_PROMPT
# from requests.exceptions import RequestException, Timeout
# from aiohttp.client_exceptions import SocketTimeoutError as AiohttpSocketTimeout, ServerTimeoutError as AiohttpServerTimeout
# from pathlib import Path

# # create the file directory path

# file_dir = Path(__file__).parent
# # creating the main IRIS AI agent using the langchain base harness : create_agent() harness

# # step 1: load the subagent using the async function
# async def load_subagents():
#     agents = await build_subagent_config()
#     return agents

# # step2: create the orchestrator tools 
# iris_tools  = iris_temporal_tools

# # create the custom function for the model retry middleware
# def format_error(exc: Exception) -> str:
#     return "Model temporarily unavailable. Please try again later."

# retry = ModelRetryMiddleware(
#                 max_retries=4,
#                 on_failure=format_error,
#                 )

# # step 3: define a function to create the IRIS.AI using the create_agent harness and configure the harness using langchain's first class primitives
# async def create_iris_agent():
#     agents = await load_subagents()
#     # create the agent 
#     return _create_agent_harness(
#         model= _chat_model,
#         tools= iris_tools,
#         system_prompt=ORCHESTRATOR_PROMPT,
#         middleware=[
#             # first the harness gives the model an execution environment via the filesystem middleware
#             _execution_environment(backend=memory_backend),
#             #the harness provides the right context to the model at the right time through the memory middleware and the skills middleware + prompt caching + sumamarization middleware
#             _skills(backend=memory_backend, sources=[str(file_dir / "skills")],),
#             _memory(backend=memory_backend, sources=[str(file_dir / "IRIS.md"), str(file_dir / "agent.md")],),
#             _context_compression(model=_chat_model,
#             backend=memory_backend,
#             trigger=("tokens", 100000),
#             keep=("messages", 20),
#             ),
#             # the harness provides the model with the planning tools via  todo middleware
#             _planning_tools(),
#             # the harness delegates to specialized subagents for context isolation to keep the context window of the main agent lean via subagent middleware
#             _subagents(backend=memory_backend, subagents=agents),
#             # using the steering, the model can pause for humans to approve high-stakes irreversible actions using the human in the loop middleware 
#             _steering(interrupt_on={
#                 "send_email": True,
#                 "gmail_send_message": True,
#                 "share_file": True,
#                 "drive_share_file": True,
#                 "delete_attio_record": True,
#             }),
#             # these are for handling errors, rate limits and timeouts and reduce latency
#             ToolRetryMiddleware(
#                 max_retries=3,
#                 # Also retry on aiohttp socket/server timeouts that occur
#                 # when MCP stdio servers (npx) are slow to start or respond.
#                 retry_on=(RequestException, Timeout, AiohttpSocketTimeout, AiohttpServerTimeout),
#                 backoff_factor=1.5,
#             ),
#             # this is to handle personally identifiable information — masks credit cards, blocks ip addresses
#             PIIMiddleware("credit_card", strategy="mask"),
#             PIIMiddleware("ip", strategy="block"),
#             # inject the model retry policy in case of model failure
#             retry,

#         ],
#         # inject the agentstate schema
#         state_schema=_baseAgentState,
#         store= memory_store
#     )

# # Synchronous helper to get or create IRIS_ai instance
# def get_iris_agent():
#     """Lazily instantiate IRIS agent synchronously if needed."""
#     try:
#         loop = asyncio.get_running_loop()
#         if loop.is_running():
#             return loop.create_task(create_iris_agent())
#     except RuntimeError:
#         pass
#     return asyncio.run(create_iris_agent())

# # Module-level variable (lazy initialized on demand)
# IRIS_ai = None


