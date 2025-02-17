import json
import os
from string import Template
from typing import Callable, Dict, List, Optional, Tuple, Union

import openai
from google.cloud import bigquery
from guardrails.document_store import DocumentStoreBase, EphemeralDocumentStore
from guardrails.embedding import EmbeddingBase, OpenAIEmbedding
from guardrails.guard import Guard
from guardrails.llm_providers import PromptCallable
from guardrails.utils.sql_utils import create_sql_driver
from guardrails.vectordb import Faiss, VectorDBBase

from chartgpt.guardrails.validators import BugFreeBigQuerySQL

REASK_PROMPT = """
You are a data scientist whose job is to write SQL queries.

@complete_json_suffix_v2

Here's schema about the database that you can use to generate the SQL query.
Try to avoid using joins if the data can be retrieved from the same table.

{{db_info}}

I will give you a list of examples.

{{examples}}

I want to create a query for the following instruction:

{{nl_instruction}}

For this instruction, I was given the following JSON, which has some incorrect values.

{previous_response}

Help me correct the incorrect values based on the given error messages.
"""


EXAMPLE_BOILERPLATE = """
I will give you a list of examples. Write a GoogleSQL query similar to the examples below:
"""


def example_formatter(
    input: str, output: str, output_schema: Optional[Callable] = None
) -> str:
    if output_schema is not None:
        output = output_schema(output)

    example = "\nINSTRUCTIONS:\n============\n"
    example += f"{input}\n\n"

    example += "SQL QUERY:\n================\n"
    example += f"{output}\n\n"

    return example


class Text2Sql:
    def __init__(
        self,
        client: bigquery.Client,
        sql_schema: str,
        examples: Optional[Dict] = None,
        embedding: Optional[EmbeddingBase] = OpenAIEmbedding,
        vector_db: Optional[VectorDBBase] = Faiss,
        document_store: Optional[DocumentStoreBase] = EphemeralDocumentStore,
        rail_spec: Optional[str] = None,
        rail_params: Optional[Dict] = None,
        example_formatter: Optional[Callable] = example_formatter,
        reask_prompt: Optional[str] = REASK_PROMPT,
        llm_api: Optional[PromptCallable] = openai.Completion.create,
        llm_api_kwargs: Optional[Dict] = None,
        num_relevant_examples: int = 2,
    ):
        """Initialize the text2sql application.

        Args:
            conn_str: Connection string to the database.
            schema_file: Path to the schema file. Defaults to None.
            examples: Examples to add to the document store. Defaults to None.
            embedding: Embedding to use for document store. Defaults to OpenAIEmbedding.
            vector_db: Vector database to use for the document store. Defaults to Faiss.
            document_store: Document store to use. Defaults to EphemeralDocumentStore.
            rail_spec: Path to the rail specification. Defaults to "text2sql.rail".
            example_formatter: Fn to format examples. Defaults to example_formatter.
            reask_prompt: Prompt to use for reasking. Defaults to REASK_PROMPT.
        """

        self.example_formatter = example_formatter
        self.llm_api = llm_api
        self.llm_api_kwargs = llm_api_kwargs or {"max_tokens": 512}

        # Initialize the SQL driver.
        # self.sql_driver = create_sql_driver(conn=conn_str, schema_file=schema_file)
        # self.sql_schema = self.sql_driver.get_schema()
        self.client = client
        self.sql_schema = sql_schema

        # Number of relevant examples to use for the LLM.
        self.num_relevant_examples = num_relevant_examples

        # Initialize the Guard class.
        self.guard = self._init_guard(
            rail_spec,
            rail_params,
            reask_prompt,
        )

        # Initialize the document store.
        self.store = self._create_docstore_with_examples(
            examples, embedding, vector_db, document_store
        )

    def _init_guard(
        self,
        client: bigquery.Client,
        rail_spec: Optional[str] = None,
        rail_params: Optional[Dict] = None,
        reask_prompt: Optional[str] = REASK_PROMPT,
    ):
        # Initialize the Guard class
        if rail_spec is None:
            rail_spec = os.path.join(os.path.dirname(__file__), "text2sql.rail")
            rail_params = None  # {"client": client}

        # Load the rail specification.
        with open(rail_spec, "r") as f:
            rail_spec_str = f.read()

        # Substitute the parameters in the rail specification.
        if rail_params is not None:
            rail_spec_str = Template(rail_spec_str).safe_substitute(**rail_params)

        guard = Guard.from_rail_string(rail_spec_str)
        guard.reask_prompt = reask_prompt

        return guard

    def _create_docstore_with_examples(
        self,
        examples: Optional[Dict],
        embedding: EmbeddingBase,
        vector_db: VectorDBBase,
        document_store: DocumentStoreBase,
    ) -> Optional[DocumentStoreBase]:
        if examples is None:
            return None

        """Add examples to the document store."""
        e = embedding()
        if vector_db == Faiss:
            db = Faiss.new_flat_l2_index(e.output_dim, embedder=e)
        else:
            raise NotImplementedError(f"VectorDB {vector_db} is not implemented.")
        store = document_store(db)
        store.add_texts(
            {example["question"]: {"ctx": example["query"]} for example in examples}
        )
        return store

    @staticmethod
    def output_schema_formatter(output) -> str:
        return json.dumps({"generated_sql": output}, indent=4)

    def __call__(self, text: str) -> str:
        """Run text2sql on a text query and return the SQL query."""

        if self.store is not None:
            similar_examples = self.store.search(text, self.num_relevant_examples)
            similar_examples_prompt = "\n".join(
                self.example_formatter(example.text, example.metadata["ctx"])
                for example in similar_examples
            )
        else:
            similar_examples_prompt = ""

        try:
            # raw_response, validated_response = self.guard(
            #     self.llm_api,
            #     prompt_params={
            #         "nl_instruction": text,
            #         "examples": similar_examples_prompt,
            #         "db_info": str(self.sql_schema),
            #     },
            #     num_reasks=10,
            #     **self.llm_api_kwargs,
            # )
            raw_response, validated_response = self.guard(
                # self.llm_api,
                openai.ChatCompletion.create,
                prompt_params={
                    "nl_instruction": text,
                    "examples": similar_examples_prompt,
                    "db_info": str(self.sql_schema),
                },
                model="gpt-4",
                num_reasks=10,
                max_tokens=2048,
                temperature=0.5,
                # **self.llm_api_kwargs,
            )
            print(raw_response)
            print(validated_response)
            output = validated_response.get("generated_sql", None)
            # output = raw_output[1].get("generated_sql", None)
        except TypeError:
            output = None

        return output


# openai.ChatCompletion.create,
#     prompt_params={"document": content[:6000]},
#     model="gpt-3.5-turbo",
#     max_tokens=2048,
#     temperature=0,
