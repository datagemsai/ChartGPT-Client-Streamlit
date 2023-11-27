import inspect
import re
from typing import Any, Dict, List, Optional

from google.cloud import bigquery
from langchain.agents.agent import AgentExecutor
from langchain.callbacks.base import BaseCallbackManager
from langchain.chains.llm import LLMChain
from langchain.llms.base import BaseLLM
from langchain.schema import BaseMemory

import api.utils as utils
from config.datasets import Dataset
from chartgpt.agents.agent_toolkits.bigquery.prompt import PREFIX, SUFFIX
from chartgpt.agents.agent_toolkits.bigquery.utils import (get_example_query,
                                                           get_tables_summary)
from chartgpt.agents.mrkl.base import CustomAgent
from chartgpt.agents.mrkl.output_parser import CustomOutputParser
from chartgpt.tools.python.tool import PythonAstREPLTool


def create_bigquery_agent(
    llm: BaseLLM,
    bigquery_client: bigquery.Client,
    datasets: List[Dataset],
    callback_manager: Optional[BaseCallbackManager] = None,
    prefix: str = PREFIX,
    suffix: str = SUFFIX,
    input_variables: Optional[List[str]] = None,
    verbose: bool = False,
    return_intermediate_steps: bool = False,
    max_iterations: Optional[int] = 15,
    max_execution_time: Optional[float] = None,
    early_stopping_method: str = "force",
    agent_executor_kwargs: Optional[Dict[str, Any]] = None,
    memory: Optional[BaseMemory] = None,
    secure_execution: bool = True,
    **kwargs: Any,
) -> AgentExecutor:
    tables_summary = get_tables_summary(client=bigquery_client, datasets=datasets)
    example_query = get_example_query(datasets=datasets)
    python_tool = PythonAstREPLTool(
        secure_execution=secure_execution,
        locals={"tables_summary": tables_summary, "bigquery_client": bigquery_client},
    )

    def query_post_processing(query: str) -> str:
        query = query.replace("print(", "display(")
        imports = inspect.cleandoc(
            """
        import streamlit as st
        import plotly.express as px
        import plotly.graph_objects as go
        import pandas as pd
        import numpy as np

        pd.set_option('display.max_columns', None)
        pd.set_option('display.max_rows', 5)

        def display(*args):
            import streamlit as st
            st.write(*args)
            return args
        """
        )
        query = imports + "\n\n" + query
        query = re.sub(".*client =.*\n?", "client = bigquery_client", query)
        query = re.sub(".*bigquery_client =.*\n?", "", query)
        return query

    python_tool.query_post_processing = query_post_processing
    tools = [python_tool]

    if input_variables is None:
        input_variables = [
            "database_schema",
            "tables_summary",
            "example_query",
            "input",
            "agent_scratchpad",
        ]
    if memory is not None:
        input_variables.append("chat_history")

    prompt = CustomAgent.create_prompt(
        tools,
        prefix=prefix,
        suffix=suffix,
        input_variables=input_variables,
    )

    dataset_schemas = []
    for dataset in datasets:
        table_id = dataset.tables[0] if dataset.tables else ""
        schema = utils.get_tables_summary(
            client=bigquery_client,
            data_source_url=f"bigquery/{dataset.project}/{dataset.id}/{table_id}",
        )
        dataset_schemas.append(schema)

    tables_summary_escaped = "{" + str(dict(tables_summary)) + "}"
    partial_prompt = prompt.partial(
        database_schema="\n\n".join(dataset_schemas),
        tables_summary=tables_summary_escaped,
        example_query=example_query,
    )

    llm_chain = LLMChain(
        llm=llm,
        prompt=partial_prompt,
    )
    tool_names = [tool.name for tool in tools]
    agent = CustomAgent(
        llm_chain=llm_chain,
        allowed_tools=tool_names,
        **kwargs,
    )
    return AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=tools,
        verbose=verbose,
        return_intermediate_steps=return_intermediate_steps,
        max_iterations=max_iterations,
        max_execution_time=max_execution_time,
        early_stopping_method=early_stopping_method,
        callback_manager=callback_manager,
        output_parser=CustomOutputParser,
        memory=memory,
        **(agent_executor_kwargs or {}),
    )
